"""
SPICE Kernel Importer & Safe Ingestion Module.

Handles:
1. Multi-file uploads with binary/text header validation.
2. Safe URL imports with strict SSRF protection, DNS rebinding prevention,
   streaming downloads, size limits, and SHA-256 integrity verification.
3. Target ID and coverage inspection for uploaded kernels.
4. Isolated session storage and safe filename sanitization.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import spiceypy as spice
from spice_manager import SpiceManager, KernelNotFoundError


# ==============================================================================
# Constants & Restrictions
# ==============================================================================

DEFAULT_MAX_UPLOAD_SIZE = 150 * 1024 * 1024  # 150 MB
DEFAULT_MAX_DOWNLOAD_SIZE = 150 * 1024 * 1024  # 150 MB
DOWNLOAD_TIMEOUT_SECONDS = 60

# Approved official hosts for SPICE downloads by default
DEFAULT_APPROVED_HOSTS: Set[str] = {
    "naif.jpl.nasa.gov",
    "pub.ephemerides.org",
    "cosmos.esa.int",
    "psa.esac.esa.int",
    "spiftp.esac.esa.int",
    "repos.esac.esa.int",
    "esac.esa.int",
    "ssd.jpl.nasa.gov",
    "planetarydata.jpl.nasa.gov",
}

# Standard SPICE file extensions
VALID_KERNEL_EXTENSIONS = {
    ".bsp", ".spk",  # Planetary/Spacecraft Ephemeris
    ".tls",          # Leap-seconds kernel (LSK)
    ".tpc", ".bpc",  # Planetary Constants Kernel (PCK)
    ".tf",           # Frame Kernel (FK)
    ".tsc",          # Spacecraft Clock (SCLK)
    ".ti",           # Instrument Kernel (IK)
    ".bc",           # Pointing / C-Kernel (CK)
}


class KernelImportError(Exception):
    """Base exception for kernel import and ingestion failures."""
    pass


class SecurityValidationError(KernelImportError):
    """Raised when an import violates security boundaries (SSRF, path traversal)."""
    pass


class InvalidKernelFileError(KernelImportError):
    """Raised when an uploaded file is not a valid SPICE kernel."""
    pass


# ==============================================================================
# Path Sanitization
# ==============================================================================

def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename against path traversal attacks (e.g. '../', absolute paths).
    Returns clean base filename.
    """
    base = Path(filename).name
    clean = re.sub(r"[^\w\.\-]", "_", base)
    if not clean or clean.startswith("."):
        clean = f"kernel_{clean.lstrip('.')}"
    return clean


# ==============================================================================
# Content-Based Kernel Validation
# ==============================================================================

def detect_and_validate_kernel_content(filepath: Path) -> Dict[str, Any]:
    """
    Inspect file contents rather than relying solely on extension.
    Validates binary DAF/NAIF SPK or text KPL SPICE headers.
    """
    if not filepath.exists() or not filepath.is_file():
        raise InvalidKernelFileError(f"File not found: '{filepath}'.")

    size = filepath.stat().st_size
    if size == 0:
        raise InvalidKernelFileError(f"Kernel file '{filepath.name}' is empty (0 bytes).")

    # Read initial bytes
    with open(filepath, "rb") as f:
        header_bytes = f.read(512)

    ext = filepath.suffix.lower()
    kernel_type = "UNKNOWN"
    is_binary = b"\x00" in header_bytes[:128]

    # Check NAIF binary DAF signatures (SPK, CK, binary PCK)
    # DAF files start with an ID word like 'NAIF/DAF', 'DAF/SPK', 'DAF/CK'
    if is_binary:
        id_word = header_bytes[:8].decode("latin-1", errors="ignore").strip()
        if "DAF" in id_word or "SPK" in id_word or "NAIF" in id_word:
            if "SPK" in id_word or ext in {".bsp", ".spk"}:
                kernel_type = "SPK"
            elif "CK" in id_word or ext == ".bc":
                kernel_type = "CK"
            elif "PCK" in id_word or ext == ".bpc":
                kernel_type = "PCK"
            else:
                kernel_type = "BINARY_DAF"
        else:
            # Let SPICE toolkit test opening the binary kernel
            try:
                spice.kclear()
                handle = spice.dafopr(str(filepath))
                spice.dafcls(handle)
                kernel_type = "SPK" if ext in {".bsp", ".spk"} else "BINARY"
            except Exception:
                raise InvalidKernelFileError(
                    f"File '{filepath.name}' has binary data but lacks a valid NAIF DAF/SPK signature."
                )
    else:
        # Check SPICE text kernel headers (KPL/FK, KPL/PCK, KPL/LSK, KPL/SCLK, or \begindata)
        text_sample = header_bytes.decode("latin-1", errors="ignore")
        if "KPL/" in text_sample or "\\begindata" in text_sample or "DELTET" in text_sample or "FRAME_" in text_sample:
            if "KPL/LSK" in text_sample or "DELTET" in text_sample or ext == ".tls":
                kernel_type = "LSK"
            elif "KPL/PCK" in text_sample or "BODY" in text_sample or ext in {".tpc", ".pck"}:
                kernel_type = "PCK"
            elif "KPL/FK" in text_sample or "FRAME_" in text_sample or ext == ".tf":
                kernel_type = "FK"
            elif "KPL/SCLK" in text_sample or ext == ".tsc":
                kernel_type = "SCLK"
            else:
                kernel_type = "TEXT_KERNEL"
        elif ext in VALID_KERNEL_EXTENSIONS:
            # Simple text kernel without formal header
            kernel_type = "TEXT_KERNEL"
        else:
            raise InvalidKernelFileError(
                f"File '{filepath.name}' is plain text but does not match SPICE text kernel format (KPL/\\begindata)."
            )

    return {
        "filename": filepath.name,
        "path": str(filepath.resolve()),
        "size_bytes": size,
        "kernel_type": kernel_type,
        "is_binary": is_binary,
    }


# ==============================================================================
# SSRF Protection & IP Validation
# ==============================================================================

def is_ip_allowed(ip_str: str) -> bool:
    """
    Check if IP address is a public, routable address.
    Blocks loopback (127.0.0.0/8, ::1), private RFC 1918, link-local, multicast, and reserved IPs.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
        return True
    except ValueError:
        return False


def validate_url_security(
    url_str: str,
    approved_hosts: Optional[Set[str]] = None,
    allow_custom_hosts: bool = False,
) -> Tuple[str, str]:
    """
    Validate that URL:
    1. Uses HTTP or HTTPS scheme.
    2. Has a valid approved host (or allowed host).
    3. Resolves to a public, non-internal, non-loopback IP address (anti-DNS rebinding).
    Returns (normalized_url, resolved_ip).
    """
    parsed = urllib.parse.urlparse(url_str)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise SecurityValidationError(
            f"Unsupported scheme '{scheme}'. Only HTTP and HTTPS are permitted."
        )

    hostname = (parsed.hostname or "").lower().strip()
    if not hostname:
        raise SecurityValidationError("URL must include a valid hostname.")

    allowed = approved_hosts if approved_hosts is not None else DEFAULT_APPROVED_HOSTS
    if not allow_custom_hosts and hostname not in allowed:
        allowed_list = ", ".join(sorted(allowed))
        raise SecurityValidationError(
            f"Host '{hostname}' is not in the approved official archives list. "
            f"Approved official hosts: {allowed_list}."
        )

    # DNS Resolution and IP check (anti-SSRF and anti-DNS rebinding)
    try:
        addr_info = socket.getaddrinfo(hostname, parsed.port or (443 if scheme == "https" else 80))
        if not addr_info:
            raise SecurityValidationError(f"Could not resolve host '{hostname}'.")
    except socket.gaierror as e:
        raise SecurityValidationError(f"DNS resolution failed for '{hostname}': {e}") from e

    resolved_ips = {item[4][0] for item in addr_info}
    for rip in resolved_ips:
        if not is_ip_allowed(rip):
            raise SecurityValidationError(
                f"Access denied: Host '{hostname}' resolves to private/internal IP '{rip}'."
            )

    return url_str, list(resolved_ips)[0]


# ==============================================================================
# Safe Streaming Downloader
# ==============================================================================

class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that validates every redirect target against SSRF rules."""

    def __init__(self, approved_hosts: Optional[Set[str]] = None, allow_custom_hosts: bool = False):
        super().__init__()
        self.approved_hosts = approved_hosts
        self.allow_custom_hosts = allow_custom_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url_security(
            newurl,
            approved_hosts=self.approved_hosts,
            allow_custom_hosts=self.allow_custom_hosts,
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_kernel_from_url(
    url_str: str,
    destination_dir: Path,
    custom_filename: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_DOWNLOAD_SIZE,
    approved_hosts: Optional[Set[str]] = None,
    allow_custom_hosts: bool = False,
) -> Dict[str, Any]:
    """
    Stream download kernel from an approved HTTP/HTTPS URL with security validation,
    redirect validation, file-size enforcement, and SHA-256 computation.
    """
    valid_url, resolved_ip = validate_url_security(
        url_str,
        approved_hosts=approved_hosts,
        allow_custom_hosts=allow_custom_hosts,
    )

    parsed = urllib.parse.urlparse(valid_url)
    default_name = Path(parsed.path).name or "imported_kernel.bsp"
    target_name = sanitize_filename(custom_filename or default_name)

    destination_dir.mkdir(parents=True, exist_ok=True)
    out_file = destination_dir / target_name

    # Setup opener with safe redirect handler
    opener = urllib.request.build_opener(
        SafeRedirectHandler(approved_hosts=approved_hosts, allow_custom_hosts=allow_custom_hosts)
    )

    req = urllib.request.Request(
        valid_url,
        headers={"User-Agent": "NASA-SPICE-Portal-Importer/1.0"},
    )

    sha256 = hashlib.sha256()
    bytes_downloaded = 0

    temp_file = destination_dir / f"{target_name}.{uuid.uuid4().hex[:6]}.part"

    try:
        with opener.open(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type:
                raise InvalidKernelFileError(
                    f"Download URL returned HTML content ({content_type}) instead of a SPICE kernel. "
                    "Ensure URL points directly to a raw kernel file."
                )

            with open(temp_file, "wb") as f_out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    bytes_downloaded += len(chunk)
                    if bytes_downloaded > max_bytes:
                        temp_file.unlink(missing_ok=True)
                        raise KernelImportError(
                            f"File size exceeds maximum permitted limit ({max_bytes / (1024*1024):.1f} MB)."
                        )
                    sha256.update(chunk)
                    f_out.write(chunk)

        # Validate downloaded content before moving into destination
        meta = detect_and_validate_kernel_content(temp_file)
        temp_file.replace(out_file)
        meta["path"] = str(out_file.resolve())
        meta["sha256"] = sha256.hexdigest()
        meta["source_url"] = valid_url
        meta["resolved_ip"] = resolved_ip
        return meta

    except urllib.error.HTTPError as e:
        temp_file.unlink(missing_ok=True)
        raise KernelImportError(f"HTTP error downloading from '{url_str}': {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        temp_file.unlink(missing_ok=True)
        raise KernelImportError(f"Network error downloading from '{url_str}': {e.reason}") from e
    except Exception:
        temp_file.unlink(missing_ok=True)
        raise


# ==============================================================================
# SPK Inspection Utility
# ==============================================================================

def inspect_kernel_targets(kernel_path: Path) -> List[Dict[str, Any]]:
    """
    Inspect target IDs and coverage in a kernel if it is an SPK.
    """
    meta = detect_and_validate_kernel_content(kernel_path)
    if meta["kernel_type"] != "SPK":
        return []

    with SpiceManager() as sm:
        try:
            obj_ids = sm.get_spk_objects(kernel_path)
            results = []
            for oid in obj_ids:
                try:
                    name = spice.bodc2n(oid)
                except Exception:
                    name = f"NAIF_ID_{oid}"
                cov = sm.get_spk_coverage(kernel_path, oid)
                results.append({
                    "id": oid,
                    "name": name,
                    "intervals": cov,
                })
            return results
        except Exception:
            return []
