"""
Vercel Serverless Function entrypoint.
Imports and exposes the SpicePortalExtendedHandler, WSGI, and ASGI applications.
"""

import sys
from pathlib import Path

# Add project root directory to sys.path so modules and templates are reachable
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from server import SpicePortalExtendedHandler, handler, app, application

__all__ = ["SpicePortalExtendedHandler", "handler", "app", "application"]
