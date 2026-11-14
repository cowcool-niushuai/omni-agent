from pathlib import Path

from model_manager import model_manager
from omni_agent import tenancy
from omni_agent.runtime.graph import AgentRuntime, _message_text


def _context(workspace: Path) -> tenancy.TenantContext:
    return tenancy.TenantContext(
        user_id="usr_test",
        project_id="proj_test",
        team_id="team_test",
        workspace=workspace,
        role="admin",
        scopes=frozenset({"*"}),
    )


def test_message_text_extracts_openai_content_blocks() -> None:
    assert _message_text("hello") == "hello"
    assert _message_text([{"type": "text", "text": "看图"}, {"type": "image_url"}]) == "看图"


def test_build_user_content_adds_workspace_image(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "picture.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(
        model_manager,
        "get_active_model_info",
        lambda: {"label": "DeepSeek 视觉模型", "model_name": "deepseek-vl", "capabilities": {"vision": True}},
    )
    runtime = AgentRuntime(checkpoint_path=tmp_path / "checkpoints.db")

    content = runtime._build_user_content(
        _context(tmp_path),
        "描述这张图片",
        [{"path": "picture.png", "name": "picture.png"}],
    )

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "描述这张图片"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_build_user_content_skips_escape_and_text_models(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "picture.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    runtime = AgentRuntime(checkpoint_path=tmp_path / "checkpoints.db")
    ctx = _context(tmp_path)

    monkeypatch.setattr(
        model_manager,
        "get_active_model_info",
        lambda: {"label": "普通文本模型", "model_name": "text-only", "capabilities": {}},
    )
    assert runtime._build_user_content(ctx, "看图", [{"path": "picture.png"}]) == "看图"

    monkeypatch.setattr(
        model_manager,
        "get_active_model_info",
        lambda: {"label": "视觉模型", "model_name": "vision", "capabilities": {"vision": True}},
    )
    assert runtime._build_user_content(ctx, "看图", [{"path": "../picture.png"}]) == "看图"
