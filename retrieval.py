"""Local, dependency-free retrieval for memories and knowledge documents.

The agent runs fully offline by default, so retrieval must not depend on an
embedding service.  This module combines a small BM25 index with a
dependency-free hashed embedding and a CJK-aware tokenizer:

* Latin words/numbers are lower-cased and split on non-word characters.
* CJK text is indexed as unigrams and bigrams, which works surprisingly well
  for Chinese without a third-party segmenter.
* Exact phrase matches receive a small bonus.

It is intentionally pure: callers pass plain dictionaries and a text extractor,
which keeps the ranking logic easy to test and reuse.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any, Callable, Sequence

from omni_agent.config import get_settings

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Tokenize mixed Chinese/English text without external dependencies."""
    if not text:
        return []
    lowered = text.lower()
    tokens: list[str] = _WORD.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
            continue
        # Unigrams make short queries such as "库" match; bigrams capture
        # words such as "知识" and "检索" much more precisely.
        tokens.extend(run)
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def embed_text(text: str, dim: int = 256) -> list[float]:
    """Dependency-free hashed embedding.

    A signed feature-hash over CJK/Latin tokens gives a stable dense vector
    without an embedding API.  It is not a semantic model, but it adds fuzzy
    overlap and is a useful second signal beside BM25.
    """
    dim = max(32, int(dim))
    vector = [0.0] * dim
    for token in tokenize(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big")
        sign = 1.0 if raw & 1 else -1.0
        vector[raw % dim] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=False))


def _rank(
    query: str,
    documents: Sequence[dict[str, Any]],
    text_of: Callable[[dict[str, Any]], str],
    limit: int,
    *,
    score_of: Callable[[dict[str, Any]], float] | None = None,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[dict[str, Any]]:
    """Rank ``documents`` with BM25.  Ties keep the caller's order."""
    query_terms = list(dict.fromkeys(tokenize(query)))
    if not query_terms or not documents or limit <= 0:
        return []

    tokenized = [tokenize(text_of(document)) for document in documents]
    lengths = [len(tokens) for tokens in tokenized]
    average_length = sum(lengths) / len(lengths) if lengths else 1.0
    average_length = average_length or 1.0

    document_frequency: Counter[str] = Counter()
    for tokens in tokenized:
        present = set(tokens)
        for term in query_terms:
            if term in present:
                document_frequency[term] += 1

    total = len(documents)
    idf = {
        term: math.log(1 + (total - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
        for term in query_terms
    }

    settings = get_settings()
    hybrid_weight = max(0.0, min(float(settings.retrieval_hybrid_weight), 1.0))
    dim = int(settings.retrieval_embedding_dim)
    query_vector = embed_text(query, dim)
    query_phrase = query.strip().lower()
    raw_scores: list[float] = []
    texts: list[str] = []
    for index, document in enumerate(documents):
        tokens = tokenized[index]
        text = text_of(document)
        texts.append(text)
        if not tokens:
            raw_scores.append(0.0)
            continue
        frequencies = Counter(tokens)
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            denominator = frequency + k1 * (
                1 - b + b * (lengths[index] / average_length)
            )
            score += idf[term] * frequency * (k1 + 1) / denominator
        raw_scores.append(score)

    max_bm25 = max(raw_scores) if raw_scores else 0.0
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for index, document in enumerate(documents):
        bm25 = raw_scores[index] / max_bm25 if max_bm25 > 0 else 0.0
        cosine = max(0.0, _cosine(query_vector, embed_text(texts[index], dim)))
        # Dense overlap may find a match even when no exact token does, but a
        # weak cosine is mostly noise, so require a clear signal.
        if bm25 <= 0 and cosine < 0.35:
            continue
        score = (1.0 - hybrid_weight) * bm25 + hybrid_weight * cosine
        if len(query_phrase) >= 2 and query_phrase in texts[index].lower():
            score += 0.08
        if score_of is not None:
            score *= score_of(document)
        scored.append((score, index, document))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [document for _, _, document in scored[:limit]]


def rank_texts(
    query: str,
    documents: Sequence[dict[str, Any]],
    text_of: Callable[[dict[str, Any]], str],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank arbitrary records by BM25 using a caller-provided text field."""
    return _rank(query, documents, text_of, limit)


def rank_memories(
    query: str,
    memories: Sequence[dict[str, Any]],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank long-term memories, boosting keys and importance slightly."""

    def text_of(memory: dict[str, Any]) -> str:
        key = str(memory.get("key") or "")
        category = str(memory.get("category") or "")
        content = str(memory.get("content") or "")
        # Repeating the key makes it behave like a title; importance nudges
        # otherwise-equal memories towards the top.
        return f"{key} {key} {category} {content}"

    def score_of(memory: dict[str, Any]) -> float:
        try:
            importance = float(memory.get("importance") or 1.0)
        except (TypeError, ValueError):
            importance = 1.0
        return 0.85 + max(0.0, min(importance, 5.0)) * 0.06

    return _rank(query, memories, text_of, limit, score_of=score_of)


def rank_knowledge(
    query: str,
    documents: Sequence[dict[str, Any]],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank knowledge documents, boosting title and tags over body text."""

    def text_of(document: dict[str, Any]) -> str:
        title = str(document.get("title") or "")
        tags = str(document.get("tags") or "")
        content = str(document.get("content") or "")
        return f"{title} {title} {tags} {tags} {content}"

    return _rank(query, documents, text_of, limit)


def relevant_memories(
    query: str,
    memories: Sequence[dict[str, Any]],
    *,
    limit: int = 6,
    always_include: int = 3,
) -> list[dict[str, Any]]:
    """Combine query-relevant memories with the most important profile facts.

    A system prompt should always carry a few stable user preferences, even
    when the current question is unrelated; the rest are retrieved by topic.
    """
    if not memories:
        return []
    ordered = sorted(
        memories,
        key=lambda item: (
            -float(item.get("importance") or 0),
            str(item.get("updated_at") or ""),
        ),
    )
    selected: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for memory in ordered[: max(0, always_include)]:
        selected.append(memory)
        seen.add(memory.get("id"))
    for memory in rank_memories(query, memories, limit):
        if memory.get("id") in seen:
            continue
        selected.append(memory)
        seen.add(memory.get("id"))
        if len(selected) >= limit:
            break
    return selected[:limit]
