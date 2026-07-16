from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "latex-word-review"


def test_skill_frontmatter_is_complete_and_portable() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---\n", 2)
    keys = {
        line.split(":", 1)[0]
        for line in frontmatter.splitlines()
        if line and not line.startswith((" ", "\t"))
    }
    assert keys == {"name", "description"}
    assert "name: latex-word-review" in frontmatter
    assert "Use when Codex" in frontmatter
    assert "[TODO" not in text
    assert "C:\\Users\\" not in text
    assert "approval" in body.lower()
    assert "do not reimplement" in body.lower()


def test_skill_is_a_thin_cli_orchestrator() -> None:
    files = {
        path.relative_to(SKILL_ROOT).as_posix() for path in SKILL_ROOT.rglob("*") if path.is_file()
    }
    assert files == {"SKILL.md", "agents/openai.yaml"}
    assert not any(path.suffix.lower() in {".py", ".ps1", ".sh"} for path in SKILL_ROOT.rglob("*"))

    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    for command in (
        "doctor",
        "snapshot",
        "export",
        "archive",
        "ingest",
        "approve",
        "plan",
        "apply",
        "verify",
        "ledger",
        "run-manifest",
        "bundle",
        "verify-bundle",
    ):
        assert f"`{command}" in text


def test_skill_ui_metadata_is_utf8_and_invocable() -> None:
    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    assert 'display_name: "LaTeX Word Review"' in metadata
    assert 'short_description: "Safely orchestrate auditable LaTeX-Word review"' in metadata
    assert "$latex-word-review" in metadata
    assert "\ufffd" not in metadata
