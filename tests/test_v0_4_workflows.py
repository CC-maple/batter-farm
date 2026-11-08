from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import yaml

from src.core.camera_control import (
    CameraReplayControl,
    CameraReplayInterrupted,
    execute_camera_turn,
    load_camera_recording,
    replay_camera_recording,
)
from src.core.input import ClientCoordinateMapper, InputController, SafetyError
from src.core.ocr import OcrTextLine
from src.core.state_machine import StateResult, StateStatus
from src.core.targets import (
    OcrTargetSpec,
    TargetMatch,
    TemplateSpec,
    _scale_ocr_spec_for_image,
    match_ocr_targets,
)
from src.tasks.farm_vehicle import FarmVehicleTask


ROOT = Path(__file__).resolve().parents[1]
MOVE_PATH = ROOT / "assets/recordings/recording_20260710_110404_processor_move.json"
COLLECT_PATH = ROOT / "assets/recordings/recording_20260710_110404_processor_collect.json"
ACTIVE_COLLECT_PATH = ROOT / "assets/recordings/recording_20260710_151930.json"
CAMERA_PATH = ROOT / "assets/recordings/mouse_input_visible_20260710_120713.json"


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(0.0, float(seconds))


class FakeInputController:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.held_keys: set[str] = set()
        self.held_mouse_buttons: set[str] = set()
        self.release_all_calls = 0

    def key_down(self, key: str) -> None:
        self.held_keys.add(key)
        self.calls.append(("key_down", key))

    def key_up(self, key: str) -> None:
        self.held_keys.discard(key)
        self.calls.append(("key_up", key))

    def move_to(self, x: int, y: int, duration_seconds: float | None = None) -> None:
        self.calls.append(("move_to", int(x), int(y), duration_seconds))

    def move_relative(self, dx: int, dy: int, duration_seconds: float | None = None) -> None:
        self.calls.append(("move_relative", int(dx), int(dy), duration_seconds))

    def mouse_down(self, button: str = "left", x: int | None = None, y: int | None = None) -> None:
        self.held_mouse_buttons.add(button)
        self.calls.append(("mouse_down", button))

    def mouse_up(self, button: str = "left", after_delay_seconds: float | None = None) -> None:
        self.held_mouse_buttons.discard(button)
        self.calls.append(("mouse_up", button, after_delay_seconds))

    def release_all(self) -> None:
        self.release_all_calls += 1
        self.held_keys.clear()
        self.held_mouse_buttons.clear()
        self.calls.append(("release_all",))


class FakeOverlay:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.pump_calls = 0
        self.toggle_calls = 0

    def update_status(self, **kwargs) -> None:
        self.updates.append(kwargs)

    def pump(self) -> None:
        self.pump_calls += 1

    def toggle(self) -> bool:
        self.toggle_calls += 1
        return False


class FakeControl:
    stopped = False
    paused = False

    def consume_interrupt_request(self) -> bool:
        return False

    def consume_overlay_toggle(self) -> bool:
        return False

    def set_busy(self, busy: bool) -> None:
        return None


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def held_state(events: list[dict]) -> tuple[set[str], set[str]]:
    keys: set[str] = set()
    buttons: set[str] = set()
    for event in events:
        event_type = event.get("type")
        if event_type == "key_down":
            keys.add(str(event.get("key")))
        elif event_type == "key_up":
            keys.discard(str(event.get("key")))
        elif event_type == "mouse_down":
            buttons.add(str(event.get("button")))
        elif event_type == "mouse_up":
            buttons.discard(str(event.get("button")))
    return keys, buttons


class RealtimeMonitorTests(unittest.TestCase):
    def test_stopped_task_does_not_capture_or_reactivate_game_window(self) -> None:
        task = object.__new__(FarmVehicleTask)
        task.control = SimpleNamespace(stopped=True)
        task.overlay = SimpleNamespace(prepare_capture=lambda: self.fail("overlay capture should not start"))

        with patch("src.tasks.farm_vehicle.capture_game_client") as capture:
            with self.assertRaisesRegex(KeyboardInterrupt, "stopped before capture"):
                task._capture(activate=True)

        capture.assert_not_called()

    def test_passive_override_does_not_change_action_capture_default(self) -> None:
        task = object.__new__(FarmVehicleTask)
        task.capture_config = {"activate_window": True}
        task.templates = []
        task.overlay = SimpleNamespace(
            update_status=lambda **_items: None,
            clear_matches=lambda: None,
        )
        task._stopped = lambda: False
        task._wait_if_paused = lambda: None
        task._scale_regions_for_capture = lambda _regions, _capture: []
        task._emit_event = lambda *_args, **_kwargs: None
        capture = SimpleNamespace(
            origin=(0, 0),
            region=(0, 0, 1600, 900),
            image=np.zeros((900, 1600, 3), dtype=np.uint8),
        )
        activation_modes: list[bool] = []
        task._capture = lambda activate=False: activation_modes.append(bool(activate)) or capture

        task._capture_and_match("action_scan", templates=[])
        task._capture_and_match("realtime_scan", templates=[], activate=False)

        self.assertEqual(activation_modes, [True, False])

    def test_match_boxes_are_drawn_only_after_stable_confirmation(self) -> None:
        task = object.__new__(FarmVehicleTask)
        task.vehicle_config = {
            "marker_sequence": {
                "recognition_source": "detector",
                "realtime_confirm_frames": 2,
                "realtime_interval_seconds": 0.05,
                "steps": [{"name": "harvest", "enabled": True}],
            }
        }
        capture = SimpleNamespace(origin=(0, 0), region=(0, 0, 1600, 900), image=None)
        match = SimpleNamespace(name="harvest", score=0.92, spec=SimpleNamespace(priority=10))
        capture_calls: list[dict] = []
        shown: list[tuple] = []
        updates: list[dict] = []
        events: list[tuple[str, dict]] = []
        task.last_capture = capture
        task.overlay = SimpleNamespace(
            show_requested=True,
            update_status=lambda **items: updates.append(items),
            show_matches=lambda current_capture, matches, selected=None: shown.append(
                (current_capture, list(matches), selected)
            ),
        )
        task._marker_match_names = lambda _step: ["harvest"]
        task._marker_detector_names = lambda names, _source: list(names)
        task._marker_template_names = lambda _names, _source, _detector_names, _ocr_names=None: []
        task._templates_by_name = lambda _names: []
        task._marker_exclude_regions = lambda _config: []
        task._handle_overlay_toggle = lambda: None
        task._wait_if_paused = lambda: None
        task._sleep_checked = lambda _seconds, allow_interrupt=False: None
        task._emit_event = lambda event_type, **payload: events.append((event_type, payload))
        task._state_result_to_exit_code = lambda _result: 0

        def capture_and_match(_label: str, **kwargs) -> list[SimpleNamespace]:
            capture_calls.append(kwargs)
            return [match]

        task._capture_and_match = capture_and_match
        task._stopped = lambda: len(capture_calls) >= 2

        self.assertEqual(task.run_realtime_monitor(continuous=True), 0)
        self.assertEqual(len(capture_calls), 2)
        self.assertTrue(all(call["display_overlay"] is False for call in capture_calls))
        self.assertTrue(all(call["activate"] is False for call in capture_calls))
        self.assertEqual(shown, [(capture, [match], match)])
        stable_events = [payload for event_type, payload in events if event_type == "realtime_detection"]
        self.assertEqual(len(stable_events), 1)
        self.assertEqual(stable_events[0]["stable_frames"], 2)


class ProcessorRecordingTests(unittest.TestCase):
    def test_split_recordings_have_confirmed_boundaries_and_balanced_input(self) -> None:
        move = load_json(MOVE_PATH)
        collect = load_json(COLLECT_PATH)
        for recording in (move, collect):
            timestamps = [int(event["t_ms"]) for event in recording["events"]]
            self.assertEqual(timestamps, sorted(timestamps))
            self.assertTrue(all(t_ms >= 0 for t_ms in timestamps))
            self.assertEqual(recording["duration_ms"], timestamps[-1])
            self.assertEqual(held_state(recording["events"]), (set(), set()))
            for name in (
                "version",
                "recording_mode",
                "window",
                "screen_size",
                "move_sample_ms",
                "stop_key",
                "mark_key",
                "events",
                "source_recording",
                "source_range_ms",
                "timestamps_rebased",
            ):
                self.assertIn(name, recording)

        move_counts = Counter(
            (event["type"], event.get("button")) for event in move["events"]
        )
        collect_counts = Counter(
            (event["type"], event.get("button")) for event in collect["events"]
        )
        self.assertEqual(move_counts[("mouse_down", "right")], 1)
        self.assertEqual(move_counts[("mouse_up", "right")], 1)
        self.assertEqual(collect_counts[("mouse_down", "right")], 13)
        self.assertEqual(collect_counts[("mouse_up", "right")], 13)
        self.assertEqual(move_counts[("key_down", None)], move_counts[("key_up", None)])
        self.assertEqual(collect_counts[("key_down", None)], collect_counts[("key_up", None)])
        self.assertEqual(move["events"][0], {"type": "session_start", "t_ms": 0})
        self.assertEqual(move["events"][-1]["type"], "session_stop")
        self.assertEqual(collect["events"][0], {"type": "session_start", "t_ms": 0})

        boundary = [
            event
            for event in move["events"]
            if event.get("type") in {"mouse_down", "mouse_up"}
        ]
        self.assertEqual([event["t_ms"] for event in boundary], [29513, 29611])
        self.assertEqual([event.get("client") for event in boundary], [[775, 403], [775, 403]])

    def test_active_collect_recording_is_balanced_and_complete(self) -> None:
        recording = load_json(ACTIVE_COLLECT_PATH)
        events = recording["events"]
        timestamps = [int(event["t_ms"]) for event in events]
        counts = Counter((event["type"], event.get("button")) for event in events)
        self.assertEqual(len(events), 62)
        self.assertEqual(recording["duration_ms"], 59140)
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual(timestamps[-1], recording["duration_ms"])
        self.assertEqual(counts[("key_down", None)], 17)
        self.assertEqual(counts[("key_up", None)], 17)
        self.assertEqual(counts[("mouse_down", "right")], 13)
        self.assertEqual(counts[("mouse_up", "right")], 13)
        self.assertEqual(held_state(events), (set(), set()))
        self.assertEqual(
            sum(event.get("type") == "key_down" and event.get("key") == "r" for event in events),
            4,
        )

    def test_fake_input_replays_each_processor_segment_without_residue(self) -> None:
        for path, expected_right_clicks in ((MOVE_PATH, 1), (ACTIVE_COLLECT_PATH, 13)):
            fake = FakeInputController()
            task = object.__new__(FarmVehicleTask)
            task.root = ROOT
            task.input = fake
            task.control = FakeControl()
            task.overlay = FakeOverlay()
            task.last_capture = SimpleNamespace(origin=(0, 0))
            task._capture = lambda activate=False: SimpleNamespace(
                origin=(0, 0),
                image=np.zeros((900, 1600, 3), dtype=np.uint8),
            )
            task._wait_until_checked = lambda deadline: 0.0
            task._run_recording_replay(
                {
                    "path": str(path),
                    "include_mouse": True,
                    "ignore_keys": [],
                    "speed": 1.0,
                    "max_initial_wait_seconds": 1.5,
                },
                label_prefix=path.stem,
            )
            downs = [call for call in fake.calls if call[:2] == ("mouse_down", "right")]
            ups = [call for call in fake.calls if call[:2] == ("mouse_up", "right")]
            self.assertEqual(len(downs), expected_right_clicks)
            self.assertGreaterEqual(len(ups), expected_right_clicks)
            self.assertEqual(fake.held_keys, set())
            self.assertEqual(fake.held_mouse_buttons, set())
            self.assertGreaterEqual(fake.release_all_calls, 1)

    def test_collect_retry_does_not_repeat_move_until_reset_clears_state(self) -> None:
        task = object.__new__(FarmVehicleTask)
        task.vehicle_config = {
            "processor": {
                "enabled": True,
                "move_recording": {"path": str(MOVE_PATH)},
                "move_verification": {"enabled": True},
                "collect_recording": {"path": str(ACTIVE_COLLECT_PATH)},
                "collect_verification": {"enabled": True},
                "collect_max_attempts": 2,
            }
        }
        task.config = {}
        task.root = ROOT
        task.input = FakeInputController()
        task.overlay = FakeOverlay()
        task.processor_state = {"move_completed": False, "collect_completed": False}
        task._sleep_checked = lambda seconds: None
        task._capture = lambda activate=False: SimpleNamespace()
        task._save_last = lambda label: None
        replay_labels: list[str] = []
        task._run_recording_replay = lambda config, label_prefix: replay_labels.append(label_prefix)
        verification_results = iter((True, False, True))
        task._verify_processor_state = lambda config, capture, label: next(verification_results)
        task._recording_contains_key_down = lambda config, key: False
        result = task._run_processor_task({}, "processor_latest")
        self.assertEqual(result.status, StateStatus.SUCCEEDED)
        self.assertEqual(
            replay_labels,
            ["processor_latest_move", "processor_latest_collect_01", "processor_latest_collect_02"],
        )

        replay_labels.clear()
        verification_results = iter((True, False))
        task._verify_processor_state = lambda config, capture, label: next(verification_results)
        task._recording_contains_key_down = lambda config, key: True
        result = task._run_processor_task({}, "processor_latest")
        self.assertEqual(result.status, StateStatus.FAILED)
        self.assertEqual(replay_labels, ["processor_latest_move", "processor_latest_collect_01"])
        self.assertFalse(task.processor_state["move_completed"])

    def test_residual_processor_marker_retries_full_flow_once_then_succeeds(self) -> None:
        task = object.__new__(FarmVehicleTask)
        task.vehicle_config = {
            "processor": {
                "enabled": True,
                "task_max_attempts": 2,
                "move_recording": {"path": str(MOVE_PATH)},
                "move_verification": {"enabled": True},
                "collect_recording": {"path": str(ACTIVE_COLLECT_PATH)},
                "collect_verification": {"enabled": True},
                "completion_verification": {"enabled": True, "consecutive_frames": 2},
                "collect_max_attempts": 1,
            }
        }
        task.config = {}
        task.root = ROOT
        task.input = FakeInputController()
        task.overlay = FakeOverlay()
        task.processor_state = {"move_completed": False, "collect_completed": False}
        task._sleep_checked = lambda seconds: None
        task._capture = lambda activate=False: SimpleNamespace()
        task._save_last = lambda label: None
        replay_labels: list[str] = []
        task._run_recording_replay = lambda config, label_prefix: replay_labels.append(label_prefix)
        verification_results = iter((True, True, False, True, True, True))
        task._wait_for_processor_state = lambda config, label: next(verification_results)
        task._recording_contains_key_down = lambda config, key: True

        result = task._run_processor_task({}, "processor_latest")

        self.assertEqual(result.status, StateStatus.SUCCEEDED)
        self.assertEqual(
            replay_labels,
            [
                "processor_latest_attempt_01_move",
                "processor_latest_attempt_01_collect_01",
                "processor_latest_attempt_02_move",
                "processor_latest_attempt_02_collect_01",
            ],
        )
        self.assertTrue(task.processor_state["collect_completed"])

    def test_residual_processor_marker_stops_after_two_full_attempts(self) -> None:
        task = object.__new__(FarmVehicleTask)
        task.vehicle_config = {
            "processor": {
                "enabled": True,
                "task_max_attempts": 2,
                "move_recording": {"path": str(MOVE_PATH)},
                "move_verification": {"enabled": True},
                "collect_recording": {"path": str(ACTIVE_COLLECT_PATH)},
                "collect_verification": {"enabled": True},
                "completion_verification": {"enabled": True, "consecutive_frames": 2},
                "collect_max_attempts": 1,
            }
        }
        task.config = {}
        task.root = ROOT
        task.input = FakeInputController()
        task.overlay = FakeOverlay()
        task.processor_state = {"move_completed": False, "collect_completed": False}
        task._sleep_checked = lambda seconds: None
        task._capture = lambda activate=False: SimpleNamespace()
        task._save_last = lambda label: None
        replay_labels: list[str] = []
        task._run_recording_replay = lambda config, label_prefix: replay_labels.append(label_prefix)
        verification_results = iter((True, True, False, True, True, False))
        task._wait_for_processor_state = lambda config, label: next(verification_results)
        task._recording_contains_key_down = lambda config, key: True

        result = task._run_processor_task({}, "processor_latest")

        self.assertEqual(result.status, StateStatus.FAILED)
        self.assertIn("人工检查", result.message)
        self.assertEqual(len(replay_labels), 4)
        self.assertFalse(task.processor_state["collect_completed"])


class RestaurantCameraTests(unittest.TestCase):
    def test_official_camera_recording_metadata(self) -> None:
        recording = load_camera_recording(CAMERA_PATH, require_mode="visible")
        self.assertEqual(recording.mode, "visible")
        self.assertEqual(len(recording.events), 4)
        self.assertEqual(recording.settings.step_dx, 4)
        self.assertEqual(recording.settings.interval_ms, 10)
        self.assertEqual(recording.settings.anchor_client, (800, 450))
        self.assertEqual(recording.settings.segment_ms, 250)
        self.assertEqual(recording.settings.segment_gap_ms, 30)

    def test_each_visible_turn_drags_and_moves_in_the_recorded_direction(self) -> None:
        recording = load_camera_recording(CAMERA_PATH, require_mode="visible")
        for event in recording.events:
            fake = FakeInputController()
            clock = FakeClock()
            execute_camera_turn(
                controller=fake,
                origin=(160, 70),
                mode="visible",
                direction=event.direction,
                settings=recording.settings,
                duration_ms=event.duration_ms,
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
            downs = [call for call in fake.calls if call[:2] == ("mouse_down", "left")]
            ups = [call for call in fake.calls if call[:2] == ("mouse_up", "left")]
            deltas = [call[1] for call in fake.calls if call[0] == "move_relative"]
            self.assertGreaterEqual(len(downs), 1)
            self.assertEqual(len(downs), len(ups))
            self.assertTrue(deltas)
            if event.direction == "left":
                self.assertTrue(all(dx < 0 for dx in deltas))
            else:
                self.assertTrue(all(dx > 0 for dx in deltas))

    def test_interruption_releases_left_button_and_all_input(self) -> None:
        recording = load_camera_recording(CAMERA_PATH, require_mode="visible")
        fake = FakeInputController()
        clock = FakeClock()

        def stop_requested() -> bool:
            return len([call for call in fake.calls if call[0] == "move_relative"]) >= 5

        with self.assertRaises(CameraReplayInterrupted):
            replay_camera_recording(
                recording=recording,
                controller=fake,
                origin=(160, 70),
                control=CameraReplayControl(stop_requested=stop_requested),
                require_mode="visible",
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(fake.held_mouse_buttons, set())
        self.assertGreaterEqual(fake.release_all_calls, 1)
        self.assertTrue(any(call[:2] == ("mouse_up", "left") for call in fake.calls))

    def test_pause_extends_absolute_timeline(self) -> None:
        recording = load_camera_recording(CAMERA_PATH, require_mode="visible")
        fake = FakeInputController()
        clock = FakeClock()

        def paused() -> bool:
            return 2.2 <= clock.value < 2.3

        replay_camera_recording(
            recording=recording,
            controller=fake,
            origin=(160, 70),
            control=CameraReplayControl(paused=paused),
            require_mode="visible",
            clock=clock.monotonic,
            sleep=clock.sleep,
        )
        self.assertGreaterEqual(clock.value, recording.duration_ms / 1000.0 + 0.09)

    def test_restaurant_attempt_runs_camera_once_and_config_is_scoped(self) -> None:
        config = yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8"))
        self.assertIn("farm_camera_replay", config["farm_vehicle"]["restaurant_service"])
        self.assertNotIn("farm_camera_replay", config["farm_vehicle"].get("processor", {}))
        self.assertNotIn("farm_camera_replay", config["farm_vehicle"].get("marker_sequence", {}))

        task = object.__new__(FarmVehicleTask)
        task.root = ROOT
        task.input = FakeInputController()
        task.control = FakeControl()
        task.last_capture = SimpleNamespace(origin=(160, 70))
        task._sleep_checked = lambda seconds: None
        task._capture = lambda activate=False: task.last_capture
        task._save_last = lambda label: None
        state = {"camera_replay_executed": False}
        recording = load_camera_recording(CAMERA_PATH, require_mode="visible")
        raw_config = {
            "path": str(CAMERA_PATH),
            "settle_seconds": 0,
            "run_once_per_attempt": True,
            "require_mode": "visible",
        }
        with patch("src.tasks.farm_vehicle.load_camera_recording", return_value=recording), patch(
            "src.tasks.farm_vehicle.replay_camera_recording"
        ) as replay:
            task._run_restaurant_camera_replay_once(raw_config, "restaurant", state)
            task._run_restaurant_camera_replay_once(raw_config, "restaurant", state)
            replay.assert_called_once()

        method_source = inspect.getsource(FarmVehicleTask._run_restaurant_service)
        self.assertIn("_run_restaurant_camera_replay_once", method_source)
        for method_name in ("_run_processor_task", "_run_recording_plan"):
            self.assertNotIn(
                "_run_restaurant_camera_replay_once",
                inspect.getsource(getattr(FarmVehicleTask, method_name)),
            )

    def test_restaurant_reset_runs_before_service_logic(self) -> None:
        task = object.__new__(FarmVehicleTask)
        task.overlay = FakeOverlay()
        task._safe_label = lambda value: value
        task._emit_event = lambda *args, **kwargs: None
        calls: list[tuple[str, object]] = []
        task._run_reset = lambda config, default_enabled=False: calls.append(("reset", dict(config)))
        task._run_restaurant_service = lambda step, label: calls.append(("service", label)) or StateResult(
            StateStatus.SUCCEEDED,
            message="ok",
        )
        task._run_post_action_match_click = lambda config, label: None
        task._capture_and_match = lambda label: []
        task._save_last = lambda label: None
        reset = {"enabled": True, "key": "r", "hold_seconds": 0.08, "wait_seconds": 0.5}
        step = {
            "name": "restaurant_service_latest",
            "action": "restaurant_service",
            "reset_before_start": reset,
            "reset_after_finish": {"enabled": False},
            "capture_after_action": False,
        }
        match = SimpleNamespace(name="farm_header", score=0.99)

        result = task._execute_marker_step(step, match, {})

        self.assertEqual(result.status, StateStatus.SUCCEEDED)
        self.assertEqual(calls[0], ("reset", reset))
        self.assertEqual(calls[1], ("service", "restaurant_service_latest"))


class CurrentReplayConfigTests(unittest.TestCase):
    def test_current_business_recordings_and_reachable_steps(self) -> None:
        config = yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8"))
        sources = config["route"]["source_recordings"]
        self.assertEqual(sources["farm_field_latest_104821"], "assets/recordings/recording_20260710_104821.json")
        self.assertEqual(sources["pasture_latest_110127"], "assets/recordings/recording_20260710_110127.json")
        self.assertEqual(sources["fishpond_latest_110224"], "assets/recordings/recording_20260710_110224.json")
        self.assertEqual(sources["hot_spring_latest_110602"], "assets/recordings/recording_20260710_110602.json")

        marker = config["farm_vehicle"]["marker_sequence"]
        self.assertEqual(marker["recognition_source"], "hybrid")
        self.assertTrue(marker["wait_for_initial_f9"])
        self.assertTrue(marker["exclude_overlay_region"])
        self.assertTrue(config["overlay"]["enabled"])
        self.assertTrue(config["overlay"]["bind_to_window"])
        self.assertTrue(config["overlay"]["click_through"])
        self.assertFalse(config["overlay"]["hide_before_capture"])
        self.assertEqual(config["overlay"]["follow_interval_ms"], 50)
        steps = {step["name"]: step for step in marker["steps"]}
        fish_card = steps["fish_card_click"]
        self.assertEqual(fish_card["action"], "click_until_gone")
        self.assertEqual(fish_card["recognition_source"], "ocr")
        self.assertEqual(fish_card["max_clicks"], 5)
        self.assertTrue(fish_card["repeatable"])
        self.assertTrue(fish_card["priority_over_run_count"])
        self.assertTrue(fish_card["wait_for_rescan_on_failure"])
        self.assertEqual(fish_card["match_any"], ["fish_card_result"])
        self.assertEqual(steps["farm_field_latest"]["recording_replay"]["source"], "farm_field_latest_104821")
        self.assertEqual(steps["fishpond_latest"]["recording_replay"]["source"], "fishpond_latest_110224")
        fishpond_wait = steps["fishpond_latest"]["recording_replay"]["match_waits"][0]
        self.assertEqual(fishpond_wait["recognition_source"], "ocr")
        self.assertTrue(fishpond_wait["click_until_gone"])
        self.assertEqual(fishpond_wait["max_clicks"], 5)
        self.assertTrue(steps["fishpond_latest"]["wait_for_rescan_on_failure"])
        self.assertEqual(steps["pasture_latest"]["recording_replay"]["source"], "pasture_latest_110127")
        self.assertNotIn("hot_spring_latest", steps)
        hot_spring = next(
            task
            for task in config["farm_vehicle"]["independent_tasks"]
            if task["name"] == "hot_spring_latest"
        )
        self.assertEqual(hot_spring["recording_replay"]["source"], "hot_spring_latest_110602")
        self.assertTrue((ROOT / sources["hot_spring_latest_110602"]).is_file())
        self.assertEqual(steps["processor_latest"]["action"], "processor")
        self.assertFalse(steps["processor_latest"]["skip_on_failure"])
        self.assertFalse(steps["processor_latest"]["mark_completed_on_failure"])
        self.assertTrue(steps["processor_latest"]["wait_for_rescan_on_failure"])
        self.assertEqual(steps["processor_latest"]["match_any"], ["harvest"])
        self.assertEqual(steps["processor_latest"]["match_regions"]["harvest"], [63, 93, 165, 118])
        processor_config = config["farm_vehicle"]["processor"]
        self.assertEqual(processor_config["task_max_attempts"], 2)
        self.assertEqual(processor_config["collect_max_attempts"], 1)
        move_verification = config["farm_vehicle"]["processor"]["move_verification"]
        self.assertEqual(move_verification["recognition_source"], "ocr")
        self.assertEqual(move_verification["contains"], ["的小屋"])
        self.assertEqual(move_verification["roi"], [70, 10, 240, 55])
        self.assertEqual(move_verification["max_wait_seconds"], 15.0)
        collect_verification = config["farm_vehicle"]["processor"]["collect_verification"]
        self.assertEqual(
            config["farm_vehicle"]["processor"]["collect_recording"]["path"],
            "assets/recordings/recording_20260710_151930.json",
        )
        self.assertEqual(collect_verification["recognition_source"], "ocr")
        self.assertEqual(collect_verification["contains"], ["的农场"])
        completion_verification = processor_config["completion_verification"]
        self.assertEqual(completion_verification["recognition_source"], "detector")
        self.assertEqual(completion_verification["expected"], "absent")
        self.assertEqual(completion_verification["match_any"], ["harvest"])
        self.assertEqual(completion_verification["match_regions"]["harvest"], [63, 93, 165, 118])
        self.assertEqual(completion_verification["consecutive_frames"], 2)
        self.assertEqual(collect_verification["expected"], "present")
        self.assertEqual(collect_verification["max_wait_seconds"], 15.0)
        self.assertNotIn("templates", collect_verification)
        template_names = {template["name"] for template in config["recognition"]["templates"]}
        self.assertNotIn("workshop_harvest", template_names)
        self.assertNotIn("processor_house_room_anchor", template_names)
        fish_card_ocr = next(
            target for target in config["recognition"]["ocr_targets"] if target["name"] == "fish_card_result"
        )
        self.assertEqual(fish_card_ocr["text"], "恭喜获得鱼卡")
        self.assertEqual(fish_card_ocr["roi"], [480, 100, 680, 140])
        self.assertEqual(fish_card_ocr["base_client_size"], [1600, 900])
        self.assertEqual(fish_card_ocr["click_point"], [1110, 137])
        self.assertNotIn("fish_card_result", template_names)
        self.assertEqual(
            steps["farm_field_latest"]["match_exclude_regions"]["harvest"],
            [80, 100, 150, 130],
        )
        self.assertEqual(steps["restaurant_service_latest"]["action"], "restaurant_service")
        self.assertEqual(steps["restaurant_service_latest"]["match_any"], ["farm_header"])
        restaurant_reset = steps["restaurant_service_latest"]["reset_before_start"]
        self.assertTrue(restaurant_reset["enabled"])
        self.assertEqual(restaurant_reset["key"], "r")
        self.assertEqual(restaurant_reset["hold_seconds"], 0.08)
        self.assertEqual(restaurant_reset["wait_seconds"], 0.5)
        self.assertNotIn("nav", config)
        self.assertNotIn("success", config)
        self.assertNotIn("recording_sequence", config["farm_vehicle"])
        self.assertNotIn("direct_cart", config["farm_vehicle"])
        self.assertNotIn("mode", config["farm_vehicle"])

    def test_overlay_pump_streams_live_client_geometry(self) -> None:
        class FollowOverlay:
            enabled = True

            def __init__(self) -> None:
                self.geometry_calls: list[tuple[tuple[int, int], tuple[int, int]]] = []
                self.pump_calls = 0

            def bind_client_geometry(self, origin: tuple[int, int], size: tuple[int, int]) -> None:
                self.geometry_calls.append((origin, size))

            def pump(self) -> None:
                self.pump_calls += 1

        task = object.__new__(FarmVehicleTask)
        task.overlay = FollowOverlay()
        task.input = SimpleNamespace(target_window_hwnd=12345)
        task.window_config = {}
        task._overlay_follow_interval_seconds = 0.05
        task._overlay_last_follow_at = 0.0
        task._overlay_follow_error_reported = False

        bounds = SimpleNamespace(left=140, top=80, width=1600, height=900)
        with patch("src.tasks.farm_vehicle.get_client_bounds", return_value=bounds) as get_bounds:
            task._pump_overlays()

        get_bounds.assert_called_once_with(12345, log_result=False)
        self.assertEqual(task.overlay.geometry_calls, [((140, 80), (1600, 900))])
        self.assertEqual(task.overlay.pump_calls, 1)

    def test_processor_harvest_roi_is_not_claimed_by_farm_step(self) -> None:
        config = yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8"))
        steps = {step["name"]: step for step in config["farm_vehicle"]["marker_sequence"]["steps"]}
        task = object.__new__(FarmVehicleTask)
        task.last_capture = None

        spec = TemplateSpec(name="harvest", path=ROOT / "models/farm-icons/best.onnx")
        processor_match = TargetMatch(spec=spec, score=0.90, top_left=(170, 195), size=(10, 10))
        farm_match = TargetMatch(spec=spec, score=0.85, top_left=(395, 245), size=(10, 10))
        matches = [processor_match, farm_match]

        self.assertEqual(
            task._filter_marker_step_matches(matches, ["harvest"], steps["farm_field_latest"]),
            [farm_match],
        )
        self.assertEqual(
            task._filter_marker_step_matches(matches, ["harvest"], steps["processor_latest"]),
            [processor_match],
        )

    def test_watering_detection_above_old_roi_is_claimed_by_farm_step(self) -> None:
        config = yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8"))
        steps = {step["name"]: step for step in config["farm_vehicle"]["marker_sequence"]["steps"]}
        task = object.__new__(FarmVehicleTask)
        task.last_capture = None
        spec = TemplateSpec(name="watering", path=ROOT / "models/farm-icons/best.onnx")
        watering = TargetMatch(spec=spec, score=0.9193, top_left=(514, 140), size=(64, 52))

        self.assertEqual(watering.center, (546, 166))
        self.assertEqual(
            task._filter_marker_step_matches([watering], ["watering"], steps["farm_field_latest"]),
            [watering],
        )


    def test_processor_collect_verification_uses_detector_and_fails_closed(self) -> None:
        task = object.__new__(FarmVehicleTask)
        task.config = {"detection": {"classes": {"harvest": {}}}}
        task.root = ROOT
        task.templates = []
        task.last_capture = None
        task._get_detector = lambda: object()
        capture = SimpleNamespace()
        verification = {
            "enabled": True,
            "recognition_source": "detector",
            "expected": "absent",
            "match_any": ["harvest"],
            "match_regions": {"harvest": [80, 100, 150, 130]},
        }
        spec = TemplateSpec(name="harvest", path=ROOT / "models/farm-icons/best.onnx")
        processor_match = TargetMatch(spec=spec, score=0.90, top_left=(170, 195), size=(10, 10))

        task._detect_targets = lambda current_capture, names, ignore_regions: [processor_match]
        self.assertFalse(task._verify_processor_state(verification, capture, "collect"))

        task._detect_targets = lambda current_capture, names, ignore_regions: []
        self.assertTrue(task._verify_processor_state(verification, capture, "collect"))

        task._get_detector = lambda: None
        self.assertFalse(task._verify_processor_state(verification, capture, "collect"))

    def test_processor_ocr_verification_uses_scene_text_result(self) -> None:
        task = object.__new__(FarmVehicleTask)
        capture = SimpleNamespace()
        verification = {
            "enabled": True,
            "recognition_source": "ocr",
            "expected": "present",
            "roi": [85, 15, 220, 55],
            "contains": ["的农场"],
        }
        task._verify_scene_text = lambda config, current_capture, label: True
        self.assertTrue(task._verify_processor_state(verification, capture, "collect"))
        task._verify_scene_text = lambda config, current_capture, label: False
        self.assertFalse(task._verify_processor_state(verification, capture, "collect"))

    def test_processor_move_verification_retries_until_room_anchor_is_visible(self) -> None:
        task = object.__new__(FarmVehicleTask)
        captures = [SimpleNamespace(), SimpleNamespace(), SimpleNamespace()]
        capture_iter = iter(captures)
        task._capture = lambda activate=False: next(capture_iter)
        verification_results = iter((False, False, True))
        task._verify_processor_state = lambda config, capture, label: next(verification_results)
        sleeps: list[float] = []
        task._sleep_checked = lambda seconds: sleeps.append(seconds)

        self.assertTrue(
            task._wait_for_processor_state(
                {"enabled": True, "max_wait_seconds": 15.0, "interval_seconds": 0.5},
                "after_processor_move",
            )
        )
        self.assertEqual(sleeps, [0.5, 0.5])

    def test_processor_verification_requires_consecutive_frames(self) -> None:
        task = object.__new__(FarmVehicleTask)
        captures = [SimpleNamespace() for _ in range(4)]
        capture_iter = iter(captures)
        task._capture = lambda activate=False: next(capture_iter)
        verification_results = iter((True, False, True, True))
        task._verify_processor_state = lambda config, capture, label: next(verification_results)
        sleeps: list[float] = []
        task._sleep_checked = lambda seconds: sleeps.append(seconds)

        self.assertTrue(
            task._wait_for_processor_state(
                {
                    "enabled": True,
                    "max_wait_seconds": 15.0,
                    "interval_seconds": 0.5,
                    "consecutive_frames": 2,
                },
                "after_processor_completion",
            )
        )
        self.assertEqual(sleeps, [0.5, 0.5, 0.5])

    def test_fish_card_ocr_geometry_scales_for_supported_client_resolutions(self) -> None:
        spec = OcrTargetSpec(
            name="fish_card_result",
            text="恭喜获得鱼卡",
            roi=(480, 100, 680, 140),
            click=True,
            click_point=(1110, 137),
            base_client_size=(1600, 900),
        )
        cases = {
            (1280, 720): ((384, 80, 544, 112), (888, 110)),
            (1600, 900): ((480, 100, 680, 140), (1110, 137)),
            (1920, 1080): ((576, 120, 816, 168), (1332, 164)),
            (2560, 1440): ((768, 160, 1088, 224), (1776, 219)),
        }

        for (width, height), (roi, click_point) in cases.items():
            with self.subTest(resolution=(width, height)):
                image = np.zeros((height, width, 3), dtype=np.uint8)
                scaled = _scale_ocr_spec_for_image(spec, image)
                self.assertEqual(scaled.roi, roi)
                self.assertEqual(scaled.click_point, click_point)

    def test_fish_card_ocr_requires_exact_text_and_minimum_score(self) -> None:
        spec = OcrTargetSpec(
            name="fish_card_result",
            text="恭喜获得鱼卡",
            min_score=0.90,
            priority=800,
            roi=(480, 100, 680, 140),
            click=True,
            click_point=(1110, 137),
            base_client_size=(1600, 900),
        )
        image = np.zeros((900, 1600, 3), dtype=np.uint8)
        exact = OcrTextLine(
            text="恭喜获得鱼卡",
            score=0.95698,
            box=((99, 17), (539, 18), (538, 137), (99, 136)),
        )
        wrong = OcrTextLine(
            text="恭喜获得鱼",
            score=0.999,
            box=((100, 20), (500, 20), (500, 100), (100, 100)),
        )
        low_score = OcrTextLine(
            text="恭喜获得鱼卡",
            score=0.899,
            box=((100, 20), (500, 20), (500, 100), (100, 100)),
        )

        exact_matches = match_ocr_targets(image, [spec], SimpleNamespace(recognize=lambda _: [wrong, exact]))
        rejected = match_ocr_targets(image, [spec], SimpleNamespace(recognize=lambda _: [wrong, low_score]))

        self.assertEqual(len(exact_matches), 1)
        self.assertEqual(exact_matches[0].top_left, (579, 117))
        self.assertAlmostEqual(exact_matches[0].score, 0.95698)
        self.assertEqual(exact_matches[0].click_point, (1110, 137))
        self.assertEqual(rejected, [])

    def test_recording_coordinates_scale_and_fail_closed(self) -> None:
        recording = {"window": {"capture_region": [160, 70, 1600, 900]}}
        cases = {
            (1280, 720): (626, 531),
            (1600, 900): (782, 664),
            (1920, 1080): (938, 797),
            (2560, 1440): (1251, 1062),
        }
        for current_size, expected_client in cases.items():
            with self.subTest(current_size=current_size):
                mapper = ClientCoordinateMapper.from_recording(recording, origin=(100, 200), current_size=current_size)
                screen = mapper.screen_point([782, 664], inside_client=True)
                self.assertEqual(screen, (100 + expected_client[0], 200 + expected_client[1]))

        mapper = ClientCoordinateMapper.from_recording(recording, origin=(0, 0), current_size=(1600, 900))
        with self.assertRaises(SafetyError):
            mapper.screen_point([-1, 200], inside_client=True)
        with self.assertRaises(SafetyError):
            mapper.screen_point([782, 664], inside_client=False)
        with self.assertRaises(SafetyError):
            ClientCoordinateMapper.from_recording(recording, origin=(0, 0), current_size=(1366, 768))

    def test_disabled_input_controller_is_a_hard_safety_boundary(self) -> None:
        controller = InputController(enabled=False)
        for action in (
            lambda: controller.move_to(100, 100),
            lambda: controller.safe_click(100, 100),
            lambda: controller.key_down("w"),
            lambda: controller.mouse_down("left"),
        ):
            with self.assertRaises(SafetyError):
                action()
        controller.release_all()

    def test_dry_run_previews_selected_step_without_execution(self) -> None:
        task = object.__new__(FarmVehicleTask)
        step = {"name": "farm_field_latest", "action": "replay"}
        match = SimpleNamespace(name="watering", score=0.91)
        task.vehicle_config = {
            "marker_sequence": {
                "max_steps": None,
                "scan_wait_seconds": 0,
                "steps": [step],
            }
        }
        task.runtime_config = {"_dry_run": True}
        task.overlay = FakeOverlay()
        task.control = FakeControl()
        task._stopped = lambda: False
        task._wait_for_initial_marker_start = lambda config: None
        task._sleep_checked = lambda seconds: None
        task._save_last = lambda label: None
        task._select_marker_steps = lambda *args, **kwargs: [(step, match)]
        task._wait_for_marker_rescan = lambda phase="idle": "exit"
        task._execute_marker_step = lambda *args, **kwargs: self.fail("dry-run must not execute marker steps")

        result = task._run_marker_sequence()

        self.assertEqual(result.status, StateStatus.SUCCEEDED)
        preview = [item for item in task.overlay.updates if item.get("run_state") == "preview"]
        self.assertEqual(len(preview), 1)
        self.assertIn("未发送任何输入", preview[0]["log"])

    def test_fish_card_reports_overlay_error_after_five_persistent_clicks(self) -> None:
        task = object.__new__(FarmVehicleTask)
        task.overlay = FakeOverlay()
        task.templates = []
        task.ocr_targets = [OcrTargetSpec(name="fish_card_result", text="恭喜获得鱼卡")]
        task._sleep_checked = lambda seconds: None
        task._save_last = lambda label: None
        clicks: list[TargetMatch] = []
        task._click_match = lambda match: clicks.append(match)

        spec = OcrTargetSpec(name="fish_card_result", text="恭喜获得鱼卡")
        match = TargetMatch(spec=spec, score=0.95, top_left=(600, 150), size=(300, 60))
        task._capture_and_match = lambda label, **kwargs: [match]
        result = task._click_match_until_gone(
            {
                "name": "fish_card_click",
                "recognition_source": "ocr",
                "match_any": ["fish_card_result"],
                "max_clicks": 5,
                "after_click_wait_seconds": 0,
            },
            match,
            "fish_card_click",
        )

        self.assertEqual(result.status, StateStatus.FAILED)
        self.assertEqual(len(clicks), 5)
        self.assertIn("连续点击 5 次", result.message)
        self.assertEqual(task.overlay.updates[-1]["run_state"], "error")
        self.assertIn("文字仍存在", task.overlay.updates[-1]["log"])

    def test_fish_card_stops_clicking_as_soon_as_text_disappears(self) -> None:
        task = object.__new__(FarmVehicleTask)
        task.overlay = FakeOverlay()
        task.templates = []
        task.ocr_targets = [OcrTargetSpec(name="fish_card_result", text="恭喜获得鱼卡")]
        task._sleep_checked = lambda seconds: None
        task._save_last = lambda label: None
        clicks: list[TargetMatch] = []
        task._click_match = lambda match: clicks.append(match)

        spec = OcrTargetSpec(name="fish_card_result", text="恭喜获得鱼卡")
        match = TargetMatch(spec=spec, score=0.95, top_left=(600, 150), size=(300, 60))
        scans = iter(([match], []))
        task._capture_and_match = lambda label, **kwargs: next(scans)
        result = task._click_match_until_gone(
            {
                "name": "fish_card_click",
                "recognition_source": "ocr",
                "match_any": ["fish_card_result"],
                "max_clicks": 5,
                "after_click_wait_seconds": 0,
            },
            match,
            "fish_card_click",
        )

        self.assertEqual(result.status, StateStatus.SUCCEEDED)
        self.assertEqual(len(clicks), 2)

    def test_marker_sequence_keeps_fish_card_error_visible_until_rescan_or_explicit_exit(self) -> None:
        task = object.__new__(FarmVehicleTask)
        fish_step = {
            "name": "fish_card_click",
            "action": "click_until_gone",
            "wait_for_rescan_on_failure": True,
        }
        task.vehicle_config = {
            "marker_sequence": {
                "max_steps": None,
                "scan_wait_seconds": 0,
                "recover_dialog_before_scan": False,
                "steps": [fish_step],
            }
        }
        task.overlay = FakeOverlay()
        task.control = FakeControl()
        task._stopped = lambda: False
        task._wait_for_initial_marker_start = lambda config: None
        task._sleep_checked = lambda seconds: None
        task._save_last = lambda label: None
        match = SimpleNamespace(name="fish_card_result", score=0.95)
        selections = iter(([(fish_step, match)], []))
        task._select_marker_steps = lambda *args, **kwargs: next(selections)
        task._execute_marker_step = lambda step, current_match, marker_config: StateResult(
            StateStatus.FAILED,
            message="鱼卡点击失败：连续点击 5 次后“恭喜获得鱼卡”文字仍存在",
        )
        commands = iter(("rescan", "exit"))
        task._wait_for_marker_rescan = lambda phase="idle": next(commands)

        result = task._run_marker_sequence()

        self.assertEqual(result.status, StateStatus.SUCCEEDED)
        error_updates = [item for item in task.overlay.updates if item.get("run_state") == "error"]
        self.assertEqual(len(error_updates), 1)
        self.assertIn("连续点击 5 次", error_updates[0]["log"])

    def test_continuous_marker_sequence_rescans_automatically_after_no_match(self) -> None:
        task = object.__new__(FarmVehicleTask)
        step = {"name": "farm_field_latest", "action": "replay", "match_any": ["watering"]}
        task.vehicle_config = {
            "marker_sequence": {
                "max_steps": 1,
                "scan_wait_seconds": 0,
                "no_match_rescan_seconds": 1.25,
                "steps": [step],
            }
        }
        task.overlay = FakeOverlay()
        task.control = FakeControl()
        task._stopped = lambda: False
        task._wait_for_initial_marker_start = lambda config: None
        task._save_last = lambda label: None
        selections = iter(([], [(step, SimpleNamespace(name="watering", score=0.95))]))
        task._select_marker_steps = lambda *args, **kwargs: next(selections)
        sleeps: list[float] = []
        task._sleep_checked = lambda seconds: sleeps.append(seconds)
        task._execute_marker_step = lambda *args, **kwargs: StateResult(StateStatus.SUCCEEDED, "done")
        task._wait_for_marker_rescan = lambda phase="idle": self.fail("continuous no-match must not wait for F9")

        result = task._run_marker_sequence()

        self.assertEqual(result.status, StateStatus.SUCCEEDED)
        self.assertEqual(sleeps, [1.25])

    def test_processor_failure_does_not_block_remaining_marker_candidates(self) -> None:
        task = object.__new__(FarmVehicleTask)
        processor_step = {
            "name": "processor_latest",
            "action": "processor",
            "skip_on_failure": True,
            "mark_completed_on_failure": True,
        }
        farm_step = {"name": "farm_field_latest", "action": "replay"}
        task.vehicle_config = {
            "marker_sequence": {
                "max_steps": 1,
                "scan_wait_seconds": 0,
                "recover_dialog_before_scan": False,
                "steps": [processor_step, farm_step],
            }
        }
        task.overlay = FakeOverlay()
        task.control = FakeControl()
        task._stopped = lambda: False
        match = SimpleNamespace(name="harvest", score=0.90)
        task._select_marker_steps = lambda *args, **kwargs: [
            (processor_step, match),
            (farm_step, match),
        ]
        executed: list[str] = []

        def execute(step, current_match, marker_config):
            executed.append(step["name"])
            if step["name"] == "processor_latest":
                return StateResult(StateStatus.FAILED, message="processor verification failed")
            return StateResult(StateStatus.SUCCEEDED, message="farm completed")

        task._execute_marker_step = execute
        result = task._run_marker_sequence()
        self.assertEqual(result.status, StateStatus.SUCCEEDED)
        self.assertEqual(executed, ["processor_latest", "farm_field_latest"])


class RecordingWindowSafetyTests(unittest.TestCase):
    def test_input_only_recorder_aborts_when_window_resolution_fails(self) -> None:
        from tools.recording.record_input_actions import InputActionRecorder
        from src.utils.window import WindowNotFoundError

        with tempfile.TemporaryDirectory() as temporary_dir:
            recorder = InputActionRecorder(
                config={"window": {}, "capture": {"use_client_region": True}},
                output_dir=Path(temporary_dir),
                move_sample_ms=1,
                stop_key="f10",
                mark_key="f9",
            )
            with patch(
                "tools.recording.record_input_actions.resolve_target_selector",
                return_value=(SimpleNamespace(), "", None),
            ), patch("tools.recording.record_input_actions.activate_window", return_value=None):
                with self.assertRaisesRegex(WindowNotFoundError, "could not be resolved"):
                    recorder.refresh_window()

    def test_screenshot_recorder_aborts_instead_of_using_full_screen_origin(self) -> None:
        from src.core.capture import Screenshot
        from src.utils.window import WindowNotFoundError
        from tools.recording.record_farm_actions import FarmActionRecorder

        full_screen = Screenshot(
            image=np.zeros((720, 1280, 3), dtype=np.uint8),
            origin=(0, 0),
            region=None,
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            recorder = FarmActionRecorder(
                config={"window": {}, "capture": {"use_client_region": True}},
                output_dir=Path(temporary_dir),
                move_sample_ms=1,
                screenshot_mode="none",
                stop_key="f10",
                mark_key="f9",
            )
            with patch(
                "tools.recording.record_farm_actions.capture_game_client",
                return_value=(full_screen, SimpleNamespace(), "", None, None),
            ):
                with self.assertRaisesRegex(WindowNotFoundError, "could not be resolved"):
                    recorder.refresh_window()


class MarkerStartupTests(unittest.TestCase):
    def _make_task(self, command: str) -> tuple[FarmVehicleTask, list[bool]]:
        task = object.__new__(FarmVehicleTask)
        task.overlay = FakeOverlay()
        captures: list[bool] = []
        task._capture = lambda activate=False: captures.append(bool(activate))
        task._wait_for_marker_rescan = lambda phase="idle": command
        return task, captures

    def test_initial_f9_focuses_game_then_starts_first_scan(self) -> None:
        task, captures = self._make_task("rescan")
        result = task._wait_for_initial_marker_start({"wait_for_initial_f9": True})

        self.assertIsNone(result)
        self.assertEqual(captures, [True])
        self.assertEqual(task.overlay.updates[-1]["run_state"], "running")
        self.assertEqual(task.overlay.updates[-1]["current_step"], "marker_sequence_scan_01")

    def test_explicit_initial_exit_stops_without_scanning(self) -> None:
        task, captures = self._make_task("exit")
        result = task._wait_for_initial_marker_start({"wait_for_initial_f9": True})

        self.assertEqual(captures, [True])
        self.assertIsNotNone(result)
        self.assertEqual(result.status, StateStatus.SUCCEEDED)
        self.assertIn("before first scan", result.message)

    def test_idle_wait_pumps_overlay_events_before_accepting_exit(self) -> None:
        class ExitControl:
            stopped = False
            paused = False

            def consume_overlay_toggle(self) -> bool:
                return False

            def consume_rescan_request(self) -> bool:
                return False

            def consume_exit_request(self) -> bool:
                return True

        task = object.__new__(FarmVehicleTask)
        task.control = ExitControl()
        task.overlay = FakeOverlay()
        self.assertEqual(task._wait_for_marker_rescan(), "exit")
        self.assertGreaterEqual(task.overlay.pump_calls, 1)


if __name__ == "__main__":
    unittest.main()
