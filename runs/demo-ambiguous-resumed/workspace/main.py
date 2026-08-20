"""Root convenience entrypoint re-exporting the ASGI application."""
from __future__ import annotations

from app.main import app, create_app, run

__all__ = ["app", "create_app", "run"]


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    run()
