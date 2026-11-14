"""Path containment helpers.

Rules:
- Relative paths are resolved against a trusted root.
- Absolute paths and ``..`` escapes are rejected, never silently re-mapped.
- Symlinks are followed before the containment check, so a link inside the
  root cannot point outside it.
"""

from __future__ import annotations

from pathlib import Path


class PathSecurityError(ValueError):
    """Raised when a path would escape its allowed root."""


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and require it to be inside ``root``."""
    resolved_root = root.resolve()
    resolved = candidate.resolve(strict=False)
    if resolved != resolved_root and not resolved.is_relative_to(resolved_root):
        raise PathSecurityError(f"path escapes allowed root: {candidate}")
    return resolved


def safe_join(root: Path, relative: str) -> Path:
    """Join a user-supplied relative path onto ``root`` safely."""
    rel = Path(relative)
    if rel.is_absolute():
        raise PathSecurityError(f"absolute paths are not allowed: {relative}")
    return ensure_within(root, root / rel)
