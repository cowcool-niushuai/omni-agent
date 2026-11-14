import pytest

import skills_manager


@pytest.mark.parametrize(
    "skill_id",
    ["../evil", "a/b", "UPPER", "", "with space", "x" * 65],
)
def test_rejects_unsafe_skill_ids(skill_id: str) -> None:
    with pytest.raises(ValueError):
        skills_manager.validate_skill_id(skill_id)


def test_saves_valid_skill(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(skills_manager, "SKILLS_DIR", str(tmp_path))
    skills_manager.save_skill("valid_skill-1", "N", "D", "C", ["a"])

    skill_file = tmp_path / "valid_skill-1" / "SKILL.md"
    assert skill_file.exists()
    assert "name: N" in skill_file.read_text(encoding="utf-8")
