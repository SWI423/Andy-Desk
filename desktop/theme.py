"""桌面层主题 desktop/theme.py —— 浅色、克制，接近纸质笔记本"""

STYLE = """
* { font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 13px; color: #1e293b; }
QMainWindow, QWidget { background: #f6f7f9; }
QLabel#h1 { font-size: 16px; font-weight: 600; color: #0f172a; }
QLabel#muted { color: #64748b; font-size: 12px; }
QLabel#toast {
    background: #0f766e; color: #ffffff; border-radius: 10px;
    padding: 12px 14px; font-size: 13px;
}
QTabWidget::pane { border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff; }
QTabBar::tab {
    background: transparent; padding: 7px 16px; margin-right: 2px;
    border-bottom: 2px solid transparent; color: #475569;
}
QTabBar::tab:selected { color: #0f766e; border-bottom: 2px solid #0f766e; font-weight: 600; }
QTableWidget {
    background: #ffffff; border: none; gridline-color: #f1f5f9;
    selection-background-color: #e6f4f1; selection-color: #0f172a;
}
QHeaderView::section {
    background: #f8fafc; border: none; border-bottom: 1px solid #e2e8f0;
    padding: 6px; color: #64748b; font-weight: 600;
}
QPushButton {
    background: #ffffff; border: 1px solid #d7dee8; border-radius: 7px;
    padding: 6px 14px; color: #334155;
}
QPushButton:hover { background: #f1f5f9; }
QPushButton:disabled { color: #94a3b8; background: #f8fafc; }
QPushButton#primary { background: #0f766e; border: 1px solid #0f766e; color: #ffffff; }
QPushButton#primary:hover { background: #115e59; }
QPushButton#danger { color: #b91c1c; border-color: #f3d2d2; }
QPushButton#danger:hover { background: #fef2f2; }
QPushButton#ghost { border: none; color: #0f766e; background: transparent; padding: 4px 8px; }
QPushButton#ghost:hover { background: #e6f4f1; }
QLineEdit, QTextEdit, QComboBox, QDateTimeEdit, QTimeEdit {
    background: #ffffff; border: 1px solid #d7dee8; border-radius: 7px;
    padding: 6px 10px;
}
QLineEdit:focus, QTextEdit:focus { border: 1px solid #0f766e; }
QCheckBox { color: #475569; }
QDialog { background: #f6f7f9; }
"""
