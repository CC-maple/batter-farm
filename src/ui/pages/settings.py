from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget

from src.ui.widgets.common import Card, PageHeader, StatusPill, section_title


class SettingsPage(QWidget):
    def __init__(self, root: Path, config_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(18)
        layout.addWidget(PageHeader("设置", "常用运行参数与环境信息。当前版本仅应用到本次运行，不自动改写 config.yaml。"))

        columns = QHBoxLayout()
        environment = Card()
        environment.body.addWidget(section_title("运行环境"))
        env_form = QFormLayout()
        env_form.setHorizontalSpacing(18)
        env_form.setVerticalSpacing(12)
        python = QLineEdit(sys.executable)
        python.setReadOnly(True)
        python.setCursorPosition(0)
        python.setToolTip(sys.executable)
        project = QLineEdit(str(root))
        project.setReadOnly(True)
        project.setCursorPosition(0)
        project.setToolTip(str(root))
        config = QLineEdit(str(config_path))
        config.setReadOnly(True)
        config.setCursorPosition(0)
        config.setToolTip(str(config_path))
        env_form.addRow("Python", python)
        env_form.addRow("项目目录", project)
        env_form.addRow("配置文件", config)
        environment.body.addLayout(env_form)

        behavior = Card()
        behavior.body.addWidget(section_title("默认行为"))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        mode = QComboBox()
        mode.addItems(["全自动（单轮）", "实时触发", "独立任务"])
        self.recognition_fps = QSpinBox()
        self.recognition_fps.setRange(1, 30)
        self.recognition_fps.setValue(8)
        self.recognition_fps.setSuffix(" FPS")
        self.recognition_fps.setToolTip("每秒执行的识别帧数")
        overlay = QCheckBox("默认显示遮罩")
        overlay.setChecked(True)
        topmost = QCheckBox("主窗口置顶")
        form.addRow("启动模式", mode)
        form.addRow("识别频率", self.recognition_fps)
        form.addRow("游戏反馈", overlay)
        form.addRow("界面", topmost)
        behavior.body.addLayout(form)

        columns.addWidget(environment, 3)
        columns.addWidget(behavior, 2)
        layout.addLayout(columns)

        notice = Card(accent=True)
        row = QHBoxLayout()
        text = QLabel("默认配置保存功能尚未接入；当前修改只影响本次运行，不会改写 config.yaml。")
        text.setWordWrap(True)
        row.addWidget(StatusPill("安全默认", "accent"))
        row.addWidget(text, 1)
        save = QPushButton("保存默认配置")
        save.setEnabled(False)
        row.addWidget(save)
        notice.body.addLayout(row)
        layout.addWidget(notice)
        layout.addStretch(1)
