from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
                               QTableWidget, QTableWidgetItem, QAbstractItemView,
                               QHeaderView, QApplication)

from history import display_date
from utils import format_e8
from theme import PANEL_STYLE


class HistoryPanel(QWidget):
    def __init__(self, refresh, older, check_selected):
        super().__init__()
        self.setStyleSheet(PANEL_STYLE)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        title = QLabel('Transactions')
        title.setProperty('pageTitle', True)
        layout.addWidget(title)
        bar = QHBoxLayout()
        self.refresh = QPushButton('Refresh')
        self.older = QPushButton('Load older')
        copy = QPushButton('Copy ID')
        copy.setToolTip('Copy the selected transaction ID')
        check = QPushButton('Check status')
        check.setToolTip('Look up the selected transaction on your node')
        self.refresh.clicked.connect(refresh)
        self.older.clicked.connect(older)
        copy.clicked.connect(self.copy_id)
        check.clicked.connect(check_selected)
        for widget in (self.refresh, self.older, check, copy):
            bar.addWidget(widget)
        layout.addLayout(bar)
        self.notice = QLabel('Payments for this wallet only. Dates are shown in UTC.')
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setProperty('muted', True)
        layout.addWidget(self.notice)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['Date (UTC)', 'Direction', 'WART', 'Status', 'Fee', 'Other address', 'Transaction ID'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setWordWrap(False)
        for column, width in enumerate((145, 80, 110, 135, 105, 180, 180)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table)

    def show_rows(self, records):
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            state = record['status'].replace('_', ' ').capitalize()
            if record['confirmations']:
                state += f" ({record['confirmations']})"
            elif record['status'] == 'not_found':
                state = 'Uncertain — not found on this node'
            values = [display_date(record['timestamp']).removesuffix(' UTC'), record['direction'].capitalize(),
                      format_e8(int(record['amount_e8'])), state, format_e8(int(record['fee_e8'])),
                      record['counterparty'] or 'Mining reward', record['tx_hash']]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 3 and record['confirmations']:
                    item.setToolTip(f"{record['confirmations']} block confirmations reported by your node")
                self.table.setItem(row, column, item)

    def copy_id(self):
        transaction = self.selected_id()
        if transaction:
            QApplication.clipboard().setText(transaction)

    def selected_id(self):
        row = self.table.currentRow()
        if row >= 0 and self.table.item(row, 6):
            return self.table.item(row, 6).text()
        return None
