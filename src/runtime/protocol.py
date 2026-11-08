from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4


class RunMode(str, Enum):
    REALTIME = "realtime"
    INDEPENDENT = "independent"
    AUTOMATIC = "automatic"


class WorkerState(str, Enum):
    IDLE = "idle"
    PREFLIGHT = "preflight"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True)
class RunProfile:
    mode: RunMode = RunMode.AUTOMATIC
    task_id: str | None = None
    dry_run: bool = False
    overlay: bool | None = None
    continuous: bool = False
    max_tasks: int | None = 1
    detector_enabled: bool | None = None
    detector_model: str | None = None
    detector_confidence: float | None = None
    detector_iou: float | None = None
    detector_imgsz: int | None = None
    detector_device: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "RunProfile":
        data = dict(raw or {})
        mode_value = data.pop("mode", RunMode.AUTOMATIC.value)
        if str(mode_value).strip().lower() == "auto":
            mode_value = RunMode.AUTOMATIC.value
        try:
            mode = mode_value if isinstance(mode_value, RunMode) else RunMode(str(mode_value))
        except ValueError as exc:
            raise ValueError(f"unsupported run mode: {mode_value!r}") from exc
        profile = cls(mode=mode, **{key: value for key, value in data.items() if key in cls.__dataclass_fields__})
        profile.validate()
        return profile

    def validate(self) -> None:
        if self.mode is RunMode.INDEPENDENT and not str(self.task_id or "").strip():
            raise ValueError("independent mode requires task_id")
        if self.max_tasks is not None and int(self.max_tasks) <= 0:
            raise ValueError("max_tasks must be positive or null")
        for name, value in (
            ("detector_confidence", self.detector_confidence),
            ("detector_iou", self.detector_iou),
        ):
            if value is not None and not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.detector_imgsz is not None and int(self.detector_imgsz) <= 0:
            raise ValueError("detector_imgsz must be positive")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mode"] = self.mode.value
        return data


@dataclass(frozen=True)
class RuntimeEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    seq: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str, separators=(",", ":"))


class EventEmitter:
    """Thread-safe JSONL event writer used by the worker and task services."""

    def __init__(self, stream: TextIO, *, run_id: str | None = None) -> None:
        self.stream = stream
        self.run_id = run_id or uuid4().hex
        self._seq = 0
        self._lock = threading.Lock()

    def new_run(self, run_id: str | None = None) -> str:
        with self._lock:
            self.run_id = run_id or uuid4().hex
            self._seq = 0
            return self.run_id

    def emit(self, event_type: str, **payload: Any) -> RuntimeEvent:
        with self._lock:
            self._seq += 1
            event = RuntimeEvent(
                type=str(event_type),
                payload=payload,
                run_id=self.run_id,
                seq=self._seq,
            )
            self.stream.write(event.to_json() + "\n")
            self.stream.flush()
            return event


def decode_command(line: str) -> tuple[str, dict[str, Any], str | None]:
    raw = json.loads(line)
    if not isinstance(raw, dict):
        raise ValueError("worker command must be a JSON object")
    command = str(raw.get("command", raw.get("type", ""))).strip().lower()
    if not command:
        raise ValueError("worker command is missing command")
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("worker command payload must be an object")
    request_id = raw.get("request_id")
    return command, payload, None if request_id is None else str(request_id)


def encode_command(command: str, payload: dict[str, Any] | None = None, *, request_id: str | None = None) -> bytes:
    message: dict[str, Any] = {"command": command, "payload": payload or {}}
    if request_id is not None:
        message["request_id"] = request_id
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path
