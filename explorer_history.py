"""Indexed display history with explicit source cursors and node fallback.

Explorer observations must be saved with DB.save_explorer_history: they never
settle a local payment or release a pending-payment reservation.
"""
from dataclasses import dataclass, field
import re
import time

from explorer_client import ExplorerClient, ExplorerError
from history import LATEST_CURSOR, fetch_account_page, integer, record_from_transaction, reconcile_local
from mined_blocks import fetch_mined_page
from node_client import NodeError
from utils import MAX_UINT64, format_e8, validate_address


@dataclass(frozen=True)
class ExplorerCursor:
    token: str
    epoch: str
    owner: str
    network: str
    rewards: bool


@dataclass(frozen=True)
class NodeCursor:
    before: int


@dataclass
class HistoryPage:
    rows: list
    cursor: object
    notice: str
    source: str = 'node'
    details: dict = field(default_factory=dict)


def native_amount(value):
    if (not isinstance(value, dict) or type(value.get('decimals')) is not int or value['decimals'] != 8
            or not isinstance(value.get('raw'), str) or not re.fullmatch(r'[0-9]{1,20}', value['raw'])
            or int(value['raw']) > MAX_UINT64):
        raise ExplorerError('Invalid explorer WART amount.')
    return format_e8(int(value['raw']))


def explorer_record(tx, owner, meta, rewards=False):
    if not isinstance(tx, dict) or not isinstance(tx.get('type'), str):
        raise ExplorerError('Invalid explorer transaction.')
    if tx['type'] not in ('reward', 'wartTransfer'):
        # Other DeFi actions are outside this wallet's native-payment history.
        return None
    if rewards and tx['type'] != 'reward':
        raise ExplorerError('Explorer returned a transfer in mining history.')
    height = integer(tx.get('height'), 'explorer block height')
    timestamp = integer(tx.get('timestamp'), 'explorer block timestamp')
    tip = meta['coverage']['indexedTip']
    if not height or height > tip or not timestamp or tx.get('assetHash') is not None:
        raise ExplorerError('Invalid explorer native-payment block.')
    payment = dict(txHash=tx.get('hash'), toAddress=tx.get('recipient'), amount=native_amount(tx.get('amount')))
    if tx['type'] == 'wartTransfer':
        payment.update(fromAddress=tx.get('sender'), fee=native_amount(tx.get('fee')))
    elif tx.get('sender') is not None:
        raise ExplorerError('Invalid explorer reward sender.')
    return record_from_transaction(payment, owner, height, tip - height + 1, timestamp,
                                   'https://wartnode.com', meta['network'])


def fetch_wallet_page(node, owner, cursor=LATEST_CURSOR, known=(), rewards=False):
    owner = validate_address(owner)
    if isinstance(cursor, NodeCursor):
        return node_page(node, owner, cursor.before, known, rewards)
    if isinstance(cursor, ExplorerCursor):
        if (cursor.owner, cursor.network, cursor.rewards) != (owner, node.expected_network, rewards):
            raise ExplorerError('History cursor belongs to another wallet, network, or view.')
    elif cursor != LATEST_CURSOR:
        return node_page(node, owner, cursor, known, rewards)
    try:
        # Reserve time for a node fallback within the caller's overall deadline.
        explorer = ExplorerClient(node.expected_network, cancel=node.cancel,
                                  deadline=min(node.deadline, time.monotonic() + 6))
        params = dict(address=owner, limit=100)
        if rewards:
            params['type'] = 'reward'
        if isinstance(cursor, ExplorerCursor):
            params['cursor'] = cursor.token
        data, meta = explorer.request('/transactions', params,
                                      epoch=cursor.epoch if isinstance(cursor, ExplorerCursor) else None)
        items, token = data.get('items'), data.get('nextCursor')
        if (not isinstance(items, list) or len(items) > 100 or 'nextCursor' not in data
                or (token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 512))
                or (isinstance(cursor, ExplorerCursor) and token == cursor.token)):
            raise ExplorerError('Invalid explorer history page or cursor.')
        rows = []
        for tx in items:
            row = explorer_record(tx, owner, meta, rewards)
            if row is not None:
                rows.append(row)
        if len({row['tx_hash'] for row in rows}) != len(rows):
            raise ExplorerError('Duplicate explorer payments.')
        next_cursor = ExplorerCursor(token, meta['epoch'], owner, node.expected_network, rewards) if token else 0
        complete = meta['coverage']['complete']
        if not next_cursor and not complete:
            next_cursor = NodeCursor(LATEST_CURSOR)
        # A filtered page covers this height interval. Verify missing saved
        # rewards on the node instead of interpreting absence as a reorg.
        if rewards and cursor == LATEST_CURSOR:
            hashes = {row['tx_hash'] for row in rows}
            floor = min((row['block_height'] for row in rows), default=0)
            checking = [row for row in known if row['direction'] == 'mined'
                        and row['wallet_address'] == owner and row['network'] == node.expected_network
                        and row['tx_hash'] not in hashes
                        and (row['block_height'] >= floor if rows else complete)][:100]
            if checking:
                node.require_network()
                rows.extend(reconcile_local(node, checking))
        notice = 'Updated from WartNode Explorer. Confirmations use its indexed height. '
        notice += ('Index coverage is complete. ' if complete else 'Index coverage is incomplete; older payments may be missing. ')
        if isinstance(next_cursor, NodeCursor):
            notice += 'Load older to continue through your node. '
        if rewards:
            notice += 'Reward totals cover loaded history only.'
        else:
            notice += 'Saved outgoing payments are verified on your node.'
        return HistoryPage(rows, next_cursor, notice, source='explorer')
    except NodeError as exc:
        node.check_cancelled()
        if getattr(exc, 'code', None) == 'CHAIN_RESET':
            raise
        # Explorer cursors cannot be translated into account-history IDs.
        # Restart at the node's latest page, merge by hash, and label the restart.
        return node_page(node, owner, LATEST_CURSOR, known, rewards,
                         'Explorer unavailable; restarted at latest node history. ')


def node_page(node, owner, before, known, rewards, prefix=''):
    node.require_network()
    if rewards:
        rows, cursor, details = fetch_mined_page(node, owner, before, known)
    else:
        rows, cursor = fetch_account_page(node, owner, before)
        details = {}
    return HistoryPage(rows, NodeCursor(cursor) if cursor else 0,
                       prefix + 'Updated from your node. ' + (
                           'Reward totals cover loaded history; recent saved rewards were rechecked.' if rewards
                           else 'Uncertain/not-found payments are never retried automatically.'), details=details)
