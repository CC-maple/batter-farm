from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml


USER_TASKS_VERSION = 1


def default_user_tasks_path(config_path: str | Path) -> Path:
    path = Path(config_path).resolve()
    return path.with_name(f"{path.stem}.user_tasks.yaml")


def merge_user_task_steps(config: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a runtime config with user replay tasks appended as normal marker steps."""

    result = copy.deepcopy(config)
    marker = result.setdefault("farm_vehicle", {}).setdefault("marker_sequence", {})
    raw_steps = marker.get("steps", [])
    base_steps = [
        copy.deepcopy(item)
        for item in raw_steps
        if isinstance(item, dict) and not bool(item.get("ui_custom", False))
    ]
    base_steps.extend(copy.deepcopy(item) for item in steps if isinstance(item, dict))
    marker["steps"] = base_steps
    return result


class UserTaskStore:
    """Persist UI-created replay tasks without rewriting the main config.yaml."""

    def __init__(self, path: str | Path, root: str | Path) -> None:
        self.path = Path(path).resolve()
        self.root = Path(root).resolve()

    def load_steps(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"用户任务文件必须是 YAML 对象：{self.path}")
        tasks = raw.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError(f"用户任务文件 tasks 必须是列表：{self.path}")
        return [self._validate_stored_step(item) for item in tasks]

    def add_replay_task(self, display_name: str, recording_path: str | Path) -> dict[str, Any]:
        name = " ".join(str(display_name).split())
        if not name:
            raise ValueError("任务名称不能为空")

        route_path = Path(recording_path).expanduser().resolve()
        self._validate_route_file(route_path)
        steps = self.load_steps()
        if any(str(item.get("display_name", "")).casefold() == name.casefold() for item in steps):
            raise ValueError(f"已经存在同名任务：{name}")

        stored_path = self._portable_path(route_path)
        step = {
            "name": f"user_{uuid4().hex[:12]}",
            "display_name": name,
            "description": f"回放用户路线：{route_path.name}",
            "action": "replay",
            "match_any": [],
            "enabled": True,
            "priority": 0,
            "ui_custom": True,
            "capture_after_action": False,
            "recording_replay": {
                "enabled": True,
                "path": stored_path,
                "include_mouse": True,
                "include_unheld_mouse_moves": True,
                "ignore_keys": [],
                "speed": 1.0,
                "max_initial_wait_seconds": 1.5,
            },
        }
        steps.append(step)
        self._write_steps(steps)
        return copy.deepcopy(step)

    def _validate_stored_step(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("用户任务条目必须是 YAML 对象")
        step = copy.deepcopy(raw)
        task_id = str(step.get("name", "")).strip()
        display_name = " ".join(str(step.get("display_name", "")).split())
        replay = step.get("recording_replay", {})
        route_path = replay.get("path") if isinstance(replay, dict) else None
        if not task_id.startswith("user_") or not display_name or not route_path:
            raise ValueError(f"用户任务条目缺少有效 ID、名称或路线：{task_id or '<unknown>'}")
        step["ui_custom"] = True
        return step

    def _validate_route_file(self, path: Path) -> None:
        if not path.is_file() or path.suffix.lower() != ".json":
            raise ValueError(f"回放路线必须是存在的 JSON 文件：{path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取回放路线：{exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ValueError("回放路线 JSON 缺少 events 列表")
        if payload.get("recording_type") == "camera_turn":
            raise ValueError("F5/F6 视角录制不能注册为任务路线，请选择普通键鼠路线 JSON")

    def _portable_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def _write_steps(self, steps: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": USER_TASKS_VERSION, "tasks": steps}
        self.path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
