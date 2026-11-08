from __future__ import annotations

from datetime import datetime

from PySide6.QtGui import QGuiApplication, QTextCharFormat, QColor
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from src.ui.theme import COLORS
from src.ui.widgets.common import PageHeader, StatusPill


class LogsPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(16)
        layout.addWidget(PageHeader("实时日志", "查看 worker、识别和路线执行事件。日志同时保存在项目日志目录。"))
        toolbar = QHBoxLayout()
        self.level_filter = QComboBox()
        self.level_filter.addItems(["全部级别", "INFO", "WARNING", "ERROR"])
        self.status = StatusPill("等待 worker", "neutral")
        clear = QPushButton("清空显示")
        copy = QPushButton("复制日志")
        clear.clicked.connect(self.editor_clear)
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.editor.toPlainText()))
        toolbar.addWidget(self.status)
        toolbar.addStretch(1)
        toolbar.addWidget(self.level_filter)
        toolbar.addWidget(copy)
        toolbar.addWidget(clear)
        layout.addLayout(toolbar)
        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setMaximumBlockCount(5000)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setStyleSheet('font-family: "Cascadia Mono", Consolas; font-size: 13px; padding: 12px;')
        layout.addWidget(self.editor, 1)

    def append_log(self, level: str, message: str) -> None:
        selected = self.level_filter.currentText()
        if selected != "全部级别" and selected != level:
            return
        cursor = self.editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        fmt = QTextCharFormat()
        colors = {"ERROR": COLORS["danger"], "WARNING": COLORS["warning"], "INFO": COLORS["muted"]}
        fmt.setForeground(QColor(colors.get(level, COLORS["text"])))
        cursor.insertText(f"[{datetime.now():%H:%M:%S}] {level:<7} {message}\n", fmt)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        self.status.setText("正在接收")
        self.status.set_tone("success")

    def editor_clear(self) -> None:
        self.editor.clear()
