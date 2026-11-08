from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal


class InputToolController(QObject):
    state_changed = Signal(str, dict)
    log_received = Signal(str, str)
    process_error = Signal(str)
    recording_launch_requested = Signal(dict)

    def __init__(
        self,
        root: Path,
        config_path: Path,
        *,
        demo: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.root = root
        self.config_path = config_path
        self.demo = demo
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._mode = ""
        self._stdout_buffer = ""
        self._stop_requested = False
        self._demo_busy = False
        self._saved_recording: Path | None = None
        self._countdown_active = False
        self._awaiting_recording_focus = False
        self._countdown_deadline = 0.0
        self._countdown_last_second: int | None = None
        self._pending_recording_options: dict = {}
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(100)
        self._countdown_timer.timeout.connect(self._tick_recording_countdown)
        self.process.started.connect(self._on_started)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.errorOccurred.connect(self._on_process_error)
        self.process.finished.connect(self._on_finished)

    @property
    def busy(self) -> bool:
        return (
            self._countdown_active
            or self._awaiting_recording_focus
            or self._demo_busy
            or self.process.state() != QProcess.ProcessState.NotRunning
        )

    def recording_arguments(self, options: dict | None = None) -> list[str]:
        values = dict(options or {})
        mode = str(values.get("mode", "visible")).strip().lower()
        if mode not in {"visible", "hidden"}:
            mode = "visible"
        sample_rate_hz = max(1, int(values.get("sample_rate_hz", 100)))
        interval_ms = max(1, int(round(1000 / sample_rate_hz)))
        arguments = [
            "-s",
            "-u",
            str(self.root / "tools" / "recording" / "record_input_actions.py"),
            "--config",
            str(self.config_path),
            "--countdown",
            "0.0",
            "--camera-mode",
            mode,
            "--move-sample-ms",
            str(interval_ms),
            "--step-dx",
            str(max(1, int(values.get("step_dx", 4)))),
            "--interval-ms",
            str(interval_ms),
            "--anchor-x",
            str(max(0, int(values.get("anchor_x", 800)))),
            "--anchor-y",
            str(max(0, int(values.get("anchor_y", 450)))),
            "--segment-ms",
            str(max(1, int(values.get("segment_ms", 250)))),
            "--segment-gap-ms",
            str(max(0, int(values.get("segment_gap_ms", 30)))),
            "--overlay",
        ]
        if bool(values.get("invert", False)):
            arguments.append("--invert")
        return arguments

    def replay_arguments(self, recording: Path) -> list[str]:
        return [
            "-s",
            "-u",
            str(self.root / "tools" / "recording" / "replay_recording.py"),
            str(recording),
        ]

    def start_recording(self, options: dict | None = None) -> bool:
        script = self.root / "tools" / "recording" / "record_input_actions.py"
        if not script.is_file():
            self.process_error.emit(f"找不到键鼠录制脚本：{script}")
            return False
        if self.busy:
            self.process_error.emit("已有录制或回放工具正在运行")
            return False
        values = dict(options or {})
        countdown_seconds = max(0.0, float(values.get("countdown_seconds", 5.0)))
        self._mode = "recording"
        self._stop_requested = False
        self._saved_recording = None
        self._pending_recording_options = values
        if countdown_seconds <= 0:
            self._request_recording_focus()
            return True
        self._countdown_active = True
        self._countdown_deadline = time.monotonic() + countdown_seconds
        self._countdown_last_second = None
        self._tick_recording_countdown()
        self._countdown_timer.start()
        return True

    def confirm_recording_focus(self, success: bool) -> None:
        if not self._awaiting_recording_focus:
            return
        options = dict(self._pending_recording_options)
        self._awaiting_recording_focus = False
        self._pending_recording_options = {}
        if not success:
            self._mode = ""
            self.state_changed.emit("error", {"mode": "recording", "detail": "未能前置游戏窗口，录制未启动"})
            return
        self._start("recording", self.recording_arguments(options))

    def start_replay(self, recording: Path) -> bool:
        path = Path(recording).resolve()
        if not path.is_file() or path.suffix.lower() != ".json":
            self.process_error.emit(f"录制文件无效：{path}")
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.process_error.emit(f"无法读取键鼠录制文件：{exc}")
            return False
        replayable_types = {
            "key_down",
            "key_up",
            "mouse_move",
            "mouse_down",
            "mouse_up",
            "mouse_scroll",
            "camera_turn",
        }
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list) or not any(
            isinstance(event, dict) and str(event.get("type", "")) in replayable_types
            for event in events
        ):
            self.process_error.emit("所选 JSON 没有可回放的键鼠、点击或 F5/F6 转向事件")
            return False
        script = self.root / "tools" / "recording" / "replay_recording.py"
        if not script.is_file():
            self.process_error.emit(f"找不到键鼠回放脚本：{script}")
            return False
        return self._start("replaying", self.replay_arguments(path), recording=path)

    def request_stop(self) -> None:
        if not self.busy:
            return
        if self._countdown_active or self._awaiting_recording_focus:
            self._cancel_recording_countdown("录制倒计时已取消")
            return
        self._stop_requested = True
        self.state_changed.emit("stopping", {"mode": self._mode, "detail": "正在安全停止并释放输入…"})
        if self.demo:
            QTimer.singleShot(120, self._finish_demo_stop)
            return
        self.process.write(b"stop\n")

    def close(self) -> None:
        if not self.busy:
            return
        if self._countdown_active or self._awaiting_recording_focus:
            self._cancel_recording_countdown("录制倒计时已取消")
            return
        if self.demo:
            self._demo_busy = False
            return
        self._stop_requested = True
        self.process.write(b"stop\n")
        self.process.waitForBytesWritten(250)
        if not self.process.waitForFinished(2500):
            self.process.terminate()
            if not self.process.waitForFinished(1000):
                self.process.kill()
                self.process.waitForFinished(1000)

    def _start(self, mode: str, arguments: list[str], *, recording: Path | None = None) -> bool:
        if self.busy:
            self.process_error.emit("已有录制或回放工具正在运行")
            return False
        self._mode = mode
        self._stop_requested = False
        self._saved_recording = recording
        self._stdout_buffer = ""
        detail = "正在准备键鼠录制与 F5/F6 转向…" if mode == "recording" else f"正在准备键鼠回放：{recording.name}"
        self.state_changed.emit("starting", {"mode": mode, "detail": detail, "recording": str(recording or "")})
        if self.demo:
            self._demo_busy = True
            QTimer.singleShot(80, self._on_demo_started)
            return True

        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.insert("PYTHONUTF8", "1")
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONNOUSERSITE", "1")
        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(str(self.root))
        self.process.setProgram(sys.executable)
        self.process.setArguments(arguments)
        self.process.start()
        return True

    def _tick_recording_countdown(self) -> None:
        if not self._countdown_active:
            self._countdown_timer.stop()
            return
        remaining = max(0.0, self._countdown_deadline - time.monotonic())
        display_second = max(1, int(math.ceil(remaining))) if remaining > 0 else 0
        if display_second != self._countdown_last_second:
            self._countdown_last_second = display_second
            if display_second > 0:
                self.state_changed.emit(
                    "countdown",
                    {
                        "mode": "recording",
                        "remaining_seconds": display_second,
                        "detail": f"{display_second} 秒后切换到游戏窗口并开始录制",
                    },
                )
        if remaining <= 0:
            self._countdown_timer.stop()
            self._countdown_active = False
            self._request_recording_focus()

    def _request_recording_focus(self) -> None:
        self._awaiting_recording_focus = True
        options = dict(self._pending_recording_options)
        self.state_changed.emit(
            "starting",
            {"mode": "recording", "detail": "正在切换到游戏窗口…"},
        )
        self.recording_launch_requested.emit(options)

    def _cancel_recording_countdown(self, detail: str) -> None:
        self._countdown_timer.stop()
        self._countdown_active = False
        self._awaiting_recording_focus = False
        self._pending_recording_options = {}
        mode = self._mode or "recording"
        self._mode = ""
        self._stop_requested = False
        self.state_changed.emit("stopped", {"mode": mode, "detail": detail})

    def _on_started(self) -> None:
        if self._mode == "recording":
            self.state_changed.emit(
                "starting",
                {"mode": self._mode, "detail": "录制进程已启动，正在等待键盘和鼠标监听器就绪…"},
            )
            return
        else:
            detail = "键鼠回放中：F10、ESC 或“安全停止”结束"
        self.state_changed.emit(self._mode, {"mode": self._mode, "detail": detail})

    def _read_output(self) -> None:
        chunk = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._stdout_buffer += chunk
        lines = self._stdout_buffer.split("\n")
        self._stdout_buffer = lines.pop()
        for line in lines:
            self._handle_output_line(line.strip())

    def _handle_output_line(self, line: str) -> None:
        if not line:
            return
        self.log_received.emit("INFO", f"输入工具：{line}")
        if line.startswith("input recording ready:"):
            self.state_changed.emit(
                "recording",
                {
                    "mode": self._mode,
                    "detail": "录制中：WASD、普通键鼠和点击会被记录；F5 左转，F6 右转",
                },
            )
            return
        saved_prefix = "input recording saved="
        if line.startswith(saved_prefix):
            self._saved_recording = Path(line[len(saved_prefix) :].strip()).resolve()

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        if self._stop_requested and error == QProcess.ProcessError.Crashed:
            return
        message = f"录制/回放工具进程错误：{error.name}"
        self.state_changed.emit("error", {"mode": self._mode, "detail": message})
        self.process_error.emit(message)

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if self._stdout_buffer.strip():
            self._handle_output_line(self._stdout_buffer.strip())
        self._stdout_buffer = ""
        mode = self._mode
        recording = str(self._saved_recording or "")
        if exit_code == 0:
            detail = "录制已保存" if mode == "recording" else "回放已完成"
            state = "completed"
        elif exit_code == 130:
            detail = "已安全停止并释放输入"
            state = "stopped"
        else:
            detail = f"工具异常退出，退出码 {exit_code}"
            state = "error"
        self._mode = ""
        self._stop_requested = False
        self.state_changed.emit(
            state,
            {"mode": mode, "detail": detail, "recording": recording, "exit_code": exit_code},
        )
        if state == "error":
            self.process_error.emit(detail)

    def _on_demo_started(self) -> None:
        if not self._demo_busy:
            return
        detail = "演示录制中：未监听键鼠" if self._mode == "recording" else "演示回放中：未发送输入"
        self.state_changed.emit(self._mode, {"mode": self._mode, "detail": detail})

    def _finish_demo_stop(self) -> None:
        if not self._demo_busy:
            return
        mode = self._mode
        self._demo_busy = False
        self._mode = ""
        self._stop_requested = False
        self.state_changed.emit("stopped", {"mode": mode, "detail": "演示工具已停止"})
