from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from typing import Any

import pyautogui
from pynput import keyboard

from main import load_config, setup_logging
from src.core.input import InputController




class HotkeyClicker:
    def __init__(self, input_controller: InputController, fixed_position: tuple[int, int] | None) -> None:
        self.input_controller = input_controller
        self.fixed_position = fixed_position
        self.stop_event = threading.Event()
        self.click_lock = threading.Lock()
        self.listener: keyboard.Listener | None = None

    def start(self) -> None:
        self.listener = keyboard.Listener(on_press=self._on_press)
        self.listener.start()

    def wait(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(0.05)

    def stop(self) -> None:
        self.stop_event.set()
        if self.listener is not None:
            self.listener.stop()

    def _on_press(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key == keyboard.Key.f9:
            logging.info("F9 pressed, exiting")
            self.stop()
            return

        if key == keyboard.Key.f8:
            if not self.click_lock.acquire(blocking=False):
                logging.info("F8 pressed while click is still running, ignored")
                return
            threading.Thread(target=self._click_once, daemon=True).start()

    def _click_once(self) -> None:
        try:
            x, y = self.fixed_position or pyautogui.position()
            logging.info("F8 pressed, single click at x=%s y=%s", x, y)
            self.input_controller.safe_click(int(x), int(y))
        except Exception:
            logging.exception("single click failed")
        finally:
            self.click_lock.release()


def parse_position(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None

    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--position must be formatted as x,y")
    return int(parts[0]), int(parts[1])


def build_input_controller(config: dict[str, Any], backend: str | None) -> InputController:
    input_config = config.get("input", {})
    return InputController(
        click_delay_seconds=float(input_config.get("click_delay_seconds", 0.15)),
        click_hold_seconds=float(input_config.get("click_hold_seconds", 0.08)),
        move_duration_seconds=float(input_config.get("move_duration_seconds", 0.05)),
        click_backend=str(backend or input_config.get("click_backend", "sendinput")),
        fail_safe=bool(input_config.get("fail_safe", True)),
        target_window_title="",
    )


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config)

    position = parse_position(args.position)
    clicker = HotkeyClicker(
        input_controller=build_input_controller(config, args.backend),
        fixed_position=position,
    )

    if position is None:
        logging.info("hotkey click test started: F8 click current cursor position, F9 exit")
        print("Hotkey click test started. Press F8 to click current cursor position, F9 to exit.")
    else:
        logging.info("hotkey click test started: F8 click fixed position=%s, F9 exit", position)
        print(f"Hotkey click test started. Press F8 to click fixed position {position}, F9 to exit.")

    try:
        clicker.start()
        clicker.wait()
    except KeyboardInterrupt:
        logging.info("keyboard interrupt, exiting")
    finally:
        clicker.stop()
        clicker.input_controller.release_all()

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Keep running and send one left click when F8 is pressed. Press F9 to exit."
    )
    parser.add_argument("--config", default="configs/config.yaml", help="Path to config yaml.")
    parser.add_argument(
        "--backend",
        choices=["pyautogui", "win32", "sendinput", "postmessage"],
        help="Override input backend. Defaults to input.click_backend in config.yaml.",
    )
    parser.add_argument(
        "--position",
        help="Optional fixed screen coordinate formatted as x,y. Defaults to current cursor position on each F8.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
