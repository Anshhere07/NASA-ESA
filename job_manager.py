"""
SPICE Extraction Job Orchestrator & Isolation Runner.

Provides:
1. Asynchronous execution of extraction jobs without blocking web server.
2. Complete job isolation (unique job ID, dedicated workspace, isolated outputs).
3. Thread-safe execution using a serial worker queue to guarantee that CSPICE
   global kernel pool operations remain strictly isolated and never collide.
4. Detailed stage and progress reporting (percentage, sample counts, failure logging).
5. Multi-format exports (CSV, Excel, Text Summary, JSON) reusing existing export logic.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from spice_manager import (
    SpiceManager,
    utc_to_et,
    is_lsk_loaded,
    MissingKernelError,
    UnsupportedTargetError,
    CoverageError,
    SpiceTimeConversionError,
)
from trajectory import (
    extract_time_series,
    validate_extraction_parameters,
    TrajectoryExtractionResult,
    InvalidParameterError,
    EphemerisExtractionError,
    DATAFRAME_COLUMNS,
)
from exporter import (
    export_to_csv,
    export_to_excel,
    generate_text_summary,
    export_text_summary,
)


JOBS_BASE_DIR = Path("output") / "jobs"
JOBS_BASE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class JobRecord:
    job_id: str
    status: str  # 'PENDING', 'RUNNING', 'COMPLETED', 'FAILED'
    stage: str
    progress_pct: int
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)
    total_samples: int = 0
    success_count: int = 0
    failure_count: int = 0
    exported_files: Dict[str, str] = field(default_factory=dict)
    error_message: Optional[str] = None
    summary_text: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class JobManager:
    """Manages asynchronous extraction jobs with guaranteed execution isolation."""

    def __init__(self, max_history: int = 100):
        self._jobs: Dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        # Single-worker executor guarantees serialized CSPICE kernel pool isolation
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="SpiceJobWorker")
        self._results_cache: Dict[str, pd.DataFrame] = {}

    def submit_job(self, config: Dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex[:8]
        now = datetime.now().isoformat()

        record = JobRecord(
            job_id=job_id,
            status="PENDING",
            stage="Queued in processing queue",
            progress_pct=0,
            created_at=now,
            config=dict(config),
        )

        with self._lock:
            self._jobs[job_id] = record

        # Dispatch to worker thread
        self._executor.submit(self._run_job_worker, job_id, config)
        return job_id

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [job.to_dict() for job in reversed(list(self._jobs.values()))]

    def get_job_dataframe(self, job_id: str) -> Optional[pd.DataFrame]:
        with self._lock:
            if job_id in self._results_cache:
                return self._results_cache[job_id]

        job_dir = JOBS_BASE_DIR / job_id
        csv_path = job_dir / "spice_trajectory.csv"
        if csv_path.exists():
            df = pd.read_csv(csv_path)
            with self._lock:
                self._results_cache[job_id] = df
            return df
        return None

    def _update_job(
        self,
        job_id: str,
        status: Optional[str] = None,
        stage: Optional[str] = None,
        progress: Optional[int] = None,
        error_msg: Optional[str] = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if status is not None:
                job.status = status
            if stage is not None:
                job.stage = stage
            if progress is not None:
                job.progress_pct = progress
            if error_msg is not None:
                job.error_message = error_msg

    def _run_job_worker(self, job_id: str, config: Dict[str, Any]) -> None:
        """Isolated worker thread execution."""
        start_time = datetime.now().isoformat()
        self._update_job(job_id, status="RUNNING", stage="Initializing SPICE environment", progress=5)

        with self._lock:
            job = self._jobs[job_id]
            job.started_at = start_time

        job_dir = JOBS_BASE_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        kernel_dir = Path(config.get("kernel_directory", "kernels"))

        # Scoped SpiceManager ensures full kernel isolation and cleanup
        with SpiceManager(kernel_dir) as sm:
            try:
                # 1. Discover kernels
                self._update_job(job_id, stage="Discovering available kernels", progress=15)
                discovered = sm.discover_kernels()
                kernels_to_load = []
                for ktype in ["lsk", "pck", "spk", "fk", "sclk"]:
                    kernels_to_load.extend(discovered.get(ktype, []))

                # Also load any custom uploaded kernels for this job
                custom_kernel_paths = config.get("custom_kernel_paths", [])
                for ckp in custom_kernel_paths:
                    cp = Path(ckp)
                    if cp.exists() and cp not in kernels_to_load:
                        kernels_to_load.append(cp)

                if not kernels_to_load:
                    raise MissingKernelError(f"No kernel files found in '{kernel_dir}' or job upload path.")

                # 2. Furnish kernels
                self._update_job(job_id, stage="Furnishing SPICE kernels into pool", progress=25)
                sm.load_kernels(kernels_to_load)
                sm.validate_required_kernels(require_lsk=True, require_spk=True)

                # 3. Parameter validation
                self._update_job(job_id, stage="Validating target, observer, and frame", progress=35)
                target = config["target"]
                observer = config["observer"]
                frame = config["reference_frame"]
                abcorr = config["aberration_correction"]

                validate_extraction_parameters(target, observer, frame, abcorr)

                # 4. Extract time-series
                self._update_job(job_id, stage="Calculating planetary state vectors", progress=50)
                start_utc = config["start_utc"]
                end_utc = config["end_utc"]
                step_hours = float(config["sampling_interval_hours"])

                res: TrajectoryExtractionResult = extract_time_series(
                    target=target,
                    observer=observer,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    step_hours=step_hours,
                    reference_frame=frame,
                    aberration_correction=abcorr,
                    include_failed_rows_as_nan=True,
                )

                self._update_job(job_id, stage="Generating output datasets", progress=80)
                df = res.dataframe
                failures_df = res.failures

                if res.success_count == 0:
                    err_msg = f"Extraction failed for all {res.total_samples} sample(s)."
                    if not failures_df.empty:
                        err_msg += f" SPICE error: {failures_df['Error_Reason'].iloc[0]}"
                    raise EphemerisExtractionError(err_msg)

                with self._lock:
                    self._results_cache[job_id] = df
                    job.total_samples = res.total_samples
                    job.success_count = res.success_count
                    job.failure_count = res.failure_count

                # 5. Export results
                exported: Dict[str, str] = {}
                formats = [f.lower().strip() for f in config.get("output_formats", ["csv", "xlsx", "txt", "json"])]

                # Always export CSV for internal cache
                csv_path = job_dir / "spice_trajectory.csv"
                export_to_csv(df, csv_path, overwrite_policy="overwrite")
                exported["csv"] = str(csv_path.resolve())

                if "xlsx" in formats or True:
                    xlsx_path = job_dir / "spice_trajectory.xlsx"
                    export_to_excel(df, xlsx_path, sheet_name="SPICE_Trajectory", failures_df=failures_df)
                    exported["xlsx"] = str(xlsx_path.resolve())

                summary_text = generate_text_summary(df, config, failures_df=failures_df)
                txt_path = job_dir / "trajectory_summary.txt"
                export_text_summary(summary_text, txt_path)
                exported["txt"] = str(txt_path.resolve())

                # JSON export
                json_path = job_dir / "spice_trajectory.json"
                json_data = {
                    "job_id": job_id,
                    "target": target,
                    "observer": observer,
                    "frame": frame,
                    "records": df.to_dict(orient="records"),
                }
                with open(json_path, "w", encoding="utf-8") as f_json:
                    json.dump(json_data, f_json, indent=2)
                exported["json"] = str(json_path.resolve())

                # Complete
                completed_time = datetime.now().isoformat()
                with self._lock:
                    job.status = "COMPLETED"
                    job.stage = "Completed successfully"
                    job.progress_pct = 100
                    job.completed_at = completed_time
                    job.exported_files = exported
                    job.summary_text = summary_text

            except Exception as e:
                with self._lock:
                    job.status = "FAILED"
                    job.stage = "Extraction failed"
                    job.progress_pct = 100
                    job.completed_at = datetime.now().isoformat()
                    job.error_message = str(e)


# Global job manager instance
global_job_manager = JobManager()
