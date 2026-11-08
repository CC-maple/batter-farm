from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.nav import load_nav_nodes_from_config, match_nav_nodes, select_best_node


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def load_image(path: Path) -> np.ndarray:
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if image is None:
        raise FileNotFoundError(f"Cannot read image: {path}")
    return image


def expand_inputs(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        raw = Path(pattern)
        if raw.exists():
            if raw.is_dir():
                paths.extend(sorted(raw.glob("*.png")))
            else:
                paths.append(raw)
            continue

        glob_pattern = str(raw if raw.is_absolute() else ROOT / raw)
        paths.extend(Path(item) for item in sorted(glob.glob(glob_pattern)))
    return _dedupe(paths)


def analyze_image(path: Path, config: dict[str, Any], top: int) -> dict[str, Any]:
    image = load_image(path)
    nodes = load_nav_nodes_from_config(config, ROOT)
    matches = match_nav_nodes(image, nodes)
    gap = float(config.get("nav", {}).get("node_match", {}).get("min_score_gap", 0.0))
    best = select_best_node(matches, min_score_gap=gap)
    return {
        "image": str(path),
        "best": best.spec.name if best is not None else None,
        "matches": [match.to_dict() for match in matches[:top]],
    }


def print_summary(result: dict[str, Any]) -> None:
    print(f"image={result['image']}")
    print(f"best={result['best']}")
    for match in result["matches"]:
        print(
            "  "
            f"{match['name']}: score={match['score']:.4f} "
            f"hits={match['hits']}/{match['min_hits']} ok={match['ok']}"
        )
        for anchor in match["anchors"]:
            if anchor.get("skipped"):
                print(f"    ~ {anchor['name']} {anchor['message']}")
            elif anchor["matched"]:
                print(
                    "    "
                    f"+ {anchor['name']} raw={anchor['raw_score']:.4f} "
                    f"pos={anchor['position_score']:.4f} center={anchor['center']}"
                )
            else:
                print(f"    - {anchor['name']} {anchor['message']}")


def write_jsonl(results: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for result in results:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    output: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        output.append(path)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze V0.3 visual navigation anchor matches.")
    parser.add_argument("images", nargs="*", help="Image files, directories, or glob patterns to analyze.")
    parser.add_argument("--config", default="configs/config.yaml", help="Config file containing nav.nodes.")
    parser.add_argument("--top", type=int, default=5, help="Number of candidate nodes to print per image.")
    parser.add_argument("--jsonl", help="Optional output JSONL path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    if not config.get("nav", {}).get("nodes"):
        print("No nav.nodes configured yet. Add anchors to config.yaml before analyzing.")
        return 1

    patterns = args.images or ["logs/recordings/*_screens/*mark*.png"]
    images = expand_inputs(patterns)
    if not images:
        print("No images matched.")
        return 1

    results: list[dict[str, Any]] = []
    for image_path in images:
        result = analyze_image(image_path, config, top=max(1, int(args.top)))
        results.append(result)
        print_summary(result)

    if args.jsonl:
        output = Path(args.jsonl)
        if not output.is_absolute():
            output = ROOT / output
        write_jsonl(results, output)
        print(f"jsonl={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
