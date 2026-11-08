"""Train and export the BatterFarm V0.4 farm icon detector on WSL/Linux GPU.

Run from WSL after activating the ROCm PyTorch environment:

    cd /usr/yolo/batterfarm
    source ~/venvs/batterfarm-yolo-rocm/bin/activate
    python train_farm_icon_detector_wsl.py

Continue from a specific checkpoint:

    python train_farm_icon_detector_wsl.py \
      --data dataset.yaml \
      --model best.pt \
      --epochs 50 \
      --imgsz 960 \
      --batch 8 \
      --device 0 \
      --copy-best

This script is intentionally separate from train_farm_icon_detector.py. The
Windows script keeps CPU-safe defaults; this WSL script defaults to the ROCm GPU
visible to PyTorch as CUDA device 0.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
DEFAULT_DATA = Path("dataset.yaml")
DEFAULT_MODEL = Path("best.pt")
DEFAULT_PROJECT = Path("runs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the BatterFarm farm icon YOLO detector on WSL/ROCm GPU.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="WSL/Linux YOLO dataset yaml.")
    parser.add_argument("--model", default=str(DEFAULT_MODEL), help="Base YOLO model or previous best.pt checkpoint.")
    parser.add_argument("--imgsz", type=int, default=960, help="Training image size. 960 preserves more small-icon detail.")
    parser.add_argument("--epochs", type=int, default=50, help="Epoch count for WSL GPU training.")
    parser.add_argument("--batch", type=int, default=8, help="Batch size. Increase if VRAM allows.")
    parser.add_argument("--workers", type=int, default=2, help="Data loader workers under WSL/Linux.")
    parser.add_argument("--device", default="0", help="GPU device. Use 0 or cuda:0 for the AMD ROCm GPU.")
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT, help="Ultralytics project directory.")
    parser.add_argument("--name", default="gpu_wsl_49_from_27", help="Run name under --project.")
    parser.add_argument("--no-export", action="store_true", help="Skip ONNX export.")
    parser.add_argument("--export-imgsz", type=int, default=None, help="ONNX export image size. Defaults to --imgsz.")
    parser.add_argument("--copy-best", action="store_true", help="Copy best.pt and best.onnx into the current training directory.")
    parser.add_argument(
        "--amp",
        action="store_true",
        help="Enable AMP. Default is disabled because ROCm AMP checks may fail on this setup.",
    )
    return parser.parse_args()


def info(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def ensure_rocm_environment() -> None:
    os.environ.setdefault("HSA_ENABLE_DXG_DETECTION", "1")
    os.environ.setdefault("ROCM_PATH", "/opt/rocm-7.2.0")


def count_files(path: Path, pattern: str) -> int:
    return len(list(path.glob(pattern))) if path.exists() else 0


def count_images(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for file_path in path.iterdir() if file_path.is_file() and file_path.suffix.lower() in IMAGE_EXTENSIONS)


def parse_scalar(value: str) -> Any:
    text = value.strip().strip("'\"")
    lowered = text.lower()
    if lowered in {"null", "none", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        return text


def load_simple_dataset_yaml(path: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    names: dict[int, str] = {}
    in_names = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        stripped = raw_line.strip()
        if stripped == "names:":
            in_names = True
            continue
        if ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if in_names and key.isdigit():
            names[int(key)] = str(parse_scalar(value))
        else:
            in_names = False
            values[key] = parse_scalar(value)
    if names:
        values["names"] = names
    return values


def resolve_dataset_root(data_yaml: Path) -> Path:
    data = load_simple_dataset_yaml(data_yaml)
    raw_path = data.get("path")
    if raw_path:
        root = Path(str(raw_path)).expanduser()
        if not root.is_absolute():
            root = data_yaml.parent / root
        return root.resolve()
    return data_yaml.parent.resolve()


def read_dataset_summary(data_yaml: Path) -> dict[str, int]:
    dataset_root = resolve_dataset_root(data_yaml)
    return {
        "train_images": count_images(dataset_root / "images" / "train"),
        "train_labels": count_files(dataset_root / "labels" / "train", "*.txt"),
        "val_images": count_images(dataset_root / "images" / "val"),
        "val_labels": count_files(dataset_root / "labels" / "val", "*.txt"),
    }


def validate_dataset(data_yaml: Path) -> None:
    if not data_yaml.exists():
        raise FileNotFoundError(f"Dataset yaml not found: {data_yaml}")
    dataset_root = resolve_dataset_root(data_yaml)
    summary = read_dataset_summary(data_yaml)
    info(f"Dataset root: {dataset_root}")
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
    # WSL /mnt/c can reject chmod/utime metadata updates from shutil.copy2().
    # Copy file contents only; metadata is not important for model artifacts.
    shutil.copyfile(source, target)
    info(f"Copied {source} -> {target}")


def is_windows_mount_path(path: Path) -> bool:
    parts = path.resolve().parts
    return len(parts) >= 3 and parts[1] == "mnt" and len(parts[2]) == 1 and parts[2].isalpha()


def log_torch_environment() -> None:
    info("Step 2/5: importing PyTorch and checking ROCm GPU")
    info(f"HSA_ENABLE_DXG_DETECTION: {os.environ.get('HSA_ENABLE_DXG_DETECTION')}")
    info(f"ROCM_PATH: {os.environ.get('ROCM_PATH')}")
    try:
        import torch
    except ImportError as exc:
        raise SystemExit("PyTorch is not installed in this WSL environment.") from exc

    info(f"Torch: {torch.__version__}")
    info(f"Torch HIP: {getattr(torch.version, 'hip', None)}")
    info(f"torch.cuda.is_available(): {torch.cuda.is_available()}")
    info(f"torch.cuda.device_count(): {torch.cuda.device_count()}")
    if torch.cuda.is_available() and torch.cuda.device_count() > 0:
        info(f"GPU 0: {torch.cuda.get_device_name(0)}")
    else:
        raise SystemExit(
            "No ROCm GPU is visible to PyTorch. Check /dev/dxg, ROCDXG, ROCm libraries, "
            "and HSA_ENABLE_DXG_DETECTION=1."
        )


def main() -> int:
    ensure_rocm_environment()
    args = parse_args()
    repo_root = Path.cwd()
    data_yaml = (repo_root / args.data).resolve() if not args.data.is_absolute() else args.data.resolve()
    project = (repo_root / args.project).resolve() if not args.project.is_absolute() else args.project.resolve()
    if is_windows_mount_path(project):
        raise SystemExit(f"Refusing to write training output to Windows mount: {project}")
    export_imgsz = args.export_imgsz or args.imgsz

    info("BatterFarm V0.4 farm icon detector WSL GPU training")
    info(f"Python: {sys.executable}")
    info(f"Platform: os.name={os.name} sys.platform={sys.platform}")
    info(f"Model: {args.model}")
    info(f"Data: {data_yaml}")
    info(f"Run: {project / args.name}")
    info(
        "Params: "
        f"imgsz={args.imgsz} epochs={args.epochs} batch={args.batch} "
        f"workers={args.workers} device={args.device} amp={args.amp}"
    )

    info("Step 1/5: validating WSL dataset")
    validate_dataset(data_yaml)

    log_torch_environment()

    info("Step 3/5: importing Ultralytics")
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit(
            "Ultralytics is not installed in this WSL environment. "
            "Use: python -m pip install ultralytics onnx onnxruntime"
        ) from exc

    info("Step 4/5: starting GPU training")
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
        amp=args.amp,
    )
    save_dir = Path(getattr(results, "save_dir", project / args.name))
    weights_dir = save_dir / "weights"
    best_pt = weights_dir / "best.pt"
    info(f"Training finished. Run directory: {save_dir}")
    info(f"Best checkpoint: {best_pt}")

    best_onnx: Path | None = None
    if args.no_export:
        info("Step 5/5: ONNX export skipped")
    else:
        info("Step 5/5: exporting best.pt to ONNX")
        export_model = YOLO(str(best_pt))
        exported = export_model.export(format="onnx", imgsz=export_imgsz, device=args.device)
        best_onnx = Path(str(exported))
        info(f"ONNX export finished: {best_onnx}")

    if args.copy_best:
        info("Copying final artifacts into the current training directory")
        copy_artifact(best_pt, repo_root / "best.pt")
        if best_onnx is not None:
            copy_artifact(best_onnx, repo_root / "best.onnx")

    info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
