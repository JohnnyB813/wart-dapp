import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication

import main
from app_info import VERSION, release_notes
from app_paths import default_wallet_path, legacy_wallets
from db import DB
from smoke_checks import verify_runtime
import test_sessions as sessions
from test_sessions import wait_for


class ReleaseTests(unittest.TestCase):
    def test_single_version_and_offline_crypto_backup_qr_runtime(self):
        expected = (Path(__file__).resolve().parents[1] / 'version.txt').read_text().strip()
        self.assertEqual(VERSION, expected)
        self.assertIn('Warthog Wallet ' + VERSION, release_notes())
        self.assertNotIn('Version 1.0', release_notes())
        verify_runtime()

    def test_production_and_test_profiles_have_separate_stable_directories(self):
        app = QCoreApplication.instance()
        original_name, original_org = app.applicationName(), app.organizationName()
        try:
            app.setOrganizationName('WarthogNetwork')
            app.setApplicationName('WartWallet')
            normal = default_wallet_path()
            app.setApplicationName('WartWallet-Test')
            testing = default_wallet_path()
            self.assertNotEqual(normal.parent, testing.parent)
            self.assertEqual(normal.name, testing.name)
        finally:
            app.setOrganizationName(original_org)
            app.setApplicationName(original_name)

    def test_legacy_test_profile_never_automatically_imports_production_database(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ('wartwallet.db', 'wallet-test.db'):
                (folder / name).touch()
            with patch('app_paths.Path.cwd', return_value=folder), \
                 patch('app_paths.sys.frozen', True, create=True), \
                 patch('app_paths.sys.executable', str(folder / 'wallet.exe')):
                self.assertEqual(legacy_wallets('wallet-test.db'), [(folder / 'wallet-test.db').resolve()])
                self.assertEqual(legacy_wallets(), [(folder / 'wartwallet.db').resolve()])


class ReleaseGuiTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    tearDown = sessions.SessionTests.tearDown

    def test_current_version_and_release_notes_are_visible(self):
        self.assertIn(VERSION, self.window.windowTitle())
        self.assertIn(VERSION, self.wallets.windowTitle())
        self.assertIn('Password authorization for each payment', self.window.release_text.toPlainText())
        self.assertNotIn('Thursday, 15 February 2024', self.window.release_text.toPlainText())

    def test_old_database_can_be_imported_without_modifying_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old-wallet.db'
            original = DB(path)
            record = main.db.get_wallets()[0]
            original.insert_wallet(record[1], record[2], record[3])
            main.db.delete_wallet(record[1])
            try:
                with patch.object(main.QFileDialog, 'getOpenFileName', return_value=(str(path), '')):
                    self.wallets.import_database()
                    wait_for(lambda: not self.wallets.backup_jobs.running('backup'))
                self.assertEqual(len(main.db.get_wallets()), 1)
                self.assertEqual(original.get_wallets()[0][1:4], record[1:4])
            finally:
                original.con.close()
