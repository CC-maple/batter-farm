from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class TemplateImage:
    path: Path
    image: np.ndarray
    mask: np.ndarray | None = None

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])


@dataclass(frozen=True)
class TemplateMatch:
    template_path: Path
    score: float
    top_left: tuple[int, int]
    size: tuple[int, int]
    scale: float = 1.0

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


def load_template(path: str | Path) -> TemplateImage:
    template_path = Path(path)
    data = np.fromfile(template_path, dtype=np.uint8)
    raw_image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED) if data.size else None
    if raw_image is None:
        raise FileNotFoundError(f"Cannot read template image: {template_path}")

    image, alpha_mask = _split_template_image(raw_image)
    sidecar_mask = _load_sidecar_mask(template_path, image.shape[:2])
    mask = sidecar_mask if sidecar_mask is not None else alpha_mask
    return TemplateImage(path=template_path, image=image, mask=mask)


def match_template(
    screenshot: np.ndarray,
    template: TemplateImage,
    threshold: float,
    scales: tuple[float, ...] | None = None,
    ignore_regions: tuple[tuple[int, int, int, int], ...] = (),
) -> TemplateMatch | None:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("recognition.threshold must be between 0 and 1.")

    screen_height, screen_width = screenshot.shape[:2]
    candidate_scales = tuple(scales or (1.0,))
    if not candidate_scales:
        candidate_scales = (1.0,)

    best_score = -1.0
    best_location: tuple[int, int] | None = None
    best_size: tuple[int, int] | None = None
    best_scale = 1.0

    for scale in candidate_scales:
        if scale <= 0:
            raise ValueError("recognition template scales must be positive.")
        mask = template.mask
        if scale == 1.0:
            scaled = template.image
            scaled_mask = mask
        else:
            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            scaled = cv2.resize(template.image, None, fx=scale, fy=scale, interpolation=interpolation)
            scaled_mask = None
            if mask is not None:
                scaled_mask = cv2.resize(mask, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)

        height, width = scaled.shape[:2]
        if width > screen_width or height > screen_height:
            continue

        if scaled_mask is not None:
            if not np.any(scaled_mask):
                continue
            result = cv2.matchTemplate(screenshot, scaled, cv2.TM_CCORR_NORMED, mask=scaled_mask)
            result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
        else:
            result = cv2.matchTemplate(screenshot, scaled, cv2.TM_CCOEFF_NORMED)
        _apply_ignore_regions(result, (width, height), ignore_regions)
        _, max_value, _, max_location = cv2.minMaxLoc(result)
        score = float(max_value)
        if score > best_score:
            best_score = score
            best_location = (int(max_location[0]), int(max_location[1]))
            best_size = (int(width), int(height))
            best_scale = float(scale)

    if best_location is None or best_size is None:
        raise ValueError(
            "Template is larger than the screenshot or configured capture region."
        )

    if best_score < threshold:
        return None

    return TemplateMatch(
        template_path=template.path,
        score=best_score,
        top_left=best_location,
        size=best_size,
        scale=best_scale,
    )


def _split_template_image(image: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), None
    if image.shape[2] == 4:
        color = image[:, :, :3]
        alpha = image[:, :, 3]
        mask = alpha if int(alpha.min()) < 255 else None
        return color, mask
    if image.shape[2] == 3:
        return image, None
    raise ValueError(f"Unsupported template image channels: {image.shape}")


def _load_sidecar_mask(template_path: Path, image_shape: tuple[int, int]) -> np.ndarray | None:
    mask_path = template_path.with_name(f"{template_path.stem}_mask{template_path.suffix}")
    if not mask_path.exists():
        return None

    data = np.fromfile(mask_path, dtype=np.uint8)
    mask = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE) if data.size else None
    if mask is None:
        raise FileNotFoundError(f"Cannot read template mask image: {mask_path}")
    if mask.shape[:2] != image_shape:
        raise ValueError(
            f"Template mask size mismatch: mask={mask_path} mask_shape={mask.shape[:2]} image_shape={image_shape}"
        )
    return mask


def _apply_ignore_regions(
    result: np.ndarray,
    template_size: tuple[int, int],
    ignore_regions: tuple[tuple[int, int, int, int], ...],
) -> None:
    if not ignore_regions:
        return

    result_height, result_width = result.shape[:2]
    template_width, template_height = template_size
    for left, top, width, height in ignore_regions:
        right = int(left) + int(width)
        bottom = int(top) + int(height)
        x0 = max(0, int(left) - template_width + 1)
        y0 = max(0, int(top) - template_height + 1)
        x1 = min(result_width, right)
        y1 = min(result_height, bottom)
        if x0 < x1 and y0 < y1:
            result[y0:y1, x0:x1] = -1.0
