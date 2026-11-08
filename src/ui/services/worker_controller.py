from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from src.runtime.protocol import encode_command
from src.runtime.task_registry import TaskRegistry
from src.runtime.user_tasks import UserTaskStore, default_user_tasks_path, merge_user_task_steps
from src.utils.window import activate_window


class WorkerController(QObject):
    worker_ready = Signal()
    event_received = Signal(dict)
    tasks_received = Signal(list)
    preflight_received = Signal(list)
    state_changed = Signal(str, dict)
    log_received = Signal(str, str)
    process_error = Signal(str)
    command_failed = Signal(str, str)

    def __init__(self, root: Path, config_path: Path, *, demo: bool = False, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.root = root
        self.config_path = config_path
        self.demo = demo
        self.process = QProcess(self)
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._ready = False
        self._closing = False
        self._terminal_failure_reported = False
        self._last_seq_by_run: dict[str, int] = {}
        self._demo_active_request_id = ""
        self._demo_stop_request_id = ""
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._on_process_error)
        self.process.finished.connect(self._on_finished)

    @property
    def ready(self) -> bool:
        return self._ready

    def start_worker(self) -> bool:
        if self.demo:
            QTimer.singleShot(60, self._start_demo)
            return True
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return False
        self._ready = False
        self._closing = False
        self._terminal_failure_reported = False
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._last_seq_by_run.clear()
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.insert("PYTHONUTF8", "1")
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONNOUSERSITE", "1")
        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(str(self.root))
        self.process.setProgram(sys.executable)
        self.process.setArguments(["-s", "-u", "-m", "src.runtime.worker", "--config", str(self.config_path)])
        self.process.start()
        return True

    def send(self, command: str, payload: dict[str, Any] | None = None) -> str:
        request_id = uuid4().hex
        if self.demo:
            self._dispatch_demo(command, payload or {}, request_id)
            return request_id
        if not self._ready or self.process.state() != QProcess.ProcessState.Running:
            self.process_error.emit("运行 worker 尚未就绪")
            return request_id
        self.process.write(encode_command(command, payload, request_id=request_id))
        return request_id

    def start_profile(self, profile: dict[str, Any]) -> str:
        return self.send("start", {"profile": profile})

    def reload_tasks(self) -> str:
        return self.send("reload_tasks")

    def activate_target_window(self) -> bool:
        """Focus the configured game from the UI thread that received the click."""

        if self.demo:
            return True
        try:
            # application.py pulls in the input/task stack.  Import it only
            # after QApplication has been constructed by src.ui.app.
            from src.runtime.application import resolve_target_selector

            with self.config_path.open("r", encoding="utf-8") as file:
                config = yaml.safe_load(file) or {}
            selector, _, _ = resolve_target_selector(config.get("window", {}))
            bounds = activate_window(selector=selector, settle_seconds=0.15)
        except Exception as exc:
            self.process_error.emit(f"无法将游戏窗口切换到前台：{exc}")
            return False
        if bounds is None:
            self.process_error.emit("无法将游戏窗口切换到前台：未找到目标窗口")
            return False
        return True

    def close(self) -> None:
        self._closing = True
        if self.demo:
            self._demo_active_request_id = ""
            self._demo_stop_request_id = ""
            return
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        if self._ready:
            self.process.write(encode_command("emergency_stop", request_id="ui-close-release"))
            self.process.write(encode_command("shutdown", request_id="ui-close"))
            self.process.waitForBytesWritten(300)
        if not self.process.waitForFinished(1500):
            self._release_input_before_force_termination()
            self.process.terminate()
            if not self.process.waitForFinished(1000):
                self.process.kill()
                self.process.waitForFinished(1000)

    def _release_input_before_force_termination(self) -> bool:
        """Release known task inputs only after strict target-window verification."""

        try:
            from src.core.input import InputController, is_admin
            from src.utils.window import activate_window, parse_window_selector, resolve_window

            if not sys.platform.startswith("win"):
                raise RuntimeError("UI 强制释放仅支持 Windows SendInput")

            with self.config_path.open("r", encoding="utf-8") as file:
                config = yaml.safe_load(file) or {}
            if not isinstance(config, dict):
                raise RuntimeError("配置根节点不是映射")

            runtime_config = config.get("runtime", {})
            if not isinstance(runtime_config, dict):
                runtime_config = {}
            if bool(runtime_config.get("require_admin", True)) and not is_admin():
                raise RuntimeError("当前 UI 未获得管理员权限")

            window_config = config.get("window", {})
            if not isinstance(window_config, dict):
                raise RuntimeError("window 配置不是映射")
            selector = parse_window_selector(window_config)
            complete_identity = bool(
                selector.title.strip()
                and selector.process_name.strip()
                and selector.class_name.strip()
            )
            if selector.hwnd is None and not complete_identity:
                raise RuntimeError("必须配置 HWND，或完整配置窗口标题、进程名和窗口类")
            resolved = resolve_window(selector)
            if resolved is None or int(resolved.hwnd) <= 0:
                raise RuntimeError("无法精确解析目标窗口")
            if activate_window(selector=selector, settle_seconds=0.1) is None:
                raise RuntimeError("无法精确激活目标窗口")

            input_config = config.get("input", {})
            if not isinstance(input_config, dict):
                input_config = {}
            backend = str(input_config.get("click_backend", "sendinput")).strip().lower()
            if backend not in {"sendinput", "send_input"}:
                raise RuntimeError(f"UI 强制释放仅支持 SendInput，当前为 {backend or '未配置'}")
            controller = InputController(
                click_delay_seconds=0.0,
                click_hold_seconds=0.0,
                move_duration_seconds=0.0,
                click_backend="sendinput",
                fail_safe=bool(input_config.get("fail_safe", True)),
                target_window_title=resolved.title,
                target_window_hwnd=int(resolved.hwnd),
                enabled=True,
            )
            controller.assert_target_window_active()
            controller.held_keys.update({"shift", "ctrl", "alt", "win", "w", "a", "s", "d", "e", "q"})
            controller.held_mouse_buttons.update({"left", "right", "middle", "x1", "x2"})
            result = controller.release_all()
            remaining_keys = frozenset(controller.held_keys)
            remaining_mouse_buttons = frozenset(controller.held_mouse_buttons)
            if not result.success or remaining_keys or remaining_mouse_buttons:
                residual = []
                if remaining_keys:
                    residual.append(f"held_keys={sorted(remaining_keys)}")
                if remaining_mouse_buttons:
                    residual.append(f"held_mouse_buttons={sorted(remaining_mouse_buttons)}")
                detail = result.failure_summary()
                if residual:
                    detail = f"{detail}; {'; '.join(residual)}"
                raise RuntimeError(f"SendInput 释放未完成：{detail}")
        except Exception as exc:
            self.log_received.emit("ERROR", f"worker 强制终止前未能安全释放输入：{exc}")
            return False
        self.log_received.emit("WARNING", "worker 协作关闭超时；已在精确目标窗口上执行 UI 侧输入释放。")
        return True

    def _read_stdout(self) -> None:
        chunk = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._stdout_buffer += chunk
        lines = self._stdout_buffer.split("\n")
        self._stdout_buffer = lines.pop()
        for line in lines:
            self._handle_line(line.strip())

    def _read_stderr(self) -> None:
        chunk = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        self._stderr_buffer += chunk
        lines = self._stderr_buffer.split("\n")
        self._stderr_buffer = lines.pop()
        for line in lines:
            if line.strip():
                self.log_received.emit("ERROR", line.strip())

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.log_received.emit("INFO", line)
            return
        if not isinstance(event, dict):
            return
        run_id = str(event.get("run_id", ""))
        seq = int(event.get("seq", 0) or 0)
        if run_id and seq:
            previous = self._last_seq_by_run.get(run_id, 0)
            if seq <= previous:
                return
            self._last_seq_by_run[run_id] = seq
        self.event_received.emit(event)
        event_type = str(event.get("type", ""))
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        if event_type == "ready":
            self._ready = True
            self.worker_ready.emit()
            self.send("list_tasks")
            self.send("preflight")
        elif event_type == "task_list":
            self.tasks_received.emit(list(payload.get("tasks", [])))
        elif event_type == "preflight":
            self.preflight_received.emit(list(payload.get("checks", [])))
        elif event_type == "state":
            self.state_changed.emit(str(payload.get("state", "unknown")), payload)
        elif event_type == "completed":
            self.state_changed.emit(str(payload.get("state", "completed")), payload)
        elif event_type == "log":
            self.log_received.emit(str(payload.get("level", "INFO")), str(payload.get("message", "")))
        elif event_type == "command_error":
            message = str(payload.get("message", "未知 worker 命令错误"))
            self.command_failed.emit(str(payload.get("request_id", "")), message)
        elif event_type == "error":
            self.process_error.emit(str(payload.get("message", "未知 worker 错误")))

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        if self._closing:
            return
        message = f"worker 进程错误：{error.name}"
        if error == QProcess.ProcessError.FailedToStart:
            self._report_worker_unavailable({"process_error": error.name}, message)
            return
        self.process_error.emit(message)

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._ready = False
        if not self._closing:
            self._report_worker_unavailable(
                {"exit_code": exit_code},
                f"worker 已退出，退出码 {exit_code}",
            )

    def _report_worker_unavailable(self, payload: dict[str, Any], message: str) -> None:
        self._ready = False
        if self._terminal_failure_reported:
            return
        self._terminal_failure_reported = True
        details = dict(payload)
        details["worker_unavailable"] = True
        self.state_changed.emit("error", details)
        self.process_error.emit(message)

    def _start_demo(self) -> None:
        self._ready = True
        self.worker_ready.emit()
        tasks = self._load_demo_tasks()
        self.tasks_received.emit(tasks)
        self.preflight_received.emit(
            [
                {"id": "admin", "label": "管理员权限", "ok": True, "blocking": True, "detail": "已获得（演示）"},
                {"id": "window", "label": "游戏窗口", "ok": True, "blocking": True, "detail": "元梦之星"},
                {"id": "model", "label": "检测模型", "ok": True, "blocking": True, "detail": "best.onnx"},
                {"id": "tasks", "label": "任务配置", "ok": True, "blocking": True, "detail": f"{len(tasks)} 个步骤"},
            ]
        )
        self.state_changed.emit("idle", {"demo": True})
        self.log_received.emit("INFO", "演示模式已就绪，未连接游戏或发送输入。")

    def _dispatch_demo(self, command: str, payload: dict[str, Any], request_id: str) -> None:
        if command == "preflight":
            return
        if command == "start":
            profile = payload.get("profile", payload)
            mode = str(profile.get("mode", "automatic"))
            task_id = profile.get("task_id")
            self._demo_active_request_id = request_id
            self._demo_stop_request_id = ""
            self.state_changed.emit(
                "starting",
                {"mode": mode, "task_id": task_id, "request_id": request_id},
            )
            QTimer.singleShot(
                250,
                lambda: self._finish_demo_start(request_id, mode, task_id),
            )
        elif command in {"stop", "emergency_stop"}:
            self._demo_active_request_id = ""
            self._demo_stop_request_id = request_id
            self.state_changed.emit("stopping", {"emergency": command == "emergency_stop"})
            QTimer.singleShot(180, lambda: self._finish_demo_stop(request_id))
        elif command == "pause":
            self.state_changed.emit("paused", {})
        elif command == "resume":
            self.state_changed.emit("running", {})
        elif command in {"list_tasks", "reload_tasks"}:
            self.tasks_received.emit(self._load_demo_tasks())

    def _finish_demo_start(self, request_id: str, mode: str, task_id: object) -> None:
        if self._demo_active_request_id != request_id:
            return
        self.state_changed.emit("running", {"mode": mode, "task_id": task_id})
        self.log_received.emit("INFO", f"演示运行：mode={mode} task={task_id or '-'}")

    def _finish_demo_stop(self, request_id: str) -> None:
        if self._demo_stop_request_id != request_id:
            return
        self._demo_stop_request_id = ""
        self.state_changed.emit("idle", {"request_id": request_id})

    def _load_demo_tasks(self) -> list[dict[str, Any]]:
        try:
            with self.config_path.open("r", encoding="utf-8") as file:
                config = yaml.safe_load(file) or {}
            store = UserTaskStore(default_user_tasks_path(self.config_path), self.root)
            merged = merge_user_task_steps(config, store.load_steps())
            return TaskRegistry.from_config(merged).as_dicts()
        except Exception as exc:
            self.log_received.emit("ERROR", f"无法加载演示任务注册表：{exc}")
            return []
