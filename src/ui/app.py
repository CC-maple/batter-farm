from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from src.ui.theme import apply_theme


ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BatterFarm PySide6 desktop UI")
    parser.add_argument("--config", default="configs/config.yaml")
    parser.add_argument("--demo", action="store_true", help="Show UI with safe sample status and no worker actions.")
    parser.add_argument("--page", default="home", help="Initial page id, useful for visual QA.")
    return parser.parse_args()


def resolve_config_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    args = parse_args()
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BatterFarm.BatterUI")
        except Exception:
            pass
    app = QApplication(sys.argv[:1])
    app.setApplicationName("BatterUI")
    app.setOrganizationName("BatterFarm")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    apply_theme(app)

    # Keep the Windows input stack (including pyautogui) out of the import
    # graph until Qt has selected its DPI awareness context.  Calling a legacy
    # DPI API before QApplication is created makes Qt's Per-Monitor V2 request
    # fail with ERROR_ACCESS_DENIED.
    from src.ui.main_window import MainWindow

    window = MainWindow(ROOT, resolve_config_path(args.config), demo=bool(args.demo))
    if args.page:
        window.show_page(str(args.page))
    window.show()
    window.raise_()
    window.activateWindow()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
