"""SSRF protection tests."""

from __future__ import annotations

import pytest

import tools
from omni_agent.security.ssrf import UnsafeURLError, validate_url_static


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://user:pass@example.com/",
        "http://example.com:22/",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_url_static(url)


def test_public_url_is_accepted() -> None:
    validated = validate_url_static("https://example.com/path?q=1")
    assert validated.host == "example.com"
    assert validated.port == 443


def test_private_literal_can_be_opted_in() -> None:
    validated = validate_url_static("http://127.0.0.1:8080/", allow_private=True)
    assert validated.port == 8080


def test_web_fetch_blocks_loopback_before_connecting() -> None:
    result = tools.tool_web_fetch("http://127.0.0.1:9/")
    assert "安全策略" in result or "SSRF" in result


def test_web_fetch_blocks_metadata_address() -> None:
    result = tools.tool_web_fetch("http://169.254.169.254/latest/meta-data/")
    assert "安全策略" in result or "SSRF" in result
