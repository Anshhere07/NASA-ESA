"""
Example runner script demonstrating SPICE state vector and time-series extraction.

Uses spice_manager and trajectory modules to perform:
1. Single state vector extraction.
2. Time-series extraction across a 24-hour interval with 6-hour sampling.
3. Demonstration of robust failure tracking (without artificial zero substitution).
"""

import pandas as pd
from spice_manager import SpiceManager
from trajectory import get_state_vector, extract_time_series

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 1000)


def main():
    print("=" * 80)
    print("NASA/ESA SPICE TRAJECTORY EXTRACTION DEMO")
    print("=" * 80)

    # 1. Manage kernel lifecycle
    with SpiceManager("kernels") as manager:
        print("\n[1] Loading official generic kernels:")
        manager.load_kernels([
            "kernels/naif0012.tls",
            "kernels/pck00010.tpc",
            "kernels/de440s.bsp",
        ])
        for k in manager.get_loaded_kernels():
            print(f"  - Loaded: {k['file']} (Type: {k['type']})")

        # 2. Single state vector extraction (Mars relative to Earth)
        print("\n[2] Single State Vector: Mars Barycenter (4) relative to Earth (399)")
        state = get_state_vector(
            target="4",
            observer="399",
            utc_timestamp="2026-10-11T12:00:00",
            reference_frame="J2000",
            aberration_correction="LT+S",
        )
        print(f"  UTC Epoch:           {state.utc}")
        print(f"  ET Seconds (J2000):  {state.et_seconds:.3f}")
        print(f"  Position (km):       X={state.x_km:,.2f}, Y={state.y_km:,.2f}, Z={state.z_km:,.2f}")
        print(f"  Velocity (km/s):     VX={state.vx_km_s:,.4f}, VY={state.vy_km_s:,.4f}, VZ={state.vz_km_s:,.4f}")
        print(f"  One-way Light Time:  {state.light_time_s:,.3f} seconds ({state.light_time_s/60:,.2f} minutes)")

        # 3. Time-Series Extraction (Mars relative to Earth)
        print("\n[3] Time-Series Extraction: 2026-10-11 to 2026-10-12 (step: 6.0 hours)")
        result = extract_time_series(
            target="4",
            observer="399",
            start_utc="2026-10-11T00:00:00",
            end_utc="2026-10-12T00:00:00",
            step_hours=6.0,
            reference_frame="J2000",
            aberration_correction="LT+S",
        )
        print("\nDataFrame Preview:")
        print(result.dataframe.to_string(index=False))
        print(f"\nSamples: {result.total_samples} total, {result.success_count} succeeded, {result.failure_count} failed")

        # 4. Failure Handling Demonstration
        print("\n[4] Failure Handling Test (Target 999999 with no ephemeris data):")
        fail_test = extract_time_series(
            target="999999",
            observer="399",
            start_utc="2026-10-11T00:00:00",
            end_utc="2026-10-11T02:00:00",
            step_hours=1.0,
            include_failed_rows_as_nan=True,
        )
        print("\nFailure Records Captured:")
        print(fail_test.failures[["UTC", "Target", "Observer", "Error_Reason"]].to_string(index=False))
        print("\nDataFrame Coordinates with Failed Data (verifying NaN, never 0.0):")
        print(fail_test.dataframe[["UTC", "X_km", "Y_km", "Z_km", "VX_km_s"]].to_string(index=False))


if __name__ == "__main__":
    main()
