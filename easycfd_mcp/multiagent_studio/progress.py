"""Deterministic progress reports for mission-board tasks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .protocol import TaskState, WorkItem


DONE_STATUSES = {"done"}
ACTIVE_STATUSES = {"in_progress", "running"}
BLOCKED_STATUSES = {"blocked", "failed"}
ATTENTION_WORDS = (
    "blocked",
    "blocker",
    "failed",
    "failure",
    "error",
    "spending cap",
    "autonomy depth limit",
    "human decision",
    "clarify",
    "ambiguity",
)
NON_BLOCKER_MARKERS = (
    "no blocker remains",
    "no blocker exists",
    "no technical blocker",
    "not a code blocker",
    "not a code issue",
    "blocker resolved",
    "product ambiguity is resolved",
    "product decision is now clear",
    "ready for implementation",
)


def build_progress_report(task: TaskState) -> dict[str, Any]:
    """Build a UI-friendly report from target TASKS.json plus mission-board state."""

    workspace = _workspace_path(task)
    task_spec = _load_tasks_json(workspace)
    phases = (
        _phases_from_tasks_json(task, workspace, task_spec)
        if task_spec
        else _phases_from_mission_board(task)
    )
    blockers = _blockers(task)
    totals = _totals(phases)
    notifications = _notifications(task, has_blockers=bool(blockers))
    current_phase = next((phase for phase in phases if phase["status"] != "done"), None)

    return {
        "task_id": task.id,
        "goal": task.goal,
        "status": task.status,
        "workspace_path": str(workspace) if workspace else "",
        "source": "TASKS.json" if task_spec else "mission_board",
        "summary": {
            "overall_percent": totals["percent"],
            "completed_items": totals["done"],
            "total_items": totals["total"],
            "phase_count": len(phases),
            "current_phase": current_phase["name"] if current_phase else "Complete",
            "needs_attention": bool(blockers or _warning_notifications(notifications)),
        },
        "phases": phases,
        "notifications": notifications,
        "blockers": blockers,
        "generated_by": "orchestrator-progress",
    }


def _workspace_path(task: TaskState) -> Path | None:
    if not task.workspace_path:
        return None
    return Path(task.workspace_path).expanduser().resolve()


def _load_tasks_json(workspace: Path | None) -> dict[str, Any] | None:
    if not workspace:
        return None
    path = workspace / "TASKS.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _phases_from_tasks_json(
    task: TaskState,
    workspace: Path | None,
    task_spec: dict[str, Any],
) -> list[dict[str, Any]]:
    phases = []
    raw_phases = task_spec.get("phases", [])
    if not isinstance(raw_phases, list):
        raw_phases = []

    for phase_index, raw_phase in enumerate(raw_phases, start=1):
        if not isinstance(raw_phase, dict):
            continue
        checklist = []
        for raw_item in raw_phase.get("tasks", []):
            if not isinstance(raw_item, dict):
                continue
            checklist.append(_checklist_item_from_spec(task, workspace, raw_item))

        phases.append(
            _phase_payload(
                phase_id=str(raw_phase.get("id") or f"phase_{phase_index}"),
                name=str(raw_phase.get("name") or f"Phase {phase_index}"),
                goal=str(raw_phase.get("goal") or ""),
                checklist=checklist,
                index=phase_index,
            )
        )

    return phases


def _checklist_item_from_spec(
    task: TaskState,
    workspace: Path | None,
    raw_item: dict[str, Any],
) -> dict[str, Any]:
    files_to_create = _file_list(raw_item, "files_to_create")
    files_to_modify = _file_list(raw_item, "files_to_modify")
    files = files_to_create + files_to_modify
    existing_files = [
        file_path
        for file_path in files
        if workspace and (workspace / file_path).exists()
    ]
    title = str(raw_item.get("title") or raw_item.get("id") or "Untitled item")
    task_id = str(raw_item.get("id") or "")
    related_work = _related_work(task.work_items, title, task_id)
    exact_work = [item for item in task.work_items if item.metadata.get("target_task_id") == task_id]
    latest_signal = _latest_item_signal(task, title, task_id)

    status = "todo"
    evidence: list[str] = []
    if exact_work:
        status = _latest_work_status(exact_work)
        evidence.append(f"{len(exact_work)} tracked TASKS.json work item(s)")
    elif related_work:
        status = _dominant_work_status(related_work)
        evidence.append(f"{len(related_work)} related mission-board item(s)")
    if files_to_create:
        existing_created = [
            file_path for file_path in files_to_create if workspace and (workspace / file_path).exists()
        ]
        if len(existing_created) == len(files_to_create):
            status = "done"
            evidence.append("all files_to_create exist")
        elif existing_created and status not in ("done", "blocked"):
            status = "in_progress"
            evidence.append(f"{len(existing_created)}/{len(files_to_create)} files_to_create exist")
    if files_to_modify and status not in ("done", "blocked"):
        if existing_files:
            status = "in_progress"
            evidence.append(f"{len(existing_files)}/{len(files)} expected files exist")
    if latest_signal == "done":
        status = "done"
        evidence.append("latest agent report claims completion")
    elif latest_signal == "blocked" and status != "done":
        status = "blocked"
        evidence.append("latest agent report flags a blocker")

    return {
        "id": task_id or title,
        "title": title,
        "status": status,
        "worker": str(raw_item.get("worker") or "any"),
        "depends_on": list(raw_item.get("depends_on", [])),
        "acceptance": str(raw_item.get("acceptance") or ""),
        "files_expected": files,
        "files_present": existing_files,
        "evidence": evidence,
    }


def _phases_from_mission_board(task: TaskState) -> list[dict[str, Any]]:
    groups: dict[str, list[WorkItem]] = {
        "Plan": [],
        "Build": [],
        "Review": [],
        "Recover": [],
    }
    for item in task.work_items:
        text = f"{item.title} {item.description}".lower()
        if "review" in text or "interpret" in text:
            groups["Review"].append(item)
        elif "take over" in text or "blocker" in text or "recover" in text:
            groups["Recover"].append(item)
        elif "implement" in text or "build" in text or "fix" in text:
            groups["Build"].append(item)
        else:
            groups["Plan"].append(item)

    phases = []
    for index, (name, items) in enumerate(groups.items(), start=1):
        if not items:
            continue
        checklist = [
            {
                "id": item.id,
                "title": item.title,
                "status": item.status,
                "worker": item.owner,
                "depends_on": item.depends_on,
                "acceptance": item.description,
                "files_expected": [],
                "files_present": [],
                "evidence": ["mission-board work item"],
            }
            for item in items
        ]
        phases.append(
            _phase_payload(
                phase_id=name.lower(),
                name=name,
                goal=f"{name} work tracked on the mission board.",
                checklist=checklist,
                index=index,
            )
        )
    return phases


def _phase_payload(
    phase_id: str,
    name: str,
    goal: str,
    checklist: list[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    total = len(checklist)
    done = sum(1 for item in checklist if item["status"] == "done")
    blocked = sum(1 for item in checklist if item["status"] == "blocked")
    active = sum(1 for item in checklist if item["status"] in ACTIVE_STATUSES)
    status = "todo"
    if blocked:
        status = "blocked"
    elif total and done == total:
        status = "done"
    elif done or active:
        status = "in_progress"
    return {
        "id": phase_id,
        "name": name,
        "goal": goal,
        "status": status,
        "index": index,
        "progress_percent": round((done / total) * 100) if total else 0,
        "completed": done,
        "total": total,
        "checklist": checklist,
    }


def _file_list(raw_item: dict[str, Any], key: str) -> list[str]:
    value = raw_item.get(key, [])
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _related_work(work_items: list[WorkItem], title: str, task_id: str) -> list[WorkItem]:
    title_l = title.lower()
    task_id_l = task_id.lower()
    result = []
    for item in work_items:
        text = f"{item.title} {item.description} {' '.join(item.depends_on)}".lower()
        if item.metadata.get("target_task_id") == task_id:
            result.append(item)
        elif task_id_l and f"tasks.json item {task_id_l}" in text:
            result.append(item)
        elif title_l and title_l in text:
            result.append(item)
    return result


def _latest_work_status(items: list[WorkItem]) -> str:
    latest = sorted(items, key=lambda item: item.created_at)[-1]
    return latest.status


def _dominant_work_status(items: list[WorkItem]) -> str:
    statuses = {item.status for item in items}
    if statuses & BLOCKED_STATUSES:
        return "blocked"
    if statuses and statuses <= DONE_STATUSES:
        return "done"
    if statuses & ACTIVE_STATUSES:
        return "in_progress"
    return next(iter(statuses), "todo")


def _latest_item_signal(task: TaskState, title: str, task_id: str) -> str:
    needles = [value.lower() for value in (title, task_id) if value]
    for message in reversed(task.messages[-120:]):
        text = message.content.lower()
        if not _contains_any(text, needles):
            continue
        if message.type not in {"report_result", "review", "decision"}:
            continue
        if _contains_any(text, NON_BLOCKER_MARKERS):
            if any(word in text for word in ("implemented", "completed", "done", "verified", "passes", "ok")):
                return "done"
            return "progress"
        if any(word in text for word in ("implemented", "completed", "done", "verified", "passes", "ok")):
            return "done"
        if any(word in text for word in ("blocked", "blocker", "failed", "error")):
            return "blocked"
    return ""


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle and needle in text for needle in needles)


def _totals(phases: list[dict[str, Any]]) -> dict[str, int]:
    total = sum(phase["total"] for phase in phases)
    done = sum(phase["completed"] for phase in phases)
    return {
        "total": total,
        "done": done,
        "percent": round((done / total) * 100) if total else 0,
    }


def _notifications(task: TaskState, *, has_blockers: bool) -> list[dict[str, Any]]:
    notifications = []
    for message in reversed(task.messages[-80:]):
        text = message.content.strip()
        lower = text.lower()
        is_orchestrator_notice = message.sender == "orchestrator" and message.recipient in {"human", "all"}
        if not is_orchestrator_notice:
            continue
        is_attention = _is_attention_notice(lower, has_blockers=has_blockers)
        tone = "warning" if is_attention else "info"
        notifications.append(
            {
                "id": message.id,
                "created_at": message.created_at,
                "title": _notice_title(text, tone),
                "body": text,
                "tone": tone,
                "sender": message.sender,
                "refs": message.refs,
            }
        )
        if len(notifications) >= 8:
            break
    return list(reversed(notifications))


def _is_attention_notice(lower_text: str, *, has_blockers: bool) -> bool:
    if _contains_any(lower_text, NON_BLOCKER_MARKERS):
        return False
    if (
        "auto-invocation failed" in lower_text
        or "spending cap" in lower_text
        or "autonomy depth limit" in lower_text
    ):
        return True
    blocker_words = ("blocked", "blocker", "human direction", "human decision", "clarify", "ambiguity")
    if has_blockers and any(word in lower_text for word in blocker_words):
        return True
    return any(word in lower_text for word in ("failed", "failure", "error"))


def _notice_title(text: str, tone: str) -> str:
    normalized = " ".join(text.split())
    if "autonomy depth limit" in normalized.lower():
        return "Autonomy limit reached"
    if "spending cap" in normalized.lower():
        return "Agent budget blocked"
    if "blocked" in normalized.lower() or "blocker" in normalized.lower():
        return "Blocker needs attention"
    if tone == "warning":
        return "Orchestrator attention"
    return "Orchestrator notice"


def _warning_notifications(notifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [notice for notice in notifications if notice["tone"] == "warning"]


def _blockers(task: TaskState) -> list[dict[str, Any]]:
    blockers = []
    for item in task.work_items:
        if item.status in BLOCKED_STATUSES and _is_current_blocker(item, task):
            blockers.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "owner": item.owner,
                    "description": item.description,
                }
            )
    return blockers


def _is_current_blocker(item: WorkItem, task: TaskState) -> bool:
    if item.metadata.get("source") == "autopilot":
        purpose = item.metadata.get("purpose")
        if purpose != "workspace_task":
            return False
        target_task_id = item.metadata.get("target_task_id")
        if target_task_id and _target_task_completed_from_board(task, str(target_task_id)):
            return False
        return True
    target_task_id = item.metadata.get("target_task_id")
    if target_task_id:
        return not _target_task_completed_from_board(task, str(target_task_id))
    if item.metadata.get("purpose") in {"blocker_recovery", "review_blocker_recovery"}:
        return False
    return True


def _target_task_completed_from_board(task: TaskState, target_task_id: str) -> bool:
    for candidate in task.work_items:
        if candidate.metadata.get("target_task_id") == target_task_id and candidate.status == "done":
            return True
    for message in reversed(task.messages[-120:]):
        text = message.content.lower()
        if target_task_id.lower() in text and _latest_item_signal(task, target_task_id, target_task_id) == "done":
            return True
    return False
