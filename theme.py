from PySide6.QtGui import QColor, QPalette, QFont

# Match the original charcoal pages, rounded outlines and quiet hover states.
CONTROL_STYLE = '''
QLabel { color: #eeeeee; background: transparent; }
QPushButton, QToolButton {
    background: transparent; color: #eeeeee; border: 1px solid #45494e;
    border-radius: 8px; padding: 7px 12px;
}
QPushButton:hover, QToolButton:hover { background: #303438; border-color: #72777c; }
QPushButton:pressed, QToolButton:pressed { background: #41464b; }
QPushButton:focus, QToolButton:focus { border-color: #a2a8ad; }
QPushButton:disabled, QToolButton:disabled { color: #858b90; border-color: #303438; }
QLineEdit, QComboBox {
    background: #1b1e22; color: #eeeeee; border: 1px solid #45494e;
    border-radius: 7px; padding: 8px; selection-background-color: #505960;
}
QLineEdit:focus, QComboBox:focus { border-color: #a2a8ad; }
QComboBox QAbstractItemView { background: #1b1e22; color: #eeeeee; selection-background-color: #384047; }
'''

PANEL_STYLE = CONTROL_STYLE + '''
QLabel[pageTitle="true"] { font-family: "Segoe UI Light"; font-size: 24px; padding: 0 0 5px 0; }
QLabel[muted="true"] { color: #b4b9be; }
QTableWidget { background: #16191d; alternate-background-color: #1c1f23;
    color: #eeeeee; border: 1px solid #383d42; border-radius: 7px;
    selection-background-color: #343b41; selection-color: white; }
QTableWidget::item { padding: 6px; border-bottom: 1px solid #2b3035; }
QHeaderView::section { background: #1c1f23; color: #c5c9cd;
    border: none; border-bottom: 1px solid #45494e; padding: 8px 5px; }
QScrollBar:horizontal { background: #16191d; height: 10px; border: none; margin: 0; }
QScrollBar:vertical { background: #16191d; width: 10px; border: none; margin: 0; }
QScrollBar::handle:horizontal { background: #454b51; min-width: 30px; border-radius: 5px; }
QScrollBar::handle:vertical { background: #454b51; min-height: 30px; border-radius: 5px; }
QScrollBar::handle:hover { background: #656d75; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QAbstractScrollArea::corner { background: #16191d; border: none; }
QTextBrowser { background: #16191d; color: #eeeeee; border: 1px solid #383d42;
    border-radius: 7px; padding: 16px; font-size: 15px; }
'''

TOOLBAR_STYLE = CONTROL_STYLE + '''
QToolBar { background: #16191d; border: none; padding: 7px; spacing: 8px; color: white; }
QToolBar QLabel { color: #aeb4ba; padding: 0 8px; }
'''

SIDEBAR_STYLE = '''
QPushButton {
    font-family: "Segoe UI"; font-size: 17px; font-weight: 400;
    text-align: left; color: #eeeeee; background: transparent;
    padding: 8px 10px; margin: 0; border: 1px solid transparent; border-radius: 8px;
}
QPushButton:hover { background: #303438; border: 1px solid #72777c; }
QPushButton:pressed { background: #41464b; border: 1px solid #72777c; }
QPushButton:focus { border: 1px solid #a2a8ad; }
'''


def hide_unused_header(ui):
    """The generated forms still contain an obsolete custom title-bar frame."""
    header = getattr(ui, 'headerContainer', None)
    if header is not None:
        header.hide()


def apply_theme(app):
    palette = QPalette()
    for role, color in [(QPalette.ColorRole.Window, '#16191d'), (QPalette.ColorRole.WindowText, '#eeeeee'),
                        (QPalette.ColorRole.Base, '#1b1e22'), (QPalette.ColorRole.AlternateBase, '#202428'),
                        (QPalette.ColorRole.Text, '#eeeeee'), (QPalette.ColorRole.Button, '#16191d'),
                        (QPalette.ColorRole.ButtonText, '#eeeeee'), (QPalette.ColorRole.Highlight, '#505960'),
                        (QPalette.ColorRole.HighlightedText, '#ffffff')]:
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor('#9aa4b2'))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor('#9aa4b2'))
    app.setPalette(palette)
    app.setFont(QFont('Segoe UI', 10))
