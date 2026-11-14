"""Shell command policy.

The policy replaces the old "regex blacklist" with three layers:

1. Syntax parsing: commands are parsed with :mod:`shlex` and executed without
   a shell. Shell operators are rejected in strict mode, which removes the
   classic ``; rm -rf /`` / ``$(...)`` bypass.
2. Executable allow-list: known read-only, workspace-write, approval-required
   and denied executables have explicit risks. Unknown binaries require human
   approval instead of being silently trusted.
3. Argument validation: path-like arguments must stay inside the workspace
   (read-only commands may additionally read system binary directories), and
   a small set of high-risk arguments (``find -exec``, ``git -c``, ...) is
   escalated to approval.

The module is pure: it never executes anything and never touches the DB.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

ALLOW = "ALLOW"
SAFE_EXEC = "SAFE_EXEC"
ASK_USER = "ASK_USER"
DENY = "DENY"

MAX_COMMAND_LENGTH = 8192
MAX_ARGUMENTS = 128
MAX_ARGUMENT_LENGTH = 4096

# Paths that must never be touched by an agent command, even after approval.
DENIED_PATHS = (
    "/etc",
    "/root",
    "/sys",
    "/proc",
    "/boot",
    "/dev",
    "/var/run",
    "/run",
)
# Root-level locations that are catastrophic to operate on directly.
DENIED_ROOTS = (
    "/",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/home",
    "/mnt",
    "/media",
    "/srv",
    "/opt",
    "/var",
    "/tmp",  # allowed for destructive commands only when a deeper path is given
)
# Read-only commands may inspect these system locations.
ALLOWED_SYSTEM_READ_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt")

DENIED_EXECUTABLES = {
    "sudo",
    "su",
    "doas",
    "pkexec",
    "mount",
    "umount",
    "mkfs",
    "mkfs.ext2",
    "mkfs.ext3",
    "mkfs.ext4",
    "mkfs.xfs",
    "fdisk",
    "parted",
    "losetup",
    "reboot",
    "shutdown",
    "poweroff",
    "halt",
    "init",
    "systemctl",
    "service",
    "iptables",
    "ip6tables",
    "nft",
    "ufw",
    "insmod",
    "rmmod",
    "modprobe",
    "sysctl",
    "setcap",
    "chroot",
    "unshare",
    "nsenter",
    "swapon",
    "swapoff",
    "visudo",
    "passwd",
    "useradd",
    "userdel",
    "usermod",
    "groupadd",
    "groupdel",
}

# Shells are approval-only because they can re-introduce arbitrary syntax.
SHELL_EXECUTABLES = {"bash", "sh", "zsh", "fish", "dash", "ksh", "csh", "tcsh"}

READ_ONLY_EXECUTABLES = {
    "ls",
    "dir",
    "cat",
    "tac",
    "head",
    "tail",
    "nl",
    "wc",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ag",
    "stat",
    "file",
    "which",
    "whereis",
    "pwd",
    "echo",
    "printf",
    "date",
    "cal",
    "uname",
    "hostname",
    "id",
    "whoami",
    "groups",
    "df",
    "du",
    "free",
    "uptime",
    "ps",
    "pgrep",
    "sort",
    "uniq",
    "cut",
    "tr",
    "rev",
    "paste",
    "join",
    "comm",
    "diff",
    "cmp",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "sha512sum",
    "base64",
    "xxd",
    "od",
    "hexdump",
    "strings",
    "jq",
    "tree",
    "realpath",
    "readlink",
    "basename",
    "dirname",
    "expr",
    "seq",
    "column",
    "test",
    "[",
}

WRITE_EXECUTABLES = {
    "mkdir",
    "touch",
    "cp",
    "mv",
    "ln",
    "tee",
    "install",
    "tar",
    "zip",
    "unzip",
    "gzip",
    "gunzip",
    "bzip2",
    "xz",
    "make",
    "gcc",
    "g++",
    "clang",
    "clang++",
    "cc",
    "python",
    "python3",
    "node",
    "java",
    "javac",
    "go",
    "rustc",
    "pytest",
    "py.test",
    "ruff",
    "mypy",
    "black",
    "isort",
    "uv",
    "pip",
    "pip3",
    "npm",
    "npx",
    "yarn",
    "pnpm",
    "bun",
    "deno",
    "cargo",
}

ASK_EXECUTABLES = {
    "rm",
    "rmdir",
    "unlink",
    "shred",
    "truncate",
    "chmod",
    "chown",
    "chgrp",
    "kill",
    "pkill",
    "killall",
    "dd",
    "crontab",
    "at",
    "ssh",
    "scp",
    "sftp",
    "rsync",
    "nc",
    "ncat",
    "netcat",
    "socat",
    "telnet",
    "ftp",
    "curl",
    "wget",
    "docker",
    "podman",
    "nerdctl",
    "kubectl",
    "env",
    "nohup",
    "timeout",
    "watch",
    "xargs",
    "eval",
    "exec",
    "source",
    ".",
}

GIT_READ_SUBCOMMANDS = {
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "blame",
    "describe",
    "shortlog",
    "grep",
    "remote",
    "tag",
    "stash",
}
GIT_WRITE_SUBCOMMANDS = {
    "add",
    "commit",
    "rm",
    "mv",
    "restore",
    "reset",
    "checkout",
    "switch",
    "merge",
    "rebase",
    "cherry-pick",
    "revert",
    "apply",
    "am",
    "clean",
    "init",
    "gc",
    "prune",
}
GIT_ASK_SUBCOMMANDS = {
    "clone",
    "fetch",
    "pull",
    "push",
    "submodule",
    "config",
    "remote",
    "worktree",
    "filter-branch",
    "daemon",
}

NETWORK_SUBCOMMANDS = {
    "pip": {"install", "download", "wheel"},
    "pip3": {"install", "download", "wheel"},
    "uv": {"add", "sync", "lock", "pip", "tool", "python"},
    "npm": {"install", "i", "add", "exec", "x", "publish", "update"},
    "npx": {"install", "exec"},
    "yarn": {"install", "add", "upgrade", "publish"},
    "pnpm": {"install", "add", "update", "publish", "dlx"},
    "bun": {"install", "add", "update", "x", "publish"},
    "cargo": {"install", "add", "publish", "update", "fetch"},
    "go": {"install", "get", "mod"},
}

SHELL_SYNTAX_CHARS = set(";|&<>(){}\n\r")
SHELL_EXPANSION_MARKERS = ("$(", "${", "`")

FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{.*:\s*\|\s*:")
SQL_DANGEROUS = re.compile(r"(;|--|/\*|\b(ATTACH|DETACH|PRAGMA)\b)", re.IGNORECASE)


class CommandPolicyError(ValueError):
    """Raised when a command cannot be parsed safely."""


@dataclass(frozen=True)
class CommandAssessment:
    argv: list[str]
    executable: str
    risk: str
    reason: str
    uses_shell_syntax: bool = False
    policy_mode: str = "strict"


def _unquoted_view(command: str) -> str:
    """Return the command with quoted spans blanked, for syntax scanning."""
    result: list[str] = []
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            result.append(" ")
            continue
        if quote == "'":
            if char == "'":
                quote = None
            result.append(" ")
            continue
        if quote == '"':
            if char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            result.append(" ")
            continue
        if char == "\\":
            escaped = True
            result.append(" ")
            continue
        if char in {"'", '"'}:
            quote = char
            result.append(" ")
            continue
        result.append(char)
    if quote is not None:
        raise CommandPolicyError("引号未闭合")
    return "".join(result)


def _has_shell_syntax(command: str) -> bool:
    view = _unquoted_view(command)
    if any(char in SHELL_SYNTAX_CHARS for char in view):
        return True
    return any(marker in command for marker in SHELL_EXPANSION_MARKERS)


def _parse_argv(command: str) -> list[str]:
    try:
        argv = shlex.split(command, comments=False, posix=True)
    except ValueError as exc:
        raise CommandPolicyError(f"命令无法解析: {exc}") from exc
    if not argv:
        raise CommandPolicyError("命令为空")
    if len(argv) > MAX_ARGUMENTS:
        raise CommandPolicyError(f"参数数量超过限制 ({MAX_ARGUMENTS})")
    if any(len(arg) > MAX_ARGUMENT_LENGTH for arg in argv):
        raise CommandPolicyError("单个参数过长")
    return argv


def _path_like(token: str) -> bool:
    if not token or token.startswith("-") or token in {"-", "|"}:
        return False
    return "/" in token or token in {".", ".."} or token.startswith("~")


def _resolve_candidate(token: str, workspace_root: Path) -> Path | None:
    if token.startswith("~"):
        return None
    candidate = Path(token)
    if candidate.is_absolute():
        return candidate
    return workspace_root / candidate


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_denied_path_token(token: str) -> bool:
    normalised = token.rstrip("/") or "/"
    if normalised in {"/", "/*", "/**", "/.*"}:
        return True
    for denied in DENIED_PATHS:
        if normalised == denied or normalised.startswith(denied + "/"):
            return True
    for root in DENIED_ROOTS:
        if normalised == root:
            return True
    return False


def _validate_arguments(
    argv: list[str],
    *,
    workspace_root: Path,
    base_risk: str,
    skip_code_arguments: bool = False,
) -> tuple[str, str]:
    """Validate path-like arguments; returns ``(risk, reason)``."""
    risk = base_risk
    for index, token in enumerate(argv[1:], start=1):
        if token.startswith("--") and "=" in token:
            _, value = token.split("=", 1)
            tokens = [value]
        else:
            tokens = [token]

        for value in tokens:
            if _is_denied_path_token(value):
                return DENY, f"系统保护拦截：命令参数包含受限路径 {value}"
            if not _path_like(value):
                continue
            if skip_code_arguments:
                break
            candidate = _resolve_candidate(value, workspace_root)
            if candidate is None:
                return DENY, f"不允许使用主目录展开路径: {value}"
            if _inside(candidate, workspace_root):
                continue
            if base_risk == ALLOW:
                text = str(candidate)
                if any(
                    text == root or text.startswith(root + "/")
                    for root in ALLOWED_SYSTEM_READ_ROOTS
                ):
                    continue
                return DENY, f"只读命令不允许访问工作区外路径: {value}"
            if base_risk == SAFE_EXEC:
                return DENY, f"写入类命令只能操作工作区目录，已阻止: {value}"
            # ASK_USER commands may target external paths, but the human sees
            # the request and the denied list above still applies.
            if index == 0:  # pragma: no cover - argv[0] is handled separately
                continue
    return risk, ""


def _git_subcommand(argv: list[str]) -> tuple[str, int]:
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in {"-C", "-c", "--git-dir", "--work-tree"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, index
    return "", index


def _git_assessment(argv: list[str]) -> tuple[str, str]:
    if any(token in {"-c", "--exec-path", "--config-env"} for token in argv[1:]):
        return ASK_USER, "git 配置/执行路径覆盖需要人工审批"
    subcommand, _ = _git_subcommand(argv)
    if not subcommand:
        return ALLOW, "git 只读信息查询"
    if subcommand in GIT_ASK_SUBCOMMANDS:
        return ASK_USER, f"git {subcommand} 可能访问网络或修改仓库配置，需要审批"
    if subcommand in GIT_READ_SUBCOMMANDS:
        return ALLOW, f"git {subcommand} 为只读仓库操作"
    if subcommand in GIT_WRITE_SUBCOMMANDS:
        return SAFE_EXEC, f"git {subcommand} 仅修改工作区仓库"
    return ASK_USER, f"未列入白名单的 git 子命令: {subcommand}"


def _sed_assessment(argv: list[str]) -> tuple[str, str]:
    script = ""
    in_place = False
    for token in argv[1:]:
        if token in {"-i", "--in-place"} or (
            token.startswith("-i") and not token.startswith("--")
        ):
            in_place = True
        elif not token.startswith("-"):
            script = token
            break
    if re.search(r"(^|[;{\s])[ew]", script):
        return ASK_USER, "sed 脚本包含写文件/执行命令指令，需要审批"
    if in_place:
        return SAFE_EXEC, "sed 原地编辑工作区文件"
    return ALLOW, "sed 只读文本处理"


def _awk_assessment(argv: list[str]) -> tuple[str, str]:
    script = ""
    for token in argv[1:]:
        if not token.startswith("-"):
            script = token
            break
    if re.search(r"\b(system|getline|ENVIRON)\b|\|\s*&?", script):
        return ASK_USER, "awk 脚本包含执行命令或读写外部数据的能力，需要审批"
    return ALLOW, "awk 只读文本处理"


def _find_assessment(argv: list[str]) -> tuple[str, str]:
    dangerous = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"}
    if any(token in dangerous for token in argv[1:]):
        return ASK_USER, "find 的删除/执行动作需要人工审批"
    return ALLOW, "find 只读目录检索"


def _network_subcommand_assessment(executable: str, argv: list[str]) -> tuple[str, str] | None:
    subcommands = NETWORK_SUBCOMMANDS.get(executable)
    if not subcommands:
        return None
    subcommand = next((token for token in argv[1:] if not token.startswith("-")), "")
    if subcommand in subcommands:
        return ASK_USER, f"{executable} {subcommand} 可能访问网络或安装依赖，需要审批"
    return None


def _python_module_assessment(executable: str, argv: list[str]) -> tuple[str, str] | None:
    if executable not in {"python", "python3"}:
        return None
    lowered = [token.lower() for token in argv[1:]]
    if "-m" not in lowered:
        return None
    index = lowered.index("-m")
    module = lowered[index + 1] if index + 1 < len(lowered) else ""
    if module in {"pip", "pip3", "ensurepip"} and any(
        token in {"install", "download", "wheel"} for token in lowered
    ):
        return ASK_USER, "python -m pip 安装依赖需要联网，需要人工审批"
    if module in {"http.server", "socketserver", "ftplib", "telnetlib", "smtplib"}:
        return ASK_USER, f"python -m {module} 涉及网络服务/外部连接，需要人工审批"
    return None


def _executable_assessment(executable: str, argv: list[str]) -> tuple[str, str]:
    if executable in DENIED_EXECUTABLES:
        return DENY, f"禁止执行系统管理命令: {executable}"
    if executable in SHELL_EXECUTABLES:
        if any(token in {"-c", "--command"} for token in argv[1:]):
            return DENY, "禁止通过 shell -c 执行任意命令"
        return ASK_USER, f"启动子 shell ({executable}) 需要人工审批"
    if executable in ASK_EXECUTABLES:
        return ASK_USER, f"{executable} 属于高风险/外部访问命令，需要人工审批"
    if executable == "git":
        return _git_assessment(argv)
    if executable == "sed":
        return _sed_assessment(argv)
    if executable in {"awk", "gawk", "mawk"}:
        return _awk_assessment(argv)
    if executable in {"find", "fd"}:
        return _find_assessment(argv)
    network = _network_subcommand_assessment(executable, argv)
    if network is not None:
        return network
    python_module = _python_module_assessment(executable, argv)
    if python_module is not None:
        return python_module
    if executable in READ_ONLY_EXECUTABLES:
        return ALLOW, f"只读命令 {executable} 已通过白名单"
    if executable in WRITE_EXECUTABLES:
        return SAFE_EXEC, f"工作区命令 {executable} 已通过白名单"
    return ASK_USER, f"未列入白名单的可执行文件: {executable}，需要人工审批"


def assess_command(
    command: str,
    workspace_root: Path | str,
    *,
    policy_mode: str = "strict",
) -> CommandAssessment:
    """Classify a shell command without executing it."""
    root = Path(workspace_root).resolve()
    raw = command.strip()
    if not raw:
        return CommandAssessment([], "", DENY, "命令为空", policy_mode=policy_mode)
    if "\x00" in raw:
        return CommandAssessment([], "", DENY, "命令包含非法空字符", policy_mode=policy_mode)
    if len(raw) > MAX_COMMAND_LENGTH:
        return CommandAssessment([], "", DENY, "命令长度超过限制", policy_mode=policy_mode)

    if FORK_BOMB.search(raw):
        return CommandAssessment([], "", DENY, "检测到 fork bomb，已阻断", policy_mode=policy_mode)

    uses_shell = False
    try:
        uses_shell = _has_shell_syntax(raw)
    except CommandPolicyError as exc:
        return CommandAssessment([], "", DENY, str(exc), policy_mode=policy_mode)

    mode = (policy_mode or "strict").lower()
    if uses_shell:
        if mode not in {"permissive", "full_access"}:
            return CommandAssessment(
                [],
                "",
                DENY,
                "严格命令模式禁止 shell 操作符（; & | < > $() 等），"
                "请拆分为单条命令，或使用 file_* / python_eval 工具。",
                uses_shell_syntax=True,
                policy_mode=policy_mode,
            )
        # Permissive mode requires approval; full access runs directly on the
        # host but still keeps catastrophic-command protection.
        for denied in DENIED_PATHS:
            if denied in raw:
                return CommandAssessment(
                    [],
                    "",
                    DENY,
                    f"系统保护拦截：命令中包含受限路径 {denied}",
                    uses_shell_syntax=True,
                    policy_mode=policy_mode,
                )
        for pattern in ("rm -rf /", "mkfs", "dd if=", ":(){", "> /dev/"):
            if pattern in raw:
                return CommandAssessment(
                    [],
                    "",
                    DENY,
                    f"检测到高危命令片段: {pattern}",
                    uses_shell_syntax=True,
                    policy_mode=policy_mode,
                )
        return CommandAssessment(
            ["/bin/sh", "-c", raw],
            "sh",
            ALLOW if mode == "full_access" else ASK_USER,
            (
                "完全访问模式：shell 命令在主机环境执行。"
                if mode == "full_access"
                else "命令包含 shell 操作符，permissive 模式需要人工审批并在沙箱中执行。"
            ),
            uses_shell_syntax=True,
            policy_mode=policy_mode,
        )

    try:
        argv = _parse_argv(raw)
    except CommandPolicyError as exc:
        return CommandAssessment([], "", DENY, str(exc), policy_mode=policy_mode)

    executable = Path(argv[0]).name
    if not executable:
        return CommandAssessment(argv, "", DENY, "无法识别可执行文件", policy_mode=policy_mode)
    if argv[0].startswith("~"):
        return CommandAssessment(argv, executable, DENY, "不允许主目录展开", policy_mode=policy_mode)
    if _is_denied_path_token(argv[0]):
        # Executing a binary directly from /etc, /proc, ... is never allowed.
        return CommandAssessment(
            argv, executable, DENY, f"禁止执行受限路径下的程序: {argv[0]}", policy_mode=policy_mode
        )

    base_risk, reason = _executable_assessment(executable, argv)
    if base_risk == DENY:
        return CommandAssessment(argv, executable, DENY, reason, policy_mode=policy_mode)

    if mode == "full_access":
        for argument in argv[1:]:
            if _is_denied_path_token(argument):
                return CommandAssessment(
                    argv,
                    executable,
                    DENY,
                    f"完全访问模式仍禁止操作系统保护路径: {argument}",
                    policy_mode=policy_mode,
                )
        return CommandAssessment(
            argv,
            executable,
            ALLOW,
            "完全访问模式：命令在主机环境执行",
            policy_mode=policy_mode,
        )

    skip_code = executable in {
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "php",
        "java",
        "javac",
        "rustc",
    }
    risk, argument_reason = _validate_arguments(
        argv,
        workspace_root=root,
        base_risk=base_risk,
        skip_code_arguments=skip_code,
    )
    if risk == DENY:
        return CommandAssessment(argv, executable, DENY, argument_reason, policy_mode=policy_mode)
    if argument_reason:
        reason = argument_reason
    return CommandAssessment(argv, executable, risk, reason, policy_mode=policy_mode)


def validate_sql_readonly(query: str) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for the read-only SQL policy."""
    stripped = query.strip()
    if not stripped:
        return False, "SQL 查询为空"
    if not stripped.upper().startswith("SELECT"):
        return False, "仅允许执行只读 SELECT 查询"
    if SQL_DANGEROUS.search(stripped):
        return False, "SQL 只能包含单条 SELECT 语句，禁止注释、PRAGMA、ATTACH 等"
    return True, "只读 SQL 查询已放行"
