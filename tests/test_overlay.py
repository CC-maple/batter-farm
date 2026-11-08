from __future__ import annotations

import inspect
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from src.core.overlay import (
    MatchOverlay,
    OverlayController,
    StatusOverlay,
    _fit_overlay_text,
    _position_overlay_window,
    _truncate_display,
)


class FakeCanvas:
    def __init__(self) -> None:
        self.items: dict[int, dict[str, object]] = {}
        self.next_id = 1

    def delete(self, _tag: str) -> None:
        self.items.clear()

    def configure(self, **_options: object) -> None:
        return

    def create_rectangle(self, *coords: float, **options: object) -> int:
        return self._add("rectangle", coords, options)

    def create_text(self, x: float, y: float, **options: object) -> int:
        return self._add("text", (x, y), options)

    def bbox(self, item_id: int) -> tuple[int, int, int, int] | None:
        item = self.items.get(item_id)
        if item is None or item["kind"] != "text":
            return None
        x, y = item["coords"]
        text = str(item["options"].get("text", ""))
        return int(x), int(y), int(x + len(text) * 8), int(y + 18)

    def coords(self, item_id: int, x: float, y: float) -> None:
        self.items[item_id]["coords"] = (x, y)

    def tag_lower(self, _item_id: int, _below_id: int) -> None:
        return

    def _add(self, kind: str, coords: tuple[float, ...], options: dict[str, object]) -> int:
        item_id = self.next_id
        self.next_id += 1
        self.items[item_id] = {"kind": kind, "coords": coords, "options": options}
        return item_id


class FakeRoot:
    def __init__(self, fail_withdraw: bool = False, fail_destroy: bool = False) -> None:
        self.fail_withdraw = fail_withdraw
        self.fail_destroy = fail_destroy
        self.withdraw_calls = 0
        self.destroy_calls = 0
        self.update_calls = 0
        self.deiconify_calls = 0
        self.geometry_calls: list[str] = []

    def withdraw(self) -> None:
        self.withdraw_calls += 1
        if self.fail_withdraw:
            raise RuntimeError("withdraw failed")

    def destroy(self) -> None:
        self.destroy_calls += 1
        if self.fail_destroy:
            raise RuntimeError("destroy failed")

    def update_idletasks(self) -> None:
        return

    def update(self) -> None:
        self.update_calls += 1

    def lift(self) -> None:
        return

    def geometry(self, value: str) -> None:
        self.geometry_calls.append(value)

    def deiconify(self) -> None:
        self.deiconify_calls += 1


@dataclass
class FakeSpec:
    priority: int = 10


@dataclass
class FakeMatch:
    name: str
    score: float
    top_left: tuple[int, int]
    bottom_right: tuple[int, int]
    spec: FakeSpec


class OverlayTests(unittest.TestCase):
    def test_overlay_text_truncation_accounts_for_wide_characters(self) -> None:
        self.assertEqual(_truncate_display("abcdef", 6), "abcdef")
        self.assertEqual(_truncate_display("农田abcdef", 6), "农田ab...")
        self.assertTrue(_fit_overlay_text("a very long marker name", 48, 12).endswith("..."))

    def test_status_overlay_displays_score_as_percentage(self) -> None:
        overlay = StatusOverlay({"enabled": True})
        overlay.state["last_score"] = "0.8864"
        self.assertEqual(overlay._score_display(), "88.6%")
        overlay.state["run_state"] = "failed"
        self.assertEqual(overlay._state_display()[0], "失败")

    def test_status_overlay_localizes_user_facing_values_and_logs(self) -> None:
        overlay = StatusOverlay({"enabled": True})
        overlay.update_status(
            current_step="marker_sequence_scan_02",
            last_marker="watering",
            last_action="mouse left down",
            mouse="left@120,240",
            log="game focused; F9 scan / ESC stop",
        )
        self.assertEqual(overlay.state["current_step"], "第 2 轮标识扫描")
        self.assertEqual(overlay.state["last_marker"], "浇水")
        self.assertEqual(overlay.state["last_action"], "按下鼠标左键")
        self.assertEqual(overlay.state["mouse"], "左键@120,240")
        self.assertEqual(overlay.history[-1], "游戏窗口已激活；F9 开始扫描 / ESC 停止")
        overlay.update_status(current_step="fishpond_latest_fish_card_result_scan_02")
        self.assertEqual(overlay.state["current_step"], "鱼塘任务·恭喜获得鱼卡·第 2 次扫描")
        overlay.add_log("farm_field_latest: mouse left down")
        self.assertEqual(overlay.history[-1], "农田任务：按下鼠标左键")

    def test_visible_status_updates_stream_without_reopening_window(self) -> None:
        overlay = StatusOverlay({"enabled": True})
        overlay.visible = True
        overlay.last_origin = (100, 200)
        with patch.object(overlay, "_render_status") as render_status, patch.object(overlay, "show") as show:
            overlay.update_status(last_action="scan", log="no match")
        render_status.assert_called_once_with()
        show.assert_not_called()

    def test_status_overlay_follows_client_move_without_hide_or_recreate(self) -> None:
        overlay = StatusOverlay(
            {
                "enabled": True,
                "region": [0, 690, 520, 210],
                "base_client_size": [1600, 900],
            }
        )
        root = FakeRoot()
        overlay.root = root
        overlay.canvas = FakeCanvas()
        overlay.visible = True
        overlay.last_origin = (10, 20)
        overlay.last_client_size = (1600, 900)
        overlay.last_geometry = (10, 710, 520, 210)

        with patch.object(overlay, "_apply_owned_position"):
            overlay.bind_client_geometry((110, 220), (1600, 900))

        self.assertEqual(root.geometry_calls, ["520x210+110+910"])
        self.assertEqual(root.withdraw_calls, 0)
        self.assertEqual(root.deiconify_calls, 0)
        self.assertTrue(overlay.visible)

    def test_stream_mode_does_not_hide_status_before_capture(self) -> None:
        status = StatusOverlay({"enabled": True, "hide_before_capture": False})
        root = FakeRoot()
        status.root = root
        status.visible = True
        matches = MatchOverlay({"enabled": False}, parent_enabled=True)
        controller = OverlayController(status=status, matches=matches)

        self.assertFalse(controller.prepare_capture())
        self.assertEqual(root.withdraw_calls, 0)
        self.assertTrue(status.visible)

    def test_first_capture_shows_stream_status_once_after_geometry_bind(self) -> None:
        status = StatusOverlay({"enabled": True, "hide_before_capture": False})
        matches = MatchOverlay({"enabled": False}, parent_enabled=True)
        controller = OverlayController(status=status, matches=matches)
        capture = SimpleNamespace(origin=(140, 80), region=(140, 80, 1600, 900), image=None)

        with patch.object(status, "show") as show:
            controller.finish_capture(capture, restore_status=False)

        self.assertEqual(status.last_origin, (140, 80))
        self.assertEqual(status.last_client_size, (1600, 900))
        show.assert_called_once_with()

    def test_error_cleanup_destroys_status_and_match_windows_even_if_withdraw_fails(self) -> None:
        status = StatusOverlay({"enabled": True})
        status_root = FakeRoot(fail_withdraw=True)
        status.root = status_root
        status.visible = True
        status._disable_after_error()
        self.assertFalse(status.enabled)
        self.assertIsNone(status.root)
        self.assertEqual(status_root.destroy_calls, 1)

        matches = MatchOverlay({"enabled": True}, parent_enabled=True)
        match_root = FakeRoot(fail_withdraw=True)
        matches.root = match_root
        matches.visible = True
        matches._disable_after_error()
        self.assertFalse(matches.enabled)
        self.assertIsNone(matches.root)
        self.assertEqual(match_root.destroy_calls, 1)

    def test_failed_destroy_keeps_overlay_references_for_retry(self) -> None:
        status = StatusOverlay({"enabled": True})
        status_root = FakeRoot(fail_destroy=True)
        status_canvas = FakeCanvas()
        status.root = status_root
        status.canvas = status_canvas

        status.close()

        self.assertIs(status.root, status_root)
        self.assertIs(status.canvas, status_canvas)
        status_root.fail_destroy = False
        status.close()
        self.assertIsNone(status.root)
        self.assertIsNone(status.canvas)
        self.assertEqual(status_root.destroy_calls, 2)

        matches = MatchOverlay({"enabled": True}, parent_enabled=True)
        match_root = FakeRoot(fail_destroy=True)
        match_canvas = FakeCanvas()
        matches.root = match_root
        matches.canvas = match_canvas

        matches.close()

        self.assertIs(matches.root, match_root)
        self.assertIs(matches.canvas, match_canvas)
        match_root.fail_destroy = False
        matches.close()
        self.assertIsNone(matches.root)
        self.assertIsNone(matches.canvas)
        self.assertEqual(match_root.destroy_calls, 2)

    def test_match_overlay_pump_expires_stale_boxes(self) -> None:
        overlay = MatchOverlay({"enabled": True, "pump_interval_ms": 10}, parent_enabled=True)
        root = FakeRoot()
        overlay.root = root
        overlay.canvas = FakeCanvas()
        overlay.visible = True
        overlay.expires_at = 0.0
        overlay.pump(force=True)
        self.assertFalse(overlay.visible)
        self.assertIsNone(overlay.expires_at)
        self.assertGreaterEqual(root.update_calls, 1)
        self.assertEqual(root.withdraw_calls, 1)

    def test_capture_temporarily_hides_and_restores_unexpired_match_boxes(self) -> None:
        status = StatusOverlay({"enabled": False})
        matches = MatchOverlay({"enabled": True}, parent_enabled=True)
        root = FakeRoot()
        matches.root = root
        matches.canvas = FakeCanvas()
        matches.visible = True
        matches.expires_at = 100.0
        matches.last_origin = (140, 80)
        matches.last_client_size = (1600, 900)
        controller = OverlayController(status=status, matches=matches)
        capture = SimpleNamespace(origin=(140, 80), region=(140, 80, 1600, 900), image=None)

        with patch("src.core.overlay.time.monotonic", return_value=50.0), patch.object(
            matches, "_apply_owned_position"
        ):
            controller.prepare_capture()
            self.assertFalse(matches.visible)
            controller.finish_capture(capture, restore_status=False)

        self.assertTrue(matches.visible)
        self.assertEqual(matches.expires_at, 100.0)
        self.assertEqual(root.withdraw_calls, 1)
        self.assertEqual(root.deiconify_calls, 1)

    def test_controller_toggle_hides_both_overlay_layers(self) -> None:
        status = StatusOverlay({"enabled": True})
        matches = MatchOverlay({"enabled": True}, parent_enabled=True)
        matches.visible = True
        controller = OverlayController(status=status, matches=matches)
        self.assertFalse(controller.toggle())
        self.assertFalse(status.show_requested)
        self.assertFalse(matches.show_requested)
        self.assertFalse(matches.visible)

    def test_owner_inactive_hides_overlay_without_changing_user_preference(self) -> None:
        status = StatusOverlay({"enabled": True}, owner_hwnd=42)
        status.root = FakeRoot()
        status.visible = True
        status.show_requested = True
        matches = MatchOverlay({"enabled": True}, parent_enabled=True, owner_hwnd=42)
        matches.root = FakeRoot()
        matches.visible = True
        controller = OverlayController(status=status, matches=matches, owner_hwnd=42)

        with patch("src.core.overlay._owner_is_active", return_value=False):
            controller.pump()

        self.assertFalse(status.visible)
        self.assertTrue(status.show_requested)
        self.assertFalse(matches.visible)

    def test_overlay_uses_owner_relationship_without_global_topmost(self) -> None:
        status_source = inspect.getsource(StatusOverlay)
        match_source = inspect.getsource(MatchOverlay)
        self.assertNotIn('attributes("-topmost", True)', status_source)
        self.assertNotIn('attributes("-topmost", True)', match_source)
        self.assertNotIn("hwnd_topmost", inspect.getsource(_position_overlay_window))

    def test_match_label_has_contrast_background_and_stays_in_client(self) -> None:
        overlay = MatchOverlay(
            {
                "enabled": True,
                "show_mode": "best",
                "label": True,
                "font_size": 13,
                "label_background": "#111827",
            },
            parent_enabled=True,
        )
        overlay.canvas = FakeCanvas()
        overlay.last_client_size = (320, 180)
        match = FakeMatch(
            name="dialog_confirm_button_with_long_name",
            score=0.9123,
            top_left=(280, 2),
            bottom_right=(319, 32),
            spec=FakeSpec(),
        )

        overlay._draw_matches([match], selected=match)

        text_items = [item for item in overlay.canvas.items.values() if item["kind"] == "text"]
        self.assertEqual(len(text_items), 1)
        self.assertIn("91.2%", str(text_items[0]["options"]["text"]))
        backgrounds = [
            item
            for item in overlay.canvas.items.values()
            if item["kind"] == "rectangle" and item["options"].get("fill") == "#111827"
        ]
        self.assertEqual(len(backgrounds), 1)
        left, top, right, bottom = backgrounds[0]["coords"]
        self.assertTrue(0 <= left < right <= 320)
        self.assertTrue(0 <= top < bottom <= 180)


if __name__ == "__main__":
    unittest.main()
