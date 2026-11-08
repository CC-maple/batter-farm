from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_recording(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_drag(events: list[dict[str, Any]], start_index: int, end_index: int) -> dict[str, Any] | None:
    start = events[start_index]
    end = events[end_index]
    start_client = start.get("client")
    end_client = end.get("client")
    if not start_client or not end_client:
        return None
    moves = [
        event
        for event in events[start_index + 1 : end_index]
        if event.get("type") == "mouse_move" and event.get("client")
    ]
    return {
        "button": start.get("button"),
        "start_t_ms": start.get("t_ms"),
        "end_t_ms": end.get("t_ms"),
        "duration_ms": int(end.get("t_ms", 0)) - int(start.get("t_ms", 0)),
        "start_client": start_client,
        "end_client": end_client,
        "dx": int(end_client[0]) - int(start_client[0]),
        "dy": int(end_client[1]) - int(start_client[1]),
        "move_samples": len(moves),
        "start_screenshot": start.get("screenshot"),
        "end_screenshot": end.get("screenshot"),
    }


def summarize(recording: dict[str, Any]) -> dict[str, Any]:
    events = recording.get("events", [])
    drags: list[dict[str, Any]] = []
    clicks: list[dict[str, Any]] = []
    keys: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    down_stack: dict[str, int] = {}

    for index, event in enumerate(events):
        event_type = event.get("type")
        if event_type == "mouse_down":
            down_stack[str(event.get("button"))] = index
        elif event_type == "mouse_up":
            button = str(event.get("button"))
            start_index = down_stack.pop(button, None)
            if start_index is None:
                continue
            drag = summarize_drag(events, start_index, index)
            if drag is None:
                continue
            if abs(drag["dx"]) <= 3 and abs(drag["dy"]) <= 3 and drag["duration_ms"] < 300:
                clicks.append(drag)
            else:
                drags.append(drag)
        elif event_type in {"key_down", "key_up"}:
            keys.append(
                {
                    "type": event_type,
                    "key": event.get("key"),
                    "t_ms": event.get("t_ms"),
                    "screenshot": event.get("screenshot"),
                }
            )
        elif event_type == "mark":
            marks.append(event)

    return {
        "session_id": recording.get("session_id"),
        "duration_ms": recording.get("duration_ms"),
        "window": recording.get("window"),
        "event_count": len(events),
        "drags": drags,
        "clicks": clicks,
        "keys": keys,
        "marks": marks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize a farm action recording.")
    parser.add_argument("recording", help="Path to logs/recordings/recording_*.json")
    parser.add_argument("--json", action="store_true", help="Print full JSON summary.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = summarize(load_recording(Path(args.recording)))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print(f"session={summary['session_id']} duration_ms={summary['duration_ms']} events={summary['event_count']}")
    print(f"window={summary['window']}")
    print("\nDrags:")
    for item in summary["drags"]:
        print(
            f"- {item['button']} {item['start_client']} -> {item['end_client']} "
            f"dx={item['dx']} dy={item['dy']} duration_ms={item['duration_ms']} samples={item['move_samples']}"
        )
    print("\nClicks:")
    for item in summary["clicks"]:
        print(f"- {item['button']} at {item['start_client']} t_ms={item['start_t_ms']}")
    print("\nKeys:")
    for item in summary["keys"]:
        print(f"- {item['type']} {item['key']} t_ms={item['t_ms']}")
    print("\nMarks:")
    for item in summary["marks"]:
        print(f"- t_ms={item.get('t_ms')} screenshot={item.get('screenshot')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
