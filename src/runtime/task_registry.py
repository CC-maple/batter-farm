from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


TASK_LABELS = {
    "fish_card_click": ("鱼卡领取", "识别奖励文字并领取鱼卡。"),
    "farm_field_latest": ("农田任务", "执行当前农田录制路线。"),
    "fishpond_latest": ("鱼塘任务", "执行当前鱼塘录制路线。"),
    "pasture_latest": ("牧场任务", "执行当前牧场录制路线。"),
    "processor_latest": ("加工台任务", "前往加工台、处理并收集产物。"),
    "restaurant_service_latest": ("餐厅任务", "进入餐厅并完成接待服务流程。"),
    "hot_spring_latest": ("泡温泉", "回放已录制的泡温泉路线；仅支持手动独立执行。"),
}

GUARD_TASK_IDS = {"fish_card_click"}


@dataclass(frozen=True)
class TaskDefinition:
    id: str
    name: str
    description: str
    action: str
    markers: tuple[str, ...]
    enabled: bool
    priority: int
    source: str | None
    independent_supported: bool
    independent_only: bool
    custom: bool
    kind: str = "business"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["markers"] = list(self.markers)
        return data


class TaskRegistry:
    def __init__(self, definitions: list[TaskDefinition]) -> None:
        seen: set[str] = set()
        for definition in definitions:
            if definition.id in seen:
                raise ValueError(f"duplicate task id: {definition.id}")
            seen.add(definition.id)
        self._definitions = definitions
        self._by_id = {item.id: item for item in definitions}

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "TaskRegistry":
        vehicle_config = config.get("farm_vehicle", {})
        marker_config = vehicle_config.get("marker_sequence", {}) if isinstance(vehicle_config, dict) else {}
        raw_steps = marker_config.get("steps", []) if isinstance(marker_config, dict) else []
        raw_independent = vehicle_config.get("independent_tasks", []) if isinstance(vehicle_config, dict) else []
        if not isinstance(raw_steps, list):
            raw_steps = []
        if not isinstance(raw_independent, list):
            raw_independent = []
        entries = (
            [(raw, False, "farm_vehicle.marker_sequence.steps") for raw in raw_steps]
            + [(raw, True, "farm_vehicle.independent_tasks") for raw in raw_independent]
        )
        definitions: list[TaskDefinition] = []
        task_sections: dict[str, str] = {}
        for index, (raw, independent_only_default, section) in enumerate(entries, start=1):
            if not isinstance(raw, dict):
                continue
            task_id = str(raw.get("name", f"task_{index}")).strip()
            if not task_id:
                continue
            previous_section = task_sections.get(task_id)
            if previous_section is not None:
                raise ValueError(
                    f"duplicate task id {task_id!r} in {previous_section} and {section}"
                )
            task_sections[task_id] = section
            action = str(raw.get("action", "replay")).strip().lower()
            raw_markers = raw.get("match_any", raw.get("templates", []))
            if isinstance(raw_markers, str):
                markers = (raw_markers,)
            else:
                markers = tuple(str(value) for value in raw_markers or [] if str(value).strip())
            default_name, default_description = TASK_LABELS.get(
                task_id,
                (task_id.replace("_", " ").title(), f"执行 {task_id} 对应的任务路线。"),
            )
            replay = raw.get("recording_replay", raw.get("replay", {}))
            source = None
            if isinstance(replay, dict):
                source_value = replay.get("source", raw.get("source"))
                if source_value in (None, ""):
                    source_value = replay.get("path")
                source = None if source_value in (None, "") else str(source_value)
            definitions.append(
                TaskDefinition(
                    id=task_id,
                    name=str(raw.get("display_name", default_name)),
                    description=str(raw.get("description", default_description)),
                    action=action,
                    markers=markers,
                    enabled=bool(raw.get("enabled", True)),
                    priority=int(raw.get("priority", 0)),
                    source=source,
                    independent_supported=action not in {"observe", "observe_only", "log_only", "click_match", "click_until_gone"},
                    independent_only=bool(raw.get("independent_only", independent_only_default)),
                    custom=bool(raw.get("ui_custom", False)),
                    kind="guard" if task_id in GUARD_TASK_IDS or bool(raw.get("internal_guard", False)) else "business",
                )
            )
        return cls(definitions)

    def all(self, *, enabled_only: bool = False) -> list[TaskDefinition]:
        if not enabled_only:
            return list(self._definitions)
        return [item for item in self._definitions if item.enabled]

    def business(self, *, enabled_only: bool = False) -> list[TaskDefinition]:
        return [item for item in self.all(enabled_only=enabled_only) if item.kind == "business"]

    def get(self, task_id: str) -> TaskDefinition:
        try:
            return self._by_id[task_id]
        except KeyError as exc:
            raise KeyError(f"unknown task_id: {task_id}") from exc

    def as_dicts(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.all(enabled_only=enabled_only)]

    def business_dicts(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.business(enabled_only=enabled_only)]
