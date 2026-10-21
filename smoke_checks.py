"""Offline checks of packaged crypto, QR and backup dependencies."""
from db import DB
from payments_ui import address_qr
from utils import wallet_from_pkhex, encrypt_pk, decrypt_pk, secp256k1_generator
from wallet_backup import encrypt_snapshot, decrypt_snapshot


def verify_runtime():
    # Public synthetic fixture; this function never contacts a node.
    private = '00' * 31 + '01'
    address, key = wallet_from_pkhex(private)
    if address != '751e76e8199196d454941c45d1b3a323f1433bd6ffded884':
        raise RuntimeError('Packaged address derivation failed.')
    token, salt = encrypt_pk(private, 'packaging test password')
    if decrypt_pk(token, 'packaging test password', salt) != private:
        raise RuntimeError('Packaged wallet encryption failed.')
    secp256k1_generator.sign_with_recid(key.privkey.secret_multiplier, 1)
    if address_qr(address).isNull():
        raise RuntimeError('Packaged QR renderer failed.')
    store = DB(':memory:')
    try:
        store.insert_wallet(address, token, salt)
        snapshot = store.snapshot()
        if decrypt_snapshot(encrypt_snapshot(snapshot, 'packaging backup password'),
                            'packaging backup password') != snapshot:
            raise RuntimeError('Packaged wallet backup failed.')
    finally:
        store.con.close()
