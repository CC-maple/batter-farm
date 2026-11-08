from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from pynput import keyboard

from src.core.game_window import capture_game_client
from src.core.hotkey import RunControl
from src.runtime.application import apply_run_profile, resolve_target_selector, run_profile
from src.runtime.protocol import (
    EventEmitter,
    RunMode,
    RunProfile,
    WorkerState,
    decode_command,
    encode_command,
)
from src.runtime.preflight import run_preflight
from src.runtime.scheduler_adapter import SchedulerAdapter
from src.runtime.task_registry import TaskRegistry
from src.runtime.user_tasks import UserTaskStore, merge_user_task_steps
from src.runtime.worker import WorkerRuntime
from src.tasks.farm_vehicle import FarmVehicleTask
from src.utils.window import (
    WindowActivationError,
    WindowBounds,
    WindowNotFoundError,
    WindowSelector,
    activate_window,
)


ROOT = Path(__file__).resolve().parents[1]


class _FakeDetector:
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def load_model_classes(self) -> dict[int, str]:
        return {index: name for index, name in enumerate(sorted(self.names))}


class RuntimeProtocolTests(unittest.TestCase):
    def test_command_round_trip_preserves_unicode(self) -> None:
        encoded = encode_command("start", {"task": "餐厅任务"}, request_id="req-1").decode("utf-8")
        command, payload, request_id = decode_command(encoded)
        self.assertEqual(command, "start")
        self.assertEqual(payload["task"], "餐厅任务")
        self.assertEqual(request_id, "req-1")

    def test_event_emitter_uses_monotonic_sequence_per_run(self) -> None:
        stream = io.StringIO()
        emitter = EventEmitter(stream, run_id="run-1")
        emitter.emit("state", state="running")
        emitter.emit("log", message="ok")
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([row["seq"] for row in rows], [1, 2])
        self.assertTrue(all(row["run_id"] == "run-1" for row in rows))

    def test_worker_stdout_remains_utf8_under_gbk_process_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "logging": {"file": str(temp_path / "worker.log")},
                        "farm_vehicle": {
                            "marker_sequence": {
                                "steps": [
                                    {
                                        "name": "farm_field_latest",
                                        "action": "replay",
                                        "match_any": ["watering", "harvest"],
                                        "recording_replay": {"source": "farm_field_latest"},
                                    }
                                ]
                            }
                        },
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "0"
            environment["PYTHONIOENCODING"] = "gbk"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-s",
                    "-u",
                    "-m",
                    "src.runtime.worker",
                    "--config",
                    str(config_path),
                ],
                cwd=ROOT,
                env=environment,
                input=b'{"command":"list_tasks"}\n{"command":"shutdown"}\n',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
            events = [json.loads(line) for line in completed.stdout.decode("utf-8").splitlines()]
            task_event = next(event for event in events if event["type"] == "task_list")
            self.assertEqual(task_event["payload"]["tasks"][0]["name"], "农田任务")

    def test_independent_profile_requires_task(self) -> None:
        with self.assertRaises(ValueError):
            RunProfile.from_dict({"mode": "independent"})


class RuntimeConfigurationTests(unittest.TestCase):
    def test_realtime_match_boxes_remain_visible_for_three_seconds(self) -> None:
        config = yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["overlay"]["match_boxes"]["duration_ms"], 3000)

    def test_current_config_disables_restaurant_until_route_is_rerecorded(self) -> None:
        config = yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8"))
        restaurant = next(
            step
            for step in config["farm_vehicle"]["marker_sequence"]["steps"]
            if step["name"] == "restaurant_service_latest"
        )
        registry = TaskRegistry.from_config(config)

        self.assertFalse(restaurant["enabled"])
        self.assertFalse(registry.get("restaurant_service_latest").enabled)
        self.assertNotIn(
            "restaurant_service_latest",
            {item.id for item in registry.business(enabled_only=True)},
        )

    def test_automatic_profile_is_noninteractive_single_run_by_default(self) -> None:
        source = {"farm_vehicle": {"marker_sequence": {"wait_for_initial_f9": True, "max_steps": None}}}
        result = apply_run_profile(source, RunProfile(mode=RunMode.AUTOMATIC))
        marker = result["farm_vehicle"]["marker_sequence"]
        self.assertFalse(marker["wait_for_initial_f9"])
        self.assertEqual(marker["max_steps"], 1)
        self.assertTrue(marker["exit_on_no_match"])
        self.assertTrue(source["farm_vehicle"]["marker_sequence"]["wait_for_initial_f9"])

    def test_registry_reads_current_marker_steps(self) -> None:
        config = {
            "farm_vehicle": {
                "independent_tasks": [
                    {
                        "name": "hot_spring_latest",
                        "action": "replay",
                        "recording_replay": {"source": "hot_spring_latest_110602"},
                    }
                ],
                "marker_sequence": {
                    "steps": [
                        {
                            "name": "farm_field_latest",
                            "action": "replay",
                            "match_any": ["farm"],
                            "recording_replay": {"source": "farm_field_latest"},
                        },
                        {"name": "fish_card_click", "action": "click_until_gone", "match_any": "fish_card_result"},
                    ]
                }
            }
        }
        registry = TaskRegistry.from_config(config)
        farm = registry.get("farm_field_latest")
        self.assertEqual(farm.name, "农田任务")
        self.assertTrue(farm.independent_supported)
        self.assertFalse(registry.get("fish_card_click").independent_supported)
        self.assertEqual(registry.get("fish_card_click").kind, "guard")
        hot_spring = registry.get("hot_spring_latest")
        self.assertTrue(hot_spring.independent_supported)
        self.assertTrue(hot_spring.independent_only)
        self.assertEqual(hot_spring.markers, ())
        self.assertEqual([item.id for item in registry.business()], ["farm_field_latest", "hot_spring_latest"])

    def test_registry_rejects_duplicate_ids_across_marker_and_independent_tasks(self) -> None:
        config = {
            "farm_vehicle": {
                "marker_sequence": {
                    "steps": [
                        {
                            "name": "shared_route",
                            "action": "key_script",
                            "match_any": ["farm"],
                        }
                    ]
                },
                "independent_tasks": [
                    {
                        "name": "shared_route",
                        "action": "replay",
                        "recording_replay": {"source": "shared_route"},
                    }
                ],
            }
        }

        with self.assertRaisesRegex(
            ValueError,
            "duplicate task id 'shared_route'.*marker_sequence[.]steps.*independent_tasks",
        ):
            TaskRegistry.from_config(config)

    def test_continuous_profile_uses_automatic_no_match_rescan(self) -> None:
        source = {"scheduler": {"idle_scan_interval_seconds": 1.25}}
        result = apply_run_profile(
            source,
            RunProfile(mode=RunMode.AUTOMATIC, continuous=True, max_tasks=None),
        )
        marker = result["farm_vehicle"]["marker_sequence"]
        self.assertEqual(marker["no_match_rescan_seconds"], 1.25)
        self.assertFalse(marker["exit_on_no_match"])

    def test_hot_spring_independent_route_can_preview_without_a_marker(self) -> None:
        config = yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8"))
        hot_spring = next(
            step
            for step in config["farm_vehicle"]["independent_tasks"]
            if step["name"] == "hot_spring_latest"
        )
        task = FarmVehicleTask.__new__(FarmVehicleTask)
        task.vehicle_config = {"marker_sequence": {"steps": []}, "independent_tasks": [hot_spring]}
        task.runtime_config = {"_dry_run": True}
        task.root = ROOT
        task.event_sink = None
        task.overlay = SimpleNamespace(update_status=Mock())

        exit_code = task.run_independent("hot_spring_latest")

        self.assertEqual(exit_code, 0)

    def test_user_replay_task_is_persisted_and_merged_into_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recording = root / "logs" / "recordings" / "recording_custom.json"
            recording.parent.mkdir(parents=True)
            recording.write_text(
                json.dumps({"recording_mode": "input_only", "events": [{"type": "key_down", "key": "w", "t_ms": 0}]}),
                encoding="utf-8",
            )
            store = UserTaskStore(root / "config.user_tasks.yaml", root)

            step = store.add_replay_task("自定义收集", recording)
            merged = merge_user_task_steps(
                {"farm_vehicle": {"marker_sequence": {"steps": []}}},
                store.load_steps(),
            )
            task = TaskRegistry.from_config(merged).get(step["name"])

            self.assertEqual(task.name, "自定义收集")
            self.assertTrue(task.custom)
            self.assertTrue(task.independent_supported)
            self.assertEqual(task.source, str(Path("logs") / "recordings" / "recording_custom.json"))
            self.assertTrue((root / "config.user_tasks.yaml").is_file())

    def test_camera_turn_json_is_rejected_as_a_custom_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recording = root / "mouse_input.json"
            recording.write_text(
                json.dumps({"recording_type": "camera_turn", "events": []}),
                encoding="utf-8",
            )
            store = UserTaskStore(root / "config.user_tasks.yaml", root)
            with self.assertRaisesRegex(ValueError, "视角录制"):
                store.add_replay_task("错误路线", recording)

    def test_worker_reload_tasks_reads_new_user_task_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            config = {"farm_vehicle": {"marker_sequence": {"steps": []}}}
            config_path.write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            recording = root / "recording_custom.json"
            recording.write_text(json.dumps({"events": []}), encoding="utf-8")
            store = UserTaskStore(root / "config.user_tasks.yaml", root)
            step = store.add_replay_task("即时同步任务", recording)
            stream = io.StringIO()
            runtime = WorkerRuntime(
                config,
                root,
                EventEmitter(stream),
                config_path=config_path,
            )

            runtime.dispatch("reload_tasks", {}, "reload-1")

            event = json.loads(stream.getvalue().splitlines()[-1])
            self.assertEqual(event["type"], "task_list")
            self.assertEqual(event["payload"]["tasks"][0]["id"], step["name"])

    def test_overlay_profile_starts_hidden_but_remains_runtime_toggleable(self) -> None:
        result = apply_run_profile({}, RunProfile(mode=RunMode.REALTIME, overlay=False))
        self.assertTrue(result["overlay"]["enabled"])
        self.assertFalse(result["overlay"]["start_visible"])

    def test_current_config_builds_disabled_fish_card_watcher_shadow_definition(self) -> None:
        config = yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8"))
        definitions = SchedulerAdapter.from_config(config)

        self.assertEqual(definitions.scheduler.mode, "legacy")
        self.assertFalse(definitions.fish_card.enabled)
        self.assertEqual(definitions.fish_card.scope, "fishpond_latest")
        self.assertEqual(definitions.fish_card.absent_confirm_frames, 2)
        self.assertIn("fish_card_click", definitions.guard_ids)
        self.assertNotIn("fish_card_click", definitions.business_task_ids)
        self.assertNotIn("hot_spring_latest", definitions.business_task_ids)

    def test_worker_task_list_hides_internal_fish_card_guard(self) -> None:
        config = {
            "farm_vehicle": {
                "marker_sequence": {
                    "steps": [
                        {"name": "farm_field_latest", "match_any": ["watering"]},
                        {"name": "fish_card_click", "action": "click_until_gone", "match_any": ["fish_card_result"]},
                    ]
                }
            }
        }
        stream = io.StringIO()
        runtime = WorkerRuntime(config, ROOT, EventEmitter(stream))
        try:
            runtime.dispatch("list_tasks", {}, "list-1")
            event = json.loads(stream.getvalue().splitlines()[-1])
            self.assertEqual([item["id"] for item in event["payload"]["tasks"]], ["farm_field_latest"])
        finally:
            runtime.close()

    def test_worker_rejects_unwired_unified_scheduler_mode(self) -> None:
        config = {"scheduler": {"enabled": True, "mode": "unified"}}

        with self.assertRaisesRegex(ValueError, "not connected to the worker"):
            WorkerRuntime(config, ROOT, EventEmitter(io.StringIO()))


class PreflightAssetTests(unittest.TestCase):
    @staticmethod
    def _config(recording_path: Path) -> dict:
        return {
            "runtime": {"require_admin": True},
            "detection": {
                "enabled": True,
                "model_path": "model.onnx",
                "classes": {"watering": {"min_score": 0.6}},
            },
            "recognition": {
                "ocr_targets": [
                    {
                        "name": "fish_card_result",
                        "text": "恭喜获得鱼卡",
                        "min_score": 0.9,
                        "roi": [480, 100, 680, 140],
                        "click": True,
                        "click_point": [1110, 137],
                    }
                ]
            },
            "route": {"source_recordings": {"farm": str(recording_path)}},
            "farm_vehicle": {
                "marker_sequence": {
                    "recognition_source": "hybrid",
                    "steps": [
                        {
                            "name": "farm_field_latest",
                            "enabled": True,
                            "action": "replay",
                            "match_any": ["watering"],
                            "recording_replay": {"source": "farm"},
                        },
                        {
                            "name": "fish_card_click",
                            "enabled": True,
                            "action": "click_until_gone",
                            "recognition_source": "ocr",
                            "match_any": ["fish_card_result"],
                        },
                    ],
                }
            },
        }

    @staticmethod
    def _checks(config: dict, root: Path, *, model_names: set[str]) -> dict[str, object]:
        with patch("src.runtime.preflight.is_admin", return_value=True), patch(
            "src.runtime.preflight.resolve_target_selector",
            return_value=(SimpleNamespace(), "offline", 1),
        ), patch(
            "src.runtime.preflight.create_farm_icon_detector",
            return_value=_FakeDetector(model_names),
        ), patch(
            "src.runtime.preflight.OcrEngine",
            return_value=SimpleNamespace(),
        ), patch("src.runtime.preflight.load_template", return_value=SimpleNamespace()):
            return {check.id: check for check in run_preflight(config, root)}

    def test_preflight_loads_model_classes_recordings_and_referenced_ocr_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recording = root / "farm.json"
            recording.write_text(
                json.dumps({"events": [{"type": "key_down", "key": "w", "t_ms": 0}]}),
                encoding="utf-8",
            )
            checks = self._checks(self._config(recording), root, model_names={"watering"})

        self.assertTrue(checks["model"].ok)
        self.assertTrue(checks["recordings"].ok)
        self.assertTrue(checks["templates"].ok)
        self.assertEqual(checks["templates"].detail, "无需固定模板")
        self.assertTrue(checks["ocr"].ok)
        self.assertIn("fish_card_result", checks["ocr"].detail)

    def test_preflight_blocks_model_with_missing_required_class(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recording = root / "farm.json"
            recording.write_text(
                json.dumps({"events": [{"type": "key_down", "key": "w", "t_ms": 0}]}),
                encoding="utf-8",
            )
            checks = self._checks(self._config(recording), root, model_names={"harvest"})

        self.assertFalse(checks["model"].ok)
        self.assertTrue(checks["model"].blocking)
        self.assertIn("watering", checks["model"].detail)

    def test_preflight_blocks_invalid_or_empty_non_restaurant_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recording = root / "farm.json"
            recording.write_text(json.dumps({"events": []}), encoding="utf-8")
            checks = self._checks(self._config(recording), root, model_names={"watering"})

        self.assertFalse(checks["recordings"].ok)
        self.assertTrue(checks["recordings"].blocking)
        self.assertIn("没有当前回放配置可用的输入事件", checks["recordings"].detail)

    def test_preflight_accepts_camera_only_unified_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recording = root / "farm.json"
            recording.write_text(
                json.dumps(
                    {
                        "recording_mode": "input_only",
                        "camera_control": {"mode": "visible", "settings": {}},
                        "events": [
                            {
                                "type": "camera_turn",
                                "mode": "visible",
                                "direction": "left",
                                "duration_ms": 120,
                                "t_ms": 0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            checks = self._checks(self._config(recording), root, model_names={"watering"})

        self.assertTrue(checks["recordings"].ok)

    def test_preflight_accepts_scroll_and_validates_its_client_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recording = root / "farm.json"
            recording.write_text(
                json.dumps(
                    {
                        "window": {"capture_region": [100, 200, 1600, 900]},
                        "events": [
                            {
                                "type": "mouse_scroll",
                                "delta": [0, -1],
                                "client": [400, 300],
                                "inside_client": True,
                                "t_ms": 0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = self._config(recording)
            config["farm_vehicle"]["marker_sequence"]["steps"][0]["recording_replay"]["include_mouse"] = True
            checks = self._checks(config, root, model_names={"watering"})

        self.assertTrue(checks["recordings"].ok)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recording = root / "farm.json"
            recording.write_text(
                json.dumps(
                    {
                        "window": {"capture_region": [100, 200, 1600, 900]},
                        "events": [
                            {
                                "type": "mouse_scroll",
                                "delta": [0, -1],
                                "client": [1700, 300],
                                "inside_client": True,
                                "t_ms": 0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = self._config(recording)
            config["farm_vehicle"]["marker_sequence"]["steps"][0]["recording_replay"]["include_mouse"] = True
            checks = self._checks(config, root, model_names={"watering"})

        self.assertFalse(checks["recordings"].ok)
        self.assertIn("超出录制客户区", checks["recordings"].detail)

    def test_restaurant_rerecord_warning_is_explicitly_non_blocking(self) -> None:
        config = {
            "detection": {"enabled": True, "classes": {"watering": {}}},
            "farm_vehicle": {
                "marker_sequence": {
                    "recognition_source": "hybrid",
                    "steps": [
                        {
                            "name": "farm_key_script",
                            "action": "key_script",
                            "match_any": ["watering"],
                            "actions": [{"action": "press", "key": "r"}],
                        },
                        {
                            "name": "restaurant_service_latest",
                            "action": "restaurant_service",
                            "match_any": ["farm_header"],
                            "recording_replay": {"path": "missing-and-invalid.json"},
                        },
                    ],
                }
            },
        }
        checks = self._checks(config, ROOT, model_names={"watering"})

        self.assertTrue(checks["recordings"].ok)
        self.assertFalse(checks["restaurant_assets"].ok)
        self.assertFalse(checks["restaurant_assets"].blocking)
        self.assertIn("重新录制", checks["restaurant_assets"].detail)


class GameWindowCaptureSafetyTests(unittest.TestCase):
    def test_unresolved_window_fails_before_screen_capture(self) -> None:
        selector = WindowSelector(title="元梦之星")
        with patch("src.core.game_window.parse_window_selector", return_value=selector), patch(
            "src.core.game_window.resolve_window", return_value=None
        ), patch("src.core.game_window.ScreenCapture") as screen_capture:
            with self.assertRaisesRegex(WindowNotFoundError, "Cannot find window"):
                capture_game_client({}, activate=False)

        screen_capture.assert_not_called()

    def test_passive_capture_uses_resolved_client_area_without_activation(self) -> None:
        selector = WindowSelector(title="元梦之星")
        resolved = SimpleNamespace(hwnd=42, title="元梦之星")
        client_bounds = WindowBounds(left=100, top=200, width=1280, height=720)
        screenshot = object()
        backend = Mock()
        backend.grab.return_value = screenshot

        with patch("src.core.game_window.parse_window_selector", return_value=selector), patch(
            "src.core.game_window.resolve_window", return_value=resolved
        ), patch("src.core.game_window.activate_window") as activate, patch(
            "src.core.game_window.get_client_bounds", return_value=client_bounds
        ) as get_client_bounds, patch(
            "src.core.game_window.ScreenCapture", return_value=backend
        ) as screen_capture:
            result = capture_game_client({}, activate=False, settle_seconds=0)

        self.assertIs(result[0], screenshot)
        self.assertIsNone(result[4])
        activate.assert_not_called()
        get_client_bounds.assert_called_once_with(42)
        screen_capture.assert_called_once_with(region=(100, 200, 1280, 720))
        backend.grab.assert_called_once_with()

    def test_active_capture_activates_then_uses_client_area(self) -> None:
        selector = WindowSelector(title="元梦之星")
        resolved = SimpleNamespace(hwnd=42, title="元梦之星")
        window_bounds = WindowBounds(left=90, top=160, width=1300, height=780)
        client_bounds = WindowBounds(left=100, top=200, width=1280, height=720)
        screenshot = object()
        backend = Mock()
        backend.grab.return_value = screenshot

        with patch("src.core.game_window.parse_window_selector", return_value=selector), patch(
            "src.core.game_window.resolve_window", return_value=resolved
        ), patch(
            "src.core.game_window.activate_window", return_value=window_bounds
        ) as activate, patch(
            "src.core.game_window.get_client_bounds", return_value=client_bounds
        ), patch(
            "src.core.game_window.ScreenCapture", return_value=backend
        ) as screen_capture:
            result = capture_game_client({}, activate=True, settle_seconds=0.25)

        self.assertIs(result[0], screenshot)
        self.assertIs(result[4], window_bounds)
        activate.assert_called_once_with(selector=selector, settle_seconds=0.25)
        screen_capture.assert_called_once_with(region=(100, 200, 1280, 720))

    def test_activation_failure_stops_before_client_lookup_or_capture(self) -> None:
        selector = WindowSelector(title="元梦之星")
        resolved = SimpleNamespace(hwnd=42, title="元梦之星")
        with patch("src.core.game_window.parse_window_selector", return_value=selector), patch(
            "src.core.game_window.resolve_window", return_value=resolved
        ), patch("src.core.game_window.activate_window", return_value=None), patch(
            "src.core.game_window.get_client_bounds"
        ) as get_client_bounds, patch("src.core.game_window.ScreenCapture") as screen_capture:
            with self.assertRaisesRegex(WindowActivationError, "Cannot activate"):
                capture_game_client({}, activate=True, settle_seconds=0)

        get_client_bounds.assert_not_called()
        screen_capture.assert_not_called()

    def test_invalid_client_area_stops_before_screen_capture(self) -> None:
        selector = WindowSelector(title="元梦之星")
        resolved = SimpleNamespace(hwnd=42, title="元梦之星")
        invalid_bounds = WindowBounds(left=100, top=200, width=0, height=720)
        with patch("src.core.game_window.parse_window_selector", return_value=selector), patch(
            "src.core.game_window.resolve_window", return_value=resolved
        ), patch(
            "src.core.game_window.get_client_bounds", return_value=invalid_bounds
        ), patch("src.core.game_window.ScreenCapture") as screen_capture:
            with self.assertRaisesRegex(WindowNotFoundError, "Invalid client area"):
                capture_game_client({}, activate=False)

        screen_capture.assert_not_called()

    def test_passive_non_client_capture_does_not_fall_back_to_full_screen(self) -> None:
        selector = WindowSelector(title="元梦之星")
        resolved = SimpleNamespace(hwnd=42, title="元梦之星")
        with patch("src.core.game_window.parse_window_selector", return_value=selector), patch(
            "src.core.game_window.resolve_window", return_value=resolved
        ), patch("src.core.game_window.ScreenCapture") as screen_capture:
            with self.assertRaisesRegex(WindowNotFoundError, "window bounds"):
                capture_game_client({}, activate=False, use_client_region=False)

        screen_capture.assert_not_called()


class RuntimeWindowActivationTests(unittest.TestCase):
    def test_full_selector_miss_fails_closed_without_title_only_retry(self) -> None:
        selector = WindowSelector(
            title="元梦之星",
            process_name="LetsGoClient-Win64-Shipping.exe",
            class_name="UnrealWindow",
        )
        with patch("src.runtime.application.parse_window_selector", return_value=selector), patch(
            "src.runtime.application.resolve_window",
            side_effect=WindowNotFoundError("configured process or class did not match"),
        ) as resolve:
            with self.assertRaisesRegex(WindowNotFoundError, "process or class"):
                resolve_target_selector({})

        resolve.assert_called_once_with(selector)

    def test_unresolved_hwnd_selector_fails_closed(self) -> None:
        selector = WindowSelector(hwnd=12345)
        with patch("src.runtime.application.parse_window_selector", return_value=selector), patch(
            "src.runtime.application.resolve_window",
            return_value=None,
        ):
            with self.assertRaisesRegex(WindowNotFoundError, "12345"):
                resolve_target_selector({})

    def test_automatic_profile_focuses_target_before_running(self) -> None:
        calls: list[str] = []
        session = SimpleNamespace(
            activate_target_window=lambda: calls.append("focus"),
            task=SimpleNamespace(run=lambda: calls.append("run") or 0),
        )

        result = run_profile(session, RunProfile(mode=RunMode.AUTOMATIC))

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["focus", "run"])

    def test_realtime_profile_focuses_once_before_passive_monitoring(self) -> None:
        session = SimpleNamespace(
            activate_target_window=Mock(),
            task=SimpleNamespace(run_realtime_monitor=Mock(return_value=0)),
        )

        result = run_profile(session, RunProfile(mode=RunMode.REALTIME, continuous=True))

        self.assertEqual(result, 0)
        session.activate_target_window.assert_called_once_with()
        session.task.run_realtime_monitor.assert_called_once_with(continuous=True)

    def test_stopped_profile_does_not_focus_target(self) -> None:
        session = SimpleNamespace(
            control=SimpleNamespace(stopped=True),
            activate_target_window=Mock(),
            task=SimpleNamespace(run=Mock()),
        )

        with self.assertRaisesRegex(KeyboardInterrupt, "before window activation"):
            run_profile(session, RunProfile(mode=RunMode.AUTOMATIC))

        session.activate_target_window.assert_not_called()
        session.task.run.assert_not_called()

    def test_silent_pygetwindow_failure_uses_verified_native_activation(self) -> None:
        window = SimpleNamespace(
            _hWnd=42,
            isMinimized=False,
            activate=Mock(),
            left=10,
            top=20,
            width=1280,
            height=720,
            title="元梦之星",
        )
        with patch("src.utils.window.find_window_by_hwnd", return_value=window), patch(
            "src.utils.window._is_foreground_window", return_value=False
        ), patch(
            "src.utils.window._is_window_minimized", return_value=False
        ), patch("src.utils.window._activate_window_native", return_value=True) as native, patch(
            "src.utils.window.time.sleep"
        ), self.assertLogs(level="INFO") as captured:
            bounds = activate_window(hwnd=42, settle_seconds=0)

        native.assert_called_once_with(42)
        self.assertEqual(bounds.as_region(), (10, 20, 1280, 720))
        self.assertTrue(any("using native activation" in message for message in captured.output))
        self.assertTrue(all(record.levelname == "INFO" for record in captured.records))

    def test_unverified_foreground_activation_stops_the_run(self) -> None:
        window = SimpleNamespace(
            _hWnd=42,
            isMinimized=False,
            activate=Mock(),
            left=10,
            top=20,
            width=1280,
            height=720,
            title="元梦之星",
        )
        with patch("src.utils.window.find_window_by_hwnd", return_value=window), patch(
            "src.utils.window._is_foreground_window", return_value=False
        ), patch(
            "src.utils.window._is_window_minimized", return_value=False
        ), patch("src.utils.window._activate_window_native", return_value=False), patch(
            "src.utils.window._get_foreground_hwnd", return_value=99
        ):
            with self.assertRaisesRegex(WindowActivationError, "expected=42, active=99"):
                activate_window(hwnd=42, settle_seconds=0)


class RunControlInterfaceTests(unittest.TestCase):
    def test_commands_do_not_require_keyboard_events(self) -> None:
        control = RunControl()
        control.request_pause()
        self.assertTrue(control.paused)
        control.request_resume()
        self.assertFalse(control.paused)
        self.assertTrue(control.request_rescan())
        self.assertTrue(control.consume_rescan_request())
        control.request_stop()
        self.assertTrue(control.stopped)

    def test_f10_safely_stops_the_whole_run_when_idle_or_busy(self) -> None:
        control = RunControl()
        control._on_press(keyboard.Key.f10)
        self.assertTrue(control.stopped)
        self.assertIsNone(control.consume_overlay_visibility_request())
        self.assertFalse(control.consume_interrupt_request())

        control.set_busy(True)
        control._on_press(keyboard.Key.f10)
        self.assertTrue(control.stopped)
        self.assertFalse(control.consume_interrupt_request())
        self.assertIsNone(control.consume_overlay_visibility_request())


class WorkerOverlayLifecycleTests(unittest.TestCase):
    def test_start_submit_failure_restores_idle_before_propagating(self) -> None:
        stream = io.StringIO()
        runtime = WorkerRuntime({}, ROOT, EventEmitter(stream))

        try:
            with patch.object(
                runtime.action_executor,
                "submit",
                side_effect=RuntimeError("submit failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "submit failed"):
                    runtime.dispatch(
                        "start",
                        {"profile": {"mode": "automatic"}},
                        "start-submit-failure",
                    )

            events = [json.loads(line) for line in stream.getvalue().splitlines()]
            state_events = [event for event in events if event["type"] == "state"]
            self.assertEqual(
                [event["payload"]["state"] for event in state_events],
                [WorkerState.STARTING.value, WorkerState.IDLE.value],
            )
            self.assertEqual(
                state_events[-1]["payload"]["request_id"],
                "start-submit-failure",
            )
            self.assertFalse(runtime.busy)
            self.assertIsNone(runtime._active_action_id)
            self.assertIsNone(runtime._active_control)
            self.assertEqual(runtime.scheduler_core.state.value, "idle")
        finally:
            runtime.close()

    def test_idle_pause_command_keeps_reusable_control_unpaused(self) -> None:
        stream = io.StringIO()
        control = RunControl()
        session = SimpleNamespace(
            config={},
            control=control,
            input_controller=SimpleNamespace(release_all=Mock()),
            task=SimpleNamespace(close=Mock()),
            close=Mock(),
        )
        runtime = WorkerRuntime({}, ROOT, EventEmitter(stream))
        runtime.session = session

        try:
            runtime.dispatch("pause", {}, "pause-idle")

            self.assertFalse(control.paused)
            event = json.loads(stream.getvalue().splitlines()[-1])
            self.assertEqual(event["type"], "state")
            self.assertEqual(event["payload"]["state"], WorkerState.IDLE.value)
            self.assertEqual(event["payload"]["request_id"], "pause-idle")
        finally:
            runtime.close()

    def test_new_run_clears_pause_left_on_reusable_control(self) -> None:
        stream = io.StringIO()
        config: dict = {}
        profile = RunProfile(mode=RunMode.AUTOMATIC)
        effective_config = apply_run_profile(config, profile)
        control = RunControl()
        control._on_press(keyboard.Key.f8)
        self.assertTrue(control.paused)
        observed_pause_states: list[bool] = []
        session = SimpleNamespace(
            config=effective_config,
            control=control,
            input_controller=SimpleNamespace(release_all=Mock()),
            task=SimpleNamespace(close=Mock()),
            close=Mock(),
        )
        runtime = WorkerRuntime(config, ROOT, EventEmitter(stream))
        runtime.session = session

        def observe_run(current_session, _profile) -> int:
            observed_pause_states.append(current_session.control.paused)
            return 0

        try:
            with patch("src.runtime.worker.run_profile", side_effect=observe_run):
                runtime.dispatch("start", {"profile": profile.to_dict()}, "start-after-idle-f8")
                self.assertTrue(runtime.action_executor.wait_idle(1))
                runtime.dispatch("list_tasks", {}, "completion-barrier")

            self.assertEqual(observed_pause_states, [False])
            self.assertFalse(control.paused)
        finally:
            runtime.close()

    def test_action_operation_closes_overlay_before_returning_outcome(self) -> None:
        stream = io.StringIO()
        config: dict = {}
        close_threads: list[int] = []
        control = RunControl()
        session = SimpleNamespace(
            config=config,
            control=control,
            input_controller=SimpleNamespace(release_all=Mock()),
            task=SimpleNamespace(close=lambda: close_threads.append(threading.get_ident())),
            close=Mock(),
        )
        runtime = WorkerRuntime(config, ROOT, EventEmitter(stream))
        runtime.session = session

        try:
            with patch("src.runtime.worker.run_profile", return_value=0):
                current_thread = threading.get_ident()
                outcome = runtime._run_action(config, RunProfile(mode=RunMode.AUTOMATIC), control)

            self.assertEqual(close_threads, [current_thread])
            session.input_controller.release_all.assert_called_once_with()
            self.assertEqual(outcome["exit_code"], 0)
            self.assertEqual(outcome["state"], "completed")
            self.assertNotIn("completed", [json.loads(line)["type"] for line in stream.getvalue().splitlines()])
        finally:
            runtime.close()

    def test_keyboard_stop_returns_clean_outcome_130(self) -> None:
        stream = io.StringIO()
        config: dict = {}
        control = RunControl()
        session = SimpleNamespace(
            config=config,
            control=control,
            input_controller=SimpleNamespace(release_all=Mock()),
            task=SimpleNamespace(close=Mock()),
            close=Mock(),
        )
        runtime = WorkerRuntime(config, ROOT, EventEmitter(stream))
        runtime.session = session

        try:
            with patch("src.runtime.worker.run_profile", side_effect=KeyboardInterrupt):
                outcome = runtime._run_action(config, RunProfile(mode=RunMode.AUTOMATIC), control)

            self.assertEqual(outcome["exit_code"], 130)
            self.assertEqual(outcome["state"], "completed")
            session.task.close.assert_called_once_with()
        finally:
            runtime.close()

    def test_completed_event_waits_for_cleanup_and_executor_release(self) -> None:
        stream = io.StringIO()
        config: dict = {}
        profile = RunProfile(mode=RunMode.AUTOMATIC)
        effective_config = apply_run_profile(config, profile)
        control = RunControl()
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()

        def close_task() -> None:
            cleanup_started.set()
            release_cleanup.wait(1)

        session = SimpleNamespace(
            config=effective_config,
            control=control,
            input_controller=SimpleNamespace(release_all=Mock()),
            task=SimpleNamespace(close=close_task),
            close=Mock(),
        )
        runtime = WorkerRuntime(config, ROOT, EventEmitter(stream))
        runtime.session = session

        try:
            with patch("src.runtime.worker.run_profile", return_value=0):
                runtime.dispatch("start", {"profile": profile.to_dict()}, "start-1")
                self.assertTrue(cleanup_started.wait(1))
                event_types = [json.loads(line)["type"] for line in stream.getvalue().splitlines()]
                self.assertNotIn("completed", event_types)
                self.assertTrue(runtime.busy)

                release_cleanup.set()
                self.assertTrue(runtime.action_executor.wait_idle(1))
                runtime.dispatch("list_tasks", {}, "barrier")

            events = [json.loads(line) for line in stream.getvalue().splitlines()]
            completed = next(event for event in events if event["type"] == "completed")
            self.assertEqual(completed["payload"]["exit_code"], 0)
            self.assertFalse(runtime.busy)
            self.assertFalse(runtime.action_executor.active)
        finally:
            release_cleanup.set()
            runtime.close()

    def test_stop_during_session_creation_prevents_profile_start(self) -> None:
        stream = io.StringIO()
        entered_creation = threading.Event()
        release_creation = threading.Event()
        created: dict[str, object] = {}

        def create_session(config, _root, **kwargs):
            control = kwargs["control"]
            session = SimpleNamespace(
                config=config,
                control=control,
                input_controller=SimpleNamespace(release_all=Mock()),
                task=SimpleNamespace(close=Mock()),
                close=Mock(),
            )
            created["control"] = control
            created["session"] = session
            entered_creation.set()
            release_creation.wait(1)
            return session

        runtime = WorkerRuntime({}, ROOT, EventEmitter(stream))
        run = Mock(return_value=0)
        try:
            with patch("src.runtime.worker.create_task_session", side_effect=create_session), patch(
                "src.runtime.worker.run_profile",
                run,
            ):
                runtime.dispatch("start", {"profile": {"mode": "automatic"}}, "start-1")
                self.assertTrue(entered_creation.wait(1))
                runtime.dispatch("stop", {}, "stop-1")

                self.assertTrue(created["control"].stopped)
                self.assertEqual(runtime.scheduler_core.state.value, "stopping")
                release_creation.set()
                self.assertTrue(runtime.action_executor.wait_idle(1))
                runtime.dispatch("list_tasks", {}, "barrier")

            run.assert_not_called()
            events = [json.loads(line) for line in stream.getvalue().splitlines()]
            completed = next(event for event in events if event["type"] == "completed")
            self.assertEqual(completed["payload"]["exit_code"], 130)
            self.assertFalse(runtime.busy)
        finally:
            release_creation.set()
            runtime.close()


if __name__ == "__main__":
    unittest.main()
