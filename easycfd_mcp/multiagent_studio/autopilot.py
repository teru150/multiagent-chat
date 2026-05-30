"""Deterministic orchestration loop for unattended multi-agent progress."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .policy import find_stalled_agents
from .protocol import Message, TaskState, WorkItem, parse_iso
from .store import MissionBoard


AUTOPILOT_SOURCE = "autopilot"
ACTIVE_AGENTS = {"claude", "codex"}
ACTIONABLE_HUMAN_RECIPIENTS = {"orchestrator", "all", "claude", "codex"}
ACTIONABLE_AGENT_TYPES = {"proposal", "request_review", "objection", "report_result", "review"}
TERMINAL_WORK_STATUSES = {"done"}
OPEN_WORK_STATUSES = {"todo", "in_progress", "blocked"}
MAX_AUTONOMY_DEPTH = 2
MAX_BLOCKER_RECOVERY_DEPTH = 2

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
    "timed out",
    "timeout",
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
    "ok",
)

IMPLEMENTATION_MARKERS = (
    "implement",
    "fix",
    "change",
    "update",
    "modify",
    "add",
    "wire",
    "test",
    "build",
)

REVIEW_MARKERS = (
    "review",
    "validate",
    "inspect",
    "check",
    "confirm",
    "risk",
)


@dataclass
class PlannedAction:
    type: str
    message: Message | None = None
    work: WorkItem | None = None
    work_id: str = ""
    status: str = ""
    reason: str = ""


@dataclass
class AutopilotResult:
    trigger_id: str = ""
    planned: list[PlannedAction] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    dispatched: list[str] = field(default_factory=list)
    skipped_reason: str = ""


def run_autopilot_once(
    board: MissionBoard,
    task_id: str,
    *,
    trigger_message_id: str = "",
    stale_after_seconds: int = 900,
    scan_backlog: bool = False,
    apply: bool = True,
    dispatch: bool = True,
    invoker: Callable[[str, str], str] | None = None,
    background: bool = True,
) -> AutopilotResult:
    """Plan and optionally apply one bounded orchestrator cycle.

    The loop is intentionally single-step. Repeated progress should happen by
    calling this after new messages arrive, not by recursively dispatching from
    inside agent execution.
    """
    task = board.load(task_id)
    trigger = _select_trigger(task, trigger_message_id, scan_backlog=scan_backlog)
    if trigger is None:
        if scan_backlog and not _has_fresh_running_agent(task, stale_after_seconds=stale_after_seconds):
            trigger = Message(
                type="plan_update",
                sender="orchestrator",
                recipient="orchestrator",
                content="Periodic autopilot tick.",
                metadata={"source": AUTOPILOT_SOURCE, "actor": "orchestrator"},
            )
            planned = _plan_periodic_progress(task, trigger)
            if planned:
                result = AutopilotResult(trigger_id=trigger.id, planned=planned)
                if not apply:
                    return result
                result.applied, result.dispatched = apply_autopilot_actions(
                    board,
                    task_id,
                    trigger,
                    planned,
                    dispatch=dispatch,
                    invoker=invoker,
                    background=background,
                )
                return result
        return AutopilotResult(skipped_reason="no_actionable_trigger")

    if _already_handled(task, trigger.id):
        return AutopilotResult(trigger_id=trigger.id, skipped_reason="trigger_already_handled")

    result = AutopilotResult(trigger_id=trigger.id)
    if _has_fresh_running_agent(task, stale_after_seconds=stale_after_seconds):
        result.skipped_reason = "agent_running"
        return result

    planned = plan_autopilot_actions(task, trigger, stale_after_seconds=stale_after_seconds)
    result.planned = planned
    if not planned:
        result.skipped_reason = "no_plan"
        return result
    if not apply:
        return result

    result.applied, result.dispatched = apply_autopilot_actions(
        board,
        task_id,
        trigger,
        planned,
        dispatch=dispatch,
        invoker=invoker,
        background=background,
    )
    return result


def plan_autopilot_actions(
    task: TaskState,
    trigger: Message,
    *,
    stale_after_seconds: int = 900,
) -> list[PlannedAction]:
    if trigger.sender in ACTIVE_AGENTS and trigger.recipient == "orchestrator":
        if trigger.type in {"proposal", "request_review", "objection"}:
            return _plan_agent_proposal(task, trigger)
        if trigger.type in {"report_result", "review"}:
            return _plan_agent_report(task, trigger)

    stalled = find_stalled_agents(task, stale_after_seconds)
    if stalled:
        return _plan_stalled_takeover(task, trigger, stalled)

    if trigger.sender == "human" and trigger.recipient in ACTIONABLE_HUMAN_RECIPIENTS:
        return _plan_human_instruction(task, trigger)

    return []


def _plan_periodic_progress(task: TaskState, trigger: Message) -> list[PlannedAction]:
    next_task = _next_workspace_task_action(task, trigger)
    if not next_task:
        return []
    return [
        _make_decision_action(trigger, "Periodic autopilot tick assigned the next incomplete workspace task."),
        next_task,
    ]


def apply_autopilot_actions(
    board: MissionBoard,
    task_id: str,
    trigger: Message,
    actions: list[PlannedAction],
    *,
    dispatch: bool = True,
    invoker: Callable[[str, str], str] | None = None,
    background: bool = True,
) -> tuple[list[str], list[str]]:
    from .agent_runner import dispatch_work_item

    applied: list[str] = []
    dispatched: list[str] = []

    for action in actions:
        if action.type == "decision" and action.message:
            board.append_message(task_id, action.message)
            applied.append("decision")
        elif action.type == "assign_work" and action.work:
            board.add_work_item(task_id, action.work)
            applied.append("assign_work")
            if dispatch and action.work.owner in ACTIVE_AGENTS:
                owner = dispatch_work_item(
                    board,
                    task_id,
                    action.work.id,
                    invoker=invoker,
                    background=background,
                )
                if owner:
                    dispatched.append(owner)
        elif action.type == "update_work" and action.work_id:
            board.update_work_item(task_id, action.work_id, status=action.status)
            applied.append("update_work")

    if not any(action.type == "decision" for action in actions):
        board.append_message(task_id, _decision(trigger, "Autopilot handled trigger without a separate decision."))
        applied.append("decision")

    return applied, dispatched


def _select_trigger(task: TaskState, trigger_message_id: str, *, scan_backlog: bool = False) -> Message | None:
    if trigger_message_id:
        return next((message for message in task.messages if message.id == trigger_message_id), None)
    if not task.messages:
        return None
    if not scan_backlog:
        latest = task.messages[-1]
        return latest if _is_actionable(latest) else None
    for message in reversed(task.messages):
        if _is_actionable(message) and not _already_handled(task, message.id):
            return message
    return None


def _is_actionable(message: Message) -> bool:
    if message.type == "user_goal":
        return False
    if message.sender == "human" and message.recipient in ACTIONABLE_HUMAN_RECIPIENTS:
        return True
    if (
        message.sender in ACTIVE_AGENTS
        and message.recipient == "orchestrator"
        and message.type in ACTIONABLE_AGENT_TYPES
    ):
        return True
    return False


def _already_handled(task: TaskState, trigger_id: str) -> bool:
    return any(
        message.sender == "orchestrator"
        and message.metadata.get("source") == AUTOPILOT_SOURCE
        and trigger_id in message.refs
        for message in task.messages
    )


def _has_fresh_running_agent(task: TaskState, *, stale_after_seconds: int) -> bool:
    now = datetime.now(timezone.utc)
    return any(
        activity.status == "running"
        and (now - parse_iso(activity.last_seen_at)).total_seconds() <= stale_after_seconds
        for agent, activity in task.agent_activity.items()
        if agent in ACTIVE_AGENTS
    )


def _plan_human_instruction(task: TaskState, trigger: Message) -> list[PlannedAction]:
    targets = _infer_targets(trigger.content, default_both=True)
    actions = [_make_decision_action(trigger, f"Accepted human instruction and assigned work to {', '.join(targets)}.")]
    workspace_action = _next_workspace_task_action(task, trigger)
    if workspace_action and _looks_like_workspace_go_ahead(trigger.content):
        actions.append(workspace_action)
    for target in targets:
        title = _human_work_title(target, trigger.content)
        actions.append(
            PlannedAction(
                type="assign_work",
                work=WorkItem(
                    title=title,
                    owner=target,
                    description=(
                        "Handle the human instruction from the mission board. "
                        "Produce a concrete report_result with what you did, what remains, "
                        "and whether another agent should review or continue.\n\n"
                        f"Instruction: {_excerpt(trigger.content, 900)}"
                    ),
                    metadata={
                        "source": AUTOPILOT_SOURCE,
                        "trigger_message_id": trigger.id,
                        "autonomy_depth": 0,
                        "purpose": "human_instruction",
                    },
                ),
            )
    )
    return actions


def _looks_like_workspace_go_ahead(content: str) -> bool:
    markers = (
        "we are making",
        "build",
        "start",
        "scaffold",
        "phase",
        "tasks.json",
        "sekret",
        "作る",
        "作って",
        "進め",
    )
    return _contains_any(content, markers)


def _plan_agent_proposal(task: TaskState, trigger: Message) -> list[PlannedAction]:
    targets = [agent for agent in _infer_targets(trigger.content, default_both=False) if agent != trigger.sender]
    if not targets:
        targets = [_peer(trigger.sender)]
    actions = [_make_decision_action(trigger, f"Converted {trigger.sender}'s {trigger.type} into peer work for {', '.join(targets)}.")]
    for target in targets:
        actions.append(
            PlannedAction(
                type="assign_work",
                work=WorkItem(
                    title=f"Act on {trigger.sender} {trigger.type}",
                    owner=target,
                    description=(
                        f"{trigger.sender} sent an actionable {trigger.type}. Evaluate it independently, "
                        "then implement, review, or object with concrete evidence.\n\n"
                        f"Message: {_excerpt(trigger.content, 900)}"
                    ),
                    metadata={
                        "source": AUTOPILOT_SOURCE,
                        "trigger_message_id": trigger.id,
                        "autonomy_depth": _trigger_depth(task, trigger),
                        "purpose": f"agent_{trigger.type}",
                    },
                ),
            )
        )
    return actions


def _plan_agent_report(task: TaskState, trigger: Message) -> list[PlannedAction]:
    depth = _trigger_depth(task, trigger)
    referenced_work = _referenced_work(task, trigger)
    actions: list[PlannedAction] = []

    if referenced_work and referenced_work.status in OPEN_WORK_STATUSES:
        status = "blocked" if _is_blocker_report(trigger.content) else "done"
        actions.append(
            PlannedAction(
                type="update_work",
                work_id=referenced_work.id,
                status=status,
                reason=f"agent report marked referenced work {status}",
            )
        )

    if referenced_work and referenced_work.metadata.get("purpose") == "peer_review":
        if _is_blocker_report(trigger.content):
            owner = "codex" if trigger.sender != "codex" else "claude"
            actions.insert(0, _make_decision_action(trigger, f"Peer review found a blocker; assigned recovery to {owner}."))
            actions.append(
                _work_action(
                    trigger,
                    owner,
                    f"Resolve review blocker from {trigger.sender}",
                    "Resolve the review finding with concrete repo work where possible, then verify and report.",
                    purpose="review_blocker_recovery",
                    depth=depth + 1,
                )
            )
            return actions
        next_constraint = _next_constraint_action(task, trigger)
        if next_constraint:
            actions.insert(0, _make_decision_action(trigger, "Peer review recorded; assigned the next unmet task constraint."))
            actions.append(next_constraint)
            return actions
        next_task = _next_workspace_task_action(task, trigger)
        if next_task:
            actions.insert(0, _make_decision_action(trigger, "Peer review recorded; assigned the next incomplete workspace task."))
            actions.append(next_task)
            return actions
        actions.insert(0, _make_decision_action(trigger, "Peer review result recorded; no incomplete workspace task found."))
        return actions

    if _is_blocker_report(trigger.content):
        if depth >= MAX_BLOCKER_RECOVERY_DEPTH:
            actions.insert(0, _make_decision_action(trigger, "Blocker recovery limit reached; waiting for human direction before generating more recovery work."))
            return actions
        owner = "codex" if trigger.sender != "codex" else "claude"
        actions.insert(0, _make_decision_action(trigger, f"{trigger.sender} reported a blocker; assigned recovery to {owner}."))
        actions.append(
            _work_action(
                trigger,
                owner,
                f"Resolve blocker from {trigger.sender} report",
                "Investigate the blocker, recover if possible, and report the concrete result.",
                purpose="blocker_recovery",
                depth=depth + 1,
            )
        )
        return actions

    if trigger.sender == "codex" and _looks_complete(trigger.content):
        if not _recent_peer_review_exists(task, trigger):
            actions.insert(0, _make_decision_action(trigger, "Codex reported completion; assigned Claude review."))
            actions.append(
                _work_action(
                    trigger,
                    "claude",
                    "Review Codex result",
                    "Review Codex's reported result against the human goal. Approve, object, or request exact changes.",
                    purpose="peer_review",
                    depth=depth + 1,
                )
            )
        elif not actions:
            actions.append(_make_decision_action(trigger, "Codex report already has peer review coverage."))
        return actions

    next_task = _next_workspace_task_action(task, trigger)
    if next_task and _clears_blocker_and_points_forward(trigger.content):
        actions.insert(0, _make_decision_action(trigger, "Agent cleared the blocker; assigned the next incomplete workspace task."))
        actions.append(next_task)
        return actions

    if trigger.sender == "claude" and _contains_any(trigger.content, IMPLEMENTATION_MARKERS):
        actions.insert(0, _make_decision_action(trigger, "Claude report contains implementation guidance; assigned Codex continuation."))
        actions.append(
            _work_action(
                trigger,
                "codex",
                "Implement Claude recommendation",
                "Turn Claude's recommendation into concrete repo work where appropriate, then verify and report.",
                purpose="implementation_followup",
                depth=depth + 1,
            )
        )
        return actions

    next_constraint = _next_constraint_action(task, trigger)
    if next_constraint and _looks_complete(trigger.content):
        actions.insert(0, _make_decision_action(trigger, "Agent reported progress; assigned the next unmet task constraint."))
        actions.append(next_constraint)
        return actions

    if next_task and (_looks_complete(trigger.content) or _clears_blocker_and_points_forward(trigger.content)):
        actions.insert(0, _make_decision_action(trigger, "Agent reported progress; assigned the next incomplete workspace task."))
        actions.append(next_task)
        return actions

    if not actions:
        actions.append(_make_decision_action(trigger, "Agent report recorded; no safe autonomous follow-up needed."))
    return actions


def _next_constraint_action(task: TaskState, trigger: Message) -> PlannedAction | None:
    constraint = _next_uncovered_constraint(task)
    if not constraint:
        return None
    owner = _constraint_owner(constraint)
    return PlannedAction(
        type="assign_work",
        work=WorkItem(
            title=f"Address constraint: {_short_title(constraint)}",
            owner=owner,
            description=(
                "Continue the task by addressing this unmet mission constraint. "
                "Inspect the current mission-board state, implement or review what is appropriate, "
                "then report exactly what changed or what remains.\n\n"
                f"Constraint: {constraint}\n"
                f"Trigger report from {trigger.sender}: {_excerpt(trigger.content, 900)}"
            ),
            metadata={
                "source": AUTOPILOT_SOURCE,
                "trigger_message_id": trigger.id,
                "autonomy_depth": _trigger_depth(task, trigger),
                "purpose": "constraint_followup",
                "target_constraint": constraint,
            },
        ),
    )


def _next_uncovered_constraint(task: TaskState) -> str:
    covered = {
        str(item.metadata.get("target_constraint"))
        for item in task.work_items
        if item.metadata.get("source") == AUTOPILOT_SOURCE
        and item.metadata.get("purpose") == "constraint_followup"
    }
    for constraint in task.constraints:
        text = str(constraint).strip()
        if text and text not in covered:
            return text
    return ""


def _constraint_owner(constraint: str) -> str:
    lowered = constraint.lower()
    if _contains_any(lowered, REVIEW_MARKERS):
        return "claude"
    if _contains_any(lowered, IMPLEMENTATION_MARKERS):
        return "codex"
    if any(marker in lowered for marker in ("ui", "api", "code", "test", "real-time", "realtime", "artifact")):
        return "codex"
    return "claude"


def _next_workspace_task_action(task: TaskState, trigger: Message) -> PlannedAction | None:
    next_item = _next_workspace_task(task)
    if not next_item:
        return None
    task_id = str(next_item.get("id") or "")
    title = str(next_item.get("title") or task_id or "Next workspace task")
    worker = str(next_item.get("worker") or "any").lower()
    owner = worker if worker in ACTIVE_AGENTS else "codex"
    files = _workspace_task_files(next_item)
    acceptance = str(next_item.get("acceptance") or "")
    description = (
        "Execute the next incomplete item from the target repository TASKS.json. "
        "Edit the target workspace when needed, run focused verification, and report exact files changed.\n\n"
        f"TASKS.json item: {task_id} - {title}\n"
        f"Expected files: {', '.join(files) if files else 'not specified'}\n"
        f"Acceptance: {acceptance or 'not specified'}"
    )
    return PlannedAction(
        type="assign_work",
        work=WorkItem(
            title=f"Execute TASKS.json item {task_id or title}",
            owner=owner,
            description=description,
            metadata={
                "source": AUTOPILOT_SOURCE,
                "trigger_message_id": trigger.id,
                "autonomy_depth": 0,
                "purpose": "workspace_task",
                "target_task_id": task_id,
            },
        ),
    )


def _next_workspace_task(task: TaskState) -> dict | None:
    spec = _load_workspace_tasks(task)
    if not spec:
        return None
    workspace = _workspace_path(task)
    assigned_ids = {
        str(item.metadata.get("target_task_id"))
        for item in task.work_items
        if item.metadata.get("source") == AUTOPILOT_SOURCE
        and item.metadata.get("purpose") == "workspace_task"
        and item.status in OPEN_WORK_STATUSES
    }
    for phase in spec.get("phases", []):
        if not isinstance(phase, dict):
            continue
        for item in phase.get("tasks", []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if item_id and item_id in assigned_ids:
                continue
            if not _workspace_task_complete(workspace, item, task):
                return item
    return None


def _load_workspace_tasks(task: TaskState) -> dict | None:
    workspace = _workspace_path(task)
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


def _workspace_path(task: TaskState) -> Path | None:
    if not task.workspace_path:
        return None
    return Path(task.workspace_path).expanduser().resolve()


def _workspace_task_complete(workspace: Path | None, item: dict, task: TaskState | None = None) -> bool:
    files_to_create = _workspace_task_file_list(item, "files_to_create")
    files_to_modify = _workspace_task_file_list(item, "files_to_modify")
    if files_to_modify:
        return _workspace_task_reported_done(task, item)
    if not workspace or not files_to_create:
        return False
    return all((workspace / file_path).exists() for file_path in files_to_create)


def _workspace_task_files(item: dict) -> list[str]:
    files: list[str] = []
    for key in ("files_to_create", "files_to_modify"):
        files.extend(_workspace_task_file_list(item, key))
    return files


def _workspace_task_file_list(item: dict, key: str) -> list[str]:
    value = item.get(key, [])
    if not isinstance(value, list):
        return []
    return [str(file_path) for file_path in value if str(file_path).strip()]


def _workspace_task_reported_done(task: TaskState | None, item: dict) -> bool:
    if not task:
        return False
    item_id = str(item.get("id") or "").lower()
    title = str(item.get("title") or "").lower()
    for work in task.work_items:
        if work.metadata.get("target_task_id") == item.get("id") and work.status == "done":
            return True
    for message in reversed(task.messages[-80:]):
        text = message.content.lower()
        if item_id and item_id in text and _looks_complete(text):
            return True
        if title and title in text and _looks_complete(text):
            return True
    return False


def _plan_stalled_takeover(task: TaskState, trigger: Message, stalled: list[str]) -> list[PlannedAction]:
    actions = [_make_decision_action(trigger, f"Detected stalled agents: {', '.join(stalled)}.")]
    for agent in stalled:
        open_work = [
            item for item in task.work_items if item.owner == agent and item.status in OPEN_WORK_STATUSES
        ]
        if not open_work:
            continue
        replacement = _peer(agent)
        item = open_work[0]
        actions.append(
            PlannedAction(
                type="assign_work",
                work=WorkItem(
                    title=f"Take over stalled {agent} work",
                    owner=replacement,
                    description=(
                        f"{agent} appears stalled on {item.id}: {item.title}. Continue from the mission board "
                        "context and produce a concise report."
                    ),
                    depends_on=[item.id],
                    metadata={
                        "source": AUTOPILOT_SOURCE,
                        "trigger_message_id": trigger.id,
                        "autonomy_depth": 0,
                        "purpose": "stalled_takeover",
                        "stalled_agent": agent,
                    },
                ),
            )
        )
    return actions


def _work_action(
    trigger: Message,
    owner: str,
    title: str,
    instruction: str,
    *,
    purpose: str,
    depth: int,
) -> PlannedAction:
    return PlannedAction(
        type="assign_work",
        work=WorkItem(
            title=title,
            owner=owner,
            description=(
                f"{instruction}\n\nTrigger report from {trigger.sender}: "
                f"{_excerpt(trigger.content, 900)}"
            ),
            metadata={
                "source": AUTOPILOT_SOURCE,
                "trigger_message_id": trigger.id,
                "autonomy_depth": depth,
                "purpose": purpose,
            },
        ),
    )


def _make_decision_action(trigger: Message, content: str) -> PlannedAction:
    return PlannedAction(type="decision", message=_decision(trigger, content))


def _decision(trigger: Message, content: str) -> Message:
    return Message(
        type="decision",
        sender="orchestrator",
        recipient="all",
        content=content,
        refs=[trigger.id],
        metadata={
            "source": AUTOPILOT_SOURCE,
            "actor": "orchestrator",
            "trigger_actor": trigger.sender,
        },
    )


def _infer_targets(content: str, *, default_both: bool) -> list[str]:
    lowered = content.lower()
    targets: list[str] = []
    if "claude" in lowered:
        targets.append("claude")
    if "codex" in lowered:
        targets.append("codex")
    if targets:
        return targets
    if _contains_any(lowered, IMPLEMENTATION_MARKERS):
        return ["codex"]
    if _contains_any(lowered, REVIEW_MARKERS):
        return ["claude"]
    return ["claude", "codex"] if default_both else []


def _human_work_title(agent: str, content: str) -> str:
    if agent == "codex":
        if _contains_any(content, IMPLEMENTATION_MARKERS):
            return "Implement human instruction"
        return "Inspect execution path"
    if _contains_any(content, REVIEW_MARKERS):
        return "Review human instruction"
    return "Analyze human instruction"


def _short_title(text: str, limit: int = 56) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit].rstrip()}..."


def _referenced_work(task: TaskState, message: Message) -> WorkItem | None:
    refs = set(message.refs)
    trigger_work_id = message.metadata.get("trigger_work_id")
    if trigger_work_id:
        refs.add(str(trigger_work_id))
    for item in task.work_items:
        if item.id in refs:
            return item
    return None


def _trigger_depth(task: TaskState, message: Message) -> int:
    work = _referenced_work(task, message)
    if work:
        try:
            return int(work.metadata.get("autonomy_depth", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def _recent_peer_review_exists(task: TaskState, trigger: Message) -> bool:
    for message in reversed(task.messages[-20:]):
        if message.sender == "claude" and message.type in {"review", "report_result"}:
            if trigger.id in message.refs:
                return True
    return False


def _looks_complete(content: str) -> bool:
    return _contains_any(content, COMPLETION_MARKERS) and not _is_blocker_report(content)


def _is_blocker_report(content: str) -> bool:
    lowered = content.lower()
    non_blocker_markers = (
        "no blocker remains",
        "no blocker exists",
        "no blocker is present",
        "not a blocker",
        "no technical blocker",
        "blocker resolved",
        "blocker does not exist",
        "blocker is absent",
        "blocker は存在しません",
        "blockerは存在しません",
        "blocker の記述がありません",
        "blockerの記述がありません",
        "ブロッカーは存在しません",
        "ブロッカーなし",
        "blocker はありません",
        "blockerはありません",
        "blocker確認済み",
        "復旧対象の blocker は残ってません",
        "product ambiguity is resolved",
        "product decision is now clear",
        "build target is sekret",
        "human confirmed",
        "next action:",
        "start p1_t1",
    )
    if _contains_any(lowered, non_blocker_markers):
        return False
    return _contains_any(lowered, BLOCKER_MARKERS)


def _clears_blocker_and_points_forward(content: str) -> bool:
    lowered = content.lower()
    clear_markers = (
        "no blocker remains",
        "blocker resolved",
        "product decision is now clear",
        "human confirmed",
    )
    forward_markers = (
        "next action",
        "start p1_t1",
        "phase 1",
        "tasks.json",
    )
    return _contains_any(lowered, clear_markers) and _contains_any(lowered, forward_markers)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def _peer(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


def _excerpt(text: str, limit: int = 500) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit].rstrip()}..."
