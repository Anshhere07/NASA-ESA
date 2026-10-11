"""
Unit tests validating deployment entrypoints for Vercel, WSGI, and Serverless runtimes.
"""

import inspect
import unittest
from http.server import BaseHTTPRequestHandler

import main
import server


class TestDeploymentEntrypoints(unittest.TestCase):
    def test_server_exports_deployment_variables(self):
        """Verify server.py exports handler, app, and application."""
        self.assertTrue(hasattr(server, "handler"), "server.py must export 'handler'")
        self.assertTrue(hasattr(server, "app"), "server.py must export 'app'")
        self.assertTrue(hasattr(server, "application"), "server.py must export 'application'")

        # handler must be a subclass of BaseHTTPRequestHandler for native Vercel python runtime
        self.assertTrue(inspect.isclass(server.handler))
        self.assertTrue(issubclass(server.handler, BaseHTTPRequestHandler))

        # app and application must be callable for WSGI / ASGI runners
        self.assertTrue(callable(server.app))
        self.assertTrue(callable(server.application))

    def test_main_exports_deployment_variables(self):
        """Verify main.py also exports handler, app, and application to prevent detection ambiguity."""
        self.assertTrue(hasattr(main, "handler"), "main.py must export 'handler'")
        self.assertTrue(hasattr(main, "app"), "main.py must export 'app'")
        self.assertTrue(hasattr(main, "application"), "main.py must export 'application'")

    def test_wsgi_adapter_execution(self):
        """Verify the WSGIAdapter executes requests and returns valid HTTP status and headers."""
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/",
            "QUERY_STRING": "",
            "wsgi.input": None,
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "8000",
            "REMOTE_ADDR": "127.0.0.1",
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        body_iterable = server.app(environ, start_response)
        self.assertIn("200", captured["status"])
        self.assertTrue(len(body_iterable) > 0)
        self.assertIn(b"NASA / ESA SPICE", body_iterable[0])

    def test_api_index_entrypoint(self):
        """Verify api/index.py correctly exports handler, app, and application."""
        import sys
        from pathlib import Path
        api_dir = str(Path(__file__).resolve().parent.parent / "api")
        if api_dir not in sys.path:
            sys.path.insert(0, api_dir)
        import index
        self.assertTrue(hasattr(index, "handler"))
        self.assertTrue(hasattr(index, "app"))
        self.assertTrue(hasattr(index, "application"))

    def test_asgi_adapter_execution(self):
        """Verify the UniversalAdapter executes ASGI requests asynchronously."""
        import asyncio
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [],
            "http_version": "1.1",
        }
        sent_messages = []

        async def mock_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def mock_send(msg):
            sent_messages.append(msg)

        async def run_asgi():
            await server.app(scope, mock_receive, mock_send)

        asyncio.run(run_asgi())
        self.assertEqual(len(sent_messages), 2)
        self.assertEqual(sent_messages[0]["type"], "http.response.start")
        self.assertEqual(sent_messages[0]["status"], 200)
        self.assertEqual(sent_messages[1]["type"], "http.response.body")
        self.assertIn(b"NASA / ESA SPICE", sent_messages[1]["body"])

    def test_vercel_rewrite_path_normalization(self):
        """Verify that /api, /api/, and /api/index.py all serve the dashboard (no 404)."""
        for test_path in ("/", "/api", "/api/", "/api/index.py", "/dashboard"):
            environ = {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": test_path,
                "QUERY_STRING": "",
                "wsgi.input": None,
            }
            captured = {}
            def start_response(status, headers):
                captured["status"] = status
            chunks = server.app(environ, start_response)
            self.assertIn("200", captured["status"], f"Expected 200 for path: {test_path}")
            self.assertIn(b"NASA / ESA SPICE", b"".join(chunks))


if __name__ == "__main__":
    unittest.main()
