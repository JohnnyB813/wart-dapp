"""Compact, resizable wallet selection screen."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QListWidget, QAbstractItemView)
from theme import PANEL_STYLE
import res_rc


class WalletsPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(PANEL_STYLE + '''
            QListWidget { background: #16191d; color: #eeeeee; border: 1px solid #383d42;
                border-radius: 7px; selection-background-color: #343b41; selection-color: white; }
            QListWidget::item { padding: 9px 12px; border-bottom: 1px solid #2b3035; }
            QListWidget::item:hover { background: #252a2f; }
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)
        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setPixmap(QPixmap(':/icon/icons/logo4.png').scaledToWidth(260, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(logo)
        title = QLabel('Wallets')
        title.setProperty('pageTitle', True)
        layout.addWidget(title)
        self.notice = QLabel('Select an address to unlock its wallet.')
        self.notice.setProperty('muted', True)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        actions = QHBoxLayout()
        self.new_wallet_button = QPushButton('Create new wallet')
        self.mnemonic_button = QPushButton('Add by Mnemonic Phrase')
        self.private_key_button = QPushButton('Add by Private Key')
        for button in (self.new_wallet_button, self.mnemonic_button, self.private_key_button):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.listWidget = QListWidget()
        self.listWidget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.listWidget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.listWidget.setUniformItemSizes(True)
        layout.addWidget(self.listWidget, 1)
