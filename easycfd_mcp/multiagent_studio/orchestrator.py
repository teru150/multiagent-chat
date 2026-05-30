"""Context-packet generation and neutral planning helpers."""

from __future__ import annotations

from pathlib import Path

from .protocol import Message, TaskState, WorkItem
from .store import MissionBoard


AGENT_BRIEFS = {
    "claude": (
        "Default strengths: requirements, design, product judgment, ambiguity, "
        "risk review, synthesis, and prose."
    ),
    "codex": (
        "Default strengths: repo inspection, implementation, command execution, "
        "debugging, tests, and integration details."
    ),
    "subagent": (
        "Default scope: narrow delegated investigations with concrete reports. "
        "Do not make final decisions."
    ),
}

COMMANDER_REPORT_TYPES = {"report_result", "review"}
COMMANDER_SOURCE = "auto_commander"

BLOCKER_MARKERS = (
    "blocked",
    "blocker",
    "cannot",
    "can't",
    "failed",
    "failure",
    "error",
    "exception",
    "not allowed",
    "permission",
    "read-only",
    "実行しません",
    "できません",
    "失敗",
    "エラー",
    "権限",
)

COMPLETION_MARKERS = (
    "implemented",
    "fixed",
    "changed",
    "updated",
    "added",
    "completed",
    "done",
    "verified",
    "tests pass",
    "実行しました",
    "実装",
    "修正",
    "追加",
    "完了",
)

IMPLEMENTATION_MARKERS = (
    "fix",
    "change",
    "update",
    "modify",
    "add",
    "wire",
    "test",
    "実装",
    "修正",
    "変更",
    "追加",
)

REVIEW_MARKERS = (
    "please review",
    "request review",
    "needs review",
    "ask claude to review",
    "ask codex to review",
    "レビューして",
    "レビューを依頼",
    "確認して",
    "検証して",
)


def bootstrap_work_items(goal: str) -> list[WorkItem]:
    """Create a conservative first mission-board split."""
    return [
        WorkItem(
            title="Independent interpretation",
            owner="claude",
            description=(
                "Interpret the human goal, identify ambiguity, constraints, success "
                "criteria, and product or reasoning risks."
            ),
        ),
        WorkItem(
            title="Repository and execution path",
            owner="codex",
            description=(
                "Inspect the local workspace, identify relevant files, propose an "
                "implementation and verification path, and note blockers."
            ),
        ),
        WorkItem(
            title="Compare plans and decide split",
            owner="orchestrator",
            description=(
                "Compare Claude and Codex proposals, resolve conflicts, then assign "
                "implementation and review work."
            ),
            depends_on=[],
        ),
    ]


def commander_already_handled(task: TaskState, message_id: str) -> bool:
    """Return true when a report already produced commander follow-up."""
    for message in task.messages:
        if (
            message.sender == "orchestrator"
            and message.metadata.get("source") == COMMANDER_SOURCE
            and message_id in message.refs
        ):
            return True
    return False


def commander_generated_work_ids(task: TaskState) -> set[str]:
    """Return work ids created by the deterministic commander layer."""
    ids: set[str] = set()
    for message in task.messages:
        if message.metadata.get("source") != COMMANDER_SOURCE:
            continue
        generated_id = message.metadata.get("generated_work_id")
        if generated_id:
            ids.add(str(generated_id))
        ids.update(ref for ref in message.refs if str(ref).startswith("work_"))
    return ids


def analyze_report_and_generate_next_work(
    task: TaskState,
    report: Message,
) -> WorkItem | None:
    """Turn an agent report into the next mission-board work item.

    This is deliberately conservative. It handles the common commander cases
    deterministically and avoids creating duplicate follow-up for the same
    report. The LLM policy layer can later replace or augment the heuristics.
    """
    if report.recipient != "orchestrator":
        return None
    if report.sender not in ("claude", "codex"):
        return None
    if report.type not in COMMANDER_REPORT_TYPES:
        return None
    if report.metadata.get("source") == COMMANDER_SOURCE:
        return None
    if commander_already_handled(task, report.id):
        return None
    if set(report.refs) & commander_generated_work_ids(task):
        return None

    content = report.content.strip()
    lowered = content.lower()
    excerpt = _excerpt(content)

    # Handle status prefix messages (実行しました/実行しません)
    # These are structured status declarations required by the agent prompt
    will_execute = (
        lowered.startswith("実行しました:") or lowered.startswith("実行しました：")
    )
    will_not_execute = (
        lowered.startswith("実行しません:") or lowered.startswith("実行しません：")
    )

    # If agent says "実行しません" (will not execute), skip all work generation
    # because there's nothing to review or follow up on
    if will_not_execute:
        return None

    # Check for blockers only in non-status-prefix messages
    if not will_execute and _contains_any(lowered, BLOCKER_MARKERS):
        return WorkItem(
            title=f"Resolve blocker from {report.sender} report",
            owner="codex",
            description=(
                f"{report.sender} reported a blocker. Inspect the report, fix the "
                "underlying issue when it is implementation-related, and report the "
                f"result back to the mission board.\n\nReport excerpt: {excerpt}"
            ),
        )

    if report.sender == "codex" and _contains_any(lowered, COMPLETION_MARKERS):
        return WorkItem(
            title="Review Codex result",
            owner="claude",
            description=(
                "Codex reported completed implementation or verification work. "
                "Review whether the result satisfies the human goal, call out risks "
                "or missing checks, and request concrete changes if needed.\n\n"
                f"Report excerpt: {excerpt}"
            ),
        )

    if report.sender == "claude" and _contains_any(lowered, IMPLEMENTATION_MARKERS):
        return WorkItem(
            title="Implement Claude recommendation",
            owner="codex",
            description=(
                "Claude's report contains an implementation-oriented recommendation. "
                "Turn it into a concrete repo change where appropriate, run focused "
                f"verification, and report back.\n\nReport excerpt: {excerpt}"
            ),
        )

    if _contains_any(lowered, REVIEW_MARKERS):
        peer = "claude" if report.sender == "codex" else "codex"
        return WorkItem(
            title=f"Peer-check {report.sender} report",
            owner=peer,
            description=(
                f"{report.sender}'s report asks for confirmation or review. Check "
                "the claim against the mission-board state and produce a concise "
                f"review.\n\nReport excerpt: {excerpt}"
            ),
        )

    return None


def make_commander_decision(report: Message, work: WorkItem) -> Message:
    """Create an auditable decision before auto-assigning commander work."""
    return Message(
        type="decision",
        sender="orchestrator",
        recipient="all",
        content=(
            "Commander analysis created follow-up work from "
            f"{report.sender}'s {report.type}: {work.title} -> {work.owner}."
        ),
        refs=[report.id, work.id],
        metadata={
            "source": COMMANDER_SOURCE,
            "actor": "orchestrator",
            "trigger_actor": report.sender,
            "generated_work_id": work.id,
            "generated_work_owner": work.owner,
            "generated_work_title": work.title,
        },
    )


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker.lower() in text for marker in markers)


def _excerpt(text: str, limit: int = 500) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit].rstrip()}..."


def create_context_packet(
    board: MissionBoard,
    task: TaskState,
    agent: str,
    recent_messages: int = 12,
) -> str:
    """Build a role-specific packet without making any agent the owner."""
    workspace_path = _display_workspace_path(board, task)
    lines = [
        "# Multi-Agent Task Packet",
        "",
        f"Task ID: {task.id}",
        f"Status: {task.status}",
        f"Recipient: {agent}",
        "",
        "## Goal",
        task.goal,
        "",
        "## Workspace",
        f"Mission board root: {board.root.resolve()}",
        f"Target workspace: {workspace_path}",
        (
            "Use the target workspace for file edits, repo inspection, commands, "
            "and tests. Use the mission board only for coordination state."
        ),
        "",
    ]

    brief = AGENT_BRIEFS.get(agent, AGENT_BRIEFS.get("subagent", ""))
    if brief:
        lines.extend(["## Role Brief", brief, ""])

    if task.constraints:
        lines.extend(["## Constraints", *[f"- {item}" for item in task.constraints], ""])

    human_memory = board.read_memory("human")
    project_memory = board.read_memory("project")
    agent_memory = board.read_memory("agents")
    memory_sections = [
        ("Human Memory", human_memory),
        ("Project Memory", project_memory),
        ("Agent Memory", agent_memory),
    ]
    for title, content in memory_sections:
        if content:
            lines.extend([f"## {title}", content, ""])

    if task.work_items:
        lines.append("## Mission Board")
        for item in task.work_items:
            marker = ">" if item.owner == agent else "-"
            lines.append(f"{marker} [{item.status}] {item.id} / {item.owner}: {item.title}")
            if item.description:
                lines.append(f"  {item.description}")
        lines.append("")

    if task.agent_activity:
        lines.append("## Agent Activity")
        for agent, activity in sorted(task.agent_activity.items()):
            current = f" current={activity.current_work_id}" if activity.current_work_id else ""
            note = f" note={activity.note}" if activity.note else ""
            lines.append(
                f"- {agent}: {activity.status} at {activity.last_seen_at}{current}{note}"
            )
        lines.append("")

    if task.artifacts:
        lines.append("## Artifacts")
        for artifact in task.artifacts:
            location = artifact.path or "inline"
            lines.append(f"- {artifact.id} / {artifact.kind} / {artifact.owner}: {artifact.title} ({location})")
        lines.append("")

    if task.reviews:
        lines.append("## Reviews")
        for review in task.reviews:
            lines.append(f"- {review.id} by {review.reviewer} on {review.artifact_id}: {review.verdict}")
            if review.required_changes:
                lines.extend(f"  change: {change}" for change in review.required_changes)
        lines.append("")

    visible = [
        message
        for message in task.messages
        if message.recipient in ("all", agent) or message.sender == agent
    ][-recent_messages:]
    if visible:
        lines.append("## Recent Messages")
        for message in visible:
            refs = f" refs={','.join(message.refs)}" if message.refs else ""
            lines.append(
                f"- {message.created_at} {message.sender} -> {message.recipient} "
                f"({message.type}{refs}): {message.content}"
            )
        lines.append("")

    lines.extend(
        [
            "## Collaboration Rule",
            (
                "Form your own view before agreeing with another agent. Raise concrete "
                "objections when needed, cite artifacts by id, and submit structured "
                "messages back to the mission board."
            ),
        ]
    )
    return "\n".join(lines).strip()


def _display_workspace_path(board: MissionBoard, task: TaskState) -> str:
    raw = task.workspace_path.strip()
    if not raw:
        return str(board.root.parent.resolve())
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (board.root.parent / path).resolve()
    return str(path.resolve())


def summarize_task(task: TaskState) -> str:
    done = sum(1 for item in task.work_items if item.status == "done")
    total = len(task.work_items)
    return (
        f"{task.id}: {task.goal}\n"
        f"status={task.status}, work={done}/{total} done, "
        f"messages={len(task.messages)}, artifacts={len(task.artifacts)}, "
        f"reviews={len(task.reviews)}"
    )


def append_decision(task: TaskState, decision: str) -> Message:
    task.decisions.append(decision)
    return Message(type="decision", sender="orchestrator", content=decision)
