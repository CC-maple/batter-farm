from __future__ import annotations

import logging
import threading
import time

from pynput import keyboard


class RunControl:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.overlay_toggle_event = threading.Event()
        self._overlay_visibility_request: bool | None = None
        self._overlay_visibility_lock = threading.Lock()
        self.rescan_event = threading.Event()
        self.exit_event = threading.Event()
        self.interrupt_event = threading.Event()
        self.busy_event = threading.Event()
        self._listener: keyboard.Listener | None = None

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    @property
    def paused(self) -> bool:
        return self.pause_event.is_set()

    def start(self) -> None:
        if self._listener is not None:
            return

        self._listener = keyboard.Listener(on_press=self._on_press)
        self._listener.daemon = True
        self._listener.start()
        logging.info(
            "hotkeys registered: F7 overlay show/hide, F8 pause/resume, F9 idle rescan, "
            "F10 safe stop, ESC stop"
        )

    def stop(self) -> None:
        self.request_stop()
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def request_stop(self) -> None:
        self.stop_event.set()

    def request_pause(self) -> None:
        self.pause_event.set()

    def request_resume(self) -> None:
        self.pause_event.clear()

    def request_pause_toggle(self) -> bool:
        if self.paused:
            self.request_resume()
        else:
            self.request_pause()
        return self.paused

    def request_overlay_toggle(self) -> None:
        self.overlay_toggle_event.set()

    def request_overlay_visibility(self, visible: bool) -> None:
        with self._overlay_visibility_lock:
            self._overlay_visibility_request = bool(visible)

    def request_rescan(self) -> bool:
        if self.busy_event.is_set():
            return False
        self.rescan_event.set()
        return True

    def request_exit(self) -> None:
        self.exit_event.set()

    def request_interrupt(self) -> None:
        self.interrupt_event.set()

    def wait_if_paused(self) -> None:
        while self.paused and not self.stopped:
            time.sleep(0.1)

    def consume_overlay_toggle(self) -> bool:
        if not self.overlay_toggle_event.is_set():
            return False
        self.overlay_toggle_event.clear()
        return True

    def consume_overlay_visibility_request(self) -> bool | None:
        with self._overlay_visibility_lock:
            requested = self._overlay_visibility_request
            self._overlay_visibility_request = None
        return requested

    def consume_rescan_request(self) -> bool:
        if not self.rescan_event.is_set():
            return False
        self.rescan_event.clear()
        return True

    def consume_exit_request(self) -> bool:
        if not self.exit_event.is_set():
            return False
        self.exit_event.clear()
        return True

    def consume_interrupt_request(self) -> bool:
        if not self.interrupt_event.is_set():
            return False
        self.interrupt_event.clear()
        return True

    def set_busy(self, busy: bool) -> None:
        if busy:
            self.busy_event.set()
        else:
            self.busy_event.clear()

    def _on_press(self, key: keyboard.Key | keyboard.KeyCode) -> bool | None:
        if key == keyboard.Key.esc:
            self.request_stop()
            logging.info("ESC pressed, stop requested")
            return False

        if key == keyboard.Key.f8:
            paused = self.request_pause_toggle()
            logging.info("F8 pressed, %s", "paused" if paused else "resumed")

        if key == keyboard.Key.f7:
            self.request_overlay_toggle()
            logging.info("F7 pressed, overlay toggle requested")

        if key == keyboard.Key.f9:
            if not self.request_rescan():
                logging.info("F9 pressed while busy, ignored")
                return None
            logging.info("F9 pressed, marker rescan requested")

        if key == keyboard.Key.f10:
            self.request_stop()
            logging.info("F10 pressed, safe stop requested")
            return False

        return None
