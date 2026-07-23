"""SPEC.md <-> structured-tree round-trip.

The migration bridge: parse an existing ``SPEC.md`` into projects/epics/tasks and
render the database back to a ``SPEC.md`` mirror. The renderer emits a canonical
form that re-parses to the same normalized tree (see ``normalize`` / the PORT-4
round-trip test).

The parser is deliberately tolerant of the real-world dialects observed across
repos: bold-wrapped ``**KEY · Title**``, trailing ``(BE, DONE, reviewer PASS)``
metadata, ``_Proof: <cmd>_`` lines, indented continuation lines, and the checkbox
legend ``[ ] [~] [x] [-]``.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# checkbox glyph -> status
_CHECKBOX = {" ": "todo", "~": "in_progress", "x": "done", "X": "done", "-": "superseded"}
_STATUS_BY_GLYPH = {"todo": " ", "in_progress": "~", "done": "x", "superseded": "-",
                    "cancelled": "-", "blocked": " ", "deferred": " "}

_PRIORITIES = {"P0", "P1", "P2", "P3"}
_COMPONENTS = {"FE", "BE", "ML", "AWS", "INFRA", "DOCS", "OPS", "INVESTIGATION"}
# inline status keywords -> canonical status (override the checkbox-derived one)
_STATUS_WORDS = {
    "done": "done", "complete": "done", "completed": "done",
    "in progress": "in_progress", "wip": "in_progress",
    "blocked": "blocked", "deferred": "deferred",
    "superseded": "superseded", "cancelled": "cancelled", "canceled": "cancelled",
}

_TASK_RE = re.compile(r"^\s*[-*]\s*\[(?P<box>[ xX~\-])\]\s*(?P<body>.*)$")
_KEY_RE = re.compile(r"^([A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)\s*(?:·|:|—|-)\s+(?P<rest>.*)$")
_PROOF_RE = re.compile(r"_Proof:\s*(?P<cmd>.*)_", re.IGNORECASE)
_PAREN_RE = re.compile(r"\(([^()]*)\)")


# Valid enum domains, mirrored from ``app.models.TaskStatus`` / ``Priority`` so the
# per-task import validator (used by BOTH storage adapters) applies identical rules
# without importing the ORM layer. Keep in sync with the enums.
VALID_STATUSES = frozenset({
    "todo", "in_progress", "blocked", "deferred", "done", "superseded", "cancelled",
})
VALID_PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})


class TaskValidationError(ValueError):
    """A single parsed task is not importable (empty title/key, bad enum). Raised
    per task so the import handler can report it in ``failed`` rather than 500 the
    whole request."""


def validate_parsed_task(pt: "ParsedTask") -> None:
    """Validate one parsed task before it is written. Shared by the Postgres and
    DynamoDB adapters so a malformed task fails identically on both backends
    (parity). Raises ``TaskValidationError`` on the first problem found."""
    if not (pt.key or "").strip():
        raise TaskValidationError("task key is empty")
    if not (pt.title or "").strip():
        raise TaskValidationError(f"task {pt.key!r} has an empty title")
    if pt.status not in VALID_STATUSES:
        raise TaskValidationError(
            f"task {pt.key!r} has invalid status {pt.status!r}"
        )
    if pt.priority is not None and pt.priority not in VALID_PRIORITIES:
        raise TaskValidationError(
            f"task {pt.key!r} has invalid priority {pt.priority!r}"
        )


@dataclass
class ParsedTask:
    key: str | None
    title: str
    description: str | None = None
    status: str = "todo"
    priority: str | None = None
    component: str | None = None
    proof_cmd: str | None = None
    tags: list[str] = field(default_factory=list)
    epic_key: str | None = None
    section: str = "backlog"
    position: float = 0.0


@dataclass
class ParsedEpic:
    key: str
    title: str
    section: str = "backlog"
    position: float = 0.0


@dataclass
class ParsedSpec:
    title: str | None = None
    epics: dict[str, ParsedEpic] = field(default_factory=dict)
    tasks: list[ParsedTask] = field(default_factory=list)


def _section_of(heading: str) -> str:
    h = heading.lower()
    if "complete" in h or "done" in h:
        return "completed"
    if "in progress" in h or "in-progress" in h:
        return "in_progress"
    if "to do" in h or "to-do" in h or "todo" in h:
        return "to_do"
    return "backlog"


def _epic_key_from_heading(heading: str) -> tuple[str, str] | None:
    """`### EPIC FOUND — Foundations` or `### EPIC: NAME - desc` -> (key, title)."""
    m = re.match(r"EPIC[:\s]+([A-Za-z][\w-]*)\s*(?:[—-]\s*(.*))?$", heading.strip())
    if not m:
        return None
    return m.group(1).upper(), (m.group(2) or m.group(1)).strip()


def _synth_key(title: str) -> str:
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:6]
    return f"AUTO-{digest}"


def _is_meta_token(token: str) -> bool:
    up = token.strip().upper()
    return up in _PRIORITIES or up in _COMPONENTS or token.strip().lower() in _STATUS_WORDS


def _strip_meta_suffix(text: str, task: ParsedTask) -> str:
    """Pull a canonical trailing ``— BE, P0, blocked`` metadata segment off the
    title (the form the renderer emits). Only strips when every token classifies
    as metadata, so prose containing an em dash is left intact."""
    if " — " not in text:
        return text
    head, _, tail = text.rpartition(" — ")
    parts = [p.strip() for p in tail.split(",") if p.strip()]
    if parts and all(_is_meta_token(p) for p in parts):
        for p in parts:
            _classify_meta(p, task)
        return head.strip()
    return text


def _classify_meta(token: str, task: ParsedTask) -> None:
    t = token.strip()
    if not t:
        return
    up = t.upper()
    low = t.lower()
    if up in _PRIORITIES:
        task.priority = up
    elif up in _COMPONENTS:
        task.component = up if up not in {"INFRA", "DOCS", "OPS", "INVESTIGATION"} else up.capitalize()
    elif low in _STATUS_WORDS:
        task.status = _STATUS_WORDS[low]
    else:
        task.tags.append(t)


def _parse_task_body(box: str, body: str) -> ParsedTask:
    status = _CHECKBOX.get(box, "todo")
    task = ParsedTask(key=None, title="", status=status)

    # Strip markdown bold; pull out the proof line.
    text = body.replace("**", "").strip()
    pm = _PROOF_RE.search(text)
    if pm:
        task.proof_cmd = pm.group("cmd").strip()
        text = _PROOF_RE.sub("", text).strip()

    # Pull parenthetical metadata groups (classify; the first that is all-meta
    # is removed from the title, others left in place if they look like prose).
    for grp in _PAREN_RE.findall(text):
        parts = [p.strip() for p in grp.split(",")]
        if all(
            p.upper() in _PRIORITIES
            or p.upper() in _COMPONENTS
            or p.lower() in _STATUS_WORDS
            for p in parts
        ):
            for p in parts:
                _classify_meta(p, task)
            text = text.replace(f"({grp})", "", 1).strip()

    # Key + title.
    km = _KEY_RE.match(text)
    if km:
        task.key = text[: km.start(1) + len(km.group(1))].strip()
        rest = km.group("rest").strip()
    else:
        rest = text

    # Pull a canonical trailing metadata segment ("— BE, P0") off the title.
    rest = _strip_meta_suffix(rest, task)

    # Title is the first sentence/line; the remainder is description.
    rest = rest.strip(" .—-")
    if "\n" in rest:
        first, _, remainder = rest.partition("\n")
        task.title = first.strip()
        task.description = remainder.strip() or None
    else:
        task.title = rest
    if not task.key:
        task.key = _synth_key(task.title or body)
    return task


def parse_spec(text: str) -> ParsedSpec:
    spec = ParsedSpec()
    section = "backlog"
    epic_key: str | None = None
    epic_pos = 0.0
    task_pos = 0.0
    current: ParsedTask | None = None
    lines = text.splitlines()

    def flush(task: ParsedTask | None):
        if task is not None:
            spec.tasks.append(task)

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        if line.startswith("# ") and spec.title is None:
            spec.title = line[2:].strip()
            continue
        if line.startswith("## "):
            flush(current)
            current = None
            section = _section_of(line[3:])
            epic_key = None
            continue
        if line.startswith("### "):
            flush(current)
            current = None
            parsed = _epic_key_from_heading(line[4:])
            if parsed:
                epic_pos += 1.0
                epic_key, title = parsed
                spec.epics[epic_key] = ParsedEpic(
                    key=epic_key, title=title, section=section, position=epic_pos
                )
            else:
                epic_key = None
            continue

        m = _TASK_RE.match(line)
        if m:
            flush(current)
            task_pos += 1.0
            current = _parse_task_body(m.group("box"), m.group("body"))
            current.section = section
            current.epic_key = epic_key
            current.position = task_pos
        elif current is not None and (raw.startswith(" ") or raw.startswith("\t")):
            # indented continuation line
            cont = raw.strip().replace("**", "")
            pm = _PROOF_RE.search(cont)
            if pm and not current.proof_cmd:
                current.proof_cmd = pm.group("cmd").strip()
            else:
                extra = _PROOF_RE.sub("", cont).strip()
                if extra:
                    current.description = (
                        f"{current.description}\n{extra}" if current.description else extra
                    )
    flush(current)
    return spec


# --------------------------------------------------------------------------- #
# Rendering (DB tree -> canonical SPEC.md)
# --------------------------------------------------------------------------- #
_SECTION_HEADINGS = [
    ("in_progress", "In Progress"),
    ("backlog", "Backlog"),
    ("to_do", "To Do"),
    ("completed", "Completed"),
]


def render_spec(title: str, epics: list, tasks: list) -> str:
    """Render a project to canonical SPEC.md.

    ``epics`` is a list of objects with .key/.title/.section/.position.
    ``tasks`` is a list of objects with the task fields plus ``.epic_key`` and
    ``.section`` resolved by the caller.
    """
    out: list[str] = [f"# {title}", ""]
    out.append("> Checkbox legend: `[ ]` todo · `[~]` in progress · `[x]` done · "
               "`[-]` superseded/cancelled.")
    out.append("")

    epics_by_key = {e.key: e for e in epics}

    for sec_key, sec_label in _SECTION_HEADINGS:
        sec_tasks = [t for t in tasks if t.section == sec_key]
        sec_epics = [e for e in epics if e.section == sec_key]
        if not sec_tasks and not sec_epics:
            continue
        out.append(f"## {sec_label}")
        out.append("")

        # group: epicless tasks first, then per epic
        epicless = sorted(
            [t for t in sec_tasks if not t.epic_key], key=lambda t: t.position
        )
        for t in epicless:
            out.extend(_render_task(t))
        rendered_epics = sorted(sec_epics, key=lambda e: e.position)
        # include epics that have tasks in this section even if epic.section differs
        extra_keys = {t.epic_key for t in sec_tasks if t.epic_key} - {
            e.key for e in rendered_epics
        }
        for k in sorted(extra_keys):
            if k in epics_by_key:
                rendered_epics.append(epics_by_key[k])
        for epic in rendered_epics:
            etasks = sorted(
                [t for t in sec_tasks if t.epic_key == epic.key],
                key=lambda t: t.position,
            )
            if not etasks and epic.section != sec_key:
                continue
            out.append(f"### EPIC {epic.key} — {epic.title}")
            out.append("")
            for t in etasks:
                out.extend(_render_task(t))
            out.append("")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _render_task(t) -> list[str]:
    glyph = _STATUS_BY_GLYPH.get(t.status, " ")
    meta = []
    if t.component:
        meta.append(t.component)
    if t.priority:
        meta.append(t.priority)
    if t.status in {"blocked", "deferred", "cancelled", "in_progress"}:
        meta.append(t.status.replace("_", " "))
    # Sort tags so the rendered meta line is deterministic and backend-agnostic
    # (Postgres association order vs DynamoDB list order would otherwise diverge).
    for tag in sorted(getattr(t, "tag_keys", []) or []):
        meta.append(tag)
    head = f"- [{glyph}] {t.key} · {t.title}"
    if meta:
        head += f" — {', '.join(meta)}"
    lines = [head]
    if t.description:
        for dl in t.description.splitlines():
            lines.append(f"  {dl}")
    if t.proof_cmd:
        lines.append(f"  _Proof: {t.proof_cmd}_")
    return lines


def normalize(spec: ParsedSpec) -> list[dict]:
    """A comparable, order-independent view of a parsed spec for round-trip tests."""
    rows = []
    for t in spec.tasks:
        rows.append({
            "key": t.key,
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "component": t.component,
            "proof_cmd": t.proof_cmd,
            "epic_key": t.epic_key,
        })
    return sorted(rows, key=lambda r: (r["key"] or ""))
