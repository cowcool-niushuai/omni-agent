import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import retrieval

DB_PATH = os.path.join(os.path.dirname(__file__), "omni_agent.db")

SCHEDULE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. Sessions table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # 2. Messages table with Thought and Tool call traces
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        thought TEXT,
        tool_calls TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )
    """)
    
    # 3. 3-Layer Memory: Long-term memory & user profile
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL, -- 'user_profile', 'skill_pref', 'fact', 'task_summary'
        key TEXT NOT NULL,
        content TEXT NOT NULL,
        importance REAL DEFAULT 1.0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # 4. RAG Knowledge documents
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS knowledge_docs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        tags TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # 5. Permission / Human-in-the-loop requests
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS permission_requests (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        arguments TEXT NOT NULL,
        risk_level TEXT NOT NULL, -- 'MEDIUM', 'HIGH', 'CRITICAL'
        reason TEXT NOT NULL,
        status TEXT DEFAULT 'PENDING', -- 'PENDING', 'APPROVED', 'REJECTED'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 6. DeerFlow-style Sub-Agent tasks record
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS subagent_tasks (
        id TEXT PRIMARY KEY,
        parent_session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        task TEXT NOT NULL,
        status TEXT DEFAULT 'RUNNING', -- 'RUNNING', 'COMPLETED', 'FAILED'
        result TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    )
    """)

    # 7. Tool execution audit trail (Phase 2)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tool_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        turn_id TEXT,
        tool_call_id TEXT,
        tool_name TEXT NOT NULL,
        arguments TEXT,
        risk_level TEXT NOT NULL,
        decision TEXT NOT NULL, -- 'AUTO', 'APPROVED', 'REJECTED', 'DENIED'
        status TEXT NOT NULL,   -- 'OK', 'ERROR', 'TIMEOUT', 'BLOCKED'
        sandbox TEXT,
        exit_code INTEGER,
        duration_ms INTEGER DEFAULT 0,
        output_chars INTEGER DEFAULT 0,
        output_preview TEXT,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 8. Scheduled / unattended tasks (Phase 3)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        prompt TEXT NOT NULL,
        interval_seconds INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        session_id TEXT NOT NULL,
        last_run_at TEXT,
        next_run_at TEXT NOT NULL,
        last_status TEXT DEFAULT 'NEVER', -- NEVER/RUNNING/OK/ERROR/BLOCKED/INTERRUPTED
        last_result TEXT,
        run_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Indexes for the hot read paths
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_permission_status ON permission_requests(status, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subagents_session ON subagent_tasks(parent_session_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_session ON tool_audit(session_id, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON tool_audit(created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_lookup ON memories(category, key)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC, updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_created ON knowledge_docs(id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_schedules_due ON scheduled_tasks(enabled, next_run_at)")
    
    # Pre-seed some knowledge docs for RAG if empty
    cursor.execute("SELECT count(*) as cnt FROM knowledge_docs")
    if cursor.fetchone()["cnt"] == 0:
        docs = [
            ("Agent架构与ReAct设计规范", 
             "ReAct (Reasoning + Acting) 范式将思考与行动紧密结合。在每次执行外部工具前，Agent 必须在 Thought 中阐述调用原因与参数规划；工具执行完毕获得 Observation 后，模型需评估当前进度，判断是继续调用下一个工具还是生成最终回复。",
             "agent,react,architecture"),
            ("MCP (Model Context Protocol) 核心规范",
             "MCP 是由 Anthropic 提出的标准化智能体扩展协议，采用 JSON-RPC 2.0 规范，提供 tools/list 与 tools/call 等标准方法。通过 MCP，Agent 无需修改核心代码即可无缝挂载外部数据库、云服务与三方接口。",
             "mcp,standard,protocol"),
            ("企业级权限拦截与安全网关",
             "企业生产环境中，禁止智能体无感知直接执行高危写操作或破坏性命令。对于包含 rm、格式化、修改生产数据库等指令，系统通过权限拦截网关暂停流程，通知人工审批 (Human-in-the-loop) ，批准后方可恢复执行。",
             "security,permission,audit"),
            ("三层记忆架构设计指南",
             "现代 Agent 的记忆架构分为三层：1. 短期工作记忆（当前会话 Context Window）；2. 语义与知识记忆（RAG 检索增强）；3. 长期偏好记忆（SQLite 持久化用户偏好与历史关键事实），在每次 Session 初始化时动态注入 Prompt。",
             "memory,profile,rag")
        ]
        for title, content, tags in docs:
            cursor.execute("INSERT INTO knowledge_docs (title, content, tags) VALUES (?, ?, ?)", (title, content, tags))
            
    # Pre-seed some default user memories
    cursor.execute("SELECT count(*) as cnt FROM memories")
    if cursor.fetchone()["cnt"] == 0:
        memories = [
            ("user_profile", "dev_preference", "用户偏好使用 Python 3 进行数据处理，编写代码时要求包含完整的注释和类型注解。"),
            ("user_profile", "security_level", "生产环境敏感度高，对于高危 Shell 命令要求进行人工审批。"),
            ("fact", "workspace_root", "当前专属工作目录位于 ./workspace，所有由 Agent 生成或修改的文件均存放于此。")
        ]
        for cat, k, c in memories:
            cursor.execute("INSERT INTO memories (category, key, content) VALUES (?, ?, ?)", (cat, k, c))
            
    conn.commit()
    conn.close()

# Session operations
def create_session(session_id: str, title: str = "新智能体对话"):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO sessions (id, title) VALUES (?, ?)", (session_id, title))
    conn.commit()
    conn.close()

def ensure_session(session_id: str, title: str = "新智能体对话"):
    """Create the session lazily; safe to call before every turn."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO sessions (id, title) VALUES (?, ?)",
        (session_id, title),
    )
    conn.commit()
    conn.close()

def list_sessions() -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sessions ORDER BY updated_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_session_messages(session_id: str) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_message(session_id: str, role: str, content: str, thought: Optional[str] = None, tool_calls: Optional[Any] = None):
    conn = get_db()
    cursor = conn.cursor()
    tc_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
    cursor.execute(
        "INSERT INTO messages (session_id, role, content, thought, tool_calls) VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, thought, tc_json)
    )
    cursor.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()

# Memory operations
def get_all_memories(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    if limit is None:
        cursor.execute("SELECT * FROM memories ORDER BY importance DESC, updated_at DESC")
    else:
        cursor.execute(
            "SELECT * FROM memories ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_memory(memory_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def save_memory(
    category: str,
    key: str,
    content: str,
    importance: float = 1.0,
) -> Dict[str, Any]:
    """Upsert a memory by ``(category, key)`` and return the stored row."""
    category = category.strip()[:40] or "fact"
    key = key.strip()
    content = content.strip()
    if not key or not content:
        raise ValueError("记忆的 key 和 content 不能为空")
    try:
        importance_value = max(0.0, min(float(importance), 5.0))
    except (TypeError, ValueError):
        importance_value = 1.0

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM memories WHERE category = ? AND key = ? ORDER BY id ASC LIMIT 1",
        (category, key),
    )
    row = cursor.fetchone()
    if row:
        memory_id = int(row["id"])
        cursor.execute(
            """
            UPDATE memories
            SET content = ?, importance = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (content, importance_value, memory_id),
        )
        # Older versions inserted duplicates for the same key; collapse them
        # now that memory_save is an explicit upsert.
        cursor.execute(
            "DELETE FROM memories WHERE category = ? AND key = ? AND id <> ?",
            (category, key, memory_id),
        )
    else:
        cursor.execute(
            """
            INSERT INTO memories (category, key, content, importance, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (category, key, content, importance_value),
        )
        memory_id = int(cursor.lastrowid)
    conn.commit()
    cursor.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
    stored = cursor.fetchone()
    conn.close()
    return dict(stored)


def update_memory(
    memory_id: int,
    *,
    category: Optional[str] = None,
    key: Optional[str] = None,
    content: Optional[str] = None,
    importance: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    current = get_memory(memory_id)
    if not current:
        return None
    category = (
        current["category"]
        if category is None
        else (category.strip()[:40] or "fact")
    )
    key = current["key"] if key is None else key.strip()
    content = current["content"] if content is None else content.strip()
    if not key or not content:
        raise ValueError("记忆的 key 和 content 不能为空")
    if importance is None:
        importance_value = float(current["importance"] or 1.0)
    else:
        importance_value = max(0.0, min(float(importance), 5.0))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE memories
        SET category = ?, key = ?, content = ?, importance = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (category, key, content, importance_value, memory_id),
    )
    conn.commit()
    cursor.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def delete_memory(memory_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def search_memories(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Return query-relevant memories, falling back to importance order."""
    safe_limit = max(1, int(limit))
    if not query.strip():
        return get_all_memories(safe_limit)
    return retrieval.rank_memories(query, get_all_memories(), safe_limit)


def relevant_memories(
    query: str,
    *,
    limit: int = 6,
    always_include: int = 3,
) -> List[Dict[str, Any]]:
    return retrieval.relevant_memories(
        query,
        get_all_memories(),
        limit=limit,
        always_include=always_include,
    )


# Knowledge / RAG
def list_knowledge(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM knowledge_docs ORDER BY id DESC LIMIT ? OFFSET ?",
        (max(1, int(limit)), max(0, int(offset))),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_knowledge(doc_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM knowledge_docs WHERE id = ?", (doc_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def add_knowledge(title: str, content: str, tags: str = "") -> Dict[str, Any]:
    title = title.strip()
    content = content.strip()
    if not title or not content:
        raise ValueError("知识文档的 title 和 content 不能为空")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO knowledge_docs (title, content, tags) VALUES (?, ?, ?)",
        (title, content, tags.strip()),
    )
    doc_id = int(cursor.lastrowid)
    conn.commit()
    cursor.execute("SELECT * FROM knowledge_docs WHERE id = ?", (doc_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row)


def update_knowledge(
    doc_id: int,
    *,
    title: Optional[str] = None,
    content: Optional[str] = None,
    tags: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_knowledge(doc_id)
    if not current:
        return None
    title = current["title"] if title is None else title.strip()
    content = current["content"] if content is None else content.strip()
    tags = (current.get("tags") or "") if tags is None else tags.strip()
    if not title or not content:
        raise ValueError("知识文档的 title 和 content 不能为空")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE knowledge_docs SET title = ?, content = ?, tags = ? WHERE id = ?",
        (title, content, tags, doc_id),
    )
    conn.commit()
    cursor.execute("SELECT * FROM knowledge_docs WHERE id = ?", (doc_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def delete_knowledge(doc_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM knowledge_docs WHERE id = ?", (doc_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def search_knowledge(query: str, limit: int = 3) -> List[Dict[str, Any]]:
    """Search knowledge with BM25 + Chinese bigrams (no embeddings needed)."""
    safe_limit = max(1, int(limit))
    if not query.strip():
        return list_knowledge(safe_limit)
    return retrieval.rank_knowledge(query, list_knowledge(1000), safe_limit)

# Scheduled task operations (Phase 3)
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_schedule_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(SCHEDULE_TIME_FORMAT)


def _parse_schedule_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:19], SCHEDULE_TIME_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _schedule_row(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    item["interval_seconds"] = int(item.get("interval_seconds") or 60)
    item["run_count"] = int(item.get("run_count") or 0)
    return item


def _next_schedule_time(interval_seconds: int, base: Optional[datetime] = None) -> str:
    interval = max(1, int(interval_seconds))
    return _format_schedule_time((base or _utc_now()) + timedelta(seconds=interval))


def create_scheduled_task(
    name: str,
    prompt: str,
    interval_seconds: int,
    enabled: bool = True,
) -> Dict[str, Any]:
    name = name.strip()
    prompt = prompt.strip()
    if not name or not prompt:
        raise ValueError("定时任务的名称和提示词不能为空")
    interval = max(1, int(interval_seconds))
    task_id = "sch_" + uuid.uuid4().hex[:10]
    session_id = "sched_" + task_id
    next_run_at = _next_schedule_time(interval)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO scheduled_tasks
            (id, name, prompt, interval_seconds, enabled, session_id, next_run_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, name, prompt, interval, 1 if enabled else 0, session_id, next_run_at),
    )
    conn.commit()
    conn.close()
    created = get_scheduled_task(task_id)
    if not created:
        raise RuntimeError("定时任务创建失败")
    return created


def list_scheduled_tasks() -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM scheduled_tasks
        ORDER BY enabled DESC, next_run_at ASC, created_at DESC
        """
    )
    rows = cursor.fetchall()
    conn.close()
    return [_schedule_row(row) for row in rows]


def get_scheduled_task(task_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    conn.close()
    return _schedule_row(row) if row else None


def update_scheduled_task(
    task_id: str,
    *,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    interval_seconds: Optional[int] = None,
    enabled: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    current = get_scheduled_task(task_id)
    if not current:
        return None

    fields: list[str] = []
    values: list[Any] = []
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("定时任务的名称不能为空")
        fields.append("name = ?")
        values.append(clean_name)
    if prompt is not None:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("定时任务的提示词不能为空")
        fields.append("prompt = ?")
        values.append(clean_prompt)

    now = _utc_now()
    interval = int(current["interval_seconds"])
    next_run_assigned = False
    if interval_seconds is not None:
        interval = max(1, int(interval_seconds))
        fields.append("interval_seconds = ?")
        values.append(interval)
        fields.append("next_run_at = ?")
        values.append(_next_schedule_time(interval, now))
        next_run_assigned = True

    if enabled is not None:
        fields.append("enabled = ?")
        values.append(1 if enabled else 0)
        next_run = _parse_schedule_time(current.get("next_run_at"))
        if enabled and not next_run_assigned and (next_run is None or next_run <= now):
            fields.append("next_run_at = ?")
            values.append(_next_schedule_time(interval, now))

    if not fields:
        return current

    fields.append("updated_at = ?")
    values.append(_format_schedule_time(now))
    values.append(task_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ?",
        tuple(values),
    )
    conn.commit()
    conn.close()
    return get_scheduled_task(task_id)


def delete_scheduled_task(task_id: str) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def due_scheduled_tasks(
    limit: int = 5,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    stamp = _format_schedule_time(now or _utc_now())
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM scheduled_tasks
        WHERE enabled = 1 AND next_run_at <= ?
        ORDER BY next_run_at ASC
        LIMIT ?
        """,
        (stamp, max(1, int(limit))),
    )
    rows = cursor.fetchall()
    conn.close()
    return [_schedule_row(row) for row in rows]


def mark_scheduled_task_running(task_id: str) -> Optional[Dict[str, Any]]:
    now = _format_schedule_time(_utc_now())
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE scheduled_tasks
        SET last_status = 'RUNNING', last_run_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (now, now, task_id),
    )
    conn.commit()
    conn.close()
    return get_scheduled_task(task_id)


def finish_scheduled_task_run(
    task_id: str,
    status: str,
    result: str,
    *,
    next_run_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_scheduled_task(task_id)
    if not current:
        return None
    now = _utc_now()
    next_run = next_run_at or _next_schedule_time(
        int(current["interval_seconds"]), now
    )
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE scheduled_tasks
        SET last_status = ?, last_result = ?, next_run_at = ?,
            run_count = run_count + 1, updated_at = ?
        WHERE id = ?
        """,
        (status, result, next_run, _format_schedule_time(now), task_id),
    )
    conn.commit()
    conn.close()
    return get_scheduled_task(task_id)


def recover_scheduled_tasks() -> int:
    """Mark runs interrupted by a process restart instead of leaving RUNNING."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE scheduled_tasks
        SET last_status = 'INTERRUPTED', updated_at = CURRENT_TIMESTAMP
        WHERE last_status = 'RUNNING'
        """
    )
    count = int(cursor.rowcount or 0)
    conn.commit()
    conn.close()
    return count


# SubAgent operations
def record_subagent_task(task_id: str, parent_session_id: str, role: str, task: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO subagent_tasks (id, parent_session_id, role, task, status)
    VALUES (?, ?, ?, ?, 'RUNNING')
    """, (task_id, parent_session_id, role, task))
    conn.commit()
    conn.close()

def update_subagent_task(task_id: str, status: str, result: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE subagent_tasks 
    SET status = ?, result = ?, completed_at = CURRENT_TIMESTAMP
    WHERE id = ?
    """, (status, result, task_id))
    conn.commit()
    conn.close()

def list_subagents(session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    if session_id:
        cursor.execute("SELECT * FROM subagent_tasks WHERE parent_session_id = ? ORDER BY created_at DESC", (session_id,))
    else:
        cursor.execute("SELECT * FROM subagent_tasks ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
