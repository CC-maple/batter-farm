r"""Train and export the BatterFarm V0.4 farm icon detector.

Run with the Python 3.12 conda environment created for CPU smoke tests:

    & ".\.venv\Scripts\python.exe" tools/training/train_farm_icon_detector.py

Optional shorter smoke test:

    & ".\.venv\Scripts\python.exe" tools/training/train_farm_icon_detector.py --epochs 3 --imgsz 640 --batch 2 --name smoke_cpu_27

This script intentionally defaults to CPU because the current Windows setup does
not expose the AMD Radeon GPU through ROCm for local training.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the BatterFarm farm icon YOLO detector.")
    parser.add_argument("--data", default="datasets/farm_icons_v0.4/dataset.yaml", help="YOLO dataset yaml.")
    parser.add_argument("--model", default="yolo11n.pt", help="Base YOLO model.")
    parser.add_argument("--imgsz", type=int, default=640, help="Training image size.")
    parser.add_argument("--epochs", type=int, default=3, help="Epoch count for the smoke run.")
    parser.add_argument("--batch", type=int, default=2, help="Batch size.")
    parser.add_argument("--workers", type=int, default=0, help="Data loader workers. Use 0 on Windows for stability.")
    parser.add_argument("--device", default="cpu", help="Training device. Keep cpu for this Windows environment.")
    parser.add_argument("--project", default="models/farm-icons/runs", help="Ultralytics project directory.")
    parser.add_argument("--name", default="smoke_cpu_27", help="Run name under --project.")
    parser.add_argument("--no-export", action="store_true", help="Skip ONNX export.")
    parser.add_argument("--export-imgsz", type=int, default=None, help="ONNX export image size. Defaults to --imgsz.")
    parser.add_argument("--copy-best", action="store_true", help="Copy best.pt and best.onnx into models/farm-icons.")
    return parser.parse_args()


def info(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def count_files(path: Path, pattern: str) -> int:
    return len(list(path.glob(pattern))) if path.exists() else 0


def count_images(path: Path) -> int:
    image_extensions = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    if not path.exists():
        return 0
    return sum(1 for file_path in path.iterdir() if file_path.is_file() and file_path.suffix.lower() in image_extensions)


def read_dataset_summary(data_yaml: Path) -> dict[str, int]:
    dataset_root = data_yaml.parent
    return {
        "train_images": count_images(dataset_root / "images" / "train"),
        "train_labels": count_files(dataset_root / "labels" / "train", "*.txt"),
        "val_images": count_images(dataset_root / "images" / "val"),
        "val_labels": count_files(dataset_root / "labels" / "val", "*.txt"),
    }


def validate_dataset(data_yaml: Path) -> None:
    if not data_yaml.exists():
        raise FileNotFoundError(f"Dataset yaml not found: {data_yaml}")
    summary = read_dataset_summary(data_yaml)
    info("Dataset summary: " + json.dumps(summary, ensure_ascii=False))
    if summary["train_images"] == 0 or summary["val_images"] == 0:
        raise ValueError("Train/val image folders must not be empty.")
    if summary["train_images"] != summary["train_labels"]:
        raise ValueError("Train image/label counts differ.")
    if summary["val_images"] != summary["val_labels"]:
        raise ValueError("Val image/label counts differ.")


def copy_artifact(source: Path, target: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"Expected artifact not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    info(f"Copied {source} -> {target}")


def main() -> int:
    args = parse_args()
    repo_root = Path.cwd()
    data_yaml = (repo_root / args.data).resolve()
    project = (repo_root / args.project).resolve()
    export_imgsz = args.export_imgsz or args.imgsz

    info("BatterFarm V0.4 farm icon detector training")
    info(f"Python: {sys.executable}")
    info(f"Model: {args.model}")
    info(f"Data: {data_yaml}")
    info(f"Run: {project / args.name}")
    info(f"Params: imgsz={args.imgsz} epochs={args.epochs} batch={args.batch} workers={args.workers} device={args.device}")

    info("Step 1/4: validating dataset")
    validate_dataset(data_yaml)

    info("Step 2/4: importing Ultralytics")
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit(
            "Ultralytics is not installed in this Python environment. "
            "Install it with: python -m pip install -U ultralytics onnx onnxruntime"
        ) from exc

    info("Step 3/4: starting training")
    model = YOLO(args.model)
    results = model.train(
        data=str(data_yaml),
        imgsz=args.imgsz,
        epochs=args.epochs,
        batch=args.batch,
        workers=args.workers,
        device=args.device,
        project=str(project),
        name=args.name,
        exist_ok=True,
    )
    save_dir = Path(getattr(results, "save_dir", project / args.name))
    weights_dir = save_dir / "weights"
    best_pt = weights_dir / "best.pt"
    info(f"Training finished. Run directory: {save_dir}")
    info(f"Best checkpoint: {best_pt}")

    best_onnx: Path | None = None
    if args.no_export:
        info("Step 4/4: ONNX export skipped")
    else:
        info("Step 4/4: exporting best.pt to ONNX")
        export_model = YOLO(str(best_pt))
        exported = export_model.export(format="onnx", imgsz=export_imgsz)
        best_onnx = Path(str(exported))
        info(f"ONNX export finished: {best_onnx}")

    if args.copy_best:
        info("Copying final artifacts into models/farm-icons")
        copy_artifact(best_pt, repo_root / "models" / "farm-icons" / "best.pt")
        if best_onnx is not None:
            copy_artifact(best_onnx, repo_root / "models" / "farm-icons" / "best.onnx")

    info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
