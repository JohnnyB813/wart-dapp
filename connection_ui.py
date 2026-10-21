"""Connection controls using the same layout and sizing as wallet selection."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
                               QLineEdit, QPushButton, QComboBox)
from theme import PANEL_STYLE
import res_rc


class ConnectionPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(PANEL_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setPixmap(QPixmap(':/icon/icons/logo4.png').scaledToWidth(260, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(logo)
        title = QLabel('Connect to a network')
        title.setProperty('pageTitle', True)
        layout.addWidget(title)
        self.network = QComboBox()
        self.network.addItem('Mainnet', 'mainnet')
        self.network.addItem('DeFi testnet', 'testnet')
        self.profiles = QComboBox()
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText('https://node.example or http://localhost:3000')
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.addRow('Network', self.network)
        form.addRow('Saved node', self.profiles)
        form.addRow('Node URL', self.input_field)
        layout.addLayout(form)
        self.network_hint = QLabel()
        self.network_hint.setWordWrap(True)
        self.network_hint.setProperty('muted', True)
        layout.addWidget(self.network_hint)
        self.profile_actions = QWidget()
        profiles_bar = QHBoxLayout(self.profile_actions)
        profiles_bar.setContentsMargins(0, 0, 0, 0)
        self.save_profile = QPushButton('Save profile')
        self.delete_profile = QPushButton('Delete profile')
        self.settings = QPushButton('Settings')
        for button in (self.save_profile, self.delete_profile, self.settings):
            profiles_bar.addWidget(button)
        profiles_bar.addStretch()
        layout.addWidget(self.profile_actions)
        actions = QHBoxLayout()
        self.public_button = QPushButton('Connect public node')
        self.connect_button = QPushButton('Connect')
        actions.addWidget(self.public_button)
        actions.addStretch()
        actions.addWidget(self.connect_button)
        layout.addLayout(actions)
        layout.addStretch()
