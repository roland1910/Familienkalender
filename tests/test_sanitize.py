"""Tests for the error-message sanitizer."""

from app.sanitize import MAX_ERROR_LENGTH, sanitize_error


class TestSanitizeError:
    def test_strips_userinfo_from_urls(self) -> None:
        text = "Fetch failed: https://roland:secret-pw@cloud.example.com/dav/ timed out"
        cleaned = sanitize_error(text)
        assert "secret-pw" not in cleaned
        assert "roland:" not in cleaned
        assert "https://cloud.example.com/dav/" in cleaned

    def test_strips_userinfo_without_password(self) -> None:
        assert sanitize_error("GET http://user@host/x") == "GET http://host/x"

    def test_handles_multiple_urls(self) -> None:
        text = "a https://u1:p1@h1/ b http://u2:p2@h2/ c"
        cleaned = sanitize_error(text)
        assert "p1" not in cleaned
        assert "p2" not in cleaned

    def test_truncates_to_max_length(self) -> None:
        assert len(sanitize_error("x" * 10_000)) == MAX_ERROR_LENGTH
        assert MAX_ERROR_LENGTH == 500

    def test_plain_message_is_unchanged(self) -> None:
        assert sanitize_error("Server unreachable") == "Server unreachable"
