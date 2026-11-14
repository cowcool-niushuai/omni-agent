import tools


def test_file_read_rejects_escape() -> None:
    result = tools.tool_file_read("../../etc/passwd")
    assert "非法路径" in result


def test_file_write_rejects_escape(tmp_path) -> None:
    result = tools.tool_file_write("../escaped.txt", "boom")
    assert "非法路径" in result
    assert not (tmp_path / "escaped.txt").exists()


def test_file_list_rejects_absolute() -> None:
    result = tools.tool_file_list("/etc")
    assert "非法路径" in result
