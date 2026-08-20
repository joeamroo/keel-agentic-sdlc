"""Convenience entry point re-exporting the ASGI application.

Allows ``uvicorn main:app`` in addition to ``uvicorn app.main:app``.
"""

from __future__ import annotations

from app.main import app, create_app

__all__ = ["app", "create_app"]
