"""Output truncation helpers shared by tools and the runtime."""

from __future__ import annotations

DEFAULT_MAX_OUTPUT_CHARS = 12_000


def truncate_text(
    text: str,
    max_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    *,
    marker: str = "\n... [输出已截断]",
) -> str:
    """Return ``text`` capped at ``max_chars`` with an explicit marker."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(marker))
    return text[:keep] + marker
