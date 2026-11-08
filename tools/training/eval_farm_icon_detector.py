r"""Evaluate a BatterFarm farm icon detector run.

Typical usage:

    & ".\.venv\Scripts\python.exe" tools/training/eval_farm_icon_detector.py

Evaluate a specific run:

    & ".\.venv\Scripts\python.exe" tools/training/eval_farm_icon_detector.py --run models/farm-icons\\runs\\smoke_cpu_49_from_27

The script reads training args/results, summarizes learning-rate and metric
trends, then runs Ultralytics validation on the configured validation split.
It writes an eval_summary.json/eval_summary.md and Ultralytics plots including
confusion_matrix.png.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_PROJECT = Path("models/farm-icons/runs")
DEFAULT_DATA = Path("datasets/farm_icons_v0.4/dataset.yaml")
DEFAULT_OUTPUT_ROOT = Path("logs/evaluations/farm_icons_v0.4")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate a BatterFarm V0.4 farm icon detector.")
    parser.add_argument(
        "--run",
        type=Path,
        default=None,
        help="Training run directory. Defaults to the latest directory under --project.",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=DEFAULT_PROJECT,
        help="Training runs directory used when --run is not provided.",
    )
    parser.add_argument("--model", type=Path, default=None, help="Model path. Defaults to RUN/weights/best.pt.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="YOLO dataset yaml.")
    parser.add_argument("--split", default="val", help="Dataset split to evaluate, usually val or test.")
    parser.add_argument("--imgsz", type=int, default=None, help="Validation image size. Defaults to run args imgsz.")
    parser.add_argument("--batch", type=int, default=1, help="Validation batch size.")
    parser.add_argument("--device", default="cpu", help="Validation device.")
    parser.add_argument("--conf", type=float, default=None, help="Optional confidence threshold for validation.")
    parser.add_argument("--iou", type=float, default=0.45, help="NMS IoU threshold.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Evaluation output directory.")
    return parser.parse_args()


def info(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def parse_scalar(value: str) -> Any:
    text = value.strip()
    lowered = text.lower()
    if lowered in {"null", "none", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if any(char in text for char in [".", "e", "E"]):
            return float(text)
        return int(text)
    except ValueError:
        return text


def load_simple_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    values: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if key and raw_value:
            values[key] = parse_scalar(raw_value)
    return values


def load_results_csv(path: Path) -> list[dict[str, float]]:
    if not path.exists():
        return []
    rows: list[dict[str, float]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            headers = [header.strip() for header in next(reader)]
        except StopIteration:
            return rows
        for raw_row in reader:
            row: dict[str, float] = {}
            for header, value in zip(headers, raw_row):
                text = value.strip()
                if not text:
                    continue
                try:
                    row[header] = float(text)
                except ValueError:
                    continue
            if row:
                rows.append(row)
    return rows


def latest_run(project: Path) -> Path:
    if not project.exists():
        raise FileNotFoundError(f"Project directory not found: {project}")
    candidates = [
        path
        for path in project.iterdir()
        if path.is_dir() and ((path / "args.yaml").exists() or (path / "results.csv").exists())
    ]
    if not candidates:
        raise FileNotFoundError(f"No training runs found under: {project}")

    def run_mtime(path: Path) -> float:
        files = [path / "results.csv", path / "args.yaml", path / "weights" / "best.pt", path / "weights" / "best.onnx"]
        return max((file.stat().st_mtime for file in files if file.exists()), default=path.stat().st_mtime)

    return max(candidates, key=run_mtime)


def resolve_model(run_dir: Path, requested_model: Path | None) -> Path:
    if requested_model is not None:
        return requested_model.resolve()
    best_pt = run_dir / "weights" / "best.pt"
    if best_pt.exists():
        return best_pt.resolve()
    best_onnx = run_dir / "weights" / "best.onnx"
    if best_onnx.exists():
        return best_onnx.resolve()
    raise FileNotFoundError(f"No best.pt or best.onnx found under: {run_dir / 'weights'}")


def metric_at(rows: list[dict[str, float]], key: str, index: int) -> float | None:
    if not rows:
        return None
    value = rows[index].get(key)
    if value is None or math.isnan(value):
        return None
    return value


def best_row(rows: list[dict[str, float]], key: str, *, higher_is_better: bool) -> dict[str, float] | None:
    usable = [row for row in rows if key in row and not math.isnan(row[key])]
    if not usable:
        return None
    selector = max if higher_is_better else min
    return selector(usable, key=lambda row: row[key])


def average(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def window_average(rows: list[dict[str, float]], key: str, start: int, end: int) -> float | None:
    values = [row[key] for row in rows[start:end] if key in row and not math.isnan(row[key])]
    return average(values)


def trend_summary(rows: list[dict[str, float]], key: str, window: int = 5) -> dict[str, Any]:
    if not rows:
        return {}
    first = metric_at(rows, key, 0)
    last = metric_at(rows, key, -1)
    higher_is_better = "loss" not in key.lower()
    best = best_row(rows, key, higher_is_better=higher_is_better)
    result: dict[str, Any] = {
        "first": first,
        "last": last,
        "best": best.get(key) if best else None,
        "best_epoch": int(best.get("epoch", 0)) if best and "epoch" in best else None,
        "higher_is_better": higher_is_better,
    }
    if first is not None and last is not None:
        result["total_change"] = last - first
    if len(rows) >= window * 2:
        prev_avg = window_average(rows, key, -window * 2, -window)
        last_avg = window_average(rows, key, -window, len(rows))
        result["previous_window_avg"] = prev_avg
        result["last_window_avg"] = last_avg
        if prev_avg is not None and last_avg is not None:
            result["last_window_change"] = last_avg - prev_avg
    return result


def learning_rate_summary(rows: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    lr_keys = sorted(key for key in rows[0].keys() if key.startswith("lr/")) if rows else []
    summary: dict[str, dict[str, float]] = {}
    for key in lr_keys:
        values = [row[key] for row in rows if key in row and not math.isnan(row[key])]
        if not values:
            continue
        summary[key] = {
            "first": values[0],
            "last": values[-1],
            "min": min(values),
            "max": max(values),
        }
    return summary


def selected_args(args_yaml: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "model",
        "data",
        "epochs",
        "batch",
        "imgsz",
        "device",
        "workers",
        "optimizer",
        "lr0",
        "lrf",
        "momentum",
        "weight_decay",
        "warmup_epochs",
        "warmup_momentum",
        "warmup_bias_lr",
        "cos_lr",
        "close_mosaic",
        "seed",
        "deterministic",
    ]
    return {key: args_yaml.get(key) for key in keys if key in args_yaml}


def training_summary(run_dir: Path) -> dict[str, Any]:
    args_yaml = load_simple_yaml(run_dir / "args.yaml")
    rows = load_results_csv(run_dir / "results.csv")
    metric_keys = [
        "metrics/precision(B)",
        "metrics/recall(B)",
        "metrics/mAP50(B)",
        "metrics/mAP50-95(B)",
        "train/box_loss",
        "train/cls_loss",
        "train/dfl_loss",
        "val/box_loss",
        "val/cls_loss",
        "val/dfl_loss",
    ]
    return {
        "run_dir": str(run_dir),
        "args": selected_args(args_yaml),
        "epochs_recorded": len(rows),
        "last_epoch": int(rows[-1].get("epoch", 0)) if rows else None,
        "learning_rates": learning_rate_summary(rows),
        "metrics": {key: trend_summary(rows, key) for key in metric_keys},
    }


def clean_for_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean_for_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_for_json(item) for item in value]
    if hasattr(value, "item"):
        return clean_for_json(value.item())
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def validation_metrics_dict(metrics: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if hasattr(metrics, "results_dict"):
        result.update(getattr(metrics, "results_dict"))
    for attr in ["fitness", "speed", "save_dir"]:
        if hasattr(metrics, attr):
            result[attr] = getattr(metrics, attr)
    return clean_for_json(result)


def plot_series(rows: list[dict[str, float]], keys: list[str], title: str, y_label: str, output_path: Path) -> str | None:
    available = [key for key in keys if any(key in row for row in rows)]
    if not rows or not available:
        return None

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        info("matplotlib is not installed; skipping training history plots")
        return None

    epochs = [row.get("epoch", index + 1) for index, row in enumerate(rows)]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    plt.figure(figsize=(10, 5.5))
    for key in available:
        values = [row.get(key, math.nan) for row in rows]
        plt.plot(epochs, values, marker="o", linewidth=1.8, markersize=3.0, label=key)
    plt.title(title)
    plt.xlabel("epoch")
    plt.ylabel(y_label)
    plt.grid(True, alpha=0.28)
    plt.legend(loc="best")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    return str(output_path)


def plot_training_history(run_dir: Path, output_dir: Path) -> dict[str, str]:
    rows = load_results_csv(run_dir / "results.csv")
    plots: dict[str, str] = {}
    specs = [
        (
            "learning_rates",
            ["lr/pg0", "lr/pg1", "lr/pg2"],
            "Learning Rate by Epoch",
            "learning rate",
            output_dir / "training_learning_rates.png",
        ),
        (
            "metrics",
            [
                "metrics/precision(B)",
                "metrics/recall(B)",
                "metrics/mAP50(B)",
                "metrics/mAP50-95(B)",
            ],
            "Validation Metrics by Epoch",
            "score",
            output_dir / "training_metrics.png",
        ),
        (
            "losses",
            [
                "train/box_loss",
                "train/cls_loss",
                "train/dfl_loss",
                "val/box_loss",
                "val/cls_loss",
                "val/dfl_loss",
            ],
            "Train and Validation Loss by Epoch",
            "loss",
            output_dir / "training_losses.png",
        ),
    ]
    for name, keys, title, y_label, path in specs:
        plotted = plot_series(rows, keys, title, y_label, path)
        if plotted is not None:
            plots[name] = plotted
            info(f"Wrote plot: {plotted}")
    return plots


def write_report(summary: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "eval_summary.json"
    md_path = output_dir / "eval_summary.md"
    json_path.write_text(json.dumps(clean_for_json(summary), ensure_ascii=False, indent=2), encoding="utf-8")

    train = summary["training"]
    val = summary["validation"]
    args = train.get("args", {})
    lines = [
        "# Farm Icon Detector Evaluation",
        "",
        f"- Run: `{summary['run_dir']}`",
        f"- Model: `{summary['model']}`",
        f"- Data: `{summary['data']}`",
        f"- Split: `{summary['split']}`",
        f"- Image size: `{summary['imgsz']}`",
        f"- Epochs recorded: `{train.get('epochs_recorded')}`",
        f"- Base model: `{args.get('model')}`",
        f"- Batch: `{args.get('batch')}`",
        f"- Device: `{args.get('device')}`",
        "",
        "## Learning Rate",
        "",
    ]
    for key, lr in train.get("learning_rates", {}).items():
        lines.append(
            f"- `{key}` first={lr['first']:.8g}, last={lr['last']:.8g}, min={lr['min']:.8g}, max={lr['max']:.8g}"
        )
    lines.extend(["", "## Metric Trend", ""])
    for key, metric in train.get("metrics", {}).items():
        if not metric:
            continue
        best = metric.get("best")
        last = metric.get("last")
        change = metric.get("last_window_change")
        lines.append(
            f"- `{key}` last={last if last is not None else 'n/a'}, best={best if best is not None else 'n/a'}, "
            f"best_epoch={metric.get('best_epoch')}, last_window_change={change if change is not None else 'n/a'}"
        )
    lines.extend(["", "## Validation", ""])
    for key, value in val.get("metrics", {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## Outputs",
            "",
            f"- Confusion matrix: `{val.get('confusion_matrix')}`",
            f"- Normalized confusion matrix: `{val.get('confusion_matrix_normalized')}`",
            f"- Ultralytics save dir: `{val.get('save_dir')}`",
        ]
    )
    plots = summary.get("training_plots", {})
    if plots:
        lines.extend(["", "## Training History Plots", ""])
        for name, path in plots.items():
            lines.append(f"- `{name}`: `{path}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    info(f"Wrote JSON summary: {json_path}")
    info(f"Wrote Markdown summary: {md_path}")


def main() -> int:
    args = parse_args()
    repo_root = Path.cwd()
    project = (repo_root / args.project).resolve() if not args.project.is_absolute() else args.project.resolve()
    run_dir = args.run.resolve() if args.run else latest_run(project)
    data_yaml = (repo_root / args.data).resolve() if not args.data.is_absolute() else args.data.resolve()
    model_path = resolve_model(run_dir, args.model)
    args_yaml = load_simple_yaml(run_dir / "args.yaml")
    imgsz = args.imgsz or int(args_yaml.get("imgsz") or 640)
    output_dir = args.output_dir or (DEFAULT_OUTPUT_ROOT / f"{run_dir.name}_{args.split}")
    output_dir = (repo_root / output_dir).resolve() if not output_dir.is_absolute() else output_dir.resolve()

    info("BatterFarm V0.4 farm icon detector evaluation")
    info(f"Python: {sys.executable}")
    info(f"Run: {run_dir}")
    info(f"Model: {model_path}")
    info(f"Data: {data_yaml}")
    info(f"Split: {args.split}")
    info(f"Output: {output_dir}")
    info(f"Params: imgsz={imgsz} batch={args.batch} conf={args.conf} iou={args.iou} device={args.device}")

    if not data_yaml.exists():
        raise FileNotFoundError(f"Dataset yaml not found: {data_yaml}")
    if not model_path.exists():
        raise FileNotFoundError(f"Model not found: {model_path}")

    info("Reading training history")
    train_summary = training_summary(run_dir)

    info("Loading Ultralytics model")
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit(
            "Ultralytics is not installed in this Python environment. "
            "Use: python -m pip install -U ultralytics onnx onnxruntime"
        ) from exc

    info("Running validation and generating confusion matrix")
    model = YOLO(str(model_path), task="detect")
    val_kwargs = {
        "data": str(data_yaml),
        "split": args.split,
        "imgsz": imgsz,
        "batch": args.batch,
        "device": args.device,
        "iou": args.iou,
        "plots": True,
        "project": str(output_dir.parent),
        "name": output_dir.name,
        "exist_ok": True,
        "verbose": False,
    }
    if args.conf is not None:
        val_kwargs["conf"] = args.conf
    metrics = model.val(**val_kwargs)
    save_dir = Path(getattr(metrics, "save_dir", output_dir)).resolve()

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "model": str(model_path),
        "data": str(data_yaml),
        "split": args.split,
        "imgsz": imgsz,
        "training": train_summary,
        "training_plots": plot_training_history(run_dir, save_dir),
        "validation": {
            "metrics": validation_metrics_dict(metrics),
            "save_dir": str(save_dir),
            "confusion_matrix": str(save_dir / "confusion_matrix.png"),
            "confusion_matrix_normalized": str(save_dir / "confusion_matrix_normalized.png"),
        },
    }
    write_report(summary, save_dir)
    info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
