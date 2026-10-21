import sqlite3
import base64
import json
import time
from contextlib import closing

from utils import validate_address
from node_client import NodeError
from bundled_nodes import BUNDLED_NODES


class DB:
    def __init__(self, path="wartwallet.db"):
        self.con = sqlite3.connect(path)
        self.network = 'mainnet'
        with self.con:
            self.con.execute("""CREATE TABLE IF NOT EXISTS wallets (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            address TEXT,
                            pk TEXT,
                            salt TEXT,
                            last_balance TEXT NOT NULL DEFAULT '0'
                        );""")
            self.con.execute("""CREATE TABLE IF NOT EXISTS friends (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            address TEXT,
                            friend TEXT
                        );""")
            self.con.execute("""CREATE TABLE IF NOT EXISTS data (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            key TEXT,
                            value TEXT
                        );""")
            r = self.con.execute("SELECT COUNT() FROM data;")
            c = r.fetchone()[0]
            if c == 0:
                self.con.execute("INSERT INTO data (key, value) VALUES ('peer', 'http://localhost:3000');")
            self.con.execute('''CREATE TABLE IF NOT EXISTS tx_history (
                wallet_address TEXT NOT NULL, tx_hash TEXT NOT NULL, direction TEXT NOT NULL,
                counterparty TEXT NOT NULL, amount_e8 TEXT NOT NULL, fee_e8 TEXT NOT NULL,
                timestamp INTEGER NOT NULL, status TEXT NOT NULL, confirmations INTEGER NOT NULL DEFAULT 0,
                block_height INTEGER NOT NULL DEFAULT 0, peer TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '',
                network TEXT NOT NULL DEFAULT 'mainnet',
                PRIMARY KEY(network, wallet_address, tx_hash))''')
            if 'network' not in {row[1] for row in self.con.execute('PRAGMA table_info(tx_history)')}:
                if not self.con.in_transaction:
                    self.con.execute('BEGIN IMMEDIATE')
                self.con.execute('ALTER TABLE tx_history RENAME TO tx_history_legacy')
                self.con.execute('''CREATE TABLE tx_history (
                    wallet_address TEXT NOT NULL, tx_hash TEXT NOT NULL, direction TEXT NOT NULL,
                    counterparty TEXT NOT NULL, amount_e8 TEXT NOT NULL, fee_e8 TEXT NOT NULL,
                    timestamp INTEGER NOT NULL, status TEXT NOT NULL, confirmations INTEGER NOT NULL DEFAULT 0,
                    block_height INTEGER NOT NULL DEFAULT 0, peer TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '',
                    network TEXT NOT NULL DEFAULT 'mainnet', PRIMARY KEY(network, wallet_address, tx_hash))''')
                self.con.execute("INSERT INTO tx_history SELECT *, 'mainnet' FROM tx_history_legacy")
                self.con.execute('DROP TABLE tx_history_legacy')
            self.con.execute('''CREATE TABLE IF NOT EXISTS node_profiles (
                name TEXT PRIMARY KEY, url TEXT NOT NULL, network TEXT NOT NULL DEFAULT 'mainnet')''')
            profile_columns = {row[1] for row in self.con.execute('PRAGMA table_info(node_profiles)')}
            for column, declaration in [('enabled', 'INTEGER NOT NULL DEFAULT 1'), ('priority', 'INTEGER NOT NULL DEFAULT 0')]:
                if column not in profile_columns:
                    self.con.execute(f'ALTER TABLE node_profiles ADD COLUMN {column} {declaration}')
            if not self.setting('bundled_nodes_v1'):
                if not self.con.execute('SELECT 1 FROM node_profiles').fetchone():
                    self.con.execute("INSERT INTO node_profiles(name, url, network) VALUES ('Local node', ?, 'mainnet')", (self.get_peer(),))
                existing = {(url.strip().rstrip('/'), network) for _, url, network in self.node_profiles()}
                for name, url, network in BUNDLED_NODES:
                    if (url, network) in existing:
                        continue
                    # A user may already have given a different node this name.
                    base, suffix = name, 2
                    while self.con.execute('SELECT 1 FROM node_profiles WHERE name=?', (name,)).fetchone():
                        name = f'{base} ({suffix})'
                        suffix += 1
                    priority = self.con.execute('SELECT COALESCE(MAX(priority), -1)+1 FROM node_profiles WHERE network=?', (network,)).fetchone()[0]
                    self.con.execute('INSERT INTO node_profiles(name, url, network, priority) VALUES (?, ?, ?, ?)',
                                     (name, url, network, priority))
                    existing.add((url, network))
                self.con.execute("INSERT INTO data(key, value) VALUES ('bundled_nodes_v1', '1')")

    def get_wallets(self):
        with self.con:
            r = self.con.execute("SELECT * FROM wallets;")
            data = r.fetchall()
            return data

    def get_name(self, friend):
        with self.con:
            r = self.con.execute("SELECT * FROM friends WHERE friend=?", (friend,))
            return r.fetchone()
    def get_friends(self):
        with self.con:
            r = self.con.execute("SELECT * FROM friends;")
            data = r.fetchall()
            return data

    def insert_friend(self,addr, friend):
        with self.con:
            self.con.execute("INSERT INTO friends (address,friend) VALUES (?, ?)", (addr, friend))

    def delete_friend(self, contact_id):
        with self.con:
            self.con.execute('DELETE FROM friends WHERE id=?', (contact_id,))

    def get_wallet_by_address(self, addr):
        with self.con:
            r = self.con.execute("SELECT * FROM wallets WHERE address=?", (addr,))
            data = r.fetchone()
            return data

    def insert_wallet(self, addr, pk, salt):
        with self.con:
            self.con.execute("INSERT INTO wallets (address, pk, salt) VALUES (?, ?, ?)", (addr, pk, salt))

    def update_balance(self, addr, balance):
        with self.con:
            self.con.execute("UPDATE wallets SET last_balance=? WHERE address=?", (balance, addr))

    def delete_wallet(self, addr):
        with self.con:
            self.con.execute("DELETE FROM wallets WHERE address=?", (addr,))

    def update_peer(self, peer, network='mainnet'):
        self.set_setting('peer' if network == 'mainnet' else 'peer:testnet', peer)

    def get_peer(self, network='mainnet'):
        if network == 'testnet':
            return self.setting('peer:testnet', 'https://testnet.wartnode.com')
        return self.setting('peer', 'http://localhost:3000')

    def snapshot(self):
        with closing(sqlite3.connect(':memory:')) as snapshot:
            self.con.backup(snapshot)
            return snapshot.serialize()

    def node_profiles(self):
        return self.con.execute('SELECT name, url, network FROM node_profiles ORDER BY priority, name').fetchall()

    def configured_nodes(self, network):
        cursor = self.con.execute('SELECT name, url, network, enabled, priority FROM node_profiles '
                                  'WHERE network=? ORDER BY priority, name', (network,))
        return [dict(zip(('name', 'url', 'network', 'enabled', 'priority'), row)) for row in cursor]

    def node_candidates(self, network, preferred=None):
        peers = [row['url'] for row in self.configured_nodes(network) if row['enabled']]
        return list(dict.fromkeys(([preferred] if preferred else []) + peers))

    def save_node_profile(self, name, url, network='mainnet', enabled=None, previous_name=None):
        from node_client import NodeClient
        name = name.strip()
        if not 1 <= len(name) <= 64:
            raise ValueError('Use a node profile name of 1 to 64 characters.')
        url = NodeClient.validate_peer(url)
        if network not in ('mainnet', 'testnet'):
            raise ValueError('Invalid node profile network.')
        if enabled is not None and type(enabled) is not bool:
            raise ValueError('Invalid node enabled flag.')
        with self.con:
            if previous_name and previous_name != name:
                if self.con.execute('SELECT 1 FROM node_profiles WHERE name=?', (name,)).fetchone():
                    raise ValueError('A node with that name already exists.')
                self.con.execute('UPDATE node_profiles SET name=? WHERE name=?', (name, previous_name))
            if not self.con.execute('SELECT 1 FROM node_profiles WHERE name=?', (name,)).fetchone():
                if self.con.execute('SELECT COUNT(*) FROM node_profiles').fetchone()[0] >= 100:
                    raise ValueError('Save at most 100 node profiles.')
                priority = self.con.execute('SELECT COALESCE(MAX(priority), -1)+1 FROM node_profiles WHERE network=?', (network,)).fetchone()[0]
                self.con.execute('INSERT INTO node_profiles(name, url, network, enabled, priority) VALUES (?, ?, ?, ?, ?)',
                                 (name, url, network, int(enabled is not False), priority))
            else:
                self.con.execute('UPDATE node_profiles SET url=?, network=?, enabled=COALESCE(?, enabled) WHERE name=?',
                                 (url, network, None if enabled is None else int(enabled), name))

    def move_node_profile(self, name, offset):
        row = self.con.execute('SELECT network FROM node_profiles WHERE name=?', (name,)).fetchone()
        if not row or offset not in (-1, 1):
            return
        nodes = self.configured_nodes(row[0])
        index = next(i for i, node in enumerate(nodes) if node['name'] == name)
        target = index + offset
        if not 0 <= target < len(nodes):
            return
        nodes[index], nodes[target] = nodes[target], nodes[index]
        with self.con:
            self.con.executemany('UPDATE node_profiles SET priority=? WHERE name=?',
                                 [(i, node['name']) for i, node in enumerate(nodes)])

    def delete_node_profile(self, name):
        with self.con:
            self.con.execute('DELETE FROM node_profiles WHERE name=?', (name,))

    def setting(self, key, default=None):
        row = self.con.execute('SELECT value FROM data WHERE key=? ORDER BY id DESC LIMIT 1', (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self.con:
            self.con.execute('DELETE FROM data WHERE key=?', (key,))
            self.con.execute('INSERT INTO data(key, value) VALUES (?, ?)', (key, str(value)))

    def reserved_amount(self, owner, network=None):
        rows = self.con.execute("SELECT amount_e8, fee_e8 FROM tx_history WHERE wallet_address=? "
            "AND network=? AND direction='outgoing' AND status IN ('broadcasting', 'pending', 'uncertain', 'not_found')",
            (owner, network or self.network))
        return sum(int(amount) + int(fee) for amount, fee in rows)

    def record_outgoing(self, owner, intent, prepared, peer, balance=None, network=None):
        network = network or self.network
        row = dict(wallet_address=owner, tx_hash=prepared.tx_hash, direction='outgoing',
                   counterparty=intent.recipient, amount_e8=str(intent.amount), fee_e8=str(intent.fee),
                   timestamp=int(time.time()), status='broadcasting', confirmations=0,
                   block_height=0, peer=peer, payload=json.dumps(prepared.payload), network=network)
        # Serialize reservations across processes before any POST can occur.
        with self.con:
            self.con.execute('BEGIN IMMEDIATE')
            if balance is not None and intent.amount + intent.fee > max(0, balance - self.reserved_amount(owner, network)):
                raise ValueError('Insufficient available balance after pending payments and fees. Nothing was broadcast.')
            self._write_history(row)

    def save_history(self, rows):
        with self.con:
            for row in rows:
                self._write_history(row)

    def _write_history(self, row):
        row = dict(row, network=row.get('network', self.network))
        if row['network'] not in ('mainnet', 'testnet'):
            raise ValueError('Invalid payment network.')
        saved = self.con.execute('SELECT amount_e8, fee_e8, counterparty, direction FROM tx_history '
                                 "WHERE wallet_address=? AND tx_hash=? AND network=? AND payload != ''",
                                 (row['wallet_address'], row['tx_hash'], row['network'])).fetchone()
        if saved and tuple(row[key] for key in ('amount_e8', 'fee_e8', 'counterparty', 'direction')) != saved:
            raise ValueError('Node history does not match the locally approved payment.')
        columns = ('wallet_address', 'tx_hash', 'direction', 'counterparty', 'amount_e8', 'fee_e8',
                   'timestamp', 'status', 'confirmations', 'block_height', 'peer', 'payload', 'network')
        self.con.execute('''INSERT INTO tx_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, wallet_address, tx_hash) DO UPDATE SET
                    direction=excluded.direction, counterparty=excluded.counterparty,
                    amount_e8=excluded.amount_e8, fee_e8=excluded.fee_e8,
                    timestamp=CASE WHEN excluded.timestamp > 0 THEN excluded.timestamp ELSE tx_history.timestamp END,
                    status=excluded.status, confirmations=excluded.confirmations,
                    block_height=excluded.block_height, peer=excluded.peer,
                    payload=CASE WHEN excluded.payload != '' THEN excluded.payload ELSE tx_history.payload END''',
                    tuple(row[key] for key in columns))

    def set_tx_status(self, owner, tx_hash, status, network=None):
        with self.con:
            self.con.execute('UPDATE tx_history SET status=? WHERE wallet_address=? AND tx_hash=? AND network=?',
                             (status, owner, tx_hash, network or self.network))

    def history(self, owner, network=None):
        cursor = self.con.execute('SELECT * FROM tx_history WHERE wallet_address=? AND network=? ORDER BY timestamp DESC, tx_hash',
                                  (owner, network or self.network))
        columns = [entry[0] for entry in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def restore_snapshot(self, content):
        """Validate everything before merging; never install an imported schema."""
        with closing(sqlite3.connect(':memory:')) as source:
            try:
                source.deserialize(content)
                source.execute('PRAGMA trusted_schema=OFF')
                source.execute('PRAGMA query_only=ON')
                if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Wallet backup is damaged.')
                kinds = dict(source.execute("SELECT name, type FROM sqlite_master"))
                if any(kinds.get(name) != 'table' for name in ('wallets', 'friends', 'data')):
                    raise ValueError('Backup is not a supported wallet database.')
                wallets = source.execute('SELECT address, pk, salt FROM wallets LIMIT 10001').fetchall()
                contacts = source.execute('SELECT address, friend FROM friends LIMIT 10001').fetchall()
                if max(len(wallets), len(contacts)) > 10000:
                    raise ValueError('Backup contains too many records.')
                for address, token, salt in wallets:
                    validate_address(address)
                    if (not isinstance(token, str) or not 80 <= len(token) <= 2048
                            or not isinstance(salt, str) or len(base64.b64decode(salt, validate=True)) != 16
                            or not base64.urlsafe_b64decode(token).startswith(b'\x80')):
                        raise ValueError('Invalid encrypted wallet record.')
                for address, name in contacts:
                    if (not isinstance(address, str) or len(address) > 256
                            or not isinstance(name, str) or len(name) > 256):
                        raise ValueError('Invalid contact in backup.')
                transactions = []
                if kinds.get('tx_history') == 'table':
                    from history import validate_saved_record
                    source.row_factory = sqlite3.Row
                    transactions = [validate_saved_record(dict(row)) for row in
                                    source.execute('SELECT * FROM tx_history LIMIT 50001')]
                    if len(transactions) > 50000:
                        raise ValueError('Backup contains too many payments.')
                profiles = []
                if kinds.get('node_profiles') == 'table':
                    from node_client import NodeClient
                    columns = {row[1] for row in source.execute('PRAGMA table_info(node_profiles)')}
                    extra = (', enabled' if 'enabled' in columns else ', 1') + (', priority' if 'priority' in columns else ', 0')
                    profiles = source.execute('SELECT name, url, network' + extra + ' FROM node_profiles LIMIT 101').fetchall()
                    if len(profiles) > 100:
                        raise ValueError('Backup contains too many node profiles.')
                    for name, url, network, enabled, priority in profiles:
                        if (not isinstance(name, str) or not 1 <= len(name) <= 64 or network not in ('mainnet', 'testnet')
                                or not isinstance(url, str) or len(url) > 2048
                                or type(enabled) is not int or enabled not in (0, 1)
                                or type(priority) is not int or not 0 <= priority <= 0x7fffffff):
                            raise ValueError('Unsupported node profile in backup.')
                        NodeClient.validate_peer(url)
                preferences = source.execute("SELECT value FROM data WHERE key='wallet_preferences' ORDER BY id DESC LIMIT 1").fetchone()
                if preferences:
                    from wallet_settings import validate_preferences
                    if not isinstance(preferences[0], str) or len(preferences[0]) > 4096:
                        raise ValueError('Invalid wallet preferences in backup.')
                    preferences = json.dumps(validate_preferences(json.loads(preferences[0])))
            except (sqlite3.Error, TypeError, KeyError, UnicodeError, NodeError) as exc:
                raise ValueError('Could not read this wallet database.') from exc
        added = 0
        with self.con:
            for address, token, salt in wallets:
                if not self.con.execute('SELECT 1 FROM wallets WHERE address=?', (address,)).fetchone():
                    self.con.execute('INSERT INTO wallets(address, pk, salt) VALUES (?, ?, ?)',
                                     (address, token, salt))
                    added += 1
            for address, name in contacts:
                self.con.execute('INSERT INTO friends(address, friend) SELECT ?, ? '
                                 'WHERE NOT EXISTS(SELECT 1 FROM friends WHERE address=? AND friend=?)',
                                 (address, name, address, name))
            if transactions:
                columns = ('wallet_address', 'tx_hash', 'direction', 'counterparty', 'amount_e8', 'fee_e8',
                           'timestamp', 'status', 'confirmations', 'block_height', 'peer', 'payload', 'network')
                for row in transactions:
                    self.con.execute('INSERT OR IGNORE INTO tx_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                     tuple(row[key] for key in columns))
            for name, url, network, enabled, priority in profiles:
                existing = self.con.execute('SELECT url, network FROM node_profiles WHERE name=?', (name,)).fetchone()
                if existing and tuple(existing) != (url, network):
                    base, counter = name[:48], 1
                    while self.con.execute('SELECT 1 FROM node_profiles WHERE name=?', (name,)).fetchone():
                        name = f'{base} (restored {counter})'
                        counter += 1
                self.con.execute('INSERT OR IGNORE INTO node_profiles(name, url, network, enabled, priority) VALUES (?, ?, ?, ?, ?)',
                                 (name, url, network, enabled, priority))
            if preferences and self.setting('wallet_preferences') is None:
                self.con.execute("INSERT INTO data(key, value) VALUES ('wallet_preferences', ?)", (preferences,))
        return added
