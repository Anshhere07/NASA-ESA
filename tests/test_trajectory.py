"""
Unit test suite for trajectory.py

Tests:
1. Parameter validations (target, observer, frame, aberration correction, step size, epochs).
2. Missing kernel handling before extraction.
3. Single state vector extraction with configurable options.
4. SPICE error reporting on ephemeris calculation failure.
5. Time-series extraction:
   - DataFrame structure and column verification.
   - Endpoint handling (interval alignment and include_exact_end).
   - Failure tracking without zero replacement (NaN verification).
   - Tuple unpacking (df, failures = extract_time_series(...)).
6. Real extraction with available planetary kernels (Mars, Moon, Earth).
"""

import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import spiceypy as spice

from spice_manager import SpiceManager
from trajectory import (
    get_state_vector,
    extract_time_series,
    validate_extraction_parameters,
    StateVector,
    TrajectoryExtractionResult,
    InvalidParameterError,
    EphemerisExtractionError,
    MissingKernelError,
    DATAFRAME_COLUMNS,
    FAILURE_COLUMNS,
)


class TestTrajectoryValidation(unittest.TestCase):
    """Test extraction parameter validation and kernel pool prerequisites."""

    def setUp(self):
        spice.kclear()
        self.manager = SpiceManager("kernels")
        self.lsk = Path("kernels/naif0012.tls")
        self.spk = Path("kernels/de440s.bsp")

    def tearDown(self):
        self.manager.unload_all()
        spice.kclear()

    def test_missing_lsk_raises_error(self):
        with self.assertRaises(MissingKernelError):
            validate_extraction_parameters("4", "399", "J2000", "NONE")

    def test_missing_spk_raises_error(self):
        self.manager.load_kernel(self.lsk)
        with self.assertRaises(MissingKernelError):
            validate_extraction_parameters("4", "399", "J2000", "NONE")

    def test_empty_target_or_observer(self):
        self.manager.load_kernels([self.lsk, self.spk])

        with self.assertRaises(InvalidParameterError):
            validate_extraction_parameters("", "399", "J2000", "NONE")

        with self.assertRaises(InvalidParameterError):
            validate_extraction_parameters("4", "  ", "J2000", "NONE")

        with self.assertRaises(InvalidParameterError):
            validate_extraction_parameters(None, "399", "J2000", "NONE")

    def test_identical_target_and_observer(self):
        self.manager.load_kernels([self.lsk, self.spk])
        with self.assertRaises(InvalidParameterError) as ctx:
            validate_extraction_parameters("399", "399", "J2000", "NONE")
        self.assertIn("cannot be the same body", str(ctx.exception))

    def test_invalid_reference_frame(self):
        self.manager.load_kernels([self.lsk, self.spk])
        with self.assertRaises(InvalidParameterError) as ctx:
            validate_extraction_parameters("4", "399", "NON_EXISTENT_FRAME", "NONE")
        self.assertIn("is not recognized by SPICE", str(ctx.exception))

    def test_invalid_aberration_correction(self):
        self.manager.load_kernels([self.lsk, self.spk])
        with self.assertRaises(InvalidParameterError) as ctx:
            validate_extraction_parameters("4", "399", "J2000", "INVALID_ABCORR")
        self.assertIn("Aberration correction 'INVALID_ABCORR' is not recognized", str(ctx.exception))


class TestStateVectorExtraction(unittest.TestCase):
    """Test single state vector extraction."""

    def setUp(self):
        spice.kclear()
        self.manager = SpiceManager("kernels")
        self.manager.load_kernels(["kernels/naif0012.tls", "kernels/de440s.bsp"])

    def tearDown(self):
        self.manager.unload_all()
        spice.kclear()

    def test_get_state_vector_success(self):
        state_vec = get_state_vector(
            target="4",
            observer="399",
            utc_timestamp="2026-10-01T12:00:00",
            reference_frame="J2000",
            aberration_correction="LT+S",
        )

        self.assertIsInstance(state_vec, StateVector)
        self.assertEqual(state_vec.target, "4")
        self.assertEqual(state_vec.observer, "399")
        self.assertEqual(state_vec.frame, "J2000")
        self.assertEqual(state_vec.aberration_correction, "LT+S")
        self.assertIsInstance(state_vec.x_km, float)
        self.assertIsInstance(state_vec.vx_km_s, float)
        self.assertGreater(state_vec.light_time_s, 0.0)

        # Mars to Earth distance in km is typically between 50 million and 400 million km
        dist = np.sqrt(state_vec.x_km**2 + state_vec.y_km**2 + state_vec.z_km**2)
        self.assertGreater(dist, 50e6)
        self.assertLess(dist, 400e6)

    def test_get_state_vector_out_of_coverage_reports_error(self):
        with self.assertRaises(EphemerisExtractionError) as ctx:
            get_state_vector(
                target="4",
                observer="399",
                utc_timestamp="3000-01-01T00:00:00",  # Outside DE440S coverage
            )
        self.assertIn("SPICE failed to compute state vector", str(ctx.exception))


class TestTimeSeriesExtraction(unittest.TestCase):
    """Test time-series extraction, failure recording, and endpoint handling."""

    def setUp(self):
        spice.kclear()
        self.manager = SpiceManager("kernels")
        self.manager.load_kernels(["kernels/naif0012.tls", "kernels/de440s.bsp"])

    def tearDown(self):
        self.manager.unload_all()
        spice.kclear()

    def test_time_series_extraction_and_columns(self):
        res = extract_time_series(
            target="4",
            observer="399",
            start_utc="2026-10-01T00:00:00",
            end_utc="2026-10-01T06:00:00",
            step_hours=2.0,
            reference_frame="J2000",
            aberration_correction="NONE",
        )

        self.assertIsInstance(res, TrajectoryExtractionResult)
        df = res.dataframe
        failures = res.failures

        self.assertEqual(list(df.columns), DATAFRAME_COLUMNS)
        self.assertEqual(list(failures.columns), FAILURE_COLUMNS)

        # Steps: 00:00, 02:00, 04:00, 06:00 -> 4 rows
        self.assertEqual(len(df), 4)
        self.assertEqual(res.success_count, 4)
        self.assertEqual(res.failure_count, 0)
        self.assertTrue(failures.empty)

        # Check end time inclusion
        self.assertIn("2026-10-01T06:00:00", df["UTC"].iloc[-1])

    def test_tuple_unpacking(self):
        df, failures = extract_time_series(
            target="301",  # Moon
            observer="399", # Earth
            start_utc="2026-10-01T00:00:00",
            end_utc="2026-10-01T02:00:00",
            step_hours=1.0,
        )
        self.assertIsInstance(df, pd.DataFrame)
        self.assertIsInstance(failures, pd.DataFrame)
        self.assertEqual(len(df), 3)  # 00:00, 01:00, 02:00

    def test_step_hours_validation(self):
        with self.assertRaises(InvalidParameterError):
            extract_time_series(
                target="4",
                observer="399",
                start_utc="2026-10-01T00:00:00",
                end_utc="2026-10-01T06:00:00",
                step_hours=-1.0,
            )

    def test_end_before_start_validation(self):
        with self.assertRaises(InvalidParameterError):
            extract_time_series(
                target="4",
                observer="399",
                start_utc="2026-10-02T00:00:00",
                end_utc="2026-10-01T00:00:00",
                step_hours=1.0,
            )

    def test_failure_tracking_without_zero_replacement(self):
        """Verifies failed samples record errors and NEVER substitute artificial zeros."""
        # Target 999999 has no ephemeris data
        res = extract_time_series(
            target="999999",
            observer="399",
            start_utc="2026-10-01T00:00:00",
            end_utc="2026-10-01T02:00:00",
            step_hours=1.0,
            include_failed_rows_as_nan=True,
        )

        df = res.dataframe
        failures = res.failures

        self.assertEqual(len(failures), 3)
        self.assertEqual(res.failure_count, 3)
        self.assertEqual(res.success_count, 0)

        # Inspect failure records
        for _, row in failures.iterrows():
            self.assertIn("Insufficient ephemeris data", row["Error_Reason"])

        # Crucial: in df, values MUST BE NaN, NEVER 0.0
        for coord in ["X_km", "Y_km", "Z_km", "VX_km_s", "VY_km_s", "VZ_km_s"]:
            self.assertTrue(df[coord].isna().all(), f"Coordinate {coord} contained non-NaN values!")
            # Ensure none are artificial 0.0
            self.assertFalse((df[coord] == 0.0).any(), f"Coordinate {coord} was artificially set to 0.0!")


if __name__ == "__main__":
    unittest.main()
