import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
import zxingcpp

import main
import utils
from db import DB
from payments_ui import address_qr
import test_sessions as sessions
from test_sessions import wait_for

OWNER = sessions.ADDRESS
OTHER = utils.wallet_from_pkhex('00' * 31 + '02')[0]


class PaymentTests(unittest.TestCase):
    def test_removing_one_contact_persists_without_removing_other_entries_for_same_address(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wallet.db'
            store = DB(path)
            try:
                store.insert_friend(OTHER, 'First entry')
                store.insert_friend(OTHER, 'Second entry')
                store.delete_friend(store.get_friends()[0][0])
            finally:
                store.con.close()
            reopened = DB(path)
            try:
                self.assertEqual([(row[1], row[2]) for row in reopened.get_friends()], [(OTHER, 'Second entry')])
            finally:
                reopened.con.close()

    def test_qr_decodes_to_exact_receiving_address_with_independent_decoder(self):
        image = address_qr(OWNER)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receive.png'
            self.assertTrue(image.save(str(path)))
            with Image.open(path) as png:
                decoded = zxingcpp.read_barcode(png)
            self.assertIsNotNone(decoded)
            self.assertEqual(decoded.text, OWNER)
        with self.assertRaises(ValueError):
            address_qr('bad address')

    def test_pending_reservations_are_atomic_between_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wallet.db'
            initial = DB(path)
            initial.con.close()
            barrier = Barrier(2)
            def attempt(index):
                store = DB(path)
                try:
                    barrier.wait(timeout=5)
                    try:
                        store.record_outgoing(OWNER, utils.TransferIntent(OTHER, 60, 1),
                            utils.SignedTransfer(f'{index:064x}', {}), 'http://localhost:3000', 100)
                        return 'saved'
                    except ValueError:
                        return 'blocked'
                finally:
                    store.con.close()
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(attempt, [1, 2]))
            self.assertCountEqual(results, ['saved', 'blocked'])
            final = DB(path)
            try:
                self.assertEqual(final.reserved_amount(OWNER), 61)
                self.assertEqual(len(final.history(OWNER)), 1)
            finally:
                final.con.close()


class PaymentGuiTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    tearDown = sessions.SessionTests.tearDown
    confirm = sessions.SessionTests.confirm

    def test_receive_view_keeps_full_address_qr_and_copy_button_in_sync(self):
        with patch.object(self.window, 'refresh_node_status'):
            self.window.show_wallet()
        self.assertEqual(self.window.mainUI.address_text.text(), OWNER)
        self.assertTrue(self.window.mainUI.address_text.isReadOnly())
        self.assertFalse(self.window.receive_qr.pixmap().isNull())
        self.window.copy_to_clipboard()
        self.assertEqual(sessions.APP.clipboard().text(), OWNER)
        self.window.mainUI.addr_text.setText(OTHER)
        self.window.lock_wallet()
        self.assertEqual(self.window.mainUI.addr_text.text(), '')
        self.assertEqual(self.window.mainUI.address_text.text(), '')

    def ready_balance(self, value):
        self.window.balance_snapshot = (main.PEER, OWNER, value, time.monotonic())
        self.window.set_node_status(dict(peer=main.PEER, network='mainnet', height=64,
            synced=True, version='0.8.20', checked_at=time.time()))

    def test_send_max_subtracts_pending_payments_and_final_locally_rounded_fee(self):
        self.ready_balance(200000000)
        main.db.record_outgoing(OWNER, utils.TransferIntent(OTHER, 100000000, 1),
                                utils.SignedTransfer('ab' * 32, {}), main.PEER)
        self.window.mainUI.fee_text.setText('0.001000100')
        self.window.send_max()
        self.assertEqual(self.window.mainUI.amount_text.text(), '0.99900031')
        self.assertIn('Final fee: 0.00099968', self.window.send_panel.summary.text())
        self.assertIn('Total: 0.99999999', self.window.send_panel.summary.text())

    def test_contact_selection_and_invalid_contact_validation(self):
        self.window.mainUI.addr_text_3.setText(OTHER)
        self.window.mainUI.addr_text_2.setText('My contact')
        self.window.add_friend()
        self.window.send_panel.contacts.setCurrentIndex(1)
        self.assertEqual(self.window.mainUI.addr_text.text(), OTHER)
        panel = self.window.contacts_panel
        self.assertEqual(panel.table.rowCount(), 1)
        self.assertEqual(panel.table.item(0, 0).text(), 'My contact')
        self.assertEqual(panel.table.item(0, 1).text(), OTHER)
        panel.table.selectRow(0)
        self.assertTrue(panel.copy.isEnabled())
        panel.copy.click()
        self.assertEqual(sessions.APP.clipboard().text(), OTHER)
        self.window.mainUI.addr_text_3.setText('invalid')
        self.window.add_friend()
        self.assertEqual(len(main.db.get_friends()), 1)

    def test_remove_contact_requires_selection_and_confirmation_and_refreshes_send_contacts(self):
        main.db.insert_friend(OTHER, 'First entry')
        main.db.insert_friend(OTHER, 'Second entry')
        self.window.get_friends()
        panel = self.window.contacts_panel
        with patch.object(main.QMessageBox, 'exec') as prompt:
            self.window.remove_friend()
            prompt.assert_not_called()
        self.assertFalse(panel.remove.isEnabled())
        panel.table.selectRow(0)
        self.assertTrue(panel.remove.isEnabled())
        with patch.object(main.QMessageBox, 'exec', return_value=main.QMessageBox.StandardButton.No):
            panel.remove.click()
        self.assertEqual(panel.table.rowCount(), 2)
        with patch.object(main.QMessageBox, 'exec', return_value=main.QMessageBox.StandardButton.Yes):
            panel.remove.click()
        self.assertEqual([(row[1], row[2]) for row in main.db.get_friends()], [(OTHER, 'Second entry')])
        self.assertEqual(panel.table.rowCount(), 1)
        self.assertEqual(panel.table.item(0, 0).text(), 'Second entry')
        self.assertEqual(self.window.send_panel.contacts.count(), 2)
        self.assertIn('Second entry', self.window.send_panel.contacts.itemText(1))
        self.assertFalse(panel.remove.isEnabled())

    def test_insufficient_or_stale_balance_never_opens_confirmation(self):
        self.ready_balance(100)
        self.window.mainUI.addr_text.setText(OTHER)
        self.window.mainUI.amount_text.setText('1')
        with patch.object(main, 'Confirm') as confirm:
            self.window.send_wart()
            confirm.assert_not_called()
            self.window.balance_snapshot = (main.PEER, OWNER, 200000000, time.monotonic() - 100)
            self.window.send_wart()
            confirm.assert_not_called()

    def test_fresh_balance_is_rechecked_after_password_before_signing(self):
        dialog = self.confirm()
        dialog.password.setText('wallet password')
        with patch.object(main.NodeClient, 'balance', return_value='0.00000001'), \
             patch.object(main, 'prepare_tx') as prepare, patch.object(main.NodeClient, 'broadcast') as post:
            dialog.confirm_btn()
            wait_for(lambda: not dialog.jobs.running('send'))
            prepare.assert_not_called()
            post.assert_not_called()
        self.assertEqual(main.db.history(OWNER), [])

    def test_confirmation_shows_immutable_total_and_masked_password(self):
        dialog = self.confirm()
        self.assertEqual(dialog.total_label.text(), '0.00000101 WART')
        self.assertEqual(dialog.password.echoMode(), main.QLineEdit.EchoMode.Password)
