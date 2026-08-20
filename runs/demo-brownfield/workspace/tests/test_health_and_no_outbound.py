"""Health endpoint and the promise that no destination is ever fetched."""
import socket

import pytest


def test_health_reports_ok_when_the_database_answers(app):
    response = app.client.get("/health")

    assert response.status_code == 200, response.text[:200]
    assert response.json()["status"] == "ok", response.text[:200]


def test_neither_creation_nor_redirect_opens_an_outbound_connection(app, monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("the service tried to contact the destination")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    try:
        import httpx

        async def refuse_async(*args, **kwargs):
            attempts.append(args)
            raise AssertionError("the service built an HTTP client for the destination")

        monkeypatch.setattr(httpx.AsyncClient, "send", refuse_async, raising=False)
    except ImportError:  # pragma: no cover
        pass
    try:
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", refuse)
    except ImportError:  # pragma: no cover
        pass

    created = app.create("https://example.com/page?a=1")
    assert created.status_code == 201, created.text[:200]
    code = created.json()["code"]

    redirected = app.visit(code)
    assert redirected.status_code == 307
    assert redirected.headers["location"] == "https://example.com/page?a=1"
    assert attempts == [], "an outbound connection was attempted"
