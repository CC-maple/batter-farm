from __future__ import annotations

import sys
import threading
from typing import TextIO


STOP_COMMANDS = {"stop", "quit", "exit"}


def start_stdin_stop_listener(stop_event: threading.Event, stream: TextIO | None = None) -> threading.Thread:
    """Listen for a cooperative stop request from BatterUI without blocking CLI use."""

    source = stream or sys.stdin

    def listen() -> None:
        try:
            for line in source:
                if line.strip().lower() in STOP_COMMANDS:
                    stop_event.set()
                    return
        except (OSError, ValueError):
            return

    thread = threading.Thread(target=listen, name="input-tool-stdin", daemon=True)
    thread.start()
    return thread
