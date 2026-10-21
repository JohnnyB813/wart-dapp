import os
from pathlib import Path
import tempfile
import time
import unittest
import sqlite3
from unittest.mock import patch
from contextlib import closing

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import main
from db import DB
from node_client import NodeClient, NodeError
import test_sessions as sessions
from test_sessions import wait_for


class ProfileTests(unittest.TestCase):
    def test_named_lan_profiles_and_last_connection_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wallet.db'
            original = DB(path)
            original.save_node_profile('LAN node', 'http://192.168.1.50:3000')
            original.update_peer('http://192.168.1.50:3000')
            original.set_setting('selected_node', 'LAN node')
            snapshot = original.snapshot()
            original.con.close()
            reopened = DB(path)
            restored = DB(':memory:')
            try:
                self.assertEqual(reopened.get_peer(), 'http://192.168.1.50:3000')
                self.assertEqual(reopened.setting('selected_node'), 'LAN node')
                self.assertIn(('LAN node', 'http://192.168.1.50:3000', 'mainnet'), reopened.node_profiles())
                restored.restore_snapshot(snapshot)
                self.assertIn(('LAN node', 'http://192.168.1.50:3000', 'mainnet'), restored.node_profiles())
                reopened.save_node_profile('LAN node', 'http://10.0.0.10:3000')
                self.assertEqual(len([r for r in reopened.node_profiles() if r[0] == 'LAN node']), 1)
                reopened.delete_node_profile('LAN node')
                self.assertFalse(any(r[0] == 'LAN node' for r in reopened.node_profiles()))
            finally:
                reopened.con.close()
                restored.con.close()

    def test_public_http_profile_and_empty_name_rejected(self):
        store = DB(':memory:')
        try:
            with self.assertRaises(NodeError):
                store.save_node_profile('Public', 'http://8.8.8.8:3000')
            with self.assertRaises(ValueError):
                store.save_node_profile(' ', 'http://127.0.0.1:3000')
            self.assertEqual(len(store.node_profiles()), 6)
        finally:
            store.con.close()

    def test_all_six_requested_nodes_are_saved_on_the_correct_network(self):
        store = DB(':memory:')
        try:
            expected = {
                'mainnet': {'http://localhost:3000', 'https://mainnet.wartnode.com', 'https://warthognode.duckdns.org'},
                'testnet': {'http://localhost:3100', 'https://testnet.wartnode.com', 'https://warthog-defitestnet.duckdns.org'},
            }
            for network, urls in expected.items():
                self.assertEqual(set(store.node_candidates(network)), urls)
                self.assertEqual(len(store.configured_nodes(network)), 3)
        finally:
            store.con.close()

    def test_upgrade_adds_missing_defaults_once_preserving_custom_profiles_and_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.db'
            # Simulate an older wallet, including a conflicting name and trailing-slash URL.
            with closing(sqlite3.connect(path)) as legacy, legacy:
                legacy.execute('CREATE TABLE data(id INTEGER PRIMARY KEY, key TEXT, value TEXT)')
                legacy.execute("INSERT INTO data(key, value) VALUES ('peer', 'http://192.168.1.50:3000')")
                legacy.execute('CREATE TABLE node_profiles(name TEXT PRIMARY KEY, url TEXT, network TEXT, enabled INTEGER, priority INTEGER)')
                legacy.executemany('INSERT INTO node_profiles VALUES (?, ?, ?, ?, ?)', [
                    ('My mainnet', 'https://mainnet.wartnode.com/', 'mainnet', 0, 4),
                    ('Local DeFi testnet', 'http://192.168.1.60:3100', 'testnet', 1, 8),
                    ('Wartnode DeFi testnet', 'https://testnet.wartnode.com', 'testnet', 0, 3),
                ])
            store = DB(path)
            original = [
                ('My mainnet', 'https://mainnet.wartnode.com/', 'mainnet', 0, 4),
                ('Local DeFi testnet', 'http://192.168.1.60:3100', 'testnet', 1, 8),
                ('Wartnode DeFi testnet', 'https://testnet.wartnode.com', 'testnet', 0, 3),
            ]
            try:
                for row in original:
                    self.assertEqual(store.con.execute('SELECT * FROM node_profiles WHERE name=?', (row[0],)).fetchone(), row)
                self.assertEqual(len(store.node_profiles()), 7)
                self.assertEqual(store.get_peer(), 'http://192.168.1.50:3000')
                self.assertNotIn('https://mainnet.wartnode.com/', store.node_candidates('mainnet'))
                self.assertIn(('Local DeFi testnet (2)', 'http://localhost:3100', 'testnet'), store.node_profiles())
                store.delete_node_profile('Warthog mainnet')
                snapshot = store.node_profiles()
            finally:
                store.con.close()
            reopened = DB(path)
            try:
                self.assertEqual(reopened.node_profiles(), snapshot)
                self.assertNotIn('https://warthognode.duckdns.org', reopened.node_candidates('mainnet'))
                for name, _, _, enabled, _ in original:
                    self.assertEqual(reopened.con.execute('SELECT enabled FROM node_profiles WHERE name=?', (name,)).fetchone()[0], enabled)
            finally:
                reopened.con.close()

    def test_status_reports_network_height_sync_and_node_version(self):
        client = NodeClient('http://192.168.1.50:3000')
        def request(path):
            if path.startswith('/chain/mine/'):
                return {'testnet': False}
            if path.endswith('/balance'):
                return {'balance': '0.00000000'}
            if path == '/tools/version':
                return {'name': '0.8.20'}
            if path == '/transaction/minfee':
                return {'E8': 9992, 'amount': '0.00009992', '16bit': 10000}
            raise AssertionError(path)
        with patch.object(client, 'head', return_value={'height': 64, 'synced': True}), patch.object(client, 'request', side_effect=request):
            state = client.probe()
        self.assertEqual((state['network'], state['height'], state['synced'], state['version']), ('mainnet', 64, True, '0.8.20'))
        self.assertLess(abs(state['checked_at'] - time.time()), 2)

    def test_testnet_and_missing_identity_never_authorize_send(self):
        client = NodeClient('http://localhost:3000')
        for mining in [{'testnet': True}, {'testnet': 'false'}, {}]:
            with self.subTest(mining=mining), patch.object(client, 'request', return_value=mining), self.assertRaises(NodeError):
                client.require_mainnet()
        with patch.object(client, 'head', return_value={'height': 64, 'synced': True}), \
             patch.object(client, 'network', return_value='testnet'), \
             patch.object(client, 'request', return_value={'name': 'testnet'}), self.assertRaisesRegex(NodeError, 'testnet'):
            client.probe()


class ProfileGuiTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    tearDown = sessions.SessionTests.tearDown

    def test_saved_selection_and_unsaved_last_lan_url_survive_new_entry(self):
        main.db.save_node_profile('My LAN', 'http://192.168.1.50:3000')
        main.db.set_setting('selected_node', 'My LAN')
        main.db.update_peer('http://192.168.1.50:3000')
        entry = main.WartEntry()
        self.widgets.insert(0, entry)
        self.assertEqual(entry.profiles.currentText(), 'My LAN')
        self.assertEqual(entry.ui.input_field.text(), 'http://192.168.1.50:3000')
        main.db.update_peer('http://10.0.0.50:3000')
        entry.load_profiles()
        self.assertEqual(entry.ui.input_field.text(), 'http://10.0.0.50:3000')
        self.assertEqual(entry.profiles.currentIndex(), -1)

    def test_offline_status_retains_last_success_and_disables_send(self):
        state = dict(peer=main.PEER, network='mainnet', synced=True, height=64,
                     version='0.8.20', checked_at=time.time())
        self.window.set_node_status(state)
        self.window.show()
        with patch.object(main.NodeClient, 'status', side_effect=NodeError('Connection refused')):
            self.window.refresh_node_status()
            wait_for(lambda: not self.window.jobs.running('status'))
        self.assertFalse(self.window.mainUI.send_btn.isEnabled())
        self.assertFalse(self.window.node_state['online'])
        self.assertIn('Last successful update', self.window.statusBar().currentMessage())
        self.window.set_node_status(dict(state, synced=False))
        self.assertIn('Synchronizing', self.window.statusBar().currentMessage())
        self.assertFalse(self.window.mainUI.send_btn.isEnabled())
