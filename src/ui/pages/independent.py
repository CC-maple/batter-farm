from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from src.ui.widgets.common import Card, PageHeader, StatusPill, action_label, clear_layout


class IndependentPage(QWidget):
    start_task_requested = Signal(str, bool)
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self.layout = QVBoxLayout(content)
        self.layout.setContentsMargins(28, 24, 28, 28)
        self.layout.setSpacing(18)
        self.layout.addWidget(PageHeader("独立任务", "选择一个明确任务并直接执行路线；不会先判断当前任务类型。"))
        notice = Card(accent=True)
        row = QHBoxLayout()
        label = QLabel("执行前仍会检查游戏窗口和路线配置。已开启实时识别时，任务会暂时接管遮罩并在结束后恢复监控。")
        label.setWordWrap(True)
        self.stop_button = QPushButton("停止当前任务")
        self.stop_button.setObjectName("DangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_requested)
        row.addWidget(label, 1)
        row.addWidget(self.stop_button)
        notice.body.addLayout(row)
        self.layout.addWidget(notice)
        self.grid = QGridLayout()
        self.grid.setSpacing(12)
        self.layout.addLayout(self.grid)
        self.layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self._buttons: list[QPushButton] = []
        self._active = False
        self._active_mode = ""
        self._request_pending = False
        self._launch_enabled = False

    def set_tasks(self, tasks: list[dict]) -> None:
        clear_layout(self.grid)
        self._buttons.clear()
        supported = [task for task in tasks if task.get("enabled") and task.get("independent_supported")]
        for index, task in enumerate(supported):
            card = Card()
            top = QHBoxLayout()
            title = QLabel(str(task.get("name", task.get("id", "任务"))))
            title.setObjectName("CardTitle")
            top.addWidget(title)
            top.addStretch(1)
            top.addWidget(StatusPill(action_label(task.get("action")), "neutral"))
            description = QLabel(str(task.get("description", "")))
            description.setObjectName("CardDescription")
            description.setWordWrap(True)
            markers = ", ".join(task.get("markers", [])) or "无需任务标识"
            meta = QLabel(f"识别标识：{markers}\n路线来源：{task.get('source') or '内置流程'}")
            meta.setObjectName("Muted")
            button = QPushButton("开始任务")
            button.setObjectName("PrimaryButton")
            button.setEnabled(self._launch_enabled and not self._active)
            task_id = str(task.get("id", ""))
            button.clicked.connect(lambda _checked=False, value=task_id: self.start_task_requested.emit(value, False))
            self._buttons.append(button)
            card.body.addLayout(top)
            card.body.addWidget(description)
            card.body.addWidget(meta)
            card.body.addWidget(button, 0, Qt.AlignmentFlag.AlignRight)
            row, column = divmod(index, 2)
            self.grid.addWidget(card, row, column)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)

    def update_state(self, state: str, _payload: dict) -> None:
        self._request_pending = bool(_payload.get("_start_pending", False))
        mode = str(_payload.get("mode", ""))
        if mode:
            self._active_mode = mode
        if state in {"idle", "error"}:
            self._active_mode = ""
        active = (
            self._active_mode == "independent"
            and not self._request_pending
            and state in {"starting", "running", "paused", "stopping"}
        )
        self._active = active
        for button in self._buttons:
            button.setEnabled(self._launch_enabled and not active)
        self.stop_button.setEnabled(active)

    def set_launch_enabled(self, enabled: bool) -> None:
        self._launch_enabled = bool(enabled)
        for button in self._buttons:
            button.setEnabled(self._launch_enabled and not self._active)
