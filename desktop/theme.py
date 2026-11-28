"""桌面层主题 desktop/theme.py —— 浅绿、留白、卡片化"""

STYLE = """
* { font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 13px; color: #244137; }
QMainWindow { background: #edf6f0; }
QWidget { background: transparent; }
QLabel { background: transparent; }
QWidget#root { background: #edf6f0; }
QToolTip { background: #173b2e; color: #ffffff; border: none; padding: 5px 8px; }

QFrame#card {
    background: #ffffff;
    border: 1px solid #dfece4;
    border-radius: 16px;
}
QFrame#stat {
    background: #f7fbf8;
    border: 1px solid #e2efe7;
    border-radius: 10px;
}
QLabel#statValue { font-size: 22px; font-weight: 700; color: #2c6e56; }
QLabel#statCaption { color: #7c9188; font-size: 12px; }
QLabel#logo {
    background: #2f7d62; color: #ffffff; border-radius: 18px;
    font-size: 20px; font-weight: 700;
}
QLabel#appName { font-size: 18px; font-weight: 700; color: #173b2e; }
QLabel#h1 { font-size: 16px; font-weight: 600; color: #173b2e; }
QLabel#muted { color: #71877c; font-size: 12px; }
QLabel#badge {
    background: #e6f5ec; color: #2f7d62; border: 1px solid #d5ebdf;
    border-radius: 10px; padding: 4px 10px; font-size: 12px;
}
QLabel#bubbleName { color: #2f7d62; font-weight: 700; font-size: 12px; }

QFrame#userBubble {
    background: #e7f5ed; border: 1px solid #d6eddf; border-radius: 14px;
}
QFrame#assistantBubble {
    background: #ffffff; border: 1px solid #dfeee5;
    border-left: 4px solid #62b394; border-radius: 14px;
}
QFrame#noticeBubble {
    background: #fff8e8; border: 1px solid #f0d9aa; border-radius: 12px;
}

QScrollArea#chatScroll { background: transparent; border: none; }
QWidget#chatBody { background: transparent; }

QTabWidget::pane {
    border: 1px solid #e2efe7; border-radius: 10px; background: #ffffff;
}
QTabBar::tab {
    background: transparent; padding: 7px 16px; margin-right: 2px;
    border-bottom: 2px solid transparent; color: #60766b;
}
QTabBar::tab:selected {
    color: #2f7d62; border-bottom: 2px solid #2f7d62; font-weight: 600;
}
QTableWidget {
    background: #ffffff; border: none; gridline-color: #eff6f1;
    selection-background-color: #e7f5ed; selection-color: #173b2e;
}
QHeaderView::section {
    background: #f7fbf8; border: none; border-bottom: 1px solid #e2efe7;
    padding: 6px; color: #71877c; font-weight: 600;
}

QPushButton {
    background: #ffffff; border: 1px solid #d7e6dd; border-radius: 8px;
    padding: 6px 14px; color: #335246;
}
QPushButton:hover { background: #f1faf5; }
QPushButton:disabled { color: #9aada3; background: #f7fbf8; }
QPushButton#primary {
    background: #2f7d62; border: 1px solid #2f7d62; color: #ffffff;
    border-radius: 9px; padding: 7px 14px;
}
QPushButton#primary:hover { background: #276b53; }
QPushButton#danger { color: #b4534f; border-color: #f0d4d1; }
QPushButton#danger:hover { background: #fff5f4; }
QPushButton#ghost {
    border: none; color: #2f7d62; background: transparent; padding: 4px 7px;
}
QPushButton#ghost:hover { background: #edf8f2; }
QPushButton#pill {
    background: #eff9f3; border: 1px solid #dcefe4; border-radius: 14px;
    padding: 5px 12px; color: #2f6b55;
}
QPushButton#pill:hover { background: #e4f5ec; }

QFrame#composer {
    background: #ffffff; border: 1px solid #d9eae0; border-radius: 12px;
}
QLineEdit#chatInput {
    border: none; background: transparent; padding: 6px 8px; color: #244137;
}
QLineEdit#chatInput:focus { border: none; }

QLineEdit, QTextEdit, QComboBox, QDateTimeEdit, QTimeEdit {
    background: #ffffff; border: 1px solid #d7e6dd; border-radius: 8px;
    padding: 6px 10px;
}
QLineEdit:focus, QTextEdit:focus { border: 1px solid #2f7d62; }
QCheckBox { color: #4d675a; }
QDialog { background: #f4faf6; }
QSplitter::handle { background: transparent; }
QLabel#toast {
    background: #2f7d62; color: #ffffff; border-radius: 10px;
    padding: 12px 14px; font-size: 13px;
}
"""
