"""
NASA/ESA SPICE Data Converter Web Portal Server.

Extends the SPICE Data Extraction project into a comprehensive web portal supporting:
1. Multi-file kernel uploads with content validation.
2. Safe URL imports with anti-SSRF protections and SHA-256 integrity verification.
3. Official NASA NAIF & ESA PSA archive discovery and online freshness checking.
4. Dynamic extraction configuration with automatic target dropdowns.
5. Asynchronous background jobs with live progress tracking and kernel pool isolation.
6. Interactive searchable data preview and multi-format exports (CSV, XLSX, TXT, JSON, ZIP).
"""

from __future__ import annotations

import email
import email.parser
import io
import json
import mimetypes
import datetime
import hashlib
import os
import sys
import uuid
import zipfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import pandas as pd

from spice_manager import SpiceManager
from kernel_importer import (
    sanitize_filename,
    detect_and_validate_kernel_content,
    download_kernel_from_url,
    inspect_kernel_targets,
    SecurityValidationError,
    InvalidKernelFileError,
    KernelImportError,
)
from archive_discovery import (
    OFFICIAL_ARCHIVE_CATALOG,
    get_archive_catalog,
    check_kernel_freshness,
)
from job_manager import global_job_manager, JOBS_BASE_DIR

PORT = 8000
OUTPUT_DIR = Path("output")
KERNELS_DIR = Path("kernels")
UPLOADS_DIR = Path("uploads")

try:
    KERNELS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


def generate_archive_kernel_txt(filename: str) -> str:
    """Generate a comprehensive human-readable plain text (.txt) report for an archive kernel."""
    matched = next((e for e in OFFICIAL_ARCHIVE_CATALOG if e["filename"].lower() == filename.lower()), None)
    local_path = KERNELS_DIR / filename
    if not local_path.exists():
        local_path = UPLOADS_DIR / filename

    lines = []
    lines.append("=" * 80)
    lines.append(" NASA / ESA SPICE ARCHIVE KERNEL SPECIFICATION & EPHEMERIS REPORT")
    lines.append("=" * 80)
    lines.append(f"Product Filename:     {filename}")
    txt_name = Path(filename).stem + ".txt"
    lines.append(f"Export Format:        Plain Text (.txt) - [{txt_name}]")
    if matched:
        lines.append(f"Kernel Type:          {matched.get('kernel_type', 'SPICE')}")
        lines.append(f"Category:             {matched.get('category', 'Ephemeris')}")
        lines.append(f"Mission / System:     {matched.get('mission_or_system', 'Solar System')}")
        lines.append(f"Product Type:         {matched.get('product_type', 'Official Archived Kernel')}")
        lines.append(f"Release Version:      {matched.get('release_version', 'N/A')}")
        lines.append(f"Source Agency:        {matched.get('source_agency', 'NASA NAIF / ESA PSA')}")
        lines.append(f"Official Archive URL: {matched.get('official_url', 'N/A')}")
        lines.append(f"Coverage Summary:     {matched.get('coverage_summary', 'N/A')}")
    lines.append(f"Report Generated:     {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    if local_path.exists():
        st = local_path.stat()
        lines.append(f"Local Kernel Path:    {local_path.resolve()}")
        lines.append(f"File Size (Bytes):    {st.st_size:,} bytes ({st.st_size / (1024 * 1024):.2f} MB)")
        try:
            with open(local_path, "rb") as f:
                h = hashlib.sha256(f.read()).hexdigest()
            lines.append(f"SHA-256 Checksum:     {h}")
        except Exception:
            pass
    lines.append("-" * 80)

    if matched:
        lines.append("CATALOG DESCRIPTION & SCIENTIFIC PURPOSE:")
        lines.append(f"  {matched.get('description', 'Authoritative SPICE ephemeris kernel product.')}")
        lines.append("")
        targets = matched.get("covered_targets", [])
        if targets:
            lines.append(f"COVERED CELESTIAL TARGETS & SPACECRAFT ({len(targets)} BODIES):")
            for tid in targets:
                tname = "UNKNOWN"
                try:
                    import spiceypy as spice
                    tname = spice.bodc2n(tid)
                except Exception:
                    tname = f"NAIF ID {tid}"
                lines.append(f"  • ID {tid:>6}: {tname}")
            lines.append("")

    # If it's a text kernel (like .tls or .tpc), append verbatim comments and constants
    if local_path.exists() and filename.lower().endswith(('.tls', '.tpc', '.tf', '.ti', '.tsc')):
        lines.append("-" * 80)
        lines.append("VERBATIM KERNEL TEXT POOL DATA & COMMENTS:")
        lines.append("-" * 80)
        try:
            content = local_path.read_text(encoding="utf-8", errors="replace")
            lines.append(content)
        except Exception as e:
            lines.append(f"[Error reading text kernel content: {e}]")
    elif local_path.exists() and filename.lower().endswith(('.bsp', '.spk')):
        lines.append("-" * 80)
        lines.append("BINARY DAF / SPK STRUCTURE & SEGMENT SUMMARY:")
        lines.append("-" * 80)
        try:
            targets_meta = inspect_kernel_targets(local_path)
            lines.append(f"Detected {len(targets_meta)} unique celestial bodies/segments in SPK ephemeris:")
            for tm in targets_meta:
                cov_str = f"| Coverage: {tm.get('start_utc', 'N/A')} to {tm.get('end_utc', 'N/A')}" if 'start_utc' in tm else ""
                lines.append(f"  • NAIF ID {tm['id']:>6}: {tm['name']:<25} ({tm.get('intervals_count', 1)} interval(s)) {cov_str}")
        except Exception as e:
            lines.append(f"Segment inspection note: {e}")

        try:
            with open(local_path, "rb") as bf:
                header = bf.read(1024)
                arch = header[:8].decode("ascii", errors="ignore").strip()
                lines.append(f"\nSPICE DAF Architecture: {arch}")
        except Exception:
            pass

    lines.append("")
    lines.append("-" * 80)
    lines.append("USAGE IN PYTHON / CSPICE:")
    lines.append(f"  import spiceypy as spice")
    lines.append(f"  spice.furnsh('{filename}')")
    lines.append(f"  state, lt = spice.spkezr(target_id, epoch_et, 'J2000', 'LT+S', observer_id)")
    lines.append("=" * 80)
    lines.append(" END OF KERNEL REPORT")
    lines.append("=" * 80)
    return "\n".join(lines)


class SpicePortalExtendedHandler(BaseHTTPRequestHandler):
    """Extended HTTP Handler supporting web dashboard, uploads, URL imports, and job control."""

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}\n")

    def send_json_response(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        return json.loads(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def get_clean_path(self) -> Tuple[str, Dict[str, List[str]]]:
        """Normalize URL path across direct local requests and Vercel/proxy rewrites."""
        raw = self.path or "/"

        # 1. Check proxy / Vercel forwarded headers
        if hasattr(self, "headers") and self.headers:
            for hk in (
                "x-forwarded-uri",
                "x-vercel-forwarded-path",
                "x-matched-path",
                "x-original-uri",
                "x-rewrite-url",
            ):
                val = self.headers.get(hk)
                if val:
                    raw = val
                    break

        parsed = urlparse(raw)
        path = parsed.path or "/"
        query = parse_qs(parsed.query)

        # 2. Strip serverless function rewrite prefixes
        for prefix in ("/api/index.py", "/api/index", "/index.py"):
            if path.startswith(prefix):
                path = path[len(prefix):] or "/"
                break

        # Remove trailing slash unless root
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]

        return path, query

    def do_GET(self) -> None:
        path, query = self.get_clean_path()

        # Route 1: Web Portal Dashboard (Root, index, or SPA route fallback)
        if (
            path in ("/", "", "/api", "/api/", "/index.html", "/api/index.html")
            or (not path.startswith("/api/") and not path.startswith("/download/") and path != "/favicon.ico")
        ):
            self.serve_portal_dashboard()
        elif path in ("/favicon.ico", "/api/favicon.ico"):
            svg_icon = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="#0284c7"/><circle cx="12" cy="12" r="4" fill="#ffffff"/></svg>'
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg_icon)))
            self.end_headers()
            self.wfile.write(svg_icon)
        elif path in ("/api/data", "/data"):
            # Backward-compatible global data API
            self.serve_legacy_data_api()
        elif path in ("/api/available-kernels", "/available-kernels"):
            self.handle_get_available_kernels()
        elif path in ("/api/archive-kernels", "/archive-kernels"):
            verify_online = query.get("verify", ["false"])[0].lower() in ("true", "1")
            self.send_json_response(get_archive_catalog(verify_online=verify_online))
        elif path in ("/api/jobs", "/jobs"):
            self.send_json_response(global_job_manager.list_jobs())
        elif (path.startswith("/api/jobs/") or path.startswith("/jobs/")) and path.endswith("/data"):
            parts = path.strip("/").split("/")
            job_idx = 2 if parts[0] == "api" else 1
            job_id = parts[job_idx]
            self.handle_get_job_data(job_id)
        elif (path.startswith("/api/jobs/") or path.startswith("/jobs/")) and "/download/" in path:
            parts = path.strip("/").split("/")
            job_idx = 2 if parts[0] == "api" else 1
            job_id = parts[job_idx]
            fmt = parts[job_idx + 2]
            self.handle_job_download(job_id, fmt)
        elif path.startswith("/api/jobs/") or path.startswith("/jobs/"):
            parts = path.strip("/").split("/")
            job_idx = 2 if parts[0] == "api" else 1
            job_id = parts[job_idx]
            job = global_job_manager.get_job(job_id)
            if job:
                self.send_json_response(job.to_dict())
            else:
                self.send_json_response({"error": f"Job '{job_id}' not found."}, status=404)
        elif "/download/archive-txt/" in path:
            kname = path.split("/download/archive-txt/")[1].strip()
            self.serve_archive_kernel_txt(kname)
        elif "/download/kernel/" in path:
            kname = path.split("/download/kernel/")[1].strip()
            kpath = KERNELS_DIR / kname
            if not kpath.exists():
                kpath = UPLOADS_DIR / kname
            if kpath.exists() and kpath.is_file():
                self.serve_file(kpath, "application/octet-stream", kname)
            else:
                self.send_error(404, f"Kernel '{kname}' not found.")
        elif path in ("/download/csv", "/api/download/csv"):
            self.serve_file(OUTPUT_DIR / "spice_trajectory.csv", "text/csv", "spice_trajectory.csv")
        elif path in ("/download/xlsx", "/api/download/xlsx"):
            self.serve_file(
                OUTPUT_DIR / "spice_trajectory.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "spice_trajectory.xlsx"
            )
        elif path in ("/download/txt", "/api/download/txt"):
            self.serve_file(OUTPUT_DIR / "trajectory_summary.txt", "text/plain", "trajectory_summary.txt")
        elif path in ("/download/all", "/api/download/all"):
            self.serve_zip_bundle()
        else:
            self.send_error(404, "Endpoint Not Found")

    def do_POST(self) -> None:
        path, _ = self.get_clean_path()

        if path in ("/api/upload", "/upload"):
            self.handle_file_upload()
        elif path in ("/api/import-url", "/import-url"):
            self.handle_url_import()
        elif path in ("/api/archive-download", "/archive-download"):
            self.handle_archive_download()
        elif path in ("/api/jobs", "/jobs"):
            self.handle_create_job()
        else:
            self.send_error(404, "Endpoint Not Found")

    # --------------------------------------------------------------------------
    # API Handlers
    # --------------------------------------------------------------------------

    def handle_get_available_kernels(self) -> None:
        """Scan kernels/ and uploads/ directories, return list of available kernels and targets."""
        all_kernels = []
        scan_dirs = [("system", KERNELS_DIR), ("uploaded", UPLOADS_DIR)]

        for source_tag, directory in scan_dirs:
            if not directory.exists():
                continue
            for item in sorted(directory.iterdir()):
                if item.is_file() and not item.name.startswith("."):
                    try:
                        meta = detect_and_validate_kernel_content(item)
                        meta["source_group"] = source_tag
                        if meta["kernel_type"] == "SPK":
                            meta["targets"] = inspect_kernel_targets(item)
                        else:
                            meta["targets"] = []
                        all_kernels.append(meta)
                    except Exception as e:
                        all_kernels.append({
                            "filename": item.name,
                            "path": str(item.resolve()),
                            "size_bytes": item.stat().st_size,
                            "kernel_type": "UNVALIDATED",
                            "error": str(e),
                            "source_group": source_tag,
                            "targets": [],
                        })

        self.send_json_response({"kernels": all_kernels, "count": len(all_kernels)})

    def handle_file_upload(self) -> None:
        """Handle multipart/form-data upload of one or more SPICE kernel files."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_json_response({"error": "Expected multipart/form-data."}, status=400)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 150 * 1024 * 1024:
            self.send_json_response({"error": "Upload exceeds 150 MB limit."}, status=413)
            return

        raw_payload = self.rfile.read(content_length)
        msg_data = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8") + raw_payload

        try:
            msg = email.parser.BytesParser().parsebytes(msg_data)
        except Exception as e:
            self.send_json_response({"error": f"Failed to parse multipart body: {e}"}, status=400)
            return

        uploaded_files = []
        session_id = uuid.uuid4().hex[:8]
        session_upload_dir = UPLOADS_DIR / session_id
        session_upload_dir.mkdir(parents=True, exist_ok=True)

        for part in msg.get_payload():
            if not isinstance(part, email.message.Message):
                continue
            raw_filename = part.get_filename()
            if not raw_filename:
                continue

            clean_name = sanitize_filename(raw_filename)
            file_bytes = part.get_payload(decode=True)
            if not file_bytes:
                continue

            target_path = session_upload_dir / clean_name
            with open(target_path, "wb") as f_out:
                f_out.write(file_bytes)

            try:
                meta = detect_and_validate_kernel_content(target_path)
                if meta["kernel_type"] == "SPK":
                    meta["targets"] = inspect_kernel_targets(target_path)
                else:
                    meta["targets"] = []
                uploaded_files.append(meta)
            except InvalidKernelFileError as e:
                target_path.unlink(missing_ok=True)
                uploaded_files.append({
                    "filename": clean_name,
                    "status": "REJECTED",
                    "error": str(e),
                })

        self.send_json_response({
            "session_id": session_id,
            "uploaded_files": uploaded_files,
            "count": len(uploaded_files),
        })

    def handle_url_import(self) -> None:
        """Download kernel from URL with SSRF protection, streaming, and verification."""
        try:
            payload = self.read_json_body()
        except Exception as e:
            self.send_json_response({"error": f"Invalid JSON payload: {e}"}, status=400)
            return

        url_str = payload.get("url", "").strip()
        if not url_str:
            self.send_json_response({"error": "Missing 'url' parameter."}, status=400)
            return

        destination_choice = payload.get("destination", "kernels").strip()
        dest_dir = KERNELS_DIR if destination_choice == "kernels" else UPLOADS_DIR

        try:
            meta = download_kernel_from_url(
                url_str=url_str,
                destination_dir=dest_dir,
                custom_filename=payload.get("filename"),
            )
            if meta["kernel_type"] == "SPK":
                meta["targets"] = inspect_kernel_targets(Path(meta["path"]))
            else:
                meta["targets"] = []
            self.send_json_response({"status": "SUCCESS", "kernel": meta})
        except SecurityValidationError as e:
            self.send_json_response({"error": f"Security restriction: {e}"}, status=403)
        except InvalidKernelFileError as e:
            self.send_json_response({"error": f"Validation failure: {e}"}, status=422)
        except KernelImportError as e:
            self.send_json_response({"error": f"Import error: {e}"}, status=400)
        except Exception as e:
            self.send_json_response({"error": f"Internal download failure: {e}"}, status=500)

    def serve_archive_kernel_txt(self, filename: str) -> None:
        """Serve generated plain text specification & inspection for an archive kernel."""
        txt_content = generate_archive_kernel_txt(filename)
        body = txt_content.encode("utf-8")
        out_filename = Path(filename).stem + ".txt"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{out_filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def handle_archive_download(self) -> None:
        """Download an authoritative kernel from official NASA NAIF catalog and deliver as readable .txt."""
        try:
            payload = self.read_json_body()
        except Exception as e:
            self.send_json_response({"error": f"Invalid JSON: {e}"}, status=400)
            return

        filename = payload.get("filename", "").strip()
        matched_entry = next((e for e in OFFICIAL_ARCHIVE_CATALOG if e["filename"] == filename), None)

        if not matched_entry:
            self.send_json_response({"error": f"Kernel '{filename}' not found in official catalog."}, status=404)
            return

        txt_filename = Path(filename).stem + ".txt"

        # Check if kernel already exists and is valid locally
        local_kernel_path = KERNELS_DIR / filename
        if local_kernel_path.exists() and local_kernel_path.stat().st_size > 0:
            try:
                meta = detect_and_validate_kernel_content(local_kernel_path)
                self.send_json_response({
                    "status": "SUCCESS",
                    "kernel": meta,
                    "download_url": f"/download/archive-txt/{filename}",
                    "download_filename": txt_filename,
                    "cached": True,
                    "message": f"Kernel '{filename}' prepared in readable format as '{txt_filename}'."
                })
                return
            except Exception:
                pass

        try:
            meta = download_kernel_from_url(
                url_str=matched_entry["official_url"],
                destination_dir=KERNELS_DIR,
                custom_filename=matched_entry["filename"],
            )
            self.send_json_response({
                "status": "SUCCESS",
                "kernel": meta,
                "download_url": f"/download/archive-txt/{filename}",
                "download_filename": txt_filename,
                "cached": False,
                "message": f"Kernel '{filename}' downloaded and converted to '{txt_filename}'."
            })
        except Exception as e:
            self.send_json_response({"error": f"Download failed: {e}"}, status=500)

    def handle_create_job(self) -> None:
        """Launch background extraction job."""
        try:
            config = self.read_json_body()
        except Exception as e:
            self.send_json_response({"error": f"Invalid JSON: {e}"}, status=400)
            return

        # Basic validation
        required = ["target", "observer", "start_utc", "end_utc", "sampling_interval_hours"]
        missing = [k for k in required if k not in config]
        if missing:
            self.send_json_response({"error": f"Missing parameters: {', '.join(missing)}."}, status=400)
            return

        config.setdefault("reference_frame", "J2000")
        config.setdefault("aberration_correction", "LT+S")
        config.setdefault("kernel_directory", "kernels")
        config.setdefault("output_directory", "output")
        config.setdefault("output_formats", ["csv", "xlsx", "txt", "json"])

        job_id = global_job_manager.submit_job(config)
        self.send_json_response({"job_id": job_id, "status": "PENDING", "message": "Extraction job submitted."})

    def handle_get_job_data(self, job_id: str) -> None:
        """Return data records for in-browser table preview."""
        df = global_job_manager.get_job_dataframe(job_id)
        if df is None:
            self.send_json_response({"error": f"No data found for job '{job_id}'."}, status=404)
            return

        records = df.to_dict(orient="records")
        job = global_job_manager.get_job(job_id)
        summary_txt = job.summary_text if job else ""

        job_dir = JOBS_BASE_DIR / job_id
        csv_p = job_dir / "spice_trajectory.csv"
        xlsx_p = job_dir / "spice_trajectory.xlsx"
        txt_p = job_dir / "trajectory_summary.txt"
        json_p = job_dir / "spice_trajectory.json"

        def fmt_sz(p: Path) -> str:
            if not p.exists(): return "0 B"
            s = p.stat().st_size
            return f"{s/1024:.1f} KB" if s >= 1024 else f"{s} B"

        files_meta = {
            "csv": {"name": f"spice_trajectory_{job_id}.csv", "exists": csv_p.exists(), "size": fmt_sz(csv_p), "url": f"/api/jobs/{job_id}/download/csv"},
            "xlsx": {"name": f"spice_trajectory_{job_id}.xlsx", "exists": xlsx_p.exists(), "size": fmt_sz(xlsx_p), "url": f"/api/jobs/{job_id}/download/xlsx"},
            "txt": {"name": f"trajectory_summary_{job_id}.txt", "exists": txt_p.exists(), "size": fmt_sz(txt_p), "url": f"/api/jobs/{job_id}/download/txt"},
            "json": {"name": f"spice_trajectory_{job_id}.json", "exists": json_p.exists(), "size": fmt_sz(json_p), "url": f"/api/jobs/{job_id}/download/json"},
            "zip": {"name": f"spice_trajectory_{job_id}.zip", "exists": True, "size": "ZIP Archive", "url": f"/api/jobs/{job_id}/download/zip"},
        }

        self.send_json_response({
            "job_id": job_id,
            "total": len(df),
            "columns": list(df.columns),
            "records": records,
            "summary": summary_txt,
            "files": files_meta,
            "timestamp": csv_p.stat().st_mtime if csv_p.exists() else 0,
        })

    def handle_job_download(self, job_id: str, fmt: str) -> None:
        """Serve specific export format for a job."""
        job_dir = JOBS_BASE_DIR / job_id
        if not job_dir.exists():
            self.send_error(404, f"Job '{job_id}' outputs not found.")
            return

        if fmt == "csv":
            self.serve_file(job_dir / "spice_trajectory.csv", "text/csv", f"spice_trajectory_{job_id}.csv")
        elif fmt == "xlsx":
            self.serve_file(
                job_dir / "spice_trajectory.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                f"spice_trajectory_{job_id}.xlsx"
            )
        elif fmt == "txt":
            self.serve_file(job_dir / "trajectory_summary.txt", "text/plain", f"trajectory_summary_{job_id}.txt")
        elif fmt == "json":
            self.serve_file(job_dir / "spice_trajectory.json", "application/json", f"spice_trajectory_{job_id}.json")
        elif fmt == "zip":
            # Bundle job files
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in job_dir.glob("*.*"):
                    zf.write(f, arcname=f.name)
            zip_data = zip_buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="spice_trajectory_{job_id}.zip"')
            self.send_header("Content-Length", str(len(zip_data)))
            self.end_headers()
            self.wfile.write(zip_data)
        else:
            self.send_error(400, f"Unsupported format: '{fmt}'.")

    # --------------------------------------------------------------------------
    # Legacy File Serving
    # --------------------------------------------------------------------------

    def serve_file(self, filepath: Path, content_type: str, filename: str) -> None:
        if not filepath.exists():
            self.send_error(404, f"File {filename} not generated yet.")
            return

        try:
            with open(filepath, "rb") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Error reading file: {e}")

    def serve_zip_bundle(self) -> None:
        files_to_pack = [
            ("spice_trajectory.csv", OUTPUT_DIR / "spice_trajectory.csv"),
            ("spice_trajectory.xlsx", OUTPUT_DIR / "spice_trajectory.xlsx"),
            ("trajectory_summary.txt", OUTPUT_DIR / "trajectory_summary.txt"),
        ]

        existing = [(name, p) for name, p in files_to_pack if p.exists()]
        if not existing:
            self.send_error(404, "No output files available to bundle.")
            return

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, p in existing:
                zf.write(p, arcname=name)

        zip_data = zip_buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="spice_trajectory_bundle.zip"')
        self.send_header("Content-Length", str(len(zip_data)))
        self.end_headers()
        self.wfile.write(zip_data)

    def serve_legacy_data_api(self) -> None:
        csv_file = OUTPUT_DIR / "spice_trajectory.csv"
        if not csv_file.exists():
            self.send_json_response({"error": "No trajectory data extracted yet.", "records": []})
            return

        try:
            df = pd.read_csv(csv_file)
            summary_file = OUTPUT_DIR / "trajectory_summary.txt"
            summary_txt = summary_file.read_text(encoding="utf-8") if summary_file.exists() else ""
            xlsx_p = OUTPUT_DIR / "spice_trajectory.xlsx"
            txt_p = OUTPUT_DIR / "trajectory_summary.txt"

            def fmt_sz(p: Path) -> str:
                if not p.exists(): return "0 B"
                s = p.stat().st_size
                return f"{s/1024:.1f} KB" if s >= 1024 else f"{s} B"

            files_meta = {
                "csv": {"name": "spice_trajectory.csv", "exists": csv_file.exists(), "size": fmt_sz(csv_file), "url": "/download/csv"},
                "xlsx": {"name": "spice_trajectory.xlsx", "exists": xlsx_p.exists(), "size": fmt_sz(xlsx_p), "url": "/download/xlsx"},
                "txt": {"name": "trajectory_summary.txt", "exists": txt_p.exists(), "size": fmt_sz(txt_p), "url": "/download/txt"},
                "zip": {"name": "spice_trajectory_bundle.zip", "exists": csv_file.exists(), "size": "ZIP Archive", "url": "/download/all"},
            }

            self.send_json_response({
                "total": len(df),
                "columns": list(df.columns),
                "records": df.to_dict(orient="records"),
                "summary": summary_txt,
                "files": files_meta,
                "timestamp": csv_file.stat().st_mtime if csv_file.exists() else 0,
            })
        except Exception as e:
            self.send_json_response({"error": str(e), "records": []}, status=500)

    # --------------------------------------------------------------------------
    # Frontend Dashboard Web Interface
    # --------------------------------------------------------------------------

    def serve_portal_dashboard(self) -> None:
        template_candidates = [
            Path(__file__).resolve().parent / "templates" / "portal.html",
            Path.cwd() / "templates" / "portal.html",
            Path("templates") / "portal.html",
        ]
        html_content = ""
        for tc in template_candidates:
            if tc.exists():
                try:
                    html_content = tc.read_text(encoding="utf-8")
                    break
                except Exception:
                    pass
        if not html_content:
            html_content = "<!DOCTYPE html><html><head><title>NASA / ESA SPICE Data Converter & Portal</title></head><body><h1>NASA / ESA SPICE Data Converter & Portal</h1></body></html>"

        body = html_content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_portal_server(port: Optional[int] = None) -> None:
    if port is None:
        port = int(os.environ.get("PORT", PORT))
    host = os.environ.get("HOST", "0.0.0.0")
    server_address = (host, port)
    httpd = HTTPServer(server_address, SpicePortalExtendedHandler)
    print("=" * 70)
    print(f" NASA / ESA SPICE DATA CONVERTER WEB PORTAL ACTIVE")
    print(f" Web Portal: http://localhost:{port}/ (listening on {host}:{port})")
    print(f" API Endpoints:")
    print(f"   • Available Kernels: http://localhost:{port}/api/available-kernels")
    print(f"   • Archive Registry:  http://localhost:{port}/api/archive-kernels")
    print(f"   • Background Jobs:   http://localhost:{port}/api/jobs")
    print("=" * 70)
    httpd.serve_forever()


# ==============================================================================
# Cloud & Serverless Deployment Adapters (Vercel, WSGI, ASGI, Render, etc.)
# ==============================================================================

class WSGIAdapter:
    """Translates WSGI requests to SpicePortalExtendedHandler execution."""

    def __init__(self, handler_cls: type):
        self.handler_cls = handler_cls

    def __call__(self, environ: Dict[str, Any], start_response: Any):
        import io
        from http.client import HTTPMessage

        request_method = environ.get("REQUEST_METHOD", "GET")
        path_info = environ.get("PATH_INFO", "/")
        query_string = environ.get("QUERY_STRING", "")
        full_path = f"{path_info}?{query_string}" if query_string else path_info

        wsgi_input = environ.get("wsgi.input", io.BytesIO())
        try:
            content_length = int(environ.get("CONTENT_LENGTH", 0) or 0)
        except (ValueError, TypeError):
            content_length = 0

        input_data = wsgi_input.read(content_length) if content_length > 0 else b""
        rfile = io.BytesIO(input_data)
        wfile = io.BytesIO()

        headers = HTTPMessage()
        for k, v in environ.items():
            if k.startswith("HTTP_"):
                header_name = k[5:].replace("_", "-").title()
                headers[header_name] = str(v)
            elif k in ("CONTENT_TYPE", "CONTENT_LENGTH") and v:
                headers[k.replace("_", "-").title()] = str(v)

        class DummyServer:
            server_name = environ.get("SERVER_NAME", "localhost")
            server_port = int(environ.get("SERVER_PORT", 80) or 80)

        handler_inst = self.handler_cls.__new__(self.handler_cls)
        handler_inst.server = DummyServer()
        handler_inst.client_address = (
            environ.get("REMOTE_ADDR", "127.0.0.1"),
            int(environ.get("REMOTE_PORT", 0) or 0),
        )
        handler_inst.request_version = environ.get("SERVER_PROTOCOL", "HTTP/1.1")
        handler_inst.command = request_method
        handler_inst.path = full_path
        handler_inst.headers = headers
        handler_inst.rfile = rfile
        handler_inst.wfile = wfile
        handler_inst.close_connection = True

        status_container = {"code": 200, "headers": []}

        def mock_send_response(code: int, message: Optional[str] = None):
            status_container["code"] = code

        def mock_send_header(keyword: str, value: Any):
            status_container["headers"].append((str(keyword), str(value)))

        def mock_end_headers():
            pass

        def mock_send_error(code: int, message: Optional[str] = None, explain: Optional[str] = None):
            status_container["code"] = code
            err_body = (message or f"HTTP Error {code}").encode("utf-8")
            status_container["headers"] = [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(err_body))),
            ]
            wfile.write(err_body)

        handler_inst.send_response = mock_send_response
        handler_inst.send_header = mock_send_header
        handler_inst.end_headers = mock_end_headers
        handler_inst.send_error = mock_send_error

        method_name = f"do_{request_method}"
        if hasattr(handler_inst, method_name):
            try:
                getattr(handler_inst, method_name)()
            except Exception as e:
                mock_send_error(500, f"Internal Error: {e}")
        else:
            mock_send_error(501, f"Unsupported method: {request_method}")

        code = status_container["code"]
        reason = getattr(self.handler_cls, "responses", {}).get(code, ("OK",))[0]
        status_line = f"{code} {reason}"

        resp_headers = status_container["headers"]
        body_bytes = wfile.getvalue()

        if not any(k.lower() == "content-length" for k, _ in resp_headers):
            resp_headers.append(("Content-Length", str(len(body_bytes))))

        start_response(status_line, resp_headers)
        return [body_bytes]


class UniversalAdapter:
    """Translates both WSGI (2 args) and ASGI (3 args) requests to handler execution."""

    def __init__(self, handler_cls: type):
        self.handler_cls = handler_cls
        self.wsgi = WSGIAdapter(handler_cls)

    def __call__(self, *args, **kwargs):
        if len(args) == 2:
            return self.wsgi(args[0], args[1])
        elif len(args) == 3:
            return self.asgi_handler(args[0], args[1], args[2])
        raise TypeError(f"UniversalAdapter expected 2 (WSGI) or 3 (ASGI) arguments, got {len(args)}")

    async def asgi_handler(self, scope: Dict[str, Any], receive: Any, send: Any):
        if scope.get("type") != "http":
            return
        import io
        body = b""
        more_body = True
        while more_body:
            msg = await receive()
            body += msg.get("body", b"")
            more_body = msg.get("more_body", False)

        environ = {
            "REQUEST_METHOD": scope.get("method", "GET"),
            "PATH_INFO": scope.get("path", "/"),
            "QUERY_STRING": scope.get("query_string", b"").decode("latin1"),
            "SERVER_PROTOCOL": f"HTTP/{scope.get('http_version', '1.1')}",
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
        }
        for name, val in scope.get("headers", []):
            k = name.decode("latin1").upper().replace("-", "_")
            v = val.decode("latin1")
            if k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                environ[k] = v
            else:
                environ[f"HTTP_{k}"] = v

        captured = {"status": 200, "headers": []}

        def start_response(status, headers, exc_info=None):
            captured["status"] = int(status.split()[0])
            captured["headers"] = headers

        chunks = self.wsgi(environ, start_response)
        body_bytes = b"".join(chunks)

        headers_list = [
            (str(k).lower().encode("latin1"), str(v).encode("latin1"))
            for k, v in captured["headers"]
        ]
        await send({
            "type": "http.response.start",
            "status": captured["status"],
            "headers": headers_list,
        })
        await send({
            "type": "http.response.body",
            "body": body_bytes,
            "more_body": False,
        })


# 1. Native Vercel Serverless Function entrypoint (inherits BaseHTTPRequestHandler)
class handler(SpicePortalExtendedHandler):
    """Vercel Python Serverless BaseHTTPRequestHandler entrypoint."""
    pass


# 2. Top-level WSGI / ASGI entrypoints for frameworks & deployment runners
app = UniversalAdapter(SpicePortalExtendedHandler)
application = app


if __name__ == "__main__":
    run_portal_server()
