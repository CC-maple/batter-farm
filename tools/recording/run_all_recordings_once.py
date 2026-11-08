# Usage:
#   python tools/recording/run_all_recordings_once.py --list
#   python tools/recording/run_all_recordings_once.py
#   python tools/recording/run_all_recordings_once.py --overlay
#
# This script unconditionally replays the current V0.4 farm recordings once.
# It skips marker/template matching, but still uses the normal
# FarmVehicleTask replay path, so F8 pause, ESC stop, target-window coordinate
# handling, and release_all cleanup remain active.
#
# Default sequence:
#   farm_field_latest_104821 -> fishpond_latest_110224 ->
#   pasture_latest_110127 -> hot_spring_latest_110602

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from main import load_config, resolve_target_selector, setup_logging
from src.core.hotkey import RunControl
from src.core.input import InputController
from src.tasks.farm_vehicle import FarmVehicleTask


BASE_RESET = {
    "key": "r",
    "hold_seconds": 0.08,
    "wait_seconds": 1.0,
}

DEFAULT_RECORDINGS = [
    {
        "name": "farm_field_latest_104821",
        "source": "farm_field_latest_104821",
        "include_mouse": True,
        "ignore_keys": [],
        # This recording already starts with its own R reset. Keep the replay
        # timing intact instead of adding an outer reset in front of it.
        "reset_before": False,
        "reset_after": False,
    },
    {
        "name": "fishpond_latest_110224",
        "source": "fishpond_latest_110224",
        "include_mouse": True,
        "ignore_keys": [],
        "reset_before": False,
        "reset_after": False,
        "match_waits": [
            {
                "label": "fish_card_result",
                "t_ms": 19668,
                "resume_t_ms": 23050,
                "duration_seconds": 60.0,
                "interval_seconds": 2.0,
                "match_any": ["fish_card_result"],
                "click_on_match": True,
                "click_until_gone": True,
                "stop_on_match": True,
                "max_clicks": 5,
                "after_click_wait_seconds": 0.5,
            }
        ],
    },
    {
        "name": "pasture_latest_110127",
        "source": "pasture_latest_110127",
        "include_mouse": False,
        "ignore_keys": [],
        "reset_before": False,
        "reset_after": False,
    },
    {
        "name": "hot_spring_latest_110602",
        "source": "hot_spring_latest_110602",
        "include_mouse": True,
        "ignore_keys": [],
        "reset_before": False,
        "reset_after": False,
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay the current V0.4 BatterFarm recordings once without marker matching."
    )
    parser.add_argument("--config", default="configs/config.yaml", help="Path to config yaml.")
    parser.add_argument("--list", action="store_true", help="Print the replay plan without running input.")
    parser.add_argument("--overlay", action="store_true", help="Enable the status overlay for this run.")
    parser.add_argument("--speed", type=float, default=1.0, help="Replay speed multiplier for all recordings.")
    parser.add_argument(
        "--max-initial-wait-seconds",
        type=float,
        default=1.5,
        help="Cap the first wait inside each recording replay.",
    )
    return parser.parse_args()


def build_recording_plan(config: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    source_recordings = config.get("route", {}).get("source_recordings", {})
    raw_items = list(DEFAULT_RECORDINGS)

    steps: list[dict[str, Any]] = []
    for item in raw_items:
        source = str(item["source"])
        path = source_recordings.get(source)
        if not path:
            raise ValueError(f"route.source_recordings is missing {source!r}")

        reset_before = {**BASE_RESET, "enabled": bool(item.get("reset_before", True))}
        reset_after = {**BASE_RESET, "enabled": bool(item.get("reset_after", True)), "wait_seconds": 0.5}

        replay: dict[str, Any] = {
            "enabled": True,
            "path": path,
            "include_mouse": bool(item.get("include_mouse", False)),
            "ignore_keys": list(item.get("ignore_keys", ["r"])),
            "speed": float(args.speed),
            "max_initial_wait_seconds": float(args.max_initial_wait_seconds),
        }
        if item.get("start_ms") is not None:
            replay["start_ms"] = int(item["start_ms"])
        if item.get("end_ms") is not None:
            replay["end_ms"] = int(item["end_ms"])
        if item.get("match_waits") is not None:
            replay["match_waits"] = list(item["match_waits"])

        steps.append(
            {
                "name": str(item["name"]),
                "enabled": True,
                "preflight_capture": True,
                "capture_after_reset": True,
                "after_actions_wait_seconds": 1.0,
                "reset_before_start": reset_before,
                "reset_after_finish": reset_after,
                "recording_replay": replay,
            }
        )

    return steps


def print_plan(steps: list[dict[str, Any]]) -> None:
    for index, step in enumerate(steps, start=1):
        replay = step["recording_replay"]
        print(
            f"{index}. {step['name']} "
            f"path={replay['path']} "
            f"include_mouse={replay['include_mouse']} "
            f"ignore_keys={replay['ignore_keys']} "
            f"reset_before={step['reset_before_start']['enabled']} "
            f"reset_after={step['reset_after_finish']['enabled']}"
        )


def run_plan(config: dict[str, Any], steps: list[dict[str, Any]]) -> int:
    window_config = config.get("window", {})
    _, target_window_title, target_window_hwnd = resolve_target_selector(window_config)
    control = RunControl()
    input_controller = InputController(
        click_delay_seconds=float(config.get("input", {}).get("click_delay_seconds", 0.15)),
        click_hold_seconds=float(config.get("input", {}).get("click_hold_seconds", 0.08)),
        move_duration_seconds=float(config.get("input", {}).get("move_duration_seconds", 0.05)),
        click_backend=str(config.get("input", {}).get("click_backend", "sendinput")),
        fail_safe=bool(config.get("input", {}).get("fail_safe", True)),
        target_window_title=target_window_title,
        target_window_hwnd=target_window_hwnd,
    )
    task: FarmVehicleTask | None = None
    try:
        control.start()
        task = FarmVehicleTask(
            config=config,
            root=ROOT,
            input_controller=input_controller,
            control=control,
        )
        return task.run_recording_plan(steps)
    finally:
        if task is not None:
            task.close()
        input_controller.release_all()
        control.stop()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    steps = build_recording_plan(config, args)
    if args.overlay:
        config.setdefault("overlay", {})["enabled"] = True

    if args.list:
        print_plan(steps)
        return 0

    setup_logging(config)
    logging.info("unconditional all-recordings replay starting steps=%s", [step["name"] for step in steps])
    return run_plan(config, steps)


if __name__ == "__main__":
    raise SystemExit(main())
