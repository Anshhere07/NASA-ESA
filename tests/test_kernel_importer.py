"""
Unit tests for kernel_importer.py

Tests:
1. Filename sanitization against path traversal.
2. Content-based kernel detection (binary DAF, text KPL).
3. Rejection of corrupt, non-SPICE, and empty files.
4. SSRF protection: blocking loopback, private IPs, link-local, unapproved hosts, invalid schemes.
5. SPK target inspection.
"""

import tempfile
import unittest
from pathlib import Path

from kernel_importer import (
    sanitize_filename,
    detect_and_validate_kernel_content,
    validate_url_security,
    is_ip_allowed,
    inspect_kernel_targets,
    SecurityValidationError,
    InvalidKernelFileError,
)


class TestKernelImporterSecurityAndValidation(unittest.TestCase):
    """Test kernel ingestion security and validation."""

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("../../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("C:\\Windows\\system32\\cmd.exe"), "cmd.exe")
        self.assertEqual(sanitize_filename("valid_kernel.bsp"), "valid_kernel.bsp")
        self.assertTrue(sanitize_filename(".hidden.tls").startswith("kernel_"))

    def test_detect_and_validate_real_kernels(self):
        # Real LSK
        lsk_meta = detect_and_validate_kernel_content(Path("kernels/naif0012.tls"))
        self.assertEqual(lsk_meta["kernel_type"], "LSK")
        self.assertFalse(lsk_meta["is_binary"])

        # Real PCK
        pck_meta = detect_and_validate_kernel_content(Path("kernels/pck00010.tpc"))
        self.assertEqual(pck_meta["kernel_type"], "PCK")

        # Real SPK
        spk_meta = detect_and_validate_kernel_content(Path("kernels/de440s.bsp"))
        self.assertEqual(spk_meta["kernel_type"], "SPK")
        self.assertTrue(spk_meta["is_binary"])

    def test_reject_empty_and_corrupt_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Empty file
            empty_path = Path(tmp_dir) / "empty.bsp"
            empty_path.write_bytes(b"")
            with self.assertRaises(InvalidKernelFileError):
                detect_and_validate_kernel_content(empty_path)

            # Random text not matching SPICE
            garbage_path = Path(tmp_dir) / "garbage.txt"
            garbage_path.write_text("Hello world, this is a random text file.", encoding="utf-8")
            with self.assertRaises(InvalidKernelFileError):
                detect_and_validate_kernel_content(garbage_path)

    def test_ssrf_ip_filtering(self):
        # Loopback
        self.assertFalse(is_ip_allowed("127.0.0.1"))
        self.assertFalse(is_ip_allowed("::1"))

        # Private RFC 1918
        self.assertFalse(is_ip_allowed("10.0.0.1"))
        self.assertFalse(is_ip_allowed("192.168.1.1"))
        self.assertFalse(is_ip_allowed("172.16.0.1"))

        # Link-local AWS/cloud metadata
        self.assertFalse(is_ip_allowed("169.254.169.254"))

        # Public routable
        self.assertTrue(is_ip_allowed("8.8.8.8"))

    def test_ssrf_url_validation_blocks_attacks(self):
        # Invalid schemes
        with self.assertRaises(SecurityValidationError):
            validate_url_security("file:///etc/passwd")

        with self.assertRaises(SecurityValidationError):
            validate_url_security("ftp://naif.jpl.nasa.gov/kernel.bsp")

        # Loopback / local
        with self.assertRaises(SecurityValidationError):
            validate_url_security("http://127.0.0.1/test.bsp")

        with self.assertRaises(SecurityValidationError):
            validate_url_security("http://localhost:8000/test.bsp")

        # Unapproved external host
        with self.assertRaises(SecurityValidationError):
            validate_url_security("https://attacker.evil.com/fake_kernel.bsp")

    def test_ssrf_url_validation_allows_official_nasa_host(self):
        # Approved official NASA host
        url, ip = validate_url_security(
            "https://naif.jpl.nasa.gov/pub/naif/generic_kernels/lsk/naif0012.tls"
        )
        self.assertTrue(url.startswith("https://naif.jpl.nasa.gov"))
        self.assertTrue(is_ip_allowed(ip))

    def test_inspect_kernel_targets(self):
        targets = inspect_kernel_targets(Path("kernels/de440s.bsp"))
        self.assertGreater(len(targets), 0)
        target_ids = [t["id"] for t in targets]
        self.assertIn(4, target_ids)   # Mars barycenter
        self.assertIn(399, target_ids) # Earth


if __name__ == "__main__":
    unittest.main()
