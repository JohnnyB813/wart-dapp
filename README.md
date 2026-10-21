# Warthog Wallet 1.4.3

A Windows desktop wallet for Warthog mainnet and native WART payments on the
DeFi testnet. Private keys and signing stay on this computer.

## Run the Windows build

Extract `dist/1.4.3/wart-dapp-1.4.3-windows-x64.zip`, then run:

- `Start-Wallet.cmd` for the normal wallet profile.
- `Start-Test-Wallet.cmd` for a separate, persistent test profile.

No Python installation is required to run the executable. This is an unsigned
build. Automated checks use public synthetic keys and a loopback mock node;
they never send real WART. No live-chain transfer has been certified.

The wallet-selection window uses compact controls and a resizable layout. Select
a saved address to unlock it, or create/add a wallet using the buttons above the list.
Connection, password and wallet recovery dialogs use the same fonts and controls.

Connect to your node using a base URL such as `http://192.168.1.50:3000`.
HTTP works for localhost, RFC1918 private IPv4, IPv6 unique-local and link-local
IPs. Public IPs and other hostnames require HTTPS. LAN HTTP is unencrypted.
Save the URL as a named node profile. The status bar shows network, sync,
height and last successful update. Sending requires a synced node whose reported
network matches the selected network. Unknown networks cannot authorize payments.

## Settings, multiple nodes and fees

Open **Settings** from the wallet sidebar or the connection screen. **Nodes**
lets you add, rename, edit, enable, remove and test nodes for each network. Use
**Up** and **Down** to set their order. **Connect** uses the selected node as the
starting point; automatic selection may choose a lower-fee configured node.
Manual node changes lock the wallet before reconnecting.

The wallet includes these saved profiles:

| Network | Localhost | Public nodes |
| --- | --- | --- |
| Mainnet | `http://localhost:3000` | `https://mainnet.wartnode.com`, `https://warthognode.duckdns.org` |
| DeFi testnet | `http://localhost:3100` | `https://testnet.wartnode.com`, `https://warthog-defitestnet.duckdns.org` |

The localhost ports match [Warthog Core's defaults](https://github.com/warthog-network/core/blob/defi/src/node/cmdline/cmdoptions.ggo).
Upgrading adds missing defaults once, keeping custom profiles, node order and
disabled flags. Profiles you remove afterward stay removed across restarts.

By default the wallet compares the reported minimum fees of responding, synced
nodes on the selected network and selects the lowest. The active node wins equal
fees; list order breaks other ties. Comparisons run on connection and during
periodic refresh. Disconnected nodes can fail over to enabled nodes on the same
network. Checking nodes happens in the background with bounded timeouts.
Disable automatic lowest-fee selection or failover in **Preferences** if desired.

The Send fee follows the connected node's minimum automatically. Clear **Use the
connected node’s minimum fee** to enter a manual fee. Older nodes may not expose
`/transaction/minfee`; they remain usable and default to **0.00009992 WART**.
Send and the confirmation identify when the node minimum is unavailable. Unknown
fees are never ranked as zero during automatic selection. The fallback fee can
be changed in Preferences. A lower fee may be rejected if an older node's actual
minimum is higher; adjust the manual/fallback fee or use a node that reports it.

Fee changes update the draft and total before review. An open confirmation stays
bound to its displayed node, amount and fee. The minimum is checked again when
available before signing; an increased minimum requires a new review. Broadcasts
are never retried on another node. Automatic node switches invalidate cached
balances and refresh them before another payment can be reviewed.

Preferences also control auto-lock (1–60 minutes, default 5) and refresh timing
(10–300 seconds, default 40). Node order, enabled flags and preferences persist
across restarts and are included in encrypted backups. Restore keeps preferences
already saved in the destination profile.

## DeFi testnet

On the connection screen, choose **DeFi testnet**, select the included
**Wartnode DeFi testnet** profile (`https://testnet.wartnode.com`), and click
**Connect**. The public-node button checks both bundled public testnet nodes.
You can save other testnet node URLs as named profiles.

Create or unlock a wallet, then use **Receive** to copy its address or QR code.
Once that address has test coins, use **Send**, review the amount and fee, and
authorize with the wallet password. The initial fee comes from the node's minimum;
changes to that minimum are shown on Send and checked again before review.
Transactions and My mined blocks support testnet's account-history format.
This release supports native testnet WART; token transfers, swaps and liquidity
operations are outside this release's scope.

Mainnet and testnet use the same address format and wallet keys, but their balances,
history and pending-payment reservations are separate. Always check the network
shown on Receive and in the payment confirmation. **Change node** locks the wallet
before switching networks. Testnet coins have no mainnet value.

The **test profile** launcher is a separate storage profile, independent of the
network selector. Either storage profile can connect to either network.

## Wallet data and old databases

Normal Windows data folder:
`%LOCALAPPDATA%/WarthogNetwork/WartWallet/wartwallet.db`

Separate test profile:
`%LOCALAPPDATA%/WarthogNetwork/WartWallet-Test/wartwallet.db`

Both paths stay the same when the application moves to another release folder.
A single legacy database next to the executable or in the working directory is
copied on first use; the original is kept. Multiple candidates require a choice.
The test profile looks for `wallet-test.db`, never the production database.

For older test builds in `dist/` or `dist/lan/`, choose **Import existing
database** on the wallet-selection screen and select that build's
`wallet-test.db`. Import merges encrypted records without replacing wallets
already in your profile. The original file is left intact.

An explicit override remains available, for example when testing a copy:

```powershell
.\wart-dapp-1.4.3-windows-x64.exe --wallet-db C:\path\to\copied-wallet.db
```

## Backup and recovery

Use **Back up wallets** on the wallet-selection screen. Choose a separate
backup password and save the `.wartbackup` file somewhere protected.
The complete snapshot is encrypted and authenticated with a fixed Scrypt KDF
and Fernet. Never overwrite your live database with a backup file.

Use **Restore backup** to decrypt and validate it, then merge wallets, contacts,
payment history and node profiles into the current profile. Existing wallet
records and their passwords are preserved. Conflicting node profile names get
a restored suffix. Restore commits atomically; malformed backups add nothing.

The backup password protects the backup container. **Original wallet passwords
are still required after restoration.** The legacy wallet cipher, recovery
phrase mapping, address derivation and pasted multiline passwords are preserved.
Keep the original recovery phrase offline; it cannot be reconstructed from a
stored private key.

## Locking and payments

Use **Lock wallet**, or wait for the configured inactivity timeout (five minutes
by default). Every payment also
requires its wallet password. The app retains no signing key for the unlocked
session. A send already being broadcast can finish after a lock; its result is
saved without reopening the wallet. A lock during preparation prevents a new
broadcast.

The Send screen provides contacts, Send Max, locally rounded final fees and
exact amount-plus-fee totals. Available balance subtracts pending/uncertain
outgoing payments. Balances are checked again before signing; a SQLite
reservation is committed before the POST, including across app processes.
The Receive screen's QR contains only the receiving address and is generated
locally without a third-party service.

Address Book uses the same compact controls as Send. Contacts appear in separate
name and address columns; select one and choose **Copy address** to copy it in full.
Saved contacts are also available from the Send page's contact selector.
Select a contact and choose **Remove contact**, then confirm, to remove that entry
from Address Book and the Send selector. Other saved entries remain available.

## Personal history

Transactions lists this wallet's incoming, outgoing and mining payments, with
UTC dates, amounts, fees, other addresses, IDs and confirmations. Load older
pages without scanning the whole chain. Saved history remains visible offline.

**My mined blocks** replaces Miners Chain. It shows mining rewards paid to the
loaded wallet's address, grouped by block, with height, UTC date, reward and
confirmations. Select a block to see and copy its block hash and select its reward
transaction ID. Block hashes load from the node during refresh; saved reward
details remain available offline. Refresh rechecks the 100 most recent saved
rewards, removing rewards no longer found on the node. Load older follows the
account-history cursor, including pages with no mining rewards. Counts and reward
totals cover loaded history, rather than claiming a lifetime total. An address
only receives this attribution when the block reward was paid to it; pool payouts
appear as incoming payments in Transactions.

An outgoing payment is saved **before** broadcast using the locally computed
transaction ID. Node acceptance means pending, not confirmed. Lost, malformed
or mismatched replies stay uncertain. Refresh checks the saved ID; a not-found
reply is not proof that a payment failed. Uncertain payments continue reserving
their amount and fee. Nothing is automatically retried or re-signed.

## Build and regression tests

Use 64-bit Windows with Python 3.14.6:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

The script creates a local `.venv`, installs the pinned Windows dependency lock,
runs all unit/integration tests, records revision/source metadata, packages the
versioned executable, runs offline checks on the actual binary, then writes a
ZIP and SHA-256 manifest. Output defaults to `dist/<version>/`. Use
`-OutputDirectory dist/custom` when an existing executable is still open.

Tests alone:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Application dependencies are in `requirements.txt`; `requirements-test.txt`
adds an independent QR decoder, and `requirements-build.txt` adds PyInstaller.
Pillow and the QR decoder are test tools and are excluded from the executable.

GitHub's Windows workflow runs the same build/test/package command for pull
requests, main/master pushes, version tags and manual runs. Tags must match
`version.txt`. Downloadable CI ZIP/checksum artifacts are retained for 30 days;
the workflow does not automatically publish a GitHub release. Wallet databases
and backup files are excluded from the release ZIP.

The app displays the release version, build revision and current release notes.
`build-info.json` records the exact runtime-source digest, revision, local-change
status, Python version and build time. `SHA256SUMS.txt` covers release files and
the ZIP; compare an executable's hash with `Get-FileHash -Algorithm SHA256`.
