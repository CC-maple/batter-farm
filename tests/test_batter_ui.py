from __future__ import annotations

import os
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit

from src.ui.main_window import MainWindow
from src.ui.pages.automatic import AutomaticPage
from src.ui.services.input_tool_controller import InputToolController
from src.ui.services.worker_controller import WorkerController
from src.ui.theme import apply_theme
from src.ui.widgets.common import StatusPill, action_label


ROOT = Path(__file__).resolve().parents[1]


class InputToolControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_unified_recording_and_replay_arguments(self) -> None:
        controller = InputToolController(ROOT, ROOT / "configs" / "config.yaml", demo=True)
        recording = ROOT / "assets" / "recordings" / "recording_example.json"

        recording_args = controller.recording_arguments(
            {"mode": "hidden", "sample_rate_hz": 50, "step_dx": 6}
        )
        replay_args = controller.replay_arguments(recording)

        self.assertEqual(Path(recording_args[2]).name, "record_input_actions.py")
        self.assertEqual(
            recording_args[recording_args.index("--camera-mode") + 1],
            "hidden",
        )
        self.assertEqual(recording_args[recording_args.index("--interval-ms") + 1], "20")
        self.assertEqual(Path(replay_args[2]).name, "replay_recording.py")
        self.assertEqual(replay_args[3], str(recording))

    def test_empty_recording_is_rejected_before_replay_process_start(self) -> None:
        controller = InputToolController(ROOT, ROOT / "configs" / "config.yaml", demo=True)
        errors: list[str] = []
        controller.process_error.connect(errors.append)
        with tempfile.TemporaryDirectory() as temp_dir:
            recording = Path(temp_dir) / "recording_empty.json"
            recording.write_text(
                json.dumps({"recording_mode": "input_only", "events": []}),
                encoding="utf-8",
            )

            self.assertFalse(controller.start_replay(recording))

        self.assertTrue(errors)
        self.assertIn("没有可回放", errors[-1])


class BatterUiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self) -> None:
        self.window = MainWindow(ROOT, ROOT / "configs" / "config.yaml", demo=True)
        self.window.show()
        QTest.qWait(120)

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(20)

    def _set_stable_monitor(self, profile: dict | None = None) -> dict:
        monitor_profile = dict(
            profile
            or {
                "mode": "realtime",
                "continuous": True,
                "overlay": True,
                "detector_confidence": 0.73,
            }
        )
        self.window.monitor_profile = dict(monitor_profile)
        self.window.handoff_phase = "none"
        self.window.start_pending = False
        self.window._state_changed("running", {"mode": "realtime"})
        return monitor_profile

    def test_all_product_pages_are_registered(self) -> None:
        self.assertEqual(
            set(self.window.pages),
            {"home", "realtime", "independent", "automatic", "routes", "detector", "logs", "settings"},
        )
        self.assertEqual(self.window.stack.count(), 8)

    def test_app_import_does_not_preload_the_input_stack(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-s",
                "-c",
                (
                    "import sys; import src.ui.app; "
                    "print(int('pyautogui' in sys.modules), "
                    "int('src.runtime.application' in sys.modules))"
                ),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), "0 0")

    def test_demo_worker_populates_tasks_and_preflight(self) -> None:
        automatic: AutomaticPage = self.window.pages["automatic"]
        self.assertGreater(automatic.priority_list.count(), 0)
        self.assertIn("已获得", self.window.pages["home"].metrics["admin"].value.text())
        self.assertTrue(self.window.controller.ready)

    def test_navigation_changes_current_page(self) -> None:
        self.window.show_page("logs")
        self.assertIs(self.window.stack.currentWidget(), self.window.pages["logs"])
        self.assertTrue(self.window.nav_buttons["logs"].isChecked())

    def test_status_pill_uses_qt_compatible_hex_colors(self) -> None:
        pill = StatusPill("状态")
        for tone in ("success", "warning", "danger", "neutral", "accent"):
            pill.set_tone(tone)
            self.assertEqual(pill.property("tone"), tone)
            self.assertEqual(pill.styleSheet(), "")
        self.assertIsNone(re.search(r"#[0-9A-Fa-f]{8}(?![0-9A-Fa-f])", pill.styleSheet()))

    def test_recognition_frequency_displays_fps_unit(self) -> None:
        settings = self.window.pages["settings"]
        self.assertEqual(settings.recognition_fps.suffix(), " FPS")
        self.assertEqual(settings.recognition_fps.text(), "8 FPS")

    def test_routes_page_uses_unified_keyboard_mouse_tools(self) -> None:
        routes = self.window.pages["routes"]
        recording_args = self.window.input_tools.recording_arguments(routes.recording_options())
        replay_path = ROOT / "assets" / "recordings" / "mouse_input_visible_20260710_120713.json"
        replay_args = self.window.input_tools.replay_arguments(replay_path)

        self.assertEqual(Path(recording_args[2]).name, "record_input_actions.py")
        camera_mode_index = recording_args.index("--camera-mode")
        self.assertEqual(recording_args[camera_mode_index + 1], "visible")
        interval_index = recording_args.index("--interval-ms")
        self.assertEqual(recording_args[interval_index + 1], "10")
        countdown_index = recording_args.index("--countdown")
        self.assertEqual(recording_args[countdown_index + 1], "0.0")
        self.assertEqual(Path(replay_args[2]).name, "replay_recording.py")
        self.assertIn("--overlay", recording_args)
        self.assertIn("WASD", routes.tool_description.text())
        self.assertIn("F5", routes.tool_description.text())
        self.assertIn("F6", routes.tool_description.text())
        self.assertIn("解除游戏内 F5/F6 绑定", routes.tool_description.text())
        self.assertNotIn("F1–F24", routes.tool_description.text())
        self.assertFalse(routes.replay_button.isEnabled())

        emitted: list[str] = []
        routes.replay_requested.connect(emitted.append)
        routes.set_selected_recording(replay_path)
        self.assertTrue(routes.replay_button.isEnabled())
        routes.replay_button.click()
        self.assertEqual(emitted, [str(replay_path.resolve())])

    def test_routes_camera_advanced_options_are_collapsed_and_convert_hz(self) -> None:
        routes = self.window.pages["routes"]
        self.assertTrue(routes.advanced_panel.isHidden())
        routes.advanced_button.click()
        self.assertFalse(routes.advanced_panel.isHidden())
        routes.sample_rate.setValue(50)
        routes.camera_mode.setCurrentIndex(1)
        routes.invert_direction.setChecked(True)

        options = routes.recording_options()
        arguments = self.window.input_tools.recording_arguments(options)
        interval_index = arguments.index("--interval-ms")
        camera_mode_index = arguments.index("--camera-mode")
        self.assertEqual(options["sample_rate_hz"], 50)
        self.assertEqual(arguments[interval_index + 1], "20")
        self.assertEqual(arguments[camera_mode_index + 1], "hidden")
        self.assertIn("--invert", arguments)

    def test_routes_reject_empty_recording_before_starting_replay(self) -> None:
        errors: list[str] = []
        self.window.input_tools.process_error.connect(errors.append)
        with tempfile.TemporaryDirectory() as temp_dir:
            recording = Path(temp_dir) / "recording_empty.json"
            recording.write_text(
                json.dumps({"recording_mode": "input_only", "events": []}),
                encoding="utf-8",
            )

            self.assertFalse(self.window.input_tools.start_replay(recording))

        self.assertTrue(errors)
        self.assertIn("没有可回放", errors[-1])

    def test_demo_recording_tool_can_start_and_stop_cooperatively(self) -> None:
        routes = self.window.pages["routes"]
        routes.countdown.setValue(0.0)
        routes.record_button.click()
        QTest.qWait(120)
        self.assertTrue(self.window.input_tools.busy)
        self.assertEqual(routes.tool_status.text(), "录制中")
        self.assertTrue(routes.stop_button.isEnabled())

        routes.stop_button.click()
        QTest.qWait(180)
        self.assertFalse(self.window.input_tools.busy)
        self.assertEqual(routes.tool_status.text(), "已停止")

    def test_recording_countdown_is_visible_and_can_be_cancelled(self) -> None:
        routes = self.window.pages["routes"]
        routes.countdown.setValue(2.0)
        routes.record_button.click()
        QTest.qWait(40)

        self.assertTrue(self.window.input_tools.busy)
        self.assertEqual(routes.tool_status.text(), "2 秒")
        self.assertIn("游戏窗口", routes.tool_detail.text())

        routes.stop_button.click()
        QTest.qWait(20)
        self.assertFalse(self.window.input_tools.busy)
        self.assertEqual(routes.tool_status.text(), "已停止")

    def test_routes_tools_are_disabled_while_automation_is_busy(self) -> None:
        routes = self.window.pages["routes"]
        routes.set_selected_recording(ROOT / "assets" / "recordings" / "sample.json")
        routes.set_automation_busy(True)
        self.assertFalse(routes.record_button.isEnabled())
        self.assertFalse(routes.replay_button.isEnabled())
        self.assertEqual(routes.tool_status.text(), "自动任务运行中")

    def test_home_has_no_horizontal_overflow_at_minimum_size(self) -> None:
        self.window.resize(1080, 700)
        self.window.show_page("home")
        QTest.qWait(60)
        home = self.window.pages["home"]
        self.assertEqual(home.scroll.horizontalScrollBar().maximum(), 0)
        widths = [card.width() for card in home.metrics.values()]
        self.assertLessEqual(max(widths) - min(widths), 2)

    def test_long_preflight_details_use_compact_metric_text(self) -> None:
        home = self.window.pages["home"]
        model_path = str(ROOT / "models" / "farm-icons" / "best.onnx")
        home.update_preflight(
            [
                {"id": "window", "ok": True, "blocking": True, "detail": "元梦之星 (HWND 12345)"},
                {"id": "model", "ok": True, "blocking": True, "detail": model_path},
            ]
        )
        self.assertEqual(home.metrics["window"].value.full_text(), "元梦之星")
        self.assertEqual(home.metrics["model"].value.full_text(), "best.onnx")
        self.assertEqual(home.metrics["model"].value.toolTip(), model_path)

    def test_product_action_labels_replace_internal_codes(self) -> None:
        independent = self.window.pages["independent"]
        badge_texts = {pill.text() for pill in independent.findChildren(StatusPill)}
        self.assertIn("路线回放", badge_texts)
        self.assertEqual(action_label("restaurant_service"), "餐厅流程")
        routes = self.window.pages["routes"]
        self.assertEqual(routes.table.item(0, 2).text(), "触发点击")
        self.assertEqual(routes.table.item(1, 2).text(), "路线回放")

    def test_log_view_wraps_long_lines(self) -> None:
        logs = self.window.pages["logs"]
        self.assertEqual(logs.editor.lineWrapMode(), QPlainTextEdit.LineWrapMode.WidgetWidth)

    def test_automatic_click_focuses_game_before_worker_start(self) -> None:
        calls: list[str] = []
        self.window.controller.activate_target_window = lambda: calls.append("focus") or True
        self.window.controller.start_profile = lambda _profile: calls.append("start") or "request-id"

        self.window.controller_start({"mode": "automatic"})

        self.assertEqual(calls, ["focus", "start"])

    def test_direct_automatic_enables_overlay_and_restores_implicit_monitor_once(self) -> None:
        focus_calls: list[str] = []
        started_profiles: list[dict] = []
        overlay_requests: list[bool] = []
        realtime = self.window.pages["realtime"]
        realtime.overlay_visibility_requested.connect(overlay_requests.append)
        realtime.show_boxes.setChecked(False)
        self.window.controller.activate_target_window = lambda: focus_calls.append("focus") or True

        def start_profile(profile: dict) -> str:
            started_profiles.append(dict(profile))
            return "automatic-request" if len(started_profiles) == 1 else "monitor-request"

        self.window.controller.start_profile = start_profile

        self.window.controller_start({"mode": "automatic", "continuous": False})

        implicit_monitor = {
            "mode": "realtime",
            "continuous": True,
            "overlay": True,
        }
        self.assertEqual(focus_calls, ["focus"])
        self.assertEqual(len(started_profiles), 1)
        self.assertTrue(started_profiles[0]["overlay"])
        self.assertTrue(realtime.show_boxes.isChecked())
        self.assertEqual(overlay_requests, [])
        self.assertEqual(self.window.monitor_profile, implicit_monitor)
        self.assertEqual(self.window.handoff_phase, "starting_action")

        self.window._state_changed(
            "starting",
            {"mode": "automatic", "request_id": "automatic-request"},
        )
        self.window._state_changed("running", {"mode": "automatic"})
        self.window._state_changed("completed", {"mode": "automatic"})
        self.window._state_changed("idle", {})

        self.assertEqual(focus_calls, ["focus"])
        self.assertEqual(len(started_profiles), 2)
        self.assertEqual(started_profiles[0]["mode"], "automatic")
        self.assertEqual(started_profiles[1], implicit_monitor)
        self.assertEqual(self.window.handoff_phase, "restoring_monitor")

        self.window._state_changed("idle", {})

        self.assertEqual(len(started_profiles), 2)

    def test_late_command_error_after_starting_restores_implicit_monitor_without_sticking(self) -> None:
        started_profiles: list[dict] = []
        self.window.controller.activate_target_window = lambda: True

        def start_profile(profile: dict) -> str:
            started_profiles.append(dict(profile))
            return "implicit-action-request" if len(started_profiles) == 1 else "monitor-restore-request"

        self.window.controller.start_profile = start_profile

        self.window.controller_start({"mode": "automatic", "continuous": False})
        self.window._state_changed(
            "starting",
            {"mode": "automatic", "request_id": "implicit-action-request"},
        )

        self.assertFalse(self.window.start_pending)
        self.assertEqual(self.window._confirmed_start_request_id, "implicit-action-request")
        self.assertIsNotNone(self.window.monitor_profile)
        self.assertEqual(self.window.handoff_phase, "action_active")

        self.window._command_failed("implicit-action-request", "late start rejection")

        implicit_monitor = {"mode": "realtime", "continuous": True, "overlay": True}
        self.assertEqual(self.window.monitor_profile, implicit_monitor)
        self.assertEqual(len(started_profiles), 2)
        self.assertEqual(started_profiles[0]["mode"], "automatic")
        self.assertEqual(started_profiles[1], implicit_monitor)
        self.assertEqual(self.window.handoff_phase, "restoring_monitor")
        self.assertEqual(self.window._confirmed_start_request_id, "")
        self.assertTrue(self.window.start_pending)
        self.assertEqual(self.window.worker_state, "idle")

        self.window._state_changed(
            "starting",
            {"mode": "realtime", "request_id": "monitor-restore-request"},
        )
        self.window._state_changed("running", {"mode": "realtime"})

        self.assertFalse(self.window.start_pending)
        self.assertEqual(self.window.handoff_phase, "none")
        self.assertTrue(self.window.pages["home"].automatic_card.button.isEnabled())
        self.assertTrue(self.window.pages["automatic"].start_button.isEnabled())

    def test_realtime_handoff_waits_for_first_idle_and_starts_action_once(self) -> None:
        commands: list[tuple[str, dict | None]] = []
        focus_calls: list[str] = []
        started_profiles: list[dict] = []

        def send(command: str, payload: dict | None = None) -> str:
            commands.append((command, payload))
            return f"{command}-request"

        self.window.controller.send = send
        self.window.controller.activate_target_window = lambda: focus_calls.append("focus") or True
        self.window.controller.start_profile = (
            lambda profile: started_profiles.append(dict(profile)) or "action-start-request"
        )
        self._set_stable_monitor()

        home = self.window.pages["home"]
        automatic = self.window.pages["automatic"]
        independent = self.window.pages["independent"]
        self.assertTrue(home.automatic_card.button.isEnabled())
        self.assertTrue(automatic.start_button.isEnabled())
        self.assertTrue(all(button.isEnabled() for button in independent._buttons))

        home.automatic_card.button.click()

        self.assertEqual(commands, [("stop", None)])
        self.assertEqual(focus_calls, [])
        self.assertEqual(started_profiles, [])
        self.assertEqual(self.window.handoff_phase, "stopping_monitor")
        self.assertTrue(self.window.pending_action_profile["overlay"])

        self.window._state_changed("stopping", {"mode": "realtime"})
        self.window._state_changed("completed", {"mode": "realtime"})

        self.assertEqual(focus_calls, [])
        self.assertEqual(started_profiles, [])

        self.window._state_changed("idle", {})

        self.assertEqual(focus_calls, ["focus"])
        self.assertEqual(len(started_profiles), 1)
        self.assertEqual(started_profiles[0]["mode"], "automatic")
        self.assertTrue(started_profiles[0]["overlay"])
        self.assertEqual(self.window.handoff_phase, "starting_action")

        self.window._state_changed("idle", {})

        self.assertEqual(focus_calls, ["focus"])
        self.assertEqual(len(started_profiles), 1)

    def test_stopping_monitor_overlay_event_updates_pending_action_profile(self) -> None:
        started_profiles: list[dict] = []
        self.window.controller.send = lambda _command, _payload=None: "handoff-stop-request"
        self.window.controller.activate_target_window = lambda: True
        self.window.controller.start_profile = (
            lambda profile: started_profiles.append(dict(profile)) or "action-start-request"
        )
        self._set_stable_monitor()

        self.window.controller_start({"mode": "automatic", "continuous": False})
        self.window._handle_event(
            {"type": "overlay_visibility", "payload": {"visible": False}}
        )

        self.assertFalse(self.window.monitor_profile["overlay"])
        self.assertFalse(self.window.pending_action_profile["overlay"])
        self.assertFalse(self.window.pages["realtime"].show_boxes.isChecked())

        self.window._state_changed("idle", {})

        self.assertEqual(len(started_profiles), 1)
        self.assertFalse(started_profiles[0]["overlay"])

    def test_overlay_checkbox_is_disabled_during_handoff_transitions(self) -> None:
        started_profiles: list[dict] = []
        self.window.controller.send = lambda _command, _payload=None: "handoff-stop-request"
        self.window.controller.activate_target_window = lambda: True

        def start_profile(profile: dict) -> str:
            started_profiles.append(dict(profile))
            return f"start-request-{len(started_profiles)}"

        self.window.controller.start_profile = start_profile
        self._set_stable_monitor()
        realtime = self.window.pages["realtime"]
        self.assertTrue(realtime.show_boxes.isEnabled())

        self.window.controller_start({"mode": "automatic", "continuous": False})

        self.assertEqual(self.window.handoff_phase, "stopping_monitor")
        self.assertFalse(realtime.show_boxes.isEnabled())

        self.window._state_changed("idle", {})

        self.assertEqual(self.window.handoff_phase, "starting_action")
        self.assertFalse(realtime.show_boxes.isEnabled())

        self.window._state_changed(
            "starting",
            {"mode": "automatic", "request_id": "start-request-1"},
        )

        self.assertEqual(self.window.handoff_phase, "action_active")
        self.assertFalse(realtime.show_boxes.isEnabled())

        self.window._state_changed("running", {"mode": "automatic"})

        self.assertTrue(realtime.show_boxes.isEnabled())

        self.window._state_changed("completed", {"mode": "automatic"})

        self.assertFalse(realtime.show_boxes.isEnabled())

        self.window._state_changed("idle", {})

        self.assertEqual(self.window.handoff_phase, "restoring_monitor")
        self.assertFalse(realtime.show_boxes.isEnabled())

        self.window._state_changed(
            "starting",
            {"mode": "realtime", "request_id": "start-request-2"},
        )
        self.assertFalse(realtime.show_boxes.isEnabled())

        self.window._state_changed("running", {"mode": "realtime"})

        self.assertEqual(self.window.handoff_phase, "none")
        self.assertTrue(realtime.show_boxes.isEnabled())

    def test_action_completion_restores_original_monitor_profile_once(self) -> None:
        commands: list[str] = []
        focus_calls: list[str] = []
        started_profiles: list[dict] = []
        self.window.controller.send = lambda command, _payload=None: commands.append(command) or "stop-request"
        self.window.controller.activate_target_window = lambda: focus_calls.append("focus") or True

        def start_profile(profile: dict) -> str:
            started_profiles.append(dict(profile))
            return f"start-request-{len(started_profiles)}"

        self.window.controller.start_profile = start_profile
        monitor_profile = self._set_stable_monitor(
            {
                "mode": "realtime",
                "continuous": True,
                "overlay": True,
                "detector_confidence": 0.81,
            }
        )

        self.window.controller_start({"mode": "automatic", "continuous": False})
        self.window._state_changed("idle", {})
        self.window._state_changed("starting", {"mode": "automatic"})
        self.window._state_changed("running", {"mode": "automatic"})
        self.window._state_changed("completed", {"mode": "automatic"})
        self.window._state_changed("idle", {})

        self.assertEqual(commands, ["stop"])
        self.assertEqual(focus_calls, ["focus"])
        self.assertEqual(len(started_profiles), 2)
        self.assertEqual(started_profiles[0]["mode"], "automatic")
        self.assertTrue(started_profiles[0]["overlay"])
        self.assertEqual(started_profiles[1], monitor_profile)
        self.assertEqual(self.window.handoff_phase, "restoring_monitor")

        self.window._state_changed("idle", {})

        self.assertEqual(len(started_profiles), 2)

    def test_closing_monitor_during_action_hides_overlay_and_skips_restore(self) -> None:
        commands: list[tuple[str, dict | None]] = []
        started_profiles: list[dict] = []

        def send(command: str, payload: dict | None = None) -> str:
            commands.append((command, payload))
            return f"{command}-request"

        self.window.controller.send = send
        self.window.controller.activate_target_window = lambda: True
        self.window.controller.start_profile = (
            lambda profile: started_profiles.append(dict(profile)) or "action-start-request"
        )
        self._set_stable_monitor()

        self.window.controller_start({"mode": "automatic", "continuous": False})
        self.window._state_changed("idle", {})
        self.window._state_changed("starting", {"mode": "automatic"})
        self.window._state_changed("running", {"mode": "automatic"})

        home = self.window.pages["home"]
        self.assertEqual(home.overlay_start_button.text(), "关闭")
        home.overlay_start_button.click()

        self.assertEqual(
            commands,
            [
                ("stop", None),
                ("set_overlay", {"visible": False}),
            ],
        )
        self.assertIsNone(self.window.monitor_profile)
        self.window._state_changed("completed", {"mode": "automatic"})
        self.window._state_changed("idle", {})

        self.assertEqual(len(started_profiles), 1)
        self.assertEqual(self.window.handoff_phase, "none")

    def test_handoff_stop_command_failure_keeps_stable_monitor_available(self) -> None:
        focus_calls: list[str] = []
        started_profiles: list[dict] = []
        self.window.controller.send = lambda _command, _payload=None: "handoff-stop-request"
        self.window.controller.activate_target_window = lambda: focus_calls.append("focus") or True
        self.window.controller.start_profile = (
            lambda profile: started_profiles.append(dict(profile)) or "action-start-request"
        )
        monitor_profile = self._set_stable_monitor()

        self.window.controller_start({"mode": "automatic", "continuous": False})
        self.window._command_failed("handoff-stop-request", "stop rejected")

        self.assertEqual(focus_calls, [])
        self.assertEqual(started_profiles, [])
        self.assertEqual(self.window.monitor_profile, monitor_profile)
        self.assertIsNone(self.window.pending_action_profile)
        self.assertEqual(self.window.handoff_phase, "none")
        self.assertTrue(self.window.pages["home"].automatic_card.button.isEnabled())
        self.assertTrue(self.window.pages["automatic"].start_button.isEnabled())

    def test_handoff_action_start_failure_restores_monitor_without_refocus(self) -> None:
        focus_calls: list[str] = []
        started_profiles: list[dict] = []
        self.window.controller.send = lambda _command, _payload=None: "handoff-stop-request"
        self.window.controller.activate_target_window = lambda: focus_calls.append("focus") or True

        def start_profile(profile: dict) -> str:
            started_profiles.append(dict(profile))
            return "action-start-request" if len(started_profiles) == 1 else "monitor-restore-request"

        self.window.controller.start_profile = start_profile
        monitor_profile = self._set_stable_monitor(
            {
                "mode": "realtime",
                "continuous": True,
                "overlay": False,
                "detector_confidence": 0.66,
            }
        )

        self.window.controller_start({"mode": "automatic", "continuous": False})
        self.window._state_changed("idle", {})
        self.window._state_changed(
            "starting",
            {"mode": "automatic", "request_id": "action-start-request"},
        )
        self.window._command_failed("action-start-request", "start rejected")

        self.assertEqual(focus_calls, ["focus"])
        self.assertEqual(len(started_profiles), 2)
        self.assertEqual(started_profiles[1], monitor_profile)
        self.assertEqual(self.window.handoff_phase, "restoring_monitor")
        self.assertTrue(self.window.start_pending)

    def test_realtime_preflight_ignores_only_admin_failure(self) -> None:
        calls: list[str] = []
        self.window.controller.activate_target_window = lambda: calls.append("focus") or True
        self.window.controller.start_profile = lambda _profile: calls.append("start") or "request-id"
        self.window._preflight_received(
            [
                {"id": "admin", "label": "管理员权限", "ok": False, "blocking": True},
                {"id": "window", "label": "游戏窗口", "ok": True, "blocking": True},
                {"id": "model", "label": "检测模型", "ok": False, "blocking": True},
                {"id": "tasks", "label": "任务配置", "ok": True, "blocking": True},
            ]
        )

        self.assertFalse(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertFalse(self.window.pages["realtime"].start_button.isEnabled())
        self.assertFalse(self.window.pages["home"].automatic_card.button.isEnabled())
        self.assertFalse(self.window.pages["automatic"].start_button.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in self.window.pages["independent"]._buttons))

        self.window.controller_start({"mode": "realtime", "continuous": True, "overlay": True})

        self.assertEqual(calls, [])
        self.window._preflight_received(
            [
                {"id": "admin", "label": "管理员权限", "ok": False, "blocking": True},
                {"id": "window", "label": "游戏窗口", "ok": True, "blocking": True},
                {"id": "model", "label": "检测模型", "ok": True, "blocking": True},
                {"id": "tasks", "label": "任务配置", "ok": True, "blocking": True},
            ]
        )

        self.assertTrue(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertTrue(self.window.pages["realtime"].start_button.isEnabled())
        self.assertFalse(self.window.pages["home"].automatic_card.button.isEnabled())
        self.assertFalse(self.window.pages["automatic"].start_button.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in self.window.pages["independent"]._buttons))

        self.window.controller_start({"mode": "realtime", "continuous": True, "overlay": True})

        self.assertEqual(calls, ["focus", "start"])

    def test_blocking_preflight_disables_all_automation_launchers_and_recovers(self) -> None:
        failed_checks = [
            {"id": "admin", "label": "管理员权限", "ok": True, "blocking": True},
            {"id": "window", "label": "游戏窗口", "ok": False, "blocking": True},
            {"id": "model", "label": "检测模型", "ok": True, "blocking": True},
            {"id": "tasks", "label": "任务配置", "ok": True, "blocking": True},
        ]
        self.window._preflight_received(failed_checks)

        self.assertFalse(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertFalse(self.window.pages["home"].automatic_card.button.isEnabled())
        self.assertFalse(self.window.pages["realtime"].start_button.isEnabled())
        self.assertFalse(self.window.pages["automatic"].start_button.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in self.window.pages["independent"]._buttons))

        recovered = [dict(item, ok=True) for item in failed_checks]
        self.window._preflight_received(recovered)

        self.assertTrue(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertTrue(self.window.pages["home"].automatic_card.button.isEnabled())
        self.assertTrue(self.window.pages["realtime"].start_button.isEnabled())
        self.assertTrue(self.window.pages["automatic"].start_button.isEnabled())
        self.assertTrue(all(button.isEnabled() for button in self.window.pages["independent"]._buttons))

    def test_nonblocking_restaurant_rerecord_warning_keeps_other_launchers_available(self) -> None:
        self.window._preflight_received(
            [
                {"id": "admin", "label": "管理员权限", "ok": True, "blocking": True},
                {"id": "window", "label": "游戏窗口", "ok": True, "blocking": True},
                {"id": "model", "label": "检测模型", "ok": True, "blocking": True},
                {"id": "tasks", "label": "任务配置", "ok": True, "blocking": True},
                {
                    "id": "restaurant_assets",
                    "label": "餐厅资产",
                    "ok": False,
                    "blocking": False,
                    "detail": "餐厅路线需要重新录制，当前任务已临时禁用",
                },
            ]
        )

        self.assertTrue(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertTrue(self.window.pages["home"].automatic_card.button.isEnabled())
        self.assertIn("WARNING", self.window.pages["logs"].editor.toPlainText())
        self.assertIn("餐厅路线需要重新录制", self.window.pages["logs"].editor.toPlainText())

    def test_worker_exit_disables_launchers_until_reconnect_preflight_succeeds(self) -> None:
        starts: list[str] = []
        self.window.demo = False
        self.window.controller._ready = False
        self.window.controller.start_worker = lambda: starts.append("start") or True

        self.window._state_changed("error", {"worker_unavailable": True, "exit_code": 1})

        self.assertTrue(self.window.restart_worker_button.isVisible())
        self.assertFalse(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertFalse(self.window.pages["home"].automatic_card.button.isEnabled())
        self.assertFalse(self.window.pages["realtime"].start_button.isEnabled())
        self.assertFalse(self.window.pages["automatic"].start_button.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in self.window.pages["independent"]._buttons))

        self.window.restart_worker_button.click()

        self.assertEqual(starts, ["start"])
        self.assertFalse(self.window.restart_worker_button.isVisible())
        self.assertFalse(self.window.preflight_ready)
        self.window.controller._ready = True
        self.window._worker_ready()
        self.window._preflight_received(
            [
                {"id": "admin", "label": "管理员权限", "ok": True, "blocking": True},
                {"id": "window", "label": "游戏窗口", "ok": True, "blocking": True},
                {"id": "model", "label": "检测模型", "ok": True, "blocking": True},
                {"id": "tasks", "label": "任务配置", "ok": True, "blocking": True},
            ]
        )
        self.assertTrue(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertTrue(self.window.pages["automatic"].start_button.isEnabled())

    def test_start_waits_for_preflight_results(self) -> None:
        calls: list[str] = []
        self.window.preflight_ready = False
        self.window.controller.activate_target_window = lambda: calls.append("focus") or True
        self.window.controller.start_profile = lambda _profile: calls.append("start") or "request-id"

        self.window.controller_start({"mode": "realtime", "continuous": True, "overlay": True})

        self.assertEqual(calls, [])

    def test_worker_ready_keeps_launchers_disabled_until_preflight_arrives(self) -> None:
        self.window._worker_ready()

        self.assertFalse(self.window.preflight_ready)
        self.assertFalse(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertFalse(self.window.pages["home"].automatic_card.button.isEnabled())
        self.assertFalse(self.window.pages["realtime"].start_button.isEnabled())
        self.assertFalse(self.window.pages["automatic"].start_button.isEnabled())

        self.window._preflight_received(
            [
                {"id": "admin", "label": "管理员权限", "ok": True, "blocking": True},
                {"id": "window", "label": "游戏窗口", "ok": True, "blocking": True},
                {"id": "model", "label": "检测模型", "ok": True, "blocking": True},
                {"id": "tasks", "label": "任务配置", "ok": True, "blocking": True},
            ]
        )

        self.assertTrue(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertTrue(self.window.pages["home"].automatic_card.button.isEnabled())

    def test_task_list_before_preflight_keeps_independent_launchers_disabled(self) -> None:
        task = {
            "id": "refresh_route",
            "name": "刷新路线",
            "description": "测试任务列表到达顺序。",
            "action": "replay",
            "markers": [],
            "source": "refresh_route",
            "enabled": True,
            "independent_supported": True,
        }
        self.window._worker_ready()

        self.window._set_tasks([task])

        independent = self.window.pages["independent"]
        self.assertFalse(self.window.preflight_ready)
        self.assertEqual(len(independent._buttons), 1)
        self.assertFalse(independent._buttons[0].isEnabled())

        self.window._preflight_received(
            [
                {"id": "admin", "label": "管理员权限", "ok": True, "blocking": True},
                {"id": "window", "label": "游戏窗口", "ok": True, "blocking": True},
                {"id": "model", "label": "检测模型", "ok": True, "blocking": True},
                {"id": "tasks", "label": "任务配置", "ok": True, "blocking": True},
            ]
        )

        self.assertTrue(independent._buttons[0].isEnabled())

    def test_task_refresh_during_handoff_keeps_independent_launcher_disabled(self) -> None:
        task = {
            "id": "refresh_route",
            "name": "刷新路线",
            "description": "切换期间刷新任务列表。",
            "action": "replay",
            "markers": [],
            "source": "refresh_route",
            "enabled": True,
            "independent_supported": True,
        }
        started_profiles: list[dict] = []
        self.window.controller.send = lambda _command, _payload=None: "handoff-stop-request"
        self.window.controller.activate_target_window = lambda: True
        self.window.controller.start_profile = (
            lambda profile: started_profiles.append(dict(profile)) or "action-start-request"
        )
        self._set_stable_monitor()

        self.window.controller_start({"mode": "automatic", "continuous": False})
        self.window._set_tasks([task])

        independent = self.window.pages["independent"]
        self.assertEqual(self.window.handoff_phase, "stopping_monitor")
        self.assertEqual(len(independent._buttons), 1)
        self.assertFalse(independent._buttons[0].isEnabled())

        self.window._state_changed("stopping", {"mode": "realtime"})
        self.window._set_tasks([task])
        self.assertFalse(independent._buttons[0].isEnabled())

        self.window._state_changed("completed", {"mode": "realtime"})
        self.window._state_changed("idle", {})
        self.window._set_tasks([task])

        self.assertEqual(len(started_profiles), 1)
        self.assertTrue(self.window.start_pending)
        self.assertEqual(self.window.handoff_phase, "starting_action")
        self.assertFalse(independent._buttons[0].isEnabled())

    def test_task_refresh_inherits_pending_error_and_confirmed_worker_states(self) -> None:
        task = {
            "id": "refresh_route",
            "name": "刷新路线",
            "description": "测试任务列表刷新。",
            "action": "replay",
            "markers": [],
            "source": "refresh_route",
            "enabled": True,
            "independent_supported": True,
        }
        profiles: list[dict] = []
        self.window.controller.activate_target_window = lambda: True
        self.window.controller.start_profile = lambda profile: profiles.append(profile) or "request-id"

        self.window.controller_start({"mode": "automatic", "continuous": False})
        self.window._set_tasks([task])

        independent = self.window.pages["independent"]
        self.assertEqual(len(profiles), 1)
        self.assertTrue(self.window.start_pending)
        self.assertFalse(independent._buttons[0].isEnabled())

        self.window._controller_error("start failed")

        self.assertFalse(self.window.start_pending)
        self.assertTrue(independent._buttons[0].isEnabled())

        self.window._state_changed("starting", {"mode": "automatic"})
        self.window._set_tasks([task])

        self.assertFalse(independent._buttons[0].isEnabled())

        self.window._state_changed("idle", {})

        self.assertTrue(independent._buttons[0].isEnabled())

    def test_start_pending_blocks_duplicate_submission_and_disables_launchers(self) -> None:
        profiles: list[dict] = []
        self.window.controller.activate_target_window = lambda: True
        self.window.controller.start_profile = lambda profile: profiles.append(profile) or "request-id"

        self.window.controller_start({"mode": "automatic", "continuous": False})
        self.window.controller_start({"mode": "automatic", "continuous": False})

        self.assertEqual(len(profiles), 1)
        self.assertTrue(self.window.start_pending)
        self.assertFalse(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertFalse(self.window.pages["home"].automatic_card.button.isEnabled())
        self.assertFalse(self.window.pages["realtime"].start_button.isEnabled())
        self.assertFalse(self.window.pages["automatic"].start_button.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in self.window.pages["independent"]._buttons))
        self.assertTrue(self.window.pages["routes"]._automation_busy)

        self.window._controller_error("start failed")

        self.assertFalse(self.window.start_pending)
        self.assertTrue(self.window.pages["home"].overlay_start_button.isEnabled())
        self.assertTrue(self.window.pages["home"].automatic_card.button.isEnabled())

    def test_home_launcher_starts_realtime_overlay_only(self) -> None:
        home = self.window.pages["home"]
        realtime = self.window.pages["realtime"]
        profiles: list[dict] = []
        overlay_requests: list[bool] = []
        realtime.overlay_visibility_requested.connect(overlay_requests.append)
        realtime.show_boxes.setChecked(False)
        self.window.controller.activate_target_window = lambda: True
        self.window.controller.start_profile = lambda profile: profiles.append(profile) or "request-id"

        home.overlay_start_button.click()

        self.assertEqual(self.window.stack.currentWidget(), home)
        self.assertEqual(
            profiles,
            [
                {
                    "mode": "realtime",
                    "continuous": True,
                    "overlay": True,
                }
            ],
        )
        self.assertTrue(realtime.show_boxes.isChecked())
        self.assertEqual(overlay_requests, [])

    def test_home_launcher_stops_active_realtime_monitor(self) -> None:
        home = self.window.pages["home"]
        commands: list[str] = []
        self.window.controller.send = (
            lambda command, _payload=None: commands.append(command) or "monitor-stop-request"
        )
        monitor_profile = self._set_stable_monitor()

        self.assertEqual(home.overlay_start_button.text(), "停止")
        home.overlay_start_button.click()

        self.assertEqual(commands, ["stop"])
        self.assertIsNone(self.window.monitor_profile)
        self.assertFalse(self.window.pages["realtime"].show_boxes.isEnabled())
        self.window._handle_event(
            {"type": "overlay_visibility", "payload": {"visible": False}}
        )
        self.assertFalse(self.window._monitor_stop_profile["overlay"])
        self.assertFalse(self.window.pages["realtime"].show_boxes.isChecked())

        self.window._command_failed("monitor-stop-request", "stop rejected")

        expected_profile = dict(monitor_profile, overlay=False)
        self.assertEqual(self.window.monitor_profile, expected_profile)
        self.assertFalse(self.window.pages["realtime"].show_boxes.isChecked())
        self.assertTrue(self.window.pages["realtime"].show_boxes.isEnabled())
        self.assertEqual(home.overlay_start_button.text(), "停止")
        self.assertTrue(home.overlay_start_button.isEnabled())
        self.assertTrue(self.window.pages["realtime"].stop_button.isEnabled())
        self.assertTrue(home.automatic_card.button.isEnabled())
        self.assertTrue(self.window.pages["automatic"].start_button.isEnabled())
        self.assertTrue(all(button.isEnabled() for button in self.window.pages["independent"]._buttons))

    def test_home_automatic_card_starts_one_round_with_implicit_overlay(self) -> None:
        home = self.window.pages["home"]
        profiles: list[dict] = []
        self.window.controller.activate_target_window = lambda: True
        self.window.controller.start_profile = lambda profile: profiles.append(profile) or "request-id"

        home.automatic_card.button.click()

        self.assertEqual(profiles[0]["mode"], "automatic")
        self.assertEqual(profiles[0]["max_tasks"], 1)
        self.assertTrue(profiles[0]["overlay"])
        self.assertEqual(
            self.window.monitor_profile,
            {"mode": "realtime", "continuous": True, "overlay": True},
        )
        self.assertEqual(self.window.handoff_phase, "starting_action")

    def test_direct_independent_task_still_starts_without_overlay(self) -> None:
        independent = self.window.pages["independent"]
        profiles: list[dict] = []
        self.window.controller.activate_target_window = lambda: True
        self.window.controller.start_profile = lambda profile: profiles.append(dict(profile)) or "request-id"

        self.assertTrue(independent._buttons)
        independent._buttons[0].click()

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["mode"], "independent")
        self.assertFalse(profiles[0]["overlay"])
        self.assertIsNone(self.window.monitor_profile)
        self.assertEqual(self.window.handoff_phase, "none")

    def test_realtime_page_exposes_runtime_overlay_switch(self) -> None:
        realtime = self.window.pages["realtime"]
        requested: list[bool] = []
        realtime.overlay_visibility_requested.connect(requested.append)
        realtime.set_monitor_service_state(True, False)
        realtime.update_state("running", {"mode": "realtime"})

        self.assertIn("监控时", realtime.show_boxes.text())
        self.assertIn("3 秒", realtime.overlay_help.text())
        self.assertTrue(realtime.show_boxes.isChecked())
        realtime.show_boxes.setChecked(False)

        self.assertEqual(requested, [False])
        self.window._handle_event({"type": "overlay_visibility", "payload": {"visible": True}})
        self.assertTrue(realtime.show_boxes.isChecked())
        self.assertEqual(requested, [False])

    def test_automatic_page_starts_with_overlay_and_tracks_substate(self) -> None:
        automatic = self.window.pages["automatic"]
        automatic.set_tasks([{"id": "farm_field_latest", "name": "农田任务", "enabled": True, "markers": ["harvest"]}])
        profiles: list[dict] = []
        self.window.controller.activate_target_window = lambda: True
        self.window.controller.start_profile = lambda profile: profiles.append(profile) or "request-id"

        automatic.start_button.click()

        self.assertTrue(profiles[0]["overlay"])
        self.assertIn("隐式开启", automatic.overlay_note.text())
        self.assertIn("继续实时识别", automatic.overlay_note.text())

        self.window._handle_event(
            {"type": "route_started", "payload": {"task_id": "farm_field_latest", "action": "replay"}}
        )
        self.assertIn("正在执行 农田任务", automatic.activity.text())

    def test_routes_page_exposes_custom_task_action(self) -> None:
        routes = self.window.pages["routes"]
        self.assertEqual(routes.add_task_button.text(), "添加自定义任务")
        self.assertTrue(routes.add_task_button.isEnabled())

    def test_custom_replay_without_marker_is_not_listed_as_automatic_candidate(self) -> None:
        automatic = self.window.pages["automatic"]
        automatic.set_tasks(
            [
                {
                    "id": "user_demo",
                    "name": "用户路线",
                    "enabled": True,
                    "markers": [],
                    "priority": 0,
                    "custom": True,
                }
            ]
        )
        self.assertEqual(automatic.priority_list.count(), 0)

    def test_hot_spring_route_is_available_only_as_an_independent_task(self) -> None:
        independent = self.window.pages["independent"]
        automatic = self.window.pages["automatic"]
        independent_titles = {
            label.text()
            for label in independent.findChildren(QLabel)
            if label.objectName() == "CardTitle"
        }
        automatic_items = [automatic.priority_list.item(index).text() for index in range(automatic.priority_list.count())]

        self.assertIn("泡温泉", independent_titles)
        self.assertFalse(any("泡温泉" in item for item in automatic_items))

    def test_restaurant_pending_rerecord_is_hidden_from_launchers(self) -> None:
        independent = self.window.pages["independent"]
        automatic = self.window.pages["automatic"]
        independent_titles = {
            label.text()
            for label in independent.findChildren(QLabel)
            if label.objectName() == "CardTitle"
        }
        automatic_items = [automatic.priority_list.item(index).text() for index in range(automatic.priority_list.count())]

        self.assertNotIn("餐厅任务", independent_titles)
        self.assertFalse(any("餐厅任务" in item for item in automatic_items))


class InputReleaseResultTests(unittest.TestCase):
    def test_sendinput_release_reports_complete_success_without_pyautogui_fallback(self) -> None:
        from src.core.input import InputController

        controller = InputController(click_backend="sendinput")
        controller.held_keys.add("w")
        controller.held_mouse_buttons.add("left")
        with patch("src.core.input.sys.platform", "win32"), patch.object(
            controller, "_sendinput_key"
        ) as send_key, patch.object(controller, "_sendinput_mouse") as send_mouse, patch(
            "src.core.input.pyautogui.keyUp"
        ) as pyautogui_key_up, patch("src.core.input.pyautogui.mouseUp") as pyautogui_mouse_up:
            result = controller.release_all()

        self.assertTrue(result.success)
        self.assertEqual(set(result.attempted_keys), set(controller._COMMON_RELEASE_KEYS))
        self.assertEqual(result.attempted_mouse_buttons, ("left",))
        self.assertEqual(set(result.released_keys), set(controller._COMMON_RELEASE_KEYS))
        self.assertEqual(result.released_mouse_buttons, ("left",))
        self.assertEqual(result.remaining_keys, frozenset())
        self.assertEqual(result.remaining_mouse_buttons, frozenset())
        self.assertEqual(send_key.call_count, len(controller._COMMON_RELEASE_KEYS))
        send_mouse.assert_called_once_with(flags=0x0004, mouse_data=0)
        pyautogui_key_up.assert_not_called()
        pyautogui_mouse_up.assert_not_called()

    def test_sendinput_release_aggregates_partial_failures_and_preserves_residuals(self) -> None:
        from src.core.input import InputController

        controller = InputController(click_backend="sendinput")
        controller.held_keys.update({"a", "w"})
        controller.held_mouse_buttons.update({"left", "right"})

        def send_key(key: str, *, key_up: bool) -> None:
            if key == "w":
                raise OSError("key release rejected")

        def send_mouse(*, flags: int, **_kwargs: object) -> None:
            if flags == 0x0010:
                raise OSError("mouse release rejected")

        with patch("src.core.input.sys.platform", "win32"), patch.object(
            controller, "_sendinput_key", side_effect=send_key
        ), patch.object(controller, "_sendinput_mouse", side_effect=send_mouse), patch(
            "src.core.input.pyautogui.keyUp"
        ) as pyautogui_key_up, patch("src.core.input.pyautogui.mouseUp") as pyautogui_mouse_up:
            result = controller.release_all()

        self.assertFalse(result.success)
        self.assertEqual([name for name, _error in result.failed_keys], ["w"])
        self.assertEqual([name for name, _error in result.failed_mouse_buttons], ["right"])
        self.assertEqual(result.remaining_keys, frozenset({"w"}))
        self.assertEqual(result.remaining_mouse_buttons, frozenset({"right"}))
        self.assertEqual(controller.held_keys, {"w"})
        self.assertEqual(controller.held_mouse_buttons, {"right"})
        self.assertIn("key release rejected", result.failure_summary())
        self.assertIn("mouse release rejected", result.failure_summary())
        pyautogui_key_up.assert_not_called()
        pyautogui_mouse_up.assert_not_called()

    def test_release_result_rejects_unreported_held_state(self) -> None:
        from src.core.input import InputController

        controller = InputController(click_backend="sendinput")
        controller.held_keys.add("w")
        with patch.object(controller, "key_up"), patch.object(controller, "mouse_up"):
            result = controller.release_all()

        self.assertFalse(result.success)
        self.assertEqual(result.failed_keys, ())
        self.assertEqual(result.remaining_keys, frozenset({"w"}))


class WorkerControllerSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_command_error_preserves_request_id_for_ui_rollback(self) -> None:
        controller = WorkerController(ROOT, ROOT / "configs" / "config.yaml")
        failures: list[tuple[str, str]] = []
        process_errors: list[str] = []
        controller.command_failed.connect(lambda request_id, message: failures.append((request_id, message)))
        controller.process_error.connect(process_errors.append)

        controller._handle_line(
            '{"type":"command_error","payload":{"request_id":"handoff-stop-42","message":"worker busy"}}'
        )

        self.assertEqual(failures, [("handoff-stop-42", "worker busy")])
        self.assertEqual(process_errors, [])

    def test_demo_stop_invalidates_delayed_running_transition(self) -> None:
        controller = WorkerController(ROOT, ROOT / "configs" / "config.yaml", demo=True)
        states: list[tuple[str, dict]] = []
        controller.state_changed.connect(lambda state, payload: states.append((state, dict(payload))))

        controller.start_profile({"mode": "automatic", "overlay": True})
        stop_request_id = controller.send("stop")
        QTest.qWait(320)

        self.assertEqual([state for state, _payload in states], ["starting", "stopping", "idle"])
        self.assertEqual(states[-1][1]["request_id"], stop_request_id)
        self.assertNotIn("running", [state for state, _payload in states])

    def test_force_termination_releases_input_before_terminate_and_kill(self) -> None:
        events: list[str] = []

        class FakeProcess:
            def __init__(self) -> None:
                self.wait_results = iter((False, False, True))

            def state(self) -> QProcess.ProcessState:
                return QProcess.ProcessState.Running

            def waitForFinished(self, timeout: int) -> bool:  # noqa: N802 - Qt-compatible fake
                events.append(f"wait:{timeout}")
                return next(self.wait_results)

            def terminate(self) -> None:
                events.append("terminate")

            def kill(self) -> None:
                events.append("kill")

        controller = WorkerController(ROOT, ROOT / "configs" / "config.yaml")
        controller.process = FakeProcess()
        controller._release_input_before_force_termination = lambda: events.append("release") or True

        controller.close()

        self.assertEqual(
            events,
            ["wait:1500", "release", "terminate", "wait:1000", "kill", "wait:1000"],
        )

    def test_force_termination_logs_release_failure_before_terminate(self) -> None:
        events: list[str] = []

        class FakeProcess:
            def __init__(self) -> None:
                self.wait_results = iter((False, True))

            def state(self) -> QProcess.ProcessState:
                return QProcess.ProcessState.Running

            def waitForFinished(self, timeout: int) -> bool:  # noqa: N802 - Qt-compatible fake
                events.append(f"wait:{timeout}")
                return next(self.wait_results)

            def terminate(self) -> None:
                events.append("terminate")

        controller = WorkerController(ROOT, ROOT / "configs" / "config.yaml")
        controller.process = FakeProcess()
        controller.log_received.connect(lambda level, _message: events.append(f"log:{level}"))

        def failed_release() -> bool:
            controller.log_received.emit("ERROR", "worker 强制终止前未能安全释放输入")
            return False

        controller._release_input_before_force_termination = failed_release
        controller.close()

        self.assertEqual(events, ["wait:1500", "log:ERROR", "terminate", "wait:1000"])

    def test_ui_force_release_requires_exact_target_and_releases_known_inputs(self) -> None:
        controller = WorkerController(ROOT, ROOT / "configs" / "config.yaml")
        attempted: dict[str, set[str]] = {}
        fake_input = SimpleNamespace(
            held_keys=set(),
            held_mouse_buttons=set(),
            assert_target_window_active=lambda: None,
        )

        def release_all() -> SimpleNamespace:
            attempted["keys"] = set(fake_input.held_keys)
            attempted["mouse_buttons"] = set(fake_input.held_mouse_buttons)
            fake_input.held_keys.clear()
            fake_input.held_mouse_buttons.clear()
            return SimpleNamespace(success=True, failure_summary=lambda: "no release failures")

        fake_input.release_all = release_all
        config = {
            "runtime": {"require_admin": True},
            "window": {
                "title": "元梦之星-山海寻灵",
                "process_name": "LetsGoClient-Win64-Shipping",
                "class_name": "UnrealWindow",
            },
            "input": {"click_backend": "sendinput", "fail_safe": True},
        }
        resolved = SimpleNamespace(hwnd=4242, title="元梦之星-山海寻灵")
        with patch("src.ui.services.worker_controller.sys.platform", "win32"), patch(
            "src.ui.services.worker_controller.yaml.safe_load", return_value=config
        ), patch(
            "src.core.input.is_admin", return_value=True
        ), patch("src.core.input.InputController", return_value=fake_input) as input_factory, patch(
            "src.utils.window.resolve_window", return_value=resolved
        ), patch("src.utils.window.activate_window", return_value=SimpleNamespace()):
            released = controller._release_input_before_force_termination()

        self.assertTrue(released)
        input_factory.assert_called_once()
        self.assertTrue({"w", "a", "s", "d", "e", "q"}.issubset(attempted["keys"]))
        self.assertEqual(attempted["mouse_buttons"], {"left", "right", "middle", "x1", "x2"})
        self.assertEqual(fake_input.held_keys, set())
        self.assertEqual(fake_input.held_mouse_buttons, set())

    def test_ui_force_release_reports_partial_sendinput_failure(self) -> None:
        controller = WorkerController(ROOT, ROOT / "configs" / "config.yaml")
        messages: list[tuple[str, str]] = []
        controller.log_received.connect(lambda level, message: messages.append((level, message)))
        fake_input = SimpleNamespace(
            held_keys=set(),
            held_mouse_buttons=set(),
            assert_target_window_active=lambda: None,
        )

        def release_all() -> SimpleNamespace:
            fake_input.held_keys.clear()
            fake_input.held_keys.add("w")
            fake_input.held_mouse_buttons.clear()
            return SimpleNamespace(
                success=False,
                failure_summary=lambda: "key w: OSError: release rejected; remaining keys=['w']",
            )

        fake_input.release_all = release_all
        config = {
            "runtime": {"require_admin": True},
            "window": {
                "title": "元梦之星-山海寻灵",
                "process_name": "LetsGoClient-Win64-Shipping",
                "class_name": "UnrealWindow",
            },
            "input": {"click_backend": "sendinput", "fail_safe": True},
        }
        resolved = SimpleNamespace(hwnd=4242, title="元梦之星-山海寻灵")
        with patch("src.ui.services.worker_controller.sys.platform", "win32"), patch(
            "src.ui.services.worker_controller.yaml.safe_load", return_value=config
        ), patch("src.core.input.is_admin", return_value=True), patch(
            "src.core.input.InputController", return_value=fake_input
        ), patch("src.utils.window.resolve_window", return_value=resolved), patch(
            "src.utils.window.activate_window", return_value=SimpleNamespace()
        ):
            released = controller._release_input_before_force_termination()

        self.assertFalse(released)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0][0], "ERROR")
        self.assertIn("SendInput 释放未完成", messages[0][1])
        self.assertIn("held_keys=['w']", messages[0][1])

    def test_ui_force_release_refuses_incomplete_window_identity(self) -> None:
        controller = WorkerController(ROOT, ROOT / "configs" / "config.yaml")
        config = {
            "runtime": {"require_admin": True},
            "window": {"title": "元梦之星-山海寻灵"},
            "input": {"click_backend": "sendinput"},
        }
        with patch("src.ui.services.worker_controller.sys.platform", "win32"), patch(
            "src.ui.services.worker_controller.yaml.safe_load", return_value=config
        ), patch(
            "src.core.input.is_admin", return_value=True
        ), patch("src.core.input.InputController") as input_factory, patch(
            "src.utils.window.resolve_window"
        ) as resolve_window:
            released = controller._release_input_before_force_termination()

        self.assertFalse(released)
        input_factory.assert_not_called()
        resolve_window.assert_not_called()


if __name__ == "__main__":
    unittest.main()
