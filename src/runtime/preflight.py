from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.core.detector import create_farm_icon_detector
from src.core.input import is_admin
from src.core.ocr import OcrEngine
from src.core.recognition import load_template
from src.core.targets import parse_ocr_target_specs, parse_template_specs
from src.runtime.application import resolve_target_selector
from src.runtime.protocol import resolve_path
from src.runtime.task_registry import TaskRegistry


@dataclass(frozen=True)
class PreflightCheck:
    id: str
    label: str
    ok: bool
    blocking: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_preflight(config: dict[str, Any], root: str | Path) -> list[PreflightCheck]:
    root_path = Path(root)
    checks: list[PreflightCheck] = []
    require_admin = bool(config.get("runtime", {}).get("require_admin", True))
    admin = is_admin()
    checks.append(PreflightCheck("admin", "管理员权限", admin or not require_admin, require_admin, "已获得" if admin else "未获得"))

    try:
        _, title, hwnd = resolve_target_selector(config.get("window", {}))
        checks.append(PreflightCheck("window", "游戏窗口", True, True, f"{title} (HWND {hwnd})"))
    except Exception as exc:
        checks.append(PreflightCheck("window", "游戏窗口", False, True, str(exc)))

    try:
        dependencies = _collect_dependencies(config)
    except Exception as exc:
        dependencies = _TaskDependencies((), (), frozenset(), frozenset(), frozenset(), frozenset(), False)
        dependency_error = str(exc)
    else:
        dependency_error = ""

    model_classes: set[str] = set()
    detection = config.get("detection", {})
    if not isinstance(detection, dict):
        detection = {}
    detection_enabled = bool(detection.get("enabled", False))
    try:
        if dependency_error:
            raise ValueError(dependency_error)
        if detection_enabled:
            detector = create_farm_icon_detector(config, root_path)
            if detector is None:
                raise RuntimeError("检测已启用，但未能创建检测器")
            model_mapping = detector.load_model_classes()
            model_classes = {str(name).strip() for name in model_mapping.values() if str(name).strip()}
            if not model_classes:
                raise ValueError("检测模型没有声明任何类别")
            configured_classes = {
                str(name).strip()
                for name in detection.get("classes", {})
                if str(name).strip()
            }
            missing_configured = sorted(configured_classes - model_classes)
            missing_required = sorted(dependencies.detector_names - model_classes)
            missing = sorted(set(missing_configured + missing_required))
            if missing:
                raise ValueError(f"检测模型缺少类别: {', '.join(missing)}")
            model_path = resolve_path(
                root_path,
                str(detection.get("model_path", "models/farm-icons/best.onnx")),
            )
            model_detail = f"{model_path}；类别: {', '.join(sorted(model_classes))}"
        elif dependencies.detector_names:
            raise ValueError(f"检测已禁用，但任务需要类别: {', '.join(sorted(dependencies.detector_names))}")
        else:
            model_detail = "检测已禁用，当前非餐厅任务不强制使用模型"
        checks.append(PreflightCheck("model", "检测模型", True, True, model_detail))
    except Exception as exc:
        checks.append(PreflightCheck("model", "检测模型", False, True, str(exc)))

    try:
        if dependency_error:
            raise ValueError(dependency_error)
        recording_details = [
            _validate_recording(root_path, config, label, replay)
            for label, replay in dependencies.recordings
        ]
        checks.append(
            PreflightCheck(
                "recordings",
                "任务录像",
                True,
                True,
                f"已验证 {len(recording_details)} 个非餐厅录像: {', '.join(recording_details)}",
            )
        )
    except Exception as exc:
        checks.append(PreflightCheck("recordings", "任务录像", False, True, str(exc)))

    try:
        if dependency_error:
            raise ValueError(dependency_error)
        specs = {spec.name: spec for spec in parse_template_specs(config, root_path)}
        ocr_specs = {spec.name: spec for spec in parse_ocr_target_specs(config)}
        duplicate_names = sorted(set(specs) & set(ocr_specs))
        if duplicate_names:
            raise ValueError(f"识别目标不能同时配置为模板和 OCR: {', '.join(duplicate_names)}")
        required_templates = set(dependencies.template_names)
        required_templates.update(
            name
            for name in dependencies.hybrid_names
            if name not in model_classes and name not in ocr_specs
        )
        for name in sorted(required_templates):
            spec = specs.get(name)
            if spec is None:
                raise ValueError(f"任务引用了未配置的模板: {name}")
            load_template(spec.path)
        detail = "无需固定模板" if not required_templates else f"已加载: {', '.join(sorted(required_templates))}"
        checks.append(PreflightCheck("templates", "任务模板", True, True, detail))
    except Exception as exc:
        checks.append(PreflightCheck("templates", "任务模板", False, True, str(exc)))

    try:
        if dependency_error:
            raise ValueError(dependency_error)
        ocr_specs = {spec.name: spec for spec in parse_ocr_target_specs(config)}
        required_ocr = set(dependencies.ocr_names)
        required_ocr.update(name for name in dependencies.hybrid_names if name in ocr_specs)
        missing_ocr = sorted(required_ocr - set(ocr_specs))
        if missing_ocr:
            raise ValueError(f"任务引用了未配置的 OCR 目标: {', '.join(missing_ocr)}")
        if required_ocr:
            recognition = config.get("recognition", {})
            engine_name = str(recognition.get("ocr_engine", "rapidocr")) if isinstance(recognition, dict) else "rapidocr"
            OcrEngine(engine_name)
            detail = f"已加载: {', '.join(sorted(required_ocr))}"
        else:
            detail = "无需固定区域 OCR"
        checks.append(PreflightCheck("ocr", "文字识别", True, True, detail))
    except Exception as exc:
        checks.append(PreflightCheck("ocr", "文字识别", False, True, str(exc)))

    log_path = resolve_path(root_path, str(config.get("logging", {}).get("file", "logs/batterfarm.log")))
    log_parent = log_path.parent
    checks.append(PreflightCheck("log_dir", "日志目录", log_parent.exists(), False, str(log_parent)))

    try:
        if dependency_error:
            raise ValueError(dependency_error)
        registry = TaskRegistry.from_config(config)
        active_business = [
            item
            for item in registry.business(enabled_only=True)
            if item.action != "restaurant_service"
        ]
        if not active_business:
            raise ValueError("没有启用的非餐厅自动或独立任务")
        checks.append(
            PreflightCheck(
                "tasks",
                "任务配置",
                True,
                True,
                f"{len(active_business)} 个非餐厅业务任务；餐厅资产等待重录，不参与本轮阻断",
            )
        )
    except Exception as exc:
        checks.append(PreflightCheck("tasks", "任务配置", False, True, str(exc)))
    if dependencies.restaurant_configured:
        checks.append(
            PreflightCheck(
                "restaurant_assets",
                "餐厅资产",
                False,
                False,
                "餐厅路线需要重新录制，当前任务已临时禁用；本轮不校验其录像内容，也不阻断其他任务",
            )
        )
    return checks


@dataclass(frozen=True)
class _TaskDependencies:
    recordings: tuple[tuple[str, dict[str, Any]], ...]
    enabled_entries: tuple[dict[str, Any], ...]
    template_names: frozenset[str]
    detector_names: frozenset[str]
    hybrid_names: frozenset[str]
    ocr_names: frozenset[str]
    restaurant_configured: bool


def _collect_dependencies(config: dict[str, Any]) -> _TaskDependencies:
    vehicle = config.get("farm_vehicle", {})
    if not isinstance(vehicle, dict):
        raise ValueError("farm_vehicle 必须是对象")
    marker = vehicle.get("marker_sequence", {})
    if not isinstance(marker, dict):
        raise ValueError("farm_vehicle.marker_sequence 必须是对象")
    raw_steps = marker.get("steps", [])
    raw_independent = vehicle.get("independent_tasks", [])
    if not isinstance(raw_steps, list) or not isinstance(raw_independent, list):
        raise ValueError("marker_sequence.steps 和 independent_tasks 必须是数组")

    entries: list[dict[str, Any]] = []
    restaurant_configured = False
    for raw in [*raw_steps, *raw_independent]:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("action", "replay")).strip().lower() == "restaurant_service":
            restaurant_configured = True
            continue
        if not bool(raw.get("enabled", True)):
            continue
        entries.append(raw)

    recordings: list[tuple[str, dict[str, Any]]] = []
    template_names: set[str] = set()
    detector_names: set[str] = set()
    hybrid_names: set[str] = set()
    ocr_names: set[str] = set()
    recognition_source = str(marker.get("recognition_source", "template")).strip().lower()
    if recognition_source not in {"template", "detector", "hybrid", "ocr"}:
        raise ValueError("marker_sequence.recognition_source 必须是 template、detector、hybrid 或 ocr")

    marker_entry_ids = {id(item) for item in raw_steps if isinstance(item, dict)}
    for entry in entries:
        name = str(entry.get("name", "未命名任务"))
        action = str(entry.get("action", "replay")).strip().lower()
        names = _names(entry.get("match_any", entry.get("templates", [])))
        if id(entry) in marker_entry_ids:
            entry_source = str(entry.get("recognition_source", recognition_source)).strip().lower()
            _collect_recognition_names(
                names,
                entry_source,
                template_names=template_names,
                detector_names=detector_names,
                hybrid_names=hybrid_names,
                ocr_names=ocr_names,
            )
        if action == "replay":
            replay = entry.get("recording_replay", entry.get("replay", {}))
            if not isinstance(replay, dict) or not bool(replay.get("enabled", True)):
                raise ValueError(f"任务 {name} 没有启用的 recording_replay")
            recordings.append((name, replay))
            for wait in _match_wait_entries(replay):
                wait_names = _names(wait.get("match_any", wait.get("templates", [])))
                wait_source = str(wait.get("recognition_source", "template")).strip().lower()
                _collect_recognition_names(
                    wait_names,
                    wait_source,
                    template_names=template_names,
                    detector_names=detector_names,
                    hybrid_names=hybrid_names,
                    ocr_names=ocr_names,
                )
        elif action == "processor":
            processor = vehicle.get("processor", {})
            if not isinstance(processor, dict) or not bool(processor.get("enabled", False)):
                raise ValueError(f"任务 {name} 已启用，但 farm_vehicle.processor 未启用")
            for key in ("move_recording", "collect_recording"):
                replay = processor.get(key, {})
                if not isinstance(replay, dict) or not bool(replay.get("enabled", False)):
                    raise ValueError(f"farm_vehicle.processor.{key} 未启用")
                recordings.append((f"{name}.{key}", replay))
            for key in ("move_verification", "collect_verification", "completion_verification"):
                verification = processor.get(key, {})
                if not isinstance(verification, dict) or not bool(verification.get("enabled", False)):
                    continue
                verification_names = _names(verification.get("match_any", verification.get("templates", [])))
                source = str(verification.get("recognition_source", "template")).strip().lower()
                if source == "detector":
                    detector_names.update(verification_names)
                elif source == "template":
                    template_names.update(verification_names)

    return _TaskDependencies(
        tuple(recordings),
        tuple(entries),
        frozenset(template_names),
        frozenset(detector_names),
        frozenset(hybrid_names),
        frozenset(ocr_names),
        restaurant_configured,
    )


def _names(raw: Any) -> set[str]:
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return {str(value).strip() for value in values if str(value).strip()}


def _match_wait_names(replay: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for wait in _match_wait_entries(replay):
        names.update(_names(wait.get("match_any", wait.get("templates", []))))
    return names


def _match_wait_entries(replay: dict[str, Any]) -> list[dict[str, Any]]:
    waits = replay.get("match_waits", replay.get("marker_waits", []))
    if not isinstance(waits, list):
        raise ValueError("recording_replay.match_waits 必须是数组")
    return [wait for wait in waits if isinstance(wait, dict) and bool(wait.get("enabled", True))]


def _collect_recognition_names(
    names: set[str],
    source: str,
    *,
    template_names: set[str],
    detector_names: set[str],
    hybrid_names: set[str],
    ocr_names: set[str],
) -> None:
    if source == "template":
        template_names.update(names)
    elif source == "detector":
        detector_names.update(names)
    elif source == "hybrid":
        hybrid_names.update(names)
    elif source == "ocr":
        ocr_names.update(names)
    else:
        raise ValueError("recognition_source 必须是 template、detector、hybrid 或 ocr")


def _validate_recording(
    root: Path,
    config: dict[str, Any],
    label: str,
    replay: dict[str, Any],
) -> str:
    raw_path = replay.get("path")
    source = str(replay.get("source", "")).strip()
    if raw_path in (None, "") and source:
        sources = config.get("route", {}).get("source_recordings", {})
        if not isinstance(sources, dict) or source not in sources:
            raise ValueError(f"{label}: 未定义录像 source {source!r}")
        raw_path = sources[source]
    if raw_path in (None, ""):
        raise ValueError(f"{label}: recording_replay 必须提供 path 或 source")
    path = resolve_path(root, str(raw_path))
    if not path.is_file():
        raise FileNotFoundError(f"{label}: 录像不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"{label}: 录像 JSON 无法读取: {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError(f"{label}: 录像 JSON 必须包含 events 数组: {path}")
    if bool(replay.get("include_mouse", False)):
        _validate_mouse_metadata(label, path, data, replay)
    if not _has_usable_events(data["events"], replay) and not _match_wait_names(replay):
        raise ValueError(f"{label}: 录像没有当前回放配置可用的输入事件: {path}")
    return f"{label}={path.name}"


def _has_usable_events(events: list[Any], replay: dict[str, Any]) -> bool:
    ignore_keys = {str(key).strip().lower() for key in replay.get("ignore_keys", [])}
    include_mouse = bool(replay.get("include_mouse", False))
    include_unheld_moves = bool(replay.get("include_unheld_mouse_moves", False))
    start_value = replay.get("start_ms")
    end_value = replay.get("end_ms")
    start_ms = None if start_value in (None, "") else int(start_value)
    end_ms = None if end_value in (None, "") else int(end_value)
    mouse_held = False
    for event in events:
        if not isinstance(event, dict):
            continue
        t_ms = int(event.get("t_ms", 0))
        if start_ms is not None and t_ms < start_ms or end_ms is not None and t_ms > end_ms:
            continue
        event_type = str(event.get("type", ""))
        if event_type in {"key_down", "key_up"}:
            key = str(event.get("key", "")).strip().lower()
            if key and key not in ignore_keys:
                return True
        if event_type == "camera_turn":
            return True
        if not include_mouse:
            continue
        if event_type == "mouse_down":
            mouse_held = True
            return True
        if event_type == "mouse_up":
            return True
        if event_type == "mouse_move" and (mouse_held or include_unheld_moves):
            return True
        if event_type == "mouse_scroll":
            return True
    return False


def _validate_mouse_metadata(
    label: str,
    path: Path,
    data: dict[str, Any],
    replay: dict[str, Any],
) -> None:
    window = data.get("window", {})
    region = window.get("capture_region") if isinstance(window, dict) else None
    if not isinstance(region, list) or len(region) != 4:
        raise ValueError(f"{label}: 含鼠标回放的录像缺少 window.capture_region: {path}")
    width, height = int(region[2]), int(region[3])
    if width <= 0 or height <= 0:
        raise ValueError(f"{label}: 录像客户区尺寸无效: {path}")
    include_unheld = bool(replay.get("include_unheld_mouse_moves", False))
    mouse_held = False
    for index, event in enumerate(data["events"], start=1):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        if event_type == "mouse_down":
            mouse_held = True
            requires_point = True
        elif event_type == "mouse_up":
            requires_point = event.get("client") is not None or event.get("screen") is not None
            mouse_held = False
        elif event_type == "mouse_move":
            requires_point = mouse_held or include_unheld
        elif event_type == "mouse_scroll":
            requires_point = True
        else:
            continue
        if not requires_point:
            continue
        client = event.get("client")
        if not bool(event.get("inside_client", False)) or not isinstance(client, list) or len(client) != 2:
            raise ValueError(f"{label}: 鼠标事件 #{index} 没有安全的客户区坐标: {path}")
        x, y = int(client[0]), int(client[1])
        if not 0 <= x < width or not 0 <= y < height:
            raise ValueError(f"{label}: 鼠标事件 #{index} 超出录制客户区: {path}")
