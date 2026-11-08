from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
import pyautogui


RegionTuple = tuple[int, int, int, int]


@dataclass(frozen=True)
class Screenshot:
    image: np.ndarray
    origin: tuple[int, int]
    region: RegionTuple | None


def normalize_region(region: Iterable[int] | None) -> RegionTuple | None:
    if region is None:
        return None

    values = tuple(int(value) for value in region)
    if len(values) != 4:
        raise ValueError("capture.region must be null or [left, top, width, height].")

    left, top, width, height = values
    if width <= 0 or height <= 0:
        raise ValueError("capture.region width and height must be positive.")

    return left, top, width, height


class ScreenCapture:
    def __init__(self, region: Iterable[int] | None = None) -> None:
        self.region = normalize_region(region)

    def grab(self) -> Screenshot:
        shot = pyautogui.screenshot(region=self.region)
        image_rgb = np.array(shot)
        image_bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)

        if self.region is None:
            origin = (0, 0)
        else:
            origin = (self.region[0], self.region[1])

        return Screenshot(image=image_bgr, origin=origin, region=self.region)


def save_screenshot(image: np.ndarray, path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not cv2.imwrite(str(output_path), image):
        raise OSError(f"Failed to write screenshot: {output_path}")
