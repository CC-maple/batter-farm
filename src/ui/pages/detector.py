from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget

from src.ui.widgets.common import Card, PageHeader, StatusPill, section_title


class DetectorPage(QWidget):
    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = root
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(18)
        layout.addWidget(PageHeader("识别测试", "选择模型和图片进行离线校准；测试参数可作为本次运行参数。"))

        columns = QHBoxLayout()
        self.settings_card = Card()
        self.settings_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.settings_card.body.addWidget(section_title("检测参数"))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        self.model_path = QLineEdit("models/farm-icons/best.onnx")
        self.model_path.setCursorPosition(0)
        self.model_path.setToolTip(self.model_path.text())
        browse_model = QPushButton("浏览")
        browse_model.clicked.connect(self._choose_model)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_path, 1)
        model_row.addWidget(browse_model)
        model_label = QLabel("模型")
        self.confidence = QDoubleSpinBox()
        self.confidence.setRange(0.05, 1.0)
        self.confidence.setValue(0.60)
        self.confidence.setSingleStep(0.05)
        self.iou = QDoubleSpinBox()
        self.iou.setRange(0.05, 1.0)
        self.iou.setValue(0.45)
        self.iou.setSingleStep(0.05)
        self.imgsz = QSpinBox()
        self.imgsz.setRange(320, 2048)
        self.imgsz.setSingleStep(32)
        self.imgsz.setValue(960)
        self.device = QComboBox()
        self.device.addItems(["CPU", "CUDA", "自动"])
        form.addRow("置信度", self.confidence)
        form.addRow("IoU", self.iou)
        form.addRow("图片尺寸", self.imgsz)
        form.addRow("设备", self.device)
        self.settings_card.body.addWidget(model_label)
        self.settings_card.body.addLayout(model_row)
        self.settings_card.body.addLayout(form)

        self.preview_card = Card(accent=True)
        top = QHBoxLayout()
        top.addWidget(section_title("测试图片"))
        top.addStretch(1)
        top.addWidget(StatusPill("离线工具", "accent"))
        self.image_path = QLineEdit()
        self.image_path.setPlaceholderText("选择一张游戏截图……")
        choose = QPushButton("选择图片")
        choose.clicked.connect(self._choose_image)
        row = QHBoxLayout()
        row.addWidget(self.image_path, 1)
        row.addWidget(choose)
        placeholder = QLabel("检测结果预览\n\n运行测试后将在这里显示检测框、类别、置信度和推理耗时。")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setObjectName("Muted")
        placeholder.setMinimumHeight(210)
        placeholder.setStyleSheet("border: 1px dashed #3A4B68; border-radius: 8px; background: #101827;")
        run = QPushButton("运行识别测试")
        run.setObjectName("PrimaryButton")
        run.setEnabled(False)
        self.preview_card.body.addLayout(top)
        self.preview_card.body.addLayout(row)
        self.preview_card.body.addWidget(placeholder, 1)
        self.preview_card.body.addWidget(run)
        columns.addWidget(self.settings_card, 2, Qt.AlignmentFlag.AlignTop)
        columns.addWidget(self.preview_card, 3)
        layout.addLayout(columns, 1)

    def _choose_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择检测模型", str(self.root / "models"), "模型 (*.onnx *.pt)")
        if path:
            self.model_path.setText(path)

    def _choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择测试图片", str(self.root), "图片 (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self.image_path.setText(path)
