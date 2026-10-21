"""Versioned, authenticated encryption for portable SQLite wallet snapshots."""
import base64
import os
from pathlib import Path
import tempfile

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b'WARTBACKUP\x01'
MAX_BACKUP_BYTES = 64 * 1024 * 1024


def _cipher(password, salt):
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
        raise ValueError('Use a backup password of 8 to 1024 characters.')
    key = Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password.encode('utf-8'))
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_snapshot(snapshot, password):
    if not snapshot.startswith(b'SQLite format 3\x00') or len(snapshot) > MAX_BACKUP_BYTES // 2:
        raise ValueError('Wallet snapshot is invalid or too large.')
    salt = os.urandom(16)
    return MAGIC + salt + _cipher(password, salt).encrypt(snapshot)


def decrypt_snapshot(content, password):
    if len(content) > MAX_BACKUP_BYTES or not content.startswith(MAGIC):
        raise ValueError('Unsupported or damaged wallet backup.')
    salt, token = content[len(MAGIC):len(MAGIC) + 16], content[len(MAGIC) + 16:]
    if len(salt) != 16:
        raise ValueError('Incomplete wallet backup.')
    try:
        snapshot = _cipher(password, salt).decrypt(token)
    except InvalidToken as exc:
        raise ValueError('Incorrect backup password or damaged backup.') from exc
    if not snapshot.startswith(b'SQLite format 3\x00'):
        raise ValueError('Backup does not contain a wallet database.')
    return snapshot


def save_backup(snapshot, password, destination):
    content = encrypt_snapshot(snapshot, password)
    destination = Path(destination)
    handle, temporary = tempfile.mkstemp(prefix='.wart-backup-', dir=destination.parent)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_backup(path, password):
    with open(path, 'rb') as stream:
        content = stream.read(MAX_BACKUP_BYTES + 1)
    return decrypt_snapshot(content, password)
