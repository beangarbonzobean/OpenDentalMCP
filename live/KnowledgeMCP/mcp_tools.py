"""
KnowledgeMCP tools: expose Claude Code memory files and skills over MCP
so any client (Claude.ai, Cowork, API/SDK, other machines) can read them.

Read-only for skills; read + write for memory files.
All writes are constrained to the configured memory_dir via path resolution.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]*\.md$")
VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference"}


def _parse_frontmatter(text: str) -> Dict[str, str]:
    """Parse YAML-ish frontmatter delimited by --- lines. Returns {} if none."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip()
    fm: Dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


class KnowledgeTools:
    """Tools for reading Claude Code memory files and skills."""

    def __init__(self, memory_dir: str, skill_roots: List[str]):
        self.memory_dir = Path(memory_dir).resolve()
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.skill_roots = [Path(p) for p in skill_roots if p]

    # -----------------------------
    # Memory
    # -----------------------------

    def _memory_path(self, name: str) -> Path:
        """Resolve a memory filename safely inside memory_dir."""
        if not SAFE_NAME_RE.match(name):
            raise ValueError(
                f"Invalid memory name: {name!r}. "
                "Must match [A-Za-z0-9_][A-Za-z0-9_-]*.md"
            )
        p = (self.memory_dir / name).resolve()
        # Enforce containment
        try:
            p.relative_to(self.memory_dir)
        except ValueError:
            raise ValueError(f"Path escapes memory_dir: {name!r}")
        return p

    def list_memories(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for md in sorted(self.memory_dir.glob("*.md")):
            if md.name == "MEMORY.md":
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception as e:
                out.append({"name": md.name, "error": str(e)})
                continue
            fm = _parse_frontmatter(text)
            out.append({
                "name": md.name,
                "title": fm.get("name", ""),
                "type": fm.get("type", ""),
                "description": fm.get("description", ""),
            })
        return out

    def read_memory(self, name: str) -> Dict[str, Any]:
        p = self._memory_path(name)
        if not p.exists():
            raise FileNotFoundError(f"Memory not found: {name}")
        text = p.read_text(encoding="utf-8")
        return {
            "name": name,
            "path": str(p),
            "content": text,
            "frontmatter": _parse_frontmatter(text),
        }

    def read_memory_index(self) -> Dict[str, Any]:
        p = self.memory_dir / "MEMORY.md"
        if not p.exists():
            return {"name": "MEMORY.md", "content": "", "exists": False}
        return {
            "name": "MEMORY.md",
            "path": str(p),
            "content": p.read_text(encoding="utf-8"),
            "exists": True,
        }

    def save_memory(
        self,
        name: str,
        type: str,
        description: str,
        content: str,
        title: Optional[str] = None,
        index_line: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write a memory file with frontmatter and optionally update MEMORY.md.

        `content` is the memory body (without frontmatter — this function adds it).
        `index_line` is an optional one-line entry to append to MEMORY.md under the
        appropriate section. Format: "- [Title](file.md) — one-line hook"
        """
        if type not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"Invalid type: {type!r}. Must be one of {sorted(VALID_MEMORY_TYPES)}"
            )
        if not description or len(description) > 300:
            raise ValueError("description is required and must be <=300 chars")

        p = self._memory_path(name)
        display_title = title or name.removesuffix(".md").replace("_", " ")

        body = (
            f"---\n"
            f"name: {display_title}\n"
            f"description: {description}\n"
            f"type: {type}\n"
            f"---\n\n"
            f"{content.rstrip()}\n"
        )
        p.write_text(body, encoding="utf-8")

        index_updated = False
        if index_line:
            index_path = self.memory_dir / "MEMORY.md"
            if index_path.exists():
                existing = index_path.read_text(encoding="utf-8")
            else:
                existing = "# Memory Index\n"
            if name not in existing:
                index_path.write_text(
                    existing.rstrip() + "\n" + index_line.rstrip() + "\n",
                    encoding="utf-8",
                )
                index_updated = True

        return {
            "name": name,
            "path": str(p),
            "bytes_written": len(body),
            "index_updated": index_updated,
        }

    # -----------------------------
    # Skills
    # -----------------------------

    def _iter_skill_files(self):
        """Yield (namespace, skill_dir_path) for every SKILL.md found under skill_roots.

        namespace rules:
          - Under AppData .../skills-plugin/*/*/skills/<name>/SKILL.md -> "anthropic-skills:<name>"
          - Under .claude/scheduled-tasks/<name>/SKILL.md -> "scheduled-tasks:<name>"
          - Under .claude/skills/<name>/SKILL.md -> "<name>"
          - Fallback: "<parent_dir_name>"
        """
        seen = set()
        for root in self.skill_roots:
            if not root.exists():
                continue
            for skill_md in root.rglob("SKILL.md"):
                skill_dir = skill_md.parent
                key = str(skill_dir.resolve())
                if key in seen:
                    continue
                seen.add(key)
                parts = skill_dir.parts
                name = skill_dir.name
                lower = [p.lower() for p in parts]
                if "skills-plugin" in lower:
                    namespaced = f"anthropic-skills:{name}"
                elif "scheduled-tasks" in lower:
                    namespaced = f"scheduled-tasks:{name}"
                else:
                    namespaced = name
                yield namespaced, skill_dir

    def list_skills(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for namespaced, skill_dir in self._iter_skill_files():
            try:
                text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            except Exception as e:
                out.append({"name": namespaced, "error": str(e)})
                continue
            fm = _parse_frontmatter(text)
            out.append({
                "name": namespaced,
                "title": fm.get("name", namespaced.split(":")[-1]),
                "description": fm.get("description", ""),
                "path": str(skill_dir),
            })
        out.sort(key=lambda r: r["name"])
        return out

    def read_skill(self, name: str) -> Dict[str, Any]:
        """Return the SKILL.md content for a skill. Accepts namespaced name or bare name."""
        target = name.strip()
        for namespaced, skill_dir in self._iter_skill_files():
            if namespaced == target or namespaced.split(":")[-1] == target:
                text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
                return {
                    "name": namespaced,
                    "path": str(skill_dir),
                    "content": text,
                    "frontmatter": _parse_frontmatter(text),
                }
        raise FileNotFoundError(f"Skill not found: {name}")

    # -----------------------------
    # Convenience
    # -----------------------------

    def get_payroll_workflow(self) -> Dict[str, Any]:
        return self.read_memory("project_payroll_workflow.md")

    # -----------------------------
    # MCP plumbing
    # -----------------------------

    def list_tools(self) -> List[Dict]:
        return [
            {
                "name": "list_memories",
                "description": (
                    "List all Claude Code memory files for this project (name, type, description). "
                    "Memory files capture user preferences, project context, feedback, and references "
                    "that persist across conversations."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "read_memory",
                "description": (
                    "Read a single memory file by filename (e.g. 'project_payroll_workflow.md'). "
                    "Returns raw markdown content and parsed frontmatter."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Memory filename, e.g. 'user_ben.md'",
                        }
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "read_memory_index",
                "description": "Read the MEMORY.md index file listing all memories.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "save_memory",
                "description": (
                    "Create or overwrite a memory file. Writes YAML frontmatter automatically. "
                    "Optionally appends an index line to MEMORY.md. "
                    "Use this to persist new context from any client so Claude Code sessions pick it up."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Filename ending in .md, e.g. 'project_new_feature.md'",
                        },
                        "type": {
                            "type": "string",
                            "description": "Memory type",
                            "enum": ["user", "feedback", "project", "reference"],
                        },
                        "description": {
                            "type": "string",
                            "description": "One-line description used for relevance matching in future sessions",
                        },
                        "content": {
                            "type": "string",
                            "description": "Memory body (markdown). Do NOT include frontmatter — it is added automatically.",
                        },
                        "title": {
                            "type": "string",
                            "description": "Optional display title (frontmatter 'name'). Defaults to filename.",
                        },
                        "index_line": {
                            "type": "string",
                            "description": "Optional line to append to MEMORY.md, e.g. '- [Title](file.md) — hook'",
                        },
                    },
                    "required": ["name", "type", "description", "content"],
                },
            },
            {
                "name": "list_skills",
                "description": (
                    "List all skills discoverable to Claude Code on this machine: "
                    "bundled anthropic-skills, scheduled-task skills, and user skills. "
                    "Returns namespaced name, title, description, and path."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "read_skill",
                "description": (
                    "Read a skill's SKILL.md content. Accepts the namespaced name "
                    "(e.g. 'anthropic-skills:payroll-processor') or the bare skill name."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Skill name, namespaced or bare",
                        }
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "get_payroll_workflow",
                "description": (
                    "Convenience: return the project_payroll_workflow.md memory directly. "
                    "Captures biweekly payroll format, CA daily OT rules, rounding, manual columns."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    def call_tool(self, name: str, arguments: Dict) -> Any:
        if name == "list_memories":
            return self.list_memories()
        if name == "read_memory":
            return self.read_memory(arguments["name"])
        if name == "read_memory_index":
            return self.read_memory_index()
        if name == "save_memory":
            return self.save_memory(
                name=arguments["name"],
                type=arguments["type"],
                description=arguments["description"],
                content=arguments["content"],
                title=arguments.get("title"),
                index_line=arguments.get("index_line"),
            )
        if name == "list_skills":
            return self.list_skills()
        if name == "read_skill":
            return self.read_skill(arguments["name"])
        if name == "get_payroll_workflow":
            return self.get_payroll_workflow()
        raise ValueError(f"Unknown tool: {name}")
