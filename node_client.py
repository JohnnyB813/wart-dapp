"""Bounded mainnet and DeFi-testnet API client. Never used on the GUI thread."""

import ipaddress
import json
import re
import time
from threading import Event
from urllib.parse import urlsplit

from PySide6.QtCore import QByteArray, QEventLoop, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkReply, QNetworkRequest


# Use explicit LAN ranges: is_private also includes reserved/documentation IPs.
LAN_HTTP_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7',
))


class NodeError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class BroadcastUncertain(NodeError):
    """The node may have accepted a transaction; do not automatically retry."""


class NodeClient:
    MAX_RESPONSE_BYTES = 1024 * 1024

    def __init__(self, peer, cancel=None, deadline=None, network='mainnet'):
        if network not in ('mainnet', 'testnet'):
            raise NodeError('Select mainnet or DeFi testnet.')
        self.expected_network = network
        self.peer = self.validate_peer(peer)
        self.cancel = cancel or Event()
        self.deadline = deadline or time.monotonic() + 15

    @staticmethod
    def validate_peer(peer):
        peer = peer.strip().rstrip('/')
        try:
            parts = urlsplit(peer)
            port = parts.port
        except ValueError as exc:
            raise NodeError('Invalid node URL.') from exc
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.path
                or (port is not None and not 1 <= port <= 65535)):
            raise NodeError('Enter a node base URL, such as http://127.0.0.1:3000.')
        local = parts.hostname.lower() == 'localhost'
        try:
            address = ipaddress.ip_address(parts.hostname)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            local = (local or address.is_loopback or address.is_link_local
                     or any(address in network for network in LAN_HTTP_NETWORKS))
        except ValueError:
            pass
        if parts.scheme == 'http' and not local:
            raise NodeError('Public nodes require HTTPS. HTTP is supported for localhost and private LAN IPs.')
        return peer

    def check_cancelled(self):
        if self.cancel.is_set():
            raise NodeError('Operation cancelled.')
        if time.monotonic() >= self.deadline:
            raise NodeError('Node operation timed out.')

    def _exchange(self, path, payload):
        """Qt timer aborts the entire exchange, including DNS/TLS/slow headers."""
        self.check_cancelled()
        manager = QNetworkAccessManager()
        manager.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        request = QNetworkRequest(QUrl(self.peer + path))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setAttribute(QNetworkRequest.Attribute.AuthenticationReuseAttribute,
                             QNetworkRequest.LoadControl.Manual)
        request.setAttribute(QNetworkRequest.Attribute.CookieLoadControlAttribute,
                             QNetworkRequest.LoadControl.Manual)
        request.setAttribute(QNetworkRequest.Attribute.CookieSaveControlAttribute,
                             QNetworkRequest.LoadControl.Manual)
        if payload is None:
            reply = manager.get(request)
        else:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, 'application/json')
            reply = manager.post(request, QByteArray(json.dumps(payload).encode()))
        reply.setReadBufferSize(65536)
        loop, timer, cancel_timer = QEventLoop(), QTimer(), QTimer()
        body, failures = bytearray(), []

        def abort(reason):
            failures.append(reason)
            reply.abort()

        def read():
            body.extend(bytes(reply.read(self.MAX_RESPONSE_BYTES + 1 - len(body))))
            if len(body) > self.MAX_RESPONSE_BYTES:
                abort('Node response exceeds the size limit.')

        reply.readyRead.connect(read)
        reply.finished.connect(loop.quit)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: abort('Node operation timed out.'))
        cancel_timer.timeout.connect(lambda: abort('Operation cancelled.') if self.cancel.is_set() else None)
        timer.start(max(1, int((self.deadline - time.monotonic()) * 1000)))
        cancel_timer.start(50)
        loop.exec()
        timer.stop()
        cancel_timer.stop()
        if reply.bytesAvailable() and not failures:
            read()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        if failures:
            raise NodeError(failures[0])
        if error != QNetworkReply.NetworkError.NoError and not status:
            raise NodeError('Could not read the node response. Check the connection and node URL.')
        return status, bytes(body)

    def request(self, path, payload=None):
        try:
            status, body = self._exchange(path, payload)
            if status != 200:
                raise NodeError(f'Node returned HTTP {status}. Check the node API URL.')
            if len(body) > self.MAX_RESPONSE_BYTES:
                raise NodeError('Node response exceeds the size limit.')
            result = json.loads(body)
        except ValueError as exc:
            if payload is not None:
                raise BroadcastUncertain('Broadcast response was lost. Check the transaction on the node before sending again.') from exc
            raise NodeError('Could not read the node response. Check the connection and node URL.') from exc
        except NodeError as exc:
            if payload is not None:
                raise BroadcastUncertain('Broadcast result is uncertain. Check the transaction on the node before sending again.') from exc
            raise
        if not isinstance(result, dict) or type(result.get('code')) is not int:
            if payload is not None:
                raise BroadcastUncertain('Malformed broadcast reply. Check the transaction before sending again.')
            raise NodeError('Unsupported node response. Check the selected network and node API URL.')
        if result['code'] != 0:
            raise NodeError('Node rejected the request: ' + str(result.get('error', result['code']))[:200], result['code'])
        # Some DeFi node versions return the broadcast ID at the top level.
        if payload is not None and self.expected_network == 'testnet' and 'txHash' in result:
            return result
        if not isinstance(result.get('data'), dict):
            if payload is not None:
                raise BroadcastUncertain('Malformed broadcast reply. Check the transaction before sending again.')
            raise NodeError('Unsupported node response data.')
        return result['data']

    def head(self, require_synced=False):
        data = self.request('/chain/head')
        if self.expected_network == 'testnet':
            chain = data.get('chainHead')
            if not isinstance(chain, dict):
                raise NodeError('This node does not provide the DeFi testnet API.')
            data = dict(chain, synced=data.get('synced'))
        height, pin = data.get('height'), data.get('pinHeight')
        pin_hash = data.get('pinHash')
        if (type(height) is not int or not 0 <= height <= 0xffffffff
                or type(pin) is not int or not 0 <= pin <= height or pin % 32
                or not isinstance(pin_hash, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', pin_hash)):
            raise NodeError('Node returned invalid chain pin data.')
        if require_synced and data.get('synced') is not True:
            raise NodeError('The node is not synced. Wait for synchronization before sending.')
        return data

    def balance(self, address):
        if self.expected_network == 'testnet':
            from utils import format_e8, MAX_UINT64
            wart = self.request(f'/account/{address}/wart_balance').get('wart')
            if not isinstance(wart, dict):
                raise NodeError('Invalid DeFi testnet balance.')
            values = {}
            for field in ('total', 'locked', 'mempool'):
                amount = wart.get(field)
                value = amount.get('E8') if isinstance(amount, dict) else None
                if type(value) is not int or not 0 <= value <= MAX_UINT64:
                    raise NodeError('Invalid DeFi testnet balance amount.')
                values[field] = value
            if values['locked'] + values['mempool'] > values['total']:
                raise NodeError('Invalid DeFi testnet locked or pending balance.')
            # Open-order funds cannot be spent. Local pending transfers are
            # reserved separately by the journal, without double subtraction.
            return format_e8(values['total'] - values['locked'])
        data = self.request(f'/account/{address}/balance')
        value = data.get('balance')
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,12}(?:\.[0-9]{1,8})?', value):
            raise NodeError('Unsupported balance format. Select a legacy mainnet node.')
        from utils import parse_e8
        try:
            parse_e8(value, allow_zero=True)
        except ValueError as exc:
            raise NodeError('Node returned an out-of-range balance.') from exc
        return value

    def probe(self):
        status = self.status()
        if status['network'] not in (self.expected_network, 'unverified'):
            raise NodeError(f"Node reports {status['network']}; select that network before connecting.")
        return status

    def network(self):
        mining = self.request('/chain/mine/0000000000000000000000000000000000000000de47c9b2')
        testnet = mining.get('testnet')
        if type(testnet) is not bool:
            raise NodeError('The node did not identify its network. Sending is disabled.')
        return 'testnet' if testnet else 'mainnet'

    def require_mainnet(self):
        if self.network() != 'mainnet':
            raise NodeError('Sending to a testnet node is disabled. Select a mainnet node.')

    def require_network(self):
        if self.expected_network == 'mainnet':
            self.require_mainnet()
        elif self.network() != self.expected_network:
            raise NodeError('This node is not on the selected DeFi testnet. Nothing was broadcast.')

    def minimum_fee(self):
        deadline = self.deadline
        self.deadline = min(deadline, time.monotonic() + 3)
        try:
            data = self.request('/transaction/minfee')
        finally:
            self.deadline = deadline
        # Mainnet returns {E8, amount, 16bit}; DeFi wraps this in minFee.
        minimum = data.get('minFee', data)
        value = minimum.get('E8') if isinstance(minimum, dict) else None
        from utils import round_fee_e8
        try:
            if round_fee_e8(value) != value:
                raise ValueError()
        except ValueError as exc:
            raise NodeError('The node returned an invalid minimum transaction fee.') from exc
        return value

    def status(self):
        head = self.head()
        try:
            network = self.network()
        except NodeError as exc:
            if head.get('synced') is True:
                raise
            network = 'unverified'
        if network == self.expected_network or network == 'unverified':
            self.balance('0000000000000000000000000000000000000000de47c9b2')
        version = 'Unknown'
        try:
            info = self.request('/tools/info') if self.expected_network == 'testnet' else self.request('/tools/version')
            version_info = info.get('version')
            reported = version_info.get('name') if self.expected_network == 'testnet' and isinstance(version_info, dict) else info.get('name')
            if isinstance(reported, str):
                version = reported[:80]
        except NodeError:
            pass
        state = dict(peer=self.peer, network=network, synced=head.get('synced') is True,
                     height=head['height'], version=version, checked_at=time.time())
        try:
            state['min_fee_e8'] = self.minimum_fee()
        except NodeError as exc:
            state['fee_error'] = str(exc)
        return state

    def block(self, height):
        data = self.request(f'/chain/block/{height}')
        body = data.get('body')
        if (not isinstance(body, dict) or not isinstance(body.get('transfers'), list)
                or not isinstance(body.get('rewards'), list)):
            raise NodeError('Unsupported block format. Select a legacy mainnet node.')
        return body

    def broadcast(self, payload, expected_hash=None):
        data = self.request('/transaction/add', payload)
        tx_hash = data.get('txHash')
        if not isinstance(tx_hash, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', tx_hash):
            raise BroadcastUncertain('Node did not return a valid transaction hash. Check the transaction before sending again.')
        if expected_hash is not None and tx_hash.lower() != expected_hash:
            raise BroadcastUncertain('Node returned a different transaction ID. Check the saved payment before sending again.')
        return tx_hash.lower()

    def lookup(self, tx_hash):
        if not isinstance(tx_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', tx_hash):
            raise NodeError('Invalid transaction ID.')
        try:
            data = self.request('/transaction/lookup/' + tx_hash)
        except NodeError as exc:
            if exc.code is not None and abs(exc.code) == 57:
                return None
            raise
        if self.expected_network == 'testnet':
            from history import defi_transaction
            transaction = defi_transaction(data.get('transaction'), data.get('type'))
            mined = data.get('mined')
            if mined is not None and (not isinstance(mined, dict) or not isinstance(mined.get('block'), dict)):
                raise NodeError('Invalid DeFi transaction block.')
            transaction.update(confirmations=data.get('confirmations'),
                               blockHeight=mined['block'].get('height') if mined else 0)
        else:
            transaction = data.get('transaction')
        if (not isinstance(transaction, dict) or not isinstance(transaction.get('txHash'), str)
                or transaction['txHash'].lower() != tx_hash):
            raise NodeError('Invalid transaction lookup response.')
        return transaction
