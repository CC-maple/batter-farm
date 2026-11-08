from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

from src.runtime.scheduler_models import ActionRequest, ActionResult


@dataclass
class _ActionWork:
    request: ActionRequest
    operation: Callable[[], Any]
    callback: Callable[[ActionResult], None] | None


class ActionExecutor:
    """Single long-lived executor for every input-capable action."""

    def __init__(self, *, name: str = "batterfarm-action-executor") -> None:
        self._queue: queue.Queue[_ActionWork | None] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._active: ActionRequest | None = None
        self._leased = False
        self._closed = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._leased

    @property
    def active_request(self) -> ActionRequest | None:
        with self._lock:
            return self._active

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    def submit(
        self,
        request: ActionRequest,
        operation: Callable[[], Any],
        *,
        callback: Callable[[ActionResult], None] | None = None,
    ) -> None:
        with self._lock:
            if self._closed.is_set():
                raise RuntimeError("action executor is closed")
            if self._leased:
                raise RuntimeError("an action task is already running")
            self._leased = True
            self._idle.clear()
            try:
                self._queue.put_nowait(_ActionWork(request, operation, callback))
            except BaseException:
                self._leased = False
                self._idle.set()
                raise

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    def close(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # A queued action will observe the closed flag and be cancelled
            # before its operation starts.  Do not block shutdown if the
            # worker has already failed or has not consumed that item yet.
            pass
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
        if not self._thread.is_alive():
            with self._lock:
                self._active = None
                self._leased = False
            self._idle.set()

    def _run(self) -> None:
        while True:
            work = self._queue.get()
            if work is None:
                return
            with self._lock:
                self._active = work.request
            if self._closed.is_set():
                result = ActionResult(
                    action_id=work.request.action_id,
                    run_id=work.request.run_id,
                    generation=work.request.generation,
                    name=work.request.name,
                    error="RuntimeError: action executor closed before action started",
                )
            else:
                try:
                    value = work.operation()
                    result = ActionResult(
                        action_id=work.request.action_id,
                        run_id=work.request.run_id,
                        generation=work.request.generation,
                        name=work.request.name,
                        value=value,
                    )
                except BaseException as exc:
                    result = ActionResult(
                        action_id=work.request.action_id,
                        run_id=work.request.run_id,
                        generation=work.request.generation,
                        name=work.request.name,
                        error=f"{type(exc).__name__}: {exc}",
                        value=exc,
                    )
            with self._lock:
                self._active = None
                self._leased = False
            try:
                if work.callback is not None:
                    work.callback(result)
            except BaseException:
                logging.exception(
                    "action executor callback failed action_id=%s name=%s",
                    work.request.action_id,
                    work.request.name,
                )
            finally:
                with self._lock:
                    if not self._leased:
                        self._idle.set()
            if self._closed.is_set() and self._queue.empty():
                return
