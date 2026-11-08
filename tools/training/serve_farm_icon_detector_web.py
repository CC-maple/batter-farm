r"""Serve a local web UI for BatterFarm V0.4 farm icon detection.

Run with the Python 3.12 conda environment used for local YOLO inference:

    & ".\.venv\Scripts\python.exe" tools/training/serve_farm_icon_detector_web.py

Then open:

    http://127.0.0.1:8765

Use another port:

    & ".\.venv\Scripts\python.exe" tools/training/serve_farm_icon_detector_web.py --port 8766

This server accepts local image uploads plus optional YOLO weight uploads and
runs offline YOLO inference. It does not click, press keys, or control the game
window.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import warnings
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import cgi


TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent.parent
DEFAULT_HTML = TOOLS_DIR / "farm_icon_detector_web.html"
DEFAULT_MODEL = REPO_ROOT / "models" / "farm-icons" / "best.onnx"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "logs" / "detections" / "farm_icons_v0.4_web"
START_FARM_BAT = REPO_ROOT / "scripts" / "start-farm.bat"
START_FARM_PREFERRED_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
MODEL_EXTENSIONS = {".onnx", ".pt"}

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from tools.training.detect_farm_icons import box_record, draw_annotated_image  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the BatterFarm farm icon detector web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Keep 127.0.0.1 for local-only access.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port.")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="Path to best.onnx or best.pt.")
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML, help="HTML file to serve at /.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Upload and annotated output folder.")
    parser.add_argument("--device", default="cpu", help="Inference device for Ultralytics. Use cpu on Windows.")
    parser.add_argument("--max-upload-mb", type=int, default=120, help="Maximum combined upload size in MB.")
    return parser.parse_args()


def info(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def sanitize_stem(name: str) -> str:
    stem = Path(name).stem
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._")
    return (safe or "upload")[:80]


def image_data_url(path: Path) -> str:
    data = path.read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def field_float(form: cgi.FieldStorage, name: str, default: float, minimum: float, maximum: float) -> float:
    raw = form.getfirst(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def field_int(form: cgi.FieldStorage, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = form.getfirst(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def field_text(form: cgi.FieldStorage, name: str, default: str = "") -> str:
    return str(form.getfirst(name, default) or "").strip()


def field_bool(form: cgi.FieldStorage, name: str, default: bool = False) -> bool:
    raw = form.getfirst(name, None)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def split_multi_value(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\r\n,]+", value) if item.strip()]


def start_farm_python_command() -> list[str]:
    if not START_FARM_PREFERRED_PYTHON.is_file():
        raise FileNotFoundError("Create the project .venv and install requirements.txt first.")
    return [str(START_FARM_PREFERRED_PYTHON)]


class DetectorApp:
    def __init__(self, model_path: Path, output_dir: Path, device: str) -> None:
        self.model_path = model_path.resolve()
        self.output_dir = output_dir.resolve()
        self.upload_dir = self.output_dir / "uploads"
        self.model_upload_dir = self.output_dir / "models"
        self.annotated_dir = self.output_dir / "annotated"
        self.main_run_dir = self.output_dir / "main_runs"
        self.device = device
        self.lock = threading.Lock()

        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.model_upload_dir.mkdir(parents=True, exist_ok=True)
        self.annotated_dir.mkdir(parents=True, exist_ok=True)
        self.main_run_dir.mkdir(parents=True, exist_ok=True)

        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise SystemExit(
                "Ultralytics is not installed. Use the batterfarm-yolo-cpu Python environment."
            ) from exc

        self.yolo_class = YOLO
        self.model = None
        self.names: dict[int, str] = {}
        self.load_model(self.model_path)

    def load_model(self, model_path: Path) -> None:
        resolved = model_path.resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Model not found: {resolved}")
        if self.model is not None and resolved == self.model_path:
            return
        info(f"Loading model: {resolved}")
        self.model = self.yolo_class(str(resolved), task="detect")
        self.model_path = resolved
        self.names = {int(key): value for key, value in self.model.names.items()}
        info(f"Classes: {self.names}")

    def infer(self, image_path: Path, conf: float, iou: float, imgsz: int, model_path: Path | None = None) -> dict[str, Any]:
        annotated_path = self.annotated_dir / f"{image_path.stem}_annotated.png"
        with self.lock:
            if model_path is not None:
                self.load_model(model_path)
            if self.model is None:
                raise RuntimeError("Model is not loaded.")
            names = dict(self.names)
            active_model_path = self.model_path
            results = self.model.predict(
                source=str(image_path),
                imgsz=imgsz,
                batch=1,
                conf=conf,
                iou=iou,
                device=self.device,
                save=False,
                exist_ok=True,
                verbose=False,
            )

        if not results:
            raise RuntimeError("Model returned no result.")

        result = results[0]
        records: list[dict[str, object]] = []
        for box in result.boxes:
            records.append(box_record(result, box, names))

        draw_annotated_image(image_path, records, annotated_path)
        return {
            "detections": records,
            "annotated_path": annotated_path,
            "annotated_image": image_data_url(annotated_path),
            "model_path": active_model_path,
            "classes": names,
        }


def make_handler(app: DetectorApp, html_path: Path, max_upload_mb: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BatterFarmDetector/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            info(f"{self.address_string()} {format % args}")

        def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_html(self) -> None:
            if not html_path.exists():
                self.send_json({"error": f"HTML file not found: {html_path}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            body = html_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path in {"/", "/index.html"}:
                self.send_html()
                return
            if self.path == "/api/status":
                self.send_json(
                    {
                        "model": str(app.model_path),
                        "device": app.device,
                        "classes": app.names,
                    }
                )
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path != "/api/detect":
                if self.path == "/api/run-main":
                    try:
                        self.handle_run_main()
                    except Exception as exc:
                        self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                self.handle_detect()
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def read_multipart_form(self) -> cgi.FieldStorage | None:
            content_length = int(self.headers.get("Content-Length", "0"))
            max_bytes = max_upload_mb * 1024 * 1024
            if content_length <= 0:
                self.send_json({"error": "empty request"}, HTTPStatus.BAD_REQUEST)
                return None
            if content_length > max_bytes:
                self.send_json({"error": f"upload too large, max {max_upload_mb} MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return None

            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self.send_json({"error": "expected multipart/form-data"}, HTTPStatus.BAD_REQUEST)
                return None

            return cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(content_length),
                },
            )

        def handle_detect(self) -> None:
            form = self.read_multipart_form()
            if form is None:
                return
            image_item = form["image"] if "image" in form else None
            if image_item is None or not getattr(image_item, "filename", ""):
                self.send_json({"error": "missing image file"}, HTTPStatus.BAD_REQUEST)
                return

            suffix = Path(image_item.filename).suffix.lower()
            if suffix not in IMAGE_EXTENSIONS:
                self.send_json({"error": f"unsupported image type: {suffix}"}, HTTPStatus.BAD_REQUEST)
                return

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            upload_path = app.upload_dir / f"{stamp}_{sanitize_stem(image_item.filename)}{suffix}"
            with upload_path.open("wb") as handle:
                shutil.copyfileobj(image_item.file, handle)

            model_path = None
            model_item = form["model"] if "model" in form else None
            if model_item is not None and getattr(model_item, "filename", ""):
                model_suffix = Path(model_item.filename).suffix.lower()
                if model_suffix not in MODEL_EXTENSIONS:
                    self.send_json({"error": f"unsupported model type: {model_suffix}"}, HTTPStatus.BAD_REQUEST)
                    return
                model_path = app.model_upload_dir / f"{stamp}_{sanitize_stem(model_item.filename)}{model_suffix}"
                with model_path.open("wb") as handle:
                    shutil.copyfileobj(model_item.file, handle)

            conf = field_float(form, "conf", 0.25, 0.01, 0.95)
            iou = field_float(form, "iou", 0.45, 0.10, 0.90)
            imgsz = field_int(form, "imgsz", 960, 320, 1600)

            if model_path is not None:
                info(f"Use uploaded model: {model_path.name}")
            info(f"Infer upload: {upload_path.name}, conf={conf}, iou={iou}, imgsz={imgsz}")
            result = app.infer(upload_path, conf=conf, iou=iou, imgsz=imgsz, model_path=model_path)
            annotated_path = Path(result["annotated_path"])
            self.send_json(
                {
                    "detections": result["detections"],
                    "annotated_image": result["annotated_image"],
                    "model": str(result["model_path"]),
                    "classes": result["classes"],
                    "saved_upload": str(upload_path),
                    "saved_model": str(model_path) if model_path is not None else None,
                    "saved_annotated": str(annotated_path),
                }
            )

        def handle_run_main(self) -> None:
            form = self.read_multipart_form()
            if form is None:
                return

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            command = start_farm_python_command() + ["main.py", "--overlay"]

            config_path = field_text(form, "config", "configs/config.yaml")
            if config_path:
                command.extend(["--config", config_path])

            if field_bool(form, "dry_run", False):
                command.append("--dry-run")

            if field_bool(form, "overlay", False):
                info("Overlay is already enabled by scripts/start-farm.bat-compatible launch.")

            detector_enabled = field_text(form, "detector_enabled")
            if detector_enabled in {"true", "false"}:
                command.extend(["--detector-enabled", detector_enabled])

            model_path = field_text(form, "detector_model_path")
            model_item = form["detector_model"] if "detector_model" in form else None
            saved_model_path: Path | None = None
            if model_item is not None and getattr(model_item, "filename", ""):
                model_suffix = Path(model_item.filename).suffix.lower()
                if model_suffix not in MODEL_EXTENSIONS:
                    self.send_json({"error": f"unsupported model type: {model_suffix}"}, HTTPStatus.BAD_REQUEST)
                    return
                saved_model_path = app.model_upload_dir / f"{stamp}_{sanitize_stem(model_item.filename)}{model_suffix}"
                with saved_model_path.open("wb") as handle:
                    shutil.copyfileobj(model_item.file, handle)
                model_path = str(saved_model_path)
            if model_path:
                command.extend(["--detector-model", model_path])

            detector_conf = field_text(form, "detector_conf")
            if detector_conf:
                command.extend(["--detector-conf", detector_conf])

            detector_iou = field_text(form, "detector_iou")
            if detector_iou:
                command.extend(["--detector-iou", detector_iou])

            detector_imgsz = field_text(form, "detector_imgsz")
            if detector_imgsz:
                command.extend(["--detector-imgsz", detector_imgsz])

            detector_device = field_text(form, "detector_device")
            if detector_device:
                command.extend(["--detector-device", detector_device])

            log_path = app.main_run_dir / f"{stamp}_main.log"
            info(f"Start main script: {' '.join(command)}")
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            log_handle = log_path.open("w", encoding="utf-8")
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(REPO_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            finally:
                log_handle.close()

            self.send_json(
                {
                    "pid": process.pid,
                    "launcher": str(START_FARM_BAT),
                    "command": command,
                    "log": str(log_path),
                    "saved_model": str(saved_model_path) if saved_model_path is not None else None,
                }
            )

    return Handler


def main() -> int:
    args = parse_args()
    app = DetectorApp(args.model, args.output_dir, args.device)
    handler = make_handler(app, args.html.resolve(), args.max_upload_mb)
    server = ThreadingHTTPServer((args.host, args.port), handler)

    info("BatterFarm V0.4 detector web server")
    info(f"Python: {sys.executable}")
    info(f"URL: http://{args.host}:{args.port}")
    info(f"Output: {args.output_dir.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        info("Stopping server")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
