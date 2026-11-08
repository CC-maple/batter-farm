from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.ui.widgets.common import Card, FeatureCard, MetricCard, PageHeader, StatusPill, section_title


class HomePage(QWidget):
    navigate_requested = Signal(str)
    realtime_start_requested = Signal(dict)
    realtime_stop_requested = Signal()
    automatic_start_requested = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(18)
        layout.addWidget(PageHeader("运行中心", "检查运行环境、选择工作模式，并集中控制当前自动化任务。"))

        launch_card = Card(accent=True)
        launch_row = QHBoxLayout()
        launch_row.setSpacing(14)
        launch_icon = QLabel("▷")
        launch_icon.setObjectName("LaunchIcon")
        launch_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        launch_icon.setFixedSize(34, 42)
        launch_copy = QVBoxLayout()
        launch_copy.setSpacing(3)
        self.launch_title = QLabel("游戏遮罩与实时识别")
        self.launch_title.setObjectName("CardTitle")
        launch_description = QLabel("启动后持续识别并显示遮罩，只提示、不发送键鼠输入；动作任务会安全接管，结束后自动恢复。")
        launch_description.setObjectName("CardDescription")
        launch_description.setWordWrap(True)
        launch_copy.addWidget(self.launch_title)
        launch_copy.addWidget(launch_description)
        self.overlay_start_button = QPushButton("启动")
        self.overlay_start_button.setObjectName("PrimaryButton")
        self.overlay_start_button.clicked.connect(self._toggle_realtime)
        launch_row.addWidget(launch_icon)
        launch_row.addLayout(launch_copy, 1)
        launch_row.addWidget(self.overlay_start_button)
        launch_card.body.addLayout(launch_row)
        layout.addWidget(launch_card)

        status_grid = QGridLayout()
        status_grid.setHorizontalSpacing(12)
        self.metrics = {
            "admin": MetricCard("管理员权限"),
            "window": MetricCard("游戏窗口"),
            "model": MetricCard("检测模型"),
            "runtime": MetricCard("运行状态", "空闲", "success"),
        }
        for column, card in enumerate(self.metrics.values()):
            card.setMinimumWidth(0)
            status_grid.addWidget(card, 0, column)
            status_grid.setColumnStretch(column, 1)
        layout.addLayout(status_grid)

        layout.addWidget(section_title("自动化模式"))
        features = QHBoxLayout()
        features.setSpacing(12)
        realtime = FeatureCard("实时触发", "持续识别农场任务，并立即在游戏遮罩中显示；不会自动控制游戏。", "配置监控")
        independent = FeatureCard("独立任务", "由你指定一个任务，跳过任务类型判断，直接执行对应路线。", "选择任务")
        automatic = FeatureCard("全自动", "识别当前任务并完成一轮执行；启动时会同时开启上方遮罩服务，结束后继续实时识别。", "开始全自动", badge="推荐", accent=True)
        self.automatic_card = automatic
        self.feature_cards = (realtime, independent, automatic)
        realtime.button.clicked.connect(lambda: self.navigate_requested.emit("realtime"))
        independent.button.clicked.connect(lambda: self.navigate_requested.emit("independent"))
        automatic.button.clicked.connect(self._start_automatic)
        for card in (realtime, independent, automatic):
            features.addWidget(card, 1)
        layout.addLayout(features)

        layout.addWidget(section_title("当前运行"))
        run_card = Card()
        top = QHBoxLayout()
        self.run_title = QLabel("没有正在运行的任务")
        self.run_title.setObjectName("CardTitle")
        self.run_pill = StatusPill("空闲", "success")
        top.addWidget(self.run_title)
        top.addStretch(1)
        top.addWidget(self.run_pill)
        self.run_detail = QLabel("从上方选择一种模式开始。实时识别会在动作期间暂停并由任务接管，动作结束后恢复。")
        self.run_detail.setObjectName("Muted")
        self.run_detail.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        run_card.body.addLayout(top)
        run_card.body.addWidget(self.run_detail)
        run_card.body.addWidget(self.progress)
        layout.addWidget(run_card)
        layout.addStretch(1)
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll)
        self._state = "idle"
        self._active_mode = ""
        self._realtime_active = False
        self._monitor_requested = False
        self._monitor_transitioning = False
        self._realtime_launch_enabled = False
        self._action_launch_enabled = False

    def _toggle_realtime(self) -> None:
        if self._monitor_requested:
            self.realtime_stop_requested.emit()
            return
        self.realtime_start_requested.emit(
            {
                "mode": "realtime",
                "continuous": True,
                "overlay": True,
            }
        )

    def _start_automatic(self) -> None:
        self.automatic_start_requested.emit(
            {
                "mode": "automatic",
                "continuous": False,
                "max_tasks": 1,
                "overlay": True,
                "dry_run": False,
            }
        )

    def update_preflight(self, checks: list[dict]) -> None:
        for check in checks:
            check_id = str(check.get("id", ""))
            card = self.metrics.get(check_id)
            if card is None:
                continue
            ok = bool(check.get("ok", False))
            detail = str(check.get("detail", "正常" if ok else "异常"))
            display = detail
            if check_id == "model":
                display = Path(detail).name or detail
            elif check_id == "window" and " (HWND " in detail:
                display = detail.split(" (HWND ", 1)[0]
            card.set_status(
                display,
                "success" if ok else ("danger" if check.get("blocking") else "warning"),
                tooltip=detail if display != detail else None,
            )

    def set_launch_enabled(self, *, realtime: bool, actions: bool) -> None:
        self._realtime_launch_enabled = bool(realtime)
        self._action_launch_enabled = bool(actions)
        self._apply_button_state()

    def set_monitor_service_state(self, requested: bool, transitioning: bool) -> None:
        self._monitor_requested = bool(requested)
        self._monitor_transitioning = bool(transitioning)
        self._apply_button_state()

    def _apply_button_state(self) -> None:
        stable = self._state in {"running", "paused"}
        actual_active = self._active_mode in {"realtime", "automatic", "independent"} and stable
        self.automatic_card.button.setEnabled(self._action_launch_enabled)
        if self._monitor_requested:
            self.overlay_start_button.setEnabled(not self._monitor_transitioning and actual_active)
            if self._monitor_transitioning:
                self.overlay_start_button.setText("切换中")
                self.launch_title.setText("游戏遮罩与实时识别，正在切换")
            elif self._active_mode == "realtime":
                self.overlay_start_button.setText("停止")
                self.launch_title.setText("游戏遮罩与实时识别，运行中")
            else:
                self.overlay_start_button.setText("关闭")
                self.launch_title.setText("游戏遮罩与实时识别，由动作任务接管")
        else:
            self.overlay_start_button.setEnabled(self._realtime_launch_enabled)
            self.overlay_start_button.setText("启动")
            self.launch_title.setText("游戏遮罩与实时识别")

    def update_state(self, state: str, payload: dict) -> None:
        self._state = str(state)
        mode_value = str(payload.get("mode", ""))
        if mode_value:
            self._active_mode = mode_value
            self._realtime_active = mode_value == "realtime"
        if state in {"idle", "error"}:
            self._active_mode = ""
            self._realtime_active = False
        names = {
            "idle": "空闲",
            "starting": "正在启动",
            "running": "运行中",
            "paused": "已暂停",
            "stopping": "正在停止",
            "completed": "已完成",
            "error": "运行错误",
        }
        tone = "success" if state in {"idle", "completed"} else "danger" if state == "error" else "accent"
        text = names.get(state, state)
        self.metrics["runtime"].set_status(text, tone)
        self.run_pill.setText(text)
        self.run_pill.set_tone(tone)
        active = state in {"starting", "running", "paused", "stopping"}
        self._apply_button_state()
        mode_names = {"realtime": "实时触发", "independent": "独立任务", "automatic": "全自动"}
        current_mode = mode_value or self._active_mode
        mode = mode_names.get(current_mode, current_mode)
        task_id = payload.get("task_id")
        self.run_title.setText(f"{mode or '任务'} · {task_id or '运行流程'}" if active else "没有正在运行的任务")
        self.run_detail.setText("worker 正在处理任务命令，运行日志会实时显示在日志页。" if active else "从上方选择一种模式开始。")
        self.progress.setVisible(active and state != "paused")
