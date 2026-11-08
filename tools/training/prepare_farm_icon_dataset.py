from __future__ import annotations

import argparse
import json
import math
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


@dataclass(frozen=True)
class Box:
    label: str
    class_id: int
    x_center: float
    y_center: float
    width: float
    height: float


@dataclass(frozen=True)
class Sample:
    image_path: Path
    label_path: Path
    width: int
    height: int
    boxes: list[Box]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert X-AnyLabeling/LabelMe JSON rectangles into a YOLO train/val dataset."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("datasets/farm_icons_v0.4/raw/batch001_manual"),
        help="Directory containing images and same-name .json annotation files.",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("datasets/farm_icons_v0.4"),
        help="Dataset root containing images/, labels/, classes.txt, and dataset.yaml.",
    )
    parser.add_argument(
        "--classes",
        type=Path,
        default=Path("datasets/farm_icons_v0.4/classes.txt"),
        help="Class list file, one class per line.",
    )
    parser.add_argument("--val-ratio", type=float, default=0.2, help="Validation split ratio.")
    parser.add_argument("--seed", type=int, default=42, help="Random split seed.")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("datasets/farm_icons_v0.4/exports/prepare_report.json"),
        help="Path for a JSON conversion report.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report only; do not copy images or write labels.",
    )
    return parser.parse_args()


def load_classes(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Class file not found: {path}")
    classes = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not classes:
        raise ValueError(f"Class file is empty: {path}")
    duplicates = sorted({label for label in classes if classes.count(label) > 1})
    if duplicates:
        raise ValueError(f"Duplicate class name(s) in {path}: {duplicates}")
    return classes


def iter_image_paths(source: Path) -> list[Path]:
    if not source.exists():
        raise FileNotFoundError(f"Source directory not found: {source}")
    return sorted(path for path in source.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)


def shape_to_box(shape: dict[str, Any], image_width: int, image_height: int, class_to_id: dict[str, int]) -> Box | None:
    label = str(shape.get("label", "")).strip()
    if not label:
        raise ValueError("Shape is missing label.")
    if label not in class_to_id:
        raise ValueError(f"Unknown label {label!r}; expected one of {sorted(class_to_id)}")

    shape_type = str(shape.get("shape_type", "rectangle")).lower()
    points = shape.get("points")
    if shape_type != "rectangle":
        raise ValueError(f"Only rectangle shapes are supported, got {shape_type!r} for label {label!r}.")
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError(f"Rectangle for label {label!r} must contain at least two points.")

    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    x0 = max(0.0, min(xs))
    y0 = max(0.0, min(ys))
    x1 = min(float(image_width), max(xs))
    y1 = min(float(image_height), max(ys))

    width = x1 - x0
    height = y1 - y0
    if width <= 0 or height <= 0:
        return None

    return Box(
        label=label,
        class_id=class_to_id[label],
        x_center=((x0 + x1) / 2.0) / image_width,
        y_center=((y0 + y1) / 2.0) / image_height,
        width=width / image_width,
        height=height / image_height,
    )


def load_sample(image_path: Path, class_to_id: dict[str, int]) -> Sample:
    label_path = image_path.with_suffix(".json")
    if not label_path.exists():
        raise FileNotFoundError(f"Missing JSON for image: {image_path}")

    data = json.loads(label_path.read_text(encoding="utf-8"))
    image_width = int(data.get("imageWidth") or 0)
    image_height = int(data.get("imageHeight") or 0)
    if image_width <= 0 or image_height <= 0:
        raise ValueError(f"Annotation has invalid image size: {label_path}")

    boxes: list[Box] = []
    for shape in data.get("shapes") or []:
        box = shape_to_box(shape, image_width, image_height, class_to_id)
        if box is not None:
            boxes.append(box)

    if not boxes:
        raise ValueError(f"Annotation has no usable boxes: {label_path}")

    return Sample(
        image_path=image_path,
        label_path=label_path,
        width=image_width,
        height=image_height,
        boxes=boxes,
    )


def split_samples(samples: list[Sample], val_ratio: float, seed: int) -> tuple[list[Sample], list[Sample]]:
    if not 0.0 <= val_ratio < 1.0:
        raise ValueError("--val-ratio must be in [0.0, 1.0).")
    shuffled = list(samples)
    random.Random(seed).shuffle(shuffled)
    val_count = 0 if len(shuffled) < 2 else max(1, math.floor(len(shuffled) * val_ratio))
    val_samples = sorted(shuffled[:val_count], key=lambda sample: sample.image_path.name)
    train_samples = sorted(shuffled[val_count:], key=lambda sample: sample.image_path.name)
    return train_samples, val_samples


def yolo_lines(sample: Sample) -> list[str]:
    lines = []
    for box in sample.boxes:
        lines.append(
            f"{box.class_id} {box.x_center:.6f} {box.y_center:.6f} {box.width:.6f} {box.height:.6f}"
        )
    return lines


def write_split(split: str, samples: list[Sample], dataset: Path, dry_run: bool) -> None:
    images_dir = dataset / "images" / split
    labels_dir = dataset / "labels" / split
    if not dry_run:
        images_dir.mkdir(parents=True, exist_ok=True)
        labels_dir.mkdir(parents=True, exist_ok=True)

    for sample in samples:
        image_dest = images_dir / sample.image_path.name
        label_dest = labels_dir / f"{sample.image_path.stem}.txt"
        if dry_run:
            continue
        shutil.copy2(sample.image_path, image_dest)
        label_dest.write_text("\n".join(yolo_lines(sample)) + "\n", encoding="utf-8")


def build_report(samples: list[Sample], train_samples: list[Sample], val_samples: list[Sample], classes: list[str]) -> dict[str, Any]:
    class_counts = {label: 0 for label in classes}
    for sample in samples:
        for box in sample.boxes:
            class_counts[box.label] += 1
    return {
        "total_images": len(samples),
        "train_images": len(train_samples),
        "val_images": len(val_samples),
        "total_boxes": sum(class_counts.values()),
        "class_counts": class_counts,
        "train_files": [sample.image_path.name for sample in train_samples],
        "val_files": [sample.image_path.name for sample in val_samples],
    }


def main() -> None:
    args = parse_args()
    classes = load_classes(args.classes)
    class_to_id = {label: index for index, label in enumerate(classes)}

    samples = [load_sample(image_path, class_to_id) for image_path in iter_image_paths(args.source)]
    if not samples:
        raise ValueError(f"No annotated images found in {args.source}")

    train_samples, val_samples = split_samples(samples, args.val_ratio, args.seed)
    write_split("train", train_samples, args.dataset, args.dry_run)
    write_split("val", val_samples, args.dataset, args.dry_run)

    report = build_report(samples, train_samples, val_samples, classes)
    if not args.dry_run:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
