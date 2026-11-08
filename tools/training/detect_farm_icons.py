r"""Run BatterFarm V0.4 farm icon detection on one image or a folder.

Use the Python 3.12 conda environment created for YOLO CPU training:

    & ".\.venv\Scripts\python.exe" tools/training/detect_farm_icons.py --source datasets\\farm_icons_v0.4\\images\\val

Infer one specific image:

    & ".\.venv\Scripts\python.exe" tools/training/detect_farm_icons.py --source datasets\\farm_icons_v0.4\\images\\val\\0017584_key_down_d.png

Infer a different folder of screenshots:

    & ".\.venv\Scripts\python.exe" tools/training/detect_farm_icons.py --source logs\\recordings\\recording_20260707_124742_screens

Use a different ONNX model:

    & ".\.venv\Scripts\python.exe" tools/training/detect_farm_icons.py --model models/farm-icons\\runs\\smoke_cpu_27\\weights\\best.onnx --source path\\to\\image_or_folder

Outputs:

    logs\\detections\\farm_icons_v0.4\\annotated\\  images with boxes labeled as "class score"
    logs\\detections\\farm_icons_v0.4\\detections.jsonl
    logs\\detections\\farm_icons_v0.4\\detections.tsv

This script only runs offline image detection. It does not click, press keys, or
control the game window.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect BatterFarm V0.4 farm icons with a YOLO ONNX model.")
    parser.add_argument(
        "--model",
        type=Path,
        default=Path("models/farm-icons/best.onnx"),
        help="Path to best.onnx or another Ultralytics-compatible model.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Image file or directory of images to infer.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("logs/detections/farm_icons_v0.4"),
        help="Directory for annotated images and detection reports.",
    )
    parser.add_argument("--imgsz", type=int, default=640, help="Inference image size.")
    parser.add_argument("--batch", type=int, default=1, help="Inference batch size. Keep 1 for static ONNX exports.")
    parser.add_argument("--conf", type=float, default=0.25, help="Confidence threshold.")
    parser.add_argument("--iou", type=float, default=0.45, help="NMS IoU threshold.")
    parser.add_argument("--device", default="cpu", help="Inference device. Use cpu for this Windows environment.")
    parser.add_argument(
        "--no-save-images",
        action="store_true",
        help="Do not save annotated images; still writes JSONL/TSV reports.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="When --source is a directory, infer images recursively.",
    )
    return parser.parse_args()


def info(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def iter_source_images(source: Path, recursive: bool) -> list[Path]:
    if source.is_file():
        if source.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError(f"Source is not a supported image file: {source}")
        return [source]
    if source.is_dir():
        iterator: Iterable[Path] = source.rglob("*") if recursive else source.iterdir()
        return sorted(path for path in iterator if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)
    raise FileNotFoundError(f"Source not found: {source}")


def box_record(result, box, names: dict[int, str]) -> dict[str, object]:
    class_id = int(box.cls[0].item())
    xyxy = [float(value) for value in box.xyxy[0].tolist()]
    x0, y0, x1, y1 = xyxy
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    return {
        "image": str(Path(result.path)),
        "name": names.get(class_id, str(class_id)),
        "class_id": class_id,
        "score": float(box.conf[0].item()),
        "box": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)],
        "center": [round(cx, 3), round(cy, 3)],
    }


def class_color(class_id: int) -> tuple[int, int, int]:
    palette = [
        (0, 170, 255),
        (110, 190, 80),
        (255, 120, 60),
        (220, 90, 220),
        (80, 220, 220),
        (180, 140, 255),
        (90, 220, 140),
    ]
    return palette[class_id % len(palette)]


def read_image(path: Path):
    import cv2
    import numpy as np

    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Could not read image: {path}")
    return image


def write_image(path: Path, image) -> None:
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    extension = path.suffix or ".png"
    ok, encoded = cv2.imencode(extension, image)
    if not ok:
        raise ValueError(f"Could not encode image: {path}")
    encoded.tofile(str(path))


def draw_annotated_image(image_path: Path, records: list[dict[str, object]], output_path: Path) -> None:
    import cv2

    image = read_image(image_path)
    for record in records:
        class_id = int(record["class_id"])
        score = float(record["score"])
        name = str(record["name"])
        x0, y0, x1, y1 = [int(round(float(value))) for value in record["box"]]
        color = class_color(class_id)
        label = f"{name} {score:.2f}"

        cv2.rectangle(image, (x0, y0), (x1, y1), color, 2, cv2.LINE_AA)

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.6
        thickness = 2
        (text_width, text_height), baseline = cv2.getTextSize(label, font, font_scale, thickness)
        label_x0 = max(0, x0)
        label_y0 = max(text_height + baseline + 4, y0)
        label_x1 = min(image.shape[1] - 1, label_x0 + text_width + 8)
        label_y1 = min(image.shape[0] - 1, label_y0 + baseline + 6)

        cv2.rectangle(
            image,
            (label_x0, label_y0 - text_height - baseline - 6),
            (label_x1, label_y1),
            color,
            -1,
        )
        cv2.putText(
            image,
            label,
            (label_x0 + 4, label_y0 - baseline - 3),
            font,
            font_scale,
            (0, 0, 0),
            thickness,
            cv2.LINE_AA,
        )

    write_image(output_path, image)


def write_reports(records: list[dict[str, object]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "detections.jsonl"
    tsv_path = output_dir / "detections.tsv"

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    with tsv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["image", "name", "class_id", "score", "x0", "y0", "x1", "y1", "cx", "cy"])
        for record in records:
            x0, y0, x1, y1 = record["box"]
            cx, cy = record["center"]
            writer.writerow(
                [
                    record["image"],
                    record["name"],
                    record["class_id"],
                    f"{record['score']:.6f}",
                    x0,
                    y0,
                    x1,
                    y1,
                    cx,
                    cy,
                ]
            )

    info(f"Wrote JSONL: {jsonl_path}")
    info(f"Wrote TSV: {tsv_path}")


def main() -> int:
    args = parse_args()
    model_path = args.model.resolve()
    source = args.source.resolve()
    output_dir = args.output_dir.resolve()
    annotated_name = "annotated"

    info("BatterFarm V0.4 farm icon detection")
    info(f"Python: {sys.executable}")
    info(f"Model: {model_path}")
    info(f"Source: {source}")
    info(f"Output: {output_dir}")
    info(f"Params: imgsz={args.imgsz} batch={args.batch} conf={args.conf} iou={args.iou} device={args.device}")

    if not model_path.exists():
        raise FileNotFoundError(f"Model not found: {model_path}")

    images = iter_source_images(source, args.recursive)
    if not images:
        raise ValueError(f"No supported images found in source: {source}")
    info(f"Found {len(images)} image(s)")

    info("Loading Ultralytics model")
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit(
            "Ultralytics is not installed in this Python environment. "
            "Use: python -m pip install -U ultralytics onnxruntime"
        ) from exc

    model = YOLO(str(model_path), task="detect")

    info("Running inference")
    results = []
    for index, image_path in enumerate(images, start=1):
        info(f"Infer {index}/{len(images)}: {image_path.name}")
        image_results = model.predict(
            source=str(image_path),
            imgsz=args.imgsz,
            batch=args.batch,
            conf=args.conf,
            iou=args.iou,
            device=args.device,
            save=False,
            exist_ok=True,
            verbose=False,
        )
        results.extend(image_results)

    names = {int(key): value for key, value in model.names.items()}
    records: list[dict[str, object]] = []
    per_image_counts: list[tuple[str, int]] = []
    annotated_dir = output_dir / annotated_name
    for result in results:
        count = 0
        image_records: list[dict[str, object]] = []
        for box in result.boxes:
            record = box_record(result, box, names)
            records.append(record)
            image_records.append(record)
            count += 1
        per_image_counts.append((str(Path(result.path)), count))
        if not args.no_save_images:
            draw_annotated_image(Path(result.path), image_records, annotated_dir / Path(result.path).name)

    for image_path, count in per_image_counts:
        info(f"{Path(image_path).name}: {count} detection(s)")

    write_reports(records, output_dir)
    if not args.no_save_images:
        info(f"Annotated images: {output_dir / annotated_name}")
    info(f"Total detections: {len(records)}")
    info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
