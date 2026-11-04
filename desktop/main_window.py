"""
桌宠主窗口 main_window.py
负责：大白形象显示、聊天面板、托盘图标

后续任务 M9 实现具体逻辑
"""

from PyQt6.QtWidgets import QMainWindow


class BaymaxWindow(QMainWindow):
    """大白桌宠主窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("大白")
        self.resize(400, 600)
        # TODO: M9 实现桌宠 UI

    def health_check(self) -> bool:
        """健康检查"""
        return True
