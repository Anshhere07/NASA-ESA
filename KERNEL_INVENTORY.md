# SPICE Kernel Inventory & Readiness Report

## 1. Kernel Files & Types

| Filename | Kernel Type | Format | File Size | Official Source |
| :--- | :--- | :--- | :--- | :--- |
| `kernels/naif0012.tls` | LSK (Leapseconds Kernel) | Text (KPL) | 5,257 bytes | [NASA NAIF Generic Kernels LSK](https://naif.jpl.nasa.gov/pub/naif/generic_kernels/lsk/naif0012.tls) |
| `kernels/pck00010.tpc` | PCK (Planetary Constants Kernel) | Text (KPL) | 126,143 bytes | [NASA NAIF Generic Kernels PCK](https://naif.jpl.nasa.gov/pub/naif/generic_kernels/pck/pck00010.tpc) |
| `kernels/de440s.bsp` | SPK (Planetary Ephemeris Kernel) | Binary (DAF) | 32,726,016 bytes | [NASA NAIF Generic Kernels SPK](https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de440s.bsp) |

---

## 2. SPK Target IDs & Coverage Intervals

All bodies in `kernels/de440s.bsp` have verified coverage via CSPICE `spkobj()` and `spkcov()`:

- **Coverage Start:** `1849-DEC-25 23:59:18.816 UTC` (ET: `-4733894441.815`)
- **Coverage End:** `2150-JAN-21 23:58:50.815 UTC` (ET: `4735588730.817`)

### Available NAIF IDs & Target Bodies:
- `ID 1`: MERCURY BARYCENTER
- `ID 2`: VENUS BARYCENTER
- `ID 3`: EARTH BARYCENTER
- `ID 4`: MARS BARYCENTER
- `ID 5`: JUPITER BARYCENTER
- `ID 6`: SATURN BARYCENTER
- `ID 7`: URANUS BARYCENTER
- `ID 8`: NEPTUNE BARYCENTER
- `ID 9`: PLUTO BARYCENTER
- `ID 10`: SUN
- `ID 199`: MERCURY
- `ID 299`: VENUS
- `ID 301`: MOON
- `ID 399`: EARTH

---

## 3. Dependency & Module Status

| Component | Version / Path | Status |
| :--- | :--- | :--- |
| `spiceypy` | `8.2.0` (CSPICE `CSPICE_N0067`) | Verified & Functional |
| `numpy` | `2.5.4` | Verified & Functional |
| `pandas` | `3.0.6` | Verified & Functional |
| `openpyxl` | `3.1.5` | Verified & Functional |
| `config.json` | [config.json](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/config.json) | Configured & Validated |
| `spice_manager.py` | [spice_manager.py](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/spice_manager.py) | Verified (Kernel lifecycle, LSK, UTC-to-ET) |
| `trajectory.py` | [trajectory.py](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/trajectory.py) | Verified (State vector & Time-series) |
| `exporter.py` | [exporter.py](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/exporter.py) | Verified (CSV, Excel, Text Summary) |
| `workflow.py` | [workflow.py](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/workflow.py) | Verified (Orchestration pipeline) |
| `main.py` | [main.py](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/main.py) | Verified (CLI & Interactive Wizard) |
| `kernel_importer.py` | [kernel_importer.py](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/kernel_importer.py) | Verified (Upload validation, SSRF-safe URL import) |
| `archive_discovery.py`| [archive_discovery.py](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/archive_discovery.py) | Verified (NASA/ESA Catalog & Freshness checks) |
| `job_manager.py` | [job_manager.py](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/job_manager.py) | Verified (Serial background queue & thread isolation) |
| `server.py` | [server.py](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/server.py) | Verified (Web Portal UI & REST API) |
| `tests/` | [tests/](file:///c:/Users/saxen/OneDrive/Desktop/Shivam_jiju/tests) | Verified (**81/81 unit and integration tests passing**) |

---

## 4. Web Portal & Job Isolation

- **Web Server**: Running on `http://localhost:8000/`.
- **Worker Isolation**: Jobs are processed serially via `ThreadPoolExecutor(max_workers=1)`. Because CSPICE maintains a single global kernel pool per process, serial queueing ensures no two jobs ever clash or clear each other's kernels.
- **Export Outputs**:
  - Global outputs: `output/spice_trajectory.csv`, `output/spice_trajectory.xlsx`, `output/trajectory_summary.txt`.
  - Job-specific outputs: `output/jobs/<job_id>/` containing all requested formats (`.csv`, `.xlsx`, `.txt`, `.json`).
- **Upload Sessions**: Uploads are partitioned under `uploads/<session_id>/` with DAF/KPL content validation and sanitized filenames.
- **SSRF Hardening**: Restricted to approved NASA/ESA archive hosts with strict IP resolution blocking loopback, link-local, and private ranges.
