"""Shared layouts for compact password and wallet recovery dialogs."""
from types import SimpleNamespace
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QVBoxLayout, QFormLayout, QLabel, QLineEdit, QDialogButtonBox
from theme import PANEL_STYLE
import res_rc


def dialog_layout(dialog, title, width=440):
    dialog.setWindowTitle(title)
    dialog.setWindowIcon(QIcon(':/icon/icons/icon.ico'))
    dialog.setModal(True)
    dialog.setMinimumWidth(width)
    dialog.setStyleSheet(PANEL_STYLE + 'QDialog { background: #16191d; }')
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(12)
    return layout


def dialog_buttons(dialog, layout, title, cancel=True):
    options = QDialogButtonBox.StandardButton.Ok
    if cancel:
        options |= QDialogButtonBox.StandardButton.Cancel
    buttons = QDialogButtonBox(options)
    button = buttons.button(QDialogButtonBox.StandardButton.Ok)
    button.setText(title)
    button.setDefault(True)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return button


def password_controls(dialog, confirmation=False):
    layout = dialog_layout(dialog, 'Set wallet password' if confirmation else 'Unlock wallet')
    form = QFormLayout()
    form.setVerticalSpacing(10)
    ui = SimpleNamespace(textEdit=QLineEdit())
    ui.textEdit.setEchoMode(QLineEdit.EchoMode.Password)
    form.addRow('Password', ui.textEdit)
    if confirmation:
        ui.textEdit_2 = QLineEdit()
        ui.textEdit_2.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('Confirm password', ui.textEdit_2)
    layout.addLayout(form)
    ui.ok_btn = dialog_buttons(dialog, layout, 'Continue' if confirmation else 'Unlock')
    return ui


def recovery_controls(dialog, private_key=False):
    layout = dialog_layout(dialog, 'Add wallet by private key' if private_key else 'Add wallet by recovery phrase', 560)
    label = QLabel('Enter the wallet’s private key.' if private_key else 'Enter the complete original recovery phrase.')
    label.setWordWrap(True)
    layout.addWidget(label)
    field = QLineEdit()
    field.setMaxLength(256)
    field.setPlaceholderText('64-character hexadecimal private key' if private_key else 'Original mnemonic phrase')
    layout.addWidget(field)
    return SimpleNamespace(pk_label=field, ok_btn=dialog_buttons(dialog, layout, 'Add wallet'))


def phrase_controls(dialog):
    layout = dialog_layout(dialog, 'Save recovery phrase', 560)
    notice = QLabel('Write down this recovery phrase and keep it somewhere private.')
    notice.setWordWrap(True)
    layout.addWidget(notice)
    phrase = QLabel()
    phrase.setTextFormat(Qt.TextFormat.PlainText)
    phrase.setWordWrap(True)
    phrase.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    phrase.setStyleSheet('QLabel { background: #1b1e22; border: 1px solid #45494e; border-radius: 7px; padding: 12px; }')
    layout.addWidget(phrase)
    return SimpleNamespace(mnemo_label=phrase, ok_btn=dialog_buttons(dialog, layout, 'Done', cancel=False))
