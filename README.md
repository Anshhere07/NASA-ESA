# SPICE Data Processing Pipeline & Web Portal

A production-grade Python application and web portal for uploading, importing, discovering, extracting, and exporting NASA/ESA SPICE ephemeris kernels using `spiceypy` (CSPICE).

---

## 1. Application Overview

This project provides both an interactive, modern **Web Portal** and a scriptable **Command-Line Interface (CLI)** for SPICE ephemeris data extraction:

- **Web Portal (`server.py`)**: A full-featured web dashboard providing:
  - Multi-file kernel uploading with binary (DAF) and text (KPL) header content validation.
  - SSRF-safe kernel URL import with IP address filtering, DNS rebinding protection, streaming downloads, and SHA-256 verification.
  - Official NASA NAIF and ESA PSA archive catalog discovery and live HTTP freshness verification.
  - Interactive Extraction Studio with auto-populated target/observer lists from loaded kernels.
  - Non-blocking background job execution with stage-by-stage progress tracking.
  - Real-time tabular data preview with search, sorting, and pagination.
  - Multi-format file downloads: CSV, professionally styled Excel (`.xlsx`), JSON, Text Summary (`.txt`), and ZIP bundle.
- **Controlled SPICE Lifecycle (`spice_manager.py`)**: Safe furnishing and scoping via CSPICE `furnsh` and `unload`, strictly avoiding accidental global pool clearance (`kclear`).
- **Generic Ephemeris Inspection**: Automatic discovery and coverage mapping of planetary barycenters, planets, moons, and spacecraft without hardcoded body assumptions.
- **Strict Time Conversion**: Lossless UTC-to-ET (Ephemeris Time) conversion requiring an active Leap-Seconds Kernel (LSK).
- **Time-Series Ephemeris Sampling (`trajectory.py`)**: Configurable extraction of 6-component state vectors ($X, Y, Z, V_X, V_Y, V_Z$) using CSPICE `spkezr`.
- **Failure Transparency**: Failed samples are captured with timestamps and explicit error reasons with missing values represented as `NaN`, strictly avoiding artificial zero substitution (`0.0`).
- **Multi-Format Exports (`exporter.py`)**: Outputs to clean CSV (RFC 4180), professionally formatted Excel (`.xlsx` with frozen panes, custom precision, and auto-filters), and detailed Text Summaries with extrema calculations.
- **Beginner-Friendly CLI & Logging (`main.py`)**: Menu-driven wizard or non-interactive CLI flags with persistent timestamped diagnostic logging in `logs/`.

---

## 2. Python Requirements & Dependencies

- **Python Version**: Python 3.10+ (tested and verified on Python 3.14.3 64-bit on Windows).
- **Core Dependencies**:
  - `spiceypy>=8.2.0` (CSPICE Toolkit `CSPICE_N0067`)
  - `numpy>=2.0.0`
  - `pandas>=3.0.0`
  - `openpyxl>=3.1.5`

Dependencies are locked and tracked in [requirements.txt](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/requirements.txt). No external web frameworks or database servers are required; the portal runs on Python's built-in standard library `http.server`.

---

## 3. How to Launch

### Launch the Web Portal
To start the web portal server:
```powershell
python server.py
```
Open your browser and navigate to:
```
http://localhost:8000/
```
To run on a custom port:
```powershell
python server.py --port 8080
```

### Launch the Command-Line Interface (CLI)
The original CLI remains fully functional and unchanged:
```powershell
# Run extraction non-interactively using config.json defaults
python main.py --run

# Inspect all kernels currently in the kernels/ directory
python main.py --inspect

# Validate dependencies and environment readiness
python main.py --validate

# Launch the interactive step-by-step terminal wizard
python main.py --wizard
```

---

## 4. Web Portal Features & Workflows

### 4.1 Kernel Upload Workflow
1. Navigate to the **Kernel Upload** tab in the portal.
2. Select one or more kernel files (`.bsp`, `.tls`, `.tpc`, `.tf`).
3. Click **Upload & Validate**.
4. The backend validates:
   - File size limits (default max 100 MB per file).
   - Content signature validation (verifies binary `DAF/SPK` or text `KPL/FK/LSK/PCK` headers, rejecting HTML error pages or arbitrary binaries).
   - Extracts target IDs and coverage epochs via CSPICE `spkobj` / `spkcov`.
5. Uploaded kernels are isolated in temporary session directories (`uploads/<session_id>/`) and can be integrated into the active pool.

### 4.2 Safe URL Import Workflow
1. Navigate to the **Import from URL** tab.
2. Provide a direct download URL from an approved archive host (e.g., `https://naif.jpl.nasa.gov/pub/naif/generic_kernels/lsk/naif0012.tls`).
3. Click **Import Kernel**.
4. The dedicated import service applies strict SSRF security protections:
   - Verifies URL scheme (`http://` or `https://`).
   - Validates hostname against approved domains (`naif.jpl.nasa.gov`, `pub.ephemerides.org`, `cosmos.esa.int`, `psa.esac.esa.int`, `ssd.jpl.nasa.gov`).
   - Resolves DNS and blocks private/loopback/link-local/multicast IP addresses (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `::1`).
   - Validates all HTTP redirects against the same SSRF policy.
   - Streams download to disk in 64 KB chunks up to the maximum size limit.
   - Computes SHA-256 integrity checksum and verifies kernel content type.

### 4.3 Official Archive Discovery & Freshness Verification
1. Navigate to the **Archive Registry** tab.
2. Browse pre-cataloged standard ephemeris kernels from NASA NAIF and ESA PSA:
   - `de440s.bsp` (NASA JPL Planetary Ephemeris 1850–2150)
   - `de440.bsp` (NASA JPL Extended Planetary Ephemeris 1550–2650)
   - `naif0012.tls` (NASA NAIF Standard Leap-Seconds Kernel)
   - `pck00010.tpc` (NASA NAIF Planetary Constants Kernel)
   - `mar097.bsp` (NASA JPL Mars & Martian Moons Ephemeris)
   - `jup365.bsp` (NASA JPL Jovian Satellite Ephemeris)
   - `MEX_STRUCT_V01.BSP` (ESA Mars Express Spacecraft Trajectory)
3. For each candidate kernel, view its official source URL, published release/version, date, and coverage.
4. Click **Check Freshness** to perform live HTTP HEAD validation against the archive. If the archive is unreachable, the portal explicitly reports `"Freshness could not be verified"` rather than assuming outdated files are current.
5. Click **Download into Kernel Pool** to fetch missing kernels directly into your local storage.

### 4.4 Extraction Studio & Job Execution
1. Navigate to the **Extraction Studio** tab.
2. Target and observer dropdowns are dynamically populated with all bodies detected in the loaded SPK kernels. You can also enter custom numeric NAIF IDs.
3. Configure reference frame (`J2000`, `ECLIPJ2000`), aberration correction (`NONE`, `LT`, `LT+S`), UTC start/end times, and step interval (hours).
4. Select desired output formats (`csv`, `xlsx`, `txt`, `json`).
5. Click **Run Extraction Job**.
6. The job is queued in a thread-isolated serial executor (`JobManager`), preventing multi-thread SPICE kernel pool race conditions.
7. Monitor live progress percentage, processing stages, and successful/failed sample counts.

### 4.5 Data Preview & Multi-Format Downloads
1. Navigate to the **Preview & Downloads** tab.
2. Select any completed job to view its summary statistics.
3. The interactive data table features real-time search filtering, column sorting, and pagination (25 records per page).
4. Download converted datasets:
   - **CSV**: Standard comma-separated values (`output/jobs/<job_id>/spice_trajectory.csv`).
   - **Excel (.xlsx)**: Styled workbook with navy header, frozen panes, filters, and formatted numbers (`output/jobs/<job_id>/spice_trajectory.xlsx`).
   - **Summary (.txt)**: Detailed textual report with position extrema and scientific provenance notice (`output/jobs/<job_id>/trajectory_summary.txt`).
   - **JSON**: Machine-readable JSON array of records (`output/jobs/<job_id>/spice_trajectory.json`).
   - **ZIP Bundle**: Download all formats in a single compressed archive.

---

## 5. Architecture & Reused Modules

The web portal strictly builds upon and reuses the existing scientific modules without duplicating calculations:

```
                  ┌──────────────────────────────┐
                  │      Web Portal Frontend     │
                  │  (HTML5 / CSS / Vanilla JS)  │
                  └──────────────┬───────────────┘
                                 │ HTTP REST API
                  ┌──────────────▼───────────────┐
                  │     server.py (Web Layer)    │
                  └──────┬───────┬───────┬───────┘
                         │       │       │
       ┌─────────────────┘       │       └─────────────────┐
       ▼                         ▼                         ▼
┌──────────────┐         ┌──────────────┐         ┌─────────────────┐
│kernel_       │         │archive_      │         │job_manager.py   │
│importer.py   │         │discovery.py  │         │(Background Jobs)│
└──────────────┘         └──────────────┘         └────────┬────────┘
                                                           │
                         ┌─────────────────────────────────┘
                         ▼
        ┌─────────────────────────────────────────────────┐
        │         EXISTING REUSED SCIENTIFIC CORE         │
        ├────────────────────────┬────────────────────────┤
        │ spice_manager.py       │ trajectory.py          │
        │ - Kernel discovery     │ - spkezr state vectors │
        │ - Controlled furnsh    │ - Time-series sampling │
        │ - UTC-to-ET conversion │ - Failure tracking     │
        ├────────────────────────┼────────────────────────┤
        │ exporter.py            │ workflow.py            │
        │ - CSV (RFC 4180)       │ - Config orchestration │
        │ - Excel (.xlsx styled) │ - Output routing       │
        │ - Text Summary (.txt)  │                        │
        └────────────────────────┴────────────────────────┘
```

- **`spice_manager.py`**: Furnishes kernels, inspects object IDs/coverage, handles UTC-to-ET conversion, and guarantees scoped unloading.
- **`trajectory.py`**: Executes CSPICE `spkezr` calculations and time-series extraction. Missing values are recorded as `NaN`; never replaced with artificial zeros.
- **`exporter.py`**: Formats CSV, Excel (`openpyxl`), and Text Summaries.
- **`workflow.py`**: Orchestrates configuration validation and file export pipelines.
- **`job_manager.py`**: Executes jobs serially via a single worker thread pool (`ThreadPoolExecutor(max_workers=1)`), maintaining process-level SPICE pool isolation so jobs cannot clobber each other's kernels.

---

## 6. Kernel Inventory

Place official SPICE kernels in the `kernels/` directory:

| Filename | Type | Official Source | Purpose |
| :--- | :--- | :--- | :--- |
| `naif0012.tls` | LSK | [NASA NAIF generic_kernels/lsk/](https://naif.jpl.nasa.gov/pub/naif/generic_kernels/lsk/naif0012.tls) | UTC to Ephemeris Time (ET) conversion |
| `pck00010.tpc` | PCK | [NASA NAIF generic_kernels/pck/](https://naif.jpl.nasa.gov/pub/naif/generic_kernels/pck/pck00010.tpc) | Planetary constants, radii, and orientation |
| `de440s.bsp` | SPK | [NASA NAIF generic_kernels/spk/planets/](https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de440s.bsp) | High-accuracy planetary ephemerides (1850–2150) |

---

## 7. Security Controls

The portal implements defense-in-depth security measures:
1. **SSRF Protection**: Rejects all internal network destinations, loopback addresses (`127.0.0.1`, `::1`), private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local metadata services (`169.254.169.254`), and non-approved hosts.
2. **Path Traversal Prevention**: Filenames are sanitized with `secure_filename()` which strips directory traversal components (`../`, `..\\`), null bytes, and illegal symbols.
3. **Download Sandboxing**: Download endpoints resolve paths strictly inside verified job output directories, rejecting attempts to access parent directories or arbitrary system files.
4. **File Size Limits**: Kernel uploads and URL imports enforce maximum size limits (default 100 MB) and stream directly to disk.
5. **Content Verification**: Uploaded files must match known SPICE magic signatures (`DAF/SPK` or `KPL`), preventing upload of malicious scripts or executables.

---

## 8. Scientific Assumptions & Limitations

1. **Mathematical Ephemeris vs. Measured Telemetry**:
   State vectors are computed via numerical integration of planetary and lunar ephemerides (e.g. JPL DE440S) and physical models in SPICE kernels. They represent modeled celestial kinematics, **not direct in-situ spacecraft telemetry or optical/radar observations**.
2. **Aberration Corrections**:
   When using `'LT+S'`, positions are adjusted for one-way light-time delay and stellar aberration due to observer motion. Use `'NONE'` for pure geometric barycentric coordinates.
3. **Ephemeris Truncation**:
   The `de440s.bsp` ephemeris includes planetary barycenters and planetary centers for inner bodies (Earth, Moon, Mercury, Venus, Sun). Outer planet satellite ephemerides (e.g., Phobos/Deimos, Galilean moons) require dedicated satellite ephemeris kernels (`.bsp`).

---

## 9. Automated Testing

Run the full automated test suite containing **81 unit and integration tests**:

```powershell
python -m unittest discover -s tests -v
```

All 81 tests execute across:
- `test_spice_manager.py`: Kernel lifecycle, LSK UTC-to-ET conversion, and coverage intervals.
- `test_trajectory.py`: State vector extraction, time-series sampling, and failure tracking.
- `test_workflow_and_export.py`: Config validation, CSV export, Excel styling, and summary text.
- `test_cli.py`: Command-line interface arguments, interactive wizard, and terminal flags.
- `test_kernel_importer.py`: Multi-file uploads, magic header verification, and SSRF filtering.
- `test_archive_discovery.py`: NASA/ESA catalog discovery and HTTP freshness verification.
- `test_web_portal.py`: HTTP endpoints, job submission, polling, preview API, and download routes.
#   N A S A - E S A  
 