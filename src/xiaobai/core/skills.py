"""Skill context loader for non-Claude providers."""

from __future__ import annotations

from pathlib import Path


def build_skills_context(
    *,
    skills_root: Path | None = None,
    max_skills: int = 12,
    max_chars_per_skill: int = 1200,
    max_total_chars: int = 10000,
) -> str:
    """Load `.claude/skills/**/SKILL.md` snippets for prompt mirroring."""
    root = skills_root or Path(".claude/skills")
    if not root.is_dir():
        return ""

    skill_files = sorted(root.glob("**/SKILL.md"))
    if not skill_files:
        return ""

    lines: list[str] = []
    total_chars = 0
    count = 0
    for file_path in skill_files:
        if count >= max_skills or total_chars >= max_total_chars:
            break
        text = file_path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        snippet = text[:max_chars_per_skill].strip()
        block = f"### {file_path.parent.name}\n{snippet}\n"
        if total_chars + len(block) > max_total_chars:
            break
        lines.append(block)
        total_chars += len(block)
        count += 1

    if not lines:
        return ""
    return (
        "Skill mirror context (from .claude/skills). "
        "Follow these as behavioral constraints when relevant:\n\n"
        + "\n".join(lines)
    )
