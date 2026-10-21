"""Address-scoped mining rewards from either network's paginated account API."""
import re

from history import fetch_account_page, reconcile_local, LATEST_CURSOR
from node_client import NodeError


def fetch_mined_page(client, owner, before=LATEST_CURSOR, known=()):
    if client.expected_network == 'testnet':
        client.require_network()
    else:
        client.require_mainnet()
    timestamps, headers = {}, {}
    rows, cursor = fetch_account_page(client, owner, before, timestamps, headers)
    # Recheck recent saved rewards so orphaned rewards leave the block list.
    checking = [row for row in known if row['wallet_address'] == owner
                and row['direction'] == 'mined'][:100]
    updates = reconcile_local(client, checking, timestamps, headers)
    records = {row['tx_hash']: row for row in updates + rows}
    details = {}
    for record in records.values():
        if record['direction'] != 'mined' or not record['block_height'] or not record['confirmations']:
            continue
        header = headers[record['block_height']]
        block_hash = header.get('hash')
        if not isinstance(block_hash, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', block_hash):
            raise NodeError('Invalid block hash in mining reward header.')
        details[record['tx_hash']] = dict(height=record['block_height'],
                                        timestamp=record['timestamp'], block_hash=block_hash.lower())
    return list(records.values()), cursor, details


def mined_blocks(records, owner, details=None):
    """Group rewards into blocks; totals describe the loaded history only."""
    blocks = {}
    for row in records:
        if (row['wallet_address'] != owner or row['direction'] != 'mined'
                or row['status'] != 'confirmed' or not row['block_height'] or not row['confirmations']):
            continue
        height = row['block_height']
        block = blocks.setdefault(height, dict(height=height, timestamp=row['timestamp'], reward_e8=0,
                                               confirmations=row['confirmations'], block_hash='', reward_ids=[]))
        block['reward_e8'] += int(row['amount_e8'])
        block['confirmations'] = min(block['confirmations'], row['confirmations'])
        block['reward_ids'].append(row['tx_hash'])
        detail = (details or {}).get(row['tx_hash'])
        if detail and (detail['height'], detail['timestamp']) == (height, row['timestamp']):
            block['block_hash'] = detail['block_hash']
    return sorted(blocks.values(), key=lambda block: block['height'], reverse=True)
