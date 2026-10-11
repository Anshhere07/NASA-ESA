"""
Unit tests for server.py HTTP API endpoints and web portal handlers.
"""

import json
import threading
import time
import unittest
from http.server import HTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import spiceypy as spice

from server import SpicePortalExtendedHandler


TEST_PORT = 8089


class TestWebPortalEndpoints(unittest.TestCase):
    """Test web server API routing and security."""

    @classmethod
    def setUpClass(cls):
        spice.kclear()
        cls.httpd = HTTPServer(("127.0.0.1", TEST_PORT), SpicePortalExtendedHandler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        spice.kclear()

    def test_get_dashboard(self):
        req = Request(f"http://127.0.0.1:{TEST_PORT}/")
        with urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
            self.assertIn("NASA / ESA SPICE Data Converter & Portal", body)

    def test_get_available_kernels_api(self):
        req = Request(f"http://127.0.0.1:{TEST_PORT}/api/available-kernels")
        with urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("kernels", data)
            self.assertGreater(data["count"], 0)

    def test_get_archive_kernels_api(self):
        req = Request(f"http://127.0.0.1:{TEST_PORT}/api/archive-kernels")
        with urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            catalog = json.loads(resp.read().decode("utf-8"))
            self.assertIsInstance(catalog, list)
            self.assertGreater(len(catalog), 0)

    def test_post_jobs_and_poll(self):
        payload = {
            "target": "4",
            "observer": "399",
            "reference_frame": "J2000",
            "aberration_correction": "LT+S",
            "start_utc": "2026-10-11T00:00:00",
            "end_utc": "2026-10-11T01:00:00",
            "sampling_interval_hours": 1.0,
        }
        req = Request(
            f"http://127.0.0.1:{TEST_PORT}/api/jobs",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("job_id", data)
            job_id = data["job_id"]

        # Poll job status
        max_wait = 6.0
        start = time.time()
        job_data = None
        while time.time() - start < max_wait:
            poll_req = Request(f"http://127.0.0.1:{TEST_PORT}/api/jobs/{job_id}")
            with urlopen(poll_req) as poll_resp:
                job_data = json.loads(poll_resp.read().decode("utf-8"))
                if job_data.get("status") in ("COMPLETED", "FAILED"):
                    break
            time.sleep(0.3)

        self.assertIsNotNone(job_data)
        self.assertEqual(job_data.get("status"), "COMPLETED")

        # Test job data endpoint
        data_req = Request(f"http://127.0.0.1:{TEST_PORT}/api/jobs/{job_id}/data")
        with urlopen(data_req) as data_resp:
            self.assertEqual(data_resp.status, 200)
            records_data = json.loads(data_resp.read().decode("utf-8"))
            self.assertEqual(records_data["total"], 2)

    def test_import_url_rejects_ssrf(self):
        payload = {"url": "http://127.0.0.1/sensitive.bsp"}
        req = Request(
            f"http://127.0.0.1:{TEST_PORT}/api/import-url",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req) as resp:
                self.fail("Expected HTTP 403 for SSRF attempt.")
        except HTTPError as e:
            self.assertEqual(e.code, 403)
            data = json.loads(e.read().decode("utf-8"))
            self.assertIn("Security restriction", data["error"])

    def test_multipart_upload_endpoint(self):
        # Multipart boundary body
        boundary = "---------------------------974767299852498929531610575"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="custom_leap.tls"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"KPL/LSK\\begindata\r\nDELTET/DELTA_T_A = 32.184\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        req = Request(
            f"http://127.0.0.1:{TEST_PORT}/api/upload",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        with urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("uploaded_files", data)
            self.assertEqual(len(data["uploaded_files"]), 1)
            self.assertEqual(data["uploaded_files"][0]["kernel_type"], "LSK")


if __name__ == "__main__":
    unittest.main()
