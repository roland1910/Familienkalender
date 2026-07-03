"""Tests for the central source URL validation (SSRF protection)."""

import pytest

from app.url_validation import SourceURLError, validate_source_url


class TestScheme:
    def test_https_is_accepted(self) -> None:
        assert validate_source_url("https://cloud.example.com/dav/") == (
            "https://cloud.example.com/dav/"
        )

    def test_http_is_rejected_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FAMILIENKALENDER_ALLOW_HTTP", raising=False)
        with pytest.raises(SourceURLError, match="https"):
            validate_source_url("http://cloud.example.com/dav/")

    def test_http_is_allowed_with_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Local test servers (E2E stubs) have no TLS.
        monkeypatch.setenv("FAMILIENKALENDER_ALLOW_HTTP", "1")
        validate_source_url("http://127.0.0.1:8123/dav/")

    def test_other_schemes_are_rejected(self) -> None:
        for url in ("ftp://cloud.example.com/", "file:///etc/passwd", "javascript:alert(1)"):
            with pytest.raises(SourceURLError):
                validate_source_url(url)

    def test_garbage_is_rejected(self) -> None:
        for url in ("", "kein url", "https://", "https:///pfad"):
            with pytest.raises(SourceURLError):
                validate_source_url(url)


class TestUserinfo:
    def test_userinfo_is_rejected(self) -> None:
        with pytest.raises(SourceURLError, match="Zugangsdaten"):
            validate_source_url("https://user:pass@cloud.example.com/dav/")

    def test_username_only_is_rejected(self) -> None:
        with pytest.raises(SourceURLError):
            validate_source_url("https://user@cloud.example.com/dav/")


class TestForbiddenNetworks:
    def test_link_local_v4_is_rejected(self) -> None:
        with pytest.raises(SourceURLError, match="nicht erlaubt"):
            validate_source_url("https://169.254.10.20/dav/")

    def test_loopback_v4_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FAMILIENKALENDER_ALLOW_HTTP", raising=False)
        with pytest.raises(SourceURLError, match="nicht erlaubt"):
            validate_source_url("https://127.0.0.1/dav/")

    def test_loopback_v6_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FAMILIENKALENDER_ALLOW_HTTP", raising=False)
        with pytest.raises(SourceURLError, match="nicht erlaubt"):
            validate_source_url("https://[::1]/dav/")

    def test_loopback_is_allowed_in_dev_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # FAMILIENKALENDER_ALLOW_HTTP marks local development/E2E stubs,
        # which listen on 127.0.0.1 — never set in the add-on container.
        monkeypatch.setenv("FAMILIENKALENDER_ALLOW_HTTP", "1")
        validate_source_url("https://127.0.0.1:8123/dav/")

    def test_reserved_range_is_rejected(self) -> None:
        with pytest.raises(SourceURLError, match="nicht erlaubt"):
            validate_source_url("https://240.0.0.1/dav/")

    def test_link_local_v6_is_rejected(self) -> None:
        with pytest.raises(SourceURLError):
            validate_source_url("https://[fe80::1]/dav/")

    def test_multicast_is_rejected(self) -> None:
        with pytest.raises(SourceURLError):
            validate_source_url("https://224.0.0.1/dav/")

    def test_unspecified_address_is_rejected(self) -> None:
        with pytest.raises(SourceURLError):
            validate_source_url("https://0.0.0.0/dav/")

    def test_ha_internal_network_is_rejected(self) -> None:
        # 172.30.32.0/23 is the HA supervisor/add-on network.
        for host in ("172.30.32.1", "172.30.32.2", "172.30.33.254"):
            with pytest.raises(SourceURLError):
                validate_source_url(f"https://{host}/dav/")

    def test_neighbouring_private_networks_are_allowed(self) -> None:
        # Ordinary LAN targets (the family Nextcloud) must stay reachable.
        for host in ("172.30.34.1", "192.168.1.10", "10.0.0.5"):
            validate_source_url(f"https://{host}/dav/")

    def test_hostnames_are_allowed(self) -> None:
        validate_source_url("https://cloud.example.com/remote.php/dav/")

    def test_lan_ip_literals_stay_allowed(self) -> None:
        # Design decision: private LAN ranges stay reachable — a Nextcloud
        # on the home network is the primary use case.
        validate_source_url("https://192.168.1.50/dav/")


class TestNumericHostnames:
    """inet_aton-style notations that bypass the IP-literal check.

    ``ipaddress`` does not parse them, but glibc resolvers do (decimal
    2130706433, hex 0x7f000001, octal 0177.0.0.1 → 127.0.0.1). The current
    musl base image does not resolve them, but the check is defensive
    against a base-image change.
    """

    def test_decimal_notation_is_rejected(self) -> None:
        with pytest.raises(SourceURLError, match="Hostname"):
            validate_source_url("https://2130706433/dav/")

    def test_hex_notation_is_rejected(self) -> None:
        with pytest.raises(SourceURLError, match="Hostname"):
            validate_source_url("https://0x7f000001/dav/")

    def test_octal_dotted_notation_is_rejected(self) -> None:
        with pytest.raises(SourceURLError, match="Hostname"):
            validate_source_url("https://0177.0.0.1/dav/")

    def test_short_dotted_numeric_is_rejected(self) -> None:
        # inet_aton("127.1") is 127.0.0.1.
        with pytest.raises(SourceURLError, match="Hostname"):
            validate_source_url("https://127.1/dav/")

    def test_error_messages_are_german(self) -> None:
        with pytest.raises(SourceURLError) as excinfo:
            validate_source_url("http://cloud.example.com/")
        assert "URL" in str(excinfo.value)
