from types import SimpleNamespace

import pytest

import desktop_apps
import permission


def _settings(**overrides):
    values = {
        "desktop_app_launch_enabled": True,
        "desktop_app_list_limit": 80,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_list_apps_deduplicates_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        desktop_apps,
        "_flatpak_apps",
        lambda: [
            desktop_apps.DesktopApp(
                "com.qq.QQ", "QQ", "flatpak", ("flatpak", "run", "com.qq.QQ")
            )
        ],
    )
    monkeypatch.setattr(desktop_apps, "_desktop_entries", lambda: [])
    monkeypatch.setattr(desktop_apps, "get_settings", lambda: _settings())
    assert desktop_apps.list_apps() == [
        {"id": "com.qq.QQ", "name": "QQ", "kind": "flatpak"}
    ]


def test_launch_qq_alias_uses_flatpak_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = desktop_apps.DesktopApp(
        "com.qq.QQ", "QQ", "flatpak", ("flatpak", "run", "com.qq.QQ")
    )
    monkeypatch.setattr(desktop_apps, "_flatpak_apps", lambda: [app])
    monkeypatch.setattr(desktop_apps, "_desktop_entries", lambda: [])
    monkeypatch.setattr(desktop_apps, "_display_ready", lambda: True)
    monkeypatch.setattr(desktop_apps, "get_settings", lambda: _settings())
    calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(desktop_apps.subprocess, "Popen", fake_popen)
    result = desktop_apps.launch_app("qq")
    assert result["ok"] is True
    assert result["id"] == "com.qq.QQ"
    assert calls[0][0] == ["flatpak", "run", "com.qq.QQ"]
    assert calls[0][1]["start_new_session"] is True


def test_launch_rejects_missing_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_apps, "_display_ready", lambda: False)
    monkeypatch.setattr(desktop_apps, "get_settings", lambda: _settings())
    with pytest.raises(desktop_apps.AppLaunchError, match="图形会话"):
        desktop_apps.launch_app("qq")


def test_launch_rejects_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        desktop_apps,
        "get_settings",
        lambda: _settings(desktop_app_launch_enabled=False),
    )
    with pytest.raises(desktop_apps.AppLaunchError, match="已关闭"):
        desktop_apps.launch_app("qq")


def test_desktop_app_permission_policy() -> None:
    assert permission.classify_permission(
        "desktop_app", {"action": "list"}
    )[0] == permission.PermissionRisk.ALLOW
    assert permission.classify_permission(
        "desktop_app", {"action": "open", "name": "qq"}
    )[0] == permission.PermissionRisk.ASK_USER
    assert permission.classify_permission(
        "desktop_app", {"action": "open", "name": ""}
    )[0] == permission.PermissionRisk.DENY
