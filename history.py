"""Mainnet/DeFi native-coin history and local payment reconciliation.

Contracts: core/master/src/node/api/http/json.cpp and node/db/chain_db.cpp.
The initial cursor is signed INT64_MAX, matching the node's SQLite query.
"""
import re
from hashlib import sha256
from datetime import datetime, timezone

from node_client import NodeError
from utils import validate_address, parse_e8

LATEST_CURSOR = (1 << 63) - 1
ACTIVE_STATUSES = ('broadcasting', 'pending', 'uncertain', 'not_found')


def integer(value, name, maximum=0xffffffff):
    if type(value) is not int or not 0 <= value <= maximum:
        raise NodeError(f'Invalid {name} in transaction history.')
    return value


def defi_transaction(tx, kind):
    """Normalize native WART transfers/rewards from the DeFi API."""
    if kind not in ('wartTransfer', 'reward') or not isinstance(tx, dict) or not isinstance(tx.get('data'), dict):
        raise NodeError('Unsupported DeFi WART transaction.')
    data = tx['data']
    result = dict(txHash=tx.get('hash'), toAddress=data.get('toAddress'))
    amount = data.get('amount')
    if not isinstance(amount, dict):
        raise NodeError('Invalid DeFi WART amount.')
    from utils import format_e8, MAX_UINT64
    result['amount'] = format_e8(integer(amount.get('E8'), 'WART amount', MAX_UINT64))
    if kind == 'wartTransfer':
        common = tx.get('signedCommon')
        if not isinstance(common, dict) or not isinstance(common.get('fee'), dict):
            raise NodeError('Invalid DeFi transfer signature details.')
        sender = common.get('originAddress')
        # The DeFi API returns the 20-byte origin without its address checksum.
        if isinstance(sender, str) and re.fullmatch(r'[a-fA-F0-9]{40}', sender):
            raw = bytes.fromhex(sender)
            sender = (raw + sha256(raw).digest()[:4]).hex()
        result['fromAddress'] = sender
        result['fee'] = format_e8(integer(common['fee'].get('E8'), 'WART fee', MAX_UINT64))
    return result


def record_from_transaction(tx, owner, height, confirmations, timestamp, peer, network='mainnet'):
    if not isinstance(tx, dict):
        raise NodeError('Invalid transaction in account history.')
    tx_hash = tx.get('txHash')
    if not isinstance(tx_hash, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', tx_hash):
        raise NodeError('Invalid transaction ID in account history.')
    try:
        recipient = validate_address(tx.get('toAddress'))
        sender = validate_address(tx['fromAddress']) if 'fromAddress' in tx else ''
        amount = parse_e8(tx.get('amount'))
        fee = parse_e8(tx.get('fee', '0'), allow_zero=True)
    except ValueError as exc:
        raise NodeError('Invalid payment amounts or addresses in account history.') from exc
    if owner not in (sender, recipient):
        raise NodeError('Node returned a transaction belonging to another wallet.')
    direction = 'outgoing' if sender == owner else ('incoming' if sender else 'mined')
    return dict(wallet_address=owner, tx_hash=tx_hash.lower(), direction=direction,
                counterparty=recipient if direction == 'outgoing' else sender,
                amount_e8=str(amount), fee_e8=str(fee), timestamp=timestamp,
                status='confirmed' if confirmations else 'pending', confirmations=confirmations,
                block_height=height, peer=peer, payload='', network=network)


def block_timestamp(client, height, cache, headers=None):
    if not height:
        return 0
    if height not in cache:
        header = client.request(f'/chain/block/{height}/header').get('header')
        if not isinstance(header, dict):
            raise NodeError('Invalid block header for payment date.')
        timestamp = header.get('time', {}).get('timestamp') if client.expected_network == 'testnet' else header.get('timestamp')
        cache[height] = integer(timestamp, 'block timestamp')
        if headers is not None:
            headers[height] = header
    return cache[height]


def fetch_account_page(client, owner, before=LATEST_CURSOR, timestamps=None, headers=None):
    owner = validate_address(owner)
    integer(before, 'history cursor', LATEST_CURSOR)
    try:
        data = client.request(f'/account/{owner}/history/{before}')
    except NodeError as exc:
        if exc.code is not None and abs(exc.code) == 57:
            return [], 0
        raise
    blocks = data.get('perBlock')
    cursor = integer(data.get('fromId'), 'history cursor', LATEST_CURSOR)
    if not isinstance(blocks, list) or len(blocks) > 100:
        raise NodeError('Unsupported account history format.')
    if cursor and cursor >= before:
        raise NodeError('Node returned a non-advancing history cursor.')
    cache, rows = timestamps if timestamps is not None else {}, []
    for block in blocks:
        if not isinstance(block, dict):
            raise NodeError('Invalid block in account history.')
        height = integer(block.get('height'), 'block height')
        confirmations = integer(block.get('confirmations'), 'confirmation count')
        defi = client.expected_network == 'testnet'
        transactions = block.get('body') if defi else block.get('transactions')
        if not isinstance(transactions, dict):
            raise NodeError('Unsupported account transactions.')
        if defi and isinstance(block.get('header'), dict):
            header = block['header']
            cache[height] = integer(header.get('time', {}).get('timestamp'), 'block timestamp')
            if headers is not None:
                headers[height] = header
        timestamp = block_timestamp(client, height, cache, headers)
        if defi:
            reward = transactions.get('reward')
            transfers = transactions.get('wartTransfer')
            if not isinstance(transfers, list) or (reward is not None and not isinstance(reward, dict)):
                raise NodeError('Invalid DeFi WART history.')
            transactions = {'rewards': [defi_transaction(reward.get('transaction'), 'reward')] if reward else [],
                            'transfers': [defi_transaction(entry.get('transaction'), 'wartTransfer')
                                          if isinstance(entry, dict) else None for entry in transfers]}
        for kind in ('rewards', 'transfers'):
            entries = transactions.get(kind)
            if not isinstance(entries, list):
                raise NodeError('Unsupported account transactions.')
            for tx in entries:
                rows.append(record_from_transaction(tx, owner, height, confirmations, timestamp, client.peer,
                                                     'testnet' if defi else 'mainnet'))
                if len(rows) > 1000:
                    raise NodeError('Too many payments in account history.')
    return rows, cursor if blocks else 0


def reconcile_local(client, records, timestamps=None, headers=None):
    cache, rows = timestamps if timestamps is not None else {}, []
    for record in records:
        client.check_cancelled()
        transaction = client.lookup(record['tx_hash'])
        if transaction is None:
            changed = dict(record, status='not_found', confirmations=0, block_height=0)
        else:
            confirmations = integer(transaction.get('confirmations'), 'confirmation count')
            height = integer(transaction.get('blockHeight'), 'block height')
            timestamp = block_timestamp(client, height, cache, headers) or record['timestamp']
            changed = record_from_transaction(transaction, record['wallet_address'], height,
                                              confirmations, timestamp, client.peer,
                                              'testnet' if client.expected_network == 'testnet' else 'mainnet')
            if (changed['amount_e8'], changed['fee_e8'], changed['counterparty']) != (
                    record['amount_e8'], record['fee_e8'], record['counterparty']):
                raise NodeError('Node lookup does not match the saved payment.')
        rows.append(changed)
    return rows


def display_date(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if timestamp else 'Pending'


def validate_saved_record(row):
    row = dict(row, network=row.get('network', 'mainnet'))
    if row['network'] not in ('mainnet', 'testnet'):
        raise ValueError('Invalid saved payment network.')
    validate_address(row['wallet_address'])
    if not re.fullmatch(r'[a-f0-9]{64}', row['tx_hash']):
        raise ValueError('Invalid saved transaction ID.')
    if row['direction'] not in ('incoming', 'outgoing', 'mined'):
        raise ValueError('Invalid saved transaction direction.')
    if row['counterparty']:
        validate_address(row['counterparty'])
    for field in ('amount_e8', 'fee_e8'):
        if (not isinstance(row[field], str) or not re.fullmatch(r'[0-9]{1,20}', row[field])
                or int(row[field]) > (1 << 64) - 1):
            raise ValueError('Invalid saved transaction amount.')
    for field in ('timestamp', 'confirmations', 'block_height'):
        if type(row[field]) is not int or not 0 <= row[field] <= 0xffffffff:
            raise ValueError('Invalid saved transaction metadata.')
    if row['status'] not in (*ACTIVE_STATUSES, 'confirmed', 'rejected'):
        raise ValueError('Invalid saved transaction status.')
    for field, maximum in [('peer', 2048), ('payload', 16384)]:
        if not isinstance(row[field], str) or len(row[field]) > maximum:
            raise ValueError('Invalid saved transaction details.')
    return row
