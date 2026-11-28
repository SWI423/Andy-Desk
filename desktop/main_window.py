"""
桌面层 main_window.py
- 左：三线看板（学习/工作/生活）+ 提醒页
- 右：对话 + 工具调用
- 状态栏：今日概览
扩展性：任务/提醒页只依赖 core 层的公开函数；新增能力只需在 core/tools.py 注册工具
"""

import json
import threading
from datetime import datetime

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer, QSettings
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTabWidget,
    QTableWidget, QTableWidgetItem, QPushButton, QLineEdit,
    QLabel, QDialog, QFormLayout, QComboBox, QDateTimeEdit, QTimeEdit,
    QCheckBox, QTextEdit, QMessageBox, QAbstractItemView, QHeaderView,
    QStackedWidget, QApplication, QDialogButtonBox, QFrame, QScrollArea,
    QSizePolicy, QGraphicsDropShadowEffect,
)

from config import APP_NAME, DOMAINS, ensure_dirs
from core import db, tasks, scheduler, voice, asr
from core.agent_runtime import AgentRuntime


# ============ 后台线程 ============

class ChatWorker(QThread):
    """一次对话在子线程里跑，Agent 事件通过信号回主线程"""
    event = pyqtSignal(dict)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.text = text
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            AgentRuntime(on_event=self.event.emit).run(
                self.text, should_stop=self._stop.is_set)
        except Exception as e:
            self.event.emit({"type": "error", "kind": type(e).__name__,
                             "message": str(e)})
            self.event.emit({"type": "done", "reply": "", "tools_called": [],
                             "tool_results": [], "error": str(e),
                             "cancelled": False})


class ASRWorker(QThread):
    """麦克风录音 + Vosk 识别放到子线程，避免卡住窗口。"""
    partial = pyqtSignal(str)
    done = pyqtSignal(str)
    error = pyqtSignal(str)
    state = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            self.state.emit("loading")
            asr.load_model()  # 先加载模型，界面能显示“准备中”
            self.state.emit("listening")
            text = asr.record_until(
                self._stop,
                on_partial=lambda s: self.partial.emit(s),
            )
            self.done.emit(text)
        except asr.AsrError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(f"语音识别失败：{e}")


class SchedulerBridge(QObject):
    fired = pyqtSignal(dict)


class MCPConfirmBridge(QObject):
    """后台对话线程 -> 主线程弹确认框：跨线程不能直接开 QMessageBox"""
    ask = pyqtSignal(str, str, dict, object, object)


# ============ 任务编辑对话框 ============

class TaskDialog(QDialog):
    def __init__(self, task: dict = None, default_domain: str = "life", parent=None):
        super().__init__(parent)
        self.task = task
        self.setWindowTitle("编辑任务" if task else "新建任务")
        self.setMinimumWidth(420)

        form = QFormLayout(self)
        self.title_edit = QLineEdit(task["title"] if task else "")
        form.addRow("任务", self.title_edit)

        self.domain_box = QComboBox()
        for k, v in DOMAINS.items():
            self.domain_box.addItem(v, k)
        idx = list(DOMAINS).index(default_domain) if default_domain in DOMAINS else 2
        self.domain_box.setCurrentIndex(idx if not task else list(DOMAINS).index(task["domain"]))
        form.addRow("领域", self.domain_box)

        self.priority_box = QComboBox()
        for p, text in ((1, "高"), (2, "中"), (3, "低")):
            self.priority_box.addItem(text, p)
        if task:
            self.priority_box.setCurrentIndex({1: 0, 2: 1, 3: 2}.get(task["priority"], 1))
        form.addRow("优先级", self.priority_box)

        self.due_check = QCheckBox("设置截止时间")
        self.due_edit = QDateTimeEdit()
        self.due_edit.setCalendarPopup(True)
        self.due_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        default_due = datetime.now().replace(second=0, microsecond=0)
        if task and task["due_at"]:
            try:
                dt = datetime.strptime(task["due_at"], "%Y-%m-%d %H:%M")
            except ValueError:
                dt = datetime.strptime(task["due_at"], "%Y-%m-%d")
            self.due_edit.setDateTime(dt)
            self.due_check.setChecked(True)
        else:
            self.due_edit.setDateTime(default_due)
        due_row = QHBoxLayout()
        due_row.addWidget(self.due_check)
        due_row.addWidget(self.due_edit, 1)
        form.addRow("截止", due_row)
        self.due_edit.setEnabled(bool(task and task["due_at"]))

        self.notes_edit = QTextEdit(task["notes"] or "" if task else "")
        self.notes_edit.setFixedHeight(70)
        form.addRow("备注", self.notes_edit)

        btns = QHBoxLayout()
        btns.addStretch(1)
        ok = QPushButton("保存")
        ok.setObjectName("primary")
        cancel = QPushButton("取消")
        ok.clicked.connect(self._on_ok)
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        form.addRow(btns)

        self.due_check.toggled.connect(self.due_edit.setEnabled)

    def _on_ok(self):
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "提示", "任务标题不能为空")
            return
        self.accept()

    def values(self) -> dict:
        due = None
        if self.due_check.isChecked():
            due = self.due_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        return {
            "title": self.title_edit.text().strip(),
            "domain": self.domain_box.currentData(),
            "priority": self.priority_box.currentData(),
            "due_at": due,
            "notes": self.notes_edit.toPlainText().strip(),
        }


# ============ 提醒编辑对话框 ============

class ReminderDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建提醒")
        self.setMinimumWidth(420)
        form = QFormLayout(self)

        self.content_edit = QLineEdit()
        self.content_edit.setPlaceholderText("要提醒什么")
        form.addRow("内容", self.content_edit)

        self.type_box = QComboBox()
        self.type_box.addItem("一次性", "once")
        self.type_box.addItem("每天", "daily")
        self.type_box.addItem("每周", "weekly")
        form.addRow("重复", self.type_box)

        self.stack = QStackedWidget()
        self.once_edit = QDateTimeEdit()
        self.once_edit.setCalendarPopup(True)
        self.once_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.once_edit.setDateTime(datetime.now().replace(second=0, microsecond=0))
        self.stack.addWidget(self.once_edit)

        self.daily_edit = QTimeEdit()
        self.daily_edit.setDisplayFormat("HH:mm")
        self.stack.addWidget(self.daily_edit)

        weekly = QWidget()
        wl = QHBoxLayout(weekly)
        wl.setContentsMargins(0, 0, 0, 0)
        self.week_box = QComboBox()
        for i, t in enumerate(["周一", "周二", "周三", "周四", "周五", "周六", "周日"], start=1):
            self.week_box.addItem(t, i)
        self.week_time = QTimeEdit()
        self.week_time.setDisplayFormat("HH:mm")
        wl.addWidget(self.week_box)
        wl.addWidget(self.week_time, 1)
        self.stack.addWidget(weekly)

        form.addRow("时间", self.stack)
        self.type_box.currentIndexChanged.connect(self.stack.setCurrentIndex)

        btns = QHBoxLayout()
        btns.addStretch(1)
        ok = QPushButton("保存")
        ok.setObjectName("primary")
        cancel = QPushButton("取消")
        ok.clicked.connect(self._on_ok)
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        form.addRow(btns)

    def _on_ok(self):
        if not self.content_edit.text().strip():
            QMessageBox.warning(self, "提示", "提醒内容不能为空")
            return
        self.accept()

    def values(self) -> dict:
        rt = self.type_box.currentData()
        if rt == "once":
            expr = self.once_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        elif rt == "daily":
            expr = self.daily_edit.time().toString("HH:mm")
        else:
            expr = f"{self.week_box.currentData()} {self.week_time.time().toString('HH:mm')}"
        return {"content": self.content_edit.text().strip(), "rule_type": rt, "rule_expr": expr}


# ============ 单领域任务页 ============

class TaskPage(QWidget):
    changed = pyqtSignal()

    def __init__(self, domain: str, parent=None):
        super().__init__(parent)
        self.domain = domain
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        self.count_label = QLabel("0 项")
        self.count_label.setObjectName("muted")
        lay.addWidget(self.count_label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["任务", "截止", "进度", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 56)
        self.table.setColumnWidth(3, 64)
        self.table.doubleClicked.connect(lambda _: self.edit_task())
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        for text, slot, name in (
            ("新建", self.new_task, "primary"),
            ("开始", lambda: self.set_status("doing"), ""),
            ("完成", lambda: self.set_status("done"), ""),
            ("编辑", self.edit_task, ""),
            ("删除", self.delete_task, "danger"),
        ):
            b = QPushButton(text)
            if name:
                b.setObjectName(name)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)

        self.refresh()

    # ---- 数据 ----
    def refresh(self):
        show_done = getattr(self, "_show_done", False)
        rows = tasks.list_tasks(domain=self.domain, include_done=show_done)
        self.table.setRowCount(len(rows))
        today = datetime.now().strftime("%Y-%m-%d")
        for i, t in enumerate(rows):
            title_item = QTableWidgetItem(t["title"])
            title_item.setData(Qt.ItemDataRole.UserRole, t["id"])
            if t["priority"] == 1:
                title_item.setForeground(QColor("#b91c1c"))
            if t["due_at"] and t["due_at"][:10] < today and t["status"] != "done":
                title_item.setForeground(QColor("#b45309"))
            self.table.setItem(i, 0, title_item)
            self.table.setItem(i, 1, QTableWidgetItem(t["due_at"] or "—"))
            self.table.setItem(i, 2, QTableWidgetItem(f"{t['progress']}%"))
            self.table.setItem(i, 3, QTableWidgetItem(t["status_text"]))
        total = len(tasks.list_tasks(domain=self.domain, include_done=True))
        done = len([t for t in tasks.list_tasks(domain=self.domain, include_done=True)
                    if t["status"] == "done"])
        open_n = len(tasks.list_tasks(domain=self.domain, include_done=False))
        self.count_label.setText(f"未完成 {open_n} 项 · 累计 {total} 项（完成 {done}）")

    def current_id(self):
        r = self.table.currentRow()
        if r < 0:
            return None
        item = self.table.item(r, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # ---- 动作 ----
    def new_task(self):
        dlg = TaskDialog(default_domain=self.domain, parent=self)
        if dlg.exec():
            tasks.create_task(**dlg.values(), source="manual")
            self.refresh()
            self.changed.emit()

    def edit_task(self):
        tid = self.current_id()
        if not tid:
            return
        task = tasks.get_task(tid)
        if not task:
            return
        dlg = TaskDialog(task=task, parent=self)
        if dlg.exec():
            v = dlg.values()
            tasks.update_task(tid, title=v["title"], priority=v["priority"],
                              due_at=v["due_at"], notes=v["notes"], domain=v["domain"])
            self.refresh()
            self.changed.emit()

    def set_status(self, status: str):
        tid = self.current_id()
        if not tid:
            QMessageBox.information(self, "提示", "先选中一条任务")
            return
        tasks.update_task(tid, status=status)
        self.refresh()
        self.changed.emit()

    def delete_task(self):
        tid = self.current_id()
        if not tid:
            return
        t = tasks.get_task(tid)
        if QMessageBox.question(self, "删除任务", f"确定删除「{t['title']}」？") == QMessageBox.StandardButton.Yes:
            tasks.delete_task(tid)
            self.refresh()
            self.changed.emit()

    def toggle_done(self, show: bool):
        self._show_done = show
        self.refresh()


# ============ 提醒页 ============

class ReminderPage(QWidget):
    changed = pyqtSignal()

    def __init__(self, sched: scheduler.Scheduler, parent=None):
        super().__init__(parent)
        self.sched = sched
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        self.hint = QLabel("提醒由后台心跳每 30 秒检查一次；程序关着的时候不会响。")
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

        self.list = QTableWidget(0, 3)
        self.list.setHorizontalHeaderLabels(["内容", "规则", "下次"])
        self.list.verticalHeader().setVisible(False)
        self.list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.list.setColumnWidth(1, 100)
        self.list.setColumnWidth(2, 130)
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        for text, slot, name in (
            ("新建", self.new_reminder, "primary"),
            ("启用/停用", self.toggle, ""),
            ("测试", self.test_fire, ""),
            ("删除", self.delete, "danger"),
        ):
            b = QPushButton(text)
            if name:
                b.setObjectName(name)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)
        self.refresh()

    def refresh(self):
        rows = scheduler.list_reminders(enabled_only=False)
        self.list.setRowCount(len(rows))
        for i, r in enumerate(rows):
            item = QTableWidgetItem(r["content"])
            item.setData(Qt.ItemDataRole.UserRole, r["id"])
            if not r["enabled"]:
                item.setForeground(QColor("#94a3b8"))
            self.list.setItem(i, 0, item)
            rule = r["rule_desc"] + ("" if r["enabled"] else "（停用）")
            self.list.setItem(i, 1, QTableWidgetItem(rule))
            self.list.setItem(i, 2, QTableWidgetItem((r["next_fire_at"] or "—")[:16]))

    def current_id(self):
        r = self.list.currentRow()
        if r < 0:
            return None
        item = self.list.item(r, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def new_reminder(self):
        dlg = ReminderDialog(parent=self)
        if dlg.exec():
            r = scheduler.create_reminder(**dlg.values())
            if not r.get("ok"):
                QMessageBox.warning(self, "提醒没有建成", r.get("error", "未知错误"))
            self.refresh()
            self.changed.emit()

    def toggle(self):
        rid = self.current_id()
        if not rid:
            QMessageBox.information(self, "提示", "先选中一条提醒")
            return
        r = db.query_one("SELECT enabled FROM reminders WHERE id=?", (rid,))
        if not r:
            return
        res = scheduler.set_enabled(rid, not bool(r["enabled"]))
        if not res.get("ok"):
            QMessageBox.warning(self, "操作失败", res.get("error", ""))
        self.refresh()
        self.changed.emit()

    def test_fire(self):
        rid = self.current_id()
        if rid:
            self.sched.fire_now(rid)

    def delete(self):
        rid = self.current_id()
        if not rid:
            return
        if QMessageBox.question(self, "删除提醒", "确定删除这条提醒？") == QMessageBox.StandardButton.Yes:
            scheduler.delete_reminder(rid)
            self.refresh()
            self.changed.emit()


# ============ 音色选择 ============

class VoiceDialog(QDialog):
    """回复语种和朗读音色分开选；音色默认跟随语种"""

    def __init__(self, current_language: str = "auto",
                 current_voice: str = "follow", parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择 Andy 的语种与音色")
        self.setMinimumWidth(600)
        form = QFormLayout(self)

        self.language_combo = QComboBox()
        for key, label in voice.language_choices():
            self.language_combo.addItem(label, key)
        idx = max(0, self.language_combo.findData(current_language))
        self.language_combo.setCurrentIndex(idx if idx >= 0 else 0)

        self.voice_combo = QComboBox()
        for key, label in voice.voice_choices():
            self.voice_combo.addItem(label, key)
        idx = max(0, self.voice_combo.findData(current_voice))
        self.voice_combo.setCurrentIndex(idx if idx >= 0 else 0)

        form.addRow("回复语种", self.language_combo)
        form.addRow("朗读音色", self.voice_combo)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color:#b45309;")
        form.addRow(self.warning)

        note = QLabel("音色默认「跟随语种」，会按回复语种自动选对应音色。"
                      "也可以单独指定音色；跨语言朗读可能不自然，"
                      "界面会提示，但不会阻止你继续。")
        note.setWordWrap(True)
        form.addRow(note)

        self.language_combo.currentIndexChanged.connect(self._update_warning)
        self.voice_combo.currentIndexChanged.connect(self._update_warning)
        self._update_warning()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _update_warning(self):
        text = voice.mismatch_warning(self.language_mode(), self.voice_mode())
        self.warning.setText(text or "当前组合的语种与音色匹配。")

    def language_mode(self) -> str:
        return self.language_combo.currentData() or "auto"

    def voice_mode(self) -> str:
        return self.voice_combo.currentData() or "follow"


# ============ 对话气泡 ============

class MessageBubble(QFrame):
    """一条左右分列的气泡；Andy 在左，用户和通知靠右/居中。"""

    def __init__(self, role: str, text: str = "", parent=None):
        super().__init__(parent)
        self.role = role
        self._thinking = role == "assistant" and not text
        self.setObjectName({
            "user": "userBubble",
            "assistant": "assistantBubble",
            "notice": "noticeBubble",
        }.get(role, "assistantBubble"))
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Minimum)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(3)

        if role == "assistant":
            name = QLabel(APP_NAME)
            name.setObjectName("bubbleName")
            lay.addWidget(name)

        self.body = QLabel(text or ("正在思考…" if self._thinking else ""))
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setStyleSheet(
            "color:#8aa397; background:transparent;" if self._thinking
            else ("color:#8a5a12; background:transparent;" if role == "notice"
                  else "color:#233b31; background:transparent;"))
        lay.addWidget(self.body)

    def append_text(self, text: str):
        if not text:
            return
        if self._thinking:
            self._thinking = False
            self.body.setStyleSheet("color:#233b31; background:transparent;")
            self.body.setText(text)
        else:
            self.body.setText(self.body.text() + text)

    def thinking(self) -> bool:
        return self._thinking


class ChatView(QScrollArea):
    """聊天区：不用 QTextBrowser 拼 HTML，改成可左右分列的气泡。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setObjectName("chatScroll")
        self.viewport().setAutoFillBackground(False)

        self.container = QWidget()
        self.container.setObjectName("chatBody")
        self.vbox = QVBoxLayout(self.container)
        self.vbox.setContentsMargins(2, 4, 2, 8)
        self.vbox.setSpacing(10)
        self.vbox.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self.container)

        self._bubbles = []
        self._current = None
        self._current_row = None

    def _max_width(self, role: str) -> int:
        base = max(280, self.viewport().width())
        ratio = 0.68 if role == "user" else 0.82
        return int(base * ratio)

    def _apply_width(self, bubble: MessageBubble):
        base = max(280, self.viewport().width())
        maxw = self._max_width(bubble.role)
        bubble.setMaximumWidth(maxw)
        if bubble.role == "assistant":
            # 让 Andy 的气泡有稳定的阅读宽度，短句也不会缩成一条窄柱
            bubble.setMinimumWidth(min(maxw, max(300, int(base * 0.72))))
        elif bubble.role == "notice":
            # 提醒文字通常一行放得下，避免被挤成两行
            bubble.setMinimumWidth(min(maxw, 220))

    def _add(self, role: str, text: str = "") -> MessageBubble:
        bubble = MessageBubble(role, text, self.container)
        self._apply_width(bubble)
        row = QHBoxLayout()
        row.setSpacing(0)
        if role == "user":
            row.addStretch(1)
            row.addWidget(bubble)
        elif role == "notice":
            row.addStretch(1)
            row.addWidget(bubble)
            row.addStretch(1)
        else:
            row.addWidget(bubble)
            row.addStretch(1)
        self.vbox.addLayout(row)
        self._bubbles.append(bubble)
        self._scroll_bottom()
        return bubble

    def add_user(self, text: str):
        self._add("user", text)

    def begin_assistant(self) -> MessageBubble:
        self.finish_assistant()
        self._current = self._add("assistant")
        self._current_row = self.vbox.itemAt(self.vbox.count() - 1).layout()
        return self._current

    def append_assistant(self, text: str):
        if not text:
            return
        if self._current is None:
            self.begin_assistant()
        self._current.append_text(text)
        self._scroll_bottom()

    def add_assistant(self, text: str):
        self.begin_assistant()
        self.append_assistant(text)

    def add_notice(self, text: str):
        self._add("notice", text)

    def plain_text(self) -> str:
        """给冒烟测试/日志用的纯文本快照，不是界面渲染路径。"""
        lines = []
        for bubble in list(self._bubbles):
            try:
                text = bubble.body.text().strip()
            except RuntimeError:
                continue
            if text:
                lines.append(text)
        return "\n".join(lines)

    def finish_assistant(self):
        if self._current is None:
            return
        if self._current.thinking():
            self._remove_current()
        self._current = None
        self._current_row = None

    def _remove_current(self):
        if self._current_row is not None:
            self.vbox.removeItem(self._current_row)
        if self._current is not None:
            self._current.setParent(None)
            self._current.deleteLater()
        self._scroll_bottom()

    def _scroll_bottom(self):
        QTimer.singleShot(
            0, lambda: self.verticalScrollBar().setValue(
                self.verticalScrollBar().maximum()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for bubble in list(self._bubbles):
            try:
                self._apply_width(bubble)
            except RuntimeError:
                pass


# ============ 主窗口 ============

class BaymaxWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ensure_dirs()
        db.init_db()
        from core.profile import ensure_first_user, get_user_name
        ensure_first_user()
        self.user_name = get_user_name()

        self.setWindowTitle(f"{APP_NAME} · 学习 / 工作 / 生活")
        self.resize(1060, 700)

        self.bridge = SchedulerBridge()
        self.bridge.fired.connect(self._on_reminder)
        self.sched = scheduler.Scheduler(on_fire=lambda p: self.bridge.fired.emit(p))
        self.sched.start()

        self.mcp_bridge = MCPConfirmBridge()
        self.mcp_bridge.ask.connect(self._on_mcp_confirm_request)
        try:
            from core import mcp_client
            mcp_client.set_confirm_callback(self._confirm_mcp)
        except Exception:
            pass

        self.settings = QSettings("AndyDesk", "Andy")
        voice_enabled = self.settings.value("tts_enabled", voice.speaker.enabled, type=bool)
        voice.speaker.set_enabled(voice_enabled)
        saved_language = self.settings.value("language_mode", None, type=str)
        saved_voice = self.settings.value("voice_mode", "follow", type=str)
        if not saved_language:
            # 旧版只有一个 voice_mode：zh/en/auto/offline 或绑定的语种档
            old = saved_voice or "auto"
            if old == "offline":
                saved_language, saved_voice = "auto", "sapi"
            elif old in voice.LANGUAGE_ALIASES:
                saved_language = voice.LANGUAGE_ALIASES[old]
                saved_voice = "follow"
            elif old in voice.LANGUAGE_MODES:
                saved_language, saved_voice = old, "follow"
            else:
                saved_language, saved_voice = "auto", "follow"
        else:
            if saved_language == "offline":
                saved_language = "auto"
                saved_voice = "sapi"
            elif saved_language not in voice.LANGUAGE_MODES:
                saved_language = "auto"
            if saved_voice not in voice.VOICE_MODES:
                saved_voice = "follow"
            # 旧版语种和音色同名绑定：迁移成“同语种 + 跟随音色”
            if saved_voice == saved_language and saved_voice not in ("follow", "sapi"):
                saved_voice = "follow"
        voice.set_language_mode(saved_language)
        voice.set_voice_mode(saved_voice)
        if voice_enabled:
            voice.speaker.warmup()
        self._asr_worker = None
        self._asr_ignore_result = False
        self._asr_partial_text = ""
        self._streamed_chars = 0
        self._turn_error = ""
        self._turn_cancelled = False

        self._worker = None
        self._build_ui()
        self._refresh_all()
        self._greet()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(60_000)

    # ---- UI ----
    def _card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(39, 105, 82, 28))
        card.setGraphicsEffect(shadow)
        return card

    def _stat_card(self, caption: str):
        card = QFrame()
        card.setObjectName("stat")
        card.setMinimumHeight(68)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(1)
        value = QLabel("0")
        value.setObjectName("statValue")
        cap = QLabel(caption)
        cap.setObjectName("statCaption")
        lay.addWidget(value)
        lay.addWidget(cap)
        return card, value

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("root")
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(12)

        # 顶部品牌栏
        header = QHBoxLayout()
        header.setSpacing(10)
        logo = QLabel("A")
        logo.setObjectName("logo")
        logo.setFixedSize(36, 36)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(logo)

        brand = QVBoxLayout()
        brand.setSpacing(0)
        name = QLabel(APP_NAME)
        name.setObjectName("appName")
        subtitle = QLabel("学习 · 工作 · 生活助手")
        subtitle.setObjectName("muted")
        brand.addWidget(name)
        brand.addWidget(subtitle)
        header.addLayout(brand)
        header.addStretch(1)
        self.date_label = QLabel("")
        self.date_label.setObjectName("muted")
        header.addWidget(self.date_label)
        root.addLayout(header)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        body.setHandleWidth(10)

        # 左：三线看板卡片
        left = self._card()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(16, 16, 16, 14)
        lv.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("三线看板")
        title.setObjectName("h1")
        head.addWidget(title)
        if self.user_name:
            who = QLabel(f"服务对象：{self.user_name}")
            who.setObjectName("muted")
            head.addWidget(who)
        head.addStretch(1)
        lv.addLayout(head)

        stats = QHBoxLayout()
        stats.setSpacing(8)
        due_card, self.stat_due = self._stat_card("今日到期")
        doing_card, self.stat_doing = self._stat_card("进行中")
        reminder_card, self.stat_reminders = self._stat_card("待提醒")
        stats.addWidget(due_card, 1)
        stats.addWidget(doing_card, 1)
        stats.addWidget(reminder_card, 1)
        lv.addLayout(stats)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        lv.addWidget(self.status_label)

        self.tabs = QTabWidget()
        self.pages = {}
        for key, name_text in DOMAINS.items():
            page = TaskPage(key)
            page.changed.connect(self._refresh_all)
            self.pages[key] = page
            self.tabs.addTab(page, name_text)

        self.reminder_page = ReminderPage(self.sched)
        self.reminder_page.changed.connect(self._refresh_all)
        self.tabs.addTab(self.reminder_page, "提醒")

        lv.addWidget(self.tabs, 1)

        done_row = QHBoxLayout()
        self.show_done = QCheckBox("显示已完成")
        self.show_done.toggled.connect(
            lambda v: [p.toggle_done(v) for p in self.pages.values()])
        done_row.addWidget(self.show_done)
        done_row.addStretch(1)
        tool_info = QLabel("function calling · 可扩展工具层")
        tool_info.setObjectName("muted")
        done_row.addWidget(tool_info)
        lv.addLayout(done_row)
        body.addWidget(left)

        # 右：对话卡片
        right = self._card()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(16, 16, 16, 14)
        rv.setSpacing(10)

        chat_head = QHBoxLayout()
        chat_title = QLabel(f"和 {APP_NAME} 说")
        chat_title.setObjectName("h1")
        chat_head.addWidget(chat_title)
        chat_head.addStretch(1)
        online = QLabel("● 在线 · 可调用工具")
        online.setObjectName("badge")
        online.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chat_head.addWidget(online)
        rv.addLayout(chat_head)

        self.chat = ChatView()
        rv.addWidget(self.chat, 1)

        # 工具进度条：正在查任务 / 正在写入 / 等待确认
        self.activity = QLabel("")
        self.activity.setObjectName("activity")
        self.activity.hide()
        rv.addWidget(self.activity)

        quick = QHBoxLayout()
        quick.setSpacing(6)
        for text, payload in (("今日概览", "今天还有什么要紧的？"),
                              ("＋ 新建任务", "__new_task__"),
                              ("＋ 新建提醒", "__new_reminder__")):
            b = QPushButton(text)
            b.setObjectName("pill")
            b.clicked.connect(lambda _=False, p=payload: self._quick(p))
            quick.addWidget(b)
        quick.addStretch(1)
        rv.addLayout(quick)

        composer = QFrame()
        composer.setObjectName("composer")
        input_row = QHBoxLayout(composer)
        input_row.setContentsMargins(10, 6, 8, 6)
        input_row.setSpacing(4)
        self.input = QLineEdit()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText("说说要做的事，例如：明早八点提醒我")
        self.input.setMinimumHeight(38)
        self.input.returnPressed.connect(self.send)
        self.asr_btn = QPushButton("语音输入")
        self.asr_btn.setObjectName("ghost")
        self.asr_btn.setToolTip("点击开始说话，再点一次停止；识别结果会填入输入框，按回车发送")
        self.asr_btn.clicked.connect(self._toggle_asr)
        self.voice_check = QCheckBox("朗读")
        self.voice_check.setToolTip("打开后 Andy 会把整段回复读出来")
        self.voice_check.setChecked(voice.speaker.enabled)
        self.voice_check.toggled.connect(self._toggle_voice)
        self.voice_btn = QPushButton("语种/音色")
        self.voice_btn.setObjectName("ghost")
        self.voice_btn.setToolTip(voice.summary_label())
        self.voice_btn.clicked.connect(self._choose_voice)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("primary")
        self.send_btn.setFixedWidth(62)
        self.send_btn.clicked.connect(self.send)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("ghost")
        self.stop_btn.setFixedWidth(46)
        self.stop_btn.setToolTip("中止本轮生成，已经出来的文字会保留")
        self.stop_btn.clicked.connect(self._stop_generation)
        self.stop_btn.hide()
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.asr_btn)
        input_row.addWidget(self.voice_check)
        input_row.addWidget(self.voice_btn)
        input_row.addWidget(self.stop_btn)
        input_row.addWidget(self.send_btn)
        rv.addWidget(composer)

        hint = QLabel("Enter 发送 · 需要写入数据时 Andy 会先向你确认")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rv.addWidget(hint)
        body.addWidget(right)

        body.setSizes([420, 640])
        root.addWidget(body, 1)
        self.setCentralWidget(central)

        # 浮动提示条（提醒弹出）
        self.toast = QLabel("", self)
        self.toast.setObjectName("toast")
        self.toast.setWordWrap(True)
        self.toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 放在顶部品牌栏的空白区，避免盖住对话和输入框
        self.toast.setGeometry(max(220, (self.width() - 320) // 2), 10, 320, 52)

    # ---- 数据刷新 ----
    def _refresh_all(self):
        for p in self.pages.values():
            p.refresh()
        self.reminder_page.refresh()
        self._refresh_status()

    def _refresh_status(self):
        ov = tasks.today_overview()
        today = datetime.now()
        weekdays = "一二三四五六日"
        self.date_label.setText(
            f"{today.year}年{today.month}月{today.day}日 "
            f"星期{weekdays[today.weekday()]}")
        self.stat_due.setText(str(ov["due_today"]))
        self.stat_doing.setText(str(ov["doing"]))
        self.stat_reminders.setText(str(ov["reminders_today"]))
        if ov["overdue"]:
            self.status_label.setText(
                f"今天到期 {ov['due_today']} 项，逾期 {ov['overdue']} 项；"
                f"先挑一件动工。")
        elif ov["due_today"]:
            self.status_label.setText(
                f"今天到期 {ov['due_today']} 项；先挑一件动工。")
        elif ov["doing"]:
            self.status_label.setText(
                f"正在进行 {ov['doing']} 项，今天没有到期或逾期。")
        else:
            self.status_label.setText(
                "今天没有压着的事，点“＋ 新建任务”记一笔。")

    # ---- 对话 ----
    def _greet(self):
        ov = tasks.today_overview()
        who = f"{self.user_name}，" if self.user_name else ""
        who_en = f"{self.user_name}, " if self.user_name else ""
        mode = voice.current_language_mode()
        if mode.startswith("en-"):
            if ov["overdue"] or ov["due_today"]:
                text = (f"{who_en}you have {ov['due_today']} due today and "
                        f"{ov['overdue']} overdue. Pick one and start; I'll watch the rest.")
            else:
                text = (f"{who_en}I'm here. Nothing is pressing today. "
                        "Tell me what to remember or set a reminder.")
        elif mode == "zh-TW":
            if ov["overdue"] or ov["due_today"]:
                text = (f"{who}今天到期 {ov['due_today']} 項，逾期 {ov['overdue']} 項。"
                        "先挑一件動工，我幫你盯著剩下的。")
            else:
                text = f"{who}我在。今天沒有壓著的事，想記點什麼、設個提醒，直接說。"
        elif ov["overdue"] or ov["due_today"]:
            text = (f"{who}今天到期 {ov['due_today']} 项，逾期 {ov['overdue']} 项。"
                    f"先挑一件动手，我帮你盯着剩下的。")
        else:
            text = f"{who}我在。今天没压着的事，想记点什么、设个提醒，直接说。"
        self.chat.add_assistant(text)

    def _append_user(self, text: str):
        self.chat.add_user(text)

    def _quick(self, payload: str):
        if payload == "__new_task__":
            page = self.pages.get("life")
            self.tabs.setCurrentWidget(page)
            page.new_task()
        elif payload == "__new_reminder__":
            self.tabs.setCurrentWidget(self.reminder_page)
            self.reminder_page.new_reminder()
        else:
            self.input.setText(payload)
            self.send()

    def send(self):
        text = self.input.text().strip()
        if not text:
            return
        if self._worker and self._worker.isRunning():
            return
        if self._asr_worker and self._asr_worker.isRunning():
            # 发送时只停录音，丢弃这次 ASR 的迟到结果，避免把文字又写回输入框
            self._asr_ignore_result = True
            self._asr_worker.stop()
            self.asr_btn.setText("结束中…")
            self.asr_btn.setEnabled(False)
        if voice.speaker.enabled:
            voice.speaker.stop()
        self._streamed_chars = 0
        self._turn_error = ""
        self._turn_cancelled = False
        self.input.clear()
        self._append_user(text)
        self.chat.begin_assistant()
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.stop_btn.show()
        self.input.setEnabled(False)

        self._worker = ChatWorker(text, self)
        self._worker.event.connect(self._on_agent_event)
        self._worker.start()

    def _stop_generation(self):
        if not (self._worker and self._worker.isRunning()):
            return
        self._worker.stop()
        self.stop_btn.setEnabled(False)
        self._set_activity("正在停止生成…")
        if voice.speaker.enabled:
            voice.speaker.stop()

    def _set_activity(self, text: str):
        if not text:
            self.activity.hide()
            return
        self.activity.setText(text)
        self.activity.show()

    @staticmethod
    def _tool_activity(name: str) -> str:
        if name.startswith("mcp__"):
            return "正在调用外部工具…"
        if name in ("task_list", "overview_today"):
            return "正在查任务…"
        if name == "reminder_list":
            return "正在查提醒…"
        if name in ("task_create", "task_update", "task_breakdown"):
            return "正在写入任务…"
        if name == "reminder_create":
            return "正在写入提醒…"
        if name == "memory_save":
            return "正在记住这条信息…"
        return "正在执行…"

    def _on_agent_event(self, event: dict):
        kind = event.get("type")
        if kind == "text_delta":
            self._on_chunk(event.get("text", ""))
        elif kind == "tool_start":
            self._set_activity(self._tool_activity(event.get("name", "")))
        elif kind == "approval_request":
            name = event.get("name", "")
            self._set_activity("等待你确认外部写入…")
            self.chat.add_notice(f"需要确认：{name}")
        elif kind == "tool_result":
            if event.get("ok") is False:
                self.chat.add_notice(
                    f"⚠ {event.get('name', '工具')} 失败：{event.get('error') or '未知原因'}")
            self._set_activity("正在处理工具结果…")
        elif kind == "error":
            self._turn_error = event.get("message") or "未知错误"
        elif kind == "done":
            self._finish_turn(event)

    def _finish_turn(self, event: dict):
        reply = (event.get("reply") or "").strip()
        error = event.get("error") or self._turn_error
        cancelled = bool(event.get("cancelled"))
        self._turn_cancelled = cancelled
        if reply and not error and not cancelled and not self._streamed_chars:
            # 硬拦截/直接返回，不经过流式分片
            self._on_chunk(reply)
        self.chat.finish_assistant()
        self._set_activity("")
        self.stop_btn.hide()
        self.send_btn.setEnabled(True)
        self.input.setEnabled(True)
        self.input.setFocus()
        if event.get("tools_called"):
            self._refresh_all()
        if error:
            self.chat.add_notice(f"⚠ {str(error)[:200]}")
        elif cancelled:
            self.chat.add_notice("已停止生成。")
        if reply and not error and not cancelled and voice.speaker.enabled:
            # 整段回复合成一次，避免流式逐句拼接造成的断续和重复起播
            voice.speaker.speak(reply)

    def _on_chunk(self, text: str):
        if not text:
            return
        self.chat.append_assistant(text)
        self._streamed_chars += len(text)

    # ---- 语音输入（ASR） ----
    def _toggle_asr(self):
        if self._asr_worker and self._asr_worker.isRunning():
            self._asr_worker.stop()
            self.asr_btn.setText("结束中…")
            self.asr_btn.setEnabled(False)
            return
        ok, reason = asr.model_available()
        if not ok:
            QMessageBox.warning(
                self, "语音输入不可用",
                f"{reason}\n\n请把 Vosk 中文模型放到 models/vosk-model-small-cn-0.22，"
                "或在 .env 里用 ASR_MODEL_DIR 指定目录。",
            )
            return
        if self._worker and self._worker.isRunning():
            return
        voice.speaker.stop()
        self._asr_ignore_result = False
        self._asr_partial_text = ""
        self._asr_worker = ASRWorker(self)
        self._asr_worker.partial.connect(self._on_asr_partial)
        self._asr_worker.done.connect(self._on_asr_done)
        self._asr_worker.error.connect(self._on_asr_error)
        self._asr_worker.state.connect(self._on_asr_state)
        self._asr_worker.finished.connect(self._on_asr_finished)
        self.asr_btn.setText("准备中…")
        self.asr_btn.setEnabled(False)
        self._asr_worker.start()

    def _on_asr_state(self, state: str):
        if self._asr_ignore_result:
            return
        if state == "loading":
            self.asr_btn.setText("准备中…")
            self.asr_btn.setEnabled(False)
        elif state == "listening":
            self.asr_btn.setText("停止录音")
            self.asr_btn.setEnabled(True)
            self.input.setPlaceholderText("正在听…… 说完点“停止录音”")

    def _on_asr_partial(self, text: str):
        if self._asr_ignore_result:
            return
        if self._asr_worker and self._asr_worker.isRunning() and text:
            self._asr_partial_text = text
            self.input.setText(text)
            self.input.setCursorPosition(len(text))

    def _on_asr_done(self, text: str):
        if self._asr_ignore_result:
            return
        text = (text or "").strip()
        if text:
            self.input.setText(text)
            self.input.setFocus()
            self.input.setCursorPosition(len(text))
        else:
            # 只有输入框里仍是这次语音的临时结果时才清空，避免误删手动输入
            if self._asr_partial_text and \
                    self.input.text().strip() == self._asr_partial_text.strip():
                self.input.clear()
            self.input.setPlaceholderText("没听清，点“语音输入”再试一次")

    def _on_asr_error(self, message: str):
        if self._asr_ignore_result:
            return
        QMessageBox.warning(self, "语音输入失败", message)

    def _on_asr_finished(self):
        self._asr_ignore_result = False
        self._asr_partial_text = ""
        self._asr_worker = None
        self.asr_btn.setText("语音输入")
        self.asr_btn.setEnabled(True)
        self.input.setPlaceholderText("说说要做的事，例如：明早八点提醒我")

    # ---- 语音 ----
    def _choose_voice(self):
        dlg = VoiceDialog(voice.current_language_mode(), voice.current_voice_mode(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        lang_mode = voice.set_language_mode(dlg.language_mode())
        voice_mode = voice.set_voice_mode(dlg.voice_mode())
        self.settings.setValue("language_mode", lang_mode)
        self.settings.setValue("voice_mode", voice_mode)
        self.voice_btn.setToolTip(voice.summary_label())
        if voice.speaker.enabled:
            voice.speaker.speak(voice.sample_text())

    def _toggle_voice(self, on: bool):
        voice.speaker.set_enabled(on)
        self.settings.setValue("tts_enabled", bool(on))
        if on:
            voice.speaker.warmup()
            voice.speaker.speak(voice.sample_text())

    # ---- MCP 写入确认（后台线程 → 主线程）----
    def _confirm_mcp(self, server: str, tool: str, args: dict) -> bool:
        if threading.current_thread() is threading.main_thread():
            return self._ask_mcp_confirm(server, tool, args)
        event = threading.Event()
        box = {}
        self.mcp_bridge.ask.emit(server, tool, dict(args or {}), event, box)
        event.wait(30)
        return bool(box.get("ok"))

    def _on_mcp_confirm_request(self, server, tool, args, event, box):
        try:
            box["ok"] = self._ask_mcp_confirm(server, tool, args)
        finally:
            event.set()

    def _ask_mcp_confirm(self, server: str, tool: str, args: dict) -> bool:
        detail = json.dumps(args or {}, ensure_ascii=False)
        if len(detail) > 300:
            detail = detail[:300] + "…"
        return QMessageBox.question(
            self, "MCP 写入确认",
            f"MCP 服务「{server}」请求执行写入工具：\n{tool}\n\n参数：{detail}\n\n允许执行吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _on_done(self, res: dict):
        """兼容旧调用：统一走 done 事件收尾。"""
        self._finish_turn(res)

    # ---- 提醒回调（调度器线程 -> Qt 主线程）----
    def _on_reminder(self, payload: dict):
        content = payload.get("content", "")
        prefix = "补发" if payload.get("type") == "catch_up" else "提醒"
        self.chat.add_notice(f"⏰ {prefix}：{content}")
        self.toast.setText(f"⏰ {prefix}：{content}")
        self.toast.show()
        self._toast_timer.start(8000)
        QApplication.alert(self, 0)
        self._refresh_all()
        if voice.speaker.enabled:
            voice.speaker.speak(f"提醒：{content}")

    def closeEvent(self, event):
        if self._asr_worker and self._asr_worker.isRunning():
            self._asr_worker.stop()
            self._asr_worker.wait(2000)
        if self._worker and self._worker.isRunning():
            self._worker.wait(2000)
        voice.speaker.shutdown()
        self.sched.shutdown()
        super().closeEvent(event)


def health_check() -> bool:
    return True
