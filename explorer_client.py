"""Read-only WartNode v1 client, sharing the node's bounded Qt transport.

Contract: https://wartnode.com/openapi.json. Monetary values stay exact integers.
The public request budget and Retry-After cooldown are shared by both networks.
"""
import json
import re
import time
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.parse import urlencode

from node_client import NodeClient, NodeError

EXPLORER_ORIGIN = 'https://wartnode.com'


class ExplorerError(NodeError):
    pass


class _RequestBudget:
    def __init__(self):
        self.lock = Lock()
        self.next_request = 0
        self.cooldown_until = 0

    def acquire(self, transport):
        while True:
            transport.check_cancelled()
            with self.lock:
                if time.monotonic() < self.cooldown_until:
                    raise ExplorerError('Explorer is cooling down; using your node.')
                remaining = self.next_request - time.monotonic()
                if remaining <= 0:
                    # Leave headroom below the API's shared 5 requests/second.
                    self.next_request = time.monotonic() + 0.25
                    return
            transport.cancel.wait(min(remaining, 0.05))

    def defer(self, seconds):
        with self.lock:
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + seconds)


_BUDGET = _RequestBudget()


class ExplorerClient:
    def __init__(self, network, cancel=None, deadline=None):
        self.network = network
        self.transport = NodeClient(EXPLORER_ORIGIN, cancel=cancel, deadline=deadline, network=network)

    def request(self, path, params=None, epoch=None):
        if not re.fullmatch(r'/transactions|/blocks/[1-9][0-9]{0,9}', path):
            raise ExplorerError('Unsupported explorer read.')
        _BUDGET.acquire(self.transport)
        query = '?' + urlencode(params) if params else ''
        try:
            status, body = self.transport._exchange(f'/api/v1/{self.network}{path}{query}', None)
        except NodeError as exc:
            if not self.transport.cancel.is_set():
                _BUDGET.defer(10)
            raise ExplorerError('Explorer connection unavailable.') from exc
        if status == 429:
            delay = 5
            retry = getattr(self.transport, 'retry_after', '')
            try:
                delay = max(delay, int(retry))
            except ValueError:
                try:
                    delay = max(delay, parsedate_to_datetime(retry).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
            _BUDGET.defer(delay)
            raise ExplorerError('Explorer rate limit reached; using your node during cooldown.')
        if status is not None and status >= 500:
            _BUDGET.defer(10)
        try:
            if len(body) > self.transport.MAX_RESPONSE_BYTES:
                raise ValueError()
            result = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise ExplorerError('Invalid explorer response.') from exc
        if not isinstance(result, dict):
            raise ExplorerError('Invalid explorer response.')
        if status != 200:
            error = result.get('error')
            code = error.get('code') if isinstance(error, dict) else None
            if code == 'CHAIN_RESET':
                raise ExplorerError('Explorer chain reset; restart history with Refresh.', code)
            raise ExplorerError(f'Explorer returned HTTP {status}.', code)
        meta, data = result.get('meta'), result.get('data')
        if (not isinstance(data, dict) or not isinstance(meta, dict)
                or meta.get('network') != self.network or meta.get('enabled') is not True
                or meta.get('stale') is not False or meta.get('source') != 'index'
                or not isinstance(meta.get('epoch'), str) or not 1 <= len(meta['epoch']) <= 512
                or not isinstance(meta.get('coverage'), dict)):
            raise ExplorerError('Explorer data is unavailable, stale, or on another network.')
        coverage = meta['coverage']
        if (coverage.get('indexerAlive') is not True or type(coverage.get('complete')) is not bool
                or any(type(coverage.get(key)) is not int or not 0 <= coverage[key] <= 0xffffffff
                       for key in ('tip', 'indexedTip'))
                or coverage['indexedTip'] < coverage['tip']):
            raise ExplorerError('Explorer index is catching up; using your node.')
        if epoch is not None and meta['epoch'] != epoch:
            raise ExplorerError('Explorer chain reset; restart history with Refresh.', 'CHAIN_RESET')
        self.transport.check_cancelled()
        return data, meta
