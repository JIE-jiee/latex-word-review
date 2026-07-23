from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "src" / "latex_word_review" / "__about__.py"
CURRENT_PUBLIC_FILES = (
    ROOT / "README.md",
    ROOT / "README.en.md",
    ROOT / "README.ja.md",
    ROOT / "SECURITY.md",
    ROOT / "docs" / "quick-start-windows.md",
    ROOT / "docs" / "guide.zh-CN.md",
    ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
)
FILES_REQUIRING_EXPLICIT_VERSION = (
    ROOT / "README.md",
    ROOT / "README.en.md",
    ROOT / "SECURITY.md",
    ROOT / "docs" / "quick-start-windows.md",
    ROOT / "docs" / "guide.zh-CN.md",
    ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
)
PRERELEASE_VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+(?:a|b|rc)\d+\b")


def _project_version() -> str:
    source = VERSION_FILE.read_text(encoding="utf-8")
    matches: list[str] = re.findall(
        r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
        source,
        flags=re.MULTILINE,
    )
    assert len(matches) == 1
    return matches[0]


def test_current_public_entrypoints_share_the_package_version() -> None:
    version = _project_version()

    for path in CURRENT_PUBLIC_FILES:
        text = path.read_text(encoding="utf-8")
        mentioned = set(PRERELEASE_VERSION_RE.findall(text))
        assert mentioned <= {version}, f"{path.relative_to(ROOT)} contains {sorted(mentioned)}"

    for path in FILES_REQUIRING_EXPLICIT_VERSION:
        assert version in path.read_text(encoding="utf-8"), path.relative_to(ROOT)


def test_public_guides_put_double_click_before_developer_setup() -> None:
    source_zip = "https://github.com/JIE-jiee/latex-word-review/archive/refs/heads/main.zip"
    quick_start = (ROOT / "docs" / "quick-start-windows.md").read_text(encoding="utf-8")
    chinese_guide = (ROOT / "docs" / "guide.zh-CN.md").read_text(encoding="utf-8")

    assert source_zip in quick_start
    assert source_zip in chinese_guide
    assert quick_start.index("## Start from the GitHub source ZIP") < quick_start.index(
        "## Step 1: choose the project"
    )
    assert quick_start.index("## Step 1: choose the project") < quick_start.index(
        "## Advanced setup and maintainer notes"
    )
    assert chinese_guide.index("## 2. 下载源码 ZIP，双击启动") < chinese_guide.index(
        "## 5. 第一步：选择 `main.tex`"
    )
    assert chinese_guide.index("## 5. 第一步：选择 `main.tex`") < chinese_guide.index(
        "## 13. 高级启动方式与未来发布计划"
    )
