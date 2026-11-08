from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

ACTION_LABELS = {
    "click_until_gone": "触发点击",
    "replay": "路线回放",
    "processor": "加工流程",
    "restaurant_service": "餐厅流程",
}


def action_label(value: object) -> str:
    action = str(value or "route")
    return ACTION_LABELS.get(action, action.replace("_", " "))


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        title_label = QLabel(title)
        title_label.setObjectName("PageTitle")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("PageSubtitle")
        subtitle_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)


class ElidedLabel(QLabel):
    """Single-line label that cannot force a card wider than its container."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        self._full_text = ""
        self._tooltip_text: str | None = None
        super().__init__("", parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        self._full_text = str(text)
        self._tooltip_text = None
        self._refresh_text()

    def set_tooltip_text(self, text: str | None) -> None:
        self._tooltip_text = None if text in (None, "") else str(text)
        self._refresh_text()

    def full_text(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._refresh_text()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        hint = super().sizeHint()
        hint.setWidth(min(hint.width(), 260))
        return hint

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        hint = super().minimumSizeHint()
        hint.setWidth(40)
        return hint

    def _refresh_text(self) -> None:
        width = max(40, self.contentsRect().width())
        elided = self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideMiddle, width)
        super().setText(elided)
        self.setToolTip(self._tooltip_text or (self._full_text if elided != self._full_text else ""))


class Card(QFrame):
    def __init__(self, *, accent: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AccentCard" if accent else "Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 17, 18, 17)
        self.body.setSpacing(10)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)


class StatusPill(QLabel):
    TONES = {"success", "warning", "danger", "neutral", "accent"}

    def __init__(self, text: str, tone: str = "neutral", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("StatusPill")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        normalized = tone if tone in self.TONES else "neutral"
        if self.property("tone") == normalized:
            return
        self.setProperty("tone", normalized)
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()


class MetricCard(Card):
    def __init__(self, label: str, value: str = "检查中", tone: str = "neutral", parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.setObjectName("MetricCard")
        top = QHBoxLayout()
        title = QLabel(label)
        title.setObjectName("Muted")
        self.pill = StatusPill("●", tone)
        self.pill.setFixedWidth(30)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(self.pill)
        self.value = ElidedLabel(value)
        self.value.setObjectName("MetricValue")
        self.body.addLayout(top)
        self.body.addWidget(self.value)

    def set_status(self, value: str, tone: str, *, tooltip: str | None = None) -> None:
        self.value.setText(value)
        self.value.set_tooltip_text(tooltip)
        self.pill.set_tone(tone)


class FeatureCard(Card):
    def __init__(
        self,
        title: str,
        description: str,
        button_text: str,
        *,
        badge: str | None = None,
        accent: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(accent=accent, parent=parent)
        heading = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("CardTitle")
        heading.addWidget(title_label)
        heading.addStretch(1)
        if badge:
            heading.addWidget(StatusPill(badge, "accent"))
        description_label = QLabel(description)
        description_label.setObjectName("CardDescription")
        description_label.setWordWrap(True)
        description_label.setMinimumHeight(38)
        self.button = QPushButton(button_text)
        if accent:
            self.button.setObjectName("PrimaryButton")
        self.body.addLayout(heading)
        self.body.addWidget(description_label)
        self.body.addStretch(1)
        self.body.addWidget(self.button, 0, Qt.AlignmentFlag.AlignLeft)


def section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label
