from __future__ import annotations

import permission
from browser import BrowserManager


def test_status_does_not_start_browser() -> None:
    manager = BrowserManager()
    assert manager.running is False
    output = manager.run("status")
    assert "未启动" in output
    assert manager._thread is None


def test_private_navigation_is_rejected_before_launch() -> None:
    manager = BrowserManager()
    output = manager.run("goto", {"url": "http://127.0.0.1:8001"})
    assert "拒绝" in output
    assert manager._thread is None


def test_action_arguments_are_validated() -> None:
    manager = BrowserManager()
    assert "需要 selector" in manager.run("click")
    assert "需要 selector" in manager.run("fill")
    assert "需要 key" in manager.run("press")
    assert manager._thread is None


def test_screenshot_path_cannot_escape_workspace() -> None:
    manager = BrowserManager()
    output = manager.run("screenshot", {"filename": "../escape.png"})
    assert "拒绝" in output
    assert manager._thread is None


def test_unknown_action_is_rejected() -> None:
    manager = BrowserManager()
    output = manager.run("eval", {"code": "alert(1)"})
    assert "不支持" in output
    assert manager._thread is None


def test_browser_permissions() -> None:
    risk, _ = permission.classify_permission(
        "browser", {"action": "goto", "url": "https://example.com"}
    )
    assert risk == permission.PermissionRisk.ALLOW

    risk, _ = permission.classify_permission(
        "browser", {"action": "goto", "url": "http://127.0.0.1:8001"}
    )
    assert risk == permission.PermissionRisk.DENY

    for action in ("click", "fill", "press"):
        risk, _ = permission.classify_permission(
            "browser", {"action": action, "selector": "#x", "value": "v", "key": "Enter"}
        )
        assert risk == permission.PermissionRisk.ASK_USER

    risk, _ = permission.classify_permission("browser", {"action": "text"})
    assert risk == permission.PermissionRisk.ALLOW

    risk, _ = permission.classify_permission(
        "browser", {"action": "screenshot", "filename": "../x.png"}
    )
    assert risk == permission.PermissionRisk.DENY

    risk, _ = permission.classify_permission(
        "browser", {"action": "screenshot", "filename": "shot.png"}
    )
    assert risk == permission.PermissionRisk.SAFE_EXEC
