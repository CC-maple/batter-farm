r"""Create X-AnyLabeling rectangle drafts by matching one reference icon.

Run from the BatterFarm repository root with the Python 3.12 environment:

    & ".\.venv\Scripts\python.exe" `
      tools\vision\auto_annotate_reference_icon.py `
      --source logs\recordings\recording_20260731_131635_screens `
      --template datasets\restaurant_restock_v0.1\references\restaurant_restock_icon.png `
      --output datasets\restaurant_restock_v0.1\raw\batch001_agent_draft `
      --review-dir datasets\restaurant_restock_v0.1\review `
      --label restaurant_restock `
      --threshold 0.85 `
      --scale-min 0.105 --scale-max 0.195 --scale-count 13 `
      --roi 0 0 1250 500 `
      --exclude-glob "restaurant_restock_reference_*"

Use --dry-run first to print the planned result without writing files. The tool
never deletes or moves source images. Without --overwrite, it refuses to replace
an existing output image, JSON annotation, preview, or report.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


@dataclass(frozen=True)
class Candidate:
    image: str
    status: str
    score: float
    scale: float
    x0: int
    y0: int
    x1: int
    y1: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate X-AnyLabeling JSON drafts with multi-scale reference-icon matching."
    )
    parser.add_argument("--source", type=Path, required=True, help="Source screenshot directory.")
    parser.add_argument("--template", type=Path, required=True, help="Clean reference-icon crop.")
    parser.add_argument("--output", type=Path, required=True, help="Output directory for copied images and JSON drafts.")
    parser.add_argument("--review-dir", type=Path, required=True, help="Output directory for previews and reports.")
    parser.add_argument("--label", required=True, help="X-AnyLabeling class name.")
    parser.add_argument("--threshold", type=float, default=0.85, help="Minimum normalized match score.")
    parser.add_argument("--scale-min", type=float, default=0.10, help="Minimum template scale.")
    parser.add_argument("--scale-max", type=float, default=0.20, help="Maximum template scale.")
    parser.add_argument("--scale-count", type=int, default=13, help="Number of evenly spaced scales.")
    parser.add_argument(
        "--roi",
        type=int,
        nargs=4,
        metavar=("X0", "Y0", "X1", "Y1"),
        help="Optional search ROI in source-image coordinates.",
    )
    parser.add_argument(
        "--exclude-glob",
        action="append",
        default=[],
        help="Filename glob to exclude; may be repeated.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Scan and report to stdout without writing files.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacement of existing generated files.")
    return parser.parse_args()


def read_image(path: Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray:
    encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    image = cv2.imdecode(encoded, flags)
    if image is None:
        raise ValueError(f"Cannot decode image: {path}")
    return image


def write_png(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError(f"Cannot encode preview: {path}")
    encoded.tofile(path)


def iter_images(source: Path, exclude_globs: list[str]) -> list[Path]:
    if not source.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source}")
    images = []
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in exclude_globs):
            continue
        images.append(path)
    return images


def validate_args(args: argparse.Namespace) -> None:
    if not args.template.is_file():
        raise FileNotFoundError(f"Template not found: {args.template}")
    if not 0.0 <= args.threshold <= 1.0:
        raise ValueError("--threshold must be between 0 and 1.")
    if args.scale_min <= 0 or args.scale_max < args.scale_min:
        raise ValueError("Template scale range is invalid.")
    if args.scale_count < 1:
        raise ValueError("--scale-count must be at least 1.")
    if not args.label.strip():
        raise ValueError("--label cannot be empty.")


def resolve_roi(image: np.ndarray, roi: list[int] | None) -> tuple[np.ndarray, int, int]:
    height, width = image.shape[:2]
    if roi is None:
        return image, 0, 0
    x0, y0, x1, y1 = roi
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError(f"ROI {roi} is outside image size {width}x{height}.")
    return image[y0:y1, x0:x1], x0, y0


def best_match(
    image_gray: np.ndarray,
    template_gray: np.ndarray,
    scales: np.ndarray,
    roi: list[int] | None,
) -> tuple[float, float, tuple[int, int, int, int]]:
    search, offset_x, offset_y = resolve_roi(image_gray, roi)
    best_score = -1.0
    best_scale = 0.0
    best_box = (0, 0, 0, 0)
    for scale in scales:
        template_width = max(8, round(template_gray.shape[1] * float(scale)))
        template_height = max(8, round(template_gray.shape[0] * float(scale)))
        if template_width > search.shape[1] or template_height > search.shape[0]:
            continue
        resized = cv2.resize(
            template_gray,
            (template_width, template_height),
            interpolation=cv2.INTER_AREA,
        )
        result = cv2.matchTemplate(search, resized, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        if score <= best_score:
            continue
        x0 = offset_x + location[0]
        y0 = offset_y + location[1]
        best_score = float(score)
        best_scale = float(scale)
        best_box = (x0, y0, x0 + template_width, y0 + template_height)
    if best_score < 0:
        raise ValueError("No valid template scale fits inside the search area.")
    return best_score, best_scale, best_box


def annotation_payload(image_path: Path, image: np.ndarray, candidate: Candidate, label: str) -> dict[str, object]:
    height, width = image.shape[:2]
    return {
        "version": "3.3.10",
        "flags": {},
        "shapes": [
            {
                "kie_linking": [],
                "label": label,
                "score": round(candidate.score, 6),
                "points": [
                    [float(candidate.x0), float(candidate.y0)],
                    [float(candidate.x1), float(candidate.y0)],
                    [float(candidate.x1), float(candidate.y1)],
                    [float(candidate.x0), float(candidate.y1)],
                ],
                "group_id": None,
                "description": "Agent draft from multi-scale reference-icon matching; requires human review.",
                "difficult": False,
                "shape_type": "rectangle",
                "flags": {"agent_draft": True},
                "attributes": {},
            }
        ],
        "imagePath": image_path.name,
        "imageData": None,
        "imageHeight": height,
        "imageWidth": width,
        "description": "Agent-generated draft; review in X-AnyLabeling before training.",
    }


def annotated_preview(image: np.ndarray, candidate: Candidate, label: str) -> np.ndarray:
    preview = image.copy()
    color = (0, 0, 255)
    cv2.rectangle(preview, (candidate.x0, candidate.y0), (candidate.x1, candidate.y1), color, 2)
    text = f"{label} {candidate.score:.3f}"
    text_y = max(20, candidate.y0 - 8)
    cv2.putText(preview, text, (candidate.x0, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
    return preview


def ensure_writable(path: Path, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing generated file: {path}")


def write_reports(review_dir: Path, candidates: list[Candidate], overwrite: bool) -> None:
    csv_path = review_dir / "candidates.csv"
    json_path = review_dir / "summary.json"
    ensure_writable(csv_path, overwrite)
    ensure_writable(json_path, overwrite)

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(candidates[0]).keys()))
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(asdict(candidate))

    accepted = [candidate for candidate in candidates if candidate.status == "accepted"]
    skipped = [candidate for candidate in candidates if candidate.status == "below_threshold"]
    summary = {
        "total_images": len(candidates),
        "accepted": len(accepted),
        "below_threshold": len(skipped),
        "accepted_score_min": min((candidate.score for candidate in accepted), default=None),
        "accepted_score_max": max((candidate.score for candidate in accepted), default=None),
        "below_threshold_images": [candidate.image for candidate in skipped],
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    validate_args(args)
    image_paths = iter_images(args.source, args.exclude_glob)
    if not image_paths:
        raise ValueError(f"No source images found in {args.source}")

    template_gray = read_image(args.template, cv2.IMREAD_GRAYSCALE)
    scales = np.linspace(args.scale_min, args.scale_max, args.scale_count)
    candidates: list[Candidate] = []
    loaded_images: dict[Path, np.ndarray] = {}

    print(f"Scanning {len(image_paths)} image(s) for label {args.label!r}...")
    for index, image_path in enumerate(image_paths, start=1):
        image = read_image(image_path)
        loaded_images[image_path] = image
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        score, scale, (x0, y0, x1, y1) = best_match(gray, template_gray, scales, args.roi)
        status = "accepted" if score >= args.threshold else "below_threshold"
        candidate = Candidate(image_path.name, status, score, scale, x0, y0, x1, y1)
        candidates.append(candidate)
        print(f"[{index:03d}/{len(image_paths):03d}] {status:15s} score={score:.6f} {image_path.name}")

    accepted = [candidate for candidate in candidates if candidate.status == "accepted"]
    skipped = [candidate for candidate in candidates if candidate.status == "below_threshold"]
    print(f"Accepted: {len(accepted)}; below threshold: {len(skipped)}")
    if args.dry_run:
        return

    preview_dir = args.review_dir / "annotated"
    for directory in (args.output, args.review_dir, preview_dir):
        directory.mkdir(parents=True, exist_ok=True)

    by_name = {path.name: path for path in image_paths}
    for candidate in accepted:
        source_path = by_name[candidate.image]
        image = loaded_images[source_path]
        image_dest = args.output / source_path.name
        json_dest = args.output / f"{source_path.stem}.json"
        preview_dest = preview_dir / f"{source_path.stem}_annotated.png"
        for path in (image_dest, json_dest, preview_dest):
            ensure_writable(path, args.overwrite)
        shutil.copy2(source_path, image_dest)
        payload = annotation_payload(source_path, image, candidate, args.label)
        json_dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_png(preview_dest, annotated_preview(image, candidate, args.label))

    write_reports(args.review_dir, candidates, args.overwrite)
    print(f"Draft annotations: {args.output.resolve()}")
    print(f"Review artifacts: {args.review_dir.resolve()}")


if __name__ == "__main__":
    main()
