"""Tests for the optional web search backends and tool formatting."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import search
import tools
from omni_agent.config import get_settings


@pytest.fixture
def search_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    settings = get_settings()
    monkeypatch.setattr(settings, "search_provider", "disabled")
    monkeypatch.setattr(settings, "search_api_key", "")
    monkeypatch.setattr(settings, "search_base_url", "")
    monkeypatch.setattr(settings, "search_max_results", 5)
    monkeypatch.setattr(settings, "search_max_results_limit", 10)
    monkeypatch.setattr(settings, "search_max_query_chars", 500)
    monkeypatch.setattr(settings, "search_timeout_seconds", 10.0)
    monkeypatch.setattr(settings, "search_language", "zh-CN")
    monkeypatch.setattr(settings, "search_allow_private_network", False)
    # The unit tests must never depend on DNS availability.
    monkeypatch.setattr(search, "_endpoint", lambda url, allow_private: url)
    return settings


def _mock_client(handler: Any) -> Any:
    def factory(timeout: float) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    return factory


def test_disabled_search_reports_configuration(search_settings: Any) -> None:
    with pytest.raises(search.SearchError, match="联网搜索未启用"):
        search.search_web("anything")


def test_tavily_success_and_request_payload(
    search_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    search_settings.search_provider = "tavily"
    search_settings.search_api_key = "tvly-test"
    search_settings.search_base_url = "https://search.example.com"
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Example",
                        "url": "https://example.com/a",
                        "content": "A useful page",
                        "score": 0.91234,
                    },
                    {"title": "Bad", "url": "file:///etc/passwd"},
                    {"title": "", "url": "https://example.com/empty"},
                ],
                "answer": "short answer",
            },
        )

    monkeypatch.setattr(search, "_client", _mock_client(handler))
    result = search.search_web("python asyncio", max_results=2)
    assert result["provider"] == "tavily"
    assert len(result["results"]) == 1
    assert result["results"][0]["score"] == 0.9123
    assert result["answer"] == "short answer"
    assert seen["url"] == "https://search.example.com/search"
    assert seen["json"]["api_key"] == "tvly-test"
    assert seen["json"]["query"] == "python asyncio"
    assert seen["json"]["max_results"] == 2


def test_searxng_success_and_language(
    search_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    search_settings.search_provider = "searxng"
    search_settings.search_base_url = "http://searx.local"
    search_settings.search_language = "zh-CN"
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "中文结果",
                        "url": "https://example.org/zh",
                        "content": "摘要",
                    }
                ]
            },
        )

    monkeypatch.setattr(search, "_client", _mock_client(handler))
    result = search.search_web("智能体", max_results=1)
    assert result["provider"] == "searxng"
    assert result["results"][0]["title"] == "中文结果"
    assert seen["params"]["format"] == "json"
    assert seen["params"]["language"] == "zh-CN"


def test_missing_search_configuration(
    search_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    search_settings.search_provider = "tavily"
    search_settings.search_api_key = ""
    with pytest.raises(search.SearchError, match="SEARCH_API_KEY"):
        search.search_web("test")

    search_settings.search_provider = "searxng"
    search_settings.search_base_url = ""
    with pytest.raises(search.SearchError, match="SEARCH_BASE_URL"):
        search.search_web("test")


def test_max_results_is_clamped(
    search_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    search_settings.search_provider = "searxng"
    search_settings.search_base_url = "http://searx.local"
    search_settings.search_max_results = 4
    search_settings.search_max_results_limit = 3
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": f"Result {index}",
                        "url": f"https://example.com/{index}",
                        "content": "snippet",
                    }
                    for index in range(5)
                ]
            },
        )

    monkeypatch.setattr(search, "_client", _mock_client(handler))
    result = search.search_web("test", max_results=99)
    assert seen["params"]["q"] == "test"
    assert len(result["results"]) == 3


def test_tool_web_search_formatting(
    search_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_search(query: str, max_results: int | None = None) -> dict[str, Any]:
        return {
            "provider": "tavily",
            "query": query,
            "answer": "answer text",
            "results": [
                {
                    "title": "Title",
                    "url": "https://example.com/page",
                    "snippet": "Snippet body",
                }
            ],
        }

    monkeypatch.setattr(search, "search_web", fake_search)
    output = tools.tool_web_search("query", 3)
    assert "Web Search (tavily)" in output
    assert "query" in output
    assert "https://example.com/page" in output
    assert "Snippet body" in output
    assert "不可信" in output
