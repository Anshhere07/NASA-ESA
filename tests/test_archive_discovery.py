"""
Unit tests for archive_discovery.py and job_manager.py
"""

import time
import unittest
from pathlib import Path
import spiceypy as spice

from archive_discovery import (
    OFFICIAL_ARCHIVE_CATALOG,
    get_archive_catalog,
    check_kernel_freshness,
)
from job_manager import JobManager


class TestArchiveDiscovery(unittest.TestCase):
    """Test archive catalog and freshness checking."""

    def test_archive_catalog_entries(self):
        catalog = get_archive_catalog(verify_online=False)
        self.assertGreater(len(catalog), 0)
        filenames = [c["filename"] for c in catalog]
        self.assertIn("de440s.bsp", filenames)
        self.assertIn("naif0012.tls", filenames)
        self.assertIn("pck00010.tpc", filenames)

    def test_freshness_check_fallback(self):
        # Entry with unreachable URL to verify robust fallback
        fake_entry = {
            "filename": "fake_kernel.bsp",
            "kernel_type": "SPK",
            "official_url": "https://naif.jpl.nasa.gov/non_existent_fake_path_123456.bsp",
        }
        res = check_kernel_freshness(fake_entry, timeout_seconds=1.5)
        self.assertIn("Freshness could not be verified", res["freshness_status"])


class TestJobManager(unittest.TestCase):
    """Test asynchronous extraction job runner and isolation."""

    def setUp(self):
        spice.kclear()
        self.job_manager = JobManager()

    def tearDown(self):
        spice.kclear()

    def test_submit_and_run_successful_job(self):
        config = {
            "target": "4",
            "observer": "399",
            "reference_frame": "J2000",
            "aberration_correction": "LT+S",
            "start_utc": "2026-10-11T00:00:00",
            "end_utc": "2026-10-11T02:00:00",
            "sampling_interval_hours": 1.0,
            "kernel_directory": "kernels",
            "output_formats": ["csv", "xlsx", "txt", "json"],
        }

        job_id = self.job_manager.submit_job(config)
        self.assertIsNotNone(job_id)

        # Wait for job completion
        max_wait = 10.0
        start = time.time()
        job = None
        while time.time() - start < max_wait:
            job = self.job_manager.get_job(job_id)
            if job and job.status in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.2)

        self.assertIsNotNone(job)
        self.assertEqual(job.status, "COMPLETED")
        self.assertEqual(job.progress_pct, 100)
        self.assertEqual(job.total_samples, 3)
        self.assertEqual(job.success_count, 3)

        # Check DataFrame cache
        df = self.job_manager.get_job_dataframe(job_id)
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 3)

        # Check exported files
        self.assertIn("csv", job.exported_files)
        self.assertIn("xlsx", job.exported_files)
        self.assertIn("txt", job.exported_files)
        self.assertIn("json", job.exported_files)

        for p_str in job.exported_files.values():
            self.assertTrue(Path(p_str).exists())

    def test_submit_failing_job(self):
        config = {
            "target": "INVALID_TARGET_XYZ",
            "observer": "399",
            "reference_frame": "J2000",
            "aberration_correction": "LT+S",
            "start_utc": "2026-10-11T00:00:00",
            "end_utc": "2026-10-11T01:00:00",
            "sampling_interval_hours": 1.0,
            "kernel_directory": "kernels",
        }

        job_id = self.job_manager.submit_job(config)
        max_wait = 5.0
        start = time.time()
        job = None
        while time.time() - start < max_wait:
            job = self.job_manager.get_job(job_id)
            if job and job.status in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.2)

        self.assertIsNotNone(job)
        self.assertEqual(job.status, "FAILED")
        self.assertIsNotNone(job.error_message)


if __name__ == "__main__":
    unittest.main()
