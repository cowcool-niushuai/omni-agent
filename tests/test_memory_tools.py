import uuid

import database
import prompts
import tools


def _find_memory_id(category: str, key: str) -> int | None:
    for memory in database.get_all_memories():
        if memory["category"] == category and memory["key"] == key:
            return int(memory["id"])
    return None


def test_memory_tool_roundtrip_and_prompt_injection() -> None:
    key = f"test_pref_{uuid.uuid4().hex[:8]}"
    try:
        saved = tools.dispatch_tool(
            "memory_save",
            {
                "category": "user_profile",
                "key": key,
                "content": "用户偏好使用本地 BM25 检索中文资料。",
                "importance": 4,
            },
        )
        assert "已保存长期记忆" in saved

        memory_id = _find_memory_id("user_profile", key)
        assert memory_id is not None

        found = tools.dispatch_tool("memory_search", {"query": "BM25 中文检索"})
        assert key in found

        # The same category+key must upsert instead of creating duplicates.
        tools.dispatch_tool(
            "memory_save",
            {
                "category": "user_profile",
                "key": key,
                "content": "用户偏好本地 BM25 检索。",
                "importance": 4,
            },
        )
        matches = [
            memory
            for memory in database.get_all_memories()
            if memory["category"] == "user_profile" and memory["key"] == key
        ]
        assert len(matches) == 1

        prompt = prompts.build_system_prompt("BM25 检索")
        assert key in prompt
    finally:
        memory_id = _find_memory_id("user_profile", key)
        if memory_id is not None:
            database.delete_memory(memory_id)


def test_knowledge_tool_roundtrip() -> None:
    title = f"测试知识 {uuid.uuid4().hex[:8]}"
    try:
        result = tools.dispatch_tool(
            "knowledge_add",
            {
                "title": title,
                "content": "MCP 使用 JSON-RPC 2.0 进行工具调用。",
                "tags": "mcp,test",
            },
        )
        assert "已写入知识库文档" in result

        found = tools.dispatch_tool("rag_search", {"query": "JSON-RPC 工具调用"})
        assert title in found
    finally:
        matches = [
            document
            for document in database.list_knowledge(1000)
            if document["title"] == title
        ]
        for document in matches:
            database.delete_knowledge(int(document["id"]))
