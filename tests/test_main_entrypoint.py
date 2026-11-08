from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main as main_module


ROOT = Path(__file__).resolve().parents[1]


def args_for(*, dry_run: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        config="config.yaml",
        dry_run=dry_run,
        overlay=False,
        detector_enabled=None,
        detector_model=None,
        detector_score_threshold=None,
        detector_nms_threshold=None,
        detector_imgsz=None,
        detector_device=None,
    )


class MainEntrypointTests(unittest.TestCase):
    def test_start_farm_uses_only_the_fixed_project_python(self) -> None:
        script = (ROOT / "scripts" / "start-farm.bat").read_text(encoding="utf-8")

        self.assertIn(r"%ROOT%\.venv\Scripts\python.exe", script)
        self.assertIn('if not exist "%YOLO_PYTHON%"', script)
        self.assertNotIn("python --version", script)
        self.assertNotIn("py -3", script)

    def test_default_entry_runs_farm_vehicle(self) -> None:
        with patch.object(main_module, "parse_args", return_value=args_for()), patch.object(
            main_module, "load_config", return_value={}
        ), patch.object(main_module, "setup_logging"), patch.object(
            main_module, "run_farm_vehicle", return_value=17
        ) as run_farm:
            self.assertEqual(main_module.main(), 17)
        run_farm.assert_called_once()

    def test_dry_run_is_passed_to_v0_4_runtime(self) -> None:
        with patch.object(main_module, "parse_args", return_value=args_for(dry_run=True)), patch.object(
            main_module, "load_config", return_value={}
        ), patch.object(main_module, "setup_logging"), patch.object(
            main_module, "run_farm_vehicle", return_value=17
        ) as run_farm:
            self.assertEqual(main_module.main(), 17)
        self.assertTrue(run_farm.call_args.args[0]["runtime"]["_dry_run"])


if __name__ == "__main__":
    unittest.main()
