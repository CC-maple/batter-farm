from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ui.main_window import MainWindow  # noqa: E402
from src.ui.pages.routes import AddTaskDialog  # noqa: E402
from src.ui.theme import apply_theme  # noqa: E402


PAGE_IDS = ["home", "realtime", "independent", "automatic", "routes", "detector", "logs", "settings"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render every BatterUI page to PNG for visual QA.")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=820)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or ROOT / "logs" / "ui_review" / datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    apply_theme(app)
    window = MainWindow(ROOT, ROOT / "configs" / "config.yaml", demo=True)
    window.resize(max(1080, args.width), max(700, args.height))
    window.show()
    QTest.qWait(250)

    results: list[dict[str, object]] = []
    for index, page_id in enumerate(PAGE_IDS, start=1):
        window.show_page(page_id)
        QTest.qWait(160)
        pixmap = window.grab()
        path = output_dir / f"{index:02d}_{page_id}.png"
        if not pixmap.save(str(path), "PNG"):
            raise RuntimeError(f"failed to save screenshot: {path}")
        results.append(
            {
                "page": page_id,
                "path": str(path),
                "width": pixmap.width(),
                "height": pixmap.height(),
                "device_pixel_ratio": pixmap.devicePixelRatio(),
            }
        )

    routes = window.pages["routes"]
    window.show_page("routes")
    routes.advanced_button.setChecked(True)
    QTest.qWait(120)
    advanced_path = output_dir / "09_routes_advanced.png"
    advanced_pixmap = window.grab()
    if not advanced_pixmap.save(str(advanced_path), "PNG"):
        raise RuntimeError(f"failed to save screenshot: {advanced_path}")
    results.append(
        {
            "page": "routes_advanced",
            "path": str(advanced_path),
            "width": advanced_pixmap.width(),
            "height": advanced_pixmap.height(),
            "device_pixel_ratio": advanced_pixmap.devicePixelRatio(),
        }
    )
    routes.advanced_button.setChecked(False)

    routes.countdown.setValue(3.0)
    routes.record_button.click()
    QTest.qWait(120)
    countdown_path = output_dir / "10_routes_countdown.png"
    countdown_pixmap = window.grab()
    if not countdown_pixmap.save(str(countdown_path), "PNG"):
        raise RuntimeError(f"failed to save screenshot: {countdown_path}")
    results.append(
        {
            "page": "routes_countdown",
            "path": str(countdown_path),
            "width": countdown_pixmap.width(),
            "height": countdown_pixmap.height(),
            "device_pixel_ratio": countdown_pixmap.devicePixelRatio(),
        }
    )
    routes.stop_button.click()
    QTest.qWait(80)

    dialog = AddTaskDialog(ROOT, window)
    dialog.show()
    QTest.qWait(120)
    dialog_path = output_dir / "11_add_custom_task_dialog.png"
    dialog_pixmap = dialog.grab()
    if not dialog_pixmap.save(str(dialog_path), "PNG"):
        raise RuntimeError(f"failed to save screenshot: {dialog_path}")
    results.append(
        {
            "page": "add_custom_task_dialog",
            "path": str(dialog_path),
            "width": dialog_pixmap.width(),
            "height": dialog_pixmap.height(),
            "device_pixel_ratio": dialog_pixmap.devicePixelRatio(),
        }
    )
    dialog.close()

    window.close()
    app.processEvents()
    report = {
        "captured_at": datetime.now().astimezone().isoformat(),
        "logical_dpi": app.primaryScreen().logicalDotsPerInch() if app.primaryScreen() else None,
        "screenshots": results,
    }
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
