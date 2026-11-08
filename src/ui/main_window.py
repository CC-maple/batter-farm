from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from src.ui.pages.automatic import AutomaticPage
from src.ui.pages.detector import DetectorPage
from src.ui.pages.home import HomePage
from src.ui.pages.independent import IndependentPage
from src.ui.pages.logs import LogsPage
from src.ui.pages.realtime import RealtimePage
from src.ui.pages.routes import RoutesPage
from src.ui.pages.settings import SettingsPage
from src.ui.services.input_tool_controller import InputToolController
from src.ui.services.worker_controller import WorkerController
from src.ui.widgets.common import StatusPill
from src.runtime.user_tasks import UserTaskStore, default_user_tasks_path


NAVIGATION = [
    ("概览", [("home", "运行中心")]),
    ("自动化", [("realtime", "实时触发"), ("independent", "独立任务"), ("automatic", "全自动")]),
    ("资源", [("routes", "任务与路线"), ("detector", "识别测试")]),
    ("记录", [("logs", "实时日志")]),
    ("系统", [("settings", "设置")]),
]


class MainWindow(QMainWindow):
    def __init__(self, root: Path, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
        self.root = root
        self.config_path = config_path
        self.demo = demo
        self.blocking_preflight_failures: list[dict] = []
        self.preflight_ready = False
        self.start_pending = False
        self.worker_state = "starting"
        self.worker_payload: dict = {}
        self.active_mode = ""
        self.monitor_profile: dict | None = None
        self.pending_action_profile: dict | None = None
        self.handoff_phase = "none"
        self._pending_start_mode = ""
        self._pending_start_purpose = ""
        self._pending_start_request_id = ""
        self._confirmed_start_mode = ""
        self._confirmed_start_purpose = ""
        self._confirmed_start_request_id = ""
        self._handoff_stop_request_id = ""
        self._monitor_stop_request_id = ""
        self._monitor_stop_profile: dict | None = None
        self.user_task_store = UserTaskStore(default_user_tasks_path(config_path), root)
        self.setWindowTitle("BatterUI · 农场自动化运行中心")
        self.resize(1280, 820)
        self.setMinimumSize(1080, 700)
        self.setObjectName("MainWindow")

        app_root = QWidget()
        app_root.setObjectName("AppRoot")
        root_layout = QHBoxLayout(app_root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        self.stack.setObjectName("PageStack")
        root_layout.addWidget(self.stack, 1)
        self.setCentralWidget(app_root)

        self.pages = {
            "home": HomePage(),
            "realtime": RealtimePage(),
            "independent": IndependentPage(),
            "automatic": AutomaticPage(),
            "routes": RoutesPage(root),
            "detector": DetectorPage(root),
            "logs": LogsPage(),
            "settings": SettingsPage(root, config_path),
        }
        for page_id in [item[0] for _, items in NAVIGATION for item in items]:
            self.stack.addWidget(self.pages[page_id])

        self.controller = WorkerController(root, config_path, demo=demo, parent=self)
        self.input_tools = InputToolController(root, config_path, demo=demo, parent=self)
        self.restart_worker_button.clicked.connect(self._restart_worker)
        self._connect_pages()
        self._connect_controller()
        self._connect_input_tools()
        self.show_page("home")
        self._update_runtime_pages("starting", {})
        if not self.controller.start_worker():
            self.worker_state = "error"
            self.worker_payload = {"worker_unavailable": True}
            self.restart_worker_button.setVisible(True)
            self.restart_worker_button.setEnabled(True)
            self.worker_pill.setText("异常")
            self.worker_pill.set_tone("danger")
            self._update_runtime_pages("error", self.worker_payload)
            self.pages["logs"].append_log("ERROR", "worker 尚未完全退出，暂时无法重连；请稍后重试。")

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(224)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(2)

        brand_row = QHBoxLayout()
        mark = QLabel("B")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(36, 36)
        mark.setStyleSheet("background: #7392FF; color: #07101F; border-radius: 10px; font-size: 20px; font-weight: 800;")
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        title = QLabel("BatterUI")
        title.setObjectName("BrandTitle")
        caption = QLabel("Farm automation")
        caption.setObjectName("BrandCaption")
        brand_text.addWidget(title)
        brand_text.addWidget(caption)
        brand_row.addWidget(mark)
        brand_row.addSpacing(8)
        brand_row.addLayout(brand_text)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)
        layout.addSpacing(12)

        self.nav_group = QButtonGroup(sidebar)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        for section, entries in NAVIGATION:
            section_label = QLabel(section.upper())
            section_label.setObjectName("NavSection")
            layout.addWidget(section_label)
            for page_id, label in entries:
                button = QPushButton(label)
                button.setObjectName("NavButton")
                button.setCheckable(True)
                button.clicked.connect(lambda _checked=False, target=page_id: self.show_page(target))
                self.nav_group.addButton(button)
                self.nav_buttons[page_id] = button
                layout.addWidget(button)
        layout.addStretch(1)

        divider = QFrame()
        divider.setObjectName("Divider")
        layout.addWidget(divider)
        footer = QHBoxLayout()
        self.worker_pill = StatusPill("连接中", "warning")
        footer.addWidget(self.worker_pill)
        footer.addStretch(1)
        footer.addWidget(QLabel("v0.1"))
        layout.addLayout(footer)
        self.restart_worker_button = QPushButton("重连 worker")
        self.restart_worker_button.setObjectName("DangerButton")
        self.restart_worker_button.setVisible(False)
        layout.addWidget(self.restart_worker_button)
        if self.demo:
            demo_label = QLabel("演示模式 · 不发送输入")
            demo_label.setObjectName("Muted")
            demo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(demo_label)
        return sidebar

    def _connect_pages(self) -> None:
        home: HomePage = self.pages["home"]
        home.navigate_requested.connect(self.show_page)
        home.realtime_start_requested.connect(self.controller_start)
        home.realtime_stop_requested.connect(self.stop_realtime)
        home.automatic_start_requested.connect(self.controller_start)
        realtime: RealtimePage = self.pages["realtime"]
        realtime.start_requested.connect(self.controller_start)
        realtime.stop_requested.connect(self.stop_realtime)
        realtime.overlay_visibility_requested.connect(self.set_monitor_overlay_visibility)
        independent: IndependentPage = self.pages["independent"]
        independent.start_task_requested.connect(self.start_independent)
        independent.stop_requested.connect(self.stop_action)
        automatic: AutomaticPage = self.pages["automatic"]
        automatic.start_requested.connect(self.controller_start)
        automatic.pause_requested.connect(lambda: self.controller.send("pause"))
        automatic.resume_requested.connect(lambda: self.controller.send("resume"))
        automatic.stop_requested.connect(self.stop_action)
        automatic.emergency_requested.connect(self.emergency_stop)
        routes: RoutesPage = self.pages["routes"]
        routes.record_requested.connect(self.start_input_recording)
        routes.replay_requested.connect(self.start_input_replay)
        routes.stop_tool_requested.connect(self.input_tools.request_stop)
        routes.custom_task_requested.connect(self.add_custom_task)

    def _connect_controller(self) -> None:
        self.controller.worker_ready.connect(self._worker_ready)
        self.controller.tasks_received.connect(self._set_tasks)
        self.controller.preflight_received.connect(self._preflight_received)
        self.controller.state_changed.connect(self._state_changed)
        self.controller.log_received.connect(self.pages["logs"].append_log)
        self.controller.process_error.connect(self._controller_error)
        self.controller.command_failed.connect(self._command_failed)
        self.controller.event_received.connect(self._handle_event)

    def _connect_input_tools(self) -> None:
        self.input_tools.state_changed.connect(self._input_tool_state_changed)
        self.input_tools.log_received.connect(self.pages["logs"].append_log)
        self.input_tools.process_error.connect(self._show_tool_error)
        self.input_tools.recording_launch_requested.connect(self._launch_recording_after_countdown)

    def _input_tool_state_changed(self, state: str, payload: dict) -> None:
        self.pages["routes"].set_tool_state(state, payload)
        self._sync_launch_availability()

    def show_page(self, page_id: str) -> None:
        page = self.pages.get(page_id)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        self.nav_buttons[page_id].setChecked(True)

    def controller_start(self, profile: dict) -> None:
        profile = dict(profile)
        mode = str(profile.get("mode"))
        if mode in {"automatic", "independent"}:
            profile.setdefault("overlay", False)
        if self.input_tools.busy:
            self._show_tool_error("键鼠录制或回放正在运行，请先安全停止")
            self.show_page("routes")
            return
        if not self.controller.ready:
            self._show_error("worker 尚未就绪，请稍后重试")
            return
        if not self.preflight_ready:
            self._show_error("运行前检查尚未完成，请稍后重试")
            return
        ignored_checks = {"admin"} if mode == "realtime" else set()
        blocking_failures = [
            item
            for item in self.blocking_preflight_failures
            if str(item.get("id", "")) not in ignored_checks
        ]
        if blocking_failures:
            labels = "、".join(str(item.get("label", item.get("id", "检查项"))) for item in blocking_failures)
            self._show_error(f"运行前检查未通过：{labels}")
            self.show_page("home")
            return

        if mode == "realtime":
            if not self._realtime_launchable():
                self._show_error("当前任务尚未结束，暂时不能启动实时识别")
                return
            if profile.get("overlay") is not None:
                self.pages["realtime"].set_overlay_visible(bool(profile["overlay"]))
            self.monitor_profile = profile
            if not self._launch_profile(profile, purpose="direct_monitor", activate=True):
                self.monitor_profile = None
                self._update_runtime_pages(self.worker_state, self.worker_payload)
            return

        if mode not in {"automatic", "independent"}:
            self._show_error(f"不支持的运行模式：{mode or '未指定'}")
            return
        if not self._action_launchable():
            self._show_error("已有动作任务正在运行，或实时识别正在切换，请稍后重试")
            return
        if self._stable_realtime_running():
            self._begin_action_handoff(profile)
            return
        if mode == "automatic":
            self.monitor_profile = {
                "mode": "realtime",
                "continuous": True,
                "overlay": True,
            }
            self.pages["realtime"].set_overlay_visible(True)
            profile["overlay"] = True
            self.handoff_phase = "starting_action"
            if self._launch_profile(profile, purpose="implicit_monitor_action", activate=True):
                return
            self.monitor_profile = None
            self.handoff_phase = "none"
            self._update_runtime_pages(self.worker_state, self.worker_payload)
            return
        self._launch_profile(profile, purpose="direct_action", activate=True)

    def _launch_profile(self, profile: dict, *, purpose: str, activate: bool) -> bool:
        profile = dict(profile)
        if not self.controller.ready:
            self._show_error("worker 尚未就绪，请稍后重试")
            return False
        if activate and not self.controller.activate_target_window():
            return False

        self._pending_start_mode = str(profile.get("mode", ""))
        self._pending_start_purpose = str(purpose)
        self._pending_start_request_id = ""
        self._set_start_pending(True, profile)
        try:
            request_id = self.controller.start_profile(profile)
        except Exception as exc:
            self._clear_pending_start()
            self._update_runtime_pages(self.worker_state, self.worker_payload)
            self._show_error(f"启动命令发送失败：{exc}")
            return False
        # Demo mode can confirm STARTING synchronously from start_profile().
        # Only retain the request id while this exact start is still pending.
        if self.start_pending and self._pending_start_purpose == purpose:
            self._pending_start_request_id = str(request_id or "")
        return True

    def _begin_action_handoff(self, profile: dict) -> None:
        monitor_profile = self.monitor_profile
        if monitor_profile is None:
            self._show_error("实时识别状态已变化，请重试")
            return
        action_profile = dict(profile)
        action_profile["overlay"] = bool(monitor_profile.get("overlay", True))
        self.pending_action_profile = action_profile
        self.handoff_phase = "stopping_monitor"
        self._handoff_stop_request_id = ""
        self._update_runtime_pages(self.worker_state, self.worker_payload)
        try:
            request_id = self.controller.send("stop")
        except Exception as exc:
            self.pending_action_profile = None
            self.handoff_phase = "none"
            self._update_runtime_pages(self.worker_state, self.worker_payload)
            self._show_error(f"切换实时识别失败：{exc}")
            return
        if self.handoff_phase == "stopping_monitor":
            self._handoff_stop_request_id = str(request_id or "")

    def _start_handoff_action(self) -> None:
        profile = self.pending_action_profile
        self.pending_action_profile = None
        if profile is None:
            self.handoff_phase = "none"
            self._update_runtime_pages(self.worker_state, self.worker_payload)
            return
        self.handoff_phase = "starting_action"
        self._update_runtime_pages(self.worker_state, self.worker_payload)
        if self._launch_profile(profile, purpose="handoff_action", activate=True):
            return
        self._restore_monitor_after_failed_action_start()

    def _restore_monitor_after_failed_action_start(self) -> None:
        if self.monitor_profile is None or not self.controller.ready:
            self.handoff_phase = "none"
            self._update_runtime_pages(self.worker_state, self.worker_payload)
            return
        self._start_monitor_restore()

    def _start_monitor_restore(self) -> None:
        profile = None if self.monitor_profile is None else dict(self.monitor_profile)
        if profile is None or not self.controller.ready:
            self.monitor_profile = None
            self.handoff_phase = "none"
            self._update_runtime_pages(self.worker_state, self.worker_payload)
            return
        self.handoff_phase = "restoring_monitor"
        self._update_runtime_pages(self.worker_state, self.worker_payload)
        # Realtime capture itself is non-input and uses activate=False. The
        # action already owned focus; restoration must not steal it again.
        if self._launch_profile(profile, purpose="restore_monitor", activate=False):
            return
        self.monitor_profile = None
        self.handoff_phase = "none"
        self._update_runtime_pages(self.worker_state, self.worker_payload)

    def stop_realtime(self) -> None:
        current_profile = self.monitor_profile
        if current_profile is None:
            return
        if not self.controller.ready:
            self._show_error("worker 尚未就绪，无法停止实时识别")
            return
        self.monitor_profile = None
        if self.pending_action_profile is not None:
            self.pending_action_profile["overlay"] = False
        if self.handoff_phase == "action_active" and self.worker_state in {"running", "paused"}:
            self.controller.send("set_overlay", {"visible": False})
        elif self.active_mode == "realtime" and self.worker_state in {"starting", "running", "paused"}:
            try:
                request_id = self.controller.send("stop")
            except Exception as exc:
                self.monitor_profile = current_profile
                self._update_runtime_pages(self.worker_state, self.worker_payload)
                self._show_error(f"停止实时识别命令发送失败：{exc}")
                return
            self._monitor_stop_request_id = str(request_id or "")
            self._monitor_stop_profile = dict(current_profile)
        self._update_runtime_pages(self.worker_state, self.worker_payload)

    def set_monitor_overlay_visibility(self, visible: bool) -> None:
        if self.monitor_profile is None:
            return
        requested = bool(visible)
        self.monitor_profile["overlay"] = requested
        if self.pending_action_profile is not None:
            self.pending_action_profile["overlay"] = requested
        if self.active_mode in {"realtime", "automatic", "independent"} and self.worker_state in {"running", "paused"}:
            self.controller.send("set_overlay", {"visible": requested})

    def stop_action(self) -> None:
        if self.active_mode not in {"automatic", "independent"}:
            return
        self.controller.send("stop")

    def emergency_stop(self) -> None:
        self._clear_handoff(clear_monitor=True)
        self.controller.send("emergency_stop")
        self._update_runtime_pages(self.worker_state, self.worker_payload)

    def start_independent(self, task_id: str, dry_run: bool) -> None:
        self.controller_start(
            {"mode": "independent", "task_id": task_id, "dry_run": dry_run, "overlay": False, "continuous": False}
        )

    def start_input_recording(self, options: dict) -> None:
        if self._automation_busy():
            self._show_tool_error("自动任务正在运行，不能同时录制键鼠操作")
            return
        self.input_tools.start_recording(options)

    def _launch_recording_after_countdown(self, _options: dict) -> None:
        focused = self.demo or self.controller.activate_target_window()
        self.input_tools.confirm_recording_focus(focused)

    def start_input_replay(self, recording: str) -> None:
        if self._automation_busy():
            self._show_tool_error("自动任务正在运行，不能同时回放键鼠操作")
            return
        if not self.demo and not self.controller.activate_target_window():
            return
        self.input_tools.start_replay(Path(recording))

    def add_custom_task(self, display_name: str, recording: str) -> None:
        if self._automation_busy() or self.input_tools.busy:
            self._show_tool_error("任务运行、录制或回放期间不能修改任务注册表")
            return
        try:
            task = self.user_task_store.add_replay_task(display_name, recording)
        except (OSError, ValueError) as exc:
            self._show_tool_error(str(exc))
            return
        self.pages["logs"].append_log(
            "INFO",
            f"已添加自定义任务：{task['display_name']}，路线：{task['recording_replay']['path']}",
        )
        self.controller.reload_tasks()

    def _automation_busy(self) -> bool:
        return self.start_pending or self.handoff_phase != "none" or self.worker_state != "idle"

    def _stable_realtime_running(self) -> bool:
        return (
            self.monitor_profile is not None
            and self.handoff_phase == "none"
            and not self.start_pending
            and self.active_mode == "realtime"
            and self.worker_state in {"running", "paused"}
        )

    def _realtime_launchable(self) -> bool:
        return (
            self.monitor_profile is None
            and self.handoff_phase == "none"
            and not self.start_pending
            and self.worker_state == "idle"
        )

    def _action_launchable(self) -> bool:
        if self.handoff_phase != "none" or self.start_pending:
            return False
        if self.worker_state == "idle":
            return True
        return self._stable_realtime_running()

    def _monitor_transitioning(self) -> bool:
        if self._monitor_stop_request_id:
            return True
        if self.handoff_phase in {"stopping_monitor", "starting_action", "restoring_monitor"}:
            return True
        if self.start_pending and self._pending_start_mode == "realtime":
            return True
        if self.active_mode == "realtime" and self.worker_state in {"starting", "stopping", "completed", "error"}:
            return True
        if self.handoff_phase == "action_active" and self.worker_state not in {"running", "paused"}:
            return True
        return False

    def _clear_pending_start(self) -> str:
        purpose = self._pending_start_purpose
        self.start_pending = False
        self._pending_start_mode = ""
        self._pending_start_purpose = ""
        self._pending_start_request_id = ""
        return purpose

    def _clear_confirmed_start(self) -> str:
        purpose = self._confirmed_start_purpose
        self._confirmed_start_mode = ""
        self._confirmed_start_purpose = ""
        self._confirmed_start_request_id = ""
        return purpose

    def _clear_handoff(self, *, clear_monitor: bool) -> None:
        self.pending_action_profile = None
        self.handoff_phase = "none"
        self._handoff_stop_request_id = ""
        self._monitor_stop_request_id = ""
        self._monitor_stop_profile = None
        self._clear_pending_start()
        self._clear_confirmed_start()
        if clear_monitor:
            self.monitor_profile = None

    def _set_start_pending(self, pending: bool, profile: dict | None = None) -> None:
        self.start_pending = bool(pending)
        if self.start_pending:
            pending_payload = dict(profile or {})
            pending_payload["_start_pending"] = True
            self._update_runtime_pages("starting", pending_payload)
            return
        self._update_runtime_pages(self.worker_state, self.worker_payload)

    def _update_runtime_pages(self, state: str, payload: dict) -> None:
        self.pages["home"].update_state(state, payload)
        self.pages["realtime"].update_state(state, payload)
        self.pages["independent"].update_state(state, payload)
        self.pages["automatic"].update_state(state, payload)
        self.pages["routes"].set_automation_busy(self._automation_busy())
        requested = self.monitor_profile is not None
        transitioning = self._monitor_transitioning()
        self.pages["home"].set_monitor_service_state(requested, transitioning)
        self.pages["realtime"].set_monitor_service_state(requested, transitioning)
        self._sync_launch_availability()

    def _sync_launch_availability(self) -> None:
        common_ready = self.controller.ready and self.preflight_ready and not self.input_tools.busy
        realtime_failures = [
            item
            for item in self.blocking_preflight_failures
            if str(item.get("id", "")) != "admin"
        ]
        realtime_enabled = common_ready and not realtime_failures and self._realtime_launchable()
        action_enabled = common_ready and not self.blocking_preflight_failures and self._action_launchable()
        self.pages["home"].set_launch_enabled(realtime=realtime_enabled, actions=action_enabled)
        self.pages["realtime"].set_launch_enabled(realtime_enabled)
        self.pages["independent"].set_launch_enabled(action_enabled)
        self.pages["automatic"].set_launch_enabled(action_enabled)

    def _worker_ready(self) -> None:
        self.preflight_ready = False
        self.worker_state = "idle"
        self.worker_payload = {}
        self.active_mode = ""
        self._clear_handoff(clear_monitor=True)
        self._update_runtime_pages("starting", {})
        self.worker_pill.setText("已连接")
        self.worker_pill.set_tone("success")
        self.pages["logs"].append_log("INFO", "worker 已连接，正在加载任务与运行前检查。")

    def _set_tasks(self, tasks: list) -> None:
        self.pages["independent"].set_tasks(tasks)
        self.pages["automatic"].set_tasks(tasks)
        self.pages["routes"].set_tasks(tasks)
        self._sync_launch_availability()

    def _preflight_received(self, checks: list) -> None:
        self.preflight_ready = True
        self.blocking_preflight_failures = [item for item in checks if item.get("blocking") and not item.get("ok")]
        self.pages["home"].update_preflight(checks)
        for item in checks:
            if item.get("ok") or item.get("blocking"):
                continue
            label = str(item.get("label", item.get("id", "运行前检查")))
            detail = str(item.get("detail", "需要人工检查"))
            self.pages["logs"].append_log("WARNING", f"{label}：{detail}")
        if not self.start_pending:
            self._update_runtime_pages(self.worker_state, self.worker_payload)

    def _state_changed(self, state: str, payload: dict) -> None:
        previous_mode = self.active_mode
        self.worker_state = state
        self.worker_payload = dict(payload)
        mode = str(payload.get("mode", ""))
        if mode:
            self.active_mode = mode

        if state == "starting" and self.start_pending:
            if not self._pending_start_mode or mode == self._pending_start_mode:
                pending_request_id = self._pending_start_request_id
                purpose = self._clear_pending_start()
                self._confirmed_start_mode = mode
                self._confirmed_start_purpose = purpose
                self._confirmed_start_request_id = str(payload.get("request_id", "") or pending_request_id)
                if purpose in {"handoff_action", "implicit_monitor_action"}:
                    self.handoff_phase = "action_active"
                elif purpose == "restore_monitor":
                    self.handoff_phase = "none"
        elif state == "running" and self._confirmed_start_purpose:
            if not self._confirmed_start_mode or not mode or mode == self._confirmed_start_mode:
                self._clear_confirmed_start()

        transition = ""
        if state == "error" and (bool(payload.get("worker_unavailable")) or not self.controller.ready):
            self.active_mode = ""
            self._clear_handoff(clear_monitor=True)
        elif state == "idle":
            self._monitor_stop_request_id = ""
            self._monitor_stop_profile = None
            self._clear_confirmed_start()
            if self.handoff_phase == "stopping_monitor":
                self._handoff_stop_request_id = ""
                self.active_mode = ""
                transition = "start_action"
            elif self.handoff_phase == "action_active":
                self.active_mode = ""
                if self.monitor_profile is not None:
                    transition = "restore_monitor"
                else:
                    self.handoff_phase = "none"
            elif self.handoff_phase in {"starting_action", "restoring_monitor"} and self.start_pending:
                # An old/duplicate idle acknowledgement must not consume a
                # newly submitted start before its STARTING confirmation.
                pass
            else:
                self._clear_pending_start()
                self.active_mode = ""
                if previous_mode == "realtime":
                    # A standalone monitor that ended unexpectedly is not
                    # auto-restarted; the user can inspect the error and retry.
                    self.monitor_profile = None
                if self.handoff_phase in {"starting_action", "restoring_monitor"}:
                    self.handoff_phase = "none"

        self._update_runtime_pages(state, payload)
        if state == "error":
            self.worker_pill.setText("异常")
            self.worker_pill.set_tone("danger")
            worker_unavailable = bool(payload.get("worker_unavailable")) or not self.controller.ready
            self.restart_worker_button.setVisible(worker_unavailable and not self.demo)
            self.restart_worker_button.setEnabled(worker_unavailable and not self.demo)
        elif state == "idle":
            self.worker_pill.setText("已连接")
            self.worker_pill.set_tone("success")
            self.restart_worker_button.setVisible(False)

        if transition == "start_action":
            self._start_handoff_action()
        elif transition == "restore_monitor":
            self._start_monitor_restore()

    def _restart_worker(self) -> None:
        if self.demo:
            return
        self.preflight_ready = False
        self.blocking_preflight_failures = []
        self._clear_handoff(clear_monitor=True)
        self.worker_state = "starting"
        self.worker_payload = {}
        self.active_mode = ""
        self.restart_worker_button.setEnabled(False)
        self.restart_worker_button.setVisible(False)
        self.worker_pill.setText("连接中")
        self.worker_pill.set_tone("warning")
        self._update_runtime_pages("starting", {})
        self.pages["logs"].append_log("INFO", "正在重新启动 worker，并重新执行任务加载与运行前检查。")
        if not self.controller.start_worker():
            self.worker_state = "error"
            self.worker_payload = {"worker_unavailable": True}
            self.restart_worker_button.setVisible(True)
            self.restart_worker_button.setEnabled(True)
            self.worker_pill.setText("异常")
            self.worker_pill.set_tone("danger")
            self._update_runtime_pages("error", self.worker_payload)
            self.pages["logs"].append_log("ERROR", "worker 尚未完全退出，暂时无法重连；请稍后重试。")

    def _handle_event(self, event: dict) -> None:
        payload = event.get("payload", {})
        event_type = event.get("type")
        if event_type == "realtime_detection":
            self.pages["realtime"].add_detection(
                str(payload.get("marker", "unknown")),
                float(payload.get("confidence", 0.0)),
            )
        elif event_type == "overlay_visibility":
            visible = bool(payload.get("visible", False))
            if self.monitor_profile is not None:
                self.monitor_profile["overlay"] = visible
            if self.pending_action_profile is not None:
                self.pending_action_profile["overlay"] = visible
            if self._monitor_stop_profile is not None:
                self._monitor_stop_profile["overlay"] = visible
            self.pages["realtime"].set_overlay_visible(visible)
        elif event_type in {"task_selected", "route_started", "route_completed", "task_result"}:
            self.pages["automatic"].update_activity(str(event_type), payload)

    def _show_error(self, message: str) -> None:
        self.pages["logs"].append_log("ERROR", message)
        self.worker_pill.setText("需检查")
        self.worker_pill.set_tone("danger")

    def _controller_error(self, message: str) -> None:
        if self.start_pending:
            purpose = self._clear_pending_start()
            self._handle_start_failure(purpose)
        self._show_error(message)

    def _command_failed(self, request_id: str, message: str) -> None:
        if request_id and request_id == self._monitor_stop_request_id:
            self._monitor_stop_request_id = ""
            self.monitor_profile = self._monitor_stop_profile
            self._monitor_stop_profile = None
            self._update_runtime_pages(self.worker_state, self.worker_payload)
        elif request_id and request_id == self._handoff_stop_request_id:
            self._handoff_stop_request_id = ""
            self.pending_action_profile = None
            self.handoff_phase = "none"
            self._update_runtime_pages(self.worker_state, self.worker_payload)
        elif request_id and request_id == self._pending_start_request_id:
            purpose = self._clear_pending_start()
            self._handle_start_failure(purpose)
        elif request_id and request_id == self._confirmed_start_request_id:
            purpose = self._clear_confirmed_start()
            # STARTING is emitted immediately before ActionExecutor.submit().
            # A command_error for that same request therefore means submit was
            # rolled back and the worker is known to be idle, even if its idle
            # line has not reached the UI yet.
            self.worker_state = "idle"
            self.worker_payload = {"request_id": request_id, "command_failed": True}
            self.active_mode = ""
            if purpose in {"handoff_action", "implicit_monitor_action"}:
                self._restore_monitor_after_failed_action_start()
            else:
                self._handle_start_failure(purpose)
        self._show_error(message)

    def _handle_start_failure(self, purpose: str) -> None:
        if purpose == "direct_monitor":
            self.monitor_profile = None
            self.handoff_phase = "none"
        elif purpose == "handoff_action":
            self._restore_monitor_after_failed_action_start()
            return
        elif purpose == "implicit_monitor_action":
            self.monitor_profile = None
            self.handoff_phase = "none"
        elif purpose == "restore_monitor":
            self.monitor_profile = None
            self.handoff_phase = "none"
        self._update_runtime_pages(self.worker_state, self.worker_payload)

    def _show_tool_error(self, message: str) -> None:
        self.pages["logs"].append_log("ERROR", message)
        self.pages["routes"].set_tool_state("error", {"detail": message})

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self._clear_handoff(clear_monitor=True)
        self.input_tools.close()
        self.controller.close()
        event.accept()
