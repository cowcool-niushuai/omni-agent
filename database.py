import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import retrieval
from omni_agent.tenancy import (
    DEFAULT_PROJECT_ID,
    DEFAULT_TEAM_ID,
    DEFAULT_USER_ID,
    current_project_id,
    current_user_id,
    project_workspace,
)

DB_PATH = os.environ.get("OMNI_DB_PATH") or os.path.join(
    os.path.dirname(__file__), "omni_agent.db"
)

SCHEDULE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PBKDF2_ITERATIONS = 260_000
PASSWORD_PREFIX = "pbkdf2_sha256"


def _utc_stamp(days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        SCHEDULE_TIME_FORMAT
    )

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _ensure_column(
    cursor: sqlite3.Cursor, table: str, column: str, definition: str
) -> None:
    columns = {
        str(row["name"]) for row in cursor.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _project_scope(project_id: Optional[str] = None) -> str:
    return project_id or current_project_id()


def _user_scope(user_id: Optional[str] = None) -> str:
    return user_id or current_user_id()


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

    # 9. Team edition: users, teams, memberships and projects (Phase 4 / M2)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        email TEXT,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'member', -- admin/member/viewer
        status TEXT NOT NULL DEFAULT 'active', -- active/disabled
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_login_at TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS teams (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        owner_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS team_members (
        team_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'member', -- owner/admin/member/viewer
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (team_id, user_id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_by TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        csrf_token TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        expires_at TEXT NOT NULL,
        last_seen_at TEXT,
        user_agent TEXT,
        ip TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS project_settings (
        project_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (project_id, key)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS api_tokens (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        token_prefix TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        scopes TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_used_at TEXT,
        expires_at TEXT,
        revoked_at TEXT
    )
    """)

    # Tenant columns are added with ALTER TABLE for existing databases.
    for table, column, definition in (
        ("sessions", "owner_id", "TEXT"),
        ("sessions", "project_id", "TEXT"),
        ("memories", "project_id", "TEXT"),
        ("knowledge_docs", "project_id", "TEXT"),
        ("permission_requests", "project_id", "TEXT"),
        ("subagent_tasks", "project_id", "TEXT"),
        ("tool_audit", "project_id", "TEXT"),
        ("tool_audit", "error_code", "TEXT"),
        ("tool_audit", "user_id", "TEXT"),
        ("scheduled_tasks", "project_id", "TEXT"),
        ("scheduled_tasks", "owner_id", "TEXT"),
    ):
        _ensure_column(cursor, table, column, definition)

    # Indexes for the hot read paths
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_permission_status ON permission_requests(status, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subagents_session ON subagent_tasks(parent_session_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_session ON tool_audit(session_id, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON tool_audit(created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_lookup ON memories(project_id, category, key)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(project_id, importance DESC, updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_created ON knowledge_docs(project_id, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_schedules_due ON scheduled_tasks(enabled, next_run_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(project_id, updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_project ON tool_audit(project_id, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id, team_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_team ON projects(team_id, status, updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id, expires_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_prefix ON api_tokens(token_prefix)")

    _ensure_default_tenant(cursor)
    
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
            cursor.execute(
                "INSERT INTO knowledge_docs (title, content, tags, project_id) VALUES (?, ?, ?, ?)",
                (title, content, tags, DEFAULT_PROJECT_ID),
            )
            
    # Pre-seed some default user memories
    cursor.execute("SELECT count(*) as cnt FROM memories")
    if cursor.fetchone()["cnt"] == 0:
        memories = [
            ("user_profile", "dev_preference", "用户偏好使用 Python 3 进行数据处理，编写代码时要求包含完整的注释和类型注解。"),
            ("user_profile", "security_level", "生产环境敏感度高，对于高危 Shell 命令要求进行人工审批。"),
            ("fact", "workspace_root", "当前专属工作目录位于 ./workspace，所有由 Agent 生成或修改的文件均存放于此。")
        ]
        for cat, k, c in memories:
            cursor.execute(
                "INSERT INTO memories (category, key, content, project_id) VALUES (?, ?, ?, ?)",
                (cat, k, c, DEFAULT_PROJECT_ID),
            )
            
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Team edition: password, users, teams, projects and tokens
# ---------------------------------------------------------------------------
def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
    )
    encoded = base64.b64encode(digest).decode("ascii")
    return f"{PASSWORD_PREFIX}${iterations}${salt}${encoded}"


def verify_password(password: str, stored: str) -> bool:
    try:
        prefix, iterations, salt, encoded = stored.split("$", 3)
        if prefix != PASSWORD_PREFIX:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), int(iterations)
        )
        return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), encoded)
    except (ValueError, TypeError):
        return False


def _public_user(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item.pop("password_hash", None)
    return item


def _ensure_default_tenant(cursor: sqlite3.Cursor) -> None:
    """Create the legacy default tenant and backfill existing rows."""
    from omni_agent.config import get_settings

    settings = get_settings()
    now = _utc_stamp()
    cursor.execute(
        "INSERT OR IGNORE INTO teams (id, name, description, owner_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            DEFAULT_TEAM_ID,
            "默认团队",
            "由单用户版本自动迁移生成的默认团队。",
            DEFAULT_USER_ID,
            now,
            now,
        ),
    )
    cursor.execute(
        "INSERT OR IGNORE INTO projects "
        "(id, team_id, name, description, status, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
        (
            DEFAULT_PROJECT_ID,
            DEFAULT_TEAM_ID,
            "默认项目",
            "兼容旧数据的默认项目，工作区位于 workspace/。",
            DEFAULT_USER_ID,
            now,
            now,
        ),
    )
    cursor.execute("SELECT COUNT(*) AS count FROM users")
    if int(cursor.fetchone()["count"]) == 0:
        password = str(getattr(settings, "admin_password", "") or "").strip()
        if not password:
            password = secrets.token_urlsafe(14)
            if str(getattr(settings, "environment", "")) != "test":
                password_path = Path(settings.data_dir) / ".initial_admin_password"
                try:
                    password_path.parent.mkdir(parents=True, exist_ok=True)
                    password_path.write_text(password + "\n", encoding="utf-8")
                    os.chmod(password_path, 0o600)
                except OSError:
                    pass
        cursor.execute(
            "INSERT INTO users "
            "(id, username, display_name, email, password_hash, role, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'admin', 'active', ?, ?)",
            (
                DEFAULT_USER_ID,
                str(getattr(settings, "admin_username", "admin") or "admin"),
                "系统管理员",
                str(getattr(settings, "admin_email", "") or ""),
                hash_password(password),
                now,
                now,
            ),
        )
    cursor.execute(
        "INSERT OR IGNORE INTO team_members (team_id, user_id, role, created_at) "
        "VALUES (?, ?, 'owner', ?)",
        (DEFAULT_TEAM_ID, DEFAULT_USER_ID, now),
    )

    # Backfill tenant columns exactly once.  Historical rows belong to the
    # default project/user so upgrading never hides existing conversations.
    for table in (
        "sessions",
        "memories",
        "knowledge_docs",
        "permission_requests",
        "subagent_tasks",
        "tool_audit",
        "scheduled_tasks",
    ):
        cursor.execute(
            f"UPDATE {table} SET project_id = ? WHERE project_id IS NULL OR project_id = ''",
            (DEFAULT_PROJECT_ID,),
        )
    cursor.execute(
        "UPDATE sessions SET owner_id = ? WHERE owner_id IS NULL OR owner_id = ''",
        (DEFAULT_USER_ID,),
    )
    cursor.execute(
        "UPDATE tool_audit SET user_id = ? WHERE user_id IS NULL OR user_id = ''",
        (DEFAULT_USER_ID,),
    )
    cursor.execute(
        "UPDATE scheduled_tasks SET owner_id = ? WHERE owner_id IS NULL OR owner_id = ''",
        (DEFAULT_USER_ID,),
    )
    project_workspace(DEFAULT_PROJECT_ID)


def create_user(
    username: str,
    password: str,
    *,
    display_name: str = "",
    email: str = "",
    role: str = "member",
    status: str = "active",
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,32}", username):
        raise ValueError("用户名需为 3-32 位字母、数字、点、下划线或短横线")
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    if role not in {"admin", "member", "viewer"}:
        raise ValueError("无效的用户角色")
    if status not in {"active", "disabled"}:
        raise ValueError("无效的用户状态")
    uid = user_id or ("usr_" + uuid.uuid4().hex[:10])
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users "
            "(id, username, display_name, email, password_hash, role, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uid,
                username,
                display_name.strip() or username,
                email.strip(),
                hash_password(password),
                role,
                status,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("用户名已存在") from exc
    finally:
        conn.close()
    user = get_user(uid)
    if not user:
        raise RuntimeError("用户创建失败")
    return user


def _user_row(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def get_user(user_id: str, *, include_password: bool = False) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _user_row(row) if include_password else _public_user(row)


def get_user_by_username(
    username: str, *, include_password: bool = False
) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _user_row(row) if include_password else _public_user(row)


def list_users(query: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        if query and query.strip():
            pattern = f"%{query.strip()}%"
            rows = conn.execute(
                "SELECT * FROM users WHERE username LIKE ? OR display_name LIKE ? "
                "OR email LIKE ? ORDER BY created_at ASC",
                (pattern, pattern, pattern),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    finally:
        conn.close()
    return [_public_user(row) for row in rows]


def update_user(
    user_id: str,
    *,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    password: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_user(user_id)
    if not current:
        return None
    fields: list[str] = []
    values: list[Any] = []
    if display_name is not None:
        name = display_name.strip()
        if not name:
            raise ValueError("显示名称不能为空")
        fields.append("display_name = ?")
        values.append(name[:80])
    if email is not None:
        fields.append("email = ?")
        values.append(email.strip()[:200])
    if role is not None:
        if role not in {"admin", "member", "viewer"}:
            raise ValueError("无效的用户角色")
        fields.append("role = ?")
        values.append(role)
    if status is not None:
        if status not in {"active", "disabled"}:
            raise ValueError("无效的用户状态")
        fields.append("status = ?")
        values.append(status)
    if password is not None:
        if len(password) < 8:
            raise ValueError("密码至少需要 8 个字符")
        fields.append("password_hash = ?")
        values.append(hash_password(password))
    if not fields:
        return current
    fields.append("updated_at = ?")
    values.append(_utc_stamp())
    values.append(user_id)
    conn = get_db()
    try:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()
    return get_user(user_id)


def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    user = get_user_by_username(username, include_password=True)
    if not user or user.get("status") != "active":
        return None
    if not verify_password(password, str(user.get("password_hash") or "")):
        return None
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (_utc_stamp(), user["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return get_user(str(user["id"]))


def count_active_admins() -> int:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM users WHERE role = 'admin' AND status = 'active'"
        ).fetchone()
    finally:
        conn.close()
    return int(row["count"]) if row else 0


# Teams ---------------------------------------------------------------------
def create_team(
    name: str,
    owner_id: str,
    *,
    description: str = "",
    team_id: Optional[str] = None,
) -> Dict[str, Any]:
    name = name.strip()
    if not name:
        raise ValueError("团队名称不能为空")
    if len(name) > 100:
        raise ValueError("团队名称过长（上限 100 字符）")
    tid = team_id or ("team_" + uuid.uuid4().hex[:10])
    now = _utc_stamp()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO teams (id, name, description, owner_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tid, name, description.strip()[:500], owner_id, now, now),
        )
        conn.execute(
            "INSERT INTO team_members (team_id, user_id, role, created_at) "
            "VALUES (?, ?, 'owner', ?)",
            (tid, owner_id, now),
        )
        conn.commit()
    finally:
        conn.close()
    team = get_team(tid)
    if not team:
        raise RuntimeError("团队创建失败")
    return team


def get_team(team_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_teams_for_user(user_id: str, *, include_all: bool = False) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        if include_all:
            rows = conn.execute(
                "SELECT t.*, (SELECT COUNT(*) FROM team_members m WHERE m.team_id = t.id) "
                "AS member_count, (SELECT COUNT(*) FROM projects p WHERE p.team_id = t.id "
                "AND p.status = 'active') AS project_count "
                "FROM teams t WHERE t.status = 'active' ORDER BY t.created_at ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t.*, m.role AS my_role, "
                "(SELECT COUNT(*) FROM team_members x WHERE x.team_id = t.id) AS member_count, "
                "(SELECT COUNT(*) FROM projects p WHERE p.team_id = t.id AND p.status = 'active') "
                "AS project_count FROM teams t JOIN team_members m ON m.team_id = t.id "
                "WHERE m.user_id = ? AND t.status = 'active' ORDER BY t.created_at ASC",
                (user_id,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_team_member(team_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM team_members WHERE team_id = ? AND user_id = ?",
            (team_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_team_members(team_id: str) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT m.team_id, m.user_id, m.role, m.created_at, u.username, "
            "u.display_name, u.email, u.status AS user_status "
            "FROM team_members m JOIN users u ON u.id = m.user_id "
            "WHERE m.team_id = ? ORDER BY CASE m.role "
            "WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'member' THEN 2 ELSE 3 END, "
            "u.created_at ASC",
            (team_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def add_team_member(team_id: str, user_id: str, role: str = "member") -> Dict[str, Any]:
    if role not in {"owner", "admin", "member", "viewer"}:
        raise ValueError("无效的团队成员角色")
    if not get_user(user_id):
        raise ValueError("用户不存在")
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO team_members (team_id, user_id, role) VALUES (?, ?, ?) "
            "ON CONFLICT(team_id, user_id) DO UPDATE SET role = excluded.role",
            (team_id, user_id, role),
        )
        conn.commit()
    finally:
        conn.close()
    member = get_team_member(team_id, user_id)
    if not member:
        raise RuntimeError("成员添加失败")
    return member


def update_team_member(team_id: str, user_id: str, role: str) -> Optional[Dict[str, Any]]:
    if role not in {"owner", "admin", "member", "viewer"}:
        raise ValueError("无效的团队成员角色")
    conn = get_db()
    try:
        conn.execute(
            "UPDATE team_members SET role = ? WHERE team_id = ? AND user_id = ?",
            (role, team_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_team_member(team_id, user_id)


def remove_team_member(team_id: str, user_id: str) -> bool:
    conn = get_db()
    try:
        cursor = conn.execute(
            "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
            (team_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def update_team(
    team_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_team(team_id)
    if not current:
        return None
    fields: list[str] = []
    values: list[Any] = []
    if name is not None:
        clean = name.strip()
        if not clean:
            raise ValueError("团队名称不能为空")
        fields.append("name = ?")
        values.append(clean[:100])
    if description is not None:
        fields.append("description = ?")
        values.append(description.strip()[:500])
    if status is not None:
        if status not in {"active", "archived"}:
            raise ValueError("无效的团队状态")
        fields.append("status = ?")
        values.append(status)
    if not fields:
        return current
    fields.append("updated_at = ?")
    values.append(_utc_stamp())
    values.append(team_id)
    conn = get_db()
    try:
        conn.execute(f"UPDATE teams SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()
    return get_team(team_id)


# Projects ------------------------------------------------------------------
def create_project(
    team_id: str,
    name: str,
    created_by: str,
    *,
    description: str = "",
) -> Dict[str, Any]:
    name = name.strip()
    if not name:
        raise ValueError("项目名称不能为空")
    if len(name) > 100:
        raise ValueError("项目名称过长（上限 100 字符）")
    if not get_team(team_id):
        raise ValueError("团队不存在")
    pid = "prj_" + uuid.uuid4().hex[:10]
    now = _utc_stamp()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO projects "
            "(id, team_id, name, description, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
            (pid, team_id, name, description.strip()[:500], created_by, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    project_workspace(pid)
    project = get_project(pid)
    if not project:
        raise RuntimeError("项目创建失败")
    return project


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT p.*, t.name AS team_name FROM projects p "
            "JOIN teams t ON t.id = p.team_id WHERE p.id = ?",
            (project_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_projects_for_user(
    user_id: str,
    *,
    include_all: bool = False,
    include_archived: bool = False,
) -> List[Dict[str, Any]]:
    status_clause = "" if include_archived else " AND p.status = 'active'"
    conn = get_db()
    try:
        if include_all:
            rows = conn.execute(
                "SELECT p.*, t.name AS team_name, "
                "(SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id) AS session_count "
                f"FROM projects p JOIN teams t ON t.id = p.team_id WHERE 1=1 {status_clause} "
                "ORDER BY p.updated_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT p.*, t.name AS team_name, m.role AS my_team_role, "
                "(SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id) AS session_count "
                "FROM projects p JOIN teams t ON t.id = p.team_id "
                "JOIN team_members m ON m.team_id = p.team_id "
                f"WHERE m.user_id = ? {status_clause} ORDER BY p.updated_at DESC",
                (user_id,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def update_project(
    project_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_project(project_id)
    if not current:
        return None
    fields: list[str] = []
    values: list[Any] = []
    if name is not None:
        clean = name.strip()
        if not clean:
            raise ValueError("项目名称不能为空")
        fields.append("name = ?")
        values.append(clean[:100])
    if description is not None:
        fields.append("description = ?")
        values.append(description.strip()[:500])
    if status is not None:
        if status not in {"active", "archived"}:
            raise ValueError("无效的项目状态")
        fields.append("status = ?")
        values.append(status)
    if not fields:
        return current
    fields.append("updated_at = ?")
    values.append(_utc_stamp())
    values.append(project_id)
    conn = get_db()
    try:
        conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()
    return get_project(project_id)


def get_project_access(
    project_id: str, user_id: str, global_role: str = "member"
) -> Optional[Dict[str, Any]]:
    """Return project + effective role, or ``None`` when access is denied."""
    project = get_project(project_id)
    if not project:
        return None
    if global_role == "admin":
        return {**project, "team_role": "owner", "effective_role": "admin"}
    member = get_team_member(str(project["team_id"]), user_id)
    if not member:
        return None
    team_role = str(member["role"])
    if global_role == "viewer":
        effective = "viewer"
    else:
        effective = "admin" if team_role in {"owner", "admin"} else "member"
    return {**project, "team_role": team_role, "effective_role": effective}


def get_project_setting(project_id: str, key: str, default: str = "") -> str:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM project_settings WHERE project_id = ? AND key = ?",
            (project_id, key),
        ).fetchone()
    finally:
        conn.close()
    return str(row["value"]) if row and row["value"] is not None else default


def set_project_setting(project_id: str, key: str, value: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO project_settings (project_id, key, value, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(project_id, key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (project_id, key, value),
        )
        conn.commit()
    finally:
        conn.close()


def list_project_sessions_stats(project_id: str) -> Dict[str, Any]:
    conn = get_db()
    try:
        sessions = conn.execute(
            "SELECT COUNT(*) AS count FROM sessions WHERE project_id = ?", (project_id,)
        ).fetchone()["count"]
    finally:
        conn.close()
    return {"sessions": int(sessions)}


# Auth sessions & API tokens -------------------------------------------------
def create_auth_session(
    user_id: str,
    *,
    days: int = 7,
    user_agent: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    expires = _utc_stamp(days)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO user_sessions (id, user_id, csrf_token, expires_at, user_agent, ip) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, user_id, csrf, expires, user_agent[:300], ip[:100]),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": sid, "csrf_token": csrf, "expires_at": expires}


def get_auth_session(session_id: str) -> Optional[Dict[str, Any]]:
    if not session_id:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT s.*, u.username, u.display_name, u.role, u.status AS user_status "
            "FROM user_sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item.get("expires_at", "") < _utc_stamp():
            conn.execute("DELETE FROM user_sessions WHERE id = ?", (session_id,))
            conn.commit()
            return None
        if item.get("user_status") != "active":
            return None
        conn.execute(
            "UPDATE user_sessions SET last_seen_at = ? WHERE id = ?",
            (_utc_stamp(), session_id),
        )
        conn.commit()
        return item
    finally:
        conn.close()


def delete_auth_session(session_id: str) -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM user_sessions WHERE id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def purge_expired_auth_sessions() -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM user_sessions WHERE expires_at < ?", (_utc_stamp(),))
        conn.commit()
    finally:
        conn.close()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_api_token(
    user_id: str,
    name: str,
    *,
    scopes: Iterable[str] = (),
    expires_days: Optional[int] = None,
) -> Dict[str, Any]:
    name = name.strip()
    if not name:
        raise ValueError("Token 名称不能为空")
    secret = "omni_" + secrets.token_urlsafe(32)
    prefix = secret[:16]
    scope_text = ",".join(sorted({str(item).strip() for item in scopes if str(item).strip()}))
    expires = _utc_stamp(expires_days) if expires_days and expires_days > 0 else None
    token_id = "tok_" + uuid.uuid4().hex[:10]
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO api_tokens "
            "(id, user_id, name, token_prefix, token_hash, scopes, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_id, user_id, name[:100], prefix, _token_hash(secret), scope_text, expires),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": token_id,
        "name": name[:100],
        "token": secret,
        "prefix": prefix,
        "scopes": [item for item in scope_text.split(",") if item],
        "expires_at": expires,
    }


def authenticate_api_token(token: str) -> Optional[Dict[str, Any]]:
    token = token.strip()
    if not token.startswith("omni_") or len(token) < 24:
        return None
    prefix = token[:16]
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT t.*, u.username, u.display_name, u.role, u.status AS user_status "
            "FROM api_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token_prefix = ? AND t.revoked_at IS NULL",
            (prefix,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if not hmac.compare_digest(str(item["token_hash"]), _token_hash(token)):
            return None
        if item.get("expires_at") and str(item["expires_at"]) < _utc_stamp():
            return None
        if item.get("user_status") != "active":
            return None
        conn.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
            (_utc_stamp(), item["id"]),
        )
        conn.commit()
        return item
    finally:
        conn.close()


def list_api_tokens(user_id: str) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, user_id, name, token_prefix, scopes, created_at, last_used_at, "
            "expires_at, revoked_at FROM api_tokens WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        item = dict(row)
        item["scopes"] = [part for part in str(item.get("scopes") or "").split(",") if part]
        result.append(item)
    return result


def revoke_api_token(token_id: str, user_id: Optional[str] = None) -> bool:
    conn = get_db()
    try:
        if user_id:
            cursor = conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? "
                "AND revoked_at IS NULL",
                (_utc_stamp(), token_id, user_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_utc_stamp(), token_id),
            )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# Session operations
DEFAULT_SESSION_TITLES = {"新智能体对话", "新对话任务", "新对话", "新任务"}


def create_session(
    session_id: str,
    title: str = "新智能体对话",
    *,
    owner_id: Optional[str] = None,
    project_id: Optional[str] = None,
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sessions (id, title, owner_id, project_id) VALUES (?, ?, ?, ?)",
        (
            session_id,
            title,
            _user_scope(owner_id),
            _project_scope(project_id),
        ),
    )
    conn.commit()
    conn.close()
    return get_session(session_id)


def ensure_session(
    session_id: str,
    title: str = "新智能体对话",
    *,
    owner_id: Optional[str] = None,
    project_id: Optional[str] = None,
):
    """Create the session lazily; safe to call before every turn."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO sessions (id, title, owner_id, project_id) "
        "VALUES (?, ?, ?, ?)",
        (
            session_id,
            title,
            _user_scope(owner_id),
            _project_scope(project_id),
        ),
    )
    conn.commit()
    conn.close()


def _session_columns() -> str:
    return """
        s.*,
        (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
        (SELECT m.content FROM messages m WHERE m.session_id = s.id
         ORDER BY m.id DESC LIMIT 1) AS last_message,
        (SELECT m.role FROM messages m WHERE m.session_id = s.id
         ORDER BY m.id DESC LIMIT 1) AS last_role,
        (SELECT m.created_at FROM messages m WHERE m.session_id = s.id
         ORDER BY m.id DESC LIMIT 1) AS last_message_at
    """


def list_sessions(
    *,
    query: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    project_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List sessions newest first with message counts and previews.

    ``project_id="*"`` is reserved for global admins.  Normal callers get the
    active tenant context, never an unscoped list.
    """
    conn = get_db()
    cursor = conn.cursor()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    project = project_id if project_id is not None else current_project_id()
    project_clause = "" if project == "*" else "s.project_id = ?"
    project_values: tuple[Any, ...] = () if project == "*" else (project,)
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        cursor.execute(
            f"""
            SELECT {_session_columns()}
            FROM sessions s
            WHERE {project_clause + " AND " if project_clause else ""}(
                  s.title LIKE ? OR s.id LIKE ?
               OR EXISTS (
                   SELECT 1 FROM messages m
                   WHERE m.session_id = s.id AND m.content LIKE ?
               ))
            ORDER BY s.updated_at DESC, s.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (*project_values, pattern, pattern, pattern, limit, offset),
        )
    else:
        cursor.execute(
            f"""
            SELECT {_session_columns()}
            FROM sessions s
            {f"WHERE {project_clause}" if project_clause else ""}
            ORDER BY s.updated_at DESC, s.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (*project_values, limit, offset),
        )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_session_tenant(session_id: str) -> Optional[Dict[str, Any]]:
    """Return owner/project for authorising a conversation."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, owner_id, project_id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {_session_columns()} FROM sessions s WHERE s.id = ?",
        (session_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def rename_session(session_id: str, title: str) -> Optional[Dict[str, Any]]:
    title = " ".join(title.split()).strip()
    if not title:
        raise ValueError("会话标题不能为空")
    if len(title) > 120:
        title = title[:120]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE sessions SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (title, session_id),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return get_session(session_id) if changed else None


def delete_session(session_id: str) -> bool:
    """Delete a conversation and all local traces tied to it."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
        if cursor.fetchone() is None:
            return False
        cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cursor.execute(
            "DELETE FROM permission_requests WHERE session_id = ?", (session_id,)
        )
        cursor.execute(
            "DELETE FROM subagent_tasks WHERE parent_session_id = ?", (session_id,)
        )
        cursor.execute("DELETE FROM tool_audit WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def clear_session_messages(session_id: str) -> bool:
    """Clear a conversation while keeping the session shell."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
        if cursor.fetchone() is None:
            return False
        cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cursor.execute(
            "DELETE FROM permission_requests WHERE session_id = ?", (session_id,)
        )
        cursor.execute(
            "DELETE FROM subagent_tasks WHERE parent_session_id = ?", (session_id,)
        )
        cursor.execute("DELETE FROM tool_audit WHERE session_id = ?", (session_id,))
        cursor.execute(
            "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def maybe_auto_title(session_id: str, first_message: str) -> None:
    """Give a new conversation a useful title from its first user message."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT title FROM sessions WHERE id = ?", (session_id,))
    row = cursor.fetchone()
    if row is None or str(row["title"] or "") not in DEFAULT_SESSION_TITLES:
        conn.close()
        return
    cursor.execute(
        "SELECT COUNT(*) AS count FROM messages WHERE session_id = ? AND role = 'user'",
        (session_id,),
    )
    count = int(cursor.fetchone()["count"])
    if count != 1:
        conn.close()
        return
    title = re.sub(r"\s+", " ", first_message).strip()
    title = re.sub(r"^[#>*\-`\s]+", "", title)
    title = title[:42] + ("…" if len(title) > 42 else "")
    if title:
        cursor.execute(
            "UPDATE sessions SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (title, session_id),
        )
        conn.commit()
    conn.close()


def get_database_stats() -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    counts: Dict[str, int] = {}
    for table in (
        "sessions",
        "messages",
        "memories",
        "knowledge_docs",
        "permission_requests",
        "subagent_tasks",
        "tool_audit",
        "scheduled_tasks",
        "users",
        "teams",
        "team_members",
        "projects",
        "api_tokens",
    ):
        cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
        counts[table] = int(cursor.fetchone()["count"])
    cursor.execute("SELECT role, COUNT(*) AS count FROM messages GROUP BY role")
    message_roles = {
        str(row["role"]): int(row["count"]) for row in cursor.fetchall()
    }
    conn.close()
    try:
        size_bytes = os.path.getsize(DB_PATH)
    except OSError:
        size_bytes = 0
    return {
        "counts": counts,
        "message_roles": message_roles,
        "database_bytes": size_bytes,
    }


def get_session_messages(session_id: str) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,)
    )
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
def get_all_memories(
    limit: Optional[int] = None, *, project_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    project = _project_scope(project_id)
    conn = get_db()
    cursor = conn.cursor()
    if limit is None:
        cursor.execute(
            "SELECT * FROM memories WHERE project_id = ? "
            "ORDER BY importance DESC, updated_at DESC",
            (project,),
        )
    else:
        cursor.execute(
            "SELECT * FROM memories WHERE project_id = ? "
            "ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (project, max(1, int(limit))),
        )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_memory(
    memory_id: int, *, project_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM memories WHERE id = ? AND project_id = ?",
        (memory_id, _project_scope(project_id)),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def save_memory(
    category: str,
    key: str,
    content: str,
    importance: float = 1.0,
    *,
    project_id: Optional[str] = None,
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

    project = _project_scope(project_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM memories WHERE project_id = ? AND category = ? AND key = ? "
        "ORDER BY id ASC LIMIT 1",
        (project, category, key),
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
            "DELETE FROM memories WHERE project_id = ? AND category = ? AND key = ? "
            "AND id <> ?",
            (project, category, key, memory_id),
        )
    else:
        cursor.execute(
            """
            INSERT INTO memories (category, key, content, importance, project_id, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (category, key, content, importance_value, project),
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


def delete_memory(memory_id: int, *, project_id: Optional[str] = None) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM memories WHERE id = ? AND project_id = ?",
        (memory_id, _project_scope(project_id)),
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def search_memories(
    query: str, limit: int = 5, *, project_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return query-relevant memories, falling back to importance order."""
    safe_limit = max(1, int(limit))
    if not query.strip():
        return get_all_memories(safe_limit, project_id=project_id)
    return retrieval.rank_memories(
        query, get_all_memories(project_id=project_id), safe_limit
    )


def relevant_memories(
    query: str,
    *,
    limit: int = 6,
    always_include: int = 3,
    project_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return retrieval.relevant_memories(
        query,
        get_all_memories(project_id=project_id),
        limit=limit,
        always_include=always_include,
    )


# Knowledge / RAG
def list_knowledge(
    limit: int = 50, offset: int = 0, *, project_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM knowledge_docs WHERE project_id = ? "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (_project_scope(project_id), max(1, int(limit)), max(0, int(offset))),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_knowledge(
    doc_id: int, *, project_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM knowledge_docs WHERE id = ? AND project_id = ?",
        (doc_id, _project_scope(project_id)),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def add_knowledge(
    title: str,
    content: str,
    tags: str = "",
    *,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    title = title.strip()
    content = content.strip()
    if not title or not content:
        raise ValueError("知识文档的 title 和 content 不能为空")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO knowledge_docs (title, content, tags, project_id) "
        "VALUES (?, ?, ?, ?)",
        (title, content, tags.strip(), _project_scope(project_id)),
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


def delete_knowledge(doc_id: int, *, project_id: Optional[str] = None) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM knowledge_docs WHERE id = ? AND project_id = ?",
        (doc_id, _project_scope(project_id)),
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def search_knowledge(
    query: str, limit: int = 3, *, project_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Search knowledge with BM25 + Chinese bigrams (no embeddings needed)."""
    safe_limit = max(1, int(limit))
    if not query.strip():
        return list_knowledge(safe_limit, project_id=project_id)
    return retrieval.rank_knowledge(
        query, list_knowledge(1000, project_id=project_id), safe_limit
    )

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
    *,
    project_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> Dict[str, Any]:
    name = name.strip()
    prompt = prompt.strip()
    if not name or not prompt:
        raise ValueError("定时任务的名称和提示词不能为空")
    interval = max(1, int(interval_seconds))
    task_id = "sch_" + uuid.uuid4().hex[:10]
    session_id = "sched_" + task_id
    next_run_at = _next_schedule_time(interval)
    project = _project_scope(project_id)
    owner = owner_id or current_user_id()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO scheduled_tasks
            (id, name, prompt, interval_seconds, enabled, session_id, next_run_at,
             project_id, owner_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            name,
            prompt,
            interval,
            1 if enabled else 0,
            session_id,
            next_run_at,
            project,
            owner,
        ),
    )
    conn.commit()
    conn.close()
    created = get_scheduled_task(task_id)
    if not created:
        raise RuntimeError("定时任务创建失败")
    return created


def list_scheduled_tasks(
    *, project_id: Optional[str] = None, include_all: bool = False
) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    if include_all:
        cursor.execute(
            """
            SELECT * FROM scheduled_tasks
            ORDER BY enabled DESC, next_run_at ASC, created_at DESC
            """
        )
    else:
        cursor.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE project_id = ?
            ORDER BY enabled DESC, next_run_at ASC, created_at DESC
            """,
            (_project_scope(project_id),),
        )
    rows = cursor.fetchall()
    conn.close()
    return [_schedule_row(row) for row in rows]


def get_scheduled_task(
    task_id: str,
    *,
    project_id: Optional[str] = None,
    include_all: bool = False,
) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    if include_all:
        cursor.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
    else:
        cursor.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ? AND project_id = ?",
            (task_id, _project_scope(project_id)),
        )
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
    project_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_scheduled_task(task_id, project_id=project_id)
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
    values.append(_project_scope(project_id))
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ? AND project_id = ?",
        tuple(values),
    )
    conn.commit()
    conn.close()
    return get_scheduled_task(task_id, project_id=project_id)


def delete_scheduled_task(
    task_id: str, *, project_id: Optional[str] = None
) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM scheduled_tasks WHERE id = ? AND project_id = ?",
        (task_id, _project_scope(project_id)),
    )
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
    return get_scheduled_task(task_id, include_all=True)


def finish_scheduled_task_run(
    task_id: str,
    status: str,
    result: str,
    *,
    next_run_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_scheduled_task(task_id, include_all=True)
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
    return get_scheduled_task(task_id, include_all=True)


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
def record_subagent_task(
    task_id: str,
    parent_session_id: str,
    role: str,
    task: str,
    *,
    project_id: Optional[str] = None,
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO subagent_tasks (id, parent_session_id, role, task, status, project_id)
    VALUES (?, ?, ?, ?, 'RUNNING', ?)
    """, (task_id, parent_session_id, role, task, _project_scope(project_id)))
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

def list_subagents(
    session_id: Optional[str] = None,
    *,
    project_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    project = _project_scope(project_id)
    if session_id:
        cursor.execute(
            "SELECT * FROM subagent_tasks WHERE parent_session_id = ? "
            "AND project_id = ? ORDER BY created_at DESC",
            (session_id, project),
        )
    else:
        cursor.execute(
            "SELECT * FROM subagent_tasks WHERE project_id = ? "
            "ORDER BY created_at DESC",
            (project,),
        )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
