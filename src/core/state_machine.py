from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class StateStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class StateResult:
    status: StateStatus
    next_state: str | None = None
    message: str = ""


class SimpleStateMachine:
    def __init__(self, initial_state: str, handlers: dict[str, Callable[[], StateResult]]) -> None:
        self.state = initial_state
        self.handlers = handlers

    def run(self) -> StateResult:
        while True:
            handler = self.handlers.get(self.state)
            if handler is None:
                return StateResult(StateStatus.FAILED, message=f"Unknown state: {self.state}")

            logging.info("state enter=%s", self.state)
            result = handler()
            logging.info("state exit=%s status=%s next=%s message=%s", self.state, result.status, result.next_state, result.message)

            if result.status != StateStatus.SUCCEEDED or not result.next_state:
                return result

            self.state = result.next_state


def monotonic_deadline(timeout_seconds: float) -> float:
    return time.monotonic() + max(0.0, float(timeout_seconds))


def deadline_expired(deadline: float) -> bool:
    return time.monotonic() >= deadline
