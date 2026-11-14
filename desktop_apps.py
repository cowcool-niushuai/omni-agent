"""Launch installed desktop applications from the OmniAgent host process.

The agent's normal ``bash_run`` tool is deliberately sandboxed and therefore
cannot see the user's GUI session.  This module is a narrow, explicit bridge:
it only launches applications that are already installed for the current user,
never passes a shell, and requires an approval for the ``open`` action.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni_agent.config import get_settings

DISPLAY_ENV = ("DISPLAY", "WAYLAND_DISPLAY")
DESKTOP_DIRS = (
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path.home() / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
)
ALIASES = {
    "qq": "com.qq.QQ",
    "qq音乐": "com.qq.QQmusic",
    "qqmusic": "com.qq.QQmusic",
    "微信": "com.tencent.WeChat",
    "wechat": "com.tencent.WeChat",
    "chrome": "com.google.Chrome",
    "谷歌浏览器": "com.google.Chrome",
    "chromium": "org.chromium.Chromium",
    "telegram": "org.telegram.desktop",
    "vscode": "com.microsoft.VSCode",
    "code": "com.microsoft.VSCode",
    "unity": "com.unity.UnityHub",
}


class AppLaunchError(RuntimeError):
    """Raised when a desktop application cannot be found or launched."""


@dataclass(frozen=True)
class DesktopApp:
    id: str
    name: str
    kind: str  # flatpak | desktop
    command: tuple[str, ...]
    desktop_file: str = ""


def _display_ready() -> bool:
    return any(os.environ.get(key) for key in DISPLAY_ENV)


def _flatpak_apps() -> list[DesktopApp]:
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return []
    try:
        proc = subprocess.run(
            [flatpak, "list", "--app", "--columns=application,name"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    apps: list[DesktopApp] = []
    for line in proc.stdout.splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) < 2 or not parts[0]:
            continue
        app_id, name = parts[0], parts[1] or parts[0]
        apps.append(
            DesktopApp(
                id=app_id,
                name=name,
                kind="flatpak",
                command=(flatpak, "run", app_id),
            )
        )
    return apps


def _desktop_value(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}=(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _desktop_entries() -> list[DesktopApp]:
    apps: list[DesktopApp] = []
    seen: set[str] = set()
    for directory in DESKTOP_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.desktop")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not text.startswith("[Desktop Entry]"):
                continue
            if _desktop_value(text, "NoDisplay").lower() == "true":
                continue
            if _desktop_value(text, "Hidden").lower() == "true":
                continue
            name = _desktop_value(text, "Name[zh_CN]") or _desktop_value(text, "Name")
            exec_line = _desktop_value(text, "Exec")
            if not name or not exec_line:
                continue
            desktop_id = path.name
            if desktop_id in seen:
                continue
            seen.add(desktop_id)
            # Parsing Exec is intentionally conservative: no shell, and only
            # the executable plus literal arguments are retained.
            command = tuple(
                part
                for part in exec_line.split()
                if part and not part.startswith("%")
            )
            if not command:
                continue
            apps.append(
                DesktopApp(
                    id=desktop_id,
                    name=name,
                    kind="desktop",
                    command=command,
                    desktop_file=str(path),
                )
            )
    return apps


def list_apps(limit: int = 80) -> list[dict[str, str]]:
    settings = get_settings()
    maximum = max(1, min(int(limit or 80), int(settings.desktop_app_list_limit)))
    apps = _flatpak_apps() + _desktop_entries()
    unique: dict[str, DesktopApp] = {}
    for app in apps:
        unique.setdefault(app.id.casefold(), app)
    return [
        {"id": app.id, "name": app.name, "kind": app.kind}
        for app in list(unique.values())[:maximum]
    ]


def _match_apps(query: str, apps: list[DesktopApp]) -> list[DesktopApp]:
    raw = query.strip().casefold()
    alias = ALIASES.get(raw.casefold())
    candidates: list[DesktopApp] = []
    for app in apps:
        if alias and app.id.casefold() == alias.casefold():
            return [app]
        if raw == app.id.casefold() or raw == app.name.casefold():
            return [app]
        if raw in app.id.casefold() or raw in app.name.casefold():
            candidates.append(app)
    # A unique prefix/substring match is enough, but never guess between apps.
    if len(candidates) == 1:
        return candidates
    return candidates


def launch_app(query: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.desktop_app_launch_enabled:
        raise AppLaunchError("桌面应用启动功能已关闭（DESKTOP_APP_LAUNCH_ENABLED=false）")
    name = query.strip()
    if not name:
        raise AppLaunchError("请告诉我要打开哪个应用")
    if len(name) > 100:
        raise AppLaunchError("应用名称过长")
    if not _display_ready():
        raise AppLaunchError(
            "当前后端没有图形会话（DISPLAY/WAYLAND_DISPLAY 为空），"
            "无法打开桌面应用。请从桌面会话启动 OmniAgent。"
        )

    apps = _flatpak_apps() + _desktop_entries()
    candidates = _match_apps(name, apps)
    if not candidates:
        available = ", ".join(item["name"] for item in list_apps(12))
        raise AppLaunchError(
            f"没有找到桌面应用「{name}」。当前可用的应用示例：{available or '无'}"
        )
    if len(candidates) > 1:
        names = "、".join(f"{item.name} ({item.id})" for item in candidates[:8])
        raise AppLaunchError(f"「{name}」匹配到多个应用，请说得更具体：{names}")
    app = candidates[0]
    try:
        if app.kind == "flatpak":
            argv = ["flatpak", "run", app.id]
        elif shutil.which("gtk-launch"):
            argv = ["gtk-launch", app.id.removesuffix(".desktop")]
        elif shutil.which("gio"):
            argv = ["gio", "launch", app.desktop_file]
        else:
            argv = list(app.command)
        process = subprocess.Popen(
            argv,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise AppLaunchError(f"启动「{app.name}」失败：{exc}") from exc
    return {
        "ok": True,
        "name": app.name,
        "id": app.id,
        "kind": app.kind,
        "pid": process.pid,
    }
