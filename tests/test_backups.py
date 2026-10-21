import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from app_paths import prepare_wallet_path, MigrationChoiceRequired, default_wallet_path
from db import DB
from utils import encrypt_pk, decrypt_pk, wallet_from_pkhex
from wallet_backup import encrypt_snapshot, decrypt_snapshot, save_backup, load_backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(':memory:')
        self.address = wallet_from_pkhex('00' * 31 + '01')[0]
        token, salt = encrypt_pk('00' * 31 + '01', 'original password')
        self.db.insert_wallet(self.address, token, salt)
        self.db.insert_friend(self.address, 'Saved contact')

    def tearDown(self):
        self.db.con.close()

    def test_encrypted_file_roundtrip_retains_wallet_password_and_contacts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.wartbackup'
            save_backup(self.db.snapshot(), 'backup password', path)
            self.assertNotIn(self.address.encode(), path.read_bytes())
            restored = DB(':memory:')
            try:
                self.assertEqual(restored.restore_snapshot(load_backup(path, 'backup password')), 1)
                record = restored.get_wallet_by_address(self.address)
                self.assertEqual(decrypt_pk(record[2], 'original password', record[3]), '00' * 31 + '01')
                self.assertIsNone(decrypt_pk(record[2], 'backup password', record[3]))
                self.assertEqual(restored.get_friends()[0][2], 'Saved contact')
                self.assertEqual(restored.restore_snapshot(load_backup(path, 'backup password')), 0)
                self.assertEqual(len(restored.get_friends()), 1)
            finally:
                restored.con.close()

    def test_wrong_password_and_tampering_rejected(self):
        content = encrypt_snapshot(self.db.snapshot(), 'backup password')
        for changed, password in [(content, 'wrong password'), (content[:-20], 'backup password'),
                                  (content[:40] + bytes([content[40] ^ 1]) + content[41:], 'backup password'),
                                  (b'unknown', 'backup password')]:
            with self.subTest(password=password, length=len(changed)), self.assertRaises(ValueError):
                decrypt_snapshot(changed, password)

    def test_merge_never_replaces_existing_wallet(self):
        before = self.db.get_wallet_by_address(self.address)
        other = DB(':memory:')
        try:
            token, salt = encrypt_pk('00' * 31 + '01', 'different password')
            other.insert_wallet(self.address, token, salt)
            self.assertEqual(self.db.restore_snapshot(other.snapshot()), 0)
            self.assertEqual(self.db.get_wallet_by_address(self.address), before)
        finally:
            other.con.close()

    def test_corrupt_wallet_record_rejected_before_any_writes(self):
        target = DB(':memory:')
        try:
            self.db.con.execute("INSERT INTO wallets(address, pk, salt) VALUES ('bad', 'bad', 'bad')")
            self.db.con.commit()
            with self.assertRaises(ValueError):
                target.restore_snapshot(self.db.snapshot())
            self.assertEqual(target.get_wallets(), [])
            self.assertEqual(target.get_friends(), [])
        finally:
            target.con.close()

    def test_failed_restore_rolls_back_entire_merge(self):
        second = wallet_from_pkhex('00' * 31 + '02')[0]
        record = self.db.get_wallet_by_address(self.address)
        self.db.insert_wallet(second, record[2], record[3])
        target = DB(':memory:')
        try:
            target.con.execute("CREATE TRIGGER fail_second BEFORE INSERT ON wallets WHEN NEW.address != "
                               f"'{self.address}' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
            with self.assertRaises(sqlite3.Error):
                target.restore_snapshot(self.db.snapshot())
            self.assertEqual(target.get_wallets(), [])
        finally:
            target.con.close()

    def test_stable_path_and_copy_only_legacy_migration_includes_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            legacy, stable = base / 'old.db', base / 'profile' / 'wartwallet.db'
            source = DB(legacy)
            try:
                source.con.execute('PRAGMA journal_mode=WAL')
                record = self.db.get_wallet_by_address(self.address)
                source.insert_wallet(self.address, record[2], record[3])
                self.assertEqual(prepare_wallet_path(candidates=[legacy], destination=stable), str(stable))
                self.assertTrue(legacy.exists())
                migrated = DB(stable)
                try:
                    self.assertEqual(migrated.get_wallets()[0][1], self.address)
                finally:
                    migrated.con.close()
                self.assertEqual(prepare_wallet_path(candidates=[], destination=stable), str(stable))
            finally:
                source.con.close()

    def test_multiple_legacy_files_require_choice_and_explicit_path_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ('one.db', 'two.db')]
            for path in paths:
                path.touch()
            with self.assertRaises(MigrationChoiceRequired):
                prepare_wallet_path(candidates=paths, destination=Path(directory) / 'profile.db')
            self.assertEqual(prepare_wallet_path(':memory:', candidates=paths), ':memory:')
            self.assertFalse((Path(directory) / 'profile.db').exists())

    def test_default_directory_comes_from_application_profile(self):
        with patch('app_paths.QStandardPaths.writableLocation', return_value='C:/Users/example/AppData/Local/WarthogNetwork/WartWallet'):
            self.assertEqual(default_wallet_path().name, 'wartwallet.db')
            self.assertIn('WartWallet', str(default_wallet_path()))
