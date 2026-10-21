import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

from db import DB
from history import fetch_account_page, reconcile_local, LATEST_CURSOR, display_date
from history_ui import HistoryPanel
from node_client import NodeClient, NodeError, BroadcastUncertain
from utils import wallet_from_pkhex, TransferIntent, SignedTransfer
import main
import test_sessions as sessions
from test_sessions import wait_for

OWNER = wallet_from_pkhex('00' * 31 + '01')[0]
OTHER = wallet_from_pkhex('00' * 31 + '02')[0]
THIRD = wallet_from_pkhex('00' * 31 + '03')[0]
APP = QApplication.instance() or QApplication([])


def transfer(**extra):
    return dict(txHash='ab' * 32, fromAddress=OTHER, toAddress=OWNER,
                amount='1.00000000', fee='0.00000001', **extra)


class HistoryTests(unittest.TestCase):
    def test_node_history_cannot_overwrite_locally_approved_payment_amounts(self):
        store = DB(':memory:')
        try:
            store.record_outgoing(OWNER, TransferIntent(OTHER, 100, 1), SignedTransfer('ab' * 32, {}), 'http://localhost:3000')
            record = store.history(OWNER)[0]
            altered = dict(record, amount_e8='999', status='confirmed')
            with self.assertRaisesRegex(ValueError, 'locally approved'):
                store.save_history([altered])
            self.assertEqual(store.history(OWNER)[0], record)
        finally:
            store.con.close()

    def test_account_endpoint_filters_owner_and_loads_real_block_dates(self):
        client = MagicMock(peer='http://192.168.1.50:3000')
        page = {'fromId': 20, 'perBlock': [{'height': 50, 'confirmations': 2,
                'transactions': {'transfers': [transfer()], 'rewards': []}}]}
        client.request.side_effect = [page, {'header': {'timestamp': 1700000000}}]
        rows, cursor = fetch_account_page(client, OWNER)
        self.assertEqual(cursor, 20)
        self.assertEqual(rows[0]['direction'], 'incoming')
        self.assertEqual(rows[0]['counterparty'], OTHER)
        self.assertEqual(rows[0]['fee_e8'], '1')
        self.assertEqual(rows[0]['confirmations'], 2)
        self.assertEqual(display_date(rows[0]['timestamp']), '2023-11-14 22:13 UTC')
        self.assertEqual(client.request.call_args_list[0].args[0], f'/account/{OWNER}/history/{LATEST_CURSOR}')
        self.assertEqual(client.request.call_args_list[1].args[0], '/chain/block/50/header')
        page['perBlock'][0]['transactions']['transfers'][0]['toAddress'] = THIRD
        client.request.side_effect = [page, {'header': {'timestamp': 1700000000}}]
        with self.assertRaisesRegex(NodeError, 'another wallet'):
            fetch_account_page(client, OWNER)

    def test_cursor_progress_and_empty_new_account(self):
        client = MagicMock()
        client.request.return_value = {'fromId': 30, 'perBlock': []}
        with self.assertRaisesRegex(NodeError, 'non-advancing'):
            fetch_account_page(client, OWNER, 30)
        client.request.side_effect = NodeError('not found', 57)
        self.assertEqual(fetch_account_page(client, OWNER), ([], 0))

    def test_pending_uncertain_and_confirmed_reconciliation(self):
        store = DB(':memory:')
        try:
            store.record_outgoing(OWNER, TransferIntent(OTHER, 100000000, 1),
                                  SignedTransfer('ab' * 32, {'signature65': 'public signature'}), 'http://localhost:3000')
            record = store.history(OWNER)[0]
            client = MagicMock(peer='http://localhost:3000')
            client.lookup.return_value = None
            unknown = reconcile_local(client, [record])[0]
            self.assertEqual(unknown['status'], 'not_found')
            self.assertEqual(unknown['payload'], record['payload'])
            tx = transfer(blockHeight=0, confirmations=0)
            tx.update(fromAddress=OWNER, toAddress=OTHER)
            client.lookup.return_value = tx
            pending = reconcile_local(client, [record])[0]
            self.assertEqual(pending['status'], 'pending')
            tx.update(blockHeight=50, confirmations=3)
            client.request.return_value = {'header': {'timestamp': 1700000000}}
            confirmed = reconcile_local(client, [record])[0]
            self.assertEqual(confirmed['status'], 'confirmed')
            store.save_history([confirmed])
            restored = DB(':memory:')
            try:
                restored.restore_snapshot(store.snapshot())
                self.assertEqual(restored.history(OWNER)[0], store.history(OWNER)[0])
            finally:
                restored.con.close()
            tx['amount'] = '999'
            with self.assertRaisesRegex(NodeError, 'does not match'):
                reconcile_local(client, [record])
        finally:
            store.con.close()

    def test_lookup_not_found_and_mismatched_broadcast_id(self):
        client = NodeClient('http://localhost:3000')
        with patch.object(client, 'request', side_effect=NodeError('not found', -57)):
            self.assertIsNone(client.lookup('ab' * 32))
        with patch.object(client, 'request', return_value={'txHash': 'cd' * 32}), self.assertRaises(BroadcastUncertain):
            client.broadcast({}, 'ab' * 32)


class JournalGuiTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    tearDown = sessions.SessionTests.tearDown
    confirm = sessions.SessionTests.confirm

    def test_selected_uncertain_payment_is_looked_up_without_a_chain_scan(self):
        main.db.record_outgoing(OWNER, TransferIntent(OTHER, 100, 1), SignedTransfer('cd' * 32, {}), main.PEER)
        self.window.history_panel.show_rows(main.db.history(OWNER))
        self.window.history_panel.table.selectRow(0)
        with patch.object(main.NodeClient, 'require_mainnet'), \
             patch.object(main.NodeClient, 'lookup', return_value=None) as lookup, \
             patch.object(main, 'fetch_account_page') as history:
            self.window.check_selected_history()
            wait_for(lambda: not self.window.jobs.running('history'))
            lookup.assert_called_once_with('cd' * 32)
            history.assert_not_called()
        self.assertEqual(main.db.history(OWNER)[0]['status'], 'not_found')
    # Exercise the journal through the actual confirmation state machine.
    def test_journal_committed_before_post_and_uncertain_result_survives(self):
        dialog = self.confirm()
        dialog.password.setText('wallet password')
        prepared = SignedTransfer('cd' * 32, {'signature65': 'public signature'})
        journaled = []
        original = main.db.record_outgoing
        def record(*args):
            original(*args)
            journaled.extend(main.db.history(OWNER))
        def broadcast(*args):
            self.assertEqual(journaled[0]['status'], 'broadcasting')
            self.assertEqual(journaled[0]['tx_hash'], prepared.tx_hash)
            raise BroadcastUncertain('Connection lost after POST')
        with patch.object(main, 'prepare_tx', return_value=prepared), \
             patch.object(main.db, 'record_outgoing', side_effect=record), \
             patch.object(main.NodeClient, 'broadcast', side_effect=broadcast):
            dialog.confirm_btn()
            wait_for(lambda: not dialog.jobs.running('send'))
        row = main.db.history(OWNER)[0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['tx_hash'], prepared.tx_hash)
        self.window.history_panel.show_rows(main.db.history(OWNER))
        self.assertEqual(self.window.history_panel.table.rowCount(), 1)
        self.assertEqual(self.window.history_panel.table.item(0, 6).text(), prepared.tx_hash)
