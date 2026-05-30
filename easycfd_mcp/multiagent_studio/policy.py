"""LLM-backed orchestration policy with deterministic action application."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .orchestrator import create_context_packet, summarize_task
from .protocol import Artifact, Message, Review, TaskState, WorkItem, parse_iso
from .store import MissionBoard


ALLOWED_ACTIONS = {
    "message",
    "assign_work",
    "update_work",
    "add_artifact",
    "add_review",
    "decision",
    "request_context",
}


@dataclass
class PolicyAction:
    type: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyResult:
    raw_output: str
    actions: list[PolicyAction]
    applied: list[str]
    errors: list[str]


class PolicyError(RuntimeError):
    pass


def call_codex_llm(prompt: str, model: str = "gpt-5.4", timeout: int = 180) -> str:
    """Call local Codex CLI as the first LLM policy backend."""
    cmd = [
        "codex",
        "exec",
        "-m",
        model,
        "-s",
        "workspace-write",
        prompt,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise PolicyError(
            f"Codex policy call failed with exit code {result.returncode}\n"
            f"stderr: {result.stderr}\nstdout: {result.stdout}"
        )
    return result.stdout.strip()


def build_policy_prompt(
    board: MissionBoard,
    task: TaskState,
    stalled_agents: list[str],
) -> str:
    task_json = json.dumps(task.to_dict(), indent=2, ensure_ascii=False)
    claude_packet = create_context_packet(board, task, "claude", recent_messages=8)
    codex_packet = create_context_packet(board, task, "codex", recent_messages=8)
    return f"""You are the neutral orchestrator for a peer multi-agent workspace.

Your job:
- Keep progress moving without waiting for the human unless the task is truly blocked.
- Preserve Claude and Codex as peers.
- If an agent is stale or stopped responding, hand off its current useful next step to the other agent.
- Use sub-agents only for narrow reports, not final decisions.
- Output only JSON. No markdown.

Allowed action types:
- message: {{sender, recipient, message_type, content, refs?}}
- assign_work: {{owner, title, description, depends_on?}}
- update_work: {{work_id, status, artifact_ids?}}
- add_artifact: {{title, kind, owner, path?, content?, metadata?}}
- add_review: {{artifact_id, reviewer, verdict, notes, required_changes?}}
- decision: {{content}}
- request_context: {{agent, reason}}

Prefer small concrete actions. Do not invent completed work. Do not mark work
done unless a message/artifact proves it. If no agent is stale, choose the next
small progress action. If an agent is stale, create a handoff message and assign
the other agent a concrete continuation task.

Stalled agents: {stalled_agents}

Task summary:
{summarize_task(task)}

Full task JSON:
{task_json}

Claude packet:
{claude_packet}

Codex packet:
{codex_packet}

Return JSON with this exact shape:
{{
  "actions": [
    {{"type": "assign_work", "params": {{"owner": "codex", "title": "...", "description": "..."}}}}
  ]
}}
"""


def parse_policy_actions(raw_output: str) -> list[PolicyAction]:
    data = _extract_json(raw_output)
    actions = data.get("actions", [])
    if not isinstance(actions, list):
        raise PolicyError("Policy output must contain an actions list")

    parsed = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise PolicyError(f"Action {index} is not an object")
        action_type = action.get("type")
        params = action.get("params", {})
        if action_type not in ALLOWED_ACTIONS:
            raise PolicyError(f"Action {index} has unsupported type: {action_type}")
        if not isinstance(params, dict):
            raise PolicyError(f"Action {index} params must be an object")
        parsed.append(PolicyAction(type=action_type, params=params))
    return parsed


def _extract_json(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def find_stalled_agents(task: TaskState, stale_after_seconds: int) -> list[str]:
    now = datetime.now(timezone.utc)
    stalled = []
    for agent in ("claude", "codex"):
        activity = task.agent_activity.get(agent)
        owned_open_work = [
            item
            for item in task.work_items
            if item.owner == agent and item.status in ("todo", "in_progress", "blocked")
        ]
        if not owned_open_work:
            continue
        if activity is None:
            stalled.append(agent)
            continue
        age = (now - parse_iso(activity.last_seen_at)).total_seconds()
        if age >= stale_after_seconds:
            stalled.append(agent)
    return stalled


def apply_policy_actions(
    board: MissionBoard,
    task_id: str,
    actions: list[PolicyAction],
) -> tuple[TaskState, list[str], list[str]]:
    task = board.load(task_id)
    applied = []
    errors = []

    for action in actions:
        try:
            task = _apply_one(board, task, action)
            applied.append(action.type)
        except Exception as exc:
            errors.append(f"{action.type}: {exc}")
            task = board.load(task_id)

    return task, applied, errors


def _apply_one(board: MissionBoard, task: TaskState, action: PolicyAction) -> TaskState:
    params = action.params
    if action.type == "message":
        sender = params.get("sender", "orchestrator")
        message = Message(
            type=params.get("message_type", "chat"),
            sender=sender,
            recipient=params.get("recipient", "all"),
            content=_required(params, "content"),
            refs=list(params.get("refs", [])),
            metadata={"actor": sender, "source": "policy_action"},
        )
        return board.append_message(task.id, message)

    if action.type == "assign_work":
        item = WorkItem(
            title=_required(params, "title"),
            owner=_required(params, "owner"),
            description=params.get("description", ""),
            depends_on=list(params.get("depends_on", [])),
        )
        return board.add_work_item(task.id, item)

    if action.type == "update_work":
        return board.update_work_item(
            task_id=task.id,
            work_id=_required(params, "work_id"),
            status=params.get("status"),
            artifact_ids=list(params.get("artifact_ids", [])),
        )

    if action.type == "add_artifact":
        artifact = Artifact(
            title=_required(params, "title"),
            kind=params.get("kind", "other"),
            owner=params.get("owner", "orchestrator"),
            path=params.get("path", ""),
            content=params.get("content", ""),
            metadata=dict(params.get("metadata", {})),
        )
        return board.add_artifact(task.id, artifact)

    if action.type == "add_review":
        review = Review(
            artifact_id=_required(params, "artifact_id"),
            reviewer=_required(params, "reviewer"),
            verdict=_required(params, "verdict"),
            notes=_required(params, "notes"),
            required_changes=list(params.get("required_changes", [])),
        )
        return board.add_review(task.id, review)

    if action.type == "decision":
        content = _required(params, "content")
        task.decisions.append(content)
        task.messages.append(
            Message(type="decision", sender="orchestrator", content=content)
        )
        board.save(task)
        return task

    if action.type == "request_context":
        agent = _required(params, "agent")
        reason = params.get("reason", "Context requested by orchestrator.")
        return board.append_message(
            task.id,
            Message(
                type="handoff",
                sender="orchestrator",
                recipient=agent,
                content=reason,
            ),
        )

    raise PolicyError(f"Unsupported action type: {action.type}")


def _required(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"Missing required string param: {key}")
    return value.strip()


def run_policy_once(
    board: MissionBoard,
    task_id: str,
    model: str = "gpt-5.4",
    stale_after_seconds: int = 900,
    dry_run: bool = False,
) -> PolicyResult:
    task = board.load(task_id)
    stalled = find_stalled_agents(task, stale_after_seconds)
    prompt = build_policy_prompt(board, task, stalled)
    raw = call_codex_llm(prompt, model=model)
    actions = parse_policy_actions(raw)

    if dry_run:
        return PolicyResult(
            raw_output=raw,
            actions=actions,
            applied=[],
            errors=[],
        )

    _, applied, errors = apply_policy_actions(board, task_id, actions)
    return PolicyResult(
        raw_output=raw,
        actions=actions,
        applied=applied,
        errors=errors,
    )
