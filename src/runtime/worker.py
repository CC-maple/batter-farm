from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from src.core.hotkey import RunControl
from src.runtime.application import apply_run_profile, create_task_session, run_profile
from src.runtime.action_executor import ActionExecutor
from src.runtime.preflight import run_preflight
from src.runtime.protocol import EventEmitter, RunProfile, WorkerState, decode_command
from src.runtime.scheduler import SchedulerCore, SchedulerThread
from src.runtime.scheduler_adapter import SchedulerAdapter, SchedulerDefinitions
from src.runtime.scheduler_models import ActionRequest, ActionResult, EventKind, RuntimeCommand, SchedulerState
from src.runtime.task_registry import TaskRegistry
from src.runtime.user_tasks import UserTaskStore, default_user_tasks_path, merge_user_task_steps


ROOT = Path(__file__).resolve().parents[2]


def configure_standard_streams() -> None:
    """Keep the JSONL protocol UTF-8 regardless of the Windows code page."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


class EventLogHandler(logging.Handler):
    def __init__(self, emitter: EventEmitter) -> None:
        super().__init__()
        self.emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.emitter.emit("log", level=record.levelname, message=self.format(record))
        except Exception:
            self.handleError(record)


class WorkerRuntime:
    def __init__(
        self,
        config: dict[str, Any],
        root: Path,
        emitter: EventEmitter,
        *,
        config_path: Path | None = None,
    ) -> None:
        self.base_config = config
        self.root = root
        self.emitter = emitter
        self.config_path = config_path
        self.registry = TaskRegistry.from_config(config)
        self.session = None
        self.scheduler_definitions = SchedulerAdapter.from_config(config)
        self._ensure_scheduler_mode_supported(self.scheduler_definitions)
        self.scheduler_core = SchedulerCore(
            max_pending_jobs=self.scheduler_definitions.scheduler.max_pending_events,
        )
        self.action_executor = ActionExecutor()
        self.scheduler = SchedulerThread(self._handle_runtime_command)
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._run_active = threading.Event()
        self._active_control: RunControl | None = None
        self._active_action_id: str | None = None

    @property
    def busy(self) -> bool:
        return self._run_active.is_set()

    @property
    def action_thread(self) -> threading.Thread:
        """Compatibility view of the long-lived action executor thread."""

        return self.action_executor.thread

    def dispatch(self, command: str, payload: dict[str, Any], request_id: str | None) -> bool:
        kind = {
            "start": EventKind.START_REQUESTED,
            "pause": EventKind.PAUSE_REQUESTED,
            "resume": EventKind.RESUME_REQUESTED,
            "stop": EventKind.STOP_REQUESTED,
            "emergency_stop": EventKind.EMERGENCY_STOP_REQUESTED,
            "rescan": EventKind.RESCAN_REQUESTED,
            "set_overlay": EventKind.OVERLAY_PREFERENCE_CHANGED,
            "shutdown": EventKind.SHUTDOWN_REQUESTED,
        }.get(command, EventKind.REFRESH_DUE)
        result = self.scheduler.submit(
            RuntimeCommand(
                kind=kind,
                payload={"command": command, "payload": payload},
                request_id=request_id,
                created_at=self.scheduler_core.clock.monotonic(),
            )
        )
        return bool(result)

    def _handle_runtime_command(self, runtime_command: RuntimeCommand) -> bool:
        command = str(runtime_command.payload["command"])
        payload = dict(runtime_command.payload.get("payload", {}))
        request_id = runtime_command.request_id
        if command == "_action_finished":
            self._finish_action(payload)
        elif command == "start":
            profile = RunProfile.from_dict(payload.get("profile", payload))
            self.start(profile, request_id=request_id)
        elif command == "pause":
            if not self.busy:
                self.emitter.emit("state", state=WorkerState.IDLE.value, request_id=request_id)
                return True
            self.scheduler_core.pause()
            self._require_active_control().request_pause()
            self.emitter.emit("state", state=WorkerState.PAUSED.value, request_id=request_id)
        elif command == "resume":
            if not self.busy:
                self.emitter.emit("state", state=WorkerState.IDLE.value, request_id=request_id)
                return True
            self.scheduler_core.resume()
            self._require_active_control().request_resume()
            self.emitter.emit("state", state=WorkerState.RUNNING.value, request_id=request_id)
        elif command == "stop":
            self.stop(emergency=False, request_id=request_id)
        elif command == "emergency_stop":
            self.stop(emergency=True, request_id=request_id)
        elif command == "toggle_overlay":
            self._require_session().control.request_overlay_toggle()
        elif command == "set_overlay":
            self._require_session().control.request_overlay_visibility(bool(payload.get("visible", True)))
        elif command == "rescan":
            self.scheduler_core.request_rescan()
            self._require_session().control.request_rescan()
        elif command == "interrupt":
            self._require_session().control.request_interrupt()
        elif command == "preflight":
            checks = [item.to_dict() for item in run_preflight(self.base_config, self.root)]
            self.emitter.emit("preflight", checks=checks, request_id=request_id)
        elif command == "list_tasks":
            self.emitter.emit("task_list", tasks=self.registry.business_dicts(), request_id=request_id)
        elif command == "reload_tasks":
            self.reload_tasks(request_id=request_id)
        elif command == "shutdown":
            self.stop(emergency=False, request_id=request_id)
            self._shutdown.set()
            return False
        else:
            raise ValueError(f"unsupported worker command: {command}")
        return True

    def reload_tasks(self, *, request_id: str | None) -> None:
        if self.busy:
            raise RuntimeError("cannot reload tasks while an action is running")
        if self.config_path is None:
            raise RuntimeError("worker config path is unavailable")
        config = load_config(self.config_path, root=self.root)
        registry = TaskRegistry.from_config(config)
        scheduler_definitions = SchedulerAdapter.from_config(config)
        self._ensure_scheduler_mode_supported(scheduler_definitions)
        self.base_config = config
        self.registry = registry
        self.scheduler_definitions = scheduler_definitions
        self.emitter.emit("task_list", tasks=self.registry.business_dicts(), request_id=request_id)

    def start(self, profile: RunProfile, *, request_id: str | None) -> None:
        with self._lock:
            if self.busy:
                raise RuntimeError("an action task is already running")
            config = apply_run_profile(self.base_config, profile)
            reusable_session = (
                self.session is not None
                and self.session.config == config
                and not self.session.control.stopped
            )
            control = self.session.control if reusable_session else RunControl()
            # A reusable control can receive F8 while the worker is idle.  Each
            # accepted start request establishes a fresh, unpaused run boundary.
            control.request_resume()
            run_id = self.emitter.new_run()
            self.scheduler_core.begin_run(run_id)
            request = ActionRequest(
                action_id=uuid4().hex,
                run_id=run_id,
                generation=self.scheduler_core.generation,
                name="legacy_run_profile",
                payload={"mode": profile.mode.value, "task_id": profile.task_id},
            )
            self._run_active.set()
            self._active_control = control
            self._active_action_id = request.action_id
            self.emitter.emit("state", state=WorkerState.STARTING.value, mode=profile.mode.value, request_id=request_id)
            try:
                self.action_executor.submit(
                    request,
                    lambda: self._run_action(config, profile, control),
                    callback=self._on_action_result,
                )
            except BaseException:
                self._active_action_id = None
                self._active_control = None
                self._run_active.clear()
                self.scheduler_core.reset_idle()
                self.emitter.emit(
                    "state",
                    state=WorkerState.IDLE.value,
                    request_id=request_id,
                )
                raise
            self.scheduler_core.set_state(SchedulerState.EXECUTING)

    def _on_action_result(self, result: ActionResult) -> None:
        if result.error and isinstance(result.value, BaseException):
            logging.debug("legacy action executor captured result error: %s", result.error)
        outcome = dict(result.value) if isinstance(result.value, dict) else {}
        try:
            self.scheduler.submit(
                RuntimeCommand(
                    kind=EventKind.ACTION_COMPLETED if not result.error else EventKind.ACTION_FAILED,
                    payload={
                        "command": "_action_finished",
                        "payload": {
                            "action_id": result.action_id,
                            "run_id": result.run_id,
                            "generation": result.generation,
                            "error": result.error,
                            "outcome": outcome,
                        },
                    },
                    created_at=self.scheduler_core.clock.monotonic(),
                ),
                wait=False,
            )
        except RuntimeError:
            logging.debug("scheduler closed before final action acknowledgement")

    def _finish_action(self, payload: dict[str, Any]) -> None:
        action_id = str(payload.get("action_id", ""))
        with self._lock:
            if action_id != self._active_action_id:
                logging.warning(
                    "ignored stale action completion action_id=%s active_action_id=%s",
                    action_id,
                    self._active_action_id,
                )
                return
            self._active_action_id = None
            self._active_control = None
            self._run_active.clear()

        outcome = payload.get("outcome", {})
        if not isinstance(outcome, dict):
            outcome = {}
        error = payload.get("error")
        exit_code = int(outcome.get("exit_code", 2))
        state = str(
            outcome.get(
                "state",
                WorkerState.ERROR.value if error else WorkerState.COMPLETED.value,
            )
        )
        mode = outcome.get("mode")
        task_id = outcome.get("task_id")
        self.scheduler_core.reset_idle()
        if error and not outcome:
            self.emitter.emit("error", message=str(error), error_type="ActionExecutorError")
        self.emitter.emit(
            "completed",
            state=state,
            exit_code=exit_code,
            mode=mode,
            task_id=task_id,
        )
        self.emitter.emit("state", state=WorkerState.IDLE.value, exit_code=exit_code)

    def _run_action(
        self,
        config: dict[str, Any],
        profile: RunProfile,
        control: RunControl,
    ) -> dict[str, Any]:
        exit_code = 2
        completion_state = WorkerState.ERROR
        try:
            if control.stopped:
                raise KeyboardInterrupt("worker action stopped before session creation")
            if self.session is None or self.session.config != config or self.session.control.stopped:
                if self.session is not None:
                    self.session.close()
                    self.session = None
                self.session = create_task_session(
                    config,
                    self.root,
                    event_sink=self.emitter,
                    start_hotkeys=True,
                    control=control,
                )
            if control.stopped:
                raise KeyboardInterrupt("worker action stopped before profile start")
            self.emitter.emit("state", state=WorkerState.RUNNING.value, mode=profile.mode.value, task_id=profile.task_id)
            exit_code = run_profile(self.session, profile)
            completion_state = WorkerState.COMPLETED if exit_code in {0, 130} else WorkerState.ERROR
        except KeyboardInterrupt:
            exit_code = 130
            completion_state = WorkerState.COMPLETED
            logging.info("worker action stopped by run control")
            self.emitter.emit("task_result", status="stopped", message="任务已安全停止")
        except Exception as exc:
            completion_state = WorkerState.ERROR
            logging.exception("worker action failed")
            self.emitter.emit("error", message=str(exc), error_type=type(exc).__name__)
        finally:
            if self.session is not None:
                try:
                    self.session.input_controller.release_all()
                except Exception:
                    logging.exception("failed to release input while finishing worker action")
                try:
                    # Tk windows must be destroyed by the action thread that
                    # created them.  The task object remains reusable and will
                    # lazily recreate the overlay on a later run.
                    self.session.task.close()
                except Exception:
                    logging.exception("failed to close overlay while finishing worker action")
        return {
            "state": completion_state.value,
            "exit_code": exit_code,
            "mode": profile.mode.value,
            "task_id": profile.task_id,
        }

    def stop(self, *, emergency: bool, request_id: str | None) -> None:
        if not self.busy:
            self.scheduler_core.reset_idle()
            self.emitter.emit("state", state=WorkerState.IDLE.value, request_id=request_id)
            return
        self.emitter.emit("state", state=WorkerState.STOPPING.value, emergency=emergency, request_id=request_id)
        self.scheduler_core.stop()
        with self._lock:
            control = self._active_control
        if control is not None:
            control.request_stop()
        session = self.session
        if emergency and session is not None:
            session.input_controller.release_all()

    def close(self) -> None:
        with self._lock:
            control = self._active_control
        if control is not None:
            control.request_stop()
        session = self.session
        if session is not None:
            session.control.request_stop()
            session.input_controller.release_all()
        self.action_executor.wait_idle(timeout=2.0)
        self.scheduler.close()
        session = self.session
        if session is not None and not self.busy:
            session.close()
        self.action_executor.close()

    @staticmethod
    def _ensure_scheduler_mode_supported(definitions: SchedulerDefinitions) -> None:
        settings = definitions.scheduler
        if settings.enabled or settings.mode != "legacy":
            raise ValueError(
                "scheduler shadow/unified modes are not connected to the worker; "
                "use scheduler.enabled=false and scheduler.mode=legacy"
            )

    def _require_session(self):
        if self.session is None:
            raise RuntimeError("no task is running")
        return self.session

    def _require_active_control(self) -> RunControl:
        with self._lock:
            control = self._active_control
        if control is None:
            raise RuntimeError("no task is running")
        return control


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BatterFarm JSONL worker")
    parser.add_argument("--config", default="configs/config.yaml")
    return parser.parse_args()


def load_config(path: str | Path, *, root: Path = ROOT) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = root / config_path
    config_path = config_path.resolve()
    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}
    user_store = UserTaskStore(default_user_tasks_path(config_path), root)
    return merge_user_task_steps(config, user_store.load_steps())


def configure_logging(config: dict[str, Any], emitter: EventEmitter) -> None:
    level_name = str(config.get("logging", {}).get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    event_handler = EventLogHandler(emitter)
    event_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(event_handler)
    log_path = ROOT / str(config.get("logging", {}).get("file", "logs/batterfarm.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root_logger.addHandler(logging.FileHandler(log_path, encoding="utf-8"))


def main() -> int:
    configure_standard_streams()
    args = parse_args()
    emitter = EventEmitter(sys.stdout)
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    config_path = config_path.resolve()
    config = load_config(config_path)
    configure_logging(config, emitter)
    runtime = WorkerRuntime(config, ROOT, emitter, config_path=config_path)
    emitter.emit("ready", state=WorkerState.IDLE.value, protocol=1)
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            request_id = None
            try:
                command, payload, request_id = decode_command(line)
                if not runtime.dispatch(command, payload, request_id):
                    break
            except Exception as exc:
                emitter.emit("command_error", message=str(exc), error_type=type(exc).__name__, request_id=request_id)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
