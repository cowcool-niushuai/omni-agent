import base64
from pathlib import Path

import pytest

from uploads import UploadError, save_attachment


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def test_save_attachment_is_workspace_contained(tmp_path: Path) -> None:
    result = save_attachment(
        tmp_path,
        "../../简历.docx",
        _b64(b"fake docx"),
        session_id="../../escape",
    )

    assert result["name"] == "简历.docx"
    assert result["path"].startswith(".uploads/")
    target = tmp_path / result["path"]
    assert target.exists()
    assert target.read_bytes() == b"fake docx"
    assert result["size"] == 9
    assert target.stat().st_mode & 0o777 == 0o600


def test_save_attachment_rejects_bad_and_empty_data(tmp_path: Path) -> None:
    with pytest.raises(UploadError, match="base64"):
        save_attachment(tmp_path, "a.txt", "not base64!")
    with pytest.raises(UploadError, match="为空"):
        save_attachment(tmp_path, "a.txt", "")


def test_save_attachment_enforces_size_limit(tmp_path: Path) -> None:
    with pytest.raises(UploadError, match="大小限制"):
        save_attachment(
            tmp_path,
            "large.bin",
            _b64(b"0123456789"),
            max_bytes=5,
        )
