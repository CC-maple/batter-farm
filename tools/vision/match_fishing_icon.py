# Usage:
#   python tools/vision/match_fishing_icon.py <screenshot.png>
#   python tools/vision/match_fishing_icon.py <screenshot.png> --output logs/farm_vehicle/fishing_icon_match.png
#
# This scans a screenshot with the current fishing icon template, draws the
# best match box directly on the original image, and writes the annotated
# result to --output. By default it uses:
#   assets/template-matching/fishing_icon_new.png
#   logs/farm_vehicle/fishing_icon_match.png
#
# Example:
#   python tools/vision/match_fishing_icon.py screenshots/fishing.png --output logs/farm_vehicle/fishing_icon_match.png

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.core.recognition import load_template, match_template

DEFAULT_TEMPLATE = ROOT / "assets" / "template-matching" / "fishing_icon_new.png"
DEFAULT_OUTPUT = ROOT / "logs" / "farm_vehicle" / "fishing_icon_match.png"


def read_image(path: Path) -> np.ndarray:
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if image is None:
        raise FileNotFoundError(f"Cannot read image: {path}")
    return image


def write_image(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(path.suffix or ".png", image)
    if not ok:
        raise RuntimeError(f"Cannot encode image: {path}")
    encoded.tofile(str(path))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Match the fishing action icon and save an annotated screenshot.")
    parser.add_argument("image", type=Path, help="Screenshot image to scan.")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="Template image path.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Annotated output image path.")
    parser.add_argument("--threshold", type=float, default=0.78, help="Minimum match score.")
    parser.add_argument("--scales", type=float, nargs="*", default=[1.0], help="Template scales to test.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    screenshot = read_image(args.image)
    template = load_template(args.template)
    match = match_template(screenshot, template, args.threshold, scales=tuple(args.scales))
    if match is None:
        print(f"NO_MATCH threshold={args.threshold:.3f} template={args.template}")
        return 2

    annotated = screenshot.copy()
    left, top = match.top_left
    right, bottom = match.bottom_right
    center_x, center_y = match.center
    cv2.rectangle(annotated, (left, top), (right, bottom), (0, 255, 0), 3)
    cv2.drawMarker(
        annotated,
        (center_x, center_y),
        (0, 255, 255),
        markerType=cv2.MARKER_CROSS,
        markerSize=24,
        thickness=2,
    )
    label = f"auto_fishing {match.score:.4f}"
    label_origin = (left, max(24, top - 10))
    cv2.putText(annotated, label, label_origin, cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(annotated, label, label_origin, cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 0), 2, cv2.LINE_AA)
    write_image(args.output, annotated)
    print(
        "MATCH "
        f"score={match.score:.6f} "
        f"center={match.center} "
        f"top_left={match.top_left} "
        f"bottom_right={match.bottom_right} "
        f"template={args.template} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
