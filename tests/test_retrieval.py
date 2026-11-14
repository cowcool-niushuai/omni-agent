from retrieval import (
    rank_knowledge,
    rank_memories,
    relevant_memories,
    tokenize,
)


def test_tokenize_supports_chinese_bigrams_and_latin_words() -> None:
    tokens = tokenize("Python 知识库检索")
    assert "python" in tokens
    assert "知识" in tokens
    assert "识库" in tokens
    assert "库检" in tokens
    assert "检索" in tokens


def test_rank_knowledge_prefers_title_and_tags() -> None:
    documents = [
        {
            "id": 1,
            "title": "MCP 核心规范",
            "tags": "mcp,protocol",
            "content": "JSON-RPC 工具协议。",
        },
        {
            "id": 2,
            "title": "记忆架构",
            "tags": "memory",
            "content": "短期、语义与长期记忆。",
        },
        {
            "id": 3,
            "title": "权限网关",
            "tags": "security",
            "content": "人工审批高风险操作。",
        },
    ]
    ranked = rank_knowledge("MCP 协议", documents, limit=2)
    assert ranked
    assert ranked[0]["id"] == 1


def test_rank_memories_matches_content_and_boosts_key() -> None:
    memories = [
        {
            "id": 1,
            "category": "user_profile",
            "key": "preferred_language",
            "content": "用户偏好 Python。",
            "importance": 3,
        },
        {
            "id": 2,
            "category": "fact",
            "key": "workspace",
            "content": "工作区在 ./workspace。",
            "importance": 1,
        },
        {
            "id": 3,
            "category": "fact",
            "key": "database",
            "content": "默认使用 SQLite 持久化。",
            "importance": 1,
        },
    ]
    ranked = rank_memories("Python 偏好", memories, limit=1)
    assert ranked and ranked[0]["id"] == 1


def test_relevant_memories_always_includes_important_profile() -> None:
    memories = [
        {
            "id": 1,
            "category": "user_profile",
            "key": "security",
            "content": "高危命令必须审批。",
            "importance": 5,
            "updated_at": "2026-01-02",
        },
        {
            "id": 2,
            "category": "fact",
            "key": "mcp",
            "content": "MCP 使用 JSON-RPC。",
            "importance": 1,
            "updated_at": "2026-01-01",
        },
        {
            "id": 3,
            "category": "fact",
            "key": "css",
            "content": "界面使用 Tailwind。",
            "importance": 1,
            "updated_at": "2026-01-01",
        },
    ]
    selected = relevant_memories(
        "MCP 协议",
        memories,
        limit=2,
        always_include=1,
    )
    assert [item["id"] for item in selected] == [1, 2]
