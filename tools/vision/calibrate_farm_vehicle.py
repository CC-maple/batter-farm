from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import load_config, setup_logging
from src.core.game_window import (
    capture_game_client,
    draw_crosshair,
    draw_match_overlay,
    draw_roi_overlay,
    get_cursor_position,
    save_debug_capture,
    screen_to_client,
    timestamped_path,
)
from src.core.input import InputController
from src.core.recognition import load_template, match_template


def calibration_dir() -> Path:
    path = ROOT / "logs" / "calibration"
    path.mkdir(parents=True, exist_ok=True)
    return path


def capture(config: dict, prefix: str = "client_capture"):
    shot, _, title, hwnd, _ = capture_game_client(
        config.get("window", {}),
        activate=True,
        use_client_region=bool(config.get("capture", {}).get("use_client_region", True)),
    )
    path = timestamped_path(calibration_dir(), prefix)
    save_debug_capture(shot.image, path)
    logging.info("capture saved path=%s title=%r hwnd=%s origin=%s region=%s", path, title, hwnd, shot.origin, shot.region)
    print(f"saved={path}")
    print(f"origin={shot.origin} region={shot.region} title={title!r} hwnd={hwnd}")
    return shot, hwnd


def cursor(config: dict) -> None:
    shot, hwnd = capture(config, prefix="cursor_capture")
    screen_x, screen_y = get_cursor_position()
    if hwnd:
        client = screen_to_client(hwnd, screen_x, screen_y)
    else:
        client = (screen_x - shot.origin[0], screen_y - shot.origin[1])
    debug = draw_crosshair(shot.image, client)
    path = timestamped_path(calibration_dir(), "cursor_marked")
    save_debug_capture(debug, path)
    print(f"cursor_screen=({screen_x}, {screen_y}) cursor_client={client} marked={path}")


def roi(config: dict, values: list[int]) -> None:
    shot, _ = capture(config, prefix="roi_capture")
    debug = draw_roi_overlay(shot.image, [tuple(values)])
    path = timestamped_path(calibration_dir(), "roi_marked")
    save_debug_capture(debug, path)
    print(f"roi={tuple(values)} marked={path}")


def probe_template(config: dict, template_path: str, threshold: float) -> int:
    shot, _ = capture(config, prefix="probe_capture")
    template = load_template(ROOT / template_path if not Path(template_path).is_absolute() else template_path)
    result = match_template(shot.image, template, threshold)
    if result is None:
        print(f"not_matched template={template.path} threshold={threshold}")
        return 1
    debug = draw_match_overlay(
        shot.image,
        result.top_left,
        result.bottom_right,
        f"{Path(template.path).name} {result.score:.4f}",
    )
    path = timestamped_path(calibration_dir(), "probe_match")
    save_debug_capture(debug, path)
    print(
        f"matched template={template.path} score={result.score:.4f} "
        f"top_left={result.top_left} center={result.center} marked={path}"
    )
    return 0


def toggle_mouse(config: dict) -> None:
    shot_before, _, _, _, _ = capture_game_client(config.get("window", {}), activate=True)
    before_path = timestamped_path(calibration_dir(), "toggle_before")
    save_debug_capture(shot_before.image, before_path)
    input_config = config.get("input", {})
    controller = InputController(
        click_delay_seconds=float(input_config.get("click_delay_seconds", 0.15)),
        click_hold_seconds=float(input_config.get("click_hold_seconds", 0.08)),
        move_duration_seconds=float(input_config.get("move_duration_seconds", 0.05)),
        click_backend=str(input_config.get("click_backend", "sendinput")),
        fail_safe=bool(input_config.get("fail_safe", True)),
    )
    try:
        controller.toggle_mouse_mode(str(config.get("farm_vehicle", {}).get("mouse_toggle_key", "`")))
        time.sleep(0.2)
    finally:
        controller.release_all()
    shot_after, _, _, _, _ = capture_game_client(config.get("window", {}), activate=False)
    after_path = timestamped_path(calibration_dir(), "toggle_after")
    save_debug_capture(shot_after.image, after_path)
    print(f"toggle_before={before_path}")
    print(f"toggle_after={after_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Farm vehicle calibration helper.")
    parser.add_argument("--config", default="configs/config.yaml")
    parser.add_argument("--capture", action="store_true", help="Capture the game client.")
    parser.add_argument("--cursor", action="store_true", help="Capture and mark current cursor client position.")
    parser.add_argument("--toggle-mouse", action="store_true", help="Press the configured mouse toggle key and capture before/after.")
    parser.add_argument("--roi", type=int, nargs=4, metavar=("LEFT", "TOP", "WIDTH", "HEIGHT"))
    parser.add_argument("--probe-template", help="Template path to match against the current client screenshot.")
    parser.add_argument("--threshold", type=float, default=0.8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config)
    if args.cursor:
        cursor(config)
        return 0
    if args.toggle_mouse:
        toggle_mouse(config)
        return 0
    if args.roi:
        roi(config, args.roi)
        return 0
    if args.probe_template:
        return probe_template(config, args.probe_template, args.threshold)
    capture(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
