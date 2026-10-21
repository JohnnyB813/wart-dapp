"""Address book controls using the same sizing and theme as the payment pages."""
from PySide6.QtCore import Qt
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
        self.remove = QPushButton('Remove contact')
        self.remove.setEnabled(False)
        self.table.itemSelectionChanged.connect(self.update_actions)
        bar.addWidget(self.copy)
        bar.addStretch()
        bar.addWidget(self.remove)
        layout.addLayout(bar)

    def set_contacts(self, contacts):
        self.table.clearContents()
        self.table.setRowCount(len(contacts))
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.copy.setEnabled(False)
        self.remove.setEnabled(False)
        for row, (contact_id, address, name) in enumerate(contacts):
            for column, value in enumerate((name, address)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setData(Qt.ItemDataRole.UserRole, contact_id)
                self.table.setItem(row, column, item)

    def selected_contact(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        name, address = self.table.item(row, 0), self.table.item(row, 1)
        if name is None or address is None:
            return None
        return dict(id=name.data(Qt.ItemDataRole.UserRole), name=name.text(), address=address.text())

    def update_actions(self):
        selected = self.selected_contact() is not None
        self.copy.setEnabled(selected)
        self.remove.setEnabled(selected)

    def copy_address(self):
        contact = self.selected_contact()
        if contact:
            QApplication.clipboard().setText(contact['address'])
