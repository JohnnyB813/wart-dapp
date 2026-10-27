"""DeFi native-coin compatibility, network isolation, and GUI/HTTP signing flow."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog

import main
import utils
from db import DB
from history import fetch_account_page, reconcile_local, LATEST_CURSOR
from mined_blocks import fetch_mined_page
from node_client import BroadcastUncertain, NodeClient, NodeError
from wallet_settings import save_preferences, DEFAULTS
from test_sessions import wait_for
import test_sessions as sessions

APP = QApplication.instance() or QApplication([])
OWNER, KEY = utils.wallet_from_pkhex('00' * 31 + '01')
OTHER = utils.wallet_from_pkhex('00' * 31 + '02')[0]
PEER = 'http://localhost:3000'
FEE = 1000448
HEADER = {'time': {'timestamp': 1781008958}, 'hash': 'ef' * 32}


def transaction(kind='wartTransfer', recipient=OTHER, sender=OWNER, amount=100000000, tx_hash='ab' * 32):
    tx = {'data': {'toAddress': recipient, 'amount': {'E8': amount, 'str': utils.format_e8(amount)}},
          'hash': tx_hash}
    if kind == 'wartTransfer':
        tx['signedCommon'] = {'originAddress': sender[:40], 'fee': {'E8': FEE}, 'nonceId': 7, 'pinHeight': 32}
    return tx


def page(tx=None):
    return {'fromId': 1, 'perBlock': [{'height': 64, 'confirmations': 2, 'header': HEADER,
            'body': {'reward': {'transaction': transaction('reward', OWNER, amount=300000000, tx_hash='cd' * 32)},
                     'wartTransfer': [{'transaction': tx}] if tx else []}}]}


class TestnetClientTests(unittest.TestCase):
    def test_nested_head_balance_and_network_mismatch(self):
        client = NodeClient(PEER, network='testnet')
        head = {'chainHead': {'height': 64, 'pinHeight': 32, 'pinHash': '00' * 32}, 'synced': True}
        with patch.object(client, 'request', return_value=head):
            self.assertEqual(client.head(True)['pinHeight'], 32)
            head['synced'] = False
            with self.assertRaisesRegex(NodeError, 'not synced'):
                client.head(True)
        with patch.object(client, 'network', return_value='mainnet'), self.assertRaisesRegex(NodeError, 'selected DeFi testnet'):
            client.require_network()
        with patch.object(client, 'network', return_value='unverified'), self.assertRaises(NodeError):
            client.require_network()
        balance = {'wart': {'total': {'E8': 1000000000}, 'locked': {'E8': 200000000}, 'mempool': {'E8': 100000000}}}
        with patch.object(client, 'request', return_value=balance) as request:
            self.assertEqual(client.balance(OWNER), '8.00000000')
            request.assert_called_once_with(f'/account/{OWNER}/wart_balance')
            for invalid in (True, -1, 1 << 64, '3'):
                balance['wart']['total']['E8'] = invalid
                with self.assertRaises(NodeError):
                    client.balance(OWNER)

    def test_signature_and_payload_match_defi_native_transfer_contract(self):
        client = NodeClient(PEER, network='testnet')
        with patch.object(utils, 'NodeClient', return_value=client), \
             patch.object(client, 'head', return_value={'pinHash': '00' * 32, 'pinHeight': 32}), \
             patch.object(client, 'network', return_value='testnet'), \
             patch.object(utils.secrets, 'randbits', return_value=7):
            signed = utils.prepare_tx(OTHER, 100000000, FEE, KEY, PEER, network='testnet')
        payload = signed.payload
        self.assertEqual(payload['type'], 'wartTransfer')
        self.assertEqual(payload['wartE8'], 100000000)
        self.assertNotIn('amountE8', payload)
        preimage = bytes(32) + (32).to_bytes(4, 'big') + (7).to_bytes(4, 'big') + bytes(3)
        preimage += FEE.to_bytes(8, 'big') + bytes.fromhex(OTHER)[:20] + (100000000).to_bytes(8, 'big')
        digest = hashlib.sha256(preimage).digest()
        self.assertEqual(signed.tx_hash, digest.hex())
        signature = bytes.fromhex(payload['signature65'])
        self.assertTrue(KEY.get_verifying_key().verify_digest(signature[:64], digest))
        self.assertLessEqual(int.from_bytes(signature[32:64], 'big'), utils.SECP256k1.order // 2)

    def test_wrong_network_prevents_signing(self):
        with patch.object(NodeClient, 'head', return_value={'pinHash': '00' * 32, 'pinHeight': 32}), \
             patch.object(NodeClient, 'network', return_value='mainnet'), \
             patch.object(utils.secp256k1_generator, 'sign_with_recid') as sign:
            with self.assertRaises(NodeError):
                utils.prepare_tx(OTHER, 100, FEE, KEY, PEER, network='testnet')
            sign.assert_not_called()

    def test_top_level_broadcast_reply_and_mismatched_hash(self):
        client = NodeClient(PEER, network='testnet')
        for response in ({'code': 0, 'txHash': 'ab' * 32}, {'code': 0, 'data': {'txHash': 'ab' * 32}}):
            with patch.object(client, '_exchange', return_value=(200, json.dumps(response).encode())):
                self.assertEqual(client.broadcast({}, 'ab' * 32), 'ab' * 32)
                with self.assertRaises(BroadcastUncertain):
                    client.broadcast({}, 'cd' * 32)

    def test_defi_not_found_uses_code_56_not_mainnet_code_57(self):
        client = NodeClient(PEER, network='testnet')
        with patch.object(client, 'request', side_effect=NodeError('Not found', 56)):
            self.assertIsNone(client.lookup('ab' * 32))
            self.assertEqual(fetch_account_page(client, OWNER), ([], 0))
        with patch.object(client, 'request', side_effect=NodeError('Empty response', 57)):
            with self.assertRaisesRegex(NodeError, 'Empty response'):
                client.lookup('ab' * 32)
            with self.assertRaisesRegex(NodeError, 'Empty response'):
                fetch_account_page(client, OWNER)

    def test_history_receive_send_rewards_and_lookup_reconciliation(self):
        client = NodeClient(PEER, network='testnet')
        tx = transaction()
        with patch.object(client, 'request', return_value=page(tx)):
            rows, cursor = fetch_account_page(client, OWNER)
        self.assertEqual(cursor, 1)
        self.assertEqual([row['direction'] for row in rows], ['mined', 'outgoing'])
        self.assertTrue(all(row['network'] == 'testnet' for row in rows))
        self.assertEqual(rows[1]['counterparty'], OTHER)
        self.assertEqual(rows[1]['fee_e8'], str(FEE))
        lookup = {'transaction': tx, 'type': 'wartTransfer', 'confirmations': 2,
                  'mined': {'block': {'height': 64}}}
        with patch.object(client, 'request', side_effect=[lookup, {'header': HEADER}]):
            self.assertEqual(reconcile_local(client, rows[1:])[0]['status'], 'confirmed')
        with patch.object(client, 'request', return_value=page(transaction(recipient=OWNER, sender=OTHER))):
            incoming, _ = fetch_account_page(client, OWNER)
        self.assertEqual(incoming[1]['direction'], 'incoming')
        self.assertEqual(incoming[1]['counterparty'], OTHER)
        with patch.object(client, 'require_network'), patch.object(client, 'request', return_value=page()):
            rewards, _, details = fetch_mined_page(client, OWNER)
        self.assertEqual(details[rewards[0]['tx_hash']]['block_hash'], 'ef' * 32)


class TestnetStorageTests(unittest.TestCase):
    def test_networks_keep_same_hash_and_reservations_separate_including_restore(self):
        store, restored = DB(':memory:'), DB(':memory:')
        try:
            signed = utils.SignedTransfer('ab' * 32, {})
            store.record_outgoing(OWNER, utils.TransferIntent(OTHER, 100, 1), signed, PEER)
            store.network = 'testnet'
            self.assertEqual(store.history(OWNER), [])
            self.assertEqual(store.reserved_amount(OWNER), 0)
            store.record_outgoing(OWNER, utils.TransferIntent(OTHER, 200, 1), signed, PEER)
            store.set_tx_status(OWNER, signed.tx_hash, 'pending')
            self.assertEqual(store.reserved_amount(OWNER), 201)
            self.assertEqual(store.reserved_amount(OWNER, 'mainnet'), 101)
            store.set_tx_status(OWNER, signed.tx_hash, 'confirmed', 'mainnet')
            self.assertEqual(store.reserved_amount(OWNER), 201)
            self.assertEqual(store.reserved_amount(OWNER, 'mainnet'), 0)
            restored.restore_snapshot(store.snapshot())
            self.assertEqual(restored.history(OWNER, 'mainnet'), store.history(OWNER, 'mainnet'))
            self.assertEqual(restored.history(OWNER, 'testnet'), store.history(OWNER, 'testnet'))
            self.assertIn(('Wartnode DeFi testnet', 'https://testnet.wartnode.com', 'testnet'), restored.node_profiles())
        finally:
            store.con.close()
            restored.con.close()

    def test_legacy_history_migrates_as_mainnet_and_old_backups_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            store = DB(path)
            store.record_outgoing(OWNER, utils.TransferIntent(OTHER, 100, 1), utils.SignedTransfer('ab' * 32, {}), PEER)
            with store.con:
                store.con.execute('ALTER TABLE tx_history RENAME TO newer_history')
                columns = [row[1] for row in store.con.execute('PRAGMA table_info(newer_history)') if row[1] != 'network']
                store.con.execute('CREATE TABLE tx_history AS SELECT ' + ','.join(columns) + ' FROM newer_history')
                store.con.execute('DROP TABLE newer_history')
            legacy = store.snapshot()
            store.con.close()
            migrated, restored = DB(path), DB(':memory:')
            try:
                self.assertEqual(migrated.history(OWNER)[0]['network'], 'mainnet')
                self.assertEqual(migrated.history(OWNER, 'testnet'), [])
                restored.restore_snapshot(legacy)
                self.assertEqual(restored.history(OWNER), migrated.history(OWNER))
            finally:
                migrated.con.close()
                restored.con.close()


class TestnetSessionGuiTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    tearDown = sessions.SessionTests.tearDown

    def test_minimum_fee_and_network_mismatch_block_payment_review(self):
        main.db.network = 'testnet'
        self.window.set_node_status(dict(peer=main.PEER, network='testnet', height=64,
            synced=True, version='defi', checked_at=time.time(), min_fee_e8=FEE))
        self.window.balance_snapshot = (main.PEER, OWNER, 1000000000, time.monotonic())
        self.window.send_panel.recipient.setText(OTHER)
        self.window.send_panel.amount.setText('1')
        self.window.send_panel.fee.setText('0.001')
        with patch.object(main, 'Confirm') as confirm:
            self.window.send_wart()
            confirm.assert_not_called()
            self.assertIn('minimum', str(self.notices.call_args.args[1]))
            self.window.send_panel.fee.setText(utils.format_e8(FEE))
            self.window.set_node_status(dict(self.window.node_state, network='mainnet'))
            self.window.send_wart()
            confirm.assert_not_called()
        self.assertFalse(self.window.mainUI.send_btn.isEnabled())

    def test_confirmation_cannot_authorize_after_network_switch_and_late_result_keeps_original_network(self):
        main.db.network = 'testnet'
        intent = utils.TransferIntent(OTHER, 100, FEE)
        signed = utils.SignedTransfer('ab' * 32, {})
        main.db.record_outgoing(OWNER, intent, signed, main.PEER)
        dialog = main.Confirm(self.window, intent, main.PEER, OWNER)
        self.widgets.insert(0, dialog)
        self.assertIn('testnet', dialog.windowTitle())
        main.db.network = 'mainnet'
        dialog.password.setText('wallet password')
        with patch.object(dialog.jobs, 'submit') as submit:
            dialog.confirm_btn()
            submit.assert_not_called()
        dialog.tx_hash = signed.tx_hash
        dialog.lock()
        dialog.sent(signed.tx_hash)
        self.assertEqual(main.db.history(OWNER), [])
        self.assertEqual(main.db.history(OWNER, 'testnet')[0]['status'], 'pending')

    def test_refresh_confirms_saved_payments_when_old_lookup_is_not_found(self):
        main.db.network = 'testnet'
        for tx_hash in ('aa' * 32, 'bb' * 32):
            main.db.record_outgoing(OWNER, utils.TransferIntent(OTHER, 100000000, FEE),
                                    utils.SignedTransfer(tx_hash, {}), main.PEER)
            main.db.set_tx_status(OWNER, tx_hash, 'pending')
        old = dict(main.db.history(OWNER)[0], tx_hash='cc' * 32, direction='mined',
                   counterparty='', amount_e8='300000000', fee_e8='0', payload='',
                   status='confirmed', confirmations=1, block_height=32)
        main.db.save_history([old])

        def response(_client, path):
            if path.startswith('/account/'):
                raise NodeError('Node rejected the request: Not found', 56)
            if path.endswith('cc' * 32):
                raise NodeError('Node rejected the request: Not found', 56)
            if path.startswith('/transaction/lookup/'):
                tx_hash = path.rsplit('/', 1)[-1]
                return {'transaction': transaction(tx_hash=tx_hash), 'type': 'wartTransfer',
                        'confirmations': 2, 'mined': {'block': {'height': 64}}}
            if path == '/chain/block/64/header':
                return {'header': HEADER}
            raise AssertionError(path)

        with patch.object(NodeClient, 'require_network'), patch.object(NodeClient, 'request', response):
            self.window.list_transactions()
            wait_for(lambda: not self.window.jobs.running('history'))
        statuses = {row['tx_hash']: row['status'] for row in main.db.history(OWNER)}
        self.assertEqual(statuses['aa' * 32], 'confirmed')
        self.assertEqual(statuses['bb' * 32], 'confirmed')
        self.assertEqual(main.db.reserved_amount(OWNER), 0)


class TestnetGuiFlowTests(unittest.TestCase):
    def test_connect_receive_sign_broadcast_and_confirm_with_real_local_http(self):
        submitted = []
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def reply(self, data):
                body = json.dumps({'code': 0, 'data': data}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                paths.append(self.path)
                if self.path == '/chain/head':
                    self.reply({'chainHead': {'height': 64, 'pinHeight': 32, 'pinHash': '00' * 32}, 'synced': True})
                elif self.path.startswith('/chain/mine/'):
                    self.reply({'testnet': True})
                elif self.path.endswith('/wart_balance'):
                    self.reply({'wart': {'total': {'E8': 1000000000}, 'locked': {'E8': 200000000}, 'mempool': {'E8': 0}}})
                elif self.path == '/transaction/minfee':
                    self.reply({'minFee': {'E8': FEE}})
                elif self.path == '/tools/info':
                    self.reply({'version': {'name': 'test-defi'}})
                elif '/history/' in self.path:
                    self.reply(page(transaction(tx_hash=submitted[-1]['hash'])) if submitted else page())
                elif '/transaction/lookup/' in self.path:
                    self.reply({'type': 'wartTransfer', 'transaction': transaction(tx_hash=submitted[-1]['hash']),
                                'confirmations': 2, 'mined': {'block': {'height': 64}}})
                elif self.path.endswith('/header'):
                    self.reply({'header': HEADER})
                else:
                    self.send_error(404)

            def do_POST(self):
                paths.append(self.path)
                tx = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                preimage = bytes(32) + tx['pinHeight'].to_bytes(4, 'big') + tx['nonceId'].to_bytes(4, 'big') + bytes(3)
                preimage += tx['feeE8'].to_bytes(8, 'big') + bytes.fromhex(tx['toAddr'])[:20] + tx['wartE8'].to_bytes(8, 'big')
                tx['hash'] = hashlib.sha256(preimage).hexdigest()
                submitted.append(tx)
                self.reply({'txHash': tx['hash']})

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        main.db = DB(':memory:')
        save_preferences(main.db, dict(DEFAULTS, auto_lowest_fee=False, auto_failover=False))
        main.wallets = main.WartWallets()
        main.mainWindow = main.MainWindow(main.wallets)
        entry = main.WartEntry()
        try:
            with patch.object(main, 'message'):
                entry.network.setCurrentIndex(entry.network.findData('testnet'))
                self.assertEqual(entry.ui.input_field.text(), 'https://testnet.wartnode.com')
                self.assertEqual(entry.profiles.currentText(), 'Wartnode DeFi testnet')
                entry.ui.input_field.setText(f'http://127.0.0.1:{server.server_port}')
                entry.private_connect()
                wait_for(lambda: not entry.jobs.running('connect'))
                self.assertEqual(main.db.network, 'testnet')
                self.assertEqual(main.mainWindow.node_state['network'], 'testnet')
                self.assertEqual(main.mainWindow.send_panel.fee.text(), utils.format_e8(FEE))
                token, salt = utils.encrypt_pk('00' * 31 + '01', 'wallet password')
                main.db.insert_wallet(OWNER, token, salt)
                # A mainnet reservation must not reduce this network's balance.
                main.db.record_outgoing(OWNER, utils.TransferIntent(OTHER, 900000000, 1),
                                        utils.SignedTransfer('aa' * 32, {}), PEER, network='mainnet')
                login = main.Login(main.wallets, OWNER)
                login.login.textEdit.setText('wallet password')
                login.ok()
                self.assertEqual(login.result(), QDialog.DialogCode.Accepted)
                login.deleteLater()
                main.wallets.hide()
                main.mainWindow.show_wallet()
                wait_for(lambda: main.mainWindow.balance_snapshot is not None)
                self.assertEqual(main.mainWindow.available_balance(), 800000000)
                self.assertIn('testnet', main.mainWindow.receive_panel.network.text())
                self.assertEqual(main.mainWindow.receive_panel.address.text(), OWNER)
                self.assertFalse(main.mainWindow.receive_qr.pixmap().isNull())
                main.mainWindow.send_panel.recipient.setText(OTHER)
                main.mainWindow.send_panel.amount.setText('1')

                def authorize():
                    dialog = main.mainWindow.active_confirm
                    dialog.password.setText('wallet password')
                    dialog.confirm_btn()

                QTimer.singleShot(0, authorize)
                main.mainWindow.send_wart()
                self.assertEqual(len(submitted), 1)
                tx = submitted[0]
                self.assertEqual((tx['type'], tx['wartE8'], tx['feeE8']), ('wartTransfer', 100000000, FEE))
                self.assertTrue(KEY.get_verifying_key().verify_digest(bytes.fromhex(tx['signature65'])[:64], bytes.fromhex(tx['hash'])))
                main.mainWindow.list_transactions()
                wait_for(lambda: not main.mainWindow.jobs.running('history'))
                rows = main.db.history(OWNER)
                self.assertEqual({row['direction'] for row in rows}, {'mined', 'outgoing'})
                self.assertTrue(all(row['status'] == 'confirmed' for row in rows))
                self.assertEqual(main.db.reserved_amount(OWNER), 0)
                self.assertEqual(main.db.reserved_amount(OWNER, 'mainnet'), 900000001)
                self.assertFalse(any(path.endswith('/balance') for path in paths))
        finally:
            for widget in (entry, main.mainWindow, main.wallets):
                jobs = getattr(widget, 'jobs', getattr(widget, 'backup_jobs', None))
                jobs.cancel_all()
                jobs.pool.waitForDone(6000)
                widget.hide()
                widget.deleteLater()
            APP.processEvents()
            main.db.con.close()
            main.db, main.PEER, main.address = None, None, ''
            server.shutdown()
            server.server_close()
            thread.join(2)
