import os
from threading import Event
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

import main
from history import LATEST_CURSOR, record_from_transaction
from mined_blocks import fetch_mined_page, mined_blocks
from node_client import NodeError
from utils import wallet_from_pkhex
import test_sessions as sessions
from test_sessions import wait_for

OWNER = sessions.ADDRESS
OTHER = wallet_from_pkhex('00' * 31 + '02')[0]
APP = QApplication.instance() or QApplication([])
PEER = 'http://localhost:3000'


def reward(tx_hash='ab' * 32, owner=OWNER):
    return dict(txHash=tx_hash, toAddress=owner, amount='1.50000001')


def saved(tx_hash='ab' * 32, owner=OWNER, height=50):
    return record_from_transaction(reward(tx_hash, owner), owner, height, 2, 1700000000, PEER)


def page(rewards, cursor=20, height=50):
    return {'fromId': cursor, 'perBlock': [{'height': height, 'confirmations': 2,
            'transactions': {'rewards': rewards, 'transfers': []}}]}


class MinedBlockTests(unittest.TestCase):
    def test_paginated_rewards_have_header_hashes_and_no_chain_scan(self):
        client = MagicMock(peer=PEER)
        response = page([reward()])
        response['perBlock'][0]['transactions']['transfers'] = [dict(
            txHash='cd' * 32, fromAddress=OTHER, toAddress=OWNER, amount='9.00000000', fee='0.00000001')]
        client.request.side_effect = [response, {'header': {'timestamp': 1700000000, 'hash': 'EF' * 32}}]
        rows, cursor, details = fetch_mined_page(client, OWNER)
        blocks = mined_blocks(rows, OWNER, details)
        self.assertEqual(cursor, 20)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['reward_e8'], 150000001)
        self.assertEqual(blocks[0]['block_hash'], 'ef' * 32)
        client.require_mainnet.assert_called_once()
        self.assertEqual([call.args[0] for call in client.request.call_args_list], [
            f'/account/{OWNER}/history/{LATEST_CURSOR}', '/chain/block/50/header'])

    def test_page_without_rewards_can_still_load_older(self):
        client = MagicMock(peer=PEER)
        client.request.side_effect = [page([], 20), {'header': {'timestamp': 1700000000}},
                                      page([reward()], 5, 40),
                                      {'header': {'timestamp': 1600000000, 'hash': 'ef' * 32}}]
        rows, cursor, details = fetch_mined_page(client, OWNER)
        self.assertEqual((mined_blocks(rows, OWNER), cursor), ([], 20))
        rows, cursor, details = fetch_mined_page(client, OWNER, cursor)
        self.assertEqual(mined_blocks(rows, OWNER, details)[0]['height'], 40)
        self.assertEqual(cursor, 5)

    def test_unrelated_rewards_and_invalid_block_hash_are_rejected(self):
        client = MagicMock(peer=PEER)
        for response, header in [(page([reward(owner=OTHER)]), {'timestamp': 1700000000, 'hash': 'ef' * 32}),
                                 (page([reward()]), {'timestamp': 1700000000, 'hash': 'bad hash'})]:
            client.request.side_effect = [response, {'header': header}]
            with self.assertRaises(NodeError):
                fetch_mined_page(client, OWNER)

    def test_multiple_rewards_group_per_block_and_exclude_other_wallet_or_orphans(self):
        rows = [saved(), saved('cd' * 32), saved('ef' * 32, OTHER),
                dict(saved('12' * 32), status='not_found', block_height=0, confirmations=0)]
        blocks = mined_blocks(rows, OWNER)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['reward_e8'], 300000002)
        self.assertEqual(blocks[0]['reward_ids'], ['ab' * 32, 'cd' * 32])

    def test_missing_reward_is_removed_and_stale_block_hash_is_not_shown(self):
        client = MagicMock(peer=PEER)
        client.request.return_value = {'fromId': 0, 'perBlock': []}
        client.lookup.return_value = None
        rows, cursor, details = fetch_mined_page(client, OWNER, known=[saved()])
        self.assertEqual(rows[0]['status'], 'not_found')
        self.assertEqual((mined_blocks(rows, OWNER), cursor, details), ([], 0, {}))
        stale = {'ab' * 32: dict(height=49, timestamp=1700000000, block_hash='ef' * 32)}
        self.assertEqual(mined_blocks([saved()], OWNER, stale)[0]['block_hash'], '')


class MinedGuiTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    tearDown = sessions.SessionTests.tearDown

    def test_refresh_persists_rewards_paginates_and_copies_block_hash(self):
        detail = {'ab' * 32: dict(height=50, timestamp=1700000000, block_hash='ef' * 32)}
        with patch.object(main, 'fetch_mined_page', side_effect=[([saved()], 20, detail), ([], 0, {})]) as fetch:
            self.window.list_mined_blocks()
            wait_for(lambda: not self.window.jobs.running('mined'))
            panel = self.window.mined_panel
            self.assertEqual(panel.table.rowCount(), 1)
            self.assertIn('1.50000001 WART', panel.summary.text())
            panel.copy_hash()
            self.assertEqual(APP.clipboard().text(), 'ef' * 32)
            self.assertEqual(main.db.history(OWNER)[0]['direction'], 'mined')
            self.window.load_older_mined_blocks()
            wait_for(lambda: not self.window.jobs.running('mined'))
            self.assertEqual(fetch.call_args.args[2], 20)
            self.assertFalse(panel.older.isEnabled())
            self.assertEqual(panel.table.rowCount(), 1)
        with patch.object(main, 'fetch_mined_page', side_effect=NodeError('Offline')):
            self.window.list_mined_blocks()
            wait_for(lambda: not self.window.jobs.running('mined'))
            self.assertEqual(panel.table.rowCount(), 1)
            self.assertIn('Showing saved rewards', panel.notice.text())

    def test_lock_cancels_late_results_and_switching_wallet_clears_details(self):
        started, finish = Event(), Event()
        def fetch(*args):
            started.set()
            finish.wait(3)
            return [saved()], 20, {}
        with patch.object(main, 'fetch_mined_page', side_effect=fetch):
            self.window.list_mined_blocks()
            wait_for(started.is_set)
            self.window.lock_wallet()
            finish.set()
            self.window.jobs.pool.waitForDone(4000)
            APP.processEvents()
            self.assertEqual(self.window.mined_panel.table.rowCount(), 0)
            self.assertEqual(main.db.history(OWNER), [])
        main.db.save_history([saved(), saved('cd' * 32, OTHER, 40)])
        main.address = OTHER
        with patch.object(self.window, 'refresh_node_status'):
            self.window.show_wallet()
        self.assertEqual(self.window.mined_panel.table.rowCount(), 1)
        self.assertEqual(self.window.mined_panel.table.item(0, 0).text(), '40')
        self.assertEqual(self.window.mined_details, {})
        self.assertFalse(self.window.mined_panel.copy.isEnabled())
