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


if __name__ == "__main__":
    unittest.main()
