"""
SPICE Extraction Workflow Orchestrator Module.

Loads configuration from config.json, validates parameters, manages the SPICE kernel
lifecycle, executes ephemeris extractions, and exports formatted CSV, Excel, and summary reports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd

from spice_manager import SpiceManager, validate_utc_timestamp_format, utc_to_et
from trajectory import extract_time_series, TrajectoryExtractionResult
from exporter import export_to_csv, export_to_excel, generate_text_summary, export_text_summary


REQUIRED_CONFIG_KEYS = {
    "target",
    "observer",
    "reference_frame",
    "aberration_correction",
    "start_utc",
    "end_utc",
    "sampling_interval_hours",
    "output_formats",
    "kernel_directory",
    "output_directory",
}

SUPPORTED_OUTPUT_FORMATS = {"csv", "xlsx", "txt"}
VALID_OVERWRITE_POLICIES = {"overwrite", "error", "skip"}


def load_and_validate_config(config_source: Union[str, Path, Dict[str, Any]] = "config.json") -> Dict[str, Any]:
    """
    Load and validate configuration dictionary or JSON file.

    Enforces mandatory keys, valid data types, positive sampling intervals,
    valid UTC formats, supported export formats, and existing kernel directory.
    """
    if isinstance(config_source, (str, Path)):
        cfg_path = Path(config_source).resolve()
        if not cfg_path.exists():
            raise FileNotFoundError(f"Configuration file not found: '{config_source}'")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                config: Dict[str, Any] = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Malformed JSON in configuration file '{config_source}': {e}") from e
    elif isinstance(config_source, dict):
        config = dict(config_source)
    else:
        raise TypeError(f"config_source must be Path, str, or dict, got {type(config_source).__name__}")

    # Check for required fields
    missing_keys = REQUIRED_CONFIG_KEYS - set(config.keys())
    if missing_keys:
        raise ValueError(
            f"Configuration is missing mandatory fields: {', '.join(sorted(missing_keys))}."
        )

    # Validate interval
    step_hours = config.get("sampling_interval_hours")
    if not isinstance(step_hours, (int, float)) or step_hours <= 0:
        raise ValueError(
            f"Invalid 'sampling_interval_hours': {step_hours}. Must be a positive number."
        )

    # Validate formats
    formats = config.get("output_formats", [])
    if not isinstance(formats, list) or len(formats) == 0:
        raise ValueError("'output_formats' must be a non-empty list of formats (e.g. ['csv', 'xlsx', 'txt']).")

    invalid_formats = set(fmt.lower() for fmt in formats) - SUPPORTED_OUTPUT_FORMATS
    if invalid_formats:
        raise ValueError(
            f"Unsupported output format(s): {', '.join(sorted(invalid_formats))}. "
            f"Supported formats are: {', '.join(sorted(SUPPORTED_OUTPUT_FORMATS))}."
        )

    # Validate overwrite policy
    overwrite_policy = config.get("overwrite_policy", "overwrite").lower().strip()
    if overwrite_policy not in VALID_OVERWRITE_POLICIES:
        raise ValueError(
            f"Invalid 'overwrite_policy': '{overwrite_policy}'. "
            f"Must be one of: {', '.join(sorted(VALID_OVERWRITE_POLICIES))}."
        )
    config["overwrite_policy"] = overwrite_policy

    # Validate kernel directory
    kernel_dir = Path(config.get("kernel_directory", "kernels")).resolve()
    if not kernel_dir.exists() or not kernel_dir.is_dir():
        raise FileNotFoundError(
            f"Specified kernel directory does not exist: '{config.get('kernel_directory')}'."
        )

    # Validate timestamps
    validate_utc_timestamp_format(config["start_utc"])
    validate_utc_timestamp_format(config["end_utc"])

    return config


def run_workflow(config_source: Union[str, Path, Dict[str, Any]] = "config.json") -> Dict[str, Any]:
    """
    Execute the complete configurable SPICE extraction and export workflow.

    1. Loads and validates configuration.
    2. Discovers and loads SPICE kernels using SpiceManager.
    3. Executes time-series ephemeris extraction.
    4. Automatically creates the output directory.
    5. Exports to CSV, Excel, and Text Summary adhering to the overwrite policy.
    6. Returns execution dictionary with DataFrame, failures, summary, and exported file paths.
    """
    config = load_and_validate_config(config_source)

    kernel_dir = Path(config["kernel_directory"])
    output_dir = Path(config["output_directory"])
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_name = config.get("csv_filename", "spice_trajectory.csv")
    xlsx_name = config.get("xlsx_filename", "spice_trajectory.xlsx")
    summary_name = config.get("summary_filename", "trajectory_summary.txt")

    overwrite_policy = config.get("overwrite_policy", "overwrite")
    formats = [f.lower().strip() for f in config["output_formats"]]

    exported_files: Dict[str, Path] = {}

    with SpiceManager(kernel_dir) as sm:
        # Discover and furnish kernels
        discovered = sm.discover_kernels()
        kernels_to_load = []
        for ktype in ["lsk", "pck", "spk", "fk", "sclk"]:
            kernels_to_load.extend(discovered.get(ktype, []))

        if not kernels_to_load:
            raise FileNotFoundError(
                f"No usable SPICE kernels discovered in directory '{kernel_dir}'."
            )

        sm.load_kernels(kernels_to_load)
        sm.validate_required_kernels(require_lsk=True, require_spk=True)

        # Execute extraction
        res: TrajectoryExtractionResult = extract_time_series(
            target=config["target"],
            observer=config["observer"],
            start_utc=config["start_utc"],
            end_utc=config["end_utc"],
            step_hours=float(config["sampling_interval_hours"]),
            reference_frame=config["reference_frame"],
            aberration_correction=config["aberration_correction"],
            include_failed_rows_as_nan=True,
        )

        df = res.dataframe
        failures_df = res.failures

        # Exports
        if "csv" in formats:
            csv_path = output_dir / csv_name
            out_csv = export_to_csv(df, csv_path, overwrite_policy=overwrite_policy)
            if out_csv:
                exported_files["csv"] = out_csv

        if "xlsx" in formats:
            xlsx_path = output_dir / xlsx_name
            out_xlsx = export_to_excel(
                df,
                xlsx_path,
                sheet_name="SPICE_Trajectory",
                overwrite_policy=overwrite_policy,
                failures_df=failures_df,
            )
            if out_xlsx:
                exported_files["xlsx"] = out_xlsx

        summary_text = generate_text_summary(df, config, failures_df=failures_df)
        if "txt" in formats:
            txt_path = output_dir / summary_name
            out_txt = export_text_summary(summary_text, txt_path, overwrite_policy=overwrite_policy)
            if out_txt:
                exported_files["txt"] = out_txt

    return {
        "config": config,
        "dataframe": df,
        "failures": failures_df,
        "summary": summary_text,
        "exported_files": exported_files,
        "success_count": res.success_count,
        "failure_count": res.failure_count,
        "total_samples": res.total_samples,
    }


if __name__ == "__main__":
    result = run_workflow("config.json")
    print(result["summary"])
    print("Exported files:")
    for fmt, p in result["exported_files"].items():
        print(f"  [{fmt.upper()}] -> {p}")
