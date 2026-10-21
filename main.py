import argparse
import sys
import time
import webbrowser
import sqlite3

from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QDialog, QMessageBox
from PySide6.QtWidgets import QFileDialog, QInputDialog, QLineEdit, QToolBar
from PySide6.QtWidgets import QFormLayout, QDialogButtonBox, QLabel
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTextBrowser, QPushButton
from datetime import datetime, timezone
from types import SimpleNamespace
from mnemonic import Mnemonic

from connection_ui import ConnectionPanel
from wallets_ui import WalletsPanel
from mainwindow import Ui_MainWindow_Wart
from dialogs_ui import password_controls, recovery_controls, phrase_controls
from db import DB
from app_paths import prepare_wallet_path, default_wallet_path, MigrationChoiceRequired, copy_database, read_database_snapshot
from app_info import VERSION, BUILD_LABEL, release_notes
from theme import apply_theme, TOOLBAR_STYLE, PANEL_STYLE, SIDEBAR_STYLE, hide_unused_header
from settings_ui import SettingsPage, SettingsDialog
from wallet_settings import load_preferences
from node_pool import probe_nodes
from wallet_backup import save_backup, load_backup
from history import fetch_account_page, reconcile_local, LATEST_CURSOR, ACTIVE_STATUSES
from history_ui import HistoryPanel
from mined_blocks import fetch_mined_page, mined_blocks
from mined_ui import MinedBlocksPanel
from payments_ui import SendPanel, ReceivePanel, address_qr
from contacts_ui import ContactsPanel
from bundled_nodes import PUBLIC_NODES, TESTNET_NODES
from PySide6.QtGui import QPixmap, QFont, QIcon
from gui_jobs import GuiJobs
from node_client import NodeClient, NodeError, BroadcastUncertain
from utils import (TransferIntent, encrypt_pk, format_e8, get_balance,
                   validate_password, wallet_from_pkhex, wallet_from_seed,
                   unlock_wallet, WalletPasswordError, prepare_tx, parse_e8, round_fee_e8, validate_address)

mnemo = Mnemonic('english')
db = None
PEER = None
address = ''


def network_label(network):
    return 'DeFi testnet (test coins)' if network == 'testnet' else 'Mainnet'


def message(parent, text, title='Warthog Network'):
    box = QMessageBox(parent)
    box.setStyleSheet(PANEL_STYLE + 'QMessageBox { background: #16191d; }')
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(str(text))
    box.exec()


class Register(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.register = password_controls(self, confirmation=True)
        self._password = None
        self.register.ok_btn.clicked.connect(self.ok)

    def ok(self):
        password = self.register.textEdit.text()
        if password != self.register.textEdit_2.text():
            message(self, 'Passwords do not match.')
            return
        try:
            validate_password(password)
        except ValueError as exc:
            message(self, exc)
            return
        self._password = password
        self.accept()

    def take_password(self):
        password = self._password if self.result() == QDialog.DialogCode.Accepted else None
        self._password = None
        self.register.textEdit.clear()
        self.register.textEdit_2.clear()
        return password

    def reject(self):
        self._password = None
        self.register.textEdit.clear()
        self.register.textEdit_2.clear()
        super().reject()


class Login(QDialog):
    def __init__(self, parent, addr):
        super().__init__(parent)
        self.login = password_controls(self)
        self.addr = addr
        self.login.ok_btn.clicked.connect(self.ok)
        self.finished.connect(lambda result: self.login.textEdit.clear())

    def ok(self):
        global address
        wallet = db.get_wallet_by_address(self.addr)
        try:
            addr, key = unlock_wallet(wallet, self.login.textEdit.text())
            del key
        except WalletPasswordError as exc:
            message(self, exc)
            return
        self.login.textEdit.clear()
        address = addr
        self.accept()


class Confirm(QDialog):
    def __init__(self, parent, intent, peer, wallet_address, available=None, network=None, fee_note=''):
        super().__init__(parent)
        self.network = network or db.network
        self.setWindowTitle('Confirm WART payment — ' + network_label(self.network))
        self.setMinimumWidth(620)
        self.setMaximumWidth(900)
        self.setStyleSheet(PANEL_STYLE + 'QDialog { background: #16191d; } '
                           'QLabel { padding: 4px; }')
        layout = QFormLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setVerticalSpacing(10)
        self.confirm = SimpleNamespace()
        for field, title, value in [('recipient_label', 'Recipient', intent.recipient),
                                    ('amount_label', 'Amount (WART)', format_e8(intent.amount)),
                                    ('amount_label_2', 'Fee (WART)', format_e8(intent.fee))]:
            label = QLabel(value)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addRow(title, label)
            setattr(self.confirm, field, label)
        self.total_label = QLabel(format_e8(intent.amount + intent.fee) + ' WART')
        layout.addRow('Total debit', self.total_label)
        self.fee_note = QLabel(fee_note)
        self.fee_note.setWordWrap(True)
        self.fee_note.setTextFormat(Qt.TextFormat.PlainText)
        if fee_note:
            layout.addRow('Fee source', self.fee_note)
        network = QLabel(network_label(self.network) + ' — ' + peer)
        network.setTextFormat(Qt.TextFormat.PlainText)
        network.setWordWrap(True)
        layout.addRow('Node', network)
        if available is not None:
            layout.addRow('Available (WART)', QLabel(format_e8(available)))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('Enter this wallet’s password to authorize this payment')
        layout.addRow('Wallet password', self.password)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.confirm.ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm.ok_btn.setText('Confirm and send')
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.intent, self.peer, self.addr = intent, peer, wallet_address
        self.sending = False
        self.locked = False
        self.tx_hash = None
        self.jobs = GuiJobs(self)
        self.confirm.ok_btn.clicked.connect(self.confirm_btn)
        self.finished.connect(lambda result: self.password.clear())

    def confirm_btn(self):
        if self.sending or self.locked or address != self.addr or db.network != self.network:
            return
        record = db.get_wallet_by_address(self.addr)
        password = self.password.text()
        self.password.clear()
        self.sending = True
        self.password.setEnabled(False)
        self.confirm.ok_btn.setEnabled(False)
        self.confirm.ok_btn.setText('Sending...')
        intent, peer = self.intent, self.peer

        def work(cancel):
            _, key = unlock_wallet(record, password)
            try:
                client = NodeClient(peer, cancel=cancel, network=self.network)
                try:
                    minimum = client.minimum_fee()
                except NodeError:
                    minimum = None  # Legacy nodes may not expose this endpoint.
                if minimum is not None and intent.fee < minimum:
                    raise NodeError('The node minimum fee has changed to ' + format_e8(minimum)
                                    + ' WART. Review the payment again. Nothing was broadcast.')
                balance = parse_e8(client.balance(self.addr), allow_zero=True)
                if intent.amount + intent.fee > balance:
                    raise NodeError('Insufficient balance for this amount and fee. Nothing was broadcast.')
                return prepare_tx(intent.recipient, intent.amount, intent.fee, key, peer, cancel, self.network), balance
            finally:
                del key

        self.jobs.submit('send', work, self.prepared, self.failed)

    def prepared(self, result):
        prepared, balance = result
        if self.locked or address != self.addr or db.network != self.network:
            self.sending = False
            self.reject()
            return
        try:
            db.record_outgoing(self.addr, self.intent, prepared, self.peer, balance, self.network)
        except (sqlite3.Error, ValueError) as exc:
            self.failed(NodeError('Could not save the payment journal. Nothing was broadcast. ' + str(exc)))
            return
        self.tx_hash = prepared.tx_hash
        self.jobs.submit('send', lambda cancel: NodeClient(self.peer, cancel=cancel, network=self.network).broadcast(
            prepared.payload, prepared.tx_hash), self.sent, self.failed)

    def sent(self, tx_hash):
        db.set_tx_status(self.addr, self.tx_hash, 'pending', self.network)
        if not self.locked:
            message(self, 'Transaction accepted.\n' + tx_hash)
        self.accept()

    def failed(self, exc):
        self.sending = False
        if self.tx_hash:
            db.set_tx_status(self.addr, self.tx_hash, 'uncertain' if isinstance(exc, BroadcastUncertain) else 'rejected', self.network)
        if not self.locked:
            message(self, exc, 'Broadcast uncertain' if isinstance(exc, BroadcastUncertain) else 'Transaction error')
        if isinstance(exc, WalletPasswordError) and not self.locked:
            self.confirm.ok_btn.setText('Confirm and send')
            self.confirm.ok_btn.setEnabled(True)
            self.password.setEnabled(True)
            self.password.setFocus()
            return
        self.reject()

    def lock(self):
        self.locked = True
        self.password.clear()
        self.setEnabled(False)
        if not self.sending:
            self.reject()

    def reject(self):
        if self.sending:
            message(self, 'A send is in progress. Wait for its result before closing.')
            return
        self.password.clear()
        self.jobs.cancel_all()
        super().reject()


class MainWindow(QMainWindow):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.mainUI = Ui_MainWindow_Wart()
        self.mainUI.setupUi(self)
        hide_unused_header(self.mainUI)
        self.setMaximumSize(16777215, 16777215)
        self.setMinimumHeight(680)
        self.mainUI.stackedWidget.setFont(QFont('Segoe UI', 10))
        self.mainUI.balance.setFont(QFont('Segoe UI', 11))
        self.mainUI.label_2.setFont(QFont('Segoe UI', 11))
        # Replace the fixed page rectangle with a layout below the balance row.
        content_layout = QVBoxLayout(self.mainUI.mainContent)
        content_layout.setContentsMargins(0, 0, 12, 0)
        content_layout.setSpacing(12)
        balance_bar = QHBoxLayout()
        balance_bar.addStretch()
        self.mainUI.label_7.setText('●')
        self.mainUI.label_7.setFont(QFont('Segoe UI', 10))
        self.mainUI.label_7.setFixedSize(18, 30)
        balance_bar.addWidget(self.mainUI.label_7)
        balance_bar.addWidget(self.mainUI.label_2)
        balance_bar.addWidget(self.mainUI.balance)
        content_layout.addLayout(balance_bar)
        content_layout.addWidget(self.mainUI.stackedWidget, 1)
        self.send_panel = SendPanel()
        self.mainUI.stackedWidget.removeWidget(self.mainUI.page_2)
        self.mainUI.stackedWidget.insertWidget(3, self.send_panel)
        self.mainUI.addr_text = self.send_panel.recipient
        self.mainUI.amount_text = self.send_panel.amount
        self.mainUI.fee_text = self.send_panel.fee
        self.mainUI.send_btn = self.send_panel.send
        self.send_panel.maximum.clicked.connect(self.send_max)
        self.balance_snapshot = None
        self.receive_panel = ReceivePanel()
        self.mainUI.stackedWidget.removeWidget(self.mainUI.page)
        self.mainUI.stackedWidget.insertWidget(2, self.receive_panel)
        self.mainUI.address_text = self.receive_panel.address
        self.mainUI.copy_btn = self.receive_panel.copy
        self.receive_qr = self.receive_panel.qr
        self.receive_qr.setToolTip('Scan to copy this receiving address. No private data is encoded.')
        self.contacts_panel = ContactsPanel()
        self.mainUI.stackedWidget.removeWidget(self.mainUI.page_3)
        self.mainUI.stackedWidget.insertWidget(4, self.contacts_panel)
        self.mainUI.addr_text_2 = self.contacts_panel.name
        self.mainUI.addr_text_3 = self.contacts_panel.address
        self.mainUI.add_bttn = self.contacts_panel.add
        self.jobs = GuiJobs(self)
        self.active_confirm = None
        self.node_state = None
        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(5 * 60 * 1000)
        self.idle_timer.timeout.connect(self.lock_wallet)
        QApplication.instance().installEventFilter(self)
        toolbar = QToolBar('Wallet session', self)
        toolbar.setStyleSheet(TOOLBAR_STYLE)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addAction('Lock wallet', self.lock_wallet)
        toolbar.addAction('Change node', self.change_node)
        toolbar.addAction('Settings', self.show_settings)
        self.auto_lock_label = QLabel()
        toolbar.addWidget(self.auto_lock_label)
        self.setWindowTitle(f'Warthog Wallet {VERSION} — Mainnet')
        notes = QWidget()
        notes_layout = QVBoxLayout(notes)
        self.release_text = QTextBrowser()
        self.release_text.setPlainText(release_notes())
        self.release_text.setStyleSheet(PANEL_STYLE)
        notes_layout.addWidget(self.release_text)
        old_notes = self.mainUI.stackedWidget.widget(0)
        self.mainUI.stackedWidget.removeWidget(old_notes)
        self.mainUI.stackedWidget.insertWidget(0, notes)
        self.mainUI.stackedWidget.setCurrentIndex(2)
        for button, page in [('send_button', 3), ('address_button', 2),
                             ('contact_button', 4), ('transactions_button', 5),
                             ('miners_button', 6), ('notes', 0)]:
            getattr(self.mainUI, button).clicked.connect(
                lambda checked=False, index=page: self.mainUI.stackedWidget.setCurrentIndex(index))
        self.mainUI.pushButton_4.clicked.connect(self.showMinimized)
        self.mainUI.send_btn.clicked.connect(self.send_wart)
        self.mainUI.add_bttn.clicked.connect(self.add_friend)
        self.contacts_panel.remove.clicked.connect(self.remove_friend)
        self.mainUI.show_btn.clicked.connect(self.list_transactions)
        self.mainUI.copy_btn.clicked.connect(self.copy_to_clipboard)
        for button, url in [('discord_btn', 'https://discord.com/invite/QMDV8bGTdQ'),
                            ('telegram_btn', 'https://t.me/warthognetwork'),
                            ('twitter_btn', 'https://twitter.com/warthognetwork'),
                            ('browser_btn', 'https://warthog.network/'),
                            ('git_btn', 'https://github.com/warthog-network')]:
            getattr(self.mainUI, button).clicked.connect(lambda checked=False, target=url: webbrowser.open(target))
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_balance)
        self.refresh_timer.timeout.connect(self.refresh_pending_history)
        self.refresh_timer.timeout.connect(self.refresh_node_status)
        self.refresh_timer.start(40000)
        self.history_cursor = LATEST_CURSOR
        self.history_panel = HistoryPanel(self.list_transactions, self.load_older_history, self.check_selected_history)
        self.mainUI.stackedWidget.removeWidget(self.mainUI.page_4)
        self.mainUI.stackedWidget.insertWidget(5, self.history_panel)
        self.mainUI.transactions_button.setText('Transactions')
        self.mainUI.transactions_button.clicked.connect(self.list_transactions)
        self.mined_cursor = LATEST_CURSOR
        self.mined_details = {}
        self.mined_panel = MinedBlocksPanel(self.list_mined_blocks, self.load_older_mined_blocks)
        self.mainUI.stackedWidget.removeWidget(self.mainUI.page_5)
        self.mainUI.stackedWidget.insertWidget(6, self.mined_panel)
        self.mainUI.miners_button.setText('My mined blocks')
        self.mainUI.miners_button.clicked.connect(self.list_mined_blocks)
        self.settings_panel = SettingsPage(db)
        self.settings_index = self.mainUI.stackedWidget.addWidget(self.settings_panel)
        self.settings_button = QPushButton('⚙  Settings')
        self.mainUI.verticalLayout_2.addWidget(self.settings_button)
        self.settings_button.clicked.connect(self.show_settings)
        self.settings_panel.preferences_changed.connect(self.apply_preferences)
        self.settings_panel.profiles_changed.connect(self.node_configuration_changed)
        self.settings_panel.connect_requested.connect(self.connect_configured_node)
        for name in ('address_button', 'send_button', 'contact_button', 'transactions_button', 'miners_button', 'notes'):
            button = getattr(self.mainUI, name)
            button.setFont(QFont('Segoe UI', 13))
            button.setStyleSheet(SIDEBAR_STYLE)
            button.setMinimumHeight(44)
        self.settings_button.setStyleSheet(SIDEBAR_STYLE)
        self.settings_button.setMinimumHeight(44)
        self.preferences = None
        self.apply_preferences()
        self.get_friends()
        self.statusBar().showMessage('Mainnet wallet — connect to a node to verify network and synchronization.')
        self.statusBar().setStyleSheet('QStatusBar { background: #16191d; color: #eeeeee; padding: 4px; }')
        self.mainUI.label_7.setStyleSheet('color: #9aa4b2;')

    def change_node(self):
        if self.active_confirm is not None:
            message(self, 'Finish or cancel the payment review before changing nodes.')
            return
        self.lock_wallet()
        self.parent.hide()
        window.load_profiles()
        window.show()

    def show_settings(self):
        self.settings_panel.reload()
        self.settings_panel.reload_preferences()
        self.mainUI.stackedWidget.setCurrentIndex(self.settings_index)

    def connect_configured_node(self, peer, network):
        if self.active_confirm is not None:
            message(self, 'Finish or cancel the payment review before changing nodes.')
            return
        self.change_node()
        window.network.setCurrentIndex(window.network.findData(network))
        window.ui.input_field.setText(peer)
        window.private_connect()

    def apply_preferences(self):
        previous = self.preferences
        self.preferences = load_preferences(db)
        self.idle_timer.setInterval(self.preferences['auto_lock_minutes'] * 60000)
        self.refresh_timer.setInterval(self.preferences['refresh_seconds'] * 1000)
        self.auto_lock_label.setText(f"  Auto-lock after {self.preferences['auto_lock_minutes']} minutes of inactivity")
        if previous is None or previous['auto_minfee'] != self.preferences['auto_minfee']:
            self.send_panel.use_minimum.setChecked(self.preferences['auto_minfee'])
        if not self.preferences['auto_failover']:
            self.jobs.cancel('failover')
        if not self.preferences['auto_lowest_fee']:
            self.jobs.cancel('selection')
        self.send_panel.fallback_fee = self.preferences['fallback_fee_e8']
        self.send_panel.set_node_fee(self.send_panel.node_minimum)
        if self.isVisible() and address:
            self.idle_timer.start()
            self.node_configuration_changed()

    def node_configuration_changed(self):
        self.jobs.cancel('selection')
        self.jobs.cancel('failover')
        if self.node_state and self.node_state.get('online'):
            self.select_lowest_fee()
        else:
            self.try_failover()

    def set_node_status(self, state):
        self.node_state = dict(state, online=True)
        checked = datetime.fromtimestamp(state['checked_at'], timezone.utc).strftime('%H:%M:%S UTC')
        network = network_label(state['network']) if state['network'] != 'unverified' else 'Network unverified'
        sync = 'Synced' if state['synced'] else 'Synchronizing'
        node_name = state['peer'].split('://', 1)[-1]
        self.statusBar().showMessage(f"Connected | {network} | {sync} | {node_name} | Height {state['height']} | Updated {checked}")
        self.statusBar().setToolTip(f"{state['peer']} — node {state['version']}")
        ready = state['network'] == db.network and state['synced']
        self.mainUI.send_btn.setEnabled(ready)
        self.mainUI.label_7.setStyleSheet('color: ' + ('#51c76f' if ready else '#e5b953') + ';')
        self.setWindowTitle(f'Warthog Wallet {VERSION} — ' + network_label(db.network))
        self.send_panel.network.setText(network_label(db.network) + ' WART')
        self.receive_panel.network.setText('Receiving address — ' + network_label(db.network))
        self.settings_panel.active_node.setText('Active node: ' + state['peer'] + ' · ' + network)
        self.settings_panel.record_checks(state.get('node_checks', []) + [state])
        self.send_panel.set_node_fee(state.get('min_fee_e8'), state.get('fee_error', ''))

    def refresh_node_status(self):
        if not PEER or self.jobs.running('status') or not (self.isVisible() or self.parent.isVisible()):
            return
        peer, network = PEER, db.network
        def success(state):
            if (PEER, db.network) == (peer, network):
                self.set_node_status(state)
                if not state['synced'] and self.preferences['auto_failover']:
                    self.try_failover()
                else:
                    self.select_lowest_fee()
        def failure(exc):
            if (PEER, db.network) == (peer, network):
                self.mainUI.send_btn.setEnabled(False)
                self.mainUI.label_7.setStyleSheet('color: #e26161;')
                if self.node_state:
                    self.node_state['online'] = False
                updated = self.node_state.get('checked_at') if self.node_state else None
                when = datetime.fromtimestamp(updated, timezone.utc).strftime('%H:%M:%S UTC') if updated else 'never'
                self.statusBar().showMessage(f'Offline | {network_label(network)} | Last successful update: {when}')
                self.statusBar().setToolTip(str(exc))
                self.try_failover()
        self.jobs.submit('status', lambda cancel: NodeClient(peer, cancel=cancel, network=network).status(), success, failure)

    def try_failover(self):
        if (not self.preferences['auto_failover'] or self.active_confirm is not None
                or self.jobs.running('failover') or not address):
            return
        peer, network = PEER, db.network
        candidates = [url for url in db.node_candidates(network) if url != peer]
        if not candidates:
            return
        self.statusBar().showMessage('Connection lost. Checking other configured nodes on ' + network_label(network) + '…')
        def success(state):
            global PEER
            if (PEER, db.network) != (peer, network) or not address or self.active_confirm is not None:
                return
            self.adopt_node(state)
        def failure(exc):
            if (PEER, db.network) == (peer, network):
                self.statusBar().showMessage('Offline | ' + network_label(network) + ' | No configured fallback is available')
                self.statusBar().setToolTip(str(exc))
        lowest = self.preferences['auto_lowest_fee']
        self.jobs.submit('failover', lambda cancel: probe_nodes(candidates, network, cancel, require_synced=True, lowest_fee=lowest), success, failure)

    def adopt_node(self, state):
        global PEER
        self.jobs.cancel_all()
        PEER = state['peer']
        db.update_peer(PEER, db.network)
        self.balance_snapshot = None
        self.mainUI.balance.setText('Loading...')
        self.send_panel.available.setText('Node changed. Refreshing available balance…')
        self.set_node_status(state)
        self.refresh_balance()
        self.refresh_pending_history()

    def select_lowest_fee(self):
        if (not self.preferences['auto_lowest_fee'] or self.active_confirm is not None
                or self.jobs.running('selection') or self.jobs.running('failover')
                or not address or not PEER or not self.node_state or not self.node_state['online']):
            return
        peer, network = PEER, db.network
        candidates = db.node_candidates(network)
        configured = db.configured_nodes(network)
        if peer in candidates or not any(node['url'] == peer for node in configured):
            candidates = list(dict.fromkeys([peer] + candidates))
        if len(candidates) < 2:
            return
        def success(state):
            if (PEER, db.network) != (peer, network) or not address or self.active_confirm is not None:
                return
            if state['peer'] == peer:
                self.set_node_status(state)
            else:
                self.adopt_node(state)
        self.jobs.submit('selection', lambda cancel: probe_nodes(candidates, network, cancel,
                         require_synced=True, lowest_fee=True), success, lambda exc: None)

    def get_friends(self):
        contacts = db.get_friends()
        self.contacts_panel.set_contacts(contacts)
        self.send_panel.set_contacts(contacts)

    def add_friend(self):
        try:
            recipient = validate_address(self.mainUI.addr_text_3.text().strip())
            name = self.mainUI.addr_text_2.text().strip()
            if not 1 <= len(name) <= 64:
                raise ValueError('Use a contact name of 1 to 64 characters.')
        except ValueError as exc:
            message(self, exc)
            return
        db.insert_friend(recipient, name)
        self.get_friends()

    def remove_friend(self):
        contact = self.contacts_panel.selected_contact()
        if contact is None:
            return
        prompt = QMessageBox(self)
        prompt.setStyleSheet(PANEL_STYLE + 'QMessageBox { background: #16191d; }')
        prompt.setWindowTitle('Remove contact')
        prompt.setTextFormat(Qt.TextFormat.PlainText)
        prompt.setText(f'Remove {contact["name"]} from Address Book?\n\n{contact["address"]}')
        prompt.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        prompt.setDefaultButton(QMessageBox.StandardButton.No)
        if prompt.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            db.delete_friend(contact['id'])
        except sqlite3.Error as exc:
            message(self, 'Could not remove this contact. ' + str(exc))
            return
        self.get_friends()

    def copy_to_clipboard(self):
        QApplication.clipboard().setText(self.mainUI.address_text.text())

    def show_wallet(self):
        self.jobs.cancel_all()
        self.mainUI.address_text.setText(address)
        self.mainUI.address_text.setCursorPosition(0)
        self.receive_qr.setPixmap(QPixmap.fromImage(address_qr(address)))
        self.balance_snapshot = None
        self.send_panel.available.setText('Available balance: updating…')
        self.mainUI.stackedWidget.setCurrentIndex(2)
        self.mainUI.balance.setText('Loading...')
        self.show()
        self.idle_timer.start()
        self.refresh_balance()
        self.history_cursor = LATEST_CURSOR
        self.history_panel.show_rows(db.history(address))
        self.mined_cursor = LATEST_CURSOR
        self.mined_details = {}
        self.mined_panel.refresh.setEnabled(True)
        self.mined_panel.older.setEnabled(True)
        self.mined_panel.show_blocks(mined_blocks(db.history(address), address))
        self.mined_panel.notice.setText('Saved rewards for this address. Refresh to verify their current status.')
        self.refresh_node_status()

    def eventFilter(self, watched, event):
        if (self.isVisible() and address and event.type() in (
                QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                QEvent.Type.Wheel, QEvent.Type.TouchBegin)):
            self.idle_timer.start()
        return super().eventFilter(watched, event)

    def lock_wallet(self):
        global address
        self.idle_timer.stop()
        self.jobs.cancel_all()
        self.settings_panel.jobs.cancel_all()
        self.settings_panel.test.setEnabled(True)
        address = ''
        if self.active_confirm is not None:
            self.active_confirm.lock()
        self.mainUI.address_text.clear()
        self.receive_qr.clear()
        self.balance_snapshot = None
        self.mainUI.balance.clear()
        self.mainUI.amount_text.clear()
        self.mainUI.addr_text.clear()
        self.send_panel.contacts.setCurrentIndex(0)
        self.send_panel.available.setText('Wallet locked')
        self.mainUI.friends_2.clear()
        self.history_panel.show_rows([])
        self.mined_details = {}
        self.mined_panel.show_blocks([])
        self.mined_panel.notice.setText('Unlock a wallet to see its mining rewards.')
        self.hide()
        self.parent.show()

    def refresh_balance(self):
        if not self.isVisible() or not address or not PEER or self.jobs.running('balance'):
            return
        peer, addr, network = PEER, address, db.network

        def success(value):
            if (PEER, address, db.network) == (peer, addr, network):
                self.mainUI.balance.setText(value)
                self.balance_snapshot = (peer, addr, parse_e8(value, allow_zero=True), time.monotonic())
                available = max(0, self.balance_snapshot[2] - db.reserved_amount(addr))
                self.send_panel.available.setText('Available after pending payments: ' + format_e8(available) + ' WART')

        def failure(exc):
            if (PEER, address, db.network) == (peer, addr, network):
                self.mainUI.balance.setText('Unavailable')
                self.balance_snapshot = None
                self.send_panel.available.setText('Available balance: unavailable — reconnect to refresh.')

        self.jobs.submit('balance', lambda cancel: get_balance(peer, addr, cancel, network), success, failure)

    def list_transactions(self):
        self.refresh_history(LATEST_CURSOR)

    def load_older_history(self):
        if self.history_cursor:
            self.refresh_history(self.history_cursor)

    def check_selected_history(self):
        selected = self.history_panel.selected_id()
        if not selected:
            message(self, 'Select a payment to check its transaction ID on your node.')
            return
        self.refresh_history(self.history_cursor, selected)

    def refresh_pending_history(self):
        if self.isVisible() and address and self.mainUI.stackedWidget.currentIndex() == 6:
            self.list_mined_blocks()
        if self.isVisible() and address and (self.mainUI.stackedWidget.currentIndex() == 5
                or any(row['status'] in ACTIVE_STATUSES for row in db.history(address))):
            self.list_transactions()

    def refresh_history(self, cursor, selected=None):
        if not address or not PEER or self.jobs.running('history'):
            return
        peer, owner, network = PEER, address, db.network
        records = db.history(owner)
        self.history_panel.show_rows(records)
        timestamps = {}
        # Recheck recent known transactions as well, including possible reorgs.
        checking = [row for row in records if row['status'] in ACTIVE_STATUSES or row['status'] == 'confirmed'][:100]
        if selected:
            checking = [row for row in records if row['tx_hash'] == selected]
        self.history_panel.notice.setText('Updating account payments and saved broadcasts…')
        self.history_panel.refresh.setEnabled(False)
        self.history_panel.older.setEnabled(False)

        def work(cancel):
            client = NodeClient(peer, cancel=cancel, deadline=time.monotonic() + 30, network=network)
            client.require_network()
            rows, next_cursor = ([], cursor) if selected else fetch_account_page(client, owner, cursor, timestamps)
            updates = reconcile_local(client, checking, timestamps)
            return updates + rows, next_cursor

        def success(result):
            if (PEER, address, db.network) != (peer, owner, network):
                return
            rows, next_cursor = result
            try:
                db.save_history(rows)
            except (sqlite3.Error, ValueError) as exc:
                failure(exc)
                return
            self.history_cursor = next_cursor
            self.history_panel.show_rows(db.history(owner))
            self.history_panel.notice.setText('Updated from your node. Uncertain/not-found payments are never retried automatically.')
            self.history_panel.refresh.setEnabled(True)
            self.history_panel.older.setEnabled(bool(next_cursor))

        def failure(exc):
            if (PEER, address, db.network) != (peer, owner, network):
                return
            self.history_panel.notice.setText('Showing saved payments. Update failed: ' + str(exc))
            self.history_panel.refresh.setEnabled(True)
            self.history_panel.older.setEnabled(bool(self.history_cursor))

        self.jobs.submit('history', work, success, failure)

    def list_mined_blocks(self):
        self.refresh_mined_blocks(LATEST_CURSOR)

    def load_older_mined_blocks(self):
        if self.mined_cursor:
            self.refresh_mined_blocks(self.mined_cursor)

    def refresh_mined_blocks(self, cursor):
        if not address or not PEER or self.jobs.running('mined'):
            return
        peer, owner, network = PEER, address, db.network
        known = db.history(owner)
        self.mined_panel.show_blocks(mined_blocks(known, owner, self.mined_details))
        self.mined_panel.notice.setText('Updating rewards and checking recent saved blocks...')
        self.mined_panel.refresh.setEnabled(False)
        self.mined_panel.older.setEnabled(False)

        def work(cancel):
            client = NodeClient(peer, cancel=cancel, deadline=time.monotonic() + 30, network=network)
            return fetch_mined_page(client, owner, cursor, known)

        def success(result):
            if (PEER, address, db.network) != (peer, owner, network):
                return
            rows, next_cursor, details = result
            try:
                db.save_history(rows)
            except (sqlite3.Error, ValueError) as exc:
                failure(exc)
                return
            self.mined_cursor = next_cursor
            for row in rows:
                self.mined_details.pop(row['tx_hash'], None)
            self.mined_details.update(details)
            self.mined_panel.show_blocks(mined_blocks(db.history(owner), owner, self.mined_details))
            self.history_panel.show_rows(db.history(owner))
            self.mined_panel.notice.setText(
                'Rewards paid to this address. Totals cover loaded history; the 100 most recent saved rewards are rechecked. '
                + ('Load older to check earlier history.' if next_cursor else 'End of account history reached.'))
            self.mined_panel.refresh.setEnabled(True)
            self.mined_panel.older.setEnabled(bool(next_cursor))

        def failure(exc):
            if (PEER, address, db.network) != (peer, owner, network):
                return
            self.mined_panel.notice.setText('Showing saved rewards. Update failed: ' + str(exc))
            self.mined_panel.refresh.setEnabled(True)
            self.mined_panel.older.setEnabled(bool(self.mined_cursor))

        self.jobs.submit('mined', work, success, failure)

    def send_wart(self):
        if not PEER or not address:
            message(self, 'Unlock a wallet and connect to a node first.')
            return
        if self.jobs.running('failover'):
            message(self, 'Wait for the node connection to finish before reviewing a payment.')
            return
        if (not self.node_state or self.node_state['peer'] != PEER or not self.node_state['online']
                or self.node_state['network'] != db.network or not self.node_state['synced']
                or time.time() - self.node_state['checked_at'] > 90):
            self.refresh_node_status()
            message(self, 'Wait for a fresh connection to a synced node on the selected network before sending.')
            return
        try:
            intent = TransferIntent.from_text(self.mainUI.addr_text.text(),
                                             self.mainUI.amount_text.text(), self.mainUI.fee_text.text())
            if intent.fee < self.node_state.get('min_fee_e8', 0):
                raise ValueError('The fee is below this node’s minimum: '
                                 + format_e8(self.node_state['min_fee_e8']) + ' WART.')
            if intent.recipient == address:
                raise ValueError('Choose a different recipient. Warthog does not support sending to the same address.')
            available = self.available_balance()
            if intent.amount + intent.fee > available:
                raise ValueError('Amount plus fee exceeds your available balance after pending payments.')
        except ValueError as exc:
            message(self, exc)
            return
        if 'min_fee_e8' in self.node_state:
            fee_note = ('Node minimum: ' if self.send_panel.use_minimum.isChecked() else 'Manual fee; node minimum: ')
            fee_note += format_e8(self.node_state['min_fee_e8']) + ' WART'
        elif self.send_panel.use_minimum.isChecked():
            fee_note = 'Wallet fallback: ' + format_e8(intent.fee) + ' WART. Node minimum unavailable.'
        else:
            fee_note = 'Manual fee. Node minimum unavailable.'
        dialog = Confirm(self, intent, PEER, address, available, db.network, fee_note)
        self.active_confirm = dialog
        dialog.exec()
        self.active_confirm = None
        dialog.deleteLater()
        self.refresh_balance()
        self.refresh_pending_history()

    def available_balance(self):
        snapshot = self.balance_snapshot
        if (not snapshot or snapshot[:2] != (PEER, address)
                or time.monotonic() - snapshot[3] > 60):
            self.refresh_balance()
            raise ValueError('Balance is refreshing. Wait for the updated balance and try again.')
        return max(0, snapshot[2] - db.reserved_amount(address))

    def send_max(self):
        try:
            available = self.available_balance()
            fee = round_fee_e8(parse_e8(self.mainUI.fee_text.text(), allow_zero=True))
            if available <= fee:
                raise ValueError('Available balance does not cover the fee and a positive payment.')
            self.mainUI.amount_text.setText(format_e8(available - fee))
        except ValueError as exc:
            message(self, exc)

    def closeEvent(self, event):
        self.lock_wallet()
        event.accept()


class PrivkeyRestore(QDialog):
    def __init__(self, parent, password):
        super().__init__(parent)
        self.key = recovery_controls(self, private_key=True)
        self.password = password
        self.key.ok_btn.clicked.connect(self.ok)

    def ok(self):
        try:
            addr, key = wallet_from_pkhex(self.key.pk_label.text().strip())
            encrypted, salt = encrypt_pk(key.to_string().hex(), self.password)
            db.insert_wallet(addr, encrypted, salt)
        except (ValueError, TypeError):
            message(self, 'Enter a valid 64-character private key.')
            return
        self.key.pk_label.clear()
        self.password = None
        self.accept()

    def reject(self):
        self.key.pk_label.clear()
        self.password = None
        super().reject()


class MnemoRestore(QDialog):
    def __init__(self, parent, password):
        super().__init__(parent)
        self.addmnemo = recovery_controls(self)
        self.password = password
        self.addmnemo.ok_btn.clicked.connect(self.okay_btn)

    def okay_btn(self):
        words = self.addmnemo.pk_label.text()
        if len(words) < 40:
            message(self, 'Enter the complete original mnemonic phrase.')
            return
        # Keep the exact legacy seed mapping, including previously accepted phrases.
        if not mnemo.check(words):
            result = QMessageBox.question(self, 'Check recovery phrase',
                'This phrase fails the standard checksum. Restore it using the original wallet derivation anyway?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if result != QMessageBox.StandardButton.Yes:
                return
        addr, key = wallet_from_seed(mnemo.to_seed(words, passphrase=''))
        encrypted, salt = encrypt_pk(key.to_string().hex(), self.password)
        db.insert_wallet(addr, encrypted, salt)
        self.addmnemo.pk_label.clear()
        self.password = None
        self.accept()

    def reject(self):
        self.addmnemo.pk_label.clear()
        self.password = None
        super().reject()


class Mnemon(QDialog):
    def __init__(self, words, parent):
        super().__init__(parent)
        self.mnemon = phrase_controls(self)
        self.mnemon.mnemo_label.setText(words)
        self.mnemon.ok_btn.clicked.connect(self.accept)
        self.finished.connect(lambda result: self.mnemon.mnemo_label.clear())


class WartWallets(QMainWindow):
    def __init__(self):
        super().__init__()
        self.wallets = WalletsPanel()
        self.setCentralWidget(self.wallets)
        self.setMinimumSize(700, 480)
        self.resize(800, 560)
        self.setWindowIcon(QIcon(':/icon/icons/icon.ico'))
        self.setWindowTitle(f'Warthog Wallet {VERSION} — Wallets')
        self.backup_jobs = GuiJobs(self)
        toolbar = QToolBar('Wallet backups', self)
        toolbar.setStyleSheet(TOOLBAR_STYLE)
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, toolbar)
        self.backup_action = toolbar.addAction('Back up wallets', self.backup_wallets)
        self.restore_action = toolbar.addAction('Restore backup', self.restore_backup)
        self.import_action = toolbar.addAction('Import existing database', self.import_database)
        self.setToolTip('Use Back up wallets before moving to another computer or release. ' + BUILD_LABEL)
        self.wallets.new_wallet_button.clicked.connect(self.new_wallet)
        self.wallets.mnemonic_button.clicked.connect(self.restore_mnemo)
        self.wallets.private_key_button.clicked.connect(self.restore_pk)
        self.wallets.listWidget.itemClicked.connect(self.open_main_window)
        self.load_wallets()

    def backup_busy(self, busy):
        self.backup_action.setEnabled(not busy)
        self.restore_action.setEnabled(not busy)
        self.import_action.setEnabled(not busy)

    def backup_failed(self, exc):
        self.backup_busy(False)
        message(self, exc, 'Wallet backup')

    def backup_wallets(self):
        destination, _ = QFileDialog.getSaveFileName(self, 'Back up wallets', 'wallets.wartbackup',
                                                    'Encrypted wallet backup (*.wartbackup)')
        if not destination:
            return
        password, accepted = QInputDialog.getText(self, 'Backup password',
            'Choose a backup password (8+ characters). Existing wallet passwords will still be required.',
            QLineEdit.EchoMode.Password)
        if not accepted:
            return
        repeat, accepted = QInputDialog.getText(self, 'Confirm backup password',
                                               'Repeat the backup password:', QLineEdit.EchoMode.Password)
        if not accepted:
            return
        if password != repeat or not 8 <= len(password) <= 1024:
            message(self, 'Use matching backup passwords of 8 to 1024 characters.')
            return
        database_path = db.con.execute('PRAGMA database_list').fetchone()[2]
        from pathlib import Path
        if database_path and Path(destination).resolve() == Path(database_path).resolve():
            message(self, 'Choose a backup file different from the live database.')
            return
        snapshot = db.snapshot()
        self.backup_busy(True)

        def success(result):
            self.backup_busy(False)
            message(self, 'Encrypted backup saved. Keep both its password and your wallet passwords.', 'Wallet backup')

        self.backup_jobs.submit('backup', lambda cancel: save_backup(snapshot, password, destination),
                                success, self.backup_failed)

    def restore_backup(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Restore encrypted backup', '',
                                             'Encrypted wallet backup (*.wartbackup)')
        if not path:
            return
        password, accepted = QInputDialog.getText(self, 'Restore backup', 'Backup password:',
                                                 QLineEdit.EchoMode.Password)
        if not accepted:
            return
        self.backup_busy(True)

        self.backup_jobs.submit('backup', lambda cancel: load_backup(path, password), self.apply_snapshot, self.backup_failed)

    def apply_snapshot(self, snapshot):
        try:
            added = db.restore_snapshot(snapshot)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.backup_failed(exc)
            return
        self.backup_busy(False)
        self.load_wallets()
        message(self, f'Restored {added} wallet(s). Existing wallets were kept. Use their original passwords to unlock.',
                'Wallet backup')

    def import_database(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Import an existing wallet database (original is kept)', '',
                                             'Wallet database (*.db *.db3 *.sqlite);;All files (*)')
        if not path:
            return
        self.backup_busy(True)
        self.backup_jobs.submit('backup', lambda cancel: read_database_snapshot(path),
                                self.apply_snapshot, self.backup_failed)

    def closeEvent(self, event):
        self.backup_jobs.cancel_all()
        event.accept()

    def open_main_window(self, item):
        dialog = Login(self, item.text())
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if accepted:
            self.hide()
            mainWindow.show_wallet()

    def load_wallets(self):
        self.wallets.listWidget.clear()
        for wallet in db.get_wallets():
            self.wallets.listWidget.addItem(wallet[1])
            self.wallets.listWidget.item(self.wallets.listWidget.count() - 1).setToolTip(wallet[1])
        self.wallets.notice.setText('Select an address to unlock its wallet.' if self.wallets.listWidget.count()
                                  else 'No saved wallets yet. Create a wallet or add an existing one above.')

    def register_password(self):
        dialog = Register(self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            return dialog.take_password()
        finally:
            dialog.take_password()
            dialog.deleteLater()

    def restore_mnemo(self):
        password = self.register_password()
        if password is None:
            return
        dialog = MnemoRestore(self, password)
        dialog.exec()
        dialog.deleteLater()
        self.load_wallets()

    def restore_pk(self):
        password = self.register_password()
        if password is None:
            return
        dialog = PrivkeyRestore(self, password)
        dialog.exec()
        dialog.deleteLater()
        self.load_wallets()

    def new_wallet(self):
        password = self.register_password()
        if password is None:
            return
        words = mnemo.generate(strength=256)
        addr, key = wallet_from_seed(mnemo.to_seed(words, passphrase=''))
        encrypted, salt = encrypt_pk(key.to_string().hex(), password)
        db.insert_wallet(addr, encrypted, salt)
        dialog = Mnemon(words, self)
        dialog.exec()
        dialog.deleteLater()
        self.load_wallets()


class WartEntry(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ui = ConnectionPanel()
        self.setCentralWidget(self.ui)
        self.setMinimumSize(700, 520)
        self.resize(800, 560)
        self.setWindowIcon(QIcon(':/icon/icons/icon.ico'))
        self.setWindowTitle(f'Warthog Wallet {VERSION} — Connect to a network')
        self.jobs = GuiJobs(self)
        self.network = self.ui.network
        self.network.setCurrentIndex(max(0, self.network.findData(db.setting('selected_network', 'mainnet'))))
        self.profiles = self.ui.profiles
        self.ui.save_profile.clicked.connect(self.save_profile)
        self.ui.delete_profile.clicked.connect(self.delete_profile)
        self.ui.settings.clicked.connect(self.open_settings)
        self.network_hint = self.ui.network_hint
        self.profiles.currentIndexChanged.connect(self.choose_profile)
        self.network.currentIndexChanged.connect(self.change_network)
        self.load_profiles()
        self.ui.public_button.clicked.connect(self.public_connect)
        self.ui.connect_button.clicked.connect(self.private_connect)
        self.ui.input_field.returnPressed.connect(self.private_connect)

    def load_profiles(self, selected=None):
        network = self.network.currentData()
        selection_key = 'selected_node' if network == 'mainnet' else 'selected_node:testnet'
        initial = selected is None
        selected = selected or db.setting(selection_key, 'Local node' if network == 'mainnet' else 'Wartnode DeFi testnet')
        self.profiles.blockSignals(True)
        self.profiles.clear()
        for node in db.configured_nodes(network):
            if node['enabled']:
                self.profiles.addItem(node['name'], node['url'])
        index = self.profiles.findText(selected)
        if initial and (index < 0 or self.profiles.itemData(index) != db.get_peer(network)):
            index = next((i for i in range(self.profiles.count()) if self.profiles.itemData(i) == db.get_peer(network)), -1)
        self.profiles.setCurrentIndex(index)
        self.profiles.blockSignals(False)
        self.choose_profile()
        if initial:
            self.ui.input_field.setText(db.get_peer(network))
            self.ui.input_field.setCursorPosition(0)
        self.network_hint.setText('Test coins only' if network == 'testnet' else '')

    def change_network(self):
        self.jobs.cancel_all()
        self.connecting(False)
        self.load_profiles()

    def open_settings(self):
        dialog = SettingsDialog(db, self)
        dialog.page.network.setCurrentIndex(dialog.page.network.findData(self.network.currentData()))
        target = []
        dialog.page.profiles_changed.connect(lambda: self.load_profiles())
        active_window = globals().get('mainWindow')
        if active_window is not None:
            dialog.page.preferences_changed.connect(active_window.apply_preferences)
        def connect(peer, network):
            target[:] = [peer, network]
            dialog.accept()
        dialog.page.connect_requested.connect(connect)
        dialog.exec()
        dialog.deleteLater()
        if target:
            peer, network = target
            self.network.setCurrentIndex(self.network.findData(network))
            self.ui.input_field.setText(peer)
            self.private_connect()

    def choose_profile(self, index=None):
        url = self.profiles.currentData()
        if url:
            self.ui.input_field.setText(url)
            self.ui.input_field.setCursorPosition(0)

    def save_profile(self):
        name, accepted = QInputDialog.getText(self, 'Save node profile', 'Profile name:',
                                             QLineEdit.EchoMode.Normal, self.profiles.currentText())
        if not accepted:
            return
        try:
            db.save_node_profile(name, self.ui.input_field.text(), self.network.currentData())
        except (ValueError, NodeError) as exc:
            message(self, exc)
            return
        self.load_profiles(name.strip())
        db.set_setting('selected_node' if self.network.currentData() == 'mainnet' else 'selected_node:testnet', name.strip())

    def delete_profile(self):
        name = self.profiles.currentText()
        if name and QMessageBox.question(self, 'Delete node profile', f'Delete the saved profile “{name}”?') == QMessageBox.StandardButton.Yes:
            db.delete_node_profile(name)
            self.load_profiles()

    def connecting(self, busy):
        self.ui.connect_button.setEnabled(not busy)
        self.ui.public_button.setEnabled(not busy)
        self.ui.connect_button.setText('Connecting...' if busy else 'Connect')
        self.network.setEnabled(not busy)
        self.profiles.setEnabled(not busy)
        self.ui.input_field.setEnabled(not busy)
        self.ui.profile_actions.setEnabled(not busy)

    def connected(self, state):
        global PEER
        network = self.network.currentData()
        if state['network'] not in (network, 'unverified'):
            self.failed(NodeError('The node does not match the selected network.'))
            return
        PEER = state['peer']
        db.network = network
        db.set_setting('selected_network', network)
        db.update_peer(PEER, network)
        if self.profiles.currentData() == PEER:
            db.set_setting('selected_node' if network == 'mainnet' else 'selected_node:testnet', self.profiles.currentText())
        mainWindow.set_node_status(state)
        mainWindow.balance_snapshot = None
        self.connecting(False)
        wallets.show()
        self.hide()

    def failed(self, exc):
        self.connecting(False)
        message(self, exc)

    def public_connect(self):
        network = self.network.currentData()
        preferences = load_preferences(db)
        peers = TESTNET_NODES if network == 'testnet' else PUBLIC_NODES
        peers = list(dict.fromkeys(peers + db.node_candidates(network)))
        configured = db.configured_nodes(network)
        disabled = ({node['url'] for node in configured if not node['enabled']}
                    - {node['url'] for node in configured if node['enabled']})
        peers = [peer for peer in peers if peer not in disabled]
        self.connecting(True)

        def work(cancel):
            return probe_nodes(peers, network, cancel, lowest_fee=preferences['auto_lowest_fee'])

        self.jobs.submit('connect', work, self.connected, self.failed)

    def private_connect(self):
        try:
            peer = NodeClient.validate_peer(self.ui.input_field.text())
        except NodeError as exc:
            message(self, exc)
            return
        self.connecting(True)
        network = self.network.currentData()
        preferences = load_preferences(db)
        matches = [node for node in db.configured_nodes(network) if node['url'] == peer]
        if matches and not any(node['enabled'] for node in matches):
            self.failed(NodeError('This saved node is disabled. Enable it in Settings before connecting.'))
            return
        peers = (db.node_candidates(network, peer) if preferences['auto_lowest_fee'] or preferences['auto_failover'] else [peer])
        def work(cancel):
            if len(peers) == 1:
                return NodeClient(peers[0], cancel=cancel, network=network).probe()
            return probe_nodes(peers, network, cancel, lowest_fee=preferences['auto_lowest_fee'])
        self.jobs.submit('connect', work,
                         self.connected, self.failed)

    def closeEvent(self, event):
        self.jobs.cancel_all()
        event.accept()


def run():
    global db, window, wallets, mainWindow
    parser = argparse.ArgumentParser(description='Warthog mainnet and DeFi-testnet desktop wallet')
    parser.add_argument('--wallet-db', default=None,
                        help='Override the wallet database path (default: Windows local application data)')
    parser.add_argument('--test-profile', action='store_true',
                        help='Use a separate, stable application data folder for testing')
    parser.add_argument('--smoke-test', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    app.setOrganizationName('WarthogNetwork')
    app.setApplicationName('WartWallet-Test' if args.test_profile else 'WartWallet')
    app.setApplicationVersion(VERSION)
    apply_theme(app)
    try:
        path = prepare_wallet_path(args.wallet_db, legacy_name='wallet-test.db' if args.test_profile else 'wartwallet.db')
    except MigrationChoiceRequired as exc:
        selected, accepted = QInputDialog.getItem(None, 'Choose existing wallet database',
            'Multiple legacy databases found. Choose one to COPY into the application data folder:',
            [str(path) for path in exc.paths], 0, False)
        if not accepted:
            return 0
        copy_database(selected, default_wallet_path())
        path = str(default_wallet_path())
    db = DB(path)
    window = WartEntry()
    wallets = WartWallets()
    mainWindow = MainWindow(wallets)
    window.show()
    if args.smoke_test:
        from smoke_checks import verify_runtime
        verify_runtime()
        QTimer.singleShot(500, app.quit)
    result = app.exec()
    window.jobs.cancel_all()
    mainWindow.jobs.cancel_all()
    wallets.backup_jobs.cancel_all()
    mainWindow.jobs.pool.waitForDone(16000)
    db.con.close()
    return result


if __name__ == '__main__':
    sys.exit(run())

