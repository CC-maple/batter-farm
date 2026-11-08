from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QListWidget, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget

from src.ui.widgets.common import Card, PageHeader, StatusPill, section_title


class RealtimePage(QWidget):
    start_requested = Signal(dict)
    stop_requested = Signal()
    overlay_visibility_requested = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(18)
        layout.addWidget(PageHeader("实时触发", "先开始监控，再持续识别画面并更新游戏遮罩。该模式只提示，不发送键鼠输入。"))

        control = Card(accent=True)
        heading = QHBoxLayout()
        title = QLabel("实时任务识别")
        title.setObjectName("CardTitle")
        self.status = StatusPill("已停止", "neutral")
        heading.addWidget(title)
        heading.addStretch(1)
        heading.addWidget(self.status)
        copy = QLabel("点击“开始监控”后持续显示识别结果；动作任务会暂时接管同一遮罩，结束后自动恢复监控。")
        copy.setObjectName("CardDescription")
        copy.setWordWrap(True)
        buttons = QHBoxLayout()
        self.start_button = QPushButton("开始监控")
        self.start_button.setObjectName("PrimaryButton")
        self.stop_button = QPushButton("停止监控")
        self.stop_button.setObjectName("DangerButton")
        self.stop_button.setEnabled(False)
        self._monitor_active = False
        self._monitor_requested = False
        self._monitor_transitioning = False
        self._active_mode = ""
        self._state = "idle"
        self._launch_enabled = False
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self.stop_requested)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        control.body.addLayout(heading)
        control.body.addWidget(copy)
        control.body.addLayout(buttons)
        layout.addWidget(control)

        columns = QHBoxLayout()
        self.settings_card = Card()
        self.settings_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.settings_card.body.addWidget(section_title("识别稳定性"))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        self.confidence = QDoubleSpinBox()
        self.confidence.setRange(0.05, 1.0)
        self.confidence.setSingleStep(0.05)
        self.confidence.setValue(0.60)
        self.confidence.setDecimals(2)
        self.confirm_frames = QSpinBox()
        self.confirm_frames.setRange(1, 10)
        self.confirm_frames.setValue(2)
        self.show_boxes = QCheckBox("监控时显示游戏遮罩")
        self.show_boxes.setChecked(True)
        self.show_boxes.toggled.connect(self._overlay_selection_changed)
        self.save_history = QCheckBox("保存识别截图")
        form.addRow("最低置信度", self.confidence)
        form.addRow("连续确认帧数", self.confirm_frames)
        form.addRow("遮罩", self.show_boxes)
        form.addRow("记录", self.save_history)
        self.settings_card.body.addLayout(form)
        self.overlay_help = QLabel("需先开始监控；检测框在最后一次稳定识别后保留约 3 秒，状态卡片在监控期间持续显示。")
        self.overlay_help.setObjectName("Muted")
        self.overlay_help.setWordWrap(True)
        self.settings_card.body.addWidget(self.overlay_help)

        self.recent_card = Card()
        self.recent_card.body.addWidget(section_title("最近识别"))
        self.recent_list = QListWidget()
        self.recent_list.addItems(["尚无识别记录", "开始监控后将在此显示任务与置信度"])
        self.recent_card.body.addWidget(self.recent_list)
        columns.addWidget(self.settings_card, 1, Qt.AlignmentFlag.AlignTop)
        columns.addWidget(self.recent_card, 2)
        layout.addLayout(columns, 1)

    def _start(self) -> None:
        self.start_requested.emit(
            {
                "mode": "realtime",
                "continuous": True,
                "overlay": self.show_boxes.isChecked(),
                "detector_confidence": self.confidence.value(),
            }
        )

    def _overlay_selection_changed(self, visible: bool) -> None:
        if (
            self._monitor_requested
            and not self._monitor_transitioning
            and self._active_mode in {"realtime", "automatic", "independent"}
            and self._state in {"running", "paused"}
        ):
            self.overlay_visibility_requested.emit(bool(visible))

    def set_overlay_visible(self, visible: bool) -> None:
        blocker = QSignalBlocker(self.show_boxes)
        self.show_boxes.setChecked(bool(visible))
        del blocker

    def set_launch_enabled(self, enabled: bool) -> None:
        self._launch_enabled = bool(enabled)
        self._apply_button_state()

    def set_monitor_service_state(self, requested: bool, transitioning: bool) -> None:
        self._monitor_requested = bool(requested)
        self._monitor_transitioning = bool(transitioning)
        self._apply_button_state()

    def _apply_button_state(self) -> None:
        stable = self._state in {"running", "paused"}
        actual_active = self._active_mode in {"realtime", "automatic", "independent"} and stable
        self.start_button.setEnabled(self._launch_enabled)
        self.stop_button.setEnabled(self._monitor_requested and not self._monitor_transitioning and actual_active)
        self.show_boxes.setEnabled(not self._monitor_transitioning)
        if not self._monitor_requested:
            self.status.setText("已停止")
            self.status.set_tone("neutral")
        elif self._monitor_transitioning:
            self.status.setText("切换中")
            self.status.set_tone("accent")
        elif self._active_mode == "realtime":
            self.status.setText("已暂停" if self._state == "paused" else "监控中")
            self.status.set_tone("accent")
        else:
            self.status.setText("任务接管")
            self.status.set_tone("accent")

    def update_state(self, state: str, payload: dict) -> None:
        self._state = str(state)
        mode = str(payload.get("mode", ""))
        if mode:
            self._active_mode = mode
            self._monitor_active = mode == "realtime"
        if state in {"idle", "error"}:
            self._active_mode = ""
            self._monitor_active = False
        self._apply_button_state()

    def add_detection(self, marker: str, confidence: float) -> None:
        if self.recent_list.count() and self.recent_list.item(0).text() == "尚无识别记录":
            self.recent_list.clear()
        self.recent_list.insertItem(0, f"{marker}    {confidence:.0%}")
