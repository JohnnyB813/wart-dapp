import base64
import os
import re
import secrets
from dataclasses import dataclass
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from ecdsa import SigningKey, SECP256k1
from ecdsa.util import randrange_from_seed__trytryagain
from Cryptodome.Hash import RIPEMD160, SHA256
from pycoin.ecdsa.secp256k1 import secp256k1_generator
from hashlib import sha256
from node_client import NodeClient, NodeError

COIN = 100000000
MAX_UINT64 = (1 << 64) - 1


def validate_password(password):
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError('Use a password with at least eight characters.')


def parse_e8(text, allow_zero=False):
    """Parse exact decimal units without floating-point rounding or large allocations."""
    if not isinstance(text, str) or len(text) > 64:
        raise ValueError('Enter a valid WART amount.')
    text = text.strip()
    if not re.fullmatch(r'(?:[0-9]{1,12}(?:\.[0-9]*)?|\.[0-9]+)', text):
        raise ValueError('Enter a non-negative decimal WART amount.')
    whole, _, fraction = text.partition('.')
    fraction = fraction.rstrip('0')
    if len(fraction) > 8:
        raise ValueError('WART amounts support at most eight decimal places.')
    value = int(whole or '0') * COIN + int(fraction.ljust(8, '0') or '0')
    if value > MAX_UINT64 or (not allow_zero and value == 0):
        raise ValueError('Amount is outside the supported range.')
    return value


def format_e8(value):
    return f'{value // COIN}.{value % COIN:08d}'


def round_fee_e8(value):
    """Legacy CompactUInt::compact/uncompact, rounding down like the reference node.

    https://github.com/warthog-network/core/blob/0.8.20/src/shared/src/general/compact_uint.cpp
    Fees have an 11-bit significand. No node response supplies spending authority.
    """
    if type(value) is not int or not 0 <= value <= MAX_UINT64:
        raise ValueError('Fee is outside the supported range.')
    shift = max(0, value.bit_length() - 11)
    return (value >> shift) << shift


def validate_address(address):
    if not isinstance(address, str) or not re.fullmatch(r'[a-fA-F0-9]{48}', address):
        raise ValueError('Invalid recipient address.')
    raw = bytes.fromhex(address)
    if sha256(raw[:20]).digest()[:4] != raw[20:]:
        raise ValueError('Recipient address checksum is invalid.')
    return address.lower()


@dataclass(frozen=True)
class TransferIntent:
    recipient: str
    amount: int
    fee: int

    def __post_init__(self):
        object.__setattr__(self, 'recipient', validate_address(self.recipient))
        if type(self.amount) is not int or not 0 < self.amount <= MAX_UINT64:
            raise ValueError('Amount is outside the supported range.')
        if round_fee_e8(self.fee) != self.fee:
            raise ValueError('Fee must be locally rounded before confirmation.')
        if self.amount + self.fee > MAX_UINT64:
            raise ValueError('Amount plus fee is outside the supported range.')

    @classmethod
    def from_text(cls, recipient, amount, fee):
        return cls(recipient.strip(), parse_e8(amount), round_fee_e8(parse_e8(fee, allow_zero=True)))


# encrypt private key
def encrypt_pk(pk, pw):
    validate_password(pw)
    salt = os.urandom(16)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(pw.encode()))
    f = Fernet(key)
    pk_enc = f.encrypt(pk.encode())
    # return pk and salt
    return pk_enc.decode(), base64.b64encode(salt).decode('utf-8')


# decrypt private key
def decrypt_pk(pk_enc, pw, salt):
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=base64.b64decode(salt),
        iterations=480000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(pw.encode()))
    f = Fernet(key)
    try:
        pkhex = f.decrypt(pk_enc.encode())
    except InvalidToken:
        # wrong password
        return None
    return pkhex.decode()


def wallet_from_seed(seed):
    # generate private key
    secexp = randrange_from_seed__trytryagain(seed, SECP256k1.order)
    pk = SigningKey.from_secret_exponent(secexp, curve=SECP256k1)

    return address_from_pk(pk), pk


def wallet_from_pkhex(pkhex):
    # generate private key
    pk = SigningKey.from_string(bytes.fromhex(pkhex),curve=SECP256k1)

    return address_from_pk(pk), pk


class WalletPasswordError(ValueError):
    pass


def unlock_wallet(record, password):
    if record is None:
        raise WalletPasswordError('Wallet record is missing.')
    try:
        private_key = decrypt_pk(record[2], password, record[3])
        if private_key is None:
            raise WalletPasswordError('Invalid wallet password.')
        address, key = wallet_from_pkhex(private_key)
        if address != validate_address(record[1]):
            raise WalletPasswordError('The wallet key does not match its saved address.')
        return address, key
    except (TypeError, ValueError) as exc:
        if isinstance(exc, WalletPasswordError):
            raise
        raise WalletPasswordError('Could not unlock this wallet record.') from exc


def address_from_pk(pk):
    pubkey = pk.get_verifying_key().to_string('compressed')

    # convert public key to raw addresss
    sha = SHA256.SHA256Hash(pubkey).digest()
    addr_raw = RIPEMD160.RIPEMD160Hash(sha).digest()
    addr_raw.hex()

    # generate address by appending checksum
    addr_hash = SHA256.SHA256Hash(addr_raw).digest()
    checksum = addr_hash[0:4]
    addr = addr_raw + checksum

    return addr.hex()


@dataclass(frozen=True)
class SignedTransfer:
    tx_hash: str
    payload: dict


def prepare_tx(recipient, amount, fee, pk, peer, cancel=None, network='mainnet'):
    # Enforce the same immutable values that were displayed for confirmation.
    intent = TransferIntent(recipient, amount, fee)
    client = NodeClient(peer, cancel=cancel, network=network)
    # get pinHash and pinHeight from warthog node
    head = client.head(require_synced=True)
    client.require_network()
    pinHash = head['pinHash']
    pinHeight = head['pinHeight']

    # send parameters
    nonceId = secrets.randbits(32)  # public transaction ID, not the ECDSA nonce
    toAddr = intent.recipient
    amountE8 = intent.amount
    feeE8 = intent.fee

    # generate bytes to sign
    to_sign = \
        bytes.fromhex(pinHash) + \
        pinHeight.to_bytes(4, byteorder='big') + \
        nonceId.to_bytes(4, byteorder='big') + \
        b'\x00\x00\x00' + \
        feeE8.to_bytes(8, byteorder='big') + \
        bytes.fromhex(toAddr)[0:20] + \
        amountE8.to_bytes(8, byteorder='big')

    # create signature
    private_key = pk.privkey.secret_multiplier
    digest = sha256(to_sign).digest()
    client.check_cancelled()

    # sign with recovery id
    (r, s, rec_id) = secp256k1_generator.sign_with_recid(private_key, int.from_bytes(digest, 'big'))

    # normalize to lower s
    if s > secp256k1_generator.order() / 2:  #
        s = secp256k1_generator.order() - s
        rec_id ^= 1  # https://github.com/bitcoin-core/secp256k1/blob/e72103932d5421f3ae501f4eba5452b1b454cb6e/src/ecdsa_impl.h#L295
    signature65 = r.to_bytes(32, byteorder='big') + s.to_bytes(32, byteorder='big') + rec_id.to_bytes(1,
                                                                                                      byteorder='big')

    # post transaction request to warthog node
    postdata = {
        "pinHeight": pinHeight,
        "nonceId": nonceId,
        "toAddr": toAddr,
        "amountE8": amountE8,
        "feeE8": feeE8,
        "signature65": signature65.hex()
    }
    if network == 'testnet':
        postdata['type'] = 'wartTransfer'
        postdata['wartE8'] = postdata.pop('amountE8')
    client.check_cancelled()
    return SignedTransfer(digest.hex(), postdata)


def send_tx(recipient, amount, fee, pk, peer, cancel=None, network='mainnet'):
    prepared = prepare_tx(recipient, amount, fee, pk, peer, cancel, network)
    return NodeClient(peer, cancel=cancel, network=network).broadcast(prepared.payload)


# get balance for address
def get_balance(peer, addr, cancel=None, network='mainnet'):
    return NodeClient(peer, cancel=cancel, network=network).balance(validate_address(addr))
