"""
桌面层 main_window.py
- 左：三线看板（学习/工作/生活）+ 提醒页
- 右：对话 + 工具调用
- 状态栏：今日概览
扩展性：任务/提醒页只依赖 core 层的公开函数；新增能力只需在 core/tools.py 注册工具
"""

import html
import json
import threading
from datetime import datetime

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer, QSettings
from PyQt6.QtGui import QTextCursor, QColor
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTabWidget,
    QTableWidget, QTableWidgetItem, QPushButton, QLineEdit, QTextBrowser,
    QLabel, QDialog, QFormLayout, QComboBox, QDateTimeEdit, QTimeEdit,
    QCheckBox, QTextEdit, QMessageBox, QAbstractItemView, QHeaderView,
    QStackedWidget, QApplication, QDialogButtonBox,
)

from config import APP_NAME, DOMAINS, ensure_dirs
from core import db, tasks, scheduler, brain, voice


# ============ 后台线程 ============

class ChatWorker(QThread):
    """一次对话在子线程里跑，流式片段通过信号回主线程"""
    chunk = pyqtSignal(str)
    done = pyqtSignal(dict)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.text = text

    def run(self):
        try:
            res = brain.ask_baymax(self.text, on_chunk=lambda s: self.chunk.emit(s))
        except Exception as e:
            res = {"reply": f"出错了：{e}", "tools_called": [], "error": str(e)}
        self.done.emit(res)


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
        self._voice_feed = voice.StreamFeed()
        self._voice_stream_chars = 0
        self._streamed_chars = 0

        self._worker = None
        self._build_ui()
        self._refresh_all()
        self._greet()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(60_000)

    # ---- UI ----
    def _build_ui(self):
        split = QSplitter(Qt.Orientation.Horizontal)

        # 左：看板
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 6, 10)

        head = QHBoxLayout()
        title = QLabel("三线看板")
        title.setObjectName("h1")
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        head.addWidget(title)
        if self.user_name:
            who = QLabel(f"服务对象：{self.user_name}")
            who.setObjectName("muted")
            head.addWidget(who)
        head.addStretch(1)
        lv.addLayout(head)
        lv.addWidget(self.status_label)

        self.tabs = QTabWidget()
        self.pages = {}
        for key, name in DOMAINS.items():
            page = TaskPage(key)
            page.changed.connect(self._refresh_all)
            self.pages[key] = page
            self.tabs.addTab(page, name)

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
        tool_info = QLabel("能力注册：function calling + 可扩展工具层")
        tool_info.setObjectName("muted")
        done_row.addWidget(tool_info)
        lv.addLayout(done_row)
        split.addWidget(left)

        # 右：对话
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 10, 10, 10)

        chat_title = QLabel(f"和 {APP_NAME} 说")
        chat_title.setObjectName("h1")
        rv.addWidget(chat_title)

        self.chat = QTextBrowser()
        self.chat.setOpenExternalLinks(False)
        self.chat.setStyleSheet("""
            QTextBrowser { background: #ffffff; border: 1px solid #e2e8f0;
                           border-radius: 10px; padding: 10px; font-size: 14px; }
        """)
        rv.addWidget(self.chat, 1)

        quick = QHBoxLayout()
        for text, payload in (("今日概览", "今天还有什么要紧的？"),
                              ("新建任务", "__new_task__"),
                              ("新建提醒", "__new_reminder__")):
            b = QPushButton(text)
            b.setObjectName("ghost")
            b.clicked.connect(lambda _=False, p=payload: self._quick(p))
            quick.addWidget(b)
        quick.addStretch(1)
        rv.addLayout(quick)

        input_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("比如：明天下午三点提醒我交周报 / 软考冲刺记到学习里")
        self.input.returnPressed.connect(self.send)
        self.voice_check = QCheckBox("语音")
        self.voice_check.setToolTip("打开后 Andy 会把回复读出来")
        self.voice_check.setChecked(voice.speaker.enabled)
        self.voice_check.toggled.connect(self._toggle_voice)
        self.voice_btn = QPushButton("语言/音色")
        self.voice_btn.setObjectName("ghost")
        self.voice_btn.setToolTip(voice.summary_label())
        self.voice_btn.clicked.connect(self._choose_voice)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self.send)
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.voice_check)
        input_row.addWidget(self.voice_btn)
        input_row.addWidget(self.send_btn)
        rv.addLayout(input_row)
        split.addWidget(right)

        split.setSizes([420, 640])
        self.setCentralWidget(split)

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
        self.toast.setGeometry(self.width() - 380, self.height() - 220, 340, 80)

    # ---- 数据刷新 ----
    def _refresh_all(self):
        for p in self.pages.values():
            p.refresh()
        self.reminder_page.refresh()
        self._refresh_status()

    def _refresh_status(self):
        self.status_label.setText(tasks.overview_text().replace("\n", "　·　"))

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
        self.chat.append(f"<p style='color:#0f766e'><b>{APP_NAME}</b></p>"
                         f"<p>{html.escape(text)}</p>")

    def _append_user(self, text: str):
        self.chat.append(f"<p style='color:#64748b'><b>你</b></p>"
                         f"<p>{html.escape(text)}</p>")

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
        if voice.speaker.enabled:
            voice.speaker.stop()
        self._voice_feed = voice.StreamFeed()
        self._voice_stream_chars = 0
        self._streamed_chars = 0
        self.input.clear()
        self._append_user(text)
        self.chat.append(f"<p style='color:#0f766e'><b>{APP_NAME}</b></p>")
        cursor = self.chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.chat.setTextCursor(cursor)
        self.chat.insertPlainText("\n")
        self.send_btn.setEnabled(False)
        self.input.setEnabled(False)

        self._worker = ChatWorker(text, self)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_chunk(self, text: str):
        if not text:
            return
        cursor = self.chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.chat.setTextCursor(cursor)
        self.chat.insertPlainText(text)
        self._streamed_chars += len(text)
        self.chat.verticalScrollBar().setValue(self.chat.verticalScrollBar().maximum())
        if voice.speaker.enabled and text:
            remaining = max(0, voice.TTS_MAX_CHARS - self._voice_stream_chars)
            if remaining:
                for sentence in self._voice_feed.feed(text[:remaining]):
                    voice.speaker.speak(sentence, interrupt=False)
            self._voice_stream_chars += len(text)

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
        self.send_btn.setEnabled(True)
        self.input.setEnabled(True)
        self.input.setFocus()
        if res.get("tools_called"):
            self._refresh_all()
        if res.get("error"):
            self.chat.append(f"<p style='color:#b45309'>{html.escape(res['error'][:200])}</p>")
        self.chat.append("")
        reply = (res.get("reply") or "").strip()
        if reply and not res.get("error") and not self._streamed_chars:
            # 日期/星期/时间等硬拦截直接返回，不经过流式分片
            self._on_chunk(reply)
        if reply and not res.get("error") and voice.speaker.enabled:
            if self._voice_stream_chars:
                for sentence in self._voice_feed.flush():
                    voice.speaker.speak(sentence, interrupt=False)
            else:
                voice.speaker.speak(reply)

    # ---- 提醒回调（调度器线程 -> Qt 主线程）----
    def _on_reminder(self, payload: dict):
        content = payload.get("content", "")
        prefix = "补发" if payload.get("type") == "catch_up" else "提醒"
        self.chat.append(f"<p style='color:#b45309'><b>⏰ {prefix}</b> {html.escape(content)}</p>")
        self.toast.setText(f"⏰ {prefix}：{content}")
        self.toast.show()
        self._toast_timer.start(8000)
        QApplication.alert(self, 0)
        self._refresh_all()
        if voice.speaker.enabled:
            voice.speaker.speak(f"提醒：{content}")

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._worker.wait(2000)
        voice.speaker.shutdown()
        self.sched.shutdown()
        super().closeEvent(event)


def health_check() -> bool:
    return True
