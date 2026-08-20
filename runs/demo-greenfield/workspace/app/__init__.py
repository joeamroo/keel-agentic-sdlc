"""URL shortener service package.

Exposes :func:`app.main.create_app`, the application factory, and the module
level ``app`` ASGI application used by uvicorn.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
