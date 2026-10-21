"""Address book controls using the same sizing and theme as the payment pages."""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QAbstractItemView, QHeaderView, QApplication)
from theme import PANEL_STYLE


class ContactsPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(PANEL_STYLE)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        title = QLabel('Address Book')
        title.setProperty('pageTitle', True)
        layout.addWidget(title)
        form = QFormLayout()
        self.name, self.address = QLineEdit(), QLineEdit()
        self.name.setMaxLength(64)
        self.name.setPlaceholderText('Contact name')
        self.address.setPlaceholderText('48-character Warthog address')
        form.addRow('Name', self.name)
        form.addRow('Address', self.address)
        layout.addLayout(form)
        actions = QHBoxLayout()
        actions.addStretch()
        self.add = QPushButton('Add contact')
        actions.addWidget(self.add)
        layout.addLayout(actions)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(['Name', 'Address'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 125)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        bar = QHBoxLayout()
        self.copy = QPushButton('Copy address')
        self.copy.setEnabled(False)
        self.copy.clicked.connect(self.copy_address)
        self.table.itemSelectionChanged.connect(lambda: self.copy.setEnabled(self.table.currentRow() >= 0))
        bar.addWidget(self.copy)
        bar.addStretch()
        layout.addLayout(bar)

    def set_contacts(self, contacts):
        self.table.clearContents()
        self.table.setRowCount(len(contacts))
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.copy.setEnabled(False)
        for row, (_, address, name) in enumerate(contacts):
            for column, value in enumerate((name, address)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)

    def copy_address(self):
        row = self.table.currentRow()
        if row >= 0 and self.table.item(row, 1):
            QApplication.clipboard().setText(self.table.item(row, 1).text())
