"""One release version, with build metadata supplied by the packaging script."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
VERSION = (ROOT / 'version.txt').read_text(encoding='utf-8').strip()
if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?', VERSION):
    raise ValueError('Invalid application version.txt')

metadata_file = ROOT / 'build-info.json'
if metadata_file.is_file():
    BUILD_INFO = json.loads(metadata_file.read_text(encoding='utf-8'))
    if BUILD_INFO.get('version') != VERSION:
        raise ValueError('Build metadata does not match the application version.')
elif getattr(sys, 'frozen', False):
    raise ValueError('Packaged application is missing its build metadata.')
else:
    BUILD_INFO = {'version': VERSION, 'revision': 'source checkout', 'dirty': True,
                  'built_at': 'Not packaged', 'source_sha256': 'Not packaged'}

BUILD_LABEL = BUILD_INFO['revision'][:12] + (' + local changes' if BUILD_INFO['dirty'] else '')


def release_notes():
    return f'''Warthog Wallet {VERSION}
Build: {BUILD_LABEL}
Built: {BUILD_INFO['built_at']}

New in {VERSION}
• Compact password, private-key import, recovery-phrase and connection screens.
• Remove a selected Address Book contact and immediately update the Send contact list.
• Consistent settings-dialog controls and a resizable wallet workspace.

In 1.4.2
• Compact, resizable wallet selection screen with matching fonts and controls.
• Full receiving addresses fit in the wallet list without oversized text.

In 1.4.1
• Compact address book with matching controls, separate name/address columns and address copying.
• Six bundled node profiles: localhost and two public endpoints for each network.
• Existing custom profiles, node order and disabled flags are preserved during upgrade.

In 1.4.0
• Settings: add, edit, test, enable and order nodes for each network.
• Automatically use the lowest reported fee from synced nodes on the selected network.
• Live node minimum fees on mainnet and testnet, with a manual fee override.
• Older nodes without a fee API use a clearly labeled 0.00009992 WART fallback by default.
• Configurable auto-lock, refresh interval, automatic fee selection and connection failover.
• Clearer sidebar font and stable borders prevent hover text clipping.

In 1.3.0
• Connect to DeFi testnet using https://testnet.wartnode.com or your own node.
• Send and receive native testnet WART, with network-specific history and pending-payment reservations.
• Network labels on Receive, Send and confirmation; fees reflect the testnet node minimum.

In 1.2.1
• Removed unused header frames from wallet and password screens.
• Charcoal tables and outlined controls match the existing wallet theme.
• My mined blocks replaces Miners Chain: address-specific rewards, block details and older history.

Wallet storage and backups
• A consistent application data folder across releases.
• Password-protected encrypted backups, verified restore and copy-only legacy migration.
• Import an existing database without replacing wallets already in your profile.

Wallet sessions
• Manual lock and configurable automatic lock (five minutes by default).
• Password authorization for each payment; signing keys are not kept for the unlocked session.

Payments and history
• Personal incoming, outgoing and mining history with dates, fees, IDs and confirmations.
• Payments journaled before broadcast; uncertain results can be checked without automatic retries.
• Send Max, exact totals, pending-payment reservations and fresh balance checks.
• Saved contacts and a receiving-address QR code generated locally.

Nodes
• Named profiles, remembered LAN nodes, connection health, sync status and last update time.
• Mainnet and DeFi testnet identification; mismatched, unverified and unsynced nodes cannot authorize payments.
• HTTP for localhost/private LAN IPs; public endpoints require HTTPS.

Releases
• Versioned Windows packages, regression tests and SHA-256 checksums.

Supports legacy mainnet and native WART payments on DeFi testnet. Testnet coins have no mainnet value.
Node acceptance is not blockchain confirmation. Check the Transactions screen for status.
'''
