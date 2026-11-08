from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from src.runtime.scheduler_models import CaptureRequest, CaptureResult


@dataclass
class _CaptureWork:
    request: CaptureRequest
    operation: Callable[[CaptureRequest], CaptureResult]
    callbacks: list[Callable[[CaptureResult], None]]


class CaptureCoordinator:
    """Single-flight capture worker with equivalent-request subscription."""

    def __init__(self, *, name: str = "batterfarm-capture-worker") -> None:
        self._queue: queue.Queue[_CaptureWork | None] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._current: _CaptureWork | None = None
        self._pending: _CaptureWork | None = None
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def in_flight(self) -> bool:
        with self._lock:
            return self._current is not None or self._pending is not None

    def submit(
        self,
        request: CaptureRequest,
        operation: Callable[[CaptureRequest], CaptureResult],
        *,
        callback: Callable[[CaptureResult], None],
    ) -> str:
        with self._lock:
            if self._closed.is_set():
                raise RuntimeError("capture coordinator is closed")
            for work in (self._current, self._pending):
                if work is not None and work.request.equivalence_key == request.equivalence_key:
                    work.callbacks.append(callback)
                    return work.request.request_id
            if self._current is not None or self._pending is not None:
                raise RuntimeError("capture request already in flight")
            work = _CaptureWork(request, operation, [callback])
            self._pending = work
            self._queue.put_nowait(work)
            return request.request_id

    def close(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # A pending request will be converted into a cancelled result by
            # the worker.  Shutdown must not block on a full single-flight
            # queue, especially if a callback previously failed.
            pass
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
        if not self._thread.is_alive():
            with self._lock:
                self._current = None
                self._pending = None

    def _run(self) -> None:
        while True:
            work = self._queue.get()
            if work is None:
                return
            with self._lock:
                self._pending = None
                self._current = work
            if self._closed.is_set():
                result = CaptureResult(
                    request_id=work.request.request_id,
                    run_id=work.request.run_id,
                    generation=work.request.generation,
                    capture_id=uuid4().hex,
                    captured_at=0.0,
                    hwnd=None,
                    client_origin=(0, 0),
                    client_size=(0, 0),
                    error="RuntimeError: capture coordinator closed before capture started",
                )
            else:
                try:
                    result = work.operation(work.request)
                except BaseException as exc:
                    result = CaptureResult(
                        request_id=work.request.request_id,
                        run_id=work.request.run_id,
                        generation=work.request.generation,
                        capture_id=uuid4().hex,
                        captured_at=0.0,
                        hwnd=None,
                        client_origin=(0, 0),
                        client_size=(0, 0),
                        error=f"{type(exc).__name__}: {exc}",
                    )
            with self._lock:
                self._current = None
                callbacks = tuple(work.callbacks)
            for callback in callbacks:
                try:
                    callback(result)
                except BaseException:
                    logging.exception(
                        "capture coordinator callback failed request_id=%s",
                        work.request.request_id,
                    )
            if self._closed.is_set() and self._queue.empty():
                return
