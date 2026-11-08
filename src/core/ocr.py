from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

import numpy as np


RegionTuple = tuple[int, int, int, int]


@dataclass(frozen=True)
class OcrTextMatch:
    text: str
    similarity: float
    score: float | None
    box: tuple[tuple[int, int], ...]

    @property
    def center(self) -> tuple[int, int]:
        xs = [point[0] for point in self.box]
        ys = [point[1] for point in self.box]
        return int(round(sum(xs) / len(xs))), int(round(sum(ys) / len(ys)))


@dataclass(frozen=True)
class OcrTextLine:
    text: str
    score: float | None
    box: tuple[tuple[int, int], ...]


def normalize_ocr_region(value: Iterable[int] | None) -> RegionTuple:
    if value is None:
        raise ValueError("recognition.ocr_roi must be [x, y, w, h].")

    values = tuple(int(item) for item in value)
    if len(values) != 4:
        raise ValueError("recognition.ocr_roi must be [x, y, w, h].")

    x, y, width, height = values
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError("recognition.ocr_roi must use non-negative x/y and positive width/height.")
    return x, y, width, height


def crop_image(image: np.ndarray, region: RegionTuple) -> np.ndarray:
    x, y, width, height = region
    image_height, image_width = image.shape[:2]
    x2 = x + width
    y2 = y + height
    if x2 > image_width or y2 > image_height:
        raise ValueError(
            "recognition.ocr_roi exceeds screenshot bounds: "
            f"roi={(x, y, width, height)} screenshot={(image_width, image_height)}"
        )
    return image[y:y2, x:x2]


def normalize_text(text: str) -> str:
    return "".join(str(text).split())


def text_similarity(candidate: str, target: str) -> float:
    candidate_norm = normalize_text(candidate)
    target_norm = normalize_text(target)
    if not candidate_norm or not target_norm:
        return 0.0
    if candidate_norm == target_norm:
        return 1.0
    if target_norm in candidate_norm:
        return 0.98
    return SequenceMatcher(None, candidate_norm, target_norm).ratio()


def find_text_match(
    lines: Iterable[OcrTextLine],
    target_text: str,
    min_similarity: float,
) -> OcrTextMatch | None:
    if not 0.0 <= min_similarity <= 1.0:
        raise ValueError("recognition.min_similarity must be between 0 and 1.")

    best: OcrTextMatch | None = None
    for line in lines:
        similarity = text_similarity(line.text, target_text)
        if similarity < min_similarity:
            continue

        match = OcrTextMatch(
            text=line.text,
            similarity=similarity,
            score=line.score,
            box=line.box,
        )
        if best is None:
            best = match
            continue

        best_score = -1.0 if best.score is None else best.score
        match_score = -1.0 if match.score is None else match.score
        if (match.similarity, match_score) > (best.similarity, best_score):
            best = match

    return best


def find_text_contains(
    lines: Iterable[OcrTextLine],
    target_text: str,
) -> OcrTextLine | None:
    target_norm = normalize_text(target_text)
    if not target_norm:
        raise ValueError("target_text must not be empty.")

    for line in lines:
        if target_norm in normalize_text(line.text):
            return line
    return None


class OcrEngine:
    def __init__(self, engine_name: str = "rapidocr") -> None:
        self.engine_name = engine_name.lower()
        self.engine = self._create_engine()

    def recognize(self, image: np.ndarray) -> list[OcrTextLine]:
        raw_result = self.engine(image)
        return list(_extract_lines(raw_result))

    def _create_engine(self) -> Any:
        if self.engine_name == "paddleocr":
            try:
                from paddleocr import PaddleOCR
            except ImportError as exc:
                raise RuntimeError(
                    "PaddleOCR is not installed. Install paddleocr or set recognition.ocr_engine to rapidocr."
                ) from exc
            engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
            return lambda image: engine.ocr(image, cls=True)

        try:
            from rapidocr import RapidOCR

            return RapidOCR()
        except ImportError:
            pass

        try:
            from rapidocr_onnxruntime import RapidOCR

            return RapidOCR()
        except ImportError as exc:
            raise RuntimeError(
                "RapidOCR is not installed. Install dependencies with: pip install -r requirements.txt"
            ) from exc


def _extract_lines(raw_result: Any) -> Iterable[OcrTextLine]:
    if raw_result is None:
        return []

    if hasattr(raw_result, "boxes") and hasattr(raw_result, "txts"):
        boxes = getattr(raw_result, "boxes")
        texts = getattr(raw_result, "txts")
        scores = getattr(raw_result, "scores", None)
        boxes = [] if boxes is None else boxes
        texts = [] if texts is None else texts
        scores = [None] * len(texts) if scores is None else scores
        return [
            OcrTextLine(text=str(text), score=_to_optional_float(score), box=_normalize_box(box))
            for box, text, score in zip(boxes, texts, scores)
        ]

    if isinstance(raw_result, tuple):
        first = raw_result[0] if raw_result else []
        return _extract_lines(first)

    if isinstance(raw_result, list):
        lines: list[OcrTextLine] = []
        for item in raw_result:
            parsed = _parse_ocr_item(item)
            if parsed is not None:
                lines.append(parsed)
        return lines

    return []


def _parse_ocr_item(item: Any) -> OcrTextLine | None:
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None

    box = item[0]
    text = item[1]
    score = item[2] if len(item) >= 3 else None

    if isinstance(text, (list, tuple)) and text:
        score = text[1] if len(text) >= 2 else score
        text = text[0]

    try:
        return OcrTextLine(
            text=str(text),
            score=_to_optional_float(score),
            box=_normalize_box(box),
        )
    except (TypeError, ValueError):
        return None


def _normalize_box(box: Any) -> tuple[tuple[int, int], ...]:
    points: list[tuple[int, int]] = []
    for point in box:
        if not isinstance(point, (list, tuple, np.ndarray)) or len(point) < 2:
            raise ValueError("OCR box point must be [x, y].")
        points.append((int(round(float(point[0]))), int(round(float(point[1])))))

    if not points:
        raise ValueError("OCR box must contain at least one point.")
    return tuple(points)


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
