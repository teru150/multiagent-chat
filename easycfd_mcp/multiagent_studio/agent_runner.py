"""Launch Claude/Codex from task context and persist their replies."""

from __future__ import annotations

import re
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .autopilot import run_autopilot_once
from .orchestrator import create_context_packet
from .protocol import Message
from .store import MissionBoard


ROOT = Path(__file__).resolve().parents[2]
SESSION_MODE = "hybrid"
DEFAULT_AGENT_TIMEOUT_SECONDS = 300
WORKSPACE_EDIT_TIMEOUT_SECONDS = 900
_ACTIVE_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: dict[str, subprocess.Popen] = {}
_STOP_REQUESTS: set[str] = set()


def infer_target_agents(content: str) -> list[str]:
    """Infer which peer agents the orchestrator should invoke."""
    lowered = content.lower()
    targets = []
    if "claude" in lowered or "クロード" in content:
        targets.append("claude")
    if "codex" in lowered or "コーデックス" in content:
        targets.append("codex")
    if not targets:
        # Default to both agents for collaborative discussion instead of just codex
        # This enables peer review and multi-perspective analysis
        targets = ["claude", "codex"]
    return targets


def dispatch_orchestrator_message(
    board: MissionBoard,
    task_id: str,
    user_message_id: str,
    invoker: Callable[[str, str], str] | None = None,
    background: bool = True,
) -> list[str]:
    """Run one orchestrator cycle for a human message."""
    result = run_autopilot_once(
        board,
        task_id,
        trigger_message_id=user_message_id,
        dispatch=True,
        invoker=invoker,
        background=background,
    )
    return result.dispatched


def dispatch_message(
    board: MissionBoard,
    task_id: str,
    message_id: str,
    invoker: Callable[[str, str], str] | None = None,
    background: bool = True,
) -> list[str]:
    """Launch agents for a human-authored message based on recipient and text."""
    task = board.load(task_id)
    message = next((item for item in task.messages if item.id == message_id), None)
    if message is None:
        raise FileNotFoundError(f"Message not found: {message_id}")
    if message.sender != "human":
        return dispatch_agent_message(
            board,
            task_id,
            message_id,
            invoker=invoker,
            background=background,
        )
    if message.recipient == "orchestrator":
        return dispatch_orchestrator_message(
            board,
            task_id,
            message_id,
            invoker=invoker,
            background=background,
        )
    if message.recipient in ("claude", "codex"):
        return _dispatch_to_agents(
            board,
            task_id,
            message,
            [message.recipient],
            invoker=invoker,
            background=background,
        )
    if message.recipient == "all":
        return _dispatch_to_agents(
            board,
            task_id,
            message,
            ["claude", "codex"],
            invoker=invoker,
            background=background,
        )
    return []


def dispatch_agent_message(
    board: MissionBoard,
    task_id: str,
    message_id: str,
    invoker: Callable[[str, str], str] | None = None,
    background: bool = True,
) -> list[str]:
    """Run one orchestrator cycle for an agent-authored message."""
    result = run_autopilot_once(
        board,
        task_id,
        trigger_message_id=message_id,
        dispatch=True,
        invoker=invoker,
        background=background,
    )
    return result.dispatched


def dispatch_commander_followup(
    board: MissionBoard,
    task_id: str,
    message_id: str,
    invoker: Callable[[str, str], str] | None = None,
    background: bool = True,
) -> str:
    """Compatibility wrapper for the new bounded autopilot cycle."""
    result = run_autopilot_once(
        board,
        task_id,
        trigger_message_id=message_id,
        dispatch=True,
        invoker=invoker,
        background=background,
    )
    return result.dispatched[0] if result.dispatched else ""


def _dispatch_to_agents(
    board: MissionBoard,
    task_id: str,
    source_message: Message,
    targets: list[str],
    invoker: Callable[[str, str], str] | None = None,
    background: bool = True,
) -> list[str]:
    trigger_actor = source_message.metadata.get("actor", source_message.sender)
    unique_targets = []
    for target in targets:
        if target in ("claude", "codex") and target not in unique_targets:
            unique_targets.append(target)
    for agent in unique_targets:
        allow_edits = _should_allow_auto_edits(agent, source_message)
        board.append_message(
            task_id,
            Message(
                type="handoff",
                sender="orchestrator",
                recipient=agent,
                content=(
                    f"The {trigger_actor} sent this to {source_message.recipient}. Continue from the "
                    "shared task context and answer back to the mission board.\n\n"
                    f"{trigger_actor} message: {source_message.content}"
                ),
                refs=[source_message.id],
                metadata={
                    "source": "auto_dispatch",
                    "actor": "orchestrator",
                    "trigger_actor": trigger_actor,
                    "direct_recipient": source_message.recipient,
                    "auto_edit_allowed": allow_edits,
                },
            ),
        )
        write_agent_inbox(board, task_id, agent)
        if background:
            thread = threading.Thread(
                target=_run_and_record,
                args=(board, task_id, agent, source_message.id, invoker, allow_edits, True),
                daemon=True,
            )
            thread.start()
        else:
            _run_and_record(
                board,
                task_id,
                agent,
                source_message.id,
                invoker,
                allow_edits,
                False,
            )
    return unique_targets


def _should_allow_auto_edits(agent: str, source_message: Message) -> bool:
    if agent != "codex":
        return False
    content = source_message.content.lower()
    edit_markers = [
        "implement",
        "fix",
        "change",
        "update",
        "modify",
        "実装",
        "修正",
        "変更",
        "直して",
        "追加",
        "非表示",
        "できるよう",
    ]
    return any(marker in content for marker in edit_markers)


def dispatch_work_item(
    board: MissionBoard,
    task_id: str,
    work_id: str,
    invoker: Callable[[str, str], str] | None = None,
    background: bool = True,
) -> str:
    """Launch the owner of a newly assigned work item."""
    task = board.load(task_id)
    work_item = next((item for item in task.work_items if item.id == work_id), None)
    if work_item is None:
        raise FileNotFoundError(f"Work item not found: {work_id}")
    if work_item.owner not in ("claude", "codex"):
        return ""

    board.append_message(
        task_id,
        Message(
            type="handoff",
            sender="orchestrator",
            recipient=work_item.owner,
            content=(
                "The orchestrator assigned you this work item. Continue from "
                "the shared task context and report back to the mission board.\n\n"
                f"Work item: {work_item.title}\n\n{work_item.description}"
            ).strip(),
            refs=[work_item.id],
            metadata={
                "source": "auto_dispatch",
                "actor": "orchestrator",
                "trigger_actor": "orchestrator",
                "trigger_work_id": work_item.id,
            },
        ),
    )
    write_agent_inbox(board, task_id, work_item.owner)
    if background:
        thread = threading.Thread(
            target=_run_and_record,
            args=(
                board,
                task_id,
                work_item.owner,
                work_item.id,
                invoker,
                work_item.owner == "codex",
                True,
            ),
            daemon=True,
        )
        thread.start()
    else:
        _run_and_record(
            board,
            task_id,
            work_item.owner,
            work_item.id,
            invoker,
            work_item.owner == "codex",
            False,
        )
    return work_item.owner


def write_agent_inbox(board: MissionBoard, task_id: str, agent: str) -> Path:
    """Write a plain Markdown context packet for agents without MCP access."""
    task = board.load(task_id)
    inbox_dir = board.root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / f"{task_id}_{agent}.md"
    path.write_text(create_context_packet(board, task, agent), encoding="utf-8")
    return path


def run_agent_once(agent: str, prompt: str) -> str:
    if agent == "claude":
        return _run_claude(prompt)
    if agent == "codex":
        return _run_codex(prompt)
    raise ValueError(f"Unsupported agent: {agent}")


def run_agent_for_task(
    board: MissionBoard,
    task_id: str,
    agent: str,
    prompt: str,
    allow_edits: bool = False,
) -> tuple[str, dict[str, str]]:
    workspace = task_workspace_path(board, task_id)
    if SESSION_MODE != "hybrid":
        return run_agent_once(agent, prompt), {
            "context_mode": "mission_board_only",
            "workspace_path": str(workspace),
        }
    if agent == "claude":
        return _run_claude_session(board, task_id, prompt, workspace=workspace)
    if agent == "codex":
        return _run_codex_session(
            board,
            task_id,
            prompt,
            allow_edits=allow_edits,
            workspace=workspace,
        )
    raise ValueError(f"Unsupported agent: {agent}")


def task_workspace_path(board: MissionBoard, task_id: str) -> Path:
    task = board.load(task_id)
    raw = task.workspace_path.strip()
    if not raw:
        return ROOT
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    return path.resolve()


def build_agent_prompt(
    board: MissionBoard,
    task_id: str,
    agent: str,
    trigger_id: str,
    allow_edits: bool = False,
) -> str:
    task = board.load(task_id)
    packet = create_context_packet(board, task, agent, recent_messages=18)
    return f"""You are {agent} participating in Multi-Agent Studio.

You were invoked automatically because the human sent a message to the
orchestrator. Read the task packet, respond to the latest relevant handoff, and
produce one concise mission-board message.

Rules:
- You may use your own resumed CLI session for continuity, but the mission-board
  packet below is authoritative if there is any conflict.
- The target workspace shown in the packet is the repository for file edits,
  repo inspection, commands, and tests. The mission board is coordination state.
- If the human asks for an implementation/fix and edits are allowed, execute the
  change now, run focused verification, then report what changed.
- If you will execute work after this response, explicitly write "実行します:" and
  then do it in the same invocation. If you will not execute it, explicitly write
  "実行しません:" with the reason.
- Do not claim to have used a browser unless your platform actually did.
- Edit permission for this invocation: {"allowed" if allow_edits else "not allowed"}.
- If asked to review UI, review from the task state and available URL/context.
- Be concrete: findings, suggested fixes, or next action.
- Output plain text only. No JSON.

Trigger message id: {trigger_id}

{packet}
"""


def _run_and_record(
    board: MissionBoard,
    task_id: str,
    agent: str,
    trigger_id: str,
    invoker: Callable[[str, str], str] | None,
    allow_edits: bool = False,
    followup_background: bool = True,
) -> None:
    try:
        board.record_heartbeat(
            task_id,
            agent=agent,
            status="running",
            note=f"Auto-invoked for {trigger_id}",
        )
        prompt = build_agent_prompt(board, task_id, agent, trigger_id, allow_edits=allow_edits)
        if invoker:
            output = invoker(agent, prompt)
            run_metadata = {"context_mode": "test_invoker"}
        else:
            output, run_metadata = run_agent_for_task(
                board,
                task_id,
                agent,
                prompt,
                allow_edits=allow_edits,
            )
        cleaned = _clean_output(output)
        recorded = Message(
            type="report_result",
            sender=agent,
            recipient="orchestrator",
            content=cleaned,
            refs=[trigger_id],
            metadata={
                "source": "auto_agent",
                "actor": agent,
                "auto_edit_allowed": allow_edits,
                **run_metadata,
            },
        )
        board.append_message(
            task_id,
            recorded,
        )
        board.record_heartbeat(
            task_id,
            agent=agent,
            status="active",
            note=f"Auto response recorded for {trigger_id}",
        )
        write_agent_inbox(board, task_id, agent)
        dispatch_commander_followup(
            board,
            task_id,
            recorded.id,
            invoker=invoker,
            background=followup_background,
        )
    except Exception as exc:
        board.record_heartbeat(
            task_id,
            agent=agent,
            status="error",
            note=f"Auto-invocation failed for {trigger_id}",
        )
        board.append_message(
            task_id,
            Message(
                type="report_result",
                sender="orchestrator",
                recipient="human",
                content=f"Auto-invocation failed for {agent}: {exc}",
                refs=[trigger_id],
                metadata={
                    "source": "auto_dispatch",
                    "actor": "orchestrator",
                    "agent": agent,
                },
            ),
        )


def _run_claude(prompt: str) -> str:
    command = _wrap_command(
        "claude",
        [
            "--print",
            "--output-format",
            "text",
            "--tools",
            "",
        ],
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=prompt,
        cwd=ROOT,
        timeout=DEFAULT_AGENT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _run_claude_session(
    board: MissionBoard,
    task_id: str,
    prompt: str,
    workspace: Path | None = None,
) -> tuple[str, dict[str, str]]:
    workspace = workspace or ROOT
    session = _get_agent_session(board, task_id, "claude")
    session_id = session.get("session_id") or str(uuid4())
    fallback_note = ""
    try:
        output = _run_claude_with_session(
            prompt,
            session_id,
            task_id=task_id,
            workspace=workspace,
        )
        context_mode = "hybrid_resumed_cli" if session.get("session_id") else "hybrid_new_cli"
    except RuntimeError as exc:
        if not _is_claude_session_busy_error(str(exc)):
            raise
        fallback_note = str(exc)
        session_id = str(uuid4())
        output = _run_claude_with_session(
            prompt,
            session_id,
            task_id=task_id,
            workspace=workspace,
        )
        context_mode = "hybrid_new_cli"

    _set_agent_session(
        board,
        task_id,
        "claude",
        {
            "session_id": session_id,
            "backend": "claude --session-id",
            "context_mode": context_mode,
        },
    )
    metadata = {
        "context_mode": context_mode,
        "session_backend": "claude --session-id",
        "session_id": session_id,
        "workspace_path": str(workspace),
    }
    if fallback_note:
        metadata["session_fallback"] = fallback_note[:500]
    return output, metadata


def _run_claude_with_session(
    prompt: str,
    session_id: str,
    task_id: str = "",
    workspace: Path | None = None,
) -> str:
    command = _wrap_command(
        "claude",
        [
            "--print",
            "--output-format",
            "text",
            "--tools",
            "",
            "--session-id",
            session_id,
        ],
    )
    result = _run_registered_command(
        command,
        input=prompt,
        cwd=workspace or ROOT,
        timeout=DEFAULT_AGENT_TIMEOUT_SECONDS,
        task_id=task_id,
        agent="claude",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _is_claude_session_busy_error(message: str) -> bool:
    lowered = message.lower()
    return "session id" in lowered and "already in use" in lowered


def _run_codex(prompt: str) -> str:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        suffix=".txt",
    ) as handle:
        output_path = Path(handle.name)
    command = _wrap_command(
        "codex",
        [
            "exec",
            "-s",
            "read-only",
            "-C",
            str(ROOT),
            "--output-last-message",
            str(output_path),
            "-",
        ],
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=prompt,
            timeout=DEFAULT_AGENT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"{detail}\n\nCodex command: {_display_command(command)}".strip()
            )
        if output_path.exists() and output_path.read_text(encoding="utf-8").strip():
            return output_path.read_text(encoding="utf-8")
        return result.stdout
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass


def _run_codex_session(
    board: MissionBoard,
    task_id: str,
    prompt: str,
    allow_edits: bool = False,
    workspace: Path | None = None,
) -> tuple[str, dict[str, str]]:
    workspace = workspace or ROOT
    sandbox = "workspace-write" if allow_edits else "read-only"
    session = _get_agent_session(board, task_id, "codex")
    if session.get("session_id") and not allow_edits:
        try:
            output, session_id = _run_codex_resume(
                prompt,
                session["session_id"],
                task_id=task_id,
                workspace=workspace,
            )
            _set_agent_session(
                board,
                task_id,
                "codex",
                {
                    "session_id": session_id,
                    "backend": "codex exec resume",
                    "context_mode": "hybrid_resumed_cli",
                },
            )
            return output, {
                "context_mode": "hybrid_resumed_cli",
                "session_backend": "codex exec resume",
                "session_id": session_id,
                "sandbox": sandbox,
                "workspace_path": str(workspace),
            }
        except RuntimeError as exc:
            fallback_note = str(exc)
    else:
        fallback_note = ""

    output, session_id = _run_codex_new_session(
        prompt,
        sandbox=sandbox,
        task_id=task_id,
        workspace=workspace,
    )
    _set_agent_session(
        board,
        task_id,
        "codex",
        {
            "session_id": session_id,
            "backend": "codex exec",
            "context_mode": "hybrid_new_cli",
        },
    )
    metadata = {
        "context_mode": "hybrid_new_cli",
        "session_backend": "codex exec",
        "session_id": session_id,
        "sandbox": sandbox,
        "workspace_path": str(workspace),
    }
    if fallback_note:
        metadata["session_fallback"] = fallback_note[:500]
    return output, metadata


def _run_codex_new_session(
    prompt: str,
    sandbox: str = "read-only",
    task_id: str = "",
    workspace: Path | None = None,
) -> tuple[str, str]:
    workspace = workspace or ROOT
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        suffix=".txt",
    ) as handle:
        output_path = Path(handle.name)
    command = _wrap_command(
        "codex",
        [
            "exec",
            "-s",
            sandbox,
            "-C",
            str(workspace),
            "--json",
            "--output-last-message",
            str(output_path),
            "-",
        ],
    )
    try:
        result = _run_registered_command(
            command,
            input=prompt,
            timeout=WORKSPACE_EDIT_TIMEOUT_SECONDS if sandbox == "workspace-write" else DEFAULT_AGENT_TIMEOUT_SECONDS,
            task_id=task_id,
            agent="codex",
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"{detail}\n\nCodex command: {_display_command(command)}".strip()
            )
        session_id = _extract_codex_session_id(result.stdout)
        output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        return output or result.stdout, session_id
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass


def _run_codex_resume(
    prompt: str,
    session_id: str,
    task_id: str = "",
    workspace: Path | None = None,
) -> tuple[str, str]:
    workspace = workspace or ROOT
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        suffix=".txt",
    ) as handle:
        output_path = Path(handle.name)
    command = _wrap_command(
        "codex",
        [
            "exec",
            "resume",
            "--json",
            "--output-last-message",
            str(output_path),
            session_id,
            "-",
        ],
    )
    try:
        result = _run_registered_command(
            command,
            input=prompt,
            timeout=DEFAULT_AGENT_TIMEOUT_SECONDS,
            cwd=workspace,
            task_id=task_id,
            agent="codex",
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"{detail}\n\nCodex command: {_display_command(command)}".strip()
            )
        resumed_id = _extract_codex_session_id(result.stdout) or session_id
        output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        return output or result.stdout, resumed_id
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass


def _extract_codex_session_id(output: str) -> str:
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return ""


def request_stop_agents(
    board: MissionBoard,
    *,
    task_id: str | None = None,
    agent: str = "all",
) -> list[str]:
    """Stop active auto-invoked CLI processes and mark them as stopped."""
    normalized_agent = agent.strip().lower() or "all"
    if normalized_agent not in {"all", "claude", "codex"}:
        raise ValueError("agent must be one of: all, claude, codex")

    stopped: list[str] = []
    with _ACTIVE_PROCESS_LOCK:
        keys = [
            key
            for key in _ACTIVE_PROCESSES
            if _matches_process_key(key, task_id=task_id, agent=normalized_agent)
        ]
        for key in keys:
            _STOP_REQUESTS.add(key)
            process = _ACTIVE_PROCESSES.get(key)
            if process and process.poll() is None:
                process.terminate()
                stopped.append(key)

    for key in stopped:
        key_task, key_agent = _split_process_key(key)
        try:
            board.record_heartbeat(
                key_task,
                agent=key_agent,
                status="stopped",
                note="Force-stopped by human",
            )
        except FileNotFoundError:
            pass
    for task in board.list_tasks():
        if task_id and task.id != task_id:
            continue
        changed = False
        for key_agent, activity in task.agent_activity.items():
            if normalized_agent != "all" and key_agent != normalized_agent:
                continue
            if key_agent in {"claude", "codex"} and activity.status == "running":
                activity.status = "stopped"
                activity.note = "Marked stopped by human request"
                changed = True
                stopped_key = f"{task.id}:{key_agent}"
                if stopped_key not in stopped:
                    stopped.append(stopped_key)
        if changed:
            board.save(task)
    return stopped


def _run_registered_command(
    command: list[str],
    *,
    input: str,
    timeout: int,
    task_id: str = "",
    agent: str = "",
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    key = _process_key(task_id, agent) if task_id and agent else ""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    if key:
        with _ACTIVE_PROCESS_LOCK:
            _ACTIVE_PROCESSES[key] = process
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise
    finally:
        if key:
            with _ACTIVE_PROCESS_LOCK:
                _ACTIVE_PROCESSES.pop(key, None)
                _STOP_REQUESTS.discard(key)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _process_key(task_id: str, agent: str) -> str:
    return f"{task_id}:{agent}"


def _split_process_key(key: str) -> tuple[str, str]:
    task_id, _, agent = key.partition(":")
    return task_id, agent


def _matches_process_key(key: str, *, task_id: str | None, agent: str) -> bool:
    key_task, key_agent = _split_process_key(key)
    if task_id and key_task != task_id:
        return False
    if agent != "all" and key_agent != agent:
        return False
    return True


def _clean_output(output: str) -> str:
    text = output.strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text or "(empty response)"


def _wrap_command(command: str, args: list[str]) -> list[str]:
    resolved = _resolve_command(command)
    if resolved is None:
        candidate = Path.home() / "AppData" / "Roaming" / "npm" / f"{command}.cmd"
        if candidate.exists():
            resolved = str(candidate)
    if resolved is None:
        candidate = Path.home() / "AppData" / "Roaming" / "npm" / f"{command}.ps1"
        if candidate.exists():
            resolved = str(candidate)
    if resolved and resolved.lower().endswith(".ps1"):
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            resolved,
            *args,
        ]
    if resolved and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", resolved, *args]
    return [resolved or command, *args]


def runner_diagnostics() -> dict[str, object]:
    codex_command = _wrap_command("codex", ["--version"])
    return {
        "codex_command": codex_command,
        "codex_executable": codex_command[0],
        "native_codex": str(_find_native_codex() or ""),
        "root": str(ROOT),
        "session_mode": SESSION_MODE,
    }


def _display_command(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)


def _agent_sessions_path(board: MissionBoard) -> Path:
    return board.root / "agent_sessions.json"


def _load_agent_sessions(board: MissionBoard) -> dict[str, dict[str, str]]:
    path = _agent_sessions_path(board)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in data.items()
        if isinstance(value, dict)
    }


def _save_agent_sessions(board: MissionBoard, sessions: dict[str, dict[str, str]]) -> None:
    path = _agent_sessions_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8")


def _agent_session_key(task_id: str, agent: str) -> str:
    return f"{task_id}:{agent}"


def _get_agent_session(board: MissionBoard, task_id: str, agent: str) -> dict[str, str]:
    return _load_agent_sessions(board).get(_agent_session_key(task_id, agent), {})


def _set_agent_session(
    board: MissionBoard,
    task_id: str,
    agent: str,
    session: dict[str, str],
) -> None:
    sessions = _load_agent_sessions(board)
    sessions[_agent_session_key(task_id, agent)] = session
    _save_agent_sessions(board, sessions)


def reset_agent_sessions(
    board: MissionBoard,
    *,
    task_id: str | None = None,
    agent: str = "all",
) -> list[str]:
    """Remove saved CLI session IDs so the next invocation starts fresh."""
    normalized_agent = agent.strip().lower() or "all"
    if normalized_agent not in {"all", "claude", "codex"}:
        raise ValueError("agent must be one of: all, claude, codex")

    sessions = _load_agent_sessions(board)
    removed: list[str] = []
    for key in list(sessions):
        key_task_id, _, key_agent = key.partition(":")
        if task_id and key_task_id != task_id:
            continue
        if normalized_agent != "all" and key_agent != normalized_agent:
            continue
        removed.append(key)
        del sessions[key]

    if removed:
        _save_agent_sessions(board, sessions)
    return removed


def _resolve_command(command: str) -> str | None:
    if command == "codex":
        native = _find_native_codex()
        if native:
            return str(native)
    resolved = shutil.which(command)
    if resolved:
        return resolved
    candidate = Path.home() / "AppData" / "Roaming" / "npm" / f"{command}.cmd"
    if candidate.exists():
        return str(candidate)
    candidate = Path.home() / "AppData" / "Roaming" / "npm" / f"{command}.ps1"
    if candidate.exists():
        return str(candidate)
    return None


def _find_native_codex() -> Path | None:
    root = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    if not root.exists():
        return None
    candidates = sorted(
        root.glob("*/codex.exe"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None
