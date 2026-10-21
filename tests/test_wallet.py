import base64
import hashlib
import json
import os
import time
import unittest
from dataclasses import FrozenInstanceError
from threading import Event
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from cryptography.fernet import Fernet
from PySide6.QtCore import QThread, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

import main
import utils
from db import DB
from gui_jobs import GuiJobs
from node_client import BroadcastUncertain, NodeClient, NodeError


PHRASE = 'abandon ' * 11 + 'about'
PRIVATE_KEY = '00' * 31 + '01'  # Public synthetic fixture; never fund it.
ADDRESS, KEY = utils.wallet_from_pkhex(PRIVATE_KEY)
APP = QApplication.instance() or QApplication([])


class TransferTests(unittest.TestCase):
    def test_exact_units_and_bad_numeric_inputs(self):
        self.assertEqual(utils.parse_e8('0.00000001'), 1)
        self.assertEqual(utils.parse_e8('0.001000100'), 100010)
        for text in ['NaN', 'inf', '-1', '0', '1e8', '0.000000001', 'x', '9' * 1000]:
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                utils.parse_e8(text)

    def test_fee_rounding_against_all_compact_encodings(self):
        # Independent reference-node decode: 6 exponent bits, 10 fraction bits.
        for encoded in range(65536):
            exponent, mantissa = encoded >> 10, (encoded & 1023) + 1024
            value = mantissa >> (10 - exponent) if exponent < 10 else mantissa << (exponent - 10)
            if value <= utils.MAX_UINT64:
                self.assertEqual(utils.round_fee_e8(value), value)
        self.assertEqual(utils.round_fee_e8(100010), 99968)
        self.assertEqual(utils.round_fee_e8(99999), 99968)
        self.assertEqual(utils.round_fee_e8(0), 0)
        for value in [-1, True, 1.0, 1 << 64]:
            with self.assertRaises(ValueError):
                utils.round_fee_e8(value)

    def test_frozen_final_intent_and_checksum(self):
        intent = utils.TransferIntent.from_text(ADDRESS, '1.00000001', '0.001000100')
        self.assertEqual(intent.amount, 100000001)
        self.assertEqual(intent.fee, 99968)
        with self.assertRaises(FrozenInstanceError):
            intent.fee = 8589934592
        with self.assertRaises(ValueError):
            utils.TransferIntent(ADDRESS, 1, 100010)
        with self.assertRaises(ValueError):
            utils.TransferIntent(ADDRESS[:-8] + '00000000', 1, 0)

    def test_real_signature_binds_approved_fee_without_fee_endpoint(self):
        client = MagicMock()
        client.head.return_value = {'pinHash': '00' * 32, 'pinHeight': 32}
        client.broadcast.return_value = 'ab' * 32
        with patch.object(utils, 'NodeClient', return_value=client), patch.object(utils.secrets, 'randbits', return_value=7):
            result = utils.send_tx(ADDRESS, 100000000, 99968, KEY, 'http://127.0.0.1:3000')
        self.assertEqual(result, 'ab' * 32)
        client.head.assert_called_once_with(require_synced=True)
        client.request.assert_not_called()
        payload = client.broadcast.call_args.args[0]
        self.assertEqual(payload['feeE8'], 99968)
        signature = bytes.fromhex(payload['signature65'])
        preimage = bytes(32) + (32).to_bytes(4, 'big') + (7).to_bytes(4, 'big') + bytes(3)
        preimage += (99968).to_bytes(8, 'big') + bytes.fromhex(ADDRESS)[:20] + (100000000).to_bytes(8, 'big')
        self.assertTrue(KEY.get_verifying_key().verify_digest(signature[:64], hashlib.sha256(preimage).digest()))
        self.assertLessEqual(int.from_bytes(signature[32:64], 'big'), utils.SECP256k1.order // 2)
        self.assertIn(signature[64], range(4))
        self.assertEqual(set(payload), {'pinHeight', 'nonceId', 'toAddr', 'amountE8', 'feeE8', 'signature65'})

    def test_unconfirmed_nonrepresentable_fee_fails_before_network(self):
        with patch.object(utils, 'NodeClient') as client, self.assertRaises(ValueError):
            utils.send_tx(ADDRESS, 1, 100010, KEY, 'http://127.0.0.1:3000')
        client.assert_not_called()


class StorageTests(unittest.TestCase):
    def test_fixed_legacy_recovery_vectors(self):
        addr, key = utils.wallet_from_seed(main.mnemo.to_seed(PHRASE, passphrase=''))
        self.assertEqual(addr, '35af6b55fc91b0761e9ecaf96b68cbef4cf0f5893538e297')
        self.assertEqual(key.to_string().hex(), 'fc21cd6bf8b1cbcc2e7dbf767ca7fd5dc8a97f88a9941f9706cc9a34c5d18781')
        self.assertEqual(ADDRESS, '751e76e8199196d454941c45d1b3a323f1433bd6ffded884')

    def test_existing_encryption_format_and_new_format_roundtrip(self):
        salt, password = bytes(range(16)), 'legacy\npassword'
        derived = base64.urlsafe_b64encode(hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 480000, 32))
        old_token = Fernet(derived).encrypt(PRIVATE_KEY.encode()).decode()
        self.assertEqual(utils.decrypt_pk(old_token, password, base64.b64encode(salt).decode()), PRIVATE_KEY)
        token, encoded_salt = utils.encrypt_pk(PRIVATE_KEY, password)
        derived = base64.urlsafe_b64encode(hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(encoded_salt), 480000, 32))
        self.assertEqual(Fernet(derived).decrypt(token.encode()).decode(), PRIVATE_KEY)
        self.assertIsNone(utils.decrypt_pk(token, 'wrong password', encoded_salt))
        for password in ['', 'short', None]:
            with self.assertRaises(ValueError):
                utils.encrypt_pk(PRIVATE_KEY, password)

    def test_database_contact_quotes_are_data(self):
        db = DB(':memory:')
        try:
            db.insert_friend(ADDRESS, "O'Brien'); DROP TABLE wallets; --")
            self.assertEqual(len(db.get_friends()), 1)
            self.assertEqual(db.get_wallets(), [])
        finally:
            db.con.close()


class NodeTests(unittest.TestCase):
    def test_response_schema_json_and_redirect_errors(self):
        client = NodeClient('http://localhost:3000')
        good = json.dumps({'code': 0, 'data': {'balance': '1.00000000'}}).encode()
        with patch.object(client, '_exchange', return_value=(200, good)):
            self.assertEqual(client.balance(ADDRESS), '1.00000000')
        for status, body in [(302, b''), (200, b'not json'), (200, b'[]'),
                             (200, b'{"code": true, "data": {}}'),
                             (200, b'{"code": 0, "data": []}')]:
            with patch.object(client, '_exchange', return_value=(status, body)), self.assertRaises(NodeError):
                client.head()

    def test_https_for_remote_and_no_credentials_or_url_suffixes(self):
        for url in ['http://public.example', 'https://user:pass@node.example', 'https://node.example/path',
                    'https://node.example?token=x', 'file:///wallet', 'https://node.example:99999']:
            with self.subTest(url=url), self.assertRaises(NodeError):
                NodeClient(url)
        self.assertEqual(NodeClient('https://node.example/').peer, 'https://node.example')
        self.assertEqual(NodeClient('http://localhost:3000').peer, 'http://localhost:3000')

    def test_http_lan_addresses_and_public_boundaries(self):
        accepted = [
            '127.0.0.1', '10.0.0.1', '10.255.255.254',
            '172.16.0.1', '172.31.255.254', '192.168.1.50',
            '169.254.1.1', '[::1]', '[fd12:3456::1]', '[fe80::1]',
            '[::ffff:192.168.1.50]',
        ]
        for host in accepted:
            url = f'http://{host}:3000'
            with self.subTest(url=url):
                self.assertEqual(NodeClient.validate_peer(url), url)
        rejected = [
            '9.255.255.255', '11.0.0.0', '172.15.255.255', '172.32.0.0',
            '192.167.255.255', '192.169.0.0', '8.8.8.8', '0.0.0.0',
            '100.64.0.1', '192.0.2.1', '198.18.0.1', '224.0.0.1',
            '[::]', '[2001:db8::1]', '[2606:4700:4700::1111]',
            '[::ffff:8.8.8.8]', 'node.lan',
        ]
        for host in rejected:
            with self.subTest(host=host), self.assertRaisesRegex(NodeError, 'Public nodes require HTTPS'):
                NodeClient.validate_peer(f'http://{host}:3000')

    def test_untrusted_head_and_defi_balance_rejected(self):
        client = NodeClient('http://localhost:3000')
        for pin in [True, -32, 33, 128]:
            with patch.object(client, 'request', return_value={'height': 64, 'pinHeight': pin, 'pinHash': '00' * 32}), self.assertRaises(NodeError):
                client.head()
        with patch.object(client, 'request', return_value={'wart': {'total': '1'}}), self.assertRaises(NodeError):
            client.balance(ADDRESS)
        with patch.object(client, 'request', return_value={'height': 64, 'pinHeight': 32, 'pinHash': '00' * 32, 'synced': False}), self.assertRaises(NodeError):
            client.head(require_synced=True)

    def test_cancellation_size_limit_and_lost_broadcast(self):
        cancel = Event()
        cancel.set()
        with self.assertRaises(NodeError):
            NodeClient('http://localhost:3000', cancel=cancel).head()
        client = NodeClient('http://localhost:3000')
        with patch.object(client, '_exchange', return_value=(200, b'x' * (NodeClient.MAX_RESPONSE_BYTES + 1))), self.assertRaises(NodeError):
            client.head()
        with patch.object(client, '_exchange', side_effect=NodeError('timed out')) as exchange, self.assertRaises(BroadcastUncertain):
            client.broadcast({'synthetic': True})
        exchange.assert_called_once()  # Never retry a possibly accepted POST.


class GuiTests(unittest.TestCase):
    def setUp(self):
        main.db = DB(':memory:')
        self.wallets = main.WartWallets()
        self.patch_message = patch.object(main, 'message')
        self.patch_message.start()
        self.widgets = [self.wallets]

    def tearDown(self):
        for widget in self.widgets:
            widget.hide()
            widget.deleteLater()
        APP.processEvents()
        main.db.con.close()
        main.db = None
        self.patch_message.stop()

    def test_cancelled_registration_blocks_all_creation_paths(self):
        for operation in ['new_wallet', 'restore_pk', 'restore_mnemo']:
            for password, confirmation in [('', ''), ('short', 'short'), ('long password', 'different')]:
                def reject(dialog):
                    dialog.register.textEdit.setText(password)
                    dialog.register.textEdit_2.setText(confirmation)
                    dialog.ok()
                    self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
                    dialog.reject()
                    return QDialog.DialogCode.Rejected
                with self.subTest(operation=operation, password=password), patch.object(main.Register, 'exec', reject), patch.object(main.mnemo, 'generate') as generate:
                    getattr(self.wallets, operation)()
                    generate.assert_not_called()
                    self.assertEqual(main.db.get_wallets(), [])

    def test_success_then_cancel_does_not_reuse_credentials(self):
        dialog = main.Register(self.wallets)
        self.widgets.append(dialog)
        dialog.register.textEdit.setText('strong password')
        dialog.register.textEdit_2.setText('strong password')
        dialog.ok()
        self.assertEqual(dialog.take_password(), 'strong password')
        self.assertIsNone(dialog.take_password())
        with patch.object(main.Register, 'exec', return_value=QDialog.DialogCode.Rejected):
            for operation in ['new_wallet', 'restore_pk', 'restore_mnemo']:
                getattr(self.wallets, operation)()
        self.assertEqual(main.db.get_wallets(), [])

    def test_successful_creation_and_import_still_work(self):
        def accept(dialog):
            dialog.register.textEdit.setText('strong password')
            dialog.register.textEdit_2.setText('strong password')
            dialog.ok()
            return dialog.result()
        with patch.object(main.Register, 'exec', accept), patch.object(main.Mnemon, 'exec', return_value=1), patch.object(main.mnemo, 'generate', return_value=PHRASE):
            self.wallets.new_wallet()
        rows = main.db.get_wallets()
        self.assertEqual(len(rows), 1)
        key_hex = utils.decrypt_pk(rows[0][2], 'strong password', rows[0][3])
        self.assertEqual(utils.wallet_from_pkhex(key_hex)[0], rows[0][1])
        restore = main.PrivkeyRestore(self.wallets, 'another password')
        self.widgets.append(restore)
        restore.key.pk_label.setText(PRIVATE_KEY)
        restore.ok()
        self.assertEqual(utils.decrypt_pk(main.db.get_wallet_by_address(ADDRESS)[2], 'another password', main.db.get_wallet_by_address(ADDRESS)[3]), PRIVATE_KEY)

    def test_passwords_mask_and_preserve_legacy_multiline_paste(self):
        register = main.Register(self.wallets)
        login = main.Login(self.wallets, ADDRESS)
        self.widgets.extend([register, login])
        for field in [register.register.textEdit, register.register.textEdit_2, login.login.textEdit]:
            self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)
            APP.clipboard().setText('legacy\npassword')
            field.paste()
            self.assertEqual(field.text(), 'legacy\npassword')

    def test_closed_dialogs_clear_password_and_recovery_phrase(self):
        login = main.Login(self.wallets, ADDRESS)
        phrase = main.Mnemon(PHRASE, self.wallets)
        self.widgets.extend([login, phrase])
        login.login.textEdit.setText('cancelled password')
        login.reject()
        self.assertEqual(login.login.textEdit.text(), '')
        phrase.accept()
        self.assertEqual(phrase.mnemon.mnemo_label.text(), '')

    def test_background_work_keeps_event_loop_alive_and_discards_stale_results(self):
        jobs = GuiJobs(self.wallets)
        values, ticks, callback_threads = [], [], []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        def delayed(cancel):
            time.sleep(0.1)
            return 'stale'
        jobs.submit('balance', delayed, values.append, lambda error: self.fail(str(error)))
        jobs.submit('balance', lambda cancel: 'current',
                    lambda value: (values.append(value), callback_threads.append(QThread.currentThread())),
                    lambda error: self.fail(str(error)))
        for _ in range(20):
            QTest.qWait(10)
        timer.stop()
        self.assertEqual(values, ['current'])
        self.assertGreater(len(ticks), 2)
        self.assertEqual(callback_threads, [APP.thread()])

    def test_confirmation_displays_final_fee_and_blocks_double_submit(self):
        intent = utils.TransferIntent.from_text(ADDRESS, '1', '0.001000100')
        main.address = ADDRESS
        dialog = main.Confirm(self.wallets, intent, 'http://localhost:3000', ADDRESS)
        self.widgets.append(dialog)
        self.assertEqual(dialog.confirm.amount_label_2.text(), '0.00099968')
        with patch.object(dialog.jobs, 'submit') as submit:
            dialog.confirm_btn()
            dialog.confirm_btn()
            self.assertEqual(submit.call_count, 1)
            dialog.reject()
            self.assertTrue(dialog.sending)
            dialog.sending = False
            dialog.reject()


if __name__ == '__main__':
    unittest.main()
