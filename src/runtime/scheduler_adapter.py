from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.runtime.watchers import StableMatchWatcher


@dataclass(frozen=True)
class SchedulerSettings:
    enabled: bool = False
    mode: str = "legacy"
    idle_scan_interval_seconds: float = 1.0
    max_pending_events: int = 256
    stale_result_ms: int = 1500
    capture_max_in_flight: int = 1
    action_max_in_flight: int = 1
    continuous_no_match_policy: str = "reschedule"
    single_no_match_policy: str = "finish"


@dataclass(frozen=True)
class FishCardWatcherSettings:
    enabled: bool = False
    scope: str = "fishpond_latest"
    interval_seconds: float = 2.0
    initial_timeout_seconds: float = 60.0
    post_route_grace_seconds: float = 5.0
    max_clicks: int = 5
    after_click_wait_seconds: float = 0.5
    absent_confirm_frames: int = 2
    formal_threshold: float = 0.90
    candidate_threshold: float = 0.90
    candidate_window: int = 3
    candidate_hits: int = 2
    timeout_policy: str = "continue_with_warning"
    persistent_policy: str = "wait_for_user"

    def create_watcher(self) -> StableMatchWatcher:
        return StableMatchWatcher(
            "fish_card_result",
            formal_threshold=self.formal_threshold,
            candidate_threshold=self.candidate_threshold,
            candidate_window=self.candidate_window,
            candidate_hits=self.candidate_hits,
            absent_confirm_frames=self.absent_confirm_frames,
            max_actions=self.max_clicks,
        )


@dataclass(frozen=True)
class SchedulerDefinitions:
    scheduler: SchedulerSettings
    fish_card: FishCardWatcherSettings
    business_task_ids: tuple[str, ...]
    guard_ids: tuple[str, ...]


class SchedulerAdapter:
    """Build scheduler definitions from the current production config.

    Legacy marker and recording entries remain untouched. This adapter is the
    single migration boundary until unified mode has completed live validation.
    """

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> SchedulerDefinitions:
        raw_scheduler = config.get("scheduler", {})
        if not isinstance(raw_scheduler, dict):
            raw_scheduler = {}
        raw_capture = raw_scheduler.get("capture", {})
        raw_action = raw_scheduler.get("action", {})
        raw_continuous = raw_scheduler.get("continuous", {})
        raw_single = raw_scheduler.get("single", {})
        for name, value in (
            ("capture", raw_capture),
            ("action", raw_action),
            ("continuous", raw_continuous),
            ("single", raw_single),
        ):
            if not isinstance(value, dict):
                raise ValueError(f"scheduler.{name} must be an object")

        scheduler = SchedulerSettings(
            enabled=bool(raw_scheduler.get("enabled", False)),
            mode=str(raw_scheduler.get("mode", "legacy")).strip().lower(),
            idle_scan_interval_seconds=max(0.1, float(raw_scheduler.get("idle_scan_interval_seconds", 1.0))),
            max_pending_events=max(1, int(raw_scheduler.get("max_pending_events", 256))),
            stale_result_ms=max(1, int(raw_scheduler.get("stale_result_ms", 1500))),
            capture_max_in_flight=max(1, int(raw_capture.get("max_in_flight", 1))),
            action_max_in_flight=max(1, int(raw_action.get("max_in_flight", 1))),
            continuous_no_match_policy=str(raw_continuous.get("no_match_policy", "reschedule")),
            single_no_match_policy=str(raw_single.get("no_match_policy", "finish")),
        )
        if scheduler.mode not in {"legacy", "shadow", "unified"}:
            raise ValueError("scheduler.mode must be legacy, shadow, or unified")
        if scheduler.capture_max_in_flight != 1 or scheduler.action_max_in_flight != 1:
            raise ValueError("BatterFarm currently requires capture/action max_in_flight to be 1")

        raw_watchers = config.get("watchers", {})
        raw_fish = raw_watchers.get("fish_card_result", {}) if isinstance(raw_watchers, dict) else {}
        if not isinstance(raw_fish, dict):
            raise ValueError("watchers.fish_card_result must be an object")
        ocr_target = next(
            (
                item
                for item in config.get("recognition", {}).get("ocr_targets", [])
                if isinstance(item, dict) and item.get("name") == "fish_card_result"
            ),
            {},
        )
        configured_threshold = ocr_target.get("min_score", 0.90)
        formal_threshold = float(raw_fish.get("formal_threshold", configured_threshold))
        fish_card = FishCardWatcherSettings(
            enabled=bool(raw_fish.get("enabled", False)),
            scope=str(raw_fish.get("scope", "fishpond_latest")),
            interval_seconds=max(0.1, float(raw_fish.get("interval_seconds", 2.0))),
            initial_timeout_seconds=max(0.1, float(raw_fish.get("initial_timeout_seconds", 60.0))),
            post_route_grace_seconds=max(0.0, float(raw_fish.get("post_route_grace_seconds", 5.0))),
            max_clicks=max(1, int(raw_fish.get("max_clicks", 5))),
            after_click_wait_seconds=max(0.0, float(raw_fish.get("after_click_wait_seconds", 0.5))),
            absent_confirm_frames=max(1, int(raw_fish.get("absent_confirm_frames", 2))),
            formal_threshold=formal_threshold,
            candidate_threshold=float(raw_fish.get("candidate_threshold", formal_threshold)),
            candidate_window=max(1, int(raw_fish.get("candidate_window", 3))),
            candidate_hits=max(1, int(raw_fish.get("candidate_hits", 2))),
            timeout_policy=str(raw_fish.get("timeout_policy", "continue_with_warning")),
            persistent_policy=str(raw_fish.get("persistent_after_max_clicks_policy", "wait_for_user")),
        )
        fish_card.create_watcher()

        raw_steps = config.get("farm_vehicle", {}).get("marker_sequence", {}).get("steps", [])
        business: list[str] = []
        guards: list[str] = []
        for raw_step in raw_steps if isinstance(raw_steps, list) else []:
            if not isinstance(raw_step, dict):
                continue
            task_id = str(raw_step.get("name", "")).strip()
            if not task_id:
                continue
            if task_id == "fish_card_click" or bool(raw_step.get("internal_guard", False)):
                guards.append(task_id)
            elif not bool(raw_step.get("independent_only", False)):
                business.append(task_id)
        return SchedulerDefinitions(scheduler, fish_card, tuple(business), tuple(guards))
