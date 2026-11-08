from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE_RELATIVE = Path("assets/recordings/recording_20260710_110404.json")
MOVE_OUTPUT_RELATIVE = Path("assets/recordings/recording_20260710_110404_processor_move.json")
COLLECT_OUTPUT_RELATIVE = Path("assets/recordings/recording_20260710_110404_processor_collect.json")
MOVE_END_MS = 29611
COLLECT_START_MS = 29612
SOURCE_DURATION_MS = 84887

REQUIRED_METADATA = (
    "version",
    "recording_mode",
    "window",
    "screen_size",
    "move_sample_ms",
    "stop_key",
    "mark_key",
)


def load_recording(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("source recording must be an object with an events list")
    missing = [name for name in REQUIRED_METADATA if name not in data]
    if missing:
        raise ValueError(f"source recording is missing metadata: {missing}")
    if int(data.get("duration_ms", -1)) != SOURCE_DURATION_MS:
        raise ValueError(f"source duration_ms must be {SOURCE_DURATION_MS}")
    return data


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def event_counter(events: list[dict[str, Any]]) -> Counter[tuple[str, str | None]]:
    return Counter((str(event.get("type", "")), event.get("button")) for event in events)


def held_state(events: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    keys: set[str] = set()
    buttons: set[str] = set()
    for event in events:
        event_type = str(event.get("type", ""))
        if event_type == "key_down":
            keys.add(str(event.get("key", "")))
        elif event_type == "key_up":
            keys.discard(str(event.get("key", "")))
        elif event_type == "mouse_down":
            buttons.add(str(event.get("button", "left")))
        elif event_type == "mouse_up":
            buttons.discard(str(event.get("button", "left")))
    return keys, buttons


def _base_payload(source: dict[str, Any]) -> dict[str, Any]:
    payload = {name: copy.deepcopy(source[name]) for name in REQUIRED_METADATA}
    for optional_name in ("started_at", "screenshot_mode"):
        if optional_name in source:
            payload[optional_name] = copy.deepcopy(source[optional_name])
    payload["source_recording"] = SOURCE_RELATIVE.as_posix()
    return payload


def split_processor_recording(source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source_events = [copy.deepcopy(event) for event in source["events"]]
    move_source_events = [event for event in source_events if int(event.get("t_ms", 0)) <= MOVE_END_MS]
    collect_source_events = [event for event in source_events if int(event.get("t_ms", 0)) >= COLLECT_START_MS]

    expected_move = Counter(
        {
            ("session_start", None): 1,
            ("key_down", None): 13,
            ("key_up", None): 13,
            ("mouse_move", None): 75,
            ("mouse_down", "right"): 1,
            ("mouse_up", "right"): 1,
        }
    )
    expected_collect = Counter(
        {
            ("mouse_move", None): 1,
            ("key_down", None): 21,
            ("key_up", None): 21,
            ("mouse_down", "right"): 13,
            ("mouse_up", "right"): 13,
            ("session_stop", None): 1,
        }
    )
    if event_counter(move_source_events) != expected_move or len(move_source_events) != 104:
        raise ValueError("processor_move source event counts do not match the confirmed boundary")
    if event_counter(collect_source_events) != expected_collect or len(collect_source_events) != 70:
        raise ValueError("processor_collect source event counts do not match the confirmed boundary")

    move_keys, move_buttons = held_state(move_source_events)
    if move_keys or move_buttons:
        raise ValueError(f"split boundary has held input: keys={sorted(move_keys)} buttons={sorted(move_buttons)}")

    for event in move_source_events:
        if event.get("type") == "session_start":
            event["t_ms"] = 0
    move_events = move_source_events + [
        {"type": "session_stop", "key": "split_boundary", "t_ms": MOVE_END_MS}
    ]

    collect_events: list[dict[str, Any]] = [{"type": "session_start", "t_ms": 0}]
    for event in collect_source_events:
        event["t_ms"] = int(event.get("t_ms", 0)) - COLLECT_START_MS
        if int(event["t_ms"]) < 0:
            raise ValueError("processor_collect produced a negative timestamp")
        collect_events.append(event)
    collect_events.sort(key=lambda event: int(event.get("t_ms", 0)))

    collect_duration_ms = SOURCE_DURATION_MS - COLLECT_START_MS
    # The recorder wrote duration_ms=84887 but its stop callback landed at
    # 84886. Align the preserved stop event to the authoritative duration so
    # each independent file satisfies duration_ms == final event t_ms.
    collect_stop_events = [event for event in collect_events if event.get("type") == "session_stop"]
    if len(collect_stop_events) != 1:
        raise ValueError("processor_collect must contain exactly one source session_stop")
    collect_stop_events[0]["t_ms"] = collect_duration_ms
    collect_events.sort(key=lambda event: int(event.get("t_ms", 0)))

    move_payload = _base_payload(source)
    move_payload.update(
        {
            "session_id": "20260710_110404_processor_move",
            "duration_ms": MOVE_END_MS,
            "source_range_ms": [0, MOVE_END_MS],
            "timestamps_rebased": False,
            "events": move_events,
        }
    )
    collect_payload = _base_payload(source)
    collect_payload.update(
        {
            "session_id": "20260710_110404_processor_collect",
            "duration_ms": collect_duration_ms,
            "source_range_ms": [COLLECT_START_MS, SOURCE_DURATION_MS],
            "timestamps_rebased": True,
            "events": collect_events,
        }
    )
    return move_payload, collect_payload


def validate_output(recording: dict[str, Any]) -> None:
    events = recording["events"]
    timestamps = [int(event.get("t_ms", 0)) for event in events]
    if any(t_ms < 0 for t_ms in timestamps):
        raise ValueError("output contains a negative timestamp")
    if timestamps != sorted(timestamps):
        raise ValueError("output timestamps are not monotonic")
    if not events or int(recording["duration_ms"]) != timestamps[-1]:
        raise ValueError("output duration_ms must equal the final event timestamp")
    keys, buttons = held_state(events)
    if keys or buttons:
        raise ValueError(f"output ends with held input: keys={sorted(keys)} buttons={sorted(buttons)}")


def write_recording(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split the confirmed 2026-07-10 processor recording.")
    parser.add_argument("--source", type=Path, default=SOURCE_RELATIVE)
    parser.add_argument("--move-output", type=Path, default=MOVE_OUTPUT_RELATIVE)
    parser.add_argument("--collect-output", type=Path, default=COLLECT_OUTPUT_RELATIVE)
    return parser.parse_args()


def resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    args = parse_args()
    source_path = resolve_path(args.source)
    move_output = resolve_path(args.move_output)
    collect_output = resolve_path(args.collect_output)
    original_hash = file_sha256(source_path)
    source = load_recording(source_path)
    move_payload, collect_payload = split_processor_recording(source)
    validate_output(move_payload)
    validate_output(collect_payload)
    write_recording(move_output, move_payload)
    write_recording(collect_output, collect_payload)
    if file_sha256(source_path) != original_hash:
        raise RuntimeError("source recording changed during split")
    print(f"processor_move={move_output} events={len(move_payload['events'])}")
    print(f"processor_collect={collect_output} events={len(collect_payload['events'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
