"""Stable wallet storage, with a copy-only migration of a legacy database."""
from pathlib import Path
from contextlib import closing
import sqlite3
import sys
import tempfile
import os

from PySide6.QtCore import QStandardPaths


class MigrationChoiceRequired(Exception):
    def __init__(self, paths):
        self.paths = paths


def default_wallet_path():
    folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    if not folder:
        raise OSError('Windows could not provide a wallet data folder.')
    return Path(folder) / 'wartwallet.db'


def legacy_wallets(file_name='wartwallet.db'):
    folders = [Path.cwd(), Path(sys.executable).parent if getattr(sys, 'frozen', False)
               else Path(__file__).resolve().parent]
    return list(dict.fromkeys((folder / file_name).resolve() for folder in folders
                             if (folder / file_name).is_file()))


def read_database_snapshot(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as source:
        maximum_pages = (32 * 1024 * 1024) // source.execute('PRAGMA page_size').fetchone()[0]
        def progress(status, remaining, total):
            if total > maximum_pages:
                raise ValueError('Wallet database is too large to import.')
        with closing(sqlite3.connect(':memory:')) as copied:
            source.backup(copied, pages=256, progress=progress)
            return copied.serialize()


def copy_database(source, destination):
    """SQLite's backup API includes committed WAL contents; never move the source."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError('The destination wallet already exists.')
    handle, temporary = tempfile.mkstemp(prefix='.wallet-migration-', dir=destination.parent)
    os.close(handle)
    try:
        with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(temporary)) as copied:
                original.backup(copied)
                if copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('The legacy wallet database is damaged.')
                tables = {row[0] for row in copied.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'wallets', 'friends', 'data'} <= tables:
                    raise ValueError('This is not a Warthog wallet database.')
        # Hard link publishes without overwriting a concurrently created database.
        os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_wallet_path(explicit=None, candidates=None, destination=None, legacy_name='wartwallet.db'):
    if explicit is not None:
        return str(explicit)
    destination = Path(destination) if destination else default_wallet_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        sources = legacy_wallets(legacy_name) if candidates is None else list(candidates)
        sources = list(dict.fromkeys(Path(path).resolve() for path in sources
                                   if Path(path).is_file() and Path(path).resolve() != destination.resolve()))
        if len(sources) > 1:
            raise MigrationChoiceRequired(sources)
        if sources:
            copy_database(sources[0], destination)
    return str(destination)
