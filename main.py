"""
SPICE Trajectory Processing CLI Application.

Provides a beginner-friendly command-line interface to:
1. Inspect available SPICE kernels and their coverage.
2. Validate kernel dependencies (LSK, PCK, SPK).
3. Configure target, observer, time span, sampling interval, and export formats.
4. Run ephemeris extraction using CSPICE spkezr_c via spiceypy.
5. Preview extracted records in the terminal.
6. Display output file locations.
7. Provide bulletproof error handling, timestamped file logging, and guaranteed kernel cleanup.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import spiceypy as spice

from spice_manager import (
    SpiceManager,
    is_lsk_loaded,
    utc_to_et,
    SpiceManagerError,
    KernelNotFoundError,
    KernelReadError,
    MissingKernelError,
    UnsupportedTargetError,
    CoverageError,
    SpiceTimeConversionError,
)
from trajectory import (
    get_state_vector,
    extract_time_series,
    validate_extraction_parameters,
    TrajectoryExtractionResult,
    TrajectoryError,
    InvalidParameterError,
    EphemerisExtractionError,
    VALID_ABCORR,
)
from exporter import (
    export_to_csv,
    export_to_excel,
    generate_text_summary,
    export_text_summary,
)
from workflow import (
    load_and_validate_config,
    SUPPORTED_OUTPUT_FORMATS,
    VALID_OVERWRITE_POLICIES,
)


# ==============================================================================
# Logging Configuration
# ==============================================================================

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("spice_cli")
logger.setLevel(logging.DEBUG)

# File Handler (detailed debug info)
log_file = LOGS_DIR / f"spice_pipeline_{datetime.now().strftime('%Y%m%d')}.log"
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
file_handler.setFormatter(file_formatter)

# Console Handler (clean user messages)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(message)s")
console_handler.setFormatter(console_formatter)

# Avoid duplicate handlers if reloaded
if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


# ==============================================================================
# Helper Functions & UI Display
# ==============================================================================

def print_header(title: str) -> None:
    """Print a visually distinct section header."""
    bar = "=" * 78
    print(f"\n{bar}")
    print(f"  {title.upper()}")
    print(f"{bar}\n")


def inspect_kernels_cli(kernels_dir: str = "kernels") -> None:
    """
    Inspect discovered kernels in the kernel directory and display coverage.
    Guarantees that kernel pool is left in a clean state.
    """
    print_header("Kernel Inventory & Inspection")
    kpath = Path(kernels_dir)
    logger.info(f"Inspecting kernel directory: '{kpath.resolve()}'")

    if not kpath.exists() or not kpath.is_dir():
        msg = f"Kernel directory '{kpath}' does not exist."
        logger.error(msg)
        print(f"[FAIL] Error: {msg}")
        return

    with SpiceManager(kpath) as sm:
        try:
            discovered = sm.discover_kernels()
        except KernelNotFoundError as e:
            logger.error(f"Failed to discover kernels: {e}")
            print(f"[FAIL] Error: {e}")
            return

        total_files = sum(len(files) for files in discovered.values())
        print(f"Discovered {total_files} kernel file(s) in '{kernels_dir}':\n")

        for ktype, files in discovered.items():
            if files:
                print(f"  [{ktype.upper()}] ({len(files)} file{'s' if len(files) > 1 else ''}):")
                for f in files:
                    size_kb = f.stat().st_size / 1024.0
                    print(f"    * {f.name} ({size_kb:,.1f} KB)")
        print()

        # Detailed inspection of SPK files
        spk_files = discovered.get("spk", [])
        if spk_files:
            print("--- SPK Ephemeris Coverage Details ---")
            for spk in spk_files:
                try:
                    obj_ids = sm.get_spk_objects(spk)
                    print(f"\nKernel: {spk.name} ({len(obj_ids)} bodies):")
                    for oid in obj_ids:
                        name = "UNKNOWN"
                        try:
                            name = spice.bodc2n(oid)
                        except Exception:
                            pass
                        cov = sm.get_spk_coverage(spk, oid)
                        intervals_str = ", ".join(
                            f"{iv['start_utc'] or iv['start_et']} to {iv['end_utc'] or iv['end_et']}"
                            for iv in cov
                        )
                        print(f"    ID {oid:6d} ({name:18s}): {intervals_str}")
                except Exception as e:
                    logger.exception(f"Error inspecting SPK file '{spk.name}': {e}")
                    print(f"    [FAIL] Error reading '{spk.name}': {e}")
        else:
            print("[WARN] No SPK (.bsp / .spk) files found in kernel directory.")


def validate_dependencies_cli(kernels_dir: str = "kernels") -> bool:
    """
    Validate that mandatory packages and required kernels are present.
    """
    print_header("System & Kernel Dependency Validation")
    logger.info("Validating system and kernel dependencies...")

    # 1. Package verification
    print("Checking Python packages:")
    packages = [
        ("spiceypy", spice.__version__),
        ("numpy", pd.np.__version__ if hasattr(pd, "np") else "Installed"),
        ("pandas", pd.__version__),
    ]
    for pkg_name, pkg_ver in packages:
        print(f"  [OK] {pkg_name:10s} (Version: {pkg_ver})")

    # CSPICE toolkit version
    try:
        toolkit_ver = spice.tkvrsn("TOOLKIT")
        print(f"  [OK] CSPICE Toolkit: {toolkit_ver}")
    except Exception as e:
        print(f"  [FAIL] CSPICE Toolkit check failed: {e}")
        return False

    # 2. Kernel verification
    print("\nChecking SPICE kernels:")
    kpath = Path(kernels_dir)
    if not kpath.exists() or not kpath.is_dir():
        print(f"  [FAIL] Kernel directory '{kpath}' not found.")
        return False

    with SpiceManager(kpath) as sm:
        try:
            discovered = sm.discover_kernels()
        except Exception as e:
            print(f"  [FAIL] Failed to scan kernel directory: {e}")
            return False

        has_lsk = len(discovered.get("lsk", [])) > 0
        has_spk = len(discovered.get("spk", [])) > 0
        has_pck = len(discovered.get("pck", [])) > 0

        print(f"  {'[OK]' if has_lsk else '[FAIL]'} Leapseconds Kernel (LSK):    {'Found' if has_lsk else 'MISSING'}")
        print(f"  {'[OK]' if has_spk else '[FAIL]'} Planetary Ephemeris (SPK): {'Found' if has_spk else 'MISSING'}")
        print(f"  {'[OK]' if has_pck else '[WARN]'} Planetary Constants (PCK):  {'Found' if has_pck else 'Optional/Missing'}")

        if not (has_lsk and has_spk):
            print("\n[FAIL] Missing required kernels! Ensure both an LSK (.tls) and SPK (.bsp) are present.")
            return False

        # Load and test furnishing
        try:
            for klist in [discovered.get("lsk", []), discovered.get("spk", []), discovered.get("pck", [])]:
                sm.load_kernels(klist)
            sm.validate_required_kernels(require_lsk=True, require_spk=True)
            print("\n[OK] Kernel pool successfully furnished and validated.")
            logger.info("Kernel pool validation succeeded.")
            return True
        except Exception as e:
            logger.exception(f"Kernel pool validation failed: {e}")
            print(f"\n[FAIL] Kernel furnishing failed: {e}")
            return False


def run_pipeline_with_cleanup(config: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Run extraction and export with guaranteed kernel cleanup and comprehensive error trapping.

    Returns:
    (success_flag, result_dict_or_none, error_message_or_none)
    """
    kernel_dir = Path(config.get("kernel_directory", "kernels"))
    output_dir = Path(config.get("output_directory", "output"))

    logger.info(f"Starting pipeline execution with target={config.get('target')}, observer={config.get('observer')}")
    logger.info(f"Time interval: {config.get('start_utc')} to {config.get('end_utc')}, step={config.get('sampling_interval_hours')}h")

    # Guaranteed cleanup via SpiceManager context manager
    with SpiceManager(kernel_dir) as sm:
        try:
            # 1. Discover kernels
            try:
                discovered = sm.discover_kernels()
            except KernelNotFoundError as e:
                err_msg = f"Kernel directory not accessible: {e}"
                logger.error(err_msg)
                return False, None, err_msg

            kernels_to_load = []
            for ktype in ["lsk", "pck", "spk", "fk", "sclk"]:
                kernels_to_load.extend(discovered.get(ktype, []))

            if not kernels_to_load:
                err_msg = f"No kernel files (.tls, .bsp, .tpc) found in '{kernel_dir}'."
                logger.error(err_msg)
                return False, None, err_msg

            # 2. Load kernels safely
            try:
                sm.load_kernels(kernels_to_load)
                sm.validate_required_kernels(require_lsk=True, require_spk=True)
            except MissingKernelError as e:
                err_msg = f"Kernel dependency check failed: {e}"
                logger.error(err_msg)
                return False, None, err_msg
            except KernelReadError as e:
                err_msg = f"Permission or readability error loading kernel: {e}"
                logger.error(err_msg)
                return False, None, err_msg

            # 3. Pre-validate parameters & target coverage
            target = config["target"]
            observer = config["observer"]
            frame = config["reference_frame"]
            abcorr = config["aberration_correction"]

            try:
                validate_extraction_parameters(target, observer, frame, abcorr)
            except InvalidParameterError as e:
                err_msg = f"Invalid extraction parameter: {e}"
                logger.error(err_msg)
                return False, None, err_msg

            # 4. Check timestamps
            start_utc = config["start_utc"]
            end_utc = config["end_utc"]
            try:
                start_et = utc_to_et(start_utc)
                end_et = utc_to_et(end_utc)
            except SpiceTimeConversionError as e:
                err_msg = f"Timestamp conversion error: {e}"
                logger.error(err_msg)
                return False, None, err_msg
            except ValueError as e:
                err_msg = f"Malformed date or time value: {e}"
                logger.error(err_msg)
                return False, None, err_msg

            if end_et < start_et:
                err_msg = f"End time '{end_utc}' is earlier than start time '{start_utc}'."
                logger.error(err_msg)
                return False, None, err_msg

            # 5. Execute extraction
            try:
                res: TrajectoryExtractionResult = extract_time_series(
                    target=target,
                    observer=observer,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    step_hours=float(config["sampling_interval_hours"]),
                    reference_frame=frame,
                    aberration_correction=abcorr,
                    include_failed_rows_as_nan=True,
                )
            except EphemerisExtractionError as e:
                err_msg = f"SPICE Ephemeris calculation failed: {e}"
                logger.error(err_msg)
                return False, None, err_msg
            except Exception as e:
                err_msg = f"Unexpected error during extraction: {e}"
                logger.exception(err_msg)
                return False, None, err_msg

            df = res.dataframe
            failures_df = res.failures

            if res.success_count == 0:
                failure_reasons = "\n".join(
                    f"* Epoch {row['UTC']}: {row['Error_Reason']}"
                    for _, row in failures_df.head(3).iterrows()
                )
                err_msg = (
                    f"Extraction failed for all {res.total_samples} requested sample(s).\n"
                    f"Primary SPICE error reasons:\n{failure_reasons}"
                )
                logger.warning(err_msg)
                return False, None, err_msg

            # 6. Export outputs
            output_dir.mkdir(parents=True, exist_ok=True)
            overwrite_policy = config.get("overwrite_policy", "overwrite")
            formats = [f.lower().strip() for f in config.get("output_formats", ["csv", "xlsx", "txt"])]
            exported_files: Dict[str, Path] = {}

            csv_name = config.get("csv_filename", "spice_trajectory.csv")
            xlsx_name = config.get("xlsx_filename", "spice_trajectory.xlsx")
            summary_name = config.get("summary_filename", "trajectory_summary.txt")

            try:
                if "csv" in formats:
                    csv_path = output_dir / csv_name
                    p = export_to_csv(df, csv_path, overwrite_policy=overwrite_policy)
                    if p:
                        exported_files["csv"] = p

                if "xlsx" in formats:
                    xlsx_path = output_dir / xlsx_name
                    p = export_to_excel(
                        df,
                        xlsx_path,
                        sheet_name="SPICE_Trajectory",
                        overwrite_policy=overwrite_policy,
                        failures_df=failures_df,
                    )
                    if p:
                        exported_files["xlsx"] = p

                summary_text = generate_text_summary(df, config, failures_df=failures_df)
                if "txt" in formats:
                    txt_path = output_dir / summary_name
                    p = export_text_summary(summary_text, txt_path, overwrite_policy=overwrite_policy)
                    if p:
                        exported_files["txt"] = p
            except PermissionError as e:
                err_msg = f"Permission denied writing output file (ensure file is not open in Excel): {e}"
                logger.error(err_msg)
                return False, None, err_msg
            except FileExistsError as e:
                err_msg = f"Export blocked by overwrite policy: {e}"
                logger.error(err_msg)
                return False, None, err_msg
            except Exception as e:
                err_msg = f"Export failure: {e}"
                logger.exception(err_msg)
                return False, None, err_msg

            result_data = {
                "config": config,
                "dataframe": df,
                "failures": failures_df,
                "summary": summary_text,
                "exported_files": exported_files,
                "total_samples": res.total_samples,
                "success_count": res.success_count,
                "failure_count": res.failure_count,
            }
            logger.info("Extraction and export completed successfully.")
            return True, result_data, None

        except Exception as e:
            err_msg = f"Unhandled error in workflow: {e}"
            logger.exception(err_msg)
            return False, None, err_msg
        finally:
            # SM context manager guarantees sm.unload_all() here
            logger.info("Controlled kernel cleanup completed (unloaded session kernels).")


# ==============================================================================
# Interactive Guided Wizard
# ==============================================================================

def prompt_with_default(prompt: str, default: Any) -> str:
    """Prompt the user with a default fallback."""
    val = input(f"{prompt} [{default}]: ").strip()
    return val if val else str(default)


def interactive_wizard() -> None:
    """Beginner-friendly interactive wizard to configure and run extraction."""
    print_header("SPICE Ephemeris Extraction Wizard")
    print("This wizard will guide you through configuring and running your extraction.\n")

    # Load defaults from config.json
    try:
        cfg = load_and_validate_config("config.json")
    except Exception as e:
        logger.warning(f"Could not load config.json ({e}). Using built-in defaults.")
        cfg = {
            "target": "4",
            "target_name": "MARS BARYCENTER",
            "observer": "399",
            "observer_name": "EARTH",
            "reference_frame": "J2000",
            "aberration_correction": "LT+S",
            "start_utc": "2026-10-11T00:00:00",
            "end_utc": "2026-10-12T00:00:00",
            "sampling_interval_hours": 1.0,
            "output_formats": ["csv", "xlsx", "txt"],
            "kernel_directory": "kernels",
            "output_directory": "output",
            "overwrite_policy": "overwrite",
        }

    # Step 1: Target and Observer
    print("--- 1. Target & Observer Configuration ---")
    cfg["target"] = prompt_with_default("Enter Target NAIF ID or Name (e.g. 4, 301, MARS BARYCENTER)", cfg["target"])
    cfg["observer"] = prompt_with_default("Enter Observer NAIF ID or Name (e.g. 399, EARTH, 0)", cfg["observer"])
    cfg["reference_frame"] = prompt_with_default("Enter Reference Frame (e.g. J2000, ECLIPJ2000)", cfg["reference_frame"]).upper()
    cfg["aberration_correction"] = prompt_with_default("Enter Aberration Correction (NONE, LT, LT+S)", cfg["aberration_correction"]).upper()

    # Step 2: Time Span & Sampling
    print("\n--- 2. Time Span & Sampling ---")
    cfg["start_utc"] = prompt_with_default("Enter Start UTC timestamp (YYYY-MM-DDTHH:MM:SS)", cfg["start_utc"])
    cfg["end_utc"] = prompt_with_default("Enter End UTC timestamp (YYYY-MM-DDTHH:MM:SS)", cfg["end_utc"])
    while True:
        step_input = prompt_with_default("Enter Sampling Interval in hours (e.g. 1.0, 0.5, 6)", cfg["sampling_interval_hours"])
        try:
            val = float(step_input)
            if val <= 0:
                print("Interval must be a positive number.")
                continue
            cfg["sampling_interval_hours"] = val
            break
        except ValueError:
            print("Please enter a valid floating-point number.")

    # Step 3: Output Formats
    print("\n--- 3. Output Formats ---")
    print("Choose output formats:")
    print("  1. CSV only")
    print("  2. Excel (.xlsx) only")
    print("  3. Both CSV and Excel (plus text summary)")
    fmt_choice = prompt_with_default("Select choice (1, 2, or 3)", "3")
    if fmt_choice == "1":
        cfg["output_formats"] = ["csv", "txt"]
    elif fmt_choice == "2":
        cfg["output_formats"] = ["xlsx", "txt"]
    else:
        cfg["output_formats"] = ["csv", "xlsx", "txt"]

    cfg["output_directory"] = prompt_with_default("Enter output directory", cfg.get("output_directory", "output"))

    print("\nConfiguration Summary:")
    print(f"  * Target:   {cfg['target']} relative to Observer: {cfg['observer']}")
    print(f"  * Interval: {cfg['start_utc']} to {cfg['end_utc']} (every {cfg['sampling_interval_hours']} hours)")
    print(f"  * Frame:    {cfg['reference_frame']} with Correction: {cfg['aberration_correction']}")
    print(f"  * Formats:  {', '.join(cfg['output_formats']).upper()} -> '{cfg['output_directory']}/'")

    proceed = prompt_with_default("Run extraction now? (y/n)", "y").lower()
    if proceed not in ("y", "yes"):
        print("Extraction cancelled.")
        return

    # Execute
    print("\nExecuting extraction...")
    success, result_data, error_msg = run_pipeline_with_cleanup(cfg)

    if not success:
        print(f"\n[FAIL] Extraction Failed:\n{error_msg}")
        return

    # Step 8: Preview Extracted Records
    print_header("Extraction Succeeded - Results Preview")
    df = result_data["dataframe"]
    print(f"Total samples: {result_data['total_samples']} | Successful: {result_data['success_count']} | Failed: {result_data['failure_count']}\n")
    print("Preview of first 5 records:")
    print(df.head(5).to_string(index=False))

    # Step 9: Display output file locations
    print_header("Exported Output Files")
    for fmt, path in result_data["exported_files"].items():
        size_kb = path.stat().st_size / 1024.0
        print(f"  [OK] [{fmt.upper()}] -> {path.resolve()} ({size_kb:,.1f} KB)")
    print()


# ==============================================================================
# Main CLI Router
# ==============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="NASA/ESA SPICE Ephemeris Trajectory Extraction Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--inspect", action="store_true", help="Inspect available SPICE kernels and exit.")
    parser.add_argument("--validate", action="store_true", help="Validate system dependencies and kernel availability.")
    parser.add_argument("--run", action="store_true", help="Run extraction using configuration without interactive wizard.")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config.json (default: config.json).")
    parser.add_argument("--target", type=str, help="Override target body identifier.")
    parser.add_argument("--observer", type=str, help="Override observer body identifier.")
    parser.add_argument("--start", type=str, help="Override start UTC timestamp.")
    parser.add_argument("--end", type=str, help="Override end UTC timestamp.")
    parser.add_argument("--step", type=float, help="Override sampling interval in hours.")
    parser.add_argument("--formats", type=str, help="Comma-separated output formats (e.g. csv,xlsx,txt).")
    parser.add_argument("--wizard", action="store_true", help="Launch the step-by-step interactive wizard.")

    args = parser.parse_args()

    # Route 1: Inspect
    if args.inspect:
        inspect_kernels_cli("kernels")
        return 0

    # Route 2: Validate
    if args.validate:
        ok = validate_dependencies_cli("kernels")
        return 0 if ok else 1

    # Route 3: Direct Run (non-interactive or automated)
    if args.run:
        try:
            cfg = load_and_validate_config(args.config)
        except Exception as e:
            logger.error(f"Failed to load configuration '{args.config}': {e}")
            print(f"[FAIL] Configuration error: {e}")
            return 1

        # Apply CLI overrides if supplied
        if args.target:
            cfg["target"] = args.target
        if args.observer:
            cfg["observer"] = args.observer
        if args.start:
            cfg["start_utc"] = args.start
        if args.end:
            cfg["end_utc"] = args.end
        if args.step:
            cfg["sampling_interval_hours"] = args.step
        if args.formats:
            cfg["output_formats"] = [f.strip() for f in args.formats.split(",")]

        success, result_data, error_msg = run_pipeline_with_cleanup(cfg)
        if not success:
            print(f"\n[FAIL] Pipeline execution failed:\n{error_msg}")
            return 1

        print_header("Trajectory Extraction Summary")
        print(result_data["summary"])
        print("\nPreview of extracted records:")
        print(result_data["dataframe"].head(5).to_string(index=False))

        print_header("Exported Files")
        for fmt, p in result_data["exported_files"].items():
            print(f"  [OK] [{fmt.upper()}] -> {p.resolve()}")
        return 0

    # Route 4: Wizard / Interactive
    if args.wizard or sys.stdin.isatty():
        interactive_wizard()
        return 0

    # Default fallback: show summary and usage
    parser.print_help()
    return 0


# ==============================================================================
# Cloud & Serverless Deployment Entrypoints
# ==============================================================================
try:
    from server import SpicePortalExtendedHandler, handler, app, application
except ImportError:
    pass


if __name__ == "__main__":
    sys.exit(main())
