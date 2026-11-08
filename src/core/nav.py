from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from src.core.recognition import TemplateImage, load_template, match_template


RegionTuple = tuple[int, int, int, int]
PointTuple = tuple[int, int]


@dataclass(frozen=True)
class NavAnchorSpec:
    name: str
    path: Path
    roi: RegionTuple | None = None
    threshold: float = 0.78
    weight: float = 1.0
    expected_center: PointTuple | None = None
    max_center_offset: PointTuple | None = None
    image_size: PointTuple | None = None
    max_image_size_delta: PointTuple | None = None
    scale_with_image_size: bool = False
    scales: tuple[float, ...] = (1.0,)
    required: bool = False


@dataclass(frozen=True)
class NavNodeSpec:
    name: str
    anchors: tuple[NavAnchorSpec, ...]
    min_score: float = 0.70
    min_hits: int = 2
    score_mode: str = "average"
    enter_from: tuple[str, ...] = ()
    recover_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedNavAnchor:
    spec: NavAnchorSpec
    template: TemplateImage


@dataclass(frozen=True)
class LoadedNavNode:
    spec: NavNodeSpec
    anchors: tuple[LoadedNavAnchor, ...]


@dataclass(frozen=True)
class NavAnchorMatch:
    spec: NavAnchorSpec
    matched: bool
    skipped: bool = False
    raw_score: float = 0.0
    position_score: float = 0.0
    weighted_score: float = 0.0
    top_left: PointTuple | None = None
    size: PointTuple | None = None
    scale: float = 1.0
    message: str = ""

    @property
    def center(self) -> PointTuple | None:
        if self.top_left is None or self.size is None:
            return None
        return (
            self.top_left[0] + self.size[0] // 2,
            self.top_left[1] + self.size[1] // 2,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "matched": self.matched,
            "skipped": self.skipped,
            "raw_score": round(self.raw_score, 4),
            "position_score": round(self.position_score, 4),
            "weighted_score": round(self.weighted_score, 4),
            "center": list(self.center) if self.center is not None else None,
            "top_left": list(self.top_left) if self.top_left is not None else None,
            "size": list(self.size) if self.size is not None else None,
            "scale": round(self.scale, 4),
            "required": self.spec.required,
            "message": self.message,
        }


@dataclass(frozen=True)
class NavNodeMatch:
    spec: NavNodeSpec
    score: float
    hits: int
    required_ok: bool
    anchors: tuple[NavAnchorMatch, ...]

    @property
    def ok(self) -> bool:
        return self.required_ok and self.hits >= self.spec.min_hits and self.score >= self.spec.min_score

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "score": round(self.score, 4),
            "hits": self.hits,
            "min_hits": self.spec.min_hits,
            "min_score": self.spec.min_score,
            "required_ok": self.required_ok,
            "ok": self.ok,
            "anchors": [anchor.to_dict() for anchor in self.anchors],
        }


def parse_nav_nodes(config: dict[str, Any], root: str | Path) -> list[NavNodeSpec]:
    root_path = Path(root)
    nav_config = config.get("nav", {})
    nodes: list[NavNodeSpec] = []
    for raw_node in nav_config.get("nodes", []):
        if not isinstance(raw_node, dict):
            raise ValueError("nav.nodes entries must be mappings.")
        nodes.append(_parse_nav_node(raw_node, root_path))
    return nodes


def load_nav_nodes(nodes: Iterable[NavNodeSpec]) -> list[LoadedNavNode]:
    loaded_nodes: list[LoadedNavNode] = []
    for node in nodes:
        loaded_anchors: list[LoadedNavAnchor] = []
        for anchor in node.anchors:
            if not anchor.path.exists() and not anchor.required:
                logging.warning("optional nav anchor missing name=%s path=%s", anchor.name, anchor.path)
                continue
            try:
                loaded_anchors.append(LoadedNavAnchor(spec=anchor, template=load_template(anchor.path)))
            except FileNotFoundError:
                if anchor.required:
                    raise
                logging.warning("optional nav anchor unreadable name=%s path=%s", anchor.name, anchor.path)
        loaded_nodes.append(LoadedNavNode(spec=node, anchors=tuple(loaded_anchors)))
    return loaded_nodes


def load_nav_nodes_from_config(config: dict[str, Any], root: str | Path) -> list[LoadedNavNode]:
    return load_nav_nodes(parse_nav_nodes(config, root))


def match_nav_nodes(image: np.ndarray, nodes: Iterable[LoadedNavNode]) -> list[NavNodeMatch]:
    matches = [_match_nav_node(image, node) for node in nodes]
    return sorted(matches, key=lambda item: (-item.score, -item.hits, item.spec.name))


def select_best_node(matches: Iterable[NavNodeMatch], min_score_gap: float = 0.0) -> NavNodeMatch | None:
    ordered = sorted(matches, key=lambda item: (-item.score, -item.hits, item.spec.name))
    if not ordered:
        return None
    best = ordered[0]
    if not best.ok:
        return None
    if len(ordered) > 1 and min_score_gap > 0 and best.score - ordered[1].score < min_score_gap:
        logging.warning(
            "nav node match ambiguous best=%s score=%.4f second=%s second_score=%.4f gap=%.4f",
            best.spec.name,
            best.score,
            ordered[1].spec.name,
            ordered[1].score,
            best.score - ordered[1].score,
        )
        return None
    return best


def _parse_nav_node(raw: dict[str, Any], root: Path) -> NavNodeSpec:
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ValueError("nav node name must not be empty.")
    anchors = tuple(_parse_nav_anchor(item, root) for item in raw.get("anchors", []))
    if not anchors:
        raise ValueError(f"nav node {name!r} must define at least one anchor.")
    return NavNodeSpec(
        name=name,
        anchors=anchors,
        min_score=float(raw.get("min_score", 0.70)),
        min_hits=int(raw.get("min_hits", 2)),
        score_mode=str(raw.get("score_mode", "average")).strip().lower(),
        enter_from=_normalize_string_tuple(raw.get("enter_from")),
        recover_from=_normalize_string_tuple(raw.get("recover_from")),
    )


def _parse_nav_anchor(raw: dict[str, Any], root: Path) -> NavAnchorSpec:
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ValueError("nav anchor name must not be empty.")
    raw_path = raw.get("path")
    if not raw_path:
        raise ValueError(f"nav anchor {name!r} path must not be empty.")
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = root / path
    threshold = float(raw.get("threshold", 0.78))
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"nav anchor {name!r} threshold must be between 0 and 1.")
    weight = float(raw.get("weight", 1.0))
    if weight <= 0:
        raise ValueError(f"nav anchor {name!r} weight must be positive.")
    return NavAnchorSpec(
        name=name,
        path=path,
        roi=_normalize_roi(raw.get("roi")),
        threshold=threshold,
        weight=weight,
        expected_center=_normalize_point(raw.get("expected_center")),
        max_center_offset=_normalize_point(raw.get("max_center_offset")),
        image_size=_normalize_point(raw.get("image_size")),
        max_image_size_delta=_normalize_point(raw.get("max_image_size_delta")) or (8, 8),
        scale_with_image_size=bool(raw.get("scale_with_image_size", False)),
        scales=_normalize_scales(raw.get("scales")),
        required=bool(raw.get("required", False)),
    )


def _match_nav_node(image: np.ndarray, node: LoadedNavNode) -> NavNodeMatch:
    anchor_matches = tuple(_match_nav_anchor(image, anchor) for anchor in node.anchors)
    active_matches = tuple(anchor for anchor in anchor_matches if not anchor.skipped)
    if node.spec.score_mode == "best":
        score = max(
            (anchor.weighted_score / anchor.spec.weight for anchor in active_matches if anchor.spec.weight > 0),
            default=0.0,
        )
    else:
        total_weight = sum(anchor.spec.weight for anchor in active_matches)
        if total_weight <= 0:
            score = 0.0
        else:
            score = sum(anchor.weighted_score for anchor in active_matches) / total_weight
    hits = sum(1 for anchor in anchor_matches if anchor.matched and anchor.position_score > 0)
    required_ok = all(
        anchor.matched and anchor.position_score > 0
        for anchor in anchor_matches
        if anchor.spec.required
    )
    return NavNodeMatch(
        spec=node.spec,
        score=float(score),
        hits=hits,
        required_ok=required_ok,
        anchors=anchor_matches,
    )


def _match_nav_anchor(image: np.ndarray, anchor: LoadedNavAnchor) -> NavAnchorMatch:
    size_scale = _image_size_scale(image, anchor.spec.image_size)
    if size_scale is not None and not anchor.spec.scale_with_image_size:
        if not _image_size_matches(image, anchor.spec.image_size, anchor.spec.max_image_size_delta):
            return NavAnchorMatch(spec=anchor.spec, matched=False, skipped=True, message="image_size_mismatch")

    roi = anchor.spec.roi
    expected_center = anchor.spec.expected_center
    max_center_offset = anchor.spec.max_center_offset
    scales = anchor.spec.scales
    if size_scale is not None and anchor.spec.scale_with_image_size:
        scale_x, scale_y = size_scale
        roi = _scale_roi(roi, scale_x, scale_y)
        expected_center = _scale_point(expected_center, scale_x, scale_y)
        max_center_offset = _scale_point(max_center_offset, scale_x, scale_y)
        template_scale = (scale_x + scale_y) / 2.0
        scales = tuple(scale * template_scale for scale in scales)

    if roi is not None and roi[0] >= image.shape[1]:
        return NavAnchorMatch(spec=anchor.spec, matched=False, skipped=True, message="image_size_mismatch")

    search_image, offset = _crop_roi(image, roi)
    try:
        result = match_template(search_image, anchor.template, anchor.spec.threshold, scales=scales)
    except ValueError as exc:
        if anchor.spec.required:
            raise
        return NavAnchorMatch(spec=anchor.spec, matched=False, message=str(exc))
    if result is None:
        return NavAnchorMatch(spec=anchor.spec, matched=False, message="below_threshold")

    top_left = (result.top_left[0] + offset[0], result.top_left[1] + offset[1])
    center = (top_left[0] + result.size[0] // 2, top_left[1] + result.size[1] // 2)
    position_score = _position_score(center, expected_center, max_center_offset)
    return NavAnchorMatch(
        spec=anchor.spec,
        matched=position_score > 0,
        raw_score=result.score,
        position_score=position_score,
        weighted_score=result.score * anchor.spec.weight * position_score,
        top_left=top_left,
        size=result.size,
        scale=result.scale,
        message="ok" if position_score > 0 else "position_out_of_range",
    )


def _position_score(
    center: PointTuple,
    expected_center: PointTuple | None,
    max_center_offset: PointTuple | None,
) -> float:
    if expected_center is None or max_center_offset is None:
        return 1.0
    max_x, max_y = max_center_offset
    if max_x <= 0 or max_y <= 0:
        return 1.0
    dx = abs(center[0] - expected_center[0])
    dy = abs(center[1] - expected_center[1])
    if dx > max_x or dy > max_y:
        return 0.0
    ratio = max(dx / max_x, dy / max_y)
    return max(0.5, 1.0 - 0.5 * ratio)


def _image_size_matches(
    image: np.ndarray,
    expected_size: PointTuple | None,
    max_delta: PointTuple | None,
) -> bool:
    if expected_size is None:
        return True
    image_height, image_width = image.shape[:2]
    max_dx, max_dy = max_delta or (0, 0)
    return abs(image_width - expected_size[0]) <= max_dx and abs(image_height - expected_size[1]) <= max_dy


def _image_size_scale(image: np.ndarray, expected_size: PointTuple | None) -> tuple[float, float] | None:
    if expected_size is None:
        return None
    image_height, image_width = image.shape[:2]
    if expected_size[0] <= 0 or expected_size[1] <= 0:
        return None
    return image_width / expected_size[0], image_height / expected_size[1]


def _scale_roi(roi: RegionTuple | None, scale_x: float, scale_y: float) -> RegionTuple | None:
    if roi is None:
        return None
    left, top, width, height = roi
    return (
        max(0, int(round(left * scale_x))),
        max(0, int(round(top * scale_y))),
        max(1, int(round(width * scale_x))),
        max(1, int(round(height * scale_y))),
    )


def _scale_point(point: PointTuple | None, scale_x: float, scale_y: float) -> PointTuple | None:
    if point is None:
        return None
    return int(round(point[0] * scale_x)), int(round(point[1] * scale_y))


def _crop_roi(image: np.ndarray, roi: RegionTuple | None) -> tuple[np.ndarray, PointTuple]:
    if roi is None:
        return image, (0, 0)
    left, top, width, height = roi
    image_height, image_width = image.shape[:2]
    right = left + width
    bottom = top + height
    if left >= image_width or top >= image_height:
        raise ValueError(f"nav roi starts outside screenshot roi={roi} screenshot={(image_width, image_height)}")
    clipped_right = min(right, image_width)
    clipped_bottom = min(bottom, image_height)
    if clipped_right <= left or clipped_bottom <= top:
        raise ValueError(f"nav roi has no overlap with screenshot roi={roi} screenshot={(image_width, image_height)}")
    return image[top:clipped_bottom, left:clipped_right], (left, top)


def _normalize_roi(value: Any) -> RegionTuple | None:
    if value in (None, "", "null"):
        return None
    values = tuple(int(item) for item in value)
    if len(values) != 4:
        raise ValueError("nav anchor roi must be [left, top, width, height].")
    left, top, width, height = values
    if left < 0 or top < 0 or width <= 0 or height <= 0:
        raise ValueError("nav anchor roi must use non-negative left/top and positive width/height.")
    return left, top, width, height


def _normalize_point(value: Any) -> PointTuple | None:
    if value in (None, "", "null"):
        return None
    values = tuple(int(item) for item in value)
    if len(values) != 2:
        raise ValueError("nav anchor point values must be [x, y].")
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
        raise ValueError("nav anchor scales must be positive.")
    return scales


def _normalize_string_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, "", "null"):
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)
