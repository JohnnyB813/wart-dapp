"""Explorer contract, transport, persistence, and GUI tests use synthetic data."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

import explorer_client
import explorer_history
import main
from db import DB
from explorer_client import ExplorerClient, ExplorerError, _RequestBudget
from explorer_history import ExplorerCursor, NodeCursor, fetch_wallet_page, explorer_record
from history import LATEST_CURSOR
from node_client import NodeClient, NodeError
from utils import SignedTransfer, TransferIntent, wallet_from_pkhex, MAX_UINT64
from wallet_settings import DEFAULTS, load_preferences, save_preferences, validate_preferences
import test_sessions as sessions
from test_sessions import wait_for

APP = QApplication.instance() or QApplication([])
OWNER = sessions.ADDRESS
OTHER = wallet_from_pkhex('00' * 31 + '02')[0]


def metadata(network='mainnet', complete=True):
    return dict(network=network, epoch=network + ':fixture', enabled=True, stale=False, source='index',
                observedAt='2026-10-28T12:00:00Z',
                coverage=dict(tip=60, indexedTip=60, complete=complete, indexerAlive=True, backfillNext=None))


def transaction(reward=False, tx_hash='ab' * 32, height=50):
    return dict(hash=tx_hash, type='reward' if reward else 'wartTransfer',
                height=height, timestamp=1700000000, sender=None if reward else OTHER, recipient=OWNER,
                amount=dict(raw='150000001', decimals=8, formatted='deliberately ignored'),
                fee=None if reward else dict(raw='9992', decimals=8, formatted='ignored'), assetHash=None)


def response(items=None, cursor=None, network='mainnet', complete=True):
    return dict(data=dict(items=[transaction()] if items is None else items, nextCursor=cursor),
                meta=metadata(network, complete))


class ExplorerContractTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(explorer_client, '_BUDGET', _RequestBudget())
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, envelope, network='mainnet', status=200, epoch=None):
        client = ExplorerClient(network)
        with patch.object(client.transport, '_exchange', return_value=(status, json.dumps(envelope).encode())):
            return client.request('/transactions', dict(address=OWNER, limit=100), epoch=epoch)

    def test_exact_amounts_both_networks_and_native_action_filter(self):
        for network in ('mainnet', 'testnet'):
            meta = metadata(network)
            tx = transaction()
            tx['amount']['raw'] = str(MAX_UINT64)
            row = explorer_record(tx, OWNER, meta)
            self.assertEqual(row['amount_e8'], str(MAX_UINT64))
            self.assertEqual((row['network'], row['confirmations'], row['direction']), (network, 11, 'incoming'))
            self.assertEqual(explorer_record(transaction(True), OWNER, meta)['direction'], 'mined')
            self.assertIsNone(explorer_record(dict(type='tokenTransfer'), OWNER, meta))
        for raw in ('-1', '1.5', str(MAX_UINT64 + 1), 1, True):
            tx = transaction()
            tx['amount']['raw'] = raw
            with self.subTest(raw=raw), self.assertRaises(NodeError):
                explorer_record(tx, OWNER, metadata())
        for changes in ({'recipient': OTHER}, {'height': 61}, {'timestamp': None}, {'sender': None}):
            with self.subTest(changes=changes), self.assertRaises(NodeError):
                explorer_record(dict(transaction(), **changes), OWNER, metadata())

    def test_stale_disabled_wrong_network_and_lagging_index_are_rejected(self):
        for changes in ({'stale': True}, {'enabled': False}, {'network': 'testnet'}, {'source': 'snapshot'},
                        {'epoch': None}, {'coverage': dict(metadata()['coverage'], indexedTip=59)},
                        {'coverage': dict(metadata()['coverage'], indexerAlive=False)}):
            envelope = response()
            envelope['meta'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ExplorerError):
                self.request(envelope)

    def test_epoch_change_and_chain_reset_are_explicit(self):
        with self.assertRaisesRegex(ExplorerError, 'chain reset'):
            self.request(response(), epoch='old-epoch')
        with self.assertRaises(ExplorerError) as caught:
            self.request({'error': {'code': 'CHAIN_RESET', 'message': 'reset'}}, status=409)
        self.assertEqual(caught.exception.code, 'CHAIN_RESET')

    def test_429_honors_retry_after_and_blocks_both_networks_without_waiting(self):
        client = ExplorerClient('mainnet')
        client.transport.retry_after = '12'
        with patch.object(client.transport, '_exchange', return_value=(429, b'{}')) as exchange:
            with self.assertRaisesRegex(ExplorerError, 'rate limit'):
                client.request('/transactions')
        self.assertGreaterEqual(explorer_client._BUDGET.cooldown_until - time.monotonic(), 11)
        other = ExplorerClient('testnet')
        with patch.object(other.transport, '_exchange') as exchange:
            started = time.monotonic()
            with self.assertRaisesRegex(ExplorerError, 'cooling down'):
                other.request('/transactions')
            self.assertLess(time.monotonic() - started, 0.2)
            exchange.assert_not_called()

    def test_rate_gate_cancellation_and_get_only_contract(self):
        cancel = Event()
        cancel.set()
        client = ExplorerClient('mainnet', cancel=cancel)
        with self.assertRaisesRegex(NodeError, 'cancelled'):
            client.request('/transactions')
        for path in ('/transaction/add', '/transactions?injected=true', '//other-host'):
            with self.assertRaises(ExplorerError):
                ExplorerClient('mainnet').request(path)

    def test_actual_qt_http_transport_encodes_cursors_and_never_posts(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                body = json.dumps(response()).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ExplorerClient('mainnet')
            client.transport.peer = f'http://127.0.0.1:{server.server_port}'
            data, meta = client.request('/transactions', dict(address=OWNER, cursor='a+/=&?'))
            self.assertEqual(data['items'][0]['hash'], 'ab' * 32)
            self.assertEqual(urlsplit(paths[0]).path, '/api/v1/mainnet/transactions')
            self.assertEqual(parse_qs(urlsplit(paths[0]).query)['cursor'], ['a+/=&?'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


class ExplorerPagingTests(unittest.TestCase):
    def setUp(self):
        self.node = NodeClient('http://localhost:3000')

    def fetch(self, envelope, cursor=LATEST_CURSOR, known=(), rewards=False):
        with patch.object(ExplorerClient, 'request', return_value=(envelope['data'], envelope['meta'])):
            return fetch_wallet_page(self.node, OWNER, cursor, known, rewards)

    def test_indexed_history_avoids_node_and_header_requests(self):
        with patch.object(self.node, 'request', side_effect=AssertionError('extra node request')):
            page = self.fetch(response(cursor='opaque+/='))
        self.assertEqual((len(page.rows), page.source), (1, 'explorer'))
        self.assertEqual(page.cursor, ExplorerCursor('opaque+/=', 'mainnet:fixture', OWNER, 'mainnet', False))
        with patch.object(ExplorerClient, 'request', return_value=(response([])['data'], metadata())) as request:
            last = fetch_wallet_page(self.node, OWNER, page.cursor)
            self.assertEqual(request.call_args.args[1]['cursor'], 'opaque+/=')
            self.assertEqual(request.call_args.kwargs['epoch'], 'mainnet:fixture')
            self.assertEqual(last.cursor, 0)

    def test_incomplete_end_offers_node_history_without_repeating_explorer(self):
        page = self.fetch(response([], complete=False))
        self.assertIsInstance(page.cursor, NodeCursor)
        self.assertIn('incomplete', page.notice)
        with patch.object(self.node, 'require_network'), \
             patch.object(explorer_history, 'fetch_account_page', return_value=([], 20)) as fallback, \
             patch.object(ExplorerClient, 'request') as explorer:
            next_page = fetch_wallet_page(self.node, OWNER, page.cursor)
        fallback.assert_called_once_with(self.node, OWNER, LATEST_CURSOR)
        explorer.assert_not_called()
        self.assertEqual(next_page.cursor, NodeCursor(20))

    def test_outage_and_malformed_page_restart_node_pagination(self):
        cursor = ExplorerCursor('old', 'mainnet:fixture', OWNER, 'mainnet', False)
        bad = response()
        bad['data']['nextCursor'] = 'old'
        for effect in (ExplorerError('offline'), None):
            with patch.object(ExplorerClient, 'request', side_effect=effect,
                              return_value=(bad['data'], bad['meta'])), \
                 patch.object(self.node, 'require_network'), \
                 patch.object(explorer_history, 'fetch_account_page', return_value=([], 20)) as fallback:
                page = fetch_wallet_page(self.node, OWNER, cursor)
                self.assertEqual((page.source, page.cursor), ('node', NodeCursor(20)))
                self.assertIn('restarted', page.notice)
                fallback.assert_called_once_with(self.node, OWNER, LATEST_CURSOR)

    def test_cursor_cannot_cross_wallet_network_or_view(self):
        for cursor in (ExplorerCursor('x', 'e', OTHER, 'mainnet', False),
                       ExplorerCursor('x', 'e', OWNER, 'testnet', False),
                       ExplorerCursor('x', 'e', OWNER, 'mainnet', True)):
            with patch.object(ExplorerClient, 'request') as request, self.assertRaises(ExplorerError):
                fetch_wallet_page(self.node, OWNER, cursor)
            request.assert_not_called()

    def test_rewards_filter_skips_repeated_saved_lookups_but_checks_missing_rewards(self):
        tx = transaction(True)
        known = explorer_record(tx, OWNER, metadata())
        with patch.object(ExplorerClient, 'request', return_value=(response([tx])['data'], metadata())) as request, \
             patch.object(self.node, 'lookup') as lookup:
            page = fetch_wallet_page(self.node, OWNER, known=[known], rewards=True)
        self.assertEqual(request.call_args.args[1]['type'], 'reward')
        lookup.assert_not_called()
        self.assertEqual(page.rows[0]['direction'], 'mined')
        missing = dict(known, tx_hash='cd' * 32, block_height=51)
        with patch.object(self.node, 'require_network'), patch.object(self.node, 'lookup', return_value=None) as lookup:
            page = self.fetch(response([tx]), known=[known, missing], rewards=True)
        lookup.assert_called_once_with(missing['tx_hash'])
        self.assertEqual(page.rows[-1]['status'], 'not_found')

    def test_incomplete_empty_rewards_do_not_invalidate_saved_rewards(self):
        known = explorer_record(transaction(True), OWNER, metadata())
        with patch.object(self.node, 'lookup') as lookup:
            self.fetch(response([], complete=False), known=[known], rewards=True)
        lookup.assert_not_called()


class ExplorerPersistenceTests(unittest.TestCase):
    def test_explorer_cannot_settle_local_or_pending_payments_even_after_concurrent_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wallet.db'
            store, other_process = DB(path), DB(path)
            try:
                tx = transaction(tx_hash='ab' * 32)
                tx.update(sender=OWNER, recipient=OTHER)
                observation = explorer_record(tx, OWNER, metadata())
                # The worker obtained its response before another process reserved this ID.
                other_process.record_outgoing(OWNER, TransferIntent(OTHER, 150000001, 9992),
                                              SignedTransfer('ab' * 32, {}), 'http://localhost:3000')
                before = store.reserved_amount(OWNER)
                store.save_explorer_history([observation])
                self.assertEqual(store.reserved_amount(OWNER), before)
                self.assertEqual(store.history(OWNER)[0]['status'], 'broadcasting')
                store.set_tx_status(OWNER, 'ab' * 32, 'confirmed')
                store.save_explorer_history([dict(observation, confirmations=99)])
                self.assertEqual(store.history(OWNER)[0]['confirmations'], 0)
                store.save_explorer_history([explorer_record(transaction(True, 'cd' * 32), OWNER, metadata())])
                self.assertEqual(len(store.history(OWNER)), 2)
            finally:
                other_process.con.close()
                store.con.close()

    def test_old_preferences_migrate_and_explorer_can_be_disabled(self):
        old = {key: value for key, value in DEFAULTS.items() if key != 'explorer_history'}
        self.assertTrue(validate_preferences(old)['explorer_history'])
        with self.assertRaises(ValueError):
            validate_preferences(dict(DEFAULTS, explorer_history='true'))


class ExplorerGuiTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    tearDown = sessions.SessionTests.tearDown

    def enable(self):
        save_preferences(main.db, dict(load_preferences(main.db), explorer_history=True))
        self.window.apply_preferences()

    def test_gui_history_uses_explorer_and_source_preference_resets_cursor(self):
        self.enable()
        with patch.object(ExplorerClient, 'request', return_value=(response(cursor='next')['data'], metadata())), \
             patch.object(main.NodeClient, 'require_network') as network, \
             patch.object(main, 'fetch_account_page') as scan:
            self.window.list_transactions()
            wait_for(lambda: not self.window.jobs.running('history'))
        self.assertEqual(self.window.history_panel.table.rowCount(), 1)
        self.assertIn('WartNode Explorer', self.window.history_panel.notice.text())
        self.assertIsInstance(self.window.history_cursor, ExplorerCursor)
        network.assert_not_called()
        scan.assert_not_called()
        panel = self.window.settings_panel
        panel.explorer_history.setChecked(False)
        panel.save_preferences()
        self.assertFalse(self.window.preferences['explorer_history'])
        self.assertEqual(self.window.history_cursor, LATEST_CURSOR)

    def test_gui_failed_node_check_does_not_allow_explorer_to_release_reservation(self):
        self.enable()
        main.db.record_outgoing(OWNER, TransferIntent(OTHER, 150000001, 9992),
                                SignedTransfer('ab' * 32, {}), main.PEER)
        tx = transaction()
        tx.update(sender=OWNER, recipient=OTHER)
        with patch.object(main.NodeClient, 'require_network'), \
             patch.object(main.NodeClient, 'lookup', side_effect=NodeError('node offline')), \
             patch.object(ExplorerClient, 'request', return_value=(response([tx])['data'], metadata())):
            self.window.list_transactions()
            wait_for(lambda: not self.window.jobs.running('history'))
        self.assertEqual(main.db.history(OWNER)[0]['status'], 'broadcasting')
        self.assertEqual(main.db.reserved_amount(OWNER), 150009993)
        self.assertIn('Direct status check failed', self.window.history_panel.notice.text())

    def test_gui_rewards_load_only_selected_block_header_and_keep_hash_on_refresh(self):
        self.enable()
        tx = transaction(True)
        with patch.object(ExplorerClient, 'request', return_value=(response([tx])['data'], metadata())), \
             patch.object(main.NodeClient, 'request', return_value={'header': {'timestamp': 1700000000, 'hash': 'ef' * 32}}) as request:
            self.window.list_mined_blocks()
            wait_for(lambda: not self.window.jobs.running('mined') and not self.window.jobs.running('mined_header'))
            self.assertEqual(self.window.mined_panel.table.rowCount(), 1)
            self.window.mined_panel.copy_hash()
            self.assertEqual(APP.clipboard().text(), 'ef' * 32)
            request.assert_called_once_with('/chain/block/50/header')
            self.window.list_mined_blocks()
            wait_for(lambda: not self.window.jobs.running('mined'))
            request.assert_called_once()

    def test_gui_lock_discards_late_explorer_response(self):
        self.enable()
        started, finish = Event(), Event()

        def delayed(*args, **kwargs):
            started.set()
            finish.wait(3)
            return response()['data'], metadata()

        with patch.object(ExplorerClient, 'request', side_effect=delayed):
            self.window.list_transactions()
            wait_for(started.is_set)
            self.window.lock_wallet()
            finish.set()
            self.window.jobs.pool.waitForDone(4000)
            APP.processEvents()
        self.assertEqual(main.db.history(OWNER), [])
        self.assertEqual(self.window.history_panel.table.rowCount(), 0)
