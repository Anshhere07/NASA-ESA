"""
SPICE Kernel Management and Time Conversion Module.

Provides robust, controlled management of NASA/ESA SPICE kernels, kernel discovery,
coverage inspection, and UTC-to-ET time conversion using spiceypy.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, List, Dict, Optional, Tuple, Union

import spiceypy as spice
from spiceypy.utils.exceptions import NotFoundError, SpiceyError


# ==============================================================================
# Custom Exceptions
# ==============================================================================

class SpiceManagerError(Exception):
    """Base exception for all SPICE manager errors."""
    pass


class KernelNotFoundError(SpiceManagerError):
    """Raised when a specified kernel file does not exist on disk."""
    pass


class KernelReadError(SpiceManagerError):
    """Raised when a kernel file exists but is not readable."""
    pass


class MissingKernelError(SpiceManagerError):
    """Raised when a required kernel type (LSK, SPK, PCK, etc.) is missing from the kernel pool."""
    pass


class UnsupportedTargetError(SpiceManagerError):
    """Raised when a requested target or body ID is not available in the specified SPK."""
    pass


class CoverageError(SpiceManagerError):
    """Raised when a requested epoch ET falls outside the coverage window for a target."""
    pass


class SpiceTimeConversionError(SpiceManagerError):
    """Raised when converting a time string to ET fails due to format or parse errors."""
    pass


# ==============================================================================
# Timestamp Validation Patterns
# ==============================================================================

# Matches ISO 8601 variants: YYYY-MM-DDTHH:MM:SS[.sss][Z| UTC] or with space separator
_ISO_DATETIME_PATTERN = re.compile(
    r"^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
    r"([T ]([01]\d|2[0-3]):([0-5]\d):([0-5]\d)(\.\d+)?(Z| UTC)?)?$"
)

# Matches SPICE calendar formats: YYYY-MON-DD [HH:MM:SS] or DD-MON-YYYY
_SPICE_CALENDAR_PATTERN = re.compile(
    r"^(\d{4}-[A-Za-z]{3}-\d{1,2}|\d{1,2}-[A-Za-z]{3}-\d{4})"
    r"([T ]([01]\d|2[0-3]):([0-5]\d):([0-5]\d)(\.\d+)?(Z| UTC)?)?$"
)


def validate_utc_timestamp_format(utc_timestamp: str) -> None:
    """
    Validate that the UTC timestamp has a recognizable and well-formed structure.

    Does NOT mutate, strip, or alter the input string.
    Raises TypeError or SpiceTimeConversionError if invalid.
    """
    if not isinstance(utc_timestamp, str):
        raise TypeError(
            f"utc_timestamp must be a string, got {type(utc_timestamp).__name__}."
        )

    if len(utc_timestamp) == 0:
        raise ValueError("utc_timestamp cannot be empty.")

    if not (_ISO_DATETIME_PATTERN.match(utc_timestamp) or _SPICE_CALENDAR_PATTERN.match(utc_timestamp)):
        raise SpiceTimeConversionError(
            f"Invalid UTC timestamp format: '{utc_timestamp}'. "
            "Expected ISO 8601 (e.g. '2026-10-01T23:00:00' or '2026-10-01T23:00:00Z') "
            "or SPICE calendar format (e.g. '2026-OCT-01 23:00:00')."
        )


# ==============================================================================
# Standalone UTC-to-ET Conversion
# ==============================================================================

def is_lsk_loaded() -> bool:
    """
    Check whether a Leap-Seconds Kernel (LSK) is currently active in the kernel pool.

    Verifies presence of the DELTET/DELTA_T_A variable in the kernel pool.
    """
    try:
        spice.dtpool("DELTET/DELTA_T_A")
        return True
    except (NotFoundError, SpiceyError):
        return False


def utc_to_et(utc_timestamp: str) -> float:
    """
    Convert a UTC timestamp string to SPICE Ephemeris Time (ET in seconds past J2000).

    Requirements enforced:
    - Input must be a valid string format (e.g. '2026-10-01T23:00:00').
    - Input is never silently modified or trimmed.
    - Requires an active leap-seconds kernel (LSK) in the kernel pool.
    - Returns ET as a float.
    - Raises actionable error messages upon failure.
    """
    validate_utc_timestamp_format(utc_timestamp)

    if not is_lsk_loaded():
        raise MissingKernelError(
            "Leap-seconds kernel (LSK) is not loaded in the SPICE pool. "
            "A valid LSK (e.g. 'naif0012.tls') must be furnished via spice.furnsh() "
            "or SpiceManager.load_kernel() before converting UTC to ET."
        )

    try:
        # Never alter the supplied timestamp string
        et = spice.str2et(utc_timestamp)
        return float(et)
    except SpiceyError as e:
        raise SpiceTimeConversionError(
            f"SPICE failed to convert timestamp '{utc_timestamp}' to ET: {e}"
        ) from e


# ==============================================================================
# SPICE Manager Class
# ==============================================================================

class SpiceManager:
    """
    Managed lifecycle for loading, inspecting, and unloading SPICE kernels.

    Tracks kernels loaded by this instance to guarantee controlled unloading
    without inadvertently clearing unrelated SPICE kernel state.
    """

    # Kernel extension mappings
    KERNEL_EXTENSIONS = {
        "lsk": {".tls", ".ti"},
        "spk": {".bsp", ".spk"},
        "pck": {".tpc", ".bpc"},
        "fk": {".tf"},
        "sclk": {".tsc"},
        "ik": {".ti"},
    }

    def __init__(self, kernels_dir: Optional[Union[str, Path]] = None) -> None:
        self.kernels_dir = Path(kernels_dir) if kernels_dir else Path("kernels")
        self._loaded_kernels: List[str] = []

    def __enter__(self) -> "SpiceManager":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.unload_all()

    @property
    def loaded_kernels(self) -> List[str]:
        """Return a copy of the list of kernels loaded by this manager instance."""
        return list(self._loaded_kernels)

    # --------------------------------------------------------------------------
    # Kernel Discovery & Validation
    # --------------------------------------------------------------------------

    def discover_kernels(self, directory: Optional[Union[str, Path]] = None) -> Dict[str, List[Path]]:
        """
        Discover and categorize available kernel files in the specified directory.

        Returns a dictionary mapping kernel types ('lsk', 'spk', 'pck', 'fk', 'sclk', 'other')
        to lists of discovered Path objects.
        """
        target_dir = Path(directory) if directory else self.kernels_dir

        if not target_dir.exists() or not target_dir.is_dir():
            raise KernelNotFoundError(
                f"Kernels directory '{target_dir}' does not exist or is not a directory."
            )

        categorized: Dict[str, List[Path]] = {
            "lsk": [],
            "spk": [],
            "pck": [],
            "fk": [],
            "sclk": [],
            "other": [],
        }

        for path in sorted(target_dir.iterdir()):
            if path.is_file():
                ext = path.suffix.lower()
                matched = False
                for ktype, extensions in self.KERNEL_EXTENSIONS.items():
                    if ext in extensions:
                        categorized[ktype].append(path)
                        matched = True
                        break
                if not matched:
                    categorized["other"].append(path)

        return categorized

    @staticmethod
    def validate_kernel_file(kernel_path: Union[str, Path]) -> Path:
        """
        Validate that a kernel file exists and is readable.

        Returns resolved Path object or raises KernelNotFoundError/KernelReadError.
        """
        path = Path(kernel_path).resolve()
        if not path.exists():
            raise KernelNotFoundError(
                f"Kernel file not found: '{kernel_path}'. "
                "Verify that the file path is correct and the kernel has been downloaded."
            )
        if not path.is_file():
            raise KernelReadError(
                f"Kernel path is not a file: '{kernel_path}'."
            )
        if not os.access(str(path), os.R_OK):
            raise KernelReadError(
                f"Kernel file is not readable (check permissions): '{kernel_path}'."
            )
        return path

    # --------------------------------------------------------------------------
    # Loading & Controlled Unloading
    # --------------------------------------------------------------------------

    def load_kernel(self, kernel_path: Union[str, Path]) -> str:
        """
        Validate and load a single kernel using spice.furnsh().

        Tracks loaded kernels so they can be safely released later.
        Returns the normalized path of the loaded kernel.
        """
        valid_path = self.validate_kernel_file(kernel_path)
        path_str = str(valid_path)

        # Furnish kernel
        spice.furnsh(path_str)

        if path_str not in self._loaded_kernels:
            self._loaded_kernels.append(path_str)

        return path_str

    def load_kernels(self, kernel_paths: Iterable[Union[str, Path]]) -> List[str]:
        """Load multiple kernels in order and register them with this manager."""
        loaded = []
        for kp in kernel_paths:
            loaded.append(self.load_kernel(kp))
        return loaded

    def unload_kernel(self, kernel_path: Union[str, Path]) -> None:
        """
        Unload a specific kernel from SPICE and remove it from tracking.

        Does not affect any other loaded kernels.
        """
        path = Path(kernel_path).resolve()
        path_str = str(path)

        spice.unload(path_str)

        if path_str in self._loaded_kernels:
            self._loaded_kernels.remove(path_str)

    def unload_all(self) -> None:
        """
        Unload all kernels tracked by this manager instance in reverse loading order.

        Preserves any kernels loaded externally outside this SpiceManager instance.
        """
        while self._loaded_kernels:
            k = self._loaded_kernels.pop()
            try:
                spice.unload(k)
            except SpiceyError:
                pass

    # --------------------------------------------------------------------------
    # Inspection of Loaded Kernels
    # --------------------------------------------------------------------------

    @staticmethod
    def get_loaded_kernels(kind: str = "ALL") -> List[Dict[str, Any]]:
        """
        Inspect all currently loaded kernels in the SPICE subsystem using spice.ktotal() and spice.kdata().

        Returns list of dictionaries containing file, type, source, and handle.
        """
        total = spice.ktotal(kind)
        kernels_info = []
        for i in range(total):
            file_name, filtyp, src, handle = spice.kdata(i, kind)
            kernels_info.append({
                "index": i,
                "file": file_name,
                "type": filtyp,
                "source": src,
                "handle": handle,
            })
        return kernels_info

    def is_kernel_loaded(self, kernel_path: Union[str, Path]) -> bool:
        """Check if a specific kernel is currently loaded in SPICE."""
        path_str = str(Path(kernel_path).resolve())
        loaded = self.get_loaded_kernels("ALL")
        for k in loaded:
            if Path(k["file"]).resolve() == Path(path_str):
                return True
        return False

    def validate_required_kernels(
        self,
        require_lsk: bool = True,
        require_spk: bool = False,
        require_pck: bool = False,
    ) -> None:
        """
        Verify that specified essential kernel types are present in the kernel pool.

        Raises MissingKernelError with clear diagnostic messages if any are absent.
        """
        if require_lsk and not is_lsk_loaded():
            raise MissingKernelError(
                "Leap-seconds kernel (LSK) is missing from the active SPICE pool. "
                "Load an LSK file (e.g. 'naif0012.tls') using manager.load_kernel()."
            )

        if require_spk and spice.ktotal("SPK") == 0:
            raise MissingKernelError(
                "No Spacecraft/Planetary Ephemeris Kernel (SPK) is currently loaded. "
                "Load an SPK file (e.g. 'de440s.bsp') using manager.load_kernel()."
            )

        if require_pck and spice.ktotal("TEXT") == 0:
            raise MissingKernelError(
                "No Planetary Constants Kernel (PCK) is currently loaded. "
                "Load a PCK file (e.g. 'pck00010.tpc') using manager.load_kernel()."
            )

    # --------------------------------------------------------------------------
    # SPK Object & Coverage Inspection (Generic, no hardcoded targets)
    # --------------------------------------------------------------------------

    def get_spk_objects(self, spk_path: Union[str, Path]) -> List[int]:
        """
        Identify all target IDs present in the given SPK file using spice.spkobj().

        Does not assume any specific planetary body or spacecraft is present.
        """
        valid_path = self.validate_kernel_file(spk_path)
        ids_cell = spice.spkobj(str(valid_path))
        return sorted(list(ids_cell))

    def get_spk_coverage(
        self,
        spk_path: Union[str, Path],
        obj_id: int,
    ) -> List[Dict[str, Any]]:
        """
        Extract coverage intervals for a specific target ID in an SPK using spice.spkcov().

        If an LSK is currently active, formatted UTC strings are also provided.
        """
        valid_path = self.validate_kernel_file(spk_path)
        cover_cell = spice.spkcov(str(valid_path), int(obj_id))
        num_intervals = spice.wncard(cover_cell)

        lsk_available = is_lsk_loaded()
        intervals = []

        for i in range(num_intervals):
            b_et, e_et = spice.wnfetd(cover_cell, i)
            b_utc = None
            e_utc = None
            if lsk_available:
                try:
                    b_utc = spice.timout(b_et, "YYYY-MON-DD HR:MN:SC.### UTC")
                    e_utc = spice.timout(e_et, "YYYY-MON-DD HR:MN:SC.### UTC")
                except SpiceyError:
                    pass

            intervals.append({
                "start_et": float(b_et),
                "end_et": float(e_et),
                "start_utc": b_utc,
                "end_utc": e_utc,
            })

        return intervals

    def get_spk_inventory(self, spk_path: Union[str, Path]) -> Dict[int, Dict[str, Any]]:
        """
        Generate a complete, generic inventory of all objects and coverage in an SPK.

        Resolves NAIF body names where available in the toolkit, else labels as 'UNKNOWN'.
        """
        valid_path = self.validate_kernel_file(spk_path)
        object_ids = self.get_spk_objects(valid_path)

        inventory: Dict[int, Dict[str, Any]] = {}
        for obj_id in object_ids:
            try:
                name = spice.bodc2n(obj_id)
            except (NotFoundError, SpiceyError):
                name = "UNKNOWN"

            cov = self.get_spk_coverage(valid_path, obj_id)
            inventory[obj_id] = {
                "name": name,
                "intervals": cov,
            }

        return inventory

    def resolve_target_id(self, target: Union[int, str]) -> Tuple[int, str]:
        """
        Resolve a target identifier (integer ID or name string) into (target_id, target_name).

        Raises UnsupportedTargetError if a string name cannot be recognized by SPICE.
        """
        if isinstance(target, int):
            try:
                name = spice.bodc2n(target)
            except (NotFoundError, SpiceyError):
                name = f"NAIF_ID_{target}"
            return target, name

        if isinstance(target, str):
            # Check if it's an integer encoded as a string
            try:
                int_id = int(target.strip())
                return self.resolve_target_id(int_id)
            except ValueError:
                pass

            # Resolve name via bodn2c
            try:
                int_id = spice.bodn2c(target.strip().upper())
                return int_id, target.strip().upper()
            except (NotFoundError, SpiceyError):
                raise UnsupportedTargetError(
                    f"Target name '{target}' could not be resolved to a NAIF integer ID. "
                    "Ensure target name is standard in SPICE or provide the integer ID directly."
                )

        raise TypeError(f"Target must be int or str, got {type(target).__name__}.")

    def check_target_coverage(
        self,
        spk_path: Union[str, Path],
        target: Union[int, str],
        et: Optional[float] = None,
    ) -> Tuple[int, str, List[Dict[str, Any]]]:
        """
        Validate that target is supported by the SPK, and optionally that ET is within coverage.

        Raises UnsupportedTargetError if target is absent.
        Raises CoverageError if ET is specified and out of coverage.
        """
        valid_path = self.validate_kernel_file(spk_path)
        available_ids = self.get_spk_objects(valid_path)

        target_id, target_name = self.resolve_target_id(target)

        if target_id not in available_ids:
            available_details = [
                f"{aid} ({spice.bodc2n(aid) if self._can_resolve(aid) else 'UNKNOWN'})"
                for aid in available_ids
            ]
            raise UnsupportedTargetError(
                f"Target '{target}' (resolved ID {target_id}) is not present in SPK '{spk_path}'. "
                f"Available target IDs in this SPK are: {', '.join(available_details)}."
            )

        intervals = self.get_spk_coverage(valid_path, target_id)

        if et is not None:
            in_range = any(iv["start_et"] <= et <= iv["end_et"] for iv in intervals)
            if not in_range:
                ranges_str = "; ".join(
                    f"[{iv['start_utc'] or iv['start_et']} to {iv['end_utc'] or iv['end_et']}]"
                    for iv in intervals
                )
                raise CoverageError(
                    f"Requested epoch ET={et} is outside the valid coverage range for "
                    f"target {target_name} (ID {target_id}) in SPK '{spk_path}'. "
                    f"Valid coverage interval(s): {ranges_str}."
                )

        return target_id, target_name, intervals

    @staticmethod
    def _can_resolve(obj_id: int) -> bool:
        try:
            spice.bodc2n(obj_id)
            return True
        except (NotFoundError, SpiceyError):
            return False
