"""Node management and wallet preferences, shared by the wallet and entry screen."""
import sqlite3
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
                               QLineEdit, QComboBox, QCheckBox, QSpinBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QAbstractItemView,
                               QHeaderView, QTabWidget, QDialog, QDialogButtonBox)
from gui_jobs import GuiJobs
from node_client import NodeClient, NodeError
from theme import PANEL_STYLE
from utils import format_e8, parse_e8, round_fee_e8
from wallet_settings import load_preferences, save_preferences


class SettingsPage(QWidget):
    profiles_changed = Signal()
    preferences_changed = Signal()
    connect_requested = Signal(str, str)

    def __init__(self, store):
        super().__init__()
        self.store, self.previous_name, self.health, self.fees = store, None, {}, {}
        self.jobs = GuiJobs(self)
        self.setStyleSheet(PANEL_STYLE + '''
            QTabWidget::pane { border: 1px solid #383d42; border-radius: 7px; }
            QTabBar::tab { background: #1b1e22; color: #eeeeee; padding: 8px 18px; }
            QTabBar::tab:selected { background: #343b41; }
            QCheckBox { color: #eeeeee; padding: 4px 0; }
            QSpinBox { background: #1b1e22; color: #eeeeee; padding: 7px; border: 1px solid #45494e; }
        ''')
        layout = QVBoxLayout(self)
        title = QLabel('Settings')
        title.setProperty('pageTitle', True)
        layout.addWidget(title)
        self.active_node = QLabel('Connect to a node to see the active connection.')
        self.active_node.setWordWrap(True)
        self.active_node.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.active_node)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        nodes_tab, preferences_tab = QWidget(), QWidget()
        self.tabs.addTab(nodes_tab, 'Nodes')
        self.tabs.addTab(preferences_tab, 'Preferences')
        nodes_layout = QVBoxLayout(nodes_tab)
        bar = QHBoxLayout()
        bar.addWidget(QLabel('Network'))
        self.network = QComboBox()
        self.network.addItem('Mainnet', 'mainnet')
        self.network.addItem('DeFi testnet', 'testnet')
        self.network.setCurrentIndex(1 if store.network == 'testnet' else 0)
        bar.addWidget(self.network)
        bar.addStretch()
        self.new = QPushButton('New')
        self.up, self.down = QPushButton('Up'), QPushButton('Down')
        for button in (self.new, self.up, self.down):
            bar.addWidget(button)
        nodes_layout.addLayout(bar)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Name / order', 'Host / port', 'Enabled', 'Min fee (WART)', 'Health'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 115)
        self.table.setMinimumHeight(135)
        self.table.setAlternatingRowColors(True)
        nodes_layout.addWidget(self.table, 1)
        form = QFormLayout()
        name_row = QHBoxLayout()
        self.name, self.url = QLineEdit(), QLineEdit()
        self.name.setMaxLength(64)
        self.url.setMaxLength(2048)
        self.url.setPlaceholderText('https://node.example or http://192.168.1.50:3000')
        self.enabled = QCheckBox('Enabled')
        name_row.addWidget(self.name, 1)
        name_row.addWidget(self.enabled)
        form.addRow('Name', name_row)
        form.addRow('URL', self.url)
        nodes_layout.addLayout(form)
        actions = QHBoxLayout()
        self.save, self.remove = QPushButton('Save node'), QPushButton('Remove')
        self.test, self.connect = QPushButton('Test node'), QPushButton('Connect')
        for button in (self.save, self.remove, self.test, self.connect):
            actions.addWidget(button)
        nodes_layout.addLayout(actions)
        self.notice = QLabel('Automatic selection chooses the lowest reported fee on this network; list order breaks ties.')
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        nodes_layout.addWidget(self.notice)
        self.network.currentIndexChanged.connect(lambda: self.reload())
        self.table.itemSelectionChanged.connect(self.choose_node)
        self.new.clicked.connect(self.new_node)
        self.save.clicked.connect(self.save_node)
        self.remove.clicked.connect(self.remove_node)
        self.up.clicked.connect(lambda: self.move_node(-1))
        self.down.clicked.connect(lambda: self.move_node(1))
        self.test.clicked.connect(self.test_node)
        self.connect.clicked.connect(self.connect_node)
        prefs_layout = QVBoxLayout(preferences_tab)
        preferences = load_preferences(store)
        self.auto_minfee = QCheckBox('Use the node minimum fee automatically by default')
        self.auto_minfee.setChecked(preferences['auto_minfee'])
        self.auto_failover = QCheckBox('Automatically switch to an available node if disconnected')
        self.auto_failover.setChecked(preferences['auto_failover'])
        self.auto_lowest_fee = QCheckBox('Automatically select the synced node with the lowest reported fee')
        self.auto_lowest_fee.setChecked(preferences['auto_lowest_fee'])
        prefs_layout.addWidget(self.auto_minfee)
        prefs_layout.addWidget(self.auto_lowest_fee)
        prefs_layout.addWidget(self.auto_failover)
        explanation = QLabel('Only enabled nodes on the same network are considered. Unknown fees are not ranked as zero. '
                             'Payments are sent once to the node shown during review. '
                             'You can choose a manual fee for an individual payment on Send.')
        explanation.setWordWrap(True)
        prefs_layout.addWidget(explanation)
        preferences_form = QFormLayout()
        self.auto_lock = QSpinBox()
        self.auto_lock.setRange(1, 60)
        self.auto_lock.setSuffix(' minutes')
        self.auto_lock.setValue(preferences['auto_lock_minutes'])
        self.refresh = QSpinBox()
        self.refresh.setRange(10, 300)
        self.refresh.setSuffix(' seconds')
        self.refresh.setValue(preferences['refresh_seconds'])
        preferences_form.addRow('Auto-lock after inactivity', self.auto_lock)
        preferences_form.addRow('Refresh balances and node status', self.refresh)
        self.fallback_fee = QLineEdit(format_e8(preferences['fallback_fee_e8']))
        preferences_form.addRow('Fallback fee for older nodes (WART)', self.fallback_fee)
        prefs_layout.addLayout(preferences_form)
        self.apply = QPushButton('Save preferences')
        prefs_layout.addWidget(self.apply)
        self.preference_notice = QLabel('Preferences apply immediately after saving.')
        self.preference_notice.setWordWrap(True)
        prefs_layout.addWidget(self.preference_notice)
        prefs_layout.addStretch()
        self.apply.clicked.connect(self.save_preferences)
        self.reload()

    def selected(self):
        index = self.table.currentRow()
        return self.nodes[index] if 0 <= index < len(self.nodes) else None

    def reload(self, selected=None):
        self.nodes = self.store.configured_nodes(self.network.currentData())
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.nodes))
        for i, node in enumerate(self.nodes):
            values = [node['name'], node['url'].split('://', 1)[-1], 'Yes' if node['enabled'] else 'No',
                      self.fees.get((node['url'], node['network']), 'Unknown'),
                      self.health.get((node['url'], node['network']), 'Not tested')]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(node['url'] if column == 1 else value)
                self.table.setItem(i, column, item)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(False)
        if selected and any(node['name'] == selected for node in self.nodes):
            self.table.selectRow(next(i for i, node in enumerate(self.nodes) if node['name'] == selected))
        else:
            self.new_node()

    def record_checks(self, checks):
        for state in checks:
            key = state['peer'], state['network']
            if 'error' in state:
                self.health[key], self.fees[key] = 'Unavailable', 'Unknown'
            else:
                self.health[key] = ('Synced' if state['synced'] else 'Syncing') + ' · ' + str(state['height'])
                self.fees[key] = format_e8(state['min_fee_e8']) if 'min_fee_e8' in state else 'Unknown'
        # Update cells in place so background checks preserve unsaved edits.
        for i, node in enumerate(self.nodes):
            key = node['url'], node['network']
            for column, value in [(3, self.fees.get(key, 'Unknown')), (4, self.health.get(key, 'Not tested'))]:
                self.table.item(i, column).setText(value)
                self.table.item(i, column).setToolTip(value)

    def reload_preferences(self):
        values = load_preferences(self.store)
        self.auto_minfee.setChecked(values['auto_minfee'])
        self.auto_lowest_fee.setChecked(values['auto_lowest_fee'])
        self.auto_failover.setChecked(values['auto_failover'])
        self.auto_lock.setValue(values['auto_lock_minutes'])
        self.refresh.setValue(values['refresh_seconds'])
        self.fallback_fee.setText(format_e8(values['fallback_fee_e8']))

    def new_node(self):
        self.previous_name = None
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.name.clear()
        self.url.clear()
        self.enabled.setChecked(True)

    def choose_node(self):
        node = self.selected()
        if node:
            self.previous_name = node['name']
            self.name.setText(node['name'])
            self.url.setText(node['url'])
            self.url.setCursorPosition(0)
            self.enabled.setChecked(bool(node['enabled']))

    def save_node(self):
        try:
            if self.previous_name is None and any(name == self.name.text().strip() for name, _, _ in self.store.node_profiles()):
                raise ValueError('A node with that name already exists. Select it to edit it.')
            self.store.save_node_profile(self.name.text(), self.url.text(), self.network.currentData(),
                                         self.enabled.isChecked(), self.previous_name)
        except (ValueError, NodeError, sqlite3.Error) as exc:
            self.notice.setText(str(exc))
            return
        self.reload(self.name.text().strip())
        self.notice.setText('Node saved. Connect to use it, or enable it as a fallback.')
        self.profiles_changed.emit()

    def remove_node(self):
        node = self.selected()
        if node:
            self.store.delete_node_profile(node['name'])
            self.reload()
            self.notice.setText('Saved node removed. The active connection continues until you switch or disconnect.')
            self.profiles_changed.emit()

    def move_node(self, offset):
        node = self.selected()
        if node:
            self.store.move_node_profile(node['name'], offset)
            self.reload(node['name'])
            self.profiles_changed.emit()

    def test_node(self):
        node = self.selected()
        if not node or self.jobs.running('probe'):
            self.notice.setText('Select a saved node to test.')
            return
        self.test.setEnabled(False)
        self.notice.setText('Checking the selected node…')
        def success(state):
            self.test.setEnabled(True)
            health = ('Synced' if state['synced'] else 'Syncing') + ' · ' + str(state['height'])
            self.record_checks([state])
            self.reload(node['name'])
            fee = format_e8(state['min_fee_e8']) + ' WART' if 'min_fee_e8' in state else 'unavailable'
            self.notice.setText(f"{node['name']}: {health}. Minimum fee: {fee}.")
        def failure(exc):
            self.test.setEnabled(True)
            self.health[node['url'], node['network']] = 'Unavailable'
            self.reload(node['name'])
            self.notice.setText(str(exc))
        self.jobs.submit('probe', lambda cancel: NodeClient(node['url'], network=node['network'], cancel=cancel).probe(),
                         success, failure)

    def connect_node(self):
        node = self.selected()
        if not node:
            self.notice.setText('Select a saved node to connect.')
        elif not node['enabled']:
            self.notice.setText('Enable and save this node before connecting.')
        else:
            self.connect_requested.emit(node['url'], node['network'])

    def save_preferences(self):
        try:
            save_preferences(self.store, dict(auto_minfee=self.auto_minfee.isChecked(), auto_failover=self.auto_failover.isChecked(),
                                              auto_lowest_fee=self.auto_lowest_fee.isChecked(),
                                              auto_lock_minutes=self.auto_lock.value(), refresh_seconds=self.refresh.value(),
                                              fallback_fee_e8=round_fee_e8(parse_e8(self.fallback_fee.text(), allow_zero=True))))
        except (ValueError, sqlite3.Error) as exc:
            self.preference_notice.setText(str(exc))
            return
        self.preference_notice.setText('Preferences saved.')
        self.preferences_changed.emit()


class SettingsDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Wallet settings')
        self.resize(760, 650)
        layout = QVBoxLayout(self)
        self.page = SettingsPage(store)
        layout.addWidget(self.page)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.finished.connect(lambda: self.page.jobs.cancel_all())
