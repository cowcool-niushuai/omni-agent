"""Local attachment storage for chat uploads."""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from pathlib import Path
from typing import Any

from omni_agent.security import ensure_within


class UploadError(ValueError):
    """Raised when an uploaded file cannot be accepted."""


def _safe_filename(filename: str) -> str:
    name = Path(str(filename or "")).name.strip()
    name = re.sub(r"[^\w.\- ()\[\]]", "_", name, flags=re.UNICODE).strip(" .")
    if not name:
        name = "attachment"
    if len(name) > 160:
        stem = Path(name).stem[:120]
        suffix = Path(name).suffix[:20]
        name = f"{stem}{suffix}"
    return name


def _safe_session_id(session_id: str) -> str:
    value = re.sub(r"[^\w.\-]", "_", str(session_id or ""), flags=re.UNICODE).strip(" .")
    return value[:80] or "adhoc"


def save_attachment(
    workspace: Path,
    filename: str,
    content_base64: str,
    *,
    session_id: str = "",
    max_bytes: int = 20 * 1024 * 1024,
) -> dict[str, Any]:
    """Persist a base64 chat attachment under ``workspace/.uploads``."""

    try:
        raw = base64.b64decode(content_base64 or "", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise UploadError("文件内容不是有效的 base64 编码。") from exc
    if not raw:
        raise UploadError("文件为空。")
    if len(raw) > max_bytes:
        raise UploadError(f"文件超过大小限制（{max_bytes // (1024 * 1024)} MB）。")

    safe_name = _safe_filename(filename)
    folder = _safe_session_id(session_id)
    root = Path(workspace).resolve() / ".uploads" / folder
    root.mkdir(parents=True, exist_ok=True)
    destination = ensure_within(
        Path(workspace).resolve(),
        root / f"{uuid.uuid4().hex[:8]}_{safe_name}",
    )
    destination.write_bytes(raw)
    try:
        destination.chmod(0o600)
    except OSError:
        pass

    relative = destination.relative_to(Path(workspace).resolve())
    return {
        "name": safe_name,
        "path": str(relative),
        "size": len(raw),
    }
