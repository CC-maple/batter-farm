from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from src.runtime.application import create_task_session, resolve_target_selector


ROOT = Path(__file__).resolve().parent


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path

    with config_path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def setup_logging(config: dict[str, Any]) -> None:
    logging_config = config.get("logging", {})
    level_name = str(logging_config.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    log_path = ROOT / str(logging_config.get("file", "logs/batterfarm.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def run_farm_vehicle(config: dict[str, Any]) -> int:
    session = create_task_session(config, ROOT)
    try:
        return session.task.run()
    finally:
        session.close()


def apply_runtime_overrides(args: argparse.Namespace, config: dict[str, Any]) -> None:
    if bool(getattr(args, "overlay", False)):
        config.setdefault("overlay", {})["enabled"] = True


def apply_detection_overrides(args: argparse.Namespace, config: dict[str, Any]) -> None:
    detection_config = config.setdefault("detection", {})
    detector_enabled = getattr(args, "detector_enabled", None)
    if detector_enabled is not None:
        detection_config["enabled"] = detector_enabled == "true"

    detector_model = getattr(args, "detector_model", None)
    if detector_model:
        detection_config["model_path"] = str(detector_model)

    detector_score_threshold = getattr(args, "detector_score_threshold", None)
    if detector_score_threshold is not None:
        if not 0.0 <= detector_score_threshold <= 1.0:
            raise ValueError("--detector-conf must be between 0 and 1.")
        detection_config["score_threshold"] = detector_score_threshold

    detector_nms_threshold = getattr(args, "detector_nms_threshold", None)
    if detector_nms_threshold is not None:
        if not 0.0 <= detector_nms_threshold <= 1.0:
            raise ValueError("--detector-iou must be between 0 and 1.")
        detection_config["nms_threshold"] = detector_nms_threshold

    detector_imgsz = getattr(args, "detector_imgsz", None)
    if detector_imgsz is not None:
        if detector_imgsz <= 0:
            raise ValueError("--detector-imgsz must be positive.")
        detection_config["input_size"] = detector_imgsz

    detector_device = getattr(args, "detector_device", None)
    if detector_device:
        detection_config["device"] = detector_device


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BatterFarm farm automation runner.")
    parser.add_argument("--config", default="configs/config.yaml", help="Path to config yaml.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview V0.4 marker recognition and selected steps without dispatching any input.",
    )
    parser.add_argument("--overlay", action="store_true", help="Enable the farm vehicle status overlay for this run.")
    parser.add_argument(
        "--detector-enabled",
        choices=["true", "false"],
        help="Temporarily enable or disable detection without editing config.yaml.",
    )
    parser.add_argument("--detector-model", type=Path, help="Temporarily override detection.model_path.")
    parser.add_argument(
        "--detector-conf",
        "--detector-score-threshold",
        dest="detector_score_threshold",
        type=float,
        help="Temporarily override detection.score_threshold.",
    )
    parser.add_argument(
        "--detector-iou",
        "--detector-nms-threshold",
        dest="detector_nms_threshold",
        type=float,
        help="Temporarily override detection.nms_threshold.",
    )
    parser.add_argument("--detector-imgsz", type=int, help="Temporarily override detection.input_size.")
    parser.add_argument("--detector-device", help="Temporarily override detection.device.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    config.setdefault("runtime", {})["_dry_run"] = bool(args.dry_run)
    apply_runtime_overrides(args, config)
    apply_detection_overrides(args, config)
    setup_logging(config)

    try:
        return run_farm_vehicle(config)
    except KeyboardInterrupt:
        logging.info("keyboard interrupt, exiting")
        return 130
    except Exception:
        logging.exception("run failed")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
