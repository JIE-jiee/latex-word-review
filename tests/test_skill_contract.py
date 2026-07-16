from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "latex-word-review"
PLUGIN_ROOT = ROOT / "plugins" / "latex-word-review"
PLUGIN_SKILL_ROOT = PLUGIN_ROOT / "skills" / "latex-word-review"
MARKETPLACE_PATH = ROOT / ".agents" / "plugins" / "marketplace.json"
SYNCED_SKILL_FILES = ("SKILL.md", "agents/openai.yaml")


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


def test_skill_prefers_resumable_workflow_without_crossing_approval_gates() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    for command in (
        "workflow init <source> <run-root> --main main.tex",
        "workflow export <run-root>",
        "workflow receive <run-root> <returned.docx>",
        "workflow status <run-root>",
        "workflow clean <run-root>",
        "workflow clean <run-root> --execute",
    ):
        assert f"`{command}`" in text
    assert "verify it against the sealed export baseline before ingest" in text
    assert "dry-run and must not remove anything without `--execute`" in text
    assert "does not approve changes or apply them to LaTeX" in text
    assert "Never infer or automatically cross either gate" in text
    assert "granular recovery" in text


def test_plugin_embeds_the_canonical_skill_byte_for_byte() -> None:
    canonical_files = {
        path.relative_to(SKILL_ROOT).as_posix() for path in SKILL_ROOT.rglob("*") if path.is_file()
    }
    embedded_files = {
        path.relative_to(PLUGIN_SKILL_ROOT).as_posix()
        for path in PLUGIN_SKILL_ROOT.rglob("*")
        if path.is_file()
    }
    assert canonical_files == embedded_files == set(SYNCED_SKILL_FILES)
    for relative in SYNCED_SKILL_FILES:
        assert (SKILL_ROOT / relative).read_bytes() == (PLUGIN_SKILL_ROOT / relative).read_bytes()


def test_plugin_manifest_and_layout_are_distribution_ready() -> None:
    files = {
        path.relative_to(PLUGIN_ROOT).as_posix()
        for path in PLUGIN_ROOT.rglob("*")
        if path.is_file()
    }
    assert files == {
        ".codex-plugin/plugin.json",
        "skills/latex-word-review/SKILL.md",
        "skills/latex-word-review/agents/openai.yaml",
    }

    manifest_path = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == PLUGIN_ROOT.name == "latex-word-review"
    assert re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", manifest["version"])
    assert manifest["skills"] == "./skills/"
    assert manifest["license"] == "Apache-2.0"
    assert set(manifest).isdisjoint({"apps", "hooks", "mcpServers"})
    assert manifest["author"]["name"] == "LaTeX Word Review contributors"
    assert {
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "defaultPrompt",
    } <= set(manifest["interface"])
    prompts = manifest["interface"]["defaultPrompt"]
    assert isinstance(prompts, list) and 1 <= len(prompts) <= 3
    assert all(isinstance(prompt, str) and len(prompt) <= 128 for prompt in prompts)
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert "[TODO:" not in manifest_text
    assert "C:\\Users\\" not in manifest_text


def test_repo_marketplace_points_to_the_packaged_plugin() -> None:
    marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
    assert marketplace["name"] == "personal"
    assert marketplace["interface"]["displayName"] == "Personal"
    matching = [
        entry for entry in marketplace["plugins"] if entry.get("name") == "latex-word-review"
    ]
    assert matching == [
        {
            "name": "latex-word-review",
            "source": {"source": "local", "path": "./plugins/latex-word-review"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }
    ]


def test_sdist_manifest_includes_canonical_and_plugin_distribution_files() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    included = set(pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])
    assert {"/.agents", "/plugins", "/skills"} <= included
