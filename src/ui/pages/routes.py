from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ui.widgets.common import Card, ElidedLabel, PageHeader, StatusPill, action_label


class RoutesPage(QWidget):
    record_requested = Signal(dict)
    replay_requested = Signal(str)
    stop_tool_requested = Signal()
    custom_task_requested = Signal(str, str)

    BUSY_TOOL_STATES = {"countdown", "starting", "recording", "replaying", "stopping"}

    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = root
        self._selected_recording = ""
        self._tool_state = "idle"
        self._tool_payload: dict = {
            "detail": "录制 WASD、普通键鼠和点击；F5/F6 用于拖动转向，请先解除游戏内绑定"
        }
        self._automation_busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(14)
        layout.addWidget(PageHeader("任务与路线", "管理任务路线，并录制或回放完整键鼠操作。"))

        tools = Card(accent=True)
        tools_heading = QHBoxLayout()
        tools_title = QLabel("键鼠录制与回放")
        tools_title.setObjectName("SectionTitle")
        self.advanced_button = QPushButton("高级选项")
        self.advanced_button.setCheckable(True)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        self.tool_status = StatusPill("空闲", "neutral")
        tools_heading.addWidget(tools_title)
        tools_heading.addStretch(1)
        tools_heading.addWidget(self.advanced_button)
        tools_heading.addWidget(self.tool_status)
        tools.body.addLayout(tools_heading)

        self.tool_description = QLabel(
            "WASD、普通按键、鼠标移动、点击和滚轮都会记录；按住 F5 向左拖动视角，"
            "按住 F6 向右拖动视角。录制前请先解除游戏内 F5/F6 绑定；F10/ESC 保存结束。"
        )
        self.tool_description.setObjectName("CardDescription")
        self.tool_description.setWordWrap(True)
        tools.body.addWidget(self.tool_description)

        self.advanced_panel = QWidget()
        advanced = QGridLayout(self.advanced_panel)
        advanced.setContentsMargins(0, 2, 0, 2)
        advanced.setHorizontalSpacing(10)
        advanced.setVerticalSpacing(8)

        self.camera_mode = QComboBox()
        self.camera_mode.addItem("可见拖动", "visible")
        self.camera_mode.addItem("相对移动", "hidden")
        self.sample_rate = QSpinBox()
        self.sample_rate.setRange(10, 500)
        self.sample_rate.setValue(100)
        self.sample_rate.setSuffix(" Hz")
        self.sample_rate.setToolTip("每秒发送鼠标位移的次数，不是游戏画面 FPS")
        self.step_dx = QSpinBox()
        self.step_dx.setRange(1, 50)
        self.step_dx.setValue(4)
        self.step_dx.setSuffix(" px/次")
        self.countdown = QDoubleSpinBox()
        self.countdown.setRange(0.0, 15.0)
        self.countdown.setDecimals(1)
        self.countdown.setSingleStep(0.5)
        self.countdown.setValue(5.0)
        self.countdown.setSuffix(" 秒")
        self.anchor_x = QSpinBox()
        self.anchor_x.setRange(0, 4000)
        self.anchor_x.setValue(800)
        self.anchor_y = QSpinBox()
        self.anchor_y.setRange(0, 3000)
        self.anchor_y.setValue(450)
        self.segment_ms = QSpinBox()
        self.segment_ms.setRange(20, 2000)
        self.segment_ms.setValue(250)
        self.segment_ms.setSuffix(" ms")
        self.segment_gap_ms = QSpinBox()
        self.segment_gap_ms.setRange(0, 1000)
        self.segment_gap_ms.setValue(30)
        self.segment_gap_ms.setSuffix(" ms")
        self.invert_direction = QCheckBox("反转左右方向")

        fields = [
            ("转向方式", self.camera_mode),
            ("转向采样率", self.sample_rate),
            ("每步水平位移", self.step_dx),
            ("开始倒计时", self.countdown),
            ("拖动锚点 X", self.anchor_x),
            ("拖动锚点 Y", self.anchor_y),
            ("单段时长", self.segment_ms),
            ("段间隔", self.segment_gap_ms),
        ]
        for index, (label_text, field) in enumerate(fields):
            row = index // 4
            column = (index % 4) * 2
            label = QLabel(label_text)
            label.setObjectName("Muted")
            advanced.addWidget(label, row, column)
            advanced.addWidget(field, row, column + 1)
        advanced.addWidget(self.invert_direction, 2, 0, 1, 2)
        advanced.setColumnStretch(1, 1)
        advanced.setColumnStretch(3, 1)
        advanced.setColumnStretch(5, 1)
        advanced.setColumnStretch(7, 1)
        self.advanced_panel.setVisible(False)
        tools.body.addWidget(self.advanced_panel)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.record_button = QPushButton("开始键鼠录制")
        self.record_button.setObjectName("PrimaryButton")
        self.record_button.clicked.connect(self._request_recording)
        self.choose_button = QPushButton("选择文件")
        self.choose_button.clicked.connect(self._choose_recording)
        self.recording_path = ElidedLabel("尚未选择键鼠录制文件")
        self.recording_path.setObjectName("Muted")
        self.replay_button = QPushButton("回放键鼠录制")
        self.replay_button.clicked.connect(self._request_replay)
        self.stop_button = QPushButton("安全停止")
        self.stop_button.setObjectName("DangerButton")
        self.stop_button.clicked.connect(self.stop_tool_requested.emit)
        controls.addWidget(self.record_button)
        controls.addWidget(self.choose_button)
        controls.addWidget(self.recording_path, 1)
        controls.addWidget(self.replay_button)
        controls.addWidget(self.stop_button)
        tools.body.addLayout(controls)

        self.tool_detail = ElidedLabel("默认：记录完整键鼠输入；F5/F6 使用可见拖动转向，请先解除游戏内绑定")
        self.tool_detail.setObjectName("Muted")
        tools.body.addWidget(self.tool_detail)
        layout.addWidget(tools)

        summary = QHBoxLayout()
        title = QLabel("任务注册表")
        title.setObjectName("SectionTitle")
        self.count = StatusPill("0 个任务", "neutral")
        sync = StatusPill("自动同步", "neutral")
        self.add_task_button = QPushButton("添加自定义任务")
        self.add_task_button.clicked.connect(self._open_add_task_dialog)
        summary.addWidget(title)
        summary.addStretch(1)
        summary.addWidget(self.add_task_button)
        summary.addWidget(self.count)
        summary.addWidget(sync)
        layout.addLayout(summary)

        card = Card()
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["任务", "内部 ID", "动作", "识别标识", "路线来源", "独立运行"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(90)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        card.body.addWidget(self.table)
        layout.addWidget(card, 1)
        self._refresh_tool_controls()

    @property
    def selected_recording(self) -> str:
        return self._selected_recording

    def set_selected_recording(self, path: str | Path) -> None:
        normalized = str(Path(path).resolve()) if str(path) else ""
        self._selected_recording = normalized
        if normalized:
            self.recording_path.setText(Path(normalized).name)
            self.recording_path.set_tooltip_text(normalized)
        else:
            self.recording_path.setText("尚未选择键鼠录制文件")
            self.recording_path.set_tooltip_text(None)
        self._refresh_tool_controls()

    def set_tool_state(self, state: str, payload: dict | None = None) -> None:
        self._tool_state = str(state)
        self._tool_payload = dict(payload or {})
        recording = str(self._tool_payload.get("recording", ""))
        if recording and Path(recording).is_file():
            self.set_selected_recording(recording)
        self._refresh_tool_controls()

    def set_automation_busy(self, busy: bool) -> None:
        self._automation_busy = bool(busy)
        self._refresh_tool_controls()

    def _choose_recording(self) -> None:
        start_dir = self.root / "logs" / "recordings"
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择键鼠录制文件",
            str(start_dir),
            "键鼠录制文件 (recording_*.json);;旧视角录制文件 (mouse_input_*.json);;JSON 文件 (*.json)",
        )
        if path:
            self.set_selected_recording(path)

    def _request_replay(self) -> None:
        if self._selected_recording:
            self.replay_requested.emit(self._selected_recording)

    def _request_recording(self) -> None:
        self.record_requested.emit(self.recording_options())

    def _open_add_task_dialog(self) -> None:
        dialog = AddTaskDialog(self.root, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            name, route = dialog.values()
            self.custom_task_requested.emit(name, route)

    def recording_options(self) -> dict:
        return {
            "mode": str(self.camera_mode.currentData()),
            "sample_rate_hz": int(self.sample_rate.value()),
            "step_dx": int(self.step_dx.value()),
            "countdown_seconds": float(self.countdown.value()),
            "anchor_x": int(self.anchor_x.value()),
            "anchor_y": int(self.anchor_y.value()),
            "segment_ms": int(self.segment_ms.value()),
            "segment_gap_ms": int(self.segment_gap_ms.value()),
            "invert": bool(self.invert_direction.isChecked()),
        }

    def _toggle_advanced(self, expanded: bool) -> None:
        self.advanced_panel.setVisible(bool(expanded))
        self.advanced_button.setText("收起高级" if expanded else "高级选项")

    def _refresh_tool_controls(self) -> None:
        tool_busy = self._tool_state in self.BUSY_TOOL_STATES
        available = not tool_busy and not self._automation_busy
        self.record_button.setEnabled(available)
        self.choose_button.setEnabled(not tool_busy)
        self.replay_button.setEnabled(available and bool(self._selected_recording))
        self.stop_button.setEnabled(tool_busy)
        self.advanced_button.setEnabled(available)
        self.advanced_panel.setEnabled(available)
        self.add_task_button.setEnabled(available)

        if self._automation_busy and not tool_busy:
            label, tone = "自动任务运行中", "warning"
            detail = "录制和回放将在当前自动任务结束后可用"
        else:
            label, tone = {
                "idle": ("空闲", "neutral"),
                "countdown": (
                    f"{int(self._tool_payload.get('remaining_seconds', 0))} 秒",
                    "warning",
                ),
                "starting": ("准备中", "warning"),
                "recording": ("录制中", "success"),
                "replaying": ("回放中", "accent"),
                "stopping": ("停止中", "warning"),
                "completed": ("已完成", "success"),
                "stopped": ("已停止", "neutral"),
                "error": ("异常", "danger"),
            }.get(self._tool_state, (self._tool_state or "空闲", "neutral"))
            detail = str(
                self._tool_payload.get(
                    "detail",
                    "录制 WASD、普通键鼠和点击；F5/F6 用于拖动转向，请先解除游戏内绑定",
                )
            )
        self.tool_status.setText(label)
        self.tool_status.set_tone(tone)
        self.tool_detail.setText(detail)

    def set_tasks(self, tasks: list[dict]) -> None:
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            raw_action = str(task.get("action", ""))
            values = [
                task.get("name", ""),
                task.get("id", ""),
                action_label(raw_action),
                ", ".join(task.get("markers", [])),
                task.get("source") or "内置流程",
                "支持" if task.get("independent_supported") else "仅识别触发",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(raw_action if column == 2 else str(value))
                if column == 5:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
        self.count.setText(f"{len(tasks)} 个任务")
        self.count.set_tone("accent" if tasks else "neutral")


class AddTaskDialog(QDialog):
    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = root
        self.setWindowTitle("添加自定义回放任务")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        description = QLabel("填写任务名称并选择一份普通键鼠录制 JSON。保存后会同步到任务注册表和独立任务页。")
        description.setObjectName("CardDescription")
        description.setWordWrap(True)
        layout.addWidget(description)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：温泉收集")
        self.route_edit = QLineEdit()
        self.route_edit.setReadOnly(True)
        self.route_edit.setPlaceholderText("选择 recording_*.json 路线文件")
        route_row = QHBoxLayout()
        route_row.addWidget(self.route_edit, 1)
        choose = QPushButton("选择路线")
        choose.clicked.connect(self._choose_route)
        route_row.addWidget(choose)
        form.addRow("任务名称", self.name_edit)
        form.addRow("回放路线", route_row)
        layout.addLayout(form)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.name_edit.textChanged.connect(self._refresh_save)
        self.route_edit.textChanged.connect(self._refresh_save)
        self._refresh_save()

    def values(self) -> tuple[str, str]:
        return " ".join(self.name_edit.text().split()), self.route_edit.text().strip()

    def _choose_route(self) -> None:
        start_dir = self.root / "logs" / "recordings"
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择任务回放路线",
            str(start_dir),
            "普通键鼠路线 (recording_*.json);;JSON 文件 (*.json)",
        )
        if path:
            self.route_edit.setText(str(Path(path).resolve()))

    def _refresh_save(self) -> None:
        button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        button.setEnabled(bool(self.name_edit.text().strip() and self.route_edit.text().strip()))
