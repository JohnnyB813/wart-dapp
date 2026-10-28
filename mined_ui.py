from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QAbstractItemView,
                               QHeaderView, QApplication)

from history import display_date
from theme import PANEL_STYLE
from utils import format_e8


class MinedBlocksPanel(QWidget):
    block_selected = Signal(object)

    def __init__(self, refresh, older):
        super().__init__()
        self.setStyleSheet(PANEL_STYLE)
        self.blocks = []
        self.hash_notice = ''
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        title = QLabel('My mined blocks')
        title.setProperty('pageTitle', True)
        layout.addWidget(title)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        bar = QHBoxLayout()
        self.refresh = QPushButton('Refresh')
        self.older = QPushButton('Load older')
        self.copy = QPushButton('Copy block hash')
        self.refresh.clicked.connect(refresh)
        self.older.clicked.connect(older)
        self.copy.clicked.connect(self.copy_hash)
        bar.addWidget(self.refresh)
        bar.addWidget(self.older)
        bar.addStretch()
        bar.addWidget(self.copy)
        layout.addLayout(bar)
        self.notice = QLabel('Rewards paid to the loaded address only. Dates are shown in UTC.')
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setProperty('muted', True)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Block', 'Date (UTC)', 'Reward (WART)', 'Confirmations'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((82, 155, 135, 125)):
            self.table.setColumnWidth(column, width)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table, 1)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setFont(QFont('Consolas', 9))
        self.details.setWordWrap(True)
        self.details.setMinimumHeight(85)
        layout.addWidget(self.details)
        self.show_blocks([])

    def show_blocks(self, blocks):
        selected = self.table.currentRow()
        selected_height = self.blocks[selected]['height'] if 0 <= selected < len(self.blocks) else None
        self.table.blockSignals(True)
        self.blocks = blocks
        total = sum(block['reward_e8'] for block in blocks)
        count = len(blocks)
        self.summary.setText(f'{count} {"block" if count == 1 else "blocks"} · {format_e8(total)} WART in loaded history')
        self.table.setRowCount(len(blocks))
        for row, block in enumerate(blocks):
            values = [str(block['height']), display_date(block['timestamp']).removesuffix(' UTC'),
                      format_e8(block['reward_e8']), str(block['confirmations'])]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        if blocks:
            selected = next((i for i, block in enumerate(blocks) if block['height'] == selected_height), 0)
            self.table.selectRow(selected)
        self.table.blockSignals(False)
        self.selection_changed()

    def selection_changed(self):
        self.hash_notice = ''
        self.show_selected()
        row = self.table.currentRow()
        self.block_selected.emit(self.blocks[row] if 0 <= row < len(self.blocks) else None)

    def show_selected(self):
        row = self.table.currentRow()
        block = self.blocks[row] if 0 <= row < len(self.blocks) else None
        self.copy.setEnabled(bool(block and block['block_hash']))
        if block:
            block_hash = block['block_hash'] or self.hash_notice or 'Refresh to load the block hash from your node.'
            self.details.setText('Block hash\n' + block_hash + '\nReward transaction ID\n' + '\n'.join(block['reward_ids']))
        else:
            self.details.setText('No mined blocks in loaded history. Load older to check earlier rewards.')

    def copy_hash(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.blocks) and self.blocks[row]['block_hash']:
            QApplication.clipboard().setText(self.blocks[row]['block_hash'])
