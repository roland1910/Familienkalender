"""Tests for the calendar index page."""

import re

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, client=("127.0.0.1", 50000))


def test_index_returns_html_with_title() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Familienkalender" in response.text


def test_index_uses_only_relative_asset_urls() -> None:
    """Absolute local URLs (href="/...") break behind HA ingress.

    The page is served under /api/hassio_ingress/<token>/, so every asset
    reference must be relative to the current URL.
    """
    response = client.get("/")
    absolute_refs = re.findall(r'(?:href|src)="/[^/"]', response.text)
    assert absolute_refs == [], f"absolute local URLs found: {absolute_refs}"


def test_static_stylesheet_is_served() -> None:
    response = client.get("/static/css/calendar.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_index_contains_calendar_shell() -> None:
    """The page ships the calendar UI shell (views are rendered client-side)."""
    response = client.get("/")
    assert 'id="calendar"' in response.text
    assert 'id="btn-today"' in response.text
    assert "static/js/main.js" in response.text
