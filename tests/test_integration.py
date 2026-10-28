"""Exercise GUI -> real local HTTP -> signer with synthetic, unfunded keys only."""
import json
import hashlib
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

import main
import utils
from db import DB
from node_client import NodeClient, NodeError
from wallet_settings import save_preferences, DEFAULTS


APP = QApplication.instance() or QApplication([])


class LocalNodeFlowTests(unittest.TestCase):
    def test_absolute_deadline_includes_slow_headers_and_body(self):
        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    if self.path == '/headers':
                        self.connection.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    else:
                        self.send_response(200)
                        self.send_header('Content-Length', '10000')
                        self.end_headers()
                    for _ in range(100):
                        self.connection.sendall(b' ')
                        time.sleep(0.04)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), SlowHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ['/headers', '/body']:
                started = time.monotonic()
                client = NodeClient(f'http://127.0.0.1:{server.server_port}', deadline=started + 0.2)
                with self.subTest(path=path), self.assertRaisesRegex(NodeError, 'timed out'):
                    client.request(path)
                self.assertLess(time.monotonic() - started, 1.5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_redirect_is_not_followed(self):
        paths = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/credential-target')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaisesRegex(NodeError, 'HTTP 302'):
                NodeClient(f'http://127.0.0.1:{server.server_port}').head()
            self.assertEqual(paths, ['/chain/head'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_connect_unlock_balance_and_confirmed_send(self):
        paths, submitted = [], []

        class Handler(BaseHTTPRequestHandler):
            def reply(self, data):
                body = json.dumps({'code': 0, 'data': data}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                paths.append(self.path)
                if self.path == '/chain/head':
                    self.reply({'height': 64, 'pinHeight': 32, 'pinHash': '00' * 32, 'synced': True})
                elif self.path.startswith('/chain/mine/'):
                    self.reply({'testnet': False})
                elif self.path == '/tools/version':
                    self.reply({'name': '0.8.20'})
                elif self.path == '/transaction/minfee':
                    self.reply({'E8': 9992, 'amount': '0.00009992'})
                elif self.path.endswith('/balance'):
                    self.reply({'balance': '10.00000000'})
                elif '/history/' in self.path:
                    tx = submitted[-1]
                    owner = utils.wallet_from_pkhex('00' * 31 + '01')[0]
                    self.reply({'fromId': 1, 'perBlock': [{'height': 64, 'confirmations': 1,
                        'transactions': {'rewards': [], 'transfers': [dict(
                            txHash=tx['_hash'], fromAddress=owner, toAddress=tx['toAddr'],
                            amount='1.00000000', fee='0.00099968')]}}]})
                elif self.path.startswith('/transaction/lookup/'):
                    tx = submitted[-1]
                    owner = utils.wallet_from_pkhex('00' * 31 + '01')[0]
                    self.reply({'transaction': dict(txHash=tx['_hash'], fromAddress=owner,
                        toAddress=tx['toAddr'], amount='1.00000000', fee='0.00099968',
                        blockHeight=64, confirmations=1)})
                elif self.path == '/chain/block/64/header':
                    self.reply({'header': {'timestamp': 1700000000}})
                else:
                    # This malicious fee response must never be requested.
                    self.reply({'roundedE8': 8589934592})

            def do_POST(self):
                paths.append(self.path)
                submitted.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                tx = submitted[-1]
                preimage = bytes(32) + tx['pinHeight'].to_bytes(4, 'big') + tx['nonceId'].to_bytes(4, 'big') + bytes(3)
                preimage += tx['feeE8'].to_bytes(8, 'big') + bytes.fromhex(tx['toAddr'])[:20] + tx['amountE8'].to_bytes(8, 'big')
                tx['_hash'] = hashlib.sha256(preimage).hexdigest()
                self.reply({'txHash': tx['_hash']})

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        main.db = DB(':memory:')
        save_preferences(main.db, dict(DEFAULTS, auto_lowest_fee=False, auto_failover=False, explorer_history=False))
        main.wallets = main.WartWallets()
        main.mainWindow = main.MainWindow(main.wallets)
        entry = main.WartEntry()
        dialog = login = None

        def wait_until(condition):
            deadline = time.monotonic() + 5
            while not condition() and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertTrue(condition(), 'GUI/API operation did not finish')

        try:
            with patch.object(main, 'message') as notice:
                peer = f'http://127.0.0.1:{server.server_port}'
                entry.ui.input_field.setText(peer)
                entry.private_connect()
                wait_until(lambda: not entry.jobs.running('connect'))
                self.assertEqual(main.PEER, peer)
                addr, key = utils.wallet_from_pkhex('00' * 31 + '01')
                encrypted, salt = utils.encrypt_pk(key.to_string().hex(), 'synthetic password')
                main.db.insert_wallet(addr, encrypted, salt)
                login = main.Login(main.wallets, addr)
                login.login.textEdit.setText('synthetic password')
                login.ok()
                self.assertEqual(login.result(), QDialog.DialogCode.Accepted)
                main.mainWindow.show_wallet()
                wait_until(lambda: main.mainWindow.mainUI.balance.text() == '10.00000000')
                recipient = utils.wallet_from_pkhex('00' * 31 + '02')[0]
                intent = utils.TransferIntent.from_text(recipient, '1', '0.001000100')
                dialog = main.Confirm(main.mainWindow, intent, peer, addr)
                dialog.password.setText('synthetic password')
                dialog.confirm_btn()
                dialog.confirm_btn()
                wait_until(lambda: dialog.result() == QDialog.DialogCode.Accepted)
                self.assertEqual(len(submitted), 1)
                self.assertEqual(submitted[0]['amountE8'], 100000000)
                self.assertEqual(submitted[0]['feeE8'], 99968)
                self.assertEqual(submitted[0]['toAddr'], recipient)
                self.assertEqual(len(submitted[0]['signature65']), 130)
                self.assertFalse(any('/tools/encode16bit/' in path for path in paths))
                self.assertIn('Transaction accepted.', str(notice.call_args))
                main.mainWindow.list_transactions()
                wait_until(lambda: not main.mainWindow.jobs.running('history'))
                row = main.db.history(addr)[0]
                self.assertEqual(row['status'], 'confirmed')
                self.assertEqual(row['confirmations'], 1)
                self.assertEqual(row['timestamp'], 1700000000)
                self.assertEqual(main.mainWindow.history_panel.table.rowCount(), 1)
        finally:
            entry.jobs.cancel_all()
            main.mainWindow.jobs.cancel_all()
            main.mainWindow.refresh_timer.stop()
            main.mainWindow.jobs.pool.waitForDone(6000)
            for widget in [dialog, login, entry, main.mainWindow, main.wallets]:
                if widget is not None:
                    widget.hide()
                    widget.deleteLater()
            APP.processEvents()
            main.db.con.close()
            main.db, main.PEER, main.address = None, None, ''
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
