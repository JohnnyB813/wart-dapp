"""Payment controls and a local-only receiving-address QR renderer."""
import qrcode
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox)
from utils import validate_address, parse_e8, round_fee_e8, format_e8
from theme import PANEL_STYLE


def address_qr(address, scale=6):
    address = validate_address(address)
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    code.add_data(address)
    code.make(fit=True)
    matrix = code.get_matrix()
    image = QImage(len(matrix) * scale, len(matrix) * scale, QImage.Format.Format_RGB32)
    image.fill(QColor('white'))
    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor('black'))
    for y, row in enumerate(matrix):
        for x, filled in enumerate(row):
            if filled:
                painter.drawRect(x * scale, y * scale, scale, scale)
    painter.end()
    return image


class SendPanel(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.network = QLabel('Mainnet WART')
        layout.addWidget(self.network)
        form = QFormLayout()
        self.contacts = QComboBox()
        self.contacts.addItem('Choose a saved contact', None)
        self.recipient = QLineEdit()
        self.recipient.setPlaceholderText('48-character Warthog address')
        self.amount = QLineEdit()
        self.amount.setPlaceholderText('0.00000000')
        self.fee = QLineEdit()
        self.node_minimum = None
        self.fallback_fee = 9992
        self.use_minimum = QCheckBox('Use the connected node’s minimum fee')
        self.use_minimum.setChecked(True)
        self.fee_hint = QLabel('Fetching the node minimum fee…')
        self.fee_hint.setWordWrap(True)
        self.available = QLabel('Available balance: updating…')
        self.summary = QLabel('Enter an amount to see the final fee and total.')
        self.summary.setWordWrap(True)
        for title, widget in [('Contact', self.contacts), ('Recipient', self.recipient),
                               ('Amount (WART)', self.amount), ('Fee (WART)', self.fee)]:
            form.addRow(title, widget)
        layout.addLayout(form)
        layout.addWidget(self.use_minimum)
        layout.addWidget(self.fee_hint)
        layout.addWidget(self.available)
        layout.addWidget(self.summary)
        bar = QHBoxLayout()
        self.maximum = QPushButton('Send Max')
        self.send = QPushButton('Review payment')
        bar.addWidget(self.maximum)
        bar.addStretch()
        bar.addWidget(self.send)
        layout.addLayout(bar)
        layout.addStretch()
        self.contacts.currentIndexChanged.connect(self.select_contact)
        self.amount.textChanged.connect(self.update_total)
        self.fee.textChanged.connect(self.update_total)
        self.use_minimum.toggled.connect(self.apply_fee_mode)
        self.apply_fee_mode()
        self.setStyleSheet(PANEL_STYLE + 'QLabel { padding: 5px; }')

    def set_node_fee(self, value, error=''):
        self.node_minimum = value
        if value is not None:
            self.fee_hint.setText('Node minimum: ' + format_e8(value) + ' WART')
            if self.use_minimum.isChecked():
                self.fee.setText(format_e8(value))
        else:
            self.fee_hint.setText('Node minimum unavailable — fallback fee: ' + format_e8(self.fallback_fee)
                                  + ' WART. Turn off automatic fees to enter a manual fee.')
            self.fee_hint.setToolTip(error)
            if self.use_minimum.isChecked():
                self.fee.setText(format_e8(self.fallback_fee))

    def apply_fee_mode(self):
        automatic = self.use_minimum.isChecked()
        self.fee.setReadOnly(automatic)
        if automatic:
            self.fee.setText(format_e8(self.node_minimum if self.node_minimum is not None else self.fallback_fee))

    def set_contacts(self, contacts):
        self.contacts.blockSignals(True)
        self.contacts.clear()
        self.contacts.addItem('Choose a saved contact', None)
        for _, address, name in contacts:
            self.contacts.addItem(f'{name} — {address}', address)
        self.contacts.blockSignals(False)

    def select_contact(self, index):
        address = self.contacts.itemData(index)
        if address:
            self.recipient.setText(address)

    def update_total(self):
        try:
            fee = round_fee_e8(parse_e8(self.fee.text(), allow_zero=True))
            amount = parse_e8(self.amount.text())
            self.summary.setText(f'Final fee: {format_e8(fee)} WART\nTotal: {format_e8(amount + fee)} WART')
        except ValueError:
            self.summary.setText('Enter a positive amount and a valid fee (up to eight decimal places).')


class ReceivePanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(PANEL_STYLE)
        layout = QVBoxLayout(self)
        self.network = QLabel('Receiving address — mainnet')
        layout.addWidget(self.network)
        row = QHBoxLayout()
        self.address = QLineEdit()
        self.address.setReadOnly(True)
        self.address.setStyleSheet('QLineEdit { font-family: Consolas; font-size: 14px; }')
        self.copy = QPushButton('Copy')
        row.addWidget(self.address, 1)
        row.addWidget(self.copy)
        layout.addLayout(row)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.setMinimumHeight(275)
        layout.addWidget(self.qr)
        hint = QLabel('The QR code contains only this address. Verify the address before sharing it.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
