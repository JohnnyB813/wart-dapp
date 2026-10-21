"""Fees, legacy API fallback, configured-node selection, and settings persistence."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStyle, QStyleOptionButton

import main
import node_pool
import utils
from db import DB
from node_client import NodeClient, NodeError
from payments_ui import SendPanel
from wallet_settings import DEFAULTS, load_preferences, save_preferences
import test_sessions as sessions
from test_sessions import wait_for

OWNER = sessions.ADDRESS
OTHER = utils.wallet_from_pkhex('00' * 31 + '02')[0]
CHEAP = 'http://192.168.1.55:3000'


def state(peer, fee=9992, network='mainnet', synced=True):
    result = dict(peer=peer, network=network, synced=synced, height=64, version='fixture', checked_at=time.time())
    if fee is not None:
        result['min_fee_e8'] = fee
    else:
        result['fee_error'] = 'HTTP 404: endpoint unavailable'
    return result


class FeeTests(unittest.TestCase):
    def test_mainnet_and_defi_fee_formats_are_exact_and_validated(self):
        client = NodeClient('http://localhost:3000')
        for response in ({'E8': 9992, 'amount': '0.00009992'}, {'minFee': {'E8': 9992}}, {'E8': 0}):
            with patch.object(client, 'request', return_value=response):
                self.assertEqual(client.minimum_fee(), response.get('minFee', response)['E8'])
        for value in (None, True, '9992', -1, 1 << 64, 100010):
            with patch.object(client, 'request', return_value={'E8': value}), self.assertRaises(NodeError):
                client.minimum_fee()

    def test_automatic_fee_refresh_and_manual_override_and_unknown_default(self):
        panel = SendPanel()
        try:
            panel.set_node_fee(1000448)
            self.assertEqual(panel.fee.text(), '0.01000448')
            panel.set_node_fee(9992)
            self.assertEqual(panel.fee.text(), '0.00009992')
            panel.use_minimum.setChecked(False)
            panel.fee.setText('0.05')
            panel.set_node_fee(1000448)
            self.assertEqual(panel.fee.text(), '0.05')
            panel.set_node_fee(None, 'Endpoint unavailable')
            self.assertEqual(panel.fee.text(), '0.05')
            panel.use_minimum.setChecked(True)
            self.assertEqual(panel.fee.text(), '0.00009992')
            self.assertIn('fallback', panel.fee_hint.text())
        finally:
            panel.deleteLater()


class NodeSelectionTests(unittest.TestCase):
    def test_cheapest_verified_synced_known_fee_wins_and_ties_keep_first(self):
        peers = ['https://unknown.example', 'https://wrong.example', 'https://syncing.example',
                 'https://expensive.example', 'https://cheap.example', 'https://equal.example']
        states = [state(peers[0], None), state(peers[1], 0, 'testnet'), state(peers[2], 0, synced=False),
                  state(peers[3], 100032), state(peers[4], 9992), state(peers[5], 9992)]
        with patch.object(node_pool, 'NodeClient', side_effect=lambda peer, **kw: MagicMock(probe=lambda: states[peers.index(peer)])):
            result = node_pool.probe_nodes(peers, 'mainnet', lowest_fee=True)
        self.assertEqual(result['peer'], peers[4])

    def test_all_unknown_fees_use_priority_and_cancellation_stops_selection(self):
        peers = ['https://older.example', 'https://other.example']
        with patch.object(node_pool, 'NodeClient', side_effect=lambda peer, **kw: MagicMock(probe=lambda: state(peer, None))):
            result = node_pool.probe_nodes(peers, 'mainnet', lowest_fee=True)
            self.assertEqual(result['peer'], peers[0])
            self.assertNotIn('min_fee_e8', result)
            cancel = Event()
            cancel.set()
            with self.assertRaisesRegex(NodeError, 'cancelled'):
                node_pool.probe_nodes(peers, 'mainnet', cancel, lowest_fee=True)

    def test_real_http_selection_changes_with_fees_and_accepts_older_nodes(self):
        servers, fees, threads = [], [100032, 9992, None], []
        def handler(index):
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == '/transaction/minfee' and fees[index] is None:
                        self.send_error(404)
                        return
                    if self.path == '/chain/head':
                        data = dict(height=64, pinHeight=32, pinHash='00' * 32, synced=True)
                    elif self.path.startswith('/chain/mine/'):
                        data = {'testnet': False}
                    elif self.path.endswith('/balance'):
                        data = {'balance': '10.00000000'}
                    elif self.path == '/tools/version':
                        data = {'name': 'older-node'}
                    else:
                        data = {'E8': fees[index]}
                    body = json.dumps({'code': 0, 'data': data}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass
            return Handler
        try:
            for i in range(3):
                server = ThreadingHTTPServer(('127.0.0.1', 0), handler(i))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                servers.append(server)
                threads.append(thread)
            peers = [f'http://127.0.0.1:{server.server_port}' for server in servers]
            self.assertEqual(node_pool.probe_nodes(peers, 'mainnet', lowest_fee=True)['peer'], peers[1])
            fees[1] = 200064
            self.assertEqual(node_pool.probe_nodes(peers, 'mainnet', lowest_fee=True)['peer'], peers[0])
            older = NodeClient(peers[2]).probe()
            self.assertEqual((older['network'], older['synced']), ('mainnet', True))
            self.assertNotIn('min_fee_e8', older)
            self.assertIn('fee_error', older)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(2)


class SettingsStorageTests(unittest.TestCase):
    def test_legacy_three_column_node_profiles_migrate_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'older.db'
            original = DB(path)
            with original.con:
                original.con.execute('ALTER TABLE node_profiles RENAME TO newer_profiles')
                original.con.execute('CREATE TABLE node_profiles(name TEXT PRIMARY KEY, url TEXT NOT NULL, network TEXT NOT NULL)')
                original.con.execute('INSERT INTO node_profiles SELECT name, url, network FROM newer_profiles')
                original.con.execute('DROP TABLE newer_profiles')
            backup = original.snapshot()
            original.con.close()
            migrated, restored = DB(path), DB(':memory:')
            try:
                self.assertTrue(all(node['enabled'] == 1 for node in migrated.configured_nodes('mainnet')))
                restored.restore_snapshot(backup)
                # This legacy schema has no stored order; destination defaults keep theirs.
                self.assertCountEqual(restored.node_profiles(), migrated.node_profiles())
                migrated.save_node_profile('New node', CHEAP, enabled=False)
                self.assertNotIn(CHEAP, migrated.node_candidates('mainnet'))
            finally:
                migrated.con.close()
                restored.con.close()

    def test_profiles_preferences_priority_and_backups_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wallet.db'
            store = DB(path)
            store.save_node_profile('First', 'http://192.168.1.50:3000')
            store.save_node_profile('Second', CHEAP, enabled=False)
            store.move_node_profile('Second', -1)
            preferences = dict(DEFAULTS, auto_lock_minutes=7, refresh_seconds=20)
            save_preferences(store, preferences)
            store.con.close()
            reopened, restored = DB(path), DB(':memory:')
            try:
                self.assertEqual(load_preferences(reopened), preferences)
                self.assertNotIn(CHEAP, reopened.node_candidates('mainnet'))
                self.assertNotIn('https://testnet.wartnode.com', reopened.node_candidates('mainnet'))
                restored.restore_snapshot(reopened.snapshot())
                self.assertEqual(load_preferences(restored), preferences)
                self.assertEqual([n for n in restored.configured_nodes('mainnet') if n['name'] == 'Second'][0]['enabled'], 0)
                self.assertEqual(restored.node_profiles(), reopened.node_profiles())
            finally:
                reopened.con.close()
                restored.con.close()

    def test_invalid_preferences_do_not_replace_saved_values_or_merge_backup(self):
        store, target = DB(':memory:'), DB(':memory:')
        try:
            save_preferences(store, DEFAULTS)
            for extra in ({'refresh_seconds': 0}, {'auto_lock_minutes': 0}, {'auto_failover': 'true'}, {'fallback_fee_e8': -1}):
                with self.assertRaises(ValueError):
                    save_preferences(store, dict(DEFAULTS, **extra))
                self.assertEqual(load_preferences(store), DEFAULTS)
            store.set_setting('wallet_preferences', '{"refresh_seconds":0}')
            store.save_node_profile('Must not import', CHEAP)
            with self.assertRaises(ValueError):
                target.restore_snapshot(store.snapshot())
            self.assertFalse(any(n[0] == 'Must not import' for n in target.node_profiles()))
        finally:
            store.con.close()
            target.con.close()


class SettingsGuiTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    tearDown = sessions.SessionTests.tearDown

    def test_settings_create_rename_disable_and_preferences_apply_immediately(self):
        panel = self.window.settings_panel
        panel.name.setText('My node')
        panel.url.setText(CHEAP)
        panel.save_node()
        self.assertEqual(panel.selected()['name'], 'My node')
        panel.name.setText('Renamed node')
        panel.enabled.setChecked(False)
        panel.save_node()
        self.assertNotIn(CHEAP, main.db.node_candidates('mainnet'))
        self.assertEqual(panel.selected()['name'], 'Renamed node')
        connected = []
        panel.connect_requested.connect(lambda *args: connected.append(args))
        panel.connect_node()
        self.assertEqual(connected, [])
        panel.auto_lock.setValue(7)
        panel.refresh.setValue(20)
        panel.fallback_fee.setText('0.00019984')
        panel.save_preferences()
        self.assertEqual(self.window.idle_timer.interval(), 7 * 60000)
        self.assertEqual(self.window.refresh_timer.interval(), 20000)
        self.assertEqual(self.window.send_panel.fee.text(), '0.00019984')

    def test_node_test_reports_health_and_fee_without_switching_connection(self):
        panel = self.window.settings_panel
        panel.table.selectRow(0)
        original = main.PEER
        with patch.object(NodeClient, 'probe', return_value=state(original)):
            panel.test_node()
            wait_for(lambda: not panel.jobs.running('probe'))
        self.assertIn('0.00009992 WART', panel.notice.text())
        self.assertEqual(main.PEER, original)

    def test_sidebar_keeps_identical_contents_on_hover(self):
        self.window.show()
        for name in ('address_button', 'send_button', 'contact_button', 'transactions_button', 'miners_button', 'notes'):
            button = getattr(self.window.mainUI, name)
            option = QStyleOptionButton()
            button.initStyleOption(option)
            before = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
            QTest.mouseMove(button, button.rect().center())
            QTest.qWait(5)
            option = QStyleOptionButton()
            button.initStyleOption(option)
            after = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
            self.assertEqual(before, after)
            self.assertEqual(button.font().family(), 'Segoe UI')
            self.assertGreaterEqual(after.height(), button.fontMetrics().height())

    def test_lowest_fee_switch_refreshes_balance_and_never_broadcasts(self):
        main.db.save_node_profile('Cheaper node', CHEAP)
        save_preferences(main.db, dict(DEFAULTS, auto_failover=False))
        self.window.apply_preferences()
        self.window.set_node_status(state(main.PEER, 100032))
        self.window.balance_snapshot = (main.PEER, OWNER, 1000000000, time.monotonic())
        with patch.object(main, 'probe_nodes', return_value=state(CHEAP)), patch.object(NodeClient, 'broadcast') as post:
            self.window.select_lowest_fee()
            wait_for(lambda: not self.window.jobs.running('selection'))
            self.assertEqual(main.PEER, CHEAP)
            self.assertEqual(self.window.send_panel.fee.text(), '0.00009992')
            self.assertIsNone(self.window.balance_snapshot)
            post.assert_not_called()

    def test_selection_result_is_discarded_if_payment_review_opens(self):
        main.db.save_node_profile('Cheaper node', CHEAP)
        save_preferences(main.db, dict(DEFAULTS, auto_failover=False))
        self.window.apply_preferences()
        self.window.set_node_status(state(main.PEER, 100032))
        original = main.PEER
        release = Event()
        def delayed(*args, **kwargs):
            release.wait(2)
            return state(CHEAP)
        with patch.object(main, 'probe_nodes', side_effect=delayed):
            self.window.select_lowest_fee()
            self.window.active_confirm = object()
            release.set()
            wait_for(lambda: not self.window.jobs.running('selection'))
        self.window.active_confirm = None
        self.assertEqual(main.PEER, original)

    def test_connection_failure_uses_configured_fallback_without_broadcast(self):
        main.db.save_node_profile('Backup', CHEAP)
        save_preferences(main.db, DEFAULTS)
        self.window.apply_preferences()
        self.window.set_node_status(state(main.PEER))
        self.window.show()
        with patch.object(NodeClient, 'status', side_effect=NodeError('Disconnected')), \
             patch.object(main, 'probe_nodes', return_value=state(CHEAP)), \
             patch.object(NodeClient, 'broadcast') as post:
            self.window.refresh_node_status()
            wait_for(lambda: main.PEER == CHEAP)
            self.assertTrue(self.window.node_state['online'])
            post.assert_not_called()

    def test_disabled_and_other_network_nodes_are_not_automatic_candidates(self):
        main.db.save_node_profile('Disabled', CHEAP, enabled=False)
        main.db.save_node_profile('Other network', 'http://192.168.1.66:3000', 'testnet')
        save_preferences(main.db, DEFAULTS)
        self.window.apply_preferences()
        self.window.set_node_status(state(main.PEER))
        with patch.object(main, 'probe_nodes') as probe:
            self.window.select_lowest_fee()
            self.window.try_failover()
            probe.assert_not_called()

    def test_disabling_a_node_discards_an_already_running_selection(self):
        main.db.save_node_profile('Cheaper node', CHEAP)
        save_preferences(main.db, dict(DEFAULTS, auto_failover=False))
        self.window.apply_preferences()
        self.window.set_node_status(state(main.PEER))
        original, release = main.PEER, Event()
        def delayed(*args, **kwargs):
            release.wait(2)
            return state(CHEAP)
        with patch.object(main, 'probe_nodes', side_effect=delayed):
            self.window.select_lowest_fee()
            panel = self.window.settings_panel
            panel.reload('Cheaper node')
            panel.enabled.setChecked(False)
            panel.save_node()
            release.set()
            wait_for(lambda: not self.window.jobs.workers)
        self.assertEqual(main.PEER, original)

    def test_older_node_can_send_reviewed_fallback_and_fee_change_blocks_signing(self):
        self.window.set_node_status(state(main.PEER, None))
        self.window.balance_snapshot = (main.PEER, OWNER, 1000000000, time.monotonic())
        self.window.send_panel.recipient.setText(OTHER)
        self.window.send_panel.amount.setText('1')
        signed = utils.SignedTransfer('ab' * 32, {'signature65': 'synthetic fixture'})
        def authorize():
            dialog = self.window.active_confirm
            self.assertIn('fallback', dialog.fee_note.text())
            self.assertEqual(dialog.confirm.amount_label_2.text(), '0.00009992')
            dialog.password.setText('wallet password')
            dialog.confirm_btn()
        with patch.object(NodeClient, 'minimum_fee', side_effect=NodeError('HTTP 404')), \
             patch.object(main, 'prepare_tx', return_value=signed) as prepare, \
             patch.object(NodeClient, 'broadcast', return_value=signed.tx_hash) as broadcast, \
             patch.object(self.window, 'refresh_pending_history'):
            QTimer.singleShot(0, authorize)
            self.window.send_wart()
            self.assertEqual(prepare.call_args.args[2], 9992)
            broadcast.assert_called_once()
        self.assertEqual(main.db.history(OWNER)[0]['fee_e8'], '9992')
        dialog = main.Confirm(self.window, utils.TransferIntent(OTHER, 100, 9992), main.PEER, OWNER)
        self.widgets.insert(0, dialog)
        dialog.password.setText('wallet password')
        with patch.object(NodeClient, 'minimum_fee', return_value=100032), patch.object(main, 'prepare_tx') as prepare:
            dialog.confirm_btn()
            wait_for(lambda: not dialog.jobs.running('send'))
            prepare.assert_not_called()
