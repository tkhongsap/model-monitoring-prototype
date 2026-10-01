"""Repository documentation contract (mirrors the playbook's check_docs rules)."""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ["README.md", "CLAUDE.md", "AGENTS.md", "CHANGELOG.md", "DEVLOG.md", "TESTING.md"]

# A machine-local path is a home directory with a user segment (e.g. "/Users/<name>/")
# or a file URL with a path. The rule text itself may name the markers without tripping.
LOCAL_PATH = re.compile(r"/Users/[A-Za-z0-9_.-]+(?:/|\b)|file://[A-Za-z0-9/~]")


def _md_files() -> list[Path]:
    skip = {".migration-backup", "node_modules", ".venv", "dist", ".git", ".pytest_cache"}
    return [p for p in ROOT.rglob("*.md")
            if not any(part in skip for part in p.parts)]


def test_required_documents_exist():
    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    assert not missing, f"missing playbook documents: {missing}"


def test_claude_md_is_navigational():
    lines = (ROOT / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 120, f"CLAUDE.md has {len(lines)} lines; budget is 120"


def test_markdown_hygiene():
    problems = []
    for path in _md_files():
        text = path.read_text(encoding="utf-8")
        h1 = [l for l in text.splitlines() if l.startswith("# ")]
        if path.name in REQUIRED and len(h1) != 1:
            problems.append(f"{path.relative_to(ROOT)}: expected one H1, found {len(h1)}")
        if not text.endswith("\n"):
            problems.append(f"{path.relative_to(ROOT)}: missing final newline")
        if LOCAL_PATH.search(text):
            problems.append(f"{path.relative_to(ROOT)}: machine-local path")
    assert not problems, "\n".join(problems)


def test_internal_links_resolve():
    broken = []
    link = re.compile(r"\]\((?!https?://|mailto:|#)([^)#]+)(?:#[^)]*)?\)")
    for path in _md_files():
        for target in link.findall(path.read_text(encoding="utf-8")):
            if not (path.parent / target).exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not broken, "\n".join(broken)


def test_post_merge_hook_never_pushes_schema():
    hook = (ROOT / "scripts" / "post-merge.sh").read_text(encoding="utf-8")
    assert "db push" not in hook and "drizzle-kit" not in hook
