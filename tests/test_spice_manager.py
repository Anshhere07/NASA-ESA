"""
Unit test suite for spice_manager.py

Tests:
1. Kernel discovery and validation.
2. Controlled loading, inspection (ktotal/kdata), and scoped unloading without clearing external state.
3. SPK object IDs and coverage extraction (generic, no hardcoded targets).
4. Unsupported target error and coverage error handling.
5. UTC-to-ET conversion:
   - Valid formats (ISO 8601, SPICE calendar).
   - Missing LSK error handling.
   - Format validation and rejection of malformed or invalid calendar dates.
   - Type and value errors.
"""

import unittest
from pathlib import Path

import spiceypy as spice
from spice_manager import (
    SpiceManager,
    utc_to_et,
    is_lsk_loaded,
    KernelNotFoundError,
    KernelReadError,
    MissingKernelError,
    UnsupportedTargetError,
    CoverageError,
    SpiceTimeConversionError,
)


class TestSpiceManagerKernelLifecycle(unittest.TestCase):
    """Test kernel discovery, validation, controlled loading, and unloading."""

    def setUp(self):
        # Ensure SPICE state is clean before each test
        spice.kclear()
        self.kernels_dir = Path("kernels")
        self.lsk_path = self.kernels_dir / "naif0012.tls"
        self.pck_path = self.kernels_dir / "pck00010.tpc"
        self.spk_path = self.kernels_dir / "de440s.bsp"

    def tearDown(self):
        spice.kclear()

    def test_kernel_discovery(self):
        manager = SpiceManager(self.kernels_dir)
        discovered = manager.discover_kernels()

        self.assertIn("lsk", discovered)
        self.assertIn("spk", discovered)
        self.assertIn("pck", discovered)

        lsk_names = [p.name for p in discovered["lsk"]]
        spk_names = [p.name for p in discovered["spk"]]
        pck_names = [p.name for p in discovered["pck"]]

        self.assertIn("naif0012.tls", lsk_names)
        self.assertIn("de440s.bsp", spk_names)
        self.assertIn("pck00010.tpc", pck_names)

    def test_discovery_invalid_directory(self):
        manager = SpiceManager("non_existent_kernels_dir_xyz")
        with self.assertRaises(KernelNotFoundError):
            manager.discover_kernels()

    def test_validate_kernel_file_not_found(self):
        with self.assertRaises(KernelNotFoundError) as ctx:
            SpiceManager.validate_kernel_file("kernels/non_existent_kernel.bsp")
        self.assertIn("Kernel file not found", str(ctx.exception))

    def test_validate_kernel_file_success(self):
        path = SpiceManager.validate_kernel_file(self.lsk_path)
        self.assertTrue(path.exists())

    def test_controlled_load_and_unload(self):
        manager = SpiceManager(self.kernels_dir)
        self.assertEqual(spice.ktotal("ALL"), 0)

        # Load LSK
        manager.load_kernel(self.lsk_path)
        self.assertEqual(spice.ktotal("ALL"), 1)
        self.assertTrue(manager.is_kernel_loaded(self.lsk_path))
        self.assertTrue(is_lsk_loaded())

        # Load PCK
        manager.load_kernel(self.pck_path)
        self.assertEqual(spice.ktotal("ALL"), 2)

        # Unload LSK specifically
        manager.unload_kernel(self.lsk_path)
        self.assertEqual(spice.ktotal("ALL"), 1)
        self.assertFalse(manager.is_kernel_loaded(self.lsk_path))
        self.assertFalse(is_lsk_loaded())
        self.assertTrue(manager.is_kernel_loaded(self.pck_path))

        # Unload remaining
        manager.unload_all()
        self.assertEqual(spice.ktotal("ALL"), 0)

    def test_controlled_unloading_preserves_external_kernels(self):
        """Verify that manager.unload_all() only unloads its own kernels, not external ones."""
        # Simulate external kernel loaded outside this manager
        spice.furnsh(str(self.pck_path.resolve()))
        self.assertEqual(spice.ktotal("ALL"), 1)

        # Inside manager, load only LSK
        with SpiceManager(self.kernels_dir) as sm:
            sm.load_kernel(self.lsk_path)
            self.assertEqual(spice.ktotal("ALL"), 2)
            self.assertEqual(len(sm.loaded_kernels), 1)

        # After manager context exit, LSK is unloaded, but external PCK kernel remains!
        self.assertEqual(spice.ktotal("ALL"), 1)
        loaded = SpiceManager.get_loaded_kernels()
        self.assertEqual(len(loaded), 1)
        self.assertIn("pck00010.tpc", loaded[0]["file"])

    def test_inspect_loaded_kernels_ktotal_kdata(self):
        with SpiceManager(self.kernels_dir) as sm:
            sm.load_kernels([self.lsk_path, self.pck_path])
            loaded_info = sm.get_loaded_kernels("ALL")

            self.assertEqual(len(loaded_info), 2)
            files = [item["file"] for item in loaded_info]
            types = [item["type"] for item in loaded_info]

            self.assertTrue(any("naif0012.tls" in f for f in files))
            self.assertTrue(any("pck00010.tpc" in f for f in files))
            self.assertIn("TEXT", types)

    def test_validate_required_kernels_checks(self):
        manager = SpiceManager(self.kernels_dir)

        # When nothing loaded, requiring LSK raises MissingKernelError
        with self.assertRaises(MissingKernelError) as ctx:
            manager.validate_required_kernels(require_lsk=True)
        self.assertIn("Leap-seconds kernel (LSK) is missing", str(ctx.exception))

        # Load LSK, require SPK
        manager.load_kernel(self.lsk_path)
        with self.assertRaises(MissingKernelError) as ctx:
            manager.validate_required_kernels(require_lsk=True, require_spk=True)
        self.assertIn("No Spacecraft/Planetary Ephemeris Kernel (SPK)", str(ctx.exception))

        # Load SPK
        manager.load_kernel(self.spk_path)
        manager.validate_required_kernels(require_lsk=True, require_spk=True)
        manager.unload_all()


class TestSpiceManagerSPKCoverage(unittest.TestCase):
    """Test generic SPK inspection, object detection, and coverage bounds."""

    def setUp(self):
        spice.kclear()
        self.manager = SpiceManager("kernels")
        self.lsk_path = Path("kernels/naif0012.tls")
        self.spk_path = Path("kernels/de440s.bsp")

    def tearDown(self):
        self.manager.unload_all()
        spice.kclear()

    def test_generic_spk_objects_detection(self):
        """Verifies SPK objects are discovered without assuming Earth or Mars."""
        obj_ids = self.manager.get_spk_objects(self.spk_path)
        self.assertIsInstance(obj_ids, list)
        self.assertGreater(len(obj_ids), 0)
        # All elements should be integer NAIF IDs
        for oid in obj_ids:
            self.assertIsInstance(oid, int)

    def test_spk_coverage_extraction(self):
        self.manager.load_kernel(self.lsk_path)
        obj_ids = self.manager.get_spk_objects(self.spk_path)
        first_id = obj_ids[0]

        intervals = self.manager.get_spk_coverage(self.spk_path, first_id)
        self.assertGreater(len(intervals), 0)
        iv = intervals[0]
        self.assertIn("start_et", iv)
        self.assertIn("end_et", iv)
        self.assertIn("start_utc", iv)
        self.assertIn("end_utc", iv)
        self.assertLess(iv["start_et"], iv["end_et"])
        self.assertIsNotNone(iv["start_utc"])

    def test_target_coverage_valid(self):
        self.manager.load_kernel(self.lsk_path)
        # Check an available body ID at a valid epoch (year 2026)
        obj_ids = self.manager.get_spk_objects(self.spk_path)
        test_id = obj_ids[0]
        et = utc_to_et("2026-10-01T12:00:00")

        target_id, name, intervals = self.manager.check_target_coverage(
            self.spk_path, test_id, et=et
        )
        self.assertEqual(target_id, test_id)
        self.assertGreater(len(intervals), 0)

    def test_unsupported_target_error(self):
        """Actionable error when an unknown/unsupported target is requested."""
        fake_target_id = 9999999
        with self.assertRaises(UnsupportedTargetError) as ctx:
            self.manager.check_target_coverage(self.spk_path, fake_target_id)
        self.assertIn("is not present in SPK", str(ctx.exception))
        self.assertIn("Available target IDs in this SPK are:", str(ctx.exception))

    def test_coverage_error_out_of_bounds(self):
        """Actionable error when requested epoch ET is outside coverage interval."""
        self.manager.load_kernel(self.lsk_path)
        obj_ids = self.manager.get_spk_objects(self.spk_path)
        test_id = obj_ids[0]

        # Year 3000 is way beyond de440s coverage (ends in 2150)
        et_year_3000 = utc_to_et("3000-01-01T00:00:00")
        with self.assertRaises(CoverageError) as ctx:
            self.manager.check_target_coverage(self.spk_path, test_id, et=et_year_3000)
        self.assertIn("outside the valid coverage range", str(ctx.exception))


class TestUtcToEtConversion(unittest.TestCase):
    """Test UTC to ET conversion function and all validation/failure cases."""

    def setUp(self):
        spice.kclear()
        self.lsk_path = Path("kernels/naif0012.tls")

    def tearDown(self):
        spice.kclear()

    def test_conversion_without_lsk_raises_missing_kernel_error(self):
        """Conversion must fail if LSK is not loaded."""
        self.assertFalse(is_lsk_loaded())
        with self.assertRaises(MissingKernelError) as ctx:
            utc_to_et("2026-10-01T23:00:00")
        self.assertIn("Leap-seconds kernel (LSK) is not loaded", str(ctx.exception))

    def test_conversion_valid_formats(self):
        spice.furnsh(str(self.lsk_path.resolve()))

        # Test ISO 8601 specified in requirements
        et1 = utc_to_et("2026-10-01T23:00:00")
        self.assertIsInstance(et1, float)
        self.assertAlmostEqual(et1, 844167669.182, delta=0.01)

        # Test with 'Z' suffix
        et2 = utc_to_et("2026-10-01T23:00:00Z")
        self.assertAlmostEqual(et1, et2, delta=1e-5)

        # Test with fractional seconds
        et3 = utc_to_et("2026-10-01T23:00:00.500")
        self.assertAlmostEqual(et3, et1 + 0.5, delta=1e-5)

        # Test calendar format
        et4 = utc_to_et("2026-OCT-01 23:00:00")
        self.assertAlmostEqual(et1, et4, delta=1e-5)

    def test_invalid_formats_raise_error(self):
        spice.furnsh(str(self.lsk_path.resolve()))

        invalid_timestamps = [
            "not-a-timestamp",
            "2026-99-01T23:00:00",  # invalid month
            "2026-10-01T25:00:00",  # invalid hour
            "2026/10/01 23:00:00",  # wrong separator
            "2026-10-01T23:00:00 with trailing garbage",
        ]

        for inv in invalid_timestamps:
            with self.assertRaises(SpiceTimeConversionError, msg=f"Failed for {inv}"):
                utc_to_et(inv)

    def test_invalid_calendar_date_raises_error(self):
        spice.furnsh(str(self.lsk_path.resolve()))
        # Feb 31st matches ISO syntax but fails calendar validation
        with self.assertRaises(SpiceTimeConversionError) as ctx:
            utc_to_et("2026-02-31T12:00:00")
        self.assertIn("SPICE failed to convert timestamp", str(ctx.exception))

    def test_type_error_for_non_string(self):
        with self.assertRaises(TypeError):
            utc_to_et(12345)  # type: ignore

        with self.assertRaises(TypeError):
            utc_to_et(None)   # type: ignore

    def test_value_error_for_empty_string(self):
        with self.assertRaises(ValueError):
            utc_to_et("")


if __name__ == "__main__":
    unittest.main()
