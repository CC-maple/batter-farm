from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

import cv2
import pyautogui

from src.core.capture import ScreenCapture, Screenshot, save_screenshot
from src.utils.window import (
    WindowActivationError,
    WindowBounds,
    WindowNotFoundError,
    ResolvedWindow,
    WindowSelector,
    activate_window,
    get_client_bounds,
    parse_window_selector,
    resolve_window,
)


def _require_window(selector: WindowSelector) -> ResolvedWindow:
    resolved = resolve_window(selector)
    if resolved is None:
        raise WindowNotFoundError(f"Cannot find window matching selector: {selector}")
    try:
        hwnd = int(resolved.hwnd)
    except (TypeError, ValueError) as exc:
        raise WindowNotFoundError(f"Resolved window has an invalid hwnd: {resolved.hwnd!r}") from exc
    if hwnd <= 0:
        raise WindowNotFoundError(f"Resolved window has an invalid hwnd: {hwnd}")
    return resolved


def _require_capture_region(bounds: WindowBounds | None, description: str) -> tuple[int, int, int, int]:
    if bounds is None:
        raise WindowNotFoundError(f"Cannot determine {description} for the target game window.")

    region = bounds.as_region()
    if region[2] <= 0 or region[3] <= 0:
        raise WindowNotFoundError(f"Invalid {description} for the target game window: {region}")
    return region


def bring_game_to_front(window_config: dict, settle_seconds: float = 0.5) -> tuple[WindowSelector, str, int | None, WindowBounds | None]:
    selector = parse_window_selector(window_config)
    resolved = _require_window(selector)
    title = resolved.title
    hwnd = resolved.hwnd
    bounds = activate_window(selector=selector, settle_seconds=settle_seconds)
    if bounds is None:
        raise WindowActivationError(f"Cannot activate target game window: {selector}")
    return selector, title, hwnd, bounds


def capture_game_client(
    window_config: dict,
    activate: bool = True,
    use_client_region: bool = True,
    settle_seconds: float = 0.5,
) -> tuple[Screenshot, WindowSelector, str, int | None, WindowBounds | None]:
    selector = parse_window_selector(window_config)
    resolved = _require_window(selector)
    title = resolved.title
    hwnd = resolved.hwnd
    window_bounds = activate_window(selector=selector, settle_seconds=settle_seconds) if activate else None
    if activate and window_bounds is None:
        raise WindowActivationError(f"Cannot activate target game window: {selector}")

    if use_client_region:
        capture_region = _require_capture_region(get_client_bounds(hwnd), "client area")
    else:
        capture_region = _require_capture_region(window_bounds, "window bounds")

    return ScreenCapture(region=capture_region).grab(), selector, title, hwnd, window_bounds


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    import ctypes
    from ctypes import wintypes

    class Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    point = Point(int(x), int(y))
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ScreenToClient.argtypes = (wintypes.HWND, ctypes.POINTER(Point))
    user32.ScreenToClient.restype = wintypes.BOOL
    if not user32.ScreenToClient(wintypes.HWND(int(hwnd)), ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(point.x), int(point.y)


def get_cursor_position() -> tuple[int, int]:
    position = pyautogui.position()
    return int(position.x), int(position.y)


def timestamped_path(directory: str | Path, prefix: str, suffix: str = ".png") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(directory) / f"{prefix}_{stamp}{suffix}"


def draw_crosshair(image, point: tuple[int, int], color: tuple[int, int, int] = (0, 0, 255)):
    x, y = int(point[0]), int(point[1])
    debug = image.copy()
    cv2.circle(debug, (x, y), 8, color, 2)
    cv2.line(debug, (x - 18, y), (x + 18, y), color, 2)
    cv2.line(debug, (x, y - 18), (x, y + 18), color, 2)
    return debug


def draw_roi_overlay(image, regions: Iterable[tuple[int, int, int, int]]):
    debug = image.copy()
    for index, (left, top, width, height) in enumerate(regions, start=1):
        top_left = (int(left), int(top))
        bottom_right = (int(left + width), int(top + height))
        cv2.rectangle(debug, top_left, bottom_right, (0, 255, 255), 2)
        cv2.putText(
            debug,
            f"roi_{index}",
            (top_left[0], max(20, top_left[1] - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
    return debug


def draw_match_overlay(image, top_left: tuple[int, int], bottom_right: tuple[int, int], label: str):
    debug = image.copy()
    cv2.rectangle(debug, top_left, bottom_right, (0, 255, 0), 2)
    cv2.putText(
        debug,
        label,
        (int(top_left[0]), max(20, int(top_left[1]) - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 0),
        2,
        cv2.LINE_AA,
    )
    return debug


def save_debug_capture(image, path: str | Path) -> Path:
    output = Path(path)
    save_screenshot(image, output)
    return output
