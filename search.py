"""Web search backends for the ``web_search`` tool.

The project deliberately does not bundle a search provider.  Operators choose
Tavily (hosted, easiest) or a self-hosted SearXNG instance (private, no
per-query fee).  Search results contain untrusted titles/snippets and URLs;
callers must treat them as data and fetch pages through ``web_fetch`` before
relying on their contents.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from omni_agent.config import get_settings
from omni_agent.security.ssrf import (
    UnsafeURLError,
    resolve_and_validate,
    validate_url_static,
)

DEFAULT_TAVILY_URL = "https://api.tavily.com"
DEFAULT_SEARXNG_URL = "http://127.0.0.1:8080"
SUPPORTED_PROVIDERS = {"tavily", "searxng"}


class SearchError(RuntimeError):
    """A user-facing search configuration or provider error."""


def normalize_provider(value: str | None) -> str:
    provider = (value or "").strip().lower()
    if provider in {"", "off", "none", "disabled", "false"}:
        return "disabled"
    if provider in {"searx", "searxng"}:
        return "searxng"
    if provider in {"tavily", "tavily-search"}:
        return "tavily"
    return provider


def provider_status() -> dict[str, Any]:
    settings = get_settings()
    provider = normalize_provider(settings.search_provider)
    configured = False
    reason = ""
    if provider == "tavily":
        configured = bool(settings.search_api_key.strip())
        reason = "" if configured else "缺少 SEARCH_API_KEY"
    elif provider == "searxng":
        configured = bool(settings.search_base_url.strip())
        reason = "" if configured else "缺少 SEARCH_BASE_URL"
    elif provider == "disabled":
        reason = "未启用联网搜索"
    else:
        reason = f"不支持的搜索后端: {provider}"
    return {
        "provider": provider,
        "configured": configured and provider in SUPPORTED_PROVIDERS,
        "reason": reason,
        "max_results": settings.search_max_results,
    }


def _endpoint(url: str, *, allow_private: bool) -> str:
    candidate = (url or "").strip().rstrip("/")
    if not candidate:
        raise SearchError("搜索服务地址为空，请配置 SEARCH_BASE_URL")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SearchError("搜索服务地址必须是合法的 http(s) URL")
    if parsed.username or parsed.password:
        raise SearchError("搜索服务地址不能包含用户名或密码")
    try:
        validated = validate_url_static(
            candidate + "/",
            allowed_ports=set(),
            allow_private=allow_private,
        )
        resolve_and_validate(validated, allow_private=allow_private)
    except UnsafeURLError as exc:
        raise SearchError(f"搜索服务地址被安全策略阻止: {exc}") from exc
    return candidate


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        headers={
            "User-Agent": "OmniAgent/0.1 (+web_search)",
            "Accept": "application/json",
        },
    )


def _clean_result(item: dict[str, Any]) -> dict[str, Any] | None:
    url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()
    if not url or not title:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    snippet = str(item.get("content") or item.get("snippet") or "").strip()
    result: dict[str, Any] = {"title": title, "url": url, "snippet": snippet}
    score = item.get("score")
    if isinstance(score, (int, float)):
        result["score"] = round(float(score), 4)
    return result


def _search_tavily(query: str, limit: int, settings: Any) -> dict[str, Any]:
    api_key = settings.search_api_key.strip()
    if not api_key:
        raise SearchError("Tavily 搜索未配置 SEARCH_API_KEY")
    base = settings.search_base_url.strip() or DEFAULT_TAVILY_URL
    endpoint = _endpoint(
        base + "/search", allow_private=settings.search_allow_private_network
    )
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": limit,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }
    try:
        with _client(settings.search_timeout_seconds) as client:
            response = client.post(endpoint, json=payload)
    except httpx.TimeoutException as exc:
        raise SearchError("搜索请求超时，请稍后重试") from exc
    except httpx.HTTPError as exc:
        raise SearchError(f"搜索请求失败: {exc}") from exc
    if response.status_code >= 400:
        raise SearchError(f"Tavily 返回 HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise SearchError("Tavily 返回了无法解析的响应") from exc
    raw_results = data.get("results") if isinstance(data, dict) else None
    results = [
        cleaned
        for cleaned in (_clean_result(item) for item in (raw_results or []) if isinstance(item, dict))
        if cleaned
    ]
    return {
        "provider": "tavily",
        "query": query,
        "results": results[:limit],
        "answer": str(data.get("answer") or "").strip() if isinstance(data, dict) else "",
    }


def _search_searxng(query: str, limit: int, settings: Any) -> dict[str, Any]:
    base = settings.search_base_url.strip()
    if not base:
        raise SearchError("SearXNG 搜索未配置 SEARCH_BASE_URL")
    endpoint = _endpoint(
        base + "/search", allow_private=settings.search_allow_private_network
    )
    params = {
        "q": query,
        "format": "json",
        "language": settings.search_language,
        "safesearch": 1,
    }
    try:
        with _client(settings.search_timeout_seconds) as client:
            response = client.get(endpoint, params=params)
    except httpx.TimeoutException as exc:
        raise SearchError("搜索请求超时，请稍后重试") from exc
    except httpx.HTTPError as exc:
        raise SearchError(f"搜索请求失败: {exc}") from exc
    if response.status_code >= 400:
        raise SearchError(f"SearXNG 返回 HTTP {response.status_code}（请确认已开启 JSON 输出）")
    try:
        data = response.json()
    except ValueError as exc:
        raise SearchError("SearXNG 返回了无法解析的响应，请确认 format=json 已启用") from exc
    raw_results = data.get("results") if isinstance(data, dict) else None
    results = [
        cleaned
        for cleaned in (_clean_result(item) for item in (raw_results or []) if isinstance(item, dict))
        if cleaned
    ]
    return {
        "provider": "searxng",
        "query": query,
        "results": results[:limit],
        "answer": "",
    }


def search_web(query: str, max_results: int | None = None) -> dict[str, Any]:
    """Run a web search and return structured, untrusted results.

    Raises :class:`SearchError` with a user-facing message on configuration or
    provider failures.  The caller is responsible for formatting and auditing.
    """
    settings = get_settings()
    provider = normalize_provider(settings.search_provider)
    text = (query or "").strip()
    if not text:
        raise SearchError("搜索关键词不能为空")
    if len(text) > settings.search_max_query_chars:
        raise SearchError(f"搜索关键词过长（上限 {settings.search_max_query_chars} 字符）")
    if provider == "disabled":
        raise SearchError(
            "联网搜索未启用。请在 .env 配置 SEARCH_PROVIDER=tavily 或 searxng，"
            "并填写对应密钥/地址后重启服务。"
        )
    if provider not in SUPPORTED_PROVIDERS:
        raise SearchError(f"不支持的搜索后端: {provider}")
    limit = max_results if max_results is not None else settings.search_max_results
    limit = max(1, min(int(limit), settings.search_max_results_limit))
    if provider == "tavily":
        return _search_tavily(text, limit, settings)
    return _search_searxng(text, limit, settings)
