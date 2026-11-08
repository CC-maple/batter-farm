from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from pynput import keyboard, mouse

from src.core.camera_control import CameraSettings
from src.core.input import InputController
from src.tasks.farm_vehicle import FarmVehicleTask
from tools.recording.input_tool_common import start_stdin_stop_listener
from tools.recording.record_farm_actions import (
    FarmActionRecorder,
    main as record_farm_actions_main,
    parse_args as parse_farm_action_args,
)
from tools.recording.record_input_actions import EmptyRecordingError, InputActionRecorder
from tools.recording.replay_recording import ReplayInterrupted, build_edges, replay_recording, sleep_until


class FarmActionRecorderScreenshotTests(unittest.TestCase):
    def test_cli_defaults_to_250_ms_automatic_screenshots(self) -> None:
        with patch("tools.recording.record_farm_actions.sys.argv", ["record_farm_actions.py"]):
            args = parse_farm_action_args()

        self.assertEqual(args.screenshot_interval_ms, 250)

    def test_automatic_screenshot_loop_captures_until_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = FarmActionRecorder(
                config={},
                output_dir=Path(temp_dir),
                move_sample_ms=10,
                screenshot_mode="none",
                stop_key="f10",
                mark_key="f9",
                screenshot_interval_ms=10,
            )
            labels: list[str] = []

            def capture(label: str) -> None:
                labels.append(label)
                if len(labels) == 2:
                    recorder.stop_event.set()

            recorder.capture_screenshot = capture  # type: ignore[method-assign]
            worker = threading.Thread(target=recorder._auto_capture_loop)
            worker.start()
            worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(labels, ["auto_000001", "auto_000002"])

    def test_recording_metadata_includes_screenshot_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = FarmActionRecorder(
                config={},
                output_dir=Path(temp_dir),
                move_sample_ms=10,
                screenshot_mode="marks",
                stop_key="f10",
                mark_key="f9",
                screenshot_interval_ms=375,
            )
            recorder.capture_origin = (100, 200)
            recorder.capture_region = (100, 200, 1600, 900)
            recorder.hwnd = 42
            recorder.title = "game"
            with patch("tools.recording.record_farm_actions.pyautogui.size", return_value=(1920, 1080)):
                output = recorder.write_output()

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["screenshot_interval_ms"], 375)

    def test_f10_sets_stop_before_recording_the_stop_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = FarmActionRecorder(
                config={},
                output_dir=Path(temp_dir),
                move_sample_ms=10,
                screenshot_mode="important",
                stop_key="f10",
                mark_key="f9",
            )

            recorder.capture_lock.acquire()
            try:
                result: list[bool | None] = []
                worker = threading.Thread(
                    target=lambda: result.append(recorder.on_key_press(keyboard.Key.f10))
                )
                worker.start()
                worker.join(timeout=0.5)
            finally:
                recorder.capture_lock.release()

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [False])
        self.assertTrue(recorder.stop_event.is_set())
        self.assertEqual(recorder.events[-1]["type"], "session_stop")
        self.assertEqual(recorder.events[-1]["key"], "f10")

    def test_ctrl_c_stops_and_saves_recording_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = FarmActionRecorder(
                config={},
                output_dir=Path(temp_dir),
                move_sample_ms=10,
                screenshot_mode="none",
                stop_key="f10",
                mark_key="f9",
            )

            def refresh_window() -> None:
                recorder.capture_origin = (100, 200)
                recorder.capture_region = (100, 200, 1600, 900)
                recorder.hwnd = 42
                recorder.title = "game"

            recorder.refresh_window = refresh_window  # type: ignore[method-assign]
            recorder._auto_capture_loop = Mock(side_effect=KeyboardInterrupt())
            mouse_listener = Mock()
            keyboard_listener = Mock()
            with patch("tools.recording.record_farm_actions.mouse.Listener", return_value=mouse_listener), patch(
                "tools.recording.record_farm_actions.keyboard.Listener", return_value=keyboard_listener
            ), patch("tools.recording.record_farm_actions.pyautogui.size", return_value=(1920, 1080)):
                output = recorder.run()

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["events"][-1]["type"], "session_stop")
        self.assertEqual(payload["events"][-1]["key"], "keyboard_interrupt")
        mouse_listener.stop.assert_called_once_with()
        keyboard_listener.stop.assert_called_once_with()

    def test_main_rejects_non_admin_recording_when_required(self) -> None:
        with patch(
            "tools.recording.record_farm_actions.sys.argv",
            ["record_farm_actions.py", "--countdown", "0"],
        ), patch(
            "tools.recording.record_farm_actions.load_config",
            return_value={"runtime": {"require_admin": True}},
        ), patch(
            "tools.recording.record_farm_actions.setup_logging"
        ), patch(
            "tools.recording.record_farm_actions.is_admin", return_value=False
        ), patch(
            "builtins.print"
        ) as print_mock:
            result = record_farm_actions_main()

        self.assertEqual(result, 2)
        print_mock.assert_called_once_with("administrator privileges are required for reliable recording hotkeys")


class RecordingToolInputCoverageTests(unittest.TestCase):
    def test_replay_edges_include_function_keys_free_mouse_moves_scroll_and_side_buttons(self) -> None:
        point = {"client": [400, 300], "screen": [500, 400], "inside_client": True}
        events = [
            {"type": "key_down", "key": "f5", "t_ms": 10},
            {"type": "key_up", "key": "f5", "t_ms": 20},
            {"type": "mouse_move", "t_ms": 30, **point},
            {"type": "mouse_scroll", "delta": [1, -2], "t_ms": 40, **point},
            {"type": "mouse_down", "button": "x1", "t_ms": 50, **point},
            {"type": "mouse_up", "button": "x1", "t_ms": 60, **point},
            {
                "type": "camera_turn",
                "mode": "visible",
                "direction": "left",
                "duration_ms": 700,
                "t_ms": 70,
            },
        ]

        edges = build_edges(events)

        self.assertEqual(
            [edge["kind"] for edge in edges],
            ["key_down", "key_up", "mouse_move", "mouse_scroll", "mouse_down", "mouse_up", "camera_turn"],
        )
        self.assertEqual(edges[3]["delta"], [1, -2])
        self.assertEqual(edges[4]["button"], "x1")
        self.assertEqual(edges[6]["duration_ms"], 700)

    def test_sendinput_maps_f1_through_f24(self) -> None:
        controller = InputController(enabled=False)
        self.assertEqual(controller._virtual_key("f1"), 0x70)
        self.assertEqual(controller._virtual_key("F10"), 0x79)
        self.assertEqual(controller._virtual_key("f24"), 0x87)
        with self.assertRaises(ValueError):
            controller._virtual_key("f25")

    def test_sendinput_supports_standard_and_side_mouse_buttons(self) -> None:
        controller = InputController(enabled=False)
        self.assertEqual(controller._mouse_button_input("left"), (0x0002, 0x0004, 0))
        self.assertEqual(controller._mouse_button_input("right"), (0x0008, 0x0010, 0))
        self.assertEqual(controller._mouse_button_input("middle"), (0x0020, 0x0040, 0))
        self.assertEqual(controller._mouse_button_input("x1"), (0x0080, 0x0100, 1))
        self.assertEqual(controller._mouse_button_input("x2"), (0x0080, 0x0100, 2))
        with self.assertRaises(ValueError):
            controller._mouse_button_input("unknown")

    def test_sendinput_scroll_replays_vertical_and_horizontal_wheel(self) -> None:
        controller = InputController(click_backend="sendinput")
        with patch.object(controller, "_sendinput_mouse") as send:
            controller.scroll(dx=1, dy=-2)

        self.assertEqual(
            send.call_args_list,
            [
                call(flags=0x0800, mouse_data=(-240) & 0xFFFFFFFF),
                call(flags=0x1000, mouse_data=120),
            ],
        )

    def test_stdin_stop_command_sets_event(self) -> None:
        stop_event = threading.Event()
        listener = start_stdin_stop_listener(stop_event, io.StringIO("stop\n"))
        listener.join(timeout=1.0)
        self.assertTrue(stop_event.is_set())

    def test_replay_sleep_honors_cooperative_stop(self) -> None:
        stop_event = threading.Event()
        stop_event.set()
        with self.assertRaises(ReplayInterrupted):
            sleep_until(10**9, stop_event)

    def test_mixed_recording_replay_dispatches_keyboard_and_camera_turn_without_live_input(self) -> None:
        recording = {
            "version": 1,
            "recording_mode": "input_only",
            "window": {"capture_region": [100, 200, 1600, 900]},
            "camera_control": {
                "mode": "visible",
                "settings": CameraSettings().to_dict(),
            },
            "events": [
                {"type": "key_down", "key": "w", "t_ms": 0},
                {
                    "type": "camera_turn",
                    "mode": "visible",
                    "direction": "left",
                    "duration_ms": 1,
                    "t_ms": 0,
                },
                {"type": "key_up", "key": "w", "t_ms": 1},
            ],
        }

        class FakeListener:
            def __init__(self, **_kwargs) -> None:
                pass

            def start(self) -> None:
                pass

            def wait(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def join(self, timeout: float | None = None) -> None:
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recording_mixed.json"
            path.write_text(json.dumps(recording), encoding="utf-8")
            controller = Mock()
            bounds = SimpleNamespace(left=10, top=20, width=1600, height=900)
            with patch("tools.recording.replay_recording.load_config", return_value={"runtime": {"require_admin": False}}), patch(
                "tools.recording.replay_recording.setup_logging"
            ), patch(
                "tools.recording.replay_recording.resolve_target_selector", return_value=(SimpleNamespace(), "game", 42)
            ), patch(
                "tools.recording.replay_recording.resolve_window", return_value=SimpleNamespace(hwnd=42, title="game")
            ), patch(
                "tools.recording.replay_recording.activate_window"
            ), patch(
                "tools.recording.replay_recording.get_client_bounds", return_value=bounds
            ), patch(
                "tools.recording.replay_recording.InputController", return_value=controller
            ), patch(
                "tools.recording.replay_recording.keyboard.Listener", FakeListener
            ), patch(
                "tools.recording.replay_recording.start_stdin_stop_listener"
            ), patch(
                "tools.recording.replay_recording.execute_camera_turn"
            ) as camera_turn:
                result = replay_recording(path)

        self.assertEqual(result, 0)
        controller.key_down.assert_called_once_with("w")
        controller.key_up.assert_called_once_with("w")
        controller.release_all.assert_called_once_with()
        camera_turn.assert_called_once()

    def test_task_recording_replay_dispatches_keyboard_and_camera_turn_without_live_input(self) -> None:
        recording = {
            "version": 1,
            "recording_mode": "input_only",
            "camera_control": {
                "mode": "visible",
                "settings": CameraSettings().to_dict(),
            },
            "events": [
                {"type": "key_down", "key": "w", "t_ms": 0},
                {
                    "type": "camera_turn",
                    "mode": "visible",
                    "direction": "right",
                    "duration_ms": 1,
                    "t_ms": 0,
                },
                {"type": "key_up", "key": "w", "t_ms": 1},
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "recording_task_mixed.json"
            path.write_text(json.dumps(recording), encoding="utf-8")
            task = object.__new__(FarmVehicleTask)
            task.root = root
            task.input = Mock()
            task.control = SimpleNamespace(paused=False)
            task.overlay = Mock()
            task.last_capture = None
            capture_threads: list[threading.Thread] = []

            def capture(*, activate: bool) -> SimpleNamespace:
                self.assertTrue(activate)
                capture_threads.append(threading.current_thread())
                task.last_capture = SimpleNamespace(origin=(10, 20))
                return task.last_capture

            task._capture = Mock(side_effect=capture)
            task._stopped = Mock(return_value=False)
            task._wait_until_checked = Mock(return_value=0.0)
            task._raise_if_interrupted = Mock()
            task._handle_overlay_toggle = Mock()
            task._pump_overlays = Mock()

            with patch("src.tasks.farm_vehicle.execute_camera_turn") as camera_turn:
                task._run_recording_replay(
                    {"path": path.name, "include_mouse": True},
                    label_prefix="test",
                )

        task.input.key_down.assert_called_once_with("w")
        task.input.key_up.assert_called_once_with("w")
        task.input.release_all.assert_called_once_with()
        camera_turn.assert_called_once()
        self.assertEqual(camera_turn.call_args.kwargs["origin"], (10, 20))
        task._capture.assert_called_once_with(activate=True)
        self.assertEqual(capture_threads, [threading.current_thread()])

    def test_task_camera_failure_interrupts_a_long_key_hold_and_releases_input(self) -> None:
        recording = {
            "version": 1,
            "recording_mode": "input_only",
            "camera_control": {
                "mode": "visible",
                "settings": CameraSettings().to_dict(),
            },
            "events": [
                {"type": "key_down", "key": "w", "t_ms": 0},
                {
                    "type": "camera_turn",
                    "mode": "visible",
                    "direction": "right",
                    "duration_ms": 1,
                    "t_ms": 0,
                },
                {"type": "key_up", "key": "w", "t_ms": 60000},
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "recording_task_camera_failure.json"
            path.write_text(json.dumps(recording), encoding="utf-8")
            task = object.__new__(FarmVehicleTask)
            task.root = root
            task.input = Mock()
            task.control = SimpleNamespace(paused=False)
            task.overlay = Mock()
            task.last_capture = SimpleNamespace(origin=(10, 20))
            task._stopped = Mock(return_value=False)
            task._raise_if_interrupted = Mock()
            task._handle_overlay_toggle = Mock()
            task._pump_overlays = Mock()

            def wait_until_failure(
                _deadline: float,
                allow_interrupt: bool = True,
                poll_callback: object | None = None,
            ) -> float:
                del allow_interrupt
                if poll_callback is None:
                    return 0.0
                if _deadline - time.monotonic() < 1.0:
                    poll_callback()
                    return 0.0
                timeout = time.monotonic() + 1.0
                while time.monotonic() < timeout:
                    poll_callback()
                    time.sleep(0.001)
                self.fail("camera failure did not wake the recording timeline")

            task._wait_until_checked = Mock(side_effect=wait_until_failure)

            with patch(
                "src.tasks.farm_vehicle.execute_camera_turn",
                side_effect=RuntimeError("camera failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "F5/F6 camera turn failed"):
                    task._run_recording_replay(
                        {"path": path.name, "include_mouse": True},
                        label_prefix="test",
                    )

        task.input.key_down.assert_called_once_with("w")
        task.input.key_up.assert_called_once_with("w")
        task.input.release_all.assert_called_once_with()

    def test_unified_recorder_captures_wasd_click_and_f5_camera_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = InputActionRecorder(
                config={},
                output_dir=Path(temp_dir),
                move_sample_ms=1,
                stop_key="f10",
                mark_key="f9",
                camera_controller=Mock(),
            )
            recorder.capture_origin = (100, 200)
            recorder.capture_region = (100, 200, 1600, 900)
            recorder.camera_origin = (100, 200)

            recorder.on_key_press(keyboard.KeyCode.from_char("w"))
            recorder.on_click(500, 400, mouse.Button.left, True)
            recorder.on_click(500, 400, mouse.Button.left, False)
            with patch("tools.recording.record_input_actions.execute_camera_turn"):
                recorder.on_key_press(keyboard.Key.f5)
                for worker in recorder.camera_workers:
                    worker.join(timeout=1.0)
            recorder.on_key_release(keyboard.KeyCode.from_char("w"))

            event_types = [event["type"] for event in recorder.events]
            self.assertEqual(
                event_types,
                ["key_down", "mouse_down", "mouse_up", "camera_turn", "key_up"],
            )
            self.assertNotIn("f5", [event.get("key") for event in recorder.events])
            recorder.hwnd = 42
            recorder.title = "game"
            with patch("tools.recording.record_input_actions.pyautogui.size", return_value=(1920, 1080)):
                output = recorder.write_output()
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["recording_mode"], "input_only")
            self.assertEqual(payload["camera_control"]["left_key"], "f5")
            self.assertEqual(payload["camera_control"]["right_key"], "f6")
            self.assertIn("camera_turn", [event["type"] for event in payload["events"]])

    def test_unified_recorder_refuses_to_save_an_empty_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = InputActionRecorder(
                config={},
                output_dir=Path(temp_dir),
                move_sample_ms=1,
                stop_key="f10",
                mark_key="f9",
            )
            recorder.capture_origin = (100, 200)
            recorder.capture_region = (100, 200, 1600, 900)
            recorder.hwnd = 42
            recorder.events = [
                {"type": "session_start", "t_ms": 0},
                {"type": "session_stop", "t_ms": 100},
            ]

            with self.assertRaisesRegex(EmptyRecordingError, "No keyboard, mouse"):
                recorder.write_output()

    def test_unified_recorder_pumps_queued_overlay_updates_on_main_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            overlay = Mock()
            recorder = InputActionRecorder(
                config={},
                output_dir=Path(temp_dir),
                move_sample_ms=1,
                stop_key="f10",
                mark_key="f9",
                overlay=overlay,
            )
            recorder.hwnd = 42
            recorder._queue_overlay(active_keys="F5", last_action="F5 按下 · 左转")

            with patch(
                "tools.recording.record_input_actions.get_client_bounds",
                return_value=SimpleNamespace(left=10, top=20, width=1600, height=900),
            ):
                recorder._pump_overlay(force=True)

            overlay.bind_client_geometry.assert_called_once_with((10, 20), (1600, 900))
            overlay.update_status.assert_called_once_with(active_keys="F5", last_action="F5 按下 · 左转")
            overlay.pump.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
