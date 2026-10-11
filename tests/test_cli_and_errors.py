"""
Unit tests for main.py CLI error handling, validation, logging, and guaranteed cleanup.
"""

import os
import unittest
from pathlib import Path
import tempfile
import spiceypy as spice

from main import (
    inspect_kernels_cli,
    validate_dependencies_cli,
    run_pipeline_with_cleanup,
    LOGS_DIR,
)
from spice_manager import SpiceManager


class TestCliAndErrorHandling(unittest.TestCase):
    """Test CLI commands and error handling paths."""

    def setUp(self):
        spice.kclear()

    def tearDown(self):
        spice.kclear()

    def test_inspect_kernels_cli_runs_and_leaves_pool_clean(self):
        self.assertEqual(spice.ktotal("ALL"), 0)
        inspect_kernels_cli("kernels")
        # Pool must be cleanly unloaded after inspection
        self.assertEqual(spice.ktotal("ALL"), 0)

    def test_validate_dependencies_cli_runs_and_leaves_pool_clean(self):
        self.assertEqual(spice.ktotal("ALL"), 0)
        valid = validate_dependencies_cli("kernels")
        self.assertTrue(valid)
        self.assertEqual(spice.ktotal("ALL"), 0)

    def test_pipeline_success_with_cleanup(self):
        cfg = {
            "target": "4",
            "observer": "399",
            "reference_frame": "J2000",
            "aberration_correction": "LT+S",
            "start_utc": "2026-10-11T00:00:00",
            "end_utc": "2026-10-11T06:00:00",
            "sampling_interval_hours": 3.0,
            "output_formats": ["csv", "xlsx", "txt"],
            "kernel_directory": "kernels",
            "output_directory": "output",
            "overwrite_policy": "overwrite",
        }
        success, result, err = run_pipeline_with_cleanup(cfg)
        self.assertTrue(success)
        self.assertIsNone(err)
        self.assertEqual(result["total_samples"], 3)
        self.assertEqual(result["success_count"], 3)
        # Verify pool is clean
        self.assertEqual(spice.ktotal("ALL"), 0)

    def test_error_handling_invalid_utc_timestamp(self):
        cfg = {
            "target": "4",
            "observer": "399",
            "reference_frame": "J2000",
            "aberration_correction": "LT+S",
            "start_utc": "invalid-timestamp",
            "end_utc": "2026-10-11T06:00:00",
            "sampling_interval_hours": 1.0,
            "output_formats": ["csv"],
            "kernel_directory": "kernels",
            "output_directory": "output",
        }
        success, result, err = run_pipeline_with_cleanup(cfg)
        self.assertFalse(success)
        self.assertIn("Timestamp conversion error", err)
        # Guaranteed cleanup check
        self.assertEqual(spice.ktotal("ALL"), 0)

    def test_error_handling_unsupported_frame(self):
        cfg = {
            "target": "4",
            "observer": "399",
            "reference_frame": "NON_EXISTENT_FRAME",
            "aberration_correction": "LT+S",
            "start_utc": "2026-10-11T00:00:00",
            "end_utc": "2026-10-11T02:00:00",
            "sampling_interval_hours": 1.0,
            "output_formats": ["csv"],
            "kernel_directory": "kernels",
            "output_directory": "output",
        }
        success, result, err = run_pipeline_with_cleanup(cfg)
        self.assertFalse(success)
        self.assertIn("Reference frame", err)
        self.assertEqual(spice.ktotal("ALL"), 0)

    def test_error_handling_out_of_coverage(self):
        cfg = {
            "target": "4",
            "observer": "399",
            "reference_frame": "J2000",
            "aberration_correction": "LT+S",
            "start_utc": "3000-01-01T00:00:00",  # year 3000 is way beyond coverage
            "end_utc": "3000-01-01T02:00:00",
            "sampling_interval_hours": 1.0,
            "output_formats": ["csv"],
            "kernel_directory": "kernels",
            "output_directory": "output",
        }
        success, result, err = run_pipeline_with_cleanup(cfg)
        self.assertFalse(success)
        self.assertIn("Extraction failed for all", err)
        self.assertEqual(spice.ktotal("ALL"), 0)

    def test_error_handling_missing_kernels(self):
        cfg = {
            "target": "4",
            "observer": "399",
            "reference_frame": "J2000",
            "aberration_correction": "LT+S",
            "start_utc": "2026-10-11T00:00:00",
            "end_utc": "2026-10-11T02:00:00",
            "sampling_interval_hours": 1.0,
            "output_formats": ["csv"],
            "kernel_directory": "nonexistent_dir",
            "output_directory": "output",
        }
        success, result, err = run_pipeline_with_cleanup(cfg)
        self.assertFalse(success)
        self.assertIn("not accessible", err)
        self.assertEqual(spice.ktotal("ALL"), 0)

    def test_cleanup_preserves_external_kernel_state(self):
        """Simulate an external kernel loaded prior to CLI execution and verify it remains loaded."""
        external_kernel = "kernels/pck00010.tpc"
        spice.furnsh(external_kernel)
        self.assertEqual(spice.ktotal("ALL"), 1)

        cfg = {
            "target": "4",
            "observer": "399",
            "reference_frame": "J2000",
            "aberration_correction": "LT+S",
            "start_utc": "2026-10-11T00:00:00",
            "end_utc": "2026-10-11T02:00:00",
            "sampling_interval_hours": 1.0,
            "output_formats": ["csv"],
            "kernel_directory": "kernels",
            "output_directory": "output",
            "overwrite_policy": "overwrite",
        }
        success, result, err = run_pipeline_with_cleanup(cfg)
        self.assertTrue(success)

        # After pipeline finishes, external kernel is still intact in the pool!
        self.assertEqual(spice.ktotal("ALL"), 1)
        loaded = SpiceManager.get_loaded_kernels()
        self.assertIn("pck00010.tpc", loaded[0]["file"])

        spice.kclear()

    def test_log_file_written(self):
        self.assertTrue(LOGS_DIR.exists())
        log_files = list(LOGS_DIR.glob("*.log"))
        self.assertGreater(len(log_files), 0)
        latest_log = log_files[-1]
        content = latest_log.read_text(encoding="utf-8")
        self.assertGreater(len(content), 0)


if __name__ == "__main__":
    unittest.main()
