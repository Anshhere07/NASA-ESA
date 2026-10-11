"""
SPICE Trajectory and State Vector Extraction Module.

Provides robust state vector and time-series ephemeris extraction using CSPICE spkezr_c
via spiceypy. Adheres strictly to NASA/ESA SPICE conventions, rigorous input validation,
and failure tracking without artificial zero replacement.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import spiceypy as spice
from spiceypy.utils.exceptions import SpiceyError

from spice_manager import (
    SpiceManager,
    is_lsk_loaded,
    utc_to_et,
    SpiceManagerError,
    MissingKernelError,
    UnsupportedTargetError,
    CoverageError,
    SpiceTimeConversionError,
)


# ==============================================================================
# Exceptions
# ==============================================================================

class TrajectoryError(SpiceManagerError):
    """Base exception for trajectory extraction failures."""
    pass


class InvalidParameterError(TrajectoryError):
    """Raised when an extraction argument (target, observer, frame, abcorr) is invalid."""
    pass


class EphemerisExtractionError(TrajectoryError):
    """Raised when SPICE fails to compute the state vector."""
    pass


# ==============================================================================
# Standard Constants & Validations
# ==============================================================================

VALID_ABCORR = {
    "NONE",
    "LT",
    "LT+S",
    "CN",
    "CN+S",
    "XLT",
    "XLT+S",
    "XCN",
    "XCN+S",
}

DATAFRAME_COLUMNS = [
    "UTC",
    "ET_seconds",
    "Target",
    "Observer",
    "Frame",
    "X_km",
    "Y_km",
    "Z_km",
    "VX_km_s",
    "VY_km_s",
    "VZ_km_s",
]

FAILURE_COLUMNS = [
    "UTC",
    "ET_seconds",
    "Target",
    "Observer",
    "Frame",
    "Error_Reason",
]


@dataclass
class StateVector:
    """Represents a 6-component state vector with metadata."""
    utc: str
    et_seconds: float
    target: str
    observer: str
    frame: str
    aberration_correction: str
    x_km: float
    y_km: float
    z_km: float
    vx_km_s: float
    vy_km_s: float
    vz_km_s: float
    light_time_s: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)


@dataclass
class TrajectoryExtractionResult:
    """
    Result container for time-series extraction.

    Supports both attribute access (.dataframe, .failures) and tuple unpacking:
    df, failures = extract_time_series(...)
    """
    dataframe: pd.DataFrame
    failures: pd.DataFrame

    @property
    def total_samples(self) -> int:
        """Total number of evaluated timestamps."""
        return len(self.dataframe) if not self.dataframe.empty else len(self.failures)

    @property
    def success_count(self) -> int:
        """Number of successfully extracted state samples."""
        if self.dataframe.empty:
            return 0
        return int(self.dataframe["X_km"].notna().sum())

    @property
    def failure_count(self) -> int:
        """Number of failed samples."""
        return len(self.failures)

    def __iter__(self):
        """Allow tuple unpacking: df, failures = result."""
        yield self.dataframe
        yield self.failures


# ==============================================================================
# Parameter Validation Functions
# ==============================================================================

def validate_extraction_parameters(
    target: Union[int, str],
    observer: Union[int, str],
    reference_frame: str,
    aberration_correction: str,
) -> Tuple[str, str, str, str]:
    """
    Validate target, observer, reference frame, and aberration correction parameters.

    Returns sanitized uppercase strings: (target_str, observer_str, frame_str, abcorr_str).
    Raises InvalidParameterError or MissingKernelError if invalid.
    """
    # 1. Target & Observer presence
    if target is None or (isinstance(target, str) and not target.strip()):
        raise InvalidParameterError("Target identifier cannot be empty or None.")
    if observer is None or (isinstance(observer, str) and not observer.strip()):
        raise InvalidParameterError("Observer identifier cannot be empty or None.")

    target_str = str(target).strip()
    observer_str = str(observer).strip()

    if target_str.upper() == observer_str.upper():
        raise InvalidParameterError(
            f"Target and Observer cannot be the same body ('{target_str}')."
        )

    # 2. Reference frame validation
    if not isinstance(reference_frame, str) or not reference_frame.strip():
        raise InvalidParameterError("Reference frame must be a non-empty string.")

    frame_str = reference_frame.strip().upper()
    try:
        frame_id = spice.namfrm(frame_str)
        if frame_id == 0:
            raise InvalidParameterError(
                f"Reference frame '{reference_frame}' is not recognized by SPICE. "
                "Ensure standard frame (e.g. 'J2000', 'ECLIPJ2000') or load an appropriate Frame Kernel (FK)."
            )
    except SpiceyError as e:
        raise InvalidParameterError(f"Error checking frame '{reference_frame}': {e}") from e

    # 3. Aberration correction validation
    if not isinstance(aberration_correction, str) or not aberration_correction.strip():
        raise InvalidParameterError("Aberration correction must be a non-empty string.")

    abcorr_str = aberration_correction.strip().upper()
    if abcorr_str not in VALID_ABCORR:
        raise InvalidParameterError(
            f"Aberration correction '{aberration_correction}' is not recognized. "
            f"Valid options are: {', '.join(sorted(VALID_ABCORR))}."
        )

    # 4. Kernel pool prerequisites
    if not is_lsk_loaded():
        raise MissingKernelError(
            "Leap-seconds kernel (LSK) is not loaded in the active SPICE pool. "
            "Please furnish an LSK (e.g. 'naif0012.tls') before extraction."
        )

    if spice.ktotal("SPK") == 0:
        raise MissingKernelError(
            "No Spacecraft/Planetary Ephemeris Kernel (SPK) is loaded in the active SPICE pool. "
            "Please furnish an SPK (e.g. 'de440s.bsp') before extraction."
        )

    return target_str, observer_str, frame_str, abcorr_str


# ==============================================================================
# Step 5: Single State Vector Extraction
# ==============================================================================

def get_state_vector(
    target: Union[int, str],
    observer: Union[int, str],
    utc_timestamp: str,
    reference_frame: str = "J2000",
    aberration_correction: str = "NONE",
) -> StateVector:
    """
    Retrieve the 6-component state vector (position & velocity) using spice.spkezr().

    Parameters:
    - target: Target body or spacecraft identifier (e.g. '4', 'MARS BARYCENTER', 399).
    - observer: Observer identifier (e.g. '399', 'EARTH', '0').
    - utc_timestamp: UTC timestamp string (e.g. '2026-10-01T23:00:00').
    - reference_frame: Reference frame name (default: 'J2000').
    - aberration_correction: SPICE aberration correction flag (default: 'NONE').

    Returns:
    - StateVector instance with position in km and velocity in km/s.
    """
    targ_str, obs_str, frame_str, abcorr_str = validate_extraction_parameters(
        target=target,
        observer=observer,
        reference_frame=reference_frame,
        aberration_correction=aberration_correction,
    )

    # Convert UTC to Ephemeris Time (ET)
    et = utc_to_et(utc_timestamp)

    try:
        state, lt = spice.spkezr(
            targ_str,
            et,
            frame_str,
            abcorr_str,
            obs_str,
        )
    except SpiceyError as e:
        raise EphemerisExtractionError(
            f"SPICE failed to compute state vector for target '{targ_str}' "
            f"relative to observer '{obs_str}' in frame '{frame_str}' "
            f"at UTC {utc_timestamp} (ET={et}): {e}"
        ) from e

    return StateVector(
        utc=utc_timestamp,
        et_seconds=float(et),
        target=targ_str,
        observer=obs_str,
        frame=frame_str,
        aberration_correction=abcorr_str,
        x_km=float(state[0]),
        y_km=float(state[1]),
        z_km=float(state[2]),
        vx_km_s=float(state[3]),
        vy_km_s=float(state[4]),
        vz_km_s=float(state[5]),
        light_time_s=float(lt),
    )


# ==============================================================================
# Step 6: Configurable Time-Series Extraction
# ==============================================================================

def extract_time_series(
    target: Union[int, str],
    observer: Union[int, str],
    start_utc: str,
    end_utc: str,
    step_hours: float,
    reference_frame: str = "J2000",
    aberration_correction: str = "NONE",
    include_failed_rows_as_nan: bool = True,
    include_exact_end: bool = False,
) -> TrajectoryExtractionResult:
    """
    Extract a time-series of 6-component state vectors into a Pandas DataFrame.

    End Time Handling:
    - Steps increment by `step_hours * 3600.0` seconds starting from `start_utc`.
    - An epoch is evaluated as long as `current_et <= end_et + 1e-4` seconds.
    - If `end_et` coincides with a step boundary, it is INCLUDED.
    - If `include_exact_end=True` and `end_et` does not fall on an exact step,
      `end_et` is evaluated as the final sample point.

    Failure Handling:
    - Failed samples are tracked with their timestamps and specific error reasons in `failures`.
    - If `include_failed_rows_as_nan=True`, failed rows are present in the DataFrame with
      `np.nan` values for the 6 state components.
    - NEVER replaces failed data with artificial zeros (0.0).

    Columns in DataFrame:
    UTC, ET_seconds, Target, Observer, Frame, X_km, Y_km, Z_km, VX_km_s, VY_km_s, VZ_km_s

    Returns:
    - TrajectoryExtractionResult(dataframe=df, failures=failures_df)
    """
    targ_str, obs_str, frame_str, abcorr_str = validate_extraction_parameters(
        target=target,
        observer=observer,
        reference_frame=reference_frame,
        aberration_correction=aberration_correction,
    )

    if step_hours <= 0:
        raise InvalidParameterError(
            f"Sampling interval 'step_hours' must be positive, got {step_hours}."
        )

    start_et = utc_to_et(start_utc)
    end_et = utc_to_et(end_utc)

    if end_et < start_et:
        raise InvalidParameterError(
            f"End timestamp '{end_utc}' (ET={end_et}) precedes start timestamp '{start_utc}' (ET={start_et})."
        )

    step_seconds = float(step_hours * 3600.0)

    # Build epoch sequence
    epochs: List[Tuple[float, Optional[str]]] = []
    cur_et = start_et

    # Numerical tolerance for endpoint inclusion (1 millisecond)
    tol = 1e-3

    while cur_et <= end_et + tol:
        utc_str = start_utc if len(epochs) == 0 else None
        epochs.append((cur_et, utc_str))
        cur_et += step_seconds

    if include_exact_end and (epochs[-1][0] < end_et - tol):
        epochs.append((end_et, end_utc))

    records: List[Dict[str, Any]] = []
    failure_records: List[Dict[str, Any]] = []

    for et, precomputed_utc in epochs:
        # Determine UTC representation for this epoch
        if precomputed_utc is not None:
            utc_str = precomputed_utc
        else:
            try:
                utc_str = spice.et2utc(et, "ISOC", 3)
            except SpiceyError:
                utc_str = f"ET_{et:.3f}"

        try:
            state, _ = spice.spkezr(
                targ_str,
                et,
                frame_str,
                abcorr_str,
                obs_str,
            )
            records.append({
                "UTC": utc_str,
                "ET_seconds": float(et),
                "Target": targ_str,
                "Observer": obs_str,
                "Frame": frame_str,
                "X_km": float(state[0]),
                "Y_km": float(state[1]),
                "Z_km": float(state[2]),
                "VX_km_s": float(state[3]),
                "VY_km_s": float(state[4]),
                "VZ_km_s": float(state[5]),
            })
        except SpiceyError as e:
            error_reason = str(e).strip()
            failure_records.append({
                "UTC": utc_str,
                "ET_seconds": float(et),
                "Target": targ_str,
                "Observer": obs_str,
                "Frame": frame_str,
                "Error_Reason": error_reason,
            })

            if include_failed_rows_as_nan:
                # Record with NaN, NEVER artificial zeros
                records.append({
                    "UTC": utc_str,
                    "ET_seconds": float(et),
                    "Target": targ_str,
                    "Observer": obs_str,
                    "Frame": frame_str,
                    "X_km": np.nan,
                    "Y_km": np.nan,
                    "Z_km": np.nan,
                    "VX_km_s": np.nan,
                    "VY_km_s": np.nan,
                    "VZ_km_s": np.nan,
                })

    df = pd.DataFrame(records, columns=DATAFRAME_COLUMNS)
    failures_df = pd.DataFrame(failure_records, columns=FAILURE_COLUMNS)

    return TrajectoryExtractionResult(dataframe=df, failures=failures_df)
