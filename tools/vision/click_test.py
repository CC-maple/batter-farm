from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2
import pyautogui

from main import load_config, setup_logging
from src.core.targets import _normalize_offset as normalize_click_offset
from src.runtime.protocol import resolve_path as resolve_project_path
from src.core.capture import ScreenCapture, save_screenshot
from src.core.input import InputController
from src.core.recognition import TemplateMatch, load_template, match_template
from src.utils.window import (
    WindowNotFoundError,
    WindowSelector,
    activate_window,
    get_client_bounds,
    parse_window_selector,
    resolve_window,
)


DEFAULT_TEMPLATE = "assets/template-matching/star_farm_entry.png"
DEFAULT_SUCCESS_TEMPLATE = "assets/template-matching/farm_header.png"
DEFAULT_TITLE_KEYWORD = "元梦之星"


def resolve_path(value: str | Path) -> Path:
    return resolve_project_path(ROOT, value)


def set_dpi_awareness() -> None:
    if not sys.platform.startswith("win"):
        return

    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            logging.debug("failed to set process dpi awareness", exc_info=True)


def resolve_target_selector(
    window_config: dict,
    fallback_title: str,
) -> tuple[WindowSelector, str, int | None]:
    primary = parse_window_selector(window_config)

    try:
        resolved = resolve_window(primary)
        if resolved is not None:
            return primary, resolved.title, resolved.hwnd
    except WindowNotFoundError:
        logging.info("primary window selector failed, falling back to title-only lookup")

    fallback = WindowSelector(
        title=primary.title or fallback_title,
        process_name="",
        class_name="",
        hwnd=None,
    )
    try:
        resolved = resolve_window(fallback)
    except WindowNotFoundError:
        if fallback_title and fallback_title != fallback.title:
            fallback = WindowSelector(title=fallback_title)
            resolved = resolve_window(fallback)
        else:
            raise

    if resolved is None:
        raise WindowNotFoundError(f"Cannot resolve target window: {fallback}")
    return fallback, resolved.title, resolved.hwnd


def draw_debug_match(
    image,
    match: TemplateMatch,
    click_local: tuple[int, int],
    output_path: Path,
) -> None:
    debug = image.copy()
    cv2.rectangle(debug, match.top_left, match.bottom_right, (0, 255, 0), 2)

    x, y = click_local
    cv2.circle(debug, (x, y), 8, (0, 0, 255), 2)
    cv2.line(debug, (x - 14, y), (x + 14, y), (0, 0, 255), 2)
    cv2.line(debug, (x, y - 14), (x, y + 14), (0, 0, 255), 2)
    cv2.putText(
        debug,
        f"score={match.score:.4f}",
        (match.top_left[0], max(20, match.top_left[1] - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 0),
        2,
        cv2.LINE_AA,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), debug):
        raise OSError(f"Failed to write debug image: {output_path}")


def crop_region(image, region: tuple[int, int, int, int]):
    left, top, width, height = region
    image_height, image_width = image.shape[:2]
    left = max(0, min(int(left), image_width))
    top = max(0, min(int(top), image_height))
    right = max(left, min(left + int(width), image_width))
    bottom = max(top, min(top + int(height), image_height))
    return image[top:bottom, left:right], (left, top)


def detect_success(image, args: argparse.Namespace) -> TemplateMatch | None:
    template = load_template(resolve_path(args.success_template))
    search_image, origin = crop_region(image, tuple(args.success_region))
    result = match_template(search_image, template, args.success_threshold)
    if result is None:
        return None

    return TemplateMatch(
        template_path=result.template_path,
        score=result.score,
        top_left=(result.top_left[0] + origin[0], result.top_left[1] + origin[1]),
        size=result.size,
    )


def build_click_candidates(
    result: TemplateMatch,
    offset: tuple[int, int],
    extra_offsets: list[list[int]],
) -> list[tuple[str, int, int]]:
    center_x, center_y = result.center
    width, height = result.size
    base_candidates = [
        ("template_center", center_x + offset[0], center_y + offset[1]),
        ("icon_center", result.top_left[0] + width // 2, result.top_left[1] + max(10, height // 3)),
        ("label_center", result.top_left[0] + width // 2, result.top_left[1] + max(10, height * 3 // 4)),
        ("left_upper", result.top_left[0] + width // 3, result.top_left[1] + height // 3),
        ("right_upper", result.top_left[0] + width * 2 // 3, result.top_left[1] + height // 3),
        ("left_middle", result.top_left[0] + width // 3, result.top_left[1] + height // 2),
        ("right_middle", result.top_left[0] + width * 2 // 3, result.top_left[1] + height // 2),
    ]

    for index, extra in enumerate(extra_offsets, start=1):
        if len(extra) != 2:
            raise ValueError("candidate offset must be [x, y].")
        base_candidates.append((f"extra_{index}", center_x + int(extra[0]), center_y + int(extra[1])))

    seen: set[tuple[int, int]] = set()
    candidates: list[tuple[str, int, int]] = []
    for name, x, y in base_candidates:
        point = (int(x), int(y))
        if point in seen:
            continue
        seen.add(point)
        candidates.append((name, point[0], point[1]))
    return candidates


def get_cursor_position() -> tuple[int, int]:
    if not sys.platform.startswith("win"):
        position = pyautogui.position()
        return int(position.x), int(position.y)

    import ctypes
    from ctypes import wintypes

    class Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    point = Point()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetCursorPos.argtypes = (ctypes.POINTER(Point),)
    user32.GetCursorPos.restype = wintypes.BOOL
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(point.x), int(point.y)


def screen_to_client(hwnd: int | None, x: int, y: int) -> tuple[int, int] | None:
    if not hwnd or not sys.platform.startswith("win"):
        return None

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


def log_cursor_geometry(
    label: str,
    bounds,
    hwnd: int | None,
    target_x: int | None = None,
    target_y: int | None = None,
) -> None:
    cursor_x, cursor_y = get_cursor_position()
    window_relative = (cursor_x - bounds.left, cursor_y - bounds.top)
    client_relative = screen_to_client(hwnd, cursor_x, cursor_y)
    target_window_relative = None
    target_client_relative = None
    if target_x is not None and target_y is not None:
        target_window_relative = (target_x - bounds.left, target_y - bounds.top)
        target_client_relative = screen_to_client(hwnd, target_x, target_y)
    logging.info(
        "%s cursor_screen=(%s, %s) cursor_window=%s cursor_client=%s "
        "target_screen=%s target_window=%s target_client=%s",
        label,
        cursor_x,
        cursor_y,
        window_relative,
        client_relative,
        None if target_x is None or target_y is None else (target_x, target_y),
        target_window_relative,
        target_client_relative,
    )


def get_input_controller(
    config: dict,
    target_window_title: str,
    target_window_hwnd: int | None,
    backend: str | None,
) -> InputController:
    input_config = config.get("input", {})
    return InputController(
        click_delay_seconds=float(input_config.get("click_delay_seconds", 0.15)),
        click_hold_seconds=float(input_config.get("click_hold_seconds", 0.08)),
        move_duration_seconds=float(input_config.get("move_duration_seconds", 0.05)),
        click_backend=str(backend or input_config.get("click_backend", "win32")),
        fail_safe=bool(input_config.get("fail_safe", True)),
        target_window_title=target_window_title,
        target_window_hwnd=target_window_hwnd,
    )


def run_click_test(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config)
    set_dpi_awareness()

    selector, target_title, target_hwnd = resolve_target_selector(
        config.get("window", {}),
        fallback_title=args.title_keyword,
    )
    window_bounds = activate_window(selector=selector, settle_seconds=args.settle_seconds)
    if window_bounds is None:
        raise WindowNotFoundError(f"Cannot activate target window: {selector}")

    bounds = get_client_bounds(target_hwnd) if target_hwnd is not None else window_bounds
    logging.info(
        "click_test target_title=%r target_hwnd=%s window_bounds=%s client_capture_bounds=%s",
        target_title,
        target_hwnd,
        window_bounds,
        bounds,
    )
    log_cursor_geometry("after activate", bounds, target_hwnd)
    if args.calibrate_cursor_only:
        logging.info("calibrate cursor only enabled; place the mouse on the target before running this mode.")
        return 0

    capture = ScreenCapture(region=bounds.as_region()).grab()
    capture_path = resolve_path(args.capture_output)
    save_screenshot(capture.image, capture_path)

    recognition_config = config.get("recognition", {})
    template_path = resolve_path(args.template or recognition_config.get("template_path") or DEFAULT_TEMPLATE)
    threshold = args.threshold
    if threshold is None:
        threshold = float(recognition_config.get("threshold", 0.80))

    template = load_template(template_path)
    result = match_template(capture.image, template, threshold)
    if result is None:
        logging.warning("template not found threshold=%s template=%s", threshold, template.path)
        return 1

    offset_x, offset_y = normalize_click_offset(recognition_config.get("click_offset"))
    local_x = result.center[0] + offset_x
    local_y = result.center[1] + offset_y
    click_x = capture.origin[0] + local_x
    click_y = capture.origin[1] + local_y

    debug_path = resolve_path(args.debug_output)
    draw_debug_match(capture.image, result, (local_x, local_y), debug_path)

    logging.info(
        "template matched score=%.4f top_left=%s center=%s click_offset=%s screen_click=(%s, %s)",
        result.score,
        result.top_left,
        result.center,
        (offset_x, offset_y),
        click_x,
        click_y,
    )
    logging.info("debug images saved capture=%s match=%s", capture_path, debug_path)

    if args.dry_run:
        logging.info("dry run enabled, click skipped")
        return 0

    input_controller = get_input_controller(config, target_title, target_hwnd, args.backend)
    candidates = build_click_candidates(
        result,
        (offset_x, offset_y),
        args.candidate_offset,
    )[: args.max_attempts]

    try:
        for attempt, (candidate_name, local_candidate_x, local_candidate_y) in enumerate(candidates, start=1):
            attempt_click_x = capture.origin[0] + local_candidate_x
            attempt_click_y = capture.origin[1] + local_candidate_y
            logging.info(
                "attempt %s/%s candidate=%s local=(%s, %s) screen=(%s, %s)",
                attempt,
                len(candidates),
                candidate_name,
                local_candidate_x,
                local_candidate_y,
                attempt_click_x,
                attempt_click_y,
            )
            log_cursor_geometry("before move", bounds, target_hwnd, attempt_click_x, attempt_click_y)

            input_controller.move_to(attempt_click_x, attempt_click_y)
            log_cursor_geometry("after move", bounds, target_hwnd, attempt_click_x, attempt_click_y)
            logging.info(
                "mouse moved before click target=(%s, %s) current=%s pause_seconds=%s",
                attempt_click_x,
                attempt_click_y,
                pyautogui.position(),
                args.pre_click_pause_seconds,
            )
            if args.pre_click_pause_seconds > 0:
                time.sleep(args.pre_click_pause_seconds)
                logging.info("mouse position after pre-click pause current=%s", pyautogui.position())
                log_cursor_geometry("after pre-click pause", bounds, target_hwnd, attempt_click_x, attempt_click_y)

            input_controller.safe_click(attempt_click_x, attempt_click_y)
            logging.info("click sent candidate=%s", candidate_name)

            if args.after_wait_seconds <= 0:
                continue

            time.sleep(args.after_wait_seconds)
            after = ScreenCapture(region=bounds.as_region()).grab()
            after_path = resolve_path(args.after_output)
            if len(candidates) > 1:
                after_path = after_path.with_name(f"{after_path.stem}_{attempt:02d}{after_path.suffix}")
            save_screenshot(after.image, after_path)
            logging.info("after-click screenshot saved path=%s", after_path)

            success = detect_success(after.image, args)
            if success is not None:
                logging.info(
                    "farm success detected candidate=%s score=%.4f top_left=%s template=%s",
                    candidate_name,
                    success.score,
                    success.top_left,
                    success.template_path,
                )
                return 0

            logging.warning(
                "farm success not detected candidate=%s threshold=%s template=%s region=%s",
                candidate_name,
                args.success_threshold,
                resolve_path(args.success_template),
                args.success_region,
            )

        return 3
    finally:
        input_controller.release_all()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Activate Yuanmeng Star, match the Star Farm entry once, then click it."
    )
    parser.add_argument("--config", default="configs/config.yaml", help="Path to config yaml.")
    parser.add_argument("--template", default=None, help="Template image path.")
    parser.add_argument("--threshold", type=float, default=None, help="Template match threshold.")
    parser.add_argument("--backend", default=None, help="Override input backend, such as win32 or pyautogui.")
    parser.add_argument("--title-keyword", default=DEFAULT_TITLE_KEYWORD, help="Fallback window title keyword.")
    parser.add_argument("--settle-seconds", type=float, default=0.8, help="Wait after activating the game window.")
    parser.add_argument("--pre-click-pause-seconds", type=float, default=2.0, help="Pause after moving to target before clicking.")
    parser.add_argument("--after-wait-seconds", type=float, default=3.0, help="Wait before after-click screenshot.")
    parser.add_argument("--max-attempts", type=int, default=3, help="Maximum click candidates to try.")
    parser.add_argument(
        "--candidate-offset",
        type=int,
        nargs=2,
        action="append",
        default=[],
        metavar=("X", "Y"),
        help="Extra click offset from the matched template center. Can be provided multiple times.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Match and draw debug image without clicking.")
    parser.add_argument(
        "--calibrate-cursor-only",
        action="store_true",
        help="Only activate the window and log current cursor screen/window/client coordinates.",
    )
    parser.add_argument("--capture-output", default="logs/click_test_capture.png")
    parser.add_argument("--debug-output", default="logs/click_test_match.png")
    parser.add_argument("--after-output", default="logs/click_test_after.png")
    parser.add_argument("--success-template", default=DEFAULT_SUCCESS_TEMPLATE)
    parser.add_argument("--success-threshold", type=float, default=0.82)
    parser.add_argument(
        "--success-region",
        type=int,
        nargs=4,
        default=[0, 0, 460, 160],
        metavar=("LEFT", "TOP", "WIDTH", "HEIGHT"),
        help="Local window region used to detect the farm header after clicking.",
    )
    return parser.parse_args()


def main() -> int:
    try:
        return run_click_test(parse_args())
    except KeyboardInterrupt:
        logging.info("keyboard interrupt, exiting")
        return 130
    except Exception:
        logging.exception("click_test failed")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
