from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QListWidget, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget

from src.ui.widgets.common import Card, PageHeader, StatusPill, section_title


class AutomaticPage(QWidget):
    start_requested = Signal(dict)
    pause_requested = Signal()
    resume_requested = Signal()
    stop_requested = Signal()
    emergency_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(18)
        layout.addWidget(PageHeader("全自动", "自动识别当前任务、选择对应路线、执行并结束；高级选项可持续运行。"))

        hero = Card(accent=True)
        heading = QHBoxLayout()
        title = QLabel("农场全流程")
        title.setObjectName("CardTitle")
        self.status = StatusPill("准备就绪", "success")
        heading.addWidget(title)
        heading.addStretch(1)
        heading.addWidget(self.status)
        description = QLabel("默认执行一轮：识别一个可处理任务并运行相应路线。没有匹配任务时安全结束。")
        description.setObjectName("CardDescription")
        description.setWordWrap(True)
        self.activity = QLabel("当前状态：等待启动")
        self.activity.setObjectName("Muted")
        self.activity.setWordWrap(True)
        buttons = QHBoxLayout()
        self.start_button = QPushButton("开始全自动")
        self.start_button.setObjectName("PrimaryButton")
        self.pause_button = QPushButton("暂停")
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("DangerButton")
        self.emergency_button = QPushButton("紧急停止")
        self.emergency_button.setObjectName("DangerButton")
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.emergency_button.setEnabled(False)
        self._paused = False
        self.start_button.clicked.connect(self._start)
        self.pause_button.clicked.connect(self._toggle_pause)
        self.stop_button.clicked.connect(self.stop_requested)
        self.emergency_button.clicked.connect(self.emergency_requested)
        for button in (self.start_button, self.pause_button, self.stop_button, self.emergency_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        hero.body.addLayout(heading)
        hero.body.addWidget(description)
        hero.body.addWidget(self.activity)
        hero.body.addLayout(buttons)
        layout.addWidget(hero)

        columns = QHBoxLayout()
        self.settings_card = Card()
        self.settings_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.settings_card.body.addWidget(section_title("运行范围"))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        self.scope = QComboBox()
        self.scope.addItem("运行一轮", "single")
        self.scope.addItem("持续运行", "continuous")
        self.max_tasks = QSpinBox()
        self.max_tasks.setRange(1, 50)
        self.max_tasks.setValue(1)
        self.overlay_note = QLabel("启动全自动会隐式开启独立遮罩服务；任务期间接管遮罩，结束后继续实时识别。")
        self.overlay_note.setObjectName("Muted")
        self.overlay_note.setWordWrap(True)
        self.dry_run = QCheckBox("仅预览，不发送输入")
        form.addRow("执行方式", self.scope)
        form.addRow("最多任务数", self.max_tasks)
        form.addRow("游戏遮罩", self.overlay_note)
        form.addRow("安全调试", self.dry_run)
        self.settings_card.body.addLayout(form)

        self.priority_card = Card()
        self.priority_card.body.addWidget(section_title("当前任务优先级"))
        self.priority_list = QListWidget()
        self.priority_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.priority_list.setMinimumHeight(230)
        self.priority_card.body.addWidget(self.priority_list)
        columns.addWidget(self.settings_card, 1, Qt.AlignmentFlag.AlignTop)
        columns.addWidget(self.priority_card, 1)
        layout.addLayout(columns)
        layout.addStretch(1)
        self._mode_active = False
        self._launch_enabled = False
        self._state = "idle"
        self._request_pending = False
        self._task_names: dict[str, str] = {}

    def set_tasks(self, tasks: list[dict]) -> None:
        self._task_names = {
            str(task.get("id", "")): str(task.get("name", task.get("id", "未知任务")))
            for task in tasks
            if str(task.get("id", ""))
        }
        self.priority_list.clear()
        ordered = sorted(
            (
                task
                for task in tasks
                if task.get("enabled") and task.get("markers") and not task.get("independent_only", False)
            ),
            key=lambda item: -int(item.get("priority", 0)),
        )
        for index, task in enumerate(ordered, start=1):
            self.priority_list.addItem(f"{index}.  {task.get('name', task.get('id'))}")

    def _start(self) -> None:
        continuous = self.scope.currentData() == "continuous"
        self.start_requested.emit(
            {
                "mode": "automatic",
                "continuous": continuous,
                "max_tasks": None if continuous else self.max_tasks.value(),
                "dry_run": self.dry_run.isChecked(),
            }
        )

    def _toggle_pause(self) -> None:
        if self._paused:
            self.resume_requested.emit()
        else:
            self.pause_requested.emit()

    def update_activity(self, event_type: str, payload: dict) -> None:
        task_id = str(payload.get("task_id", ""))
        task_name = self._task_names.get(task_id, task_id or "任务")
        if event_type == "task_selected":
            self.activity.setText(f"当前状态：已识别 {task_name}，准备执行")
        elif event_type == "route_started":
            self.activity.setText(f"当前状态：正在执行 {task_name}")
        elif event_type == "route_completed":
            if bool(payload.get("success", False)):
                self.activity.setText(f"当前状态：{task_name} 已完成，正在检查下一任务")
            else:
                message = str(payload.get("message", "执行失败"))
                self.activity.setText(f"当前状态：{task_name} 未完成 · {message}")
        elif event_type == "task_result":
            message = str(payload.get("message", "任务状态已更新"))
            self.activity.setText(f"当前状态：{message}")

    def set_launch_enabled(self, enabled: bool) -> None:
        self._launch_enabled = bool(enabled)
        self.start_button.setEnabled(self._launch_enabled)

    def update_state(self, state: str, _payload: dict) -> None:
        self._state = str(state)
        self._request_pending = bool(_payload.get("_start_pending", False))
        mode = str(_payload.get("mode", ""))
        if mode:
            self._mode_active = mode == "automatic"
        if state in {"idle", "completed", "error"}:
            self._mode_active = False
        active = (
            self._mode_active
            and not self._request_pending
            and state in {"starting", "running", "paused", "stopping"}
        )
        self.start_button.setEnabled(self._launch_enabled)
        self.pause_button.setEnabled(active and state in {"running", "paused"})
        self.stop_button.setEnabled(active)
        self.emergency_button.setEnabled(active)
        self._paused = state == "paused"
        self.pause_button.setText("继续" if state == "paused" else "暂停")
        labels = {"starting": "启动检查", "running": "运行中", "paused": "已暂停", "stopping": "停止中", "error": "运行错误"}
        self.status.setText(labels.get(state, "准备就绪") if active or state == "error" else "准备就绪")
        self.status.set_tone("danger" if state == "error" else "accent" if active else "success")
        if state == "starting":
            self.activity.setText("当前状态：正在启动并检查游戏窗口")
        elif state == "paused":
            self.activity.setText("当前状态：任务已暂停")
        elif state == "stopping":
            self.activity.setText("当前状态：正在安全停止并释放输入")
        elif state == "idle" and int(_payload.get("exit_code", 0) or 0) == 130:
            self.activity.setText("当前状态：任务已安全停止")
