from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


RegionTuple = tuple[int, int, int, int]


@dataclass(frozen=True)
class DetectionMatch:
    name: str
    class_id: int
    score: float
    top_left: tuple[int, int]
    size: tuple[int, int]

    @property
    def center(self) -> tuple[int, int]:
        x, y = self.top_left
        width, height = self.size
        return x + width // 2, y + height // 2

    @property
    def bottom_right(self) -> tuple[int, int]:
        x, y = self.top_left
        width, height = self.size
        return x + width, y + height


class FarmIconDetector:
    def __init__(self, config: dict[str, Any], root: str | Path) -> None:
        self.config = config
        self.root = Path(root)
        self.model_path = self._resolve_path(config.get("model_path", "models/farm-icons/best.onnx"))
        self.labels_path = self._resolve_path(config.get("labels_path", "models/farm-icons/labels.txt"))
        self.input_size = int(config.get("input_size", config.get("imgsz", 960)))
        self.score_threshold = float(config.get("score_threshold", config.get("conf_threshold", 0.25)))
        self.nms_threshold = float(config.get("nms_threshold", config.get("iou_threshold", 0.45)))
        self.device = str(config.get("device", "cpu"))
        self.class_thresholds = self._load_class_thresholds(config.get("classes", {}))
        self.label_names = self._load_labels()
        self._model: Any | None = None
        self._model_names: dict[int, str] = {}

    @property
    def class_names(self) -> set[str]:
        names = set(self.label_names.values())
        names.update(self.class_thresholds)
        names.update(self._model_names.values())
        return {name for name in names if name}

    def load_model_classes(self) -> dict[int, str]:
        """Load the configured model and return its declared class mapping."""

        self._ensure_model()
        return dict(self._model_names)

    def detect(
        self,
        image: np.ndarray,
        names: Iterable[str] | None = None,
        ignore_regions: Iterable[RegionTuple] | None = None,
    ) -> list[DetectionMatch]:
        model = self._ensure_model()
        requested_names = None if names is None else {str(name) for name in names}
        ignored = tuple(ignore_regions or ())

        results = model.predict(
            source=image,
            imgsz=self.input_size,
            batch=1,
            conf=self.score_threshold,
            iou=self.nms_threshold,
            device=self.device,
            save=False,
            exist_ok=True,
            verbose=False,
        )
        if not results:
            return []

        matches: list[DetectionMatch] = []
        for box in results[0].boxes:
            class_id = int(box.cls[0].item())
            name = self._class_name(class_id)
            if requested_names is not None and name not in requested_names:
                continue

            score = float(box.conf[0].item())
            min_score = self.class_thresholds.get(name, self.score_threshold)
            if score < min_score:
                logging.info(
                    "detection ignored class=%s score=%.4f reason=below_min_score min_score=%.4f",
                    name,
                    score,
                    min_score,
                )
                continue

            x0, y0, x1, y1 = [int(round(float(value))) for value in box.xyxy[0].tolist()]
            width = max(1, x1 - x0)
            height = max(1, y1 - y0)
            match = DetectionMatch(
                name=name,
                class_id=class_id,
                score=score,
                top_left=(max(0, x0), max(0, y0)),
                size=(width, height),
            )
            if _point_in_regions(match.center, ignored):
                logging.info(
                    "detection ignored class=%s score=%.4f center=%s reason=ignore_region",
                    name,
                    score,
                    match.center,
                )
                continue

            logging.info(
                "detection matched class=%s score=%.4f center=%s box=%s min_score=%.4f",
                name,
                score,
                match.center,
                (x0, y0, x1, y1),
                min_score,
            )
            matches.append(match)

        return matches

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        if not self.model_path.exists():
            raise FileNotFoundError(f"YOLO model not found: {self.model_path}")

        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError(
                "Ultralytics is not installed in this Python environment. "
                f"Current Python: {sys.executable}"
            ) from exc

        logging.info("loading farm icon detector model=%s device=%s", self.model_path, self.device)
        self._model = YOLO(str(self.model_path), task="detect")
        self._model_names = {int(key): str(value) for key, value in self._model.names.items()}
        logging.info("farm icon detector classes=%s", self._model_names)
        return self._model

    def _class_name(self, class_id: int) -> str:
        return self._model_names.get(class_id) or self.label_names.get(class_id) or str(class_id)

    def _resolve_path(self, value: Any) -> Path:
        path = Path(str(value))
        if path.is_absolute():
            return path
        return self.root / path

    def _load_labels(self) -> dict[int, str]:
        if not self.labels_path.exists():
            return {}
        labels: dict[int, str] = {}
        for index, line in enumerate(self.labels_path.read_text(encoding="utf-8").splitlines()):
            name = line.strip()
            if name:
                labels[index] = name
        return labels

    def _load_class_thresholds(self, raw_classes: Any) -> dict[str, float]:
        thresholds: dict[str, float] = {}
        if not isinstance(raw_classes, dict):
            return thresholds
        for raw_name, raw_config in raw_classes.items():
            name = str(raw_name).strip()
            if not name:
                continue
            if isinstance(raw_config, dict):
                raw_value = raw_config.get("min_score", raw_config.get("score_threshold", self.score_threshold))
            else:
                raw_value = raw_config
            thresholds[name] = float(raw_value)
        return thresholds


def create_farm_icon_detector(config: dict[str, Any], root: str | Path) -> FarmIconDetector | None:
    detector_config = config.get("detection", {})
    if not isinstance(detector_config, dict) or not bool(detector_config.get("enabled", False)):
        return None
    return FarmIconDetector(detector_config, root)


def _point_in_regions(point: tuple[int, int], regions: Iterable[RegionTuple]) -> bool:
    x, y = point
    for left, top, width, height in regions:
        if left <= x <= left + width and top <= y <= top + height:
            return True
    return False
