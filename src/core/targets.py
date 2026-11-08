from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from src.core.ocr import OcrEngine, crop_image, normalize_text
from src.core.recognition import TemplateImage, load_template, match_template


RegionTuple = tuple[int, int, int, int]
ALLOWED_ROLES = {
    "entry",
    "in_farm",
    "success",
    "launcher_target",
    "launcher_prompt",
    "aim_mode",
    "running",
    "done",
    "observe",
    "dialog",
}


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    path: Path
    category: str = "farm"
    role: str = "observe"
    threshold: float = 0.85
    priority: int = 0
    roi: RegionTuple | None = None
    click: bool = False
    click_offset: tuple[int, int] = (0, 0)
    required: bool = True
    scales: tuple[float, ...] = (1.0,)
    base_client_size: tuple[int, int] | None = None


@dataclass(frozen=True)
class OcrTargetSpec:
    name: str
    text: str
    category: str = "farm"
    role: str = "observe"
    min_score: float = 0.90
    priority: int = 0
    roi: RegionTuple = (0, 0, 1, 1)
    click: bool = False
    click_point: tuple[int, int] | None = None
    click_offset: tuple[int, int] = (0, 0)
    base_client_size: tuple[int, int] | None = None


@dataclass(frozen=True)
class LoadedTemplate:
    spec: TemplateSpec
    template: TemplateImage


@dataclass(frozen=True)
class TargetMatch:
    spec: TemplateSpec | OcrTargetSpec
    score: float
    top_left: tuple[int, int]
    size: tuple[int, int]

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def role(self) -> str:
        return self.spec.role

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

    @property
    def click_point(self) -> tuple[int, int]:
        if isinstance(self.spec, OcrTargetSpec) and self.spec.click_point is not None:
            return self.spec.click_point
        x, y = self.center
        ox, oy = self.spec.click_offset
        return x + ox, y + oy


def parse_template_specs(config: dict[str, Any], root: str | Path) -> list[TemplateSpec]:
    recognition = config.get("recognition", {})
    default_threshold = float(recognition.get("default_threshold", recognition.get("threshold", 0.85)))
    root_path = Path(root)
    specs: list[TemplateSpec] = []
    for raw in recognition.get("templates", []):
        specs.append(_parse_one_spec(raw, default_threshold, root_path))
    return specs


def parse_ocr_target_specs(config: dict[str, Any]) -> list[OcrTargetSpec]:
    recognition = config.get("recognition", {})
    raw_targets = recognition.get("ocr_targets", []) if isinstance(recognition, dict) else []
    if not isinstance(raw_targets, list):
        raise ValueError("recognition.ocr_targets must be a list.")

    specs: list[OcrTargetSpec] = []
    names: set[str] = set()
    for raw in raw_targets:
        if not isinstance(raw, dict):
            raise ValueError("recognition.ocr_targets entries must be objects.")
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("OCR target name must not be empty.")
        if name in names:
            raise ValueError(f"duplicate OCR target name: {name!r}")
        names.add(name)

        text = normalize_text(str(raw.get("text", "")))
        if not text:
            raise ValueError(f"OCR target {name!r} text must not be empty.")
        min_score = float(raw.get("min_score", 0.90))
        if not 0.0 <= min_score <= 1.0:
            raise ValueError(f"OCR target {name!r} min_score must be between 0 and 1.")
        role = str(raw.get("role", "observe")).strip()
        if role not in ALLOWED_ROLES:
            raise ValueError(f"OCR target {name!r} role {role!r} is not supported.")
        roi = _normalize_roi(raw.get("roi"))
        if roi is None:
            raise ValueError(f"OCR target {name!r} roi is required.")
        click = bool(raw.get("click", False))
        click_point = _normalize_optional_point(raw.get("click_point"))
        if click and click_point is None:
            raise ValueError(f"OCR target {name!r} click_point is required when click is true.")

        specs.append(
            OcrTargetSpec(
                name=name,
                text=text,
                category=str(raw.get("category", "farm")),
                role=role,
                min_score=min_score,
                priority=int(raw.get("priority", 0)),
                roi=roi,
                click=click,
                click_point=click_point,
                base_client_size=_normalize_size(raw.get("base_client_size")),
            )
        )
    return specs


def _parse_one_spec(raw: dict[str, Any], default_threshold: float, root: Path) -> TemplateSpec:
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ValueError("template name must not be empty.")

    raw_path = raw.get("path")
    if not raw_path:
        raise ValueError(f"template {name!r} path must not be empty.")
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = root / path

    threshold = float(raw.get("threshold", default_threshold))
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"template {name!r} threshold must be between 0 and 1.")

    role = str(raw.get("role", "observe")).strip()
    if role not in ALLOWED_ROLES:
        raise ValueError(f"template {name!r} role {role!r} is not supported.")

    return TemplateSpec(
        name=name,
        path=path,
        category=str(raw.get("category", "farm")),
        role=role,
        threshold=threshold,
        priority=int(raw.get("priority", 0)),
        roi=_normalize_roi(raw.get("roi")),
        click=bool(raw.get("click", False)),
        click_offset=_normalize_offset(raw.get("click_offset")),
        required=bool(raw.get("required", True)),
        scales=_normalize_scales(raw.get("scales")),
        base_client_size=_normalize_size(raw.get("base_client_size")),
    )


def load_templates(specs: Iterable[TemplateSpec]) -> list[LoadedTemplate]:
    loaded: list[LoadedTemplate] = []
    for spec in specs:
        if not spec.path.exists() and not spec.required:
            logging.warning("optional template missing name=%s path=%s", spec.name, spec.path)
            continue
        try:
            loaded.append(LoadedTemplate(spec=spec, template=load_template(spec.path)))
        except FileNotFoundError:
            if spec.required:
                raise
            logging.warning("optional template unreadable name=%s path=%s", spec.name, spec.path)
    return loaded


def match_targets(
    image: np.ndarray,
    templates: Iterable[LoadedTemplate],
    ignore_regions: Iterable[RegionTuple] | None = None,
) -> list[TargetMatch]:
    matches: list[TargetMatch] = []
    normalized_ignore_regions = tuple(ignore_regions or ())
    for loaded in templates:
        spec = _scale_spec_for_image(loaded.spec, image)
        search_image, offset = _crop_roi(image, spec.roi)
        local_ignore_regions = _local_ignore_regions(normalized_ignore_regions, spec.roi)
        try:
            result = match_template(
                search_image,
                loaded.template,
                spec.threshold,
                scales=spec.scales,
                ignore_regions=local_ignore_regions,
            )
        except ValueError as exc:
            if spec.required:
                raise
            logging.warning("optional template skipped name=%s reason=%s", spec.name, exc)
            continue
        if result is None:
            logging.debug("template not matched name=%s role=%s threshold=%s", spec.name, spec.role, spec.threshold)
            continue
        ox, oy = offset
        match = TargetMatch(
            spec=spec,
            score=result.score,
            top_left=(result.top_left[0] + ox, result.top_left[1] + oy),
            size=result.size,
        )
        logging.info(
            "template matched name=%s role=%s score=%.4f threshold=%.4f center=%s click_point=%s priority=%s",
            match.name,
            match.role,
            match.score,
            spec.threshold,
            match.center,
            match.click_point,
            spec.priority,
        )
        if result.scale != 1.0:
            logging.info("template matched with scale name=%s scale=%.3f", match.name, result.scale)
        matches.append(match)
    return matches


def match_ocr_targets(
    image: np.ndarray,
    targets: Iterable[OcrTargetSpec],
    engine: OcrEngine,
) -> list[TargetMatch]:
    matches: list[TargetMatch] = []
    for raw_spec in targets:
        spec = _scale_ocr_spec_for_image(raw_spec, image)
        roi_left, roi_top, _, _ = spec.roi
        lines = engine.recognize(crop_image(image, spec.roi))
        exact_lines = [
            line
            for line in lines
            if normalize_text(line.text) == spec.text
            and line.score is not None
            and float(line.score) >= spec.min_score
        ]
        if not exact_lines:
            logging.debug(
                "OCR target not matched name=%s text=%r min_score=%.4f lines=%s",
                spec.name,
                spec.text,
                spec.min_score,
                [(line.text, line.score) for line in lines],
            )
            continue

        line = max(exact_lines, key=lambda item: float(item.score))
        xs = [point[0] for point in line.box]
        ys = [point[1] for point in line.box]
        left = min(xs)
        top = min(ys)
        right = max(xs)
        bottom = max(ys)
        match = TargetMatch(
            spec=spec,
            score=float(line.score),
            top_left=(roi_left + left, roi_top + top),
            size=(max(1, right - left), max(1, bottom - top)),
        )
        logging.info(
            "OCR target matched name=%s text=%r score=%.4f min_score=%.4f center=%s click_point=%s",
            match.name,
            line.text,
            match.score,
            spec.min_score,
            match.center,
            match.click_point,
        )
        matches.append(match)
    return matches


def filter_matches(
    matches: Iterable[TargetMatch],
    role: str | None = None,
    clickable: bool | None = None,
    names: Iterable[str] | None = None,
) -> list[TargetMatch]:
    name_set = None if names is None else set(names)
    filtered: list[TargetMatch] = []
    for match in matches:
        if role is not None and match.role != role:
            continue
        if clickable is not None and match.spec.click != clickable:
            continue
        if name_set is not None and match.name not in name_set:
            continue
        filtered.append(match)
    return filtered


def select_best_match(matches: Iterable[TargetMatch]) -> TargetMatch | None:
    ordered = sorted(matches, key=lambda item: (-item.spec.priority, -item.score, item.name))
    return ordered[0] if ordered else None


def _normalize_roi(value: Any) -> RegionTuple | None:
    if value in (None, "", "null"):
        return None
    values = tuple(int(item) for item in value)
    if len(values) != 4:
        raise ValueError("template roi must be [left, top, width, height].")
    left, top, width, height = values
    if left < 0 or top < 0 or width <= 0 or height <= 0:
        raise ValueError("template roi must use non-negative left/top and positive width/height.")
    return left, top, width, height


def _normalize_offset(value: Any) -> tuple[int, int]:
    if value in (None, ""):
        return 0, 0
    values = tuple(int(item) for item in value)
    if len(values) != 2:
        raise ValueError("template click_offset must be [x, y].")
    return values[0], values[1]


def _normalize_scales(value: Any) -> tuple[float, ...]:
    if value in (None, "", "null"):
        return (1.0,)
    if isinstance(value, (int, float)):
        scales = (float(value),)
    else:
        scales = tuple(float(item) for item in value)
    if not scales:
        return (1.0,)
    if any(scale <= 0 for scale in scales):
        raise ValueError("template scales must be positive.")
    return scales


def _normalize_size(value: Any) -> tuple[int, int] | None:
    if value in (None, "", "null"):
        return None
    values = tuple(int(item) for item in value)
    if len(values) != 2:
        raise ValueError("template base_client_size must be [width, height].")
    width, height = values
    if width <= 0 or height <= 0:
        raise ValueError("template base_client_size must use positive width/height.")
    return width, height


def _normalize_optional_point(value: Any) -> tuple[int, int] | None:
    if value in (None, "", "null"):
        return None
    values = tuple(int(item) for item in value)
    if len(values) != 2:
        raise ValueError("OCR target click_point must be [x, y].")
    x, y = values
    if x < 0 or y < 0:
        raise ValueError("OCR target click_point must use non-negative coordinates.")
    return x, y


def _scale_spec_for_image(spec: TemplateSpec, image: np.ndarray) -> TemplateSpec:
    if spec.base_client_size is None:
        return spec

    image_height, image_width = image.shape[:2]
    base_width, base_height = spec.base_client_size
    scale_x = image_width / base_width
    scale_y = image_height / base_height
    if abs(scale_x - scale_y) > 0.02:
        logging.warning(
            "template client aspect ratio differs from reference name=%s image=%s base=%s scale_x=%.4f scale_y=%.4f",
            spec.name,
            (image_width, image_height),
            spec.base_client_size,
            scale_x,
            scale_y,
        )
    template_scale = min(scale_x, scale_y)

    roi = spec.roi
    if roi is not None:
        left, top, width, height = roi
        roi = (
            max(0, int(round(left * scale_x))),
            max(0, int(round(top * scale_y))),
            max(1, int(round(width * scale_x))),
            max(1, int(round(height * scale_y))),
        )

    offset_x, offset_y = spec.click_offset
    return replace(
        spec,
        roi=roi,
        click_offset=(int(round(offset_x * scale_x)), int(round(offset_y * scale_y))),
        scales=tuple(scale * template_scale for scale in spec.scales),
    )


def _scale_ocr_spec_for_image(spec: OcrTargetSpec, image: np.ndarray) -> OcrTargetSpec:
    if spec.base_client_size is None:
        return spec

    image_height, image_width = image.shape[:2]
    base_width, base_height = spec.base_client_size
    scale_x = image_width / base_width
    scale_y = image_height / base_height
    if abs(scale_x - scale_y) > 0.02:
        logging.warning(
            "OCR target client aspect ratio differs from reference name=%s image=%s base=%s scale_x=%.4f scale_y=%.4f",
            spec.name,
            (image_width, image_height),
            spec.base_client_size,
            scale_x,
            scale_y,
        )

    left, top, width, height = spec.roi
    click_point = spec.click_point
    return replace(
        spec,
        roi=(
            max(0, int(round(left * scale_x))),
            max(0, int(round(top * scale_y))),
            max(1, int(round(width * scale_x))),
            max(1, int(round(height * scale_y))),
        ),
        click_point=(
            int(round(click_point[0] * scale_x)),
            int(round(click_point[1] * scale_y)),
        )
        if click_point is not None
        else None,
    )


def _crop_roi(image: np.ndarray, roi: RegionTuple | None) -> tuple[np.ndarray, tuple[int, int]]:
    if roi is None:
        return image, (0, 0)
    left, top, width, height = roi
    image_height, image_width = image.shape[:2]
    right = left + width
    bottom = top + height
    if left >= image_width or top >= image_height:
        raise ValueError(f"template roi starts outside screenshot roi={roi} screenshot={(image_width, image_height)}")

    clipped_right = min(right, image_width)
    clipped_bottom = min(bottom, image_height)
    if clipped_right <= left or clipped_bottom <= top:
        raise ValueError(f"template roi has no overlap with screenshot roi={roi} screenshot={(image_width, image_height)}")
    if clipped_right != right or clipped_bottom != bottom:
        logging.debug(
            "template roi clipped roi=%s clipped=%s screenshot=%s",
            roi,
            (left, top, clipped_right - left, clipped_bottom - top),
            (image_width, image_height),
        )
    return image[top:clipped_bottom, left:clipped_right], (left, top)


def _local_ignore_regions(ignore_regions: tuple[RegionTuple, ...], roi: RegionTuple | None) -> tuple[RegionTuple, ...]:
    if not ignore_regions:
        return ()
    if roi is None:
        return ignore_regions

    roi_left, roi_top, roi_width, roi_height = roi
    roi_right = roi_left + roi_width
    roi_bottom = roi_top + roi_height
    local_regions: list[RegionTuple] = []
    for left, top, width, height in ignore_regions:
        right = left + width
        bottom = top + height
        clipped_left = max(left, roi_left)
        clipped_top = max(top, roi_top)
        clipped_right = min(right, roi_right)
        clipped_bottom = min(bottom, roi_bottom)
        if clipped_left >= clipped_right or clipped_top >= clipped_bottom:
            continue
        local_regions.append(
            (
                clipped_left - roi_left,
                clipped_top - roi_top,
                clipped_right - clipped_left,
                clipped_bottom - clipped_top,
            )
        )
    return tuple(local_regions)
