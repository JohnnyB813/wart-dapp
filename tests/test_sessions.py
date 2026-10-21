import os
from pathlib import Path
import tempfile
import time
from threading import Event
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

import main
import utils
from db import DB
from wallet_settings import save_preferences, DEFAULTS

APP = QApplication.instance() or QApplication([])
KEY_HEX = '00' * 31 + '01'
ADDRESS = utils.wallet_from_pkhex(KEY_HEX)[0]


def wait_for(condition):
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(10)
    if not condition():
        raise AssertionError('GUI operation did not complete')


class SessionTests(unittest.TestCase):
    def setUp(self):
        main.db = DB(':memory:')
        # Session tests use a synthetic local node; keep bundled public endpoints offline.
        for name, url, network in main.db.node_profiles():
            if url != 'http://localhost:3000':
                main.db.save_node_profile(name, url, network, enabled=False)
        save_preferences(main.db, dict(DEFAULTS, auto_lowest_fee=False, auto_failover=False))
        token, salt = utils.encrypt_pk(KEY_HEX, 'wallet password')
        main.db.insert_wallet(ADDRESS, token, salt)
        main.address, main.PEER = ADDRESS, 'http://localhost:3000'
        self.wallets = main.WartWallets()
        self.window = main.MainWindow(self.wallets)
        self.widgets = [self.window, self.wallets]
        self.notices = patch.object(main, 'message').start()
        self.refresh_patch = patch.object(self.window, 'refresh_balance').start()
        patch.object(main.NodeClient, 'balance', return_value='10.00000000').start()
        patch.object(main.NodeClient, 'minimum_fee', return_value=0).start()

    def tearDown(self):
        self.window.jobs.cancel_all()
        self.wallets.backup_jobs.cancel_all()
        self.window.jobs.pool.waitForDone(6000)
        for widget in self.widgets:
            widget.hide()
            widget.deleteLater()
        APP.processEvents()
        main.db.con.close()
        main.db, main.PEER, main.address = None, None, ''
        patch.stopall()

    def confirm(self):
        dialog = main.Confirm(self.window, utils.TransferIntent(ADDRESS, 100, 1), main.PEER, ADDRESS)
        self.widgets.insert(0, dialog)
        self.window.active_confirm = dialog
        return dialog

    def test_login_checks_password_without_retaining_signing_key(self):
        main.address = ''
        login = main.Login(self.wallets, ADDRESS)
        self.widgets.insert(0, login)
        login.login.textEdit.setText('wrong')
        login.ok()
        self.assertEqual(main.address, '')
        login.login.textEdit.setText('wallet password')
        login.ok()
        self.assertEqual(main.address, ADDRESS)
        self.assertEqual(login.result(), QDialog.DialogCode.Accepted)
        self.assertIsNone(getattr(main, 'privkey', None))

    def test_each_send_requires_password_and_cancel_never_sends(self):
        dialog = self.confirm()
        prepared = utils.SignedTransfer('ab' * 32, {'signed': 'public data'})
        with patch.object(main, 'prepare_tx', return_value=prepared) as send, \
             patch.object(main.NodeClient, 'broadcast', return_value=prepared.tx_hash) as broadcast:
            dialog.password.setText('wrong password')
            dialog.confirm_btn()
            wait_for(lambda: not dialog.jobs.running('send'))
            send.assert_not_called()
            self.assertTrue(dialog.password.isEnabled())
            dialog.password.setText('wallet password')
            dialog.confirm_btn()
            wait_for(lambda: dialog.result() == QDialog.DialogCode.Accepted)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(broadcast.call_count, 1)
            self.assertEqual(dialog.password.text(), '')
            another = self.confirm()
            another.password.setText('wallet password')
            another.reject()
            self.assertEqual(another.password.text(), '')
            self.assertEqual(send.call_count, 1)

    def test_manual_lock_clears_session_and_invalidates_open_confirmation(self):
        dialog = self.confirm()
        dialog.password.setText('wallet password')
        self.window.lock_wallet()
        self.assertEqual(main.address, '')
        self.assertEqual(dialog.password.text(), '')
        with patch.object(dialog.jobs, 'submit') as submit:
            dialog.confirm_btn()
            submit.assert_not_called()
        self.assertFalse(self.window.idle_timer.isActive())

    def test_activity_resets_idle_timeout_then_wallet_locks(self):
        self.window.idle_timer.setInterval(150)
        self.window.show_wallet()
        QTest.qWait(80)
        QTest.keyClick(self.window.mainUI.amount_text, Qt.Key.Key_1)
        QTest.qWait(90)
        self.assertEqual(main.address, ADDRESS)
        wait_for(lambda: main.address == '')
        self.assertFalse(self.window.isVisible())

    def test_lock_during_authorized_send_finishes_once_without_reopening_wallet(self):
        dialog = self.confirm()
        dialog.password.setText('wallet password')
        started = Event()
        def delayed(*args):
            started.set()
            time.sleep(0.15)
            return 'ab' * 32
        prepared = utils.SignedTransfer('ab' * 32, {'signed': 'public data'})
        with patch.object(main, 'prepare_tx', return_value=prepared), \
             patch.object(main.NodeClient, 'broadcast', side_effect=delayed) as send:
            dialog.confirm_btn()
            wait_for(started.is_set)
            self.window.lock_wallet()
            wait_for(lambda: not dialog.jobs.running('send'))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(main.address, '')
            self.assertFalse(self.window.isVisible())
            self.assertEqual(dialog.password.text(), '')

    def test_backup_and_restore_buttons_complete_real_encrypted_file_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'wallet.wartbackup')
            with patch.object(main.QFileDialog, 'getSaveFileName', return_value=(path, '')), \
                 patch.object(main.QInputDialog, 'getText', side_effect=[('backup password', True)] * 2):
                self.wallets.backup_wallets()
                wait_for(lambda: not self.wallets.backup_jobs.running('backup'))
            main.db.delete_wallet(ADDRESS)
            with patch.object(main.QFileDialog, 'getOpenFileName', return_value=(path, '')), \
                 patch.object(main.QInputDialog, 'getText', return_value=('backup password', True)):
                self.wallets.restore_backup()
                wait_for(lambda: not self.wallets.backup_jobs.running('backup'))
            self.assertIsNotNone(main.db.get_wallet_by_address(ADDRESS))
            self.assertEqual(self.wallets.wallets.listWidget.count(), 1)
