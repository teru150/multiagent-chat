#!/usr/bin/env python3
"""MCP server for the neutral multi-agent studio."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .agent_runner import dispatch_message, dispatch_work_item
from .autopilot import run_autopilot_once
from .orchestrator import bootstrap_work_items, create_context_packet, summarize_task
from .policy import find_stalled_agents, run_policy_once
from .protocol import Artifact, Message, Review, WorkItem
from .store import MissionBoard


BOARD = MissionBoard()
app = Server("multiagent-studio")


def text(value: str) -> list[TextContent]:
    return [TextContent(type="text", text=value)]


TOOLS = [
    Tool(
        name="studio_create_task",
        description=(
            "Create a neutral multi-agent task. Returns the task id and initial "
            "context packets for Claude and Codex."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "user_message": {"type": "string", "default": ""},
                "constraints": {"type": "array", "items": {"type": "string"}, "default": []},
                "workspace_path": {
                    "type": "string",
                    "default": "",
                    "description": "Target repository path. Mission board state remains in this server's .multiagent directory.",
                },
                "bootstrap": {
                    "type": "boolean",
                    "description": "Add default Claude/Codex/orchestrator work items.",
                    "default": True,
                },
            },
            "required": ["goal"],
        },
    ),
    Tool(
        name="studio_list_tasks",
        description="List known multi-agent tasks.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="studio_get_context",
        description="Get a role-specific context packet for an agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "agent": {"type": "string"},
                "recent_messages": {"type": "integer", "default": 12},
            },
            "required": ["task_id", "agent"],
        },
    ),
    Tool(
        name="studio_submit_message",
        description="Submit a structured message from any agent to the mission board.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "sender": {"type": "string"},
                "message_type": {"type": "string", "default": "chat"},
                "content": {"type": "string"},
                "recipient": {"type": "string", "default": "all"},
                "refs": {"type": "array", "items": {"type": "string"}, "default": []},
                "metadata": {"type": "object", "default": {}},
            },
            "required": ["task_id", "sender", "content"],
        },
    ),
    Tool(
        name="studio_assign_work",
        description="Assign a concrete work item to Claude, Codex, orchestrator, or a sub-agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "owner": {"type": "string"},
                "description": {"type": "string", "default": ""},
                "depends_on": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["task_id", "title", "owner"],
        },
    ),
    Tool(
        name="studio_update_work",
        description="Update work item status and attach artifact ids.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "work_id": {"type": "string"},
                "status": {"type": "string"},
                "artifact_ids": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["task_id", "work_id"],
        },
    ),
    Tool(
        name="studio_add_artifact",
        description="Register a patch, report, doc, test log, or other artifact.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "kind": {"type": "string", "default": "other"},
                "owner": {"type": "string", "default": ""},
                "path": {"type": "string", "default": ""},
                "content": {"type": "string", "default": ""},
                "metadata": {"type": "object", "default": {}},
            },
            "required": ["task_id", "title"],
        },
    ),
    Tool(
        name="studio_add_review",
        description="Record a review verdict for an artifact.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "artifact_id": {"type": "string"},
                "reviewer": {"type": "string"},
                "verdict": {"type": "string"},
                "notes": {"type": "string"},
                "required_changes": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["task_id", "artifact_id", "reviewer", "verdict", "notes"],
        },
    ),
    Tool(
        name="studio_write_memory",
        description="Write project, human, or agent memory used in future context packets.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["name", "content"],
        },
    ),
    Tool(
        name="studio_heartbeat",
        description=(
            "Record that an agent is still alive. Use this periodically so the "
            "orchestrator does not hand off its work."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "agent": {"type": "string"},
                "status": {"type": "string", "default": "active"},
                "current_work_id": {"type": "string", "default": ""},
                "note": {"type": "string", "default": ""},
            },
            "required": ["task_id", "agent"],
        },
    ),
    Tool(
        name="studio_check_stalled",
        description="Return Claude/Codex agents with open work and stale heartbeats.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "stale_after_seconds": {"type": "integer", "default": 900},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="studio_run_orchestrator",
        description=(
            "Run one orchestrator step. By default this uses deterministic "
            "autopilot planning; pass mode=policy to use the LLM-backed JSON policy."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "trigger_message_id": {"type": "string", "default": ""},
                "mode": {"type": "string", "default": "autopilot"},
                "model": {"type": "string", "default": "gpt-5.4"},
                "stale_after_seconds": {"type": "integer", "default": 900},
                "scan_backlog": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["task_id"],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    args = arguments or {}
    try:
        if name == "studio_create_task":
            return create_task(args)
        if name == "studio_list_tasks":
            return list_tasks()
        if name == "studio_get_context":
            return get_context(args)
        if name == "studio_submit_message":
            return submit_message(args)
        if name == "studio_assign_work":
            return assign_work(args)
        if name == "studio_update_work":
            return update_work(args)
        if name == "studio_add_artifact":
            return add_artifact(args)
        if name == "studio_add_review":
            return add_review(args)
        if name == "studio_write_memory":
            return write_memory(args)
        if name == "studio_heartbeat":
            return heartbeat(args)
        if name == "studio_check_stalled":
            return check_stalled(args)
        if name == "studio_run_orchestrator":
            return run_orchestrator(args)
    except Exception as exc:
        return text(f"Error in {name}: {exc}")
    return text(f"Unknown tool: {name}")


def create_task(args: dict[str, Any]) -> list[TextContent]:
    task = BOARD.create_task(
        goal=args["goal"],
        constraints=list(args.get("constraints", [])),
        user_message=args.get("user_message", ""),
        workspace_path=args.get("workspace_path", ""),
    )
    if args.get("bootstrap", True):
        for item in bootstrap_work_items(task.goal):
            task.work_items.append(item)
        BOARD.save(task)

    response = {
        "task": task.to_dict(),
        "claude_context": create_context_packet(BOARD, task, "claude"),
        "codex_context": create_context_packet(BOARD, task, "codex"),
    }
    return text(json.dumps(response, indent=2, ensure_ascii=False))


def list_tasks() -> list[TextContent]:
    return text("\n\n".join(summarize_task(task) for task in BOARD.list_tasks()) or "No tasks.")


def get_context(args: dict[str, Any]) -> list[TextContent]:
    task = BOARD.load(args["task_id"])
    return text(
        create_context_packet(
            BOARD,
            task,
            args["agent"],
            int(args.get("recent_messages", 12)),
        )
    )


def submit_message(args: dict[str, Any]) -> list[TextContent]:
    sender = args["sender"]
    metadata = dict(args.get("metadata", {}))
    metadata.setdefault("actor", sender)
    message = Message(
        type=args.get("message_type", "chat"),
        sender=sender,
        recipient=args.get("recipient", "all"),
        content=args["content"],
        refs=list(args.get("refs", [])),
        metadata=metadata,
    )
    task = BOARD.append_message(args["task_id"], message)
    dispatched = dispatch_message(BOARD, args["task_id"], message.id)
    suffix = f"\n\nAuto-dispatched: {', '.join(dispatched)}" if dispatched else ""
    return text(f"Recorded {message.id}{suffix}\n\n{summarize_task(BOARD.load(args['task_id']))}")


def assign_work(args: dict[str, Any]) -> list[TextContent]:
    item = WorkItem(
        title=args["title"],
        owner=args["owner"],
        description=args.get("description", ""),
        depends_on=list(args.get("depends_on", [])),
    )
    task = BOARD.add_work_item(args["task_id"], item)
    dispatched = dispatch_work_item(BOARD, args["task_id"], item.id)
    suffix = f"\n\nAuto-dispatched: {dispatched}" if dispatched else ""
    return text(f"Assigned {item.id}{suffix}\n\n{summarize_task(BOARD.load(args['task_id']))}")


def update_work(args: dict[str, Any]) -> list[TextContent]:
    task = BOARD.update_work_item(
        task_id=args["task_id"],
        work_id=args["work_id"],
        status=args.get("status"),
        artifact_ids=list(args.get("artifact_ids", [])),
    )
    return text(summarize_task(task))


def add_artifact(args: dict[str, Any]) -> list[TextContent]:
    artifact = Artifact(
        title=args["title"],
        kind=args.get("kind", "other"),
        owner=args.get("owner", ""),
        path=args.get("path", ""),
        content=args.get("content", ""),
        metadata=dict(args.get("metadata", {})),
    )
    task = BOARD.add_artifact(args["task_id"], artifact)
    return text(f"Artifact {artifact.id} registered\n\n{summarize_task(task)}")


def add_review(args: dict[str, Any]) -> list[TextContent]:
    review = Review(
        artifact_id=args["artifact_id"],
        reviewer=args["reviewer"],
        verdict=args["verdict"],
        notes=args["notes"],
        required_changes=list(args.get("required_changes", [])),
    )
    task = BOARD.add_review(args["task_id"], review)
    return text(f"Review {review.id} recorded\n\n{summarize_task(task)}")


def write_memory(args: dict[str, Any]) -> list[TextContent]:
    name = Path(args["name"]).stem
    BOARD.write_memory(name, args["content"])
    return text(f"Wrote memory: {name}")


def heartbeat(args: dict[str, Any]) -> list[TextContent]:
    task = BOARD.record_heartbeat(
        task_id=args["task_id"],
        agent=args["agent"],
        status=args.get("status", "active"),
        current_work_id=args.get("current_work_id", ""),
        note=args.get("note", ""),
    )
    return text(summarize_task(task))


def check_stalled(args: dict[str, Any]) -> list[TextContent]:
    task = BOARD.load(args["task_id"])
    stalled = find_stalled_agents(
        task,
        stale_after_seconds=int(args.get("stale_after_seconds", 900)),
    )
    return text(json.dumps({"stalled_agents": stalled}, indent=2))


def run_orchestrator(args: dict[str, Any]) -> list[TextContent]:
    mode = args.get("mode", "autopilot")
    if mode == "autopilot":
        result = run_autopilot_once(
            BOARD,
            task_id=args["task_id"],
            trigger_message_id=args.get("trigger_message_id", ""),
            stale_after_seconds=int(args.get("stale_after_seconds", 900)),
            scan_backlog=bool(args.get("scan_backlog", False)),
            apply=not bool(args.get("dry_run", False)),
            dispatch=True,
        )
        payload = {
            "mode": "autopilot",
            "trigger_id": result.trigger_id,
            "planned": [
                {
                    "type": action.type,
                    "work_id": action.work.id if action.work else "",
                    "work_owner": action.work.owner if action.work else "",
                    "work_title": action.work.title if action.work else "",
                    "message_id": action.message.id if action.message else "",
                    "status": action.status,
                    "reason": action.reason,
                }
                for action in result.planned
            ],
            "applied": result.applied,
            "dispatched": result.dispatched,
            "skipped_reason": result.skipped_reason,
        }
        return text(json.dumps(payload, indent=2, ensure_ascii=False))

    if mode != "policy":
        return text(f"Error in studio_run_orchestrator: unsupported mode {mode!r}")

    result = run_policy_once(
        BOARD,
        task_id=args["task_id"],
        model=args.get("model", "gpt-5.4"),
        stale_after_seconds=int(args.get("stale_after_seconds", 900)),
        dry_run=bool(args.get("dry_run", False)),
    )
    payload = {
        "actions": [
            {"type": action.type, "params": action.params}
            for action in result.actions
        ],
        "applied": result.applied,
        "errors": result.errors,
        "raw_output": result.raw_output,
    }
    return text(json.dumps(payload, indent=2, ensure_ascii=False))


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
