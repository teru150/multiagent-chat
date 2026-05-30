#!/usr/bin/env python3
"""Local dashboard server for Multi-Agent Studio task files."""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .agent_runner import (
    dispatch_message,
    request_stop_agents,
    reset_agent_sessions,
    runner_diagnostics,
)
from .autopilot import run_autopilot_once
from .orchestrator import create_context_packet, summarize_task
from .progress import build_progress_report
from .protocol import Message
from .store import MissionBoard


ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "ui"
BOARD = MissionBoard(ROOT / ".multiagent")
AUTOPILOT_INTERVAL_SECONDS = 30
_AUTOPILOT_THREAD_STARTED = False
_AUTOPILOT_THREAD_LOCK = threading.Lock()


class StudioUiHandler(BaseHTTPRequestHandler):
    server_version = "MultiAgentStudioUI/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/":
            self._serve_static("index.html")
            return
        if path == "/progress":
            self._serve_static("progress.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path.removeprefix("/static/"))
            return
        if path == "/api/health":
            self._send_json({"ok": True, "runner": runner_diagnostics()})
            return
        if path == "/api/tasks":
            self._send_json({"tasks": [task_summary(task) for task in BOARD.list_tasks()]})
            return
        if path.startswith("/api/tasks/") and path.endswith("/progress"):
            task_id = path.removeprefix("/api/tasks/").removesuffix("/progress").strip("/")
            task = BOARD.load(task_id)
            self._send_json(build_progress_report(task))
            return
        if path.startswith("/api/tasks/"):
            task_id = path.removeprefix("/api/tasks/").strip("/")
            self._send_task(task_id)
            return
        if path.startswith("/api/context/"):
            parts = path.removeprefix("/api/context/").strip("/").split("/")
            if len(parts) != 2:
                self._send_error(404, "Expected /api/context/{task_id}/{agent}")
                return
            task_id, agent = parts
            task = BOARD.load(task_id)
            self._send_json({"context": create_context_packet(BOARD, task, agent)})
            return
        self._send_error(404, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/reset_session":
            try:
                payload = self._read_json_body()
                task_id = str(payload.get("task_id", "")).strip() or None
                agent = str(payload.get("agent", "all")).strip() or "all"
                removed = reset_agent_sessions(BOARD, task_id=task_id, agent=agent)
            except ValueError as exc:
                self._send_error(400, str(exc))
                return
            self._send_json({"ok": True, "agent": agent, "task_id": task_id, "removed": removed})
            return
        if path == "/api/stop_agents":
            try:
                payload = self._read_json_body()
                task_id = str(payload.get("task_id", "")).strip() or None
                agent = str(payload.get("agent", "all")).strip() or "all"
                stopped = request_stop_agents(BOARD, task_id=task_id, agent=agent)
            except ValueError as exc:
                self._send_error(400, str(exc))
                return
            self._send_json({"ok": True, "agent": agent, "task_id": task_id, "stopped": stopped})
            return
        if path.startswith("/api/tasks/") and path.endswith("/messages"):
            task_id = path.removeprefix("/api/tasks/").removesuffix("/messages").strip("/")
            try:
                payload = self._read_json_body()
                task = append_browser_message(BOARD, task_id, payload)
            except ValueError as exc:
                self._send_error(400, str(exc))
                return
            except FileNotFoundError as exc:
                self._send_error(404, str(exc))
                return
            dispatched = dispatch_message(
                BOARD,
                task_id,
                task.messages[-1].id,
            )
            self._send_json(
                {
                    "ok": True,
                    "task": task_summary(BOARD.load(task_id)),
                    "dispatched_agents": dispatched,
                }
            )
            return
        self._send_error(404, "Not found")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("Request body is required")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _send_task(self, task_id: str) -> None:
        task = BOARD.load(task_id)
        payload = task.to_dict()
        payload["summary"] = summarize_task(task)
        payload["message_summaries"] = [
            {
                **message.to_dict(),
                "summary": summarize_text(message.content),
                "word_count": len(message.content.split()),
                "char_count": len(message.content),
            }
            for message in task.messages
        ]
        self._send_json(payload)

    def _serve_static(self, relative_path: str) -> None:
        target = (STATIC_DIR / relative_path).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self._send_error(403, "Forbidden")
            return
        if not target.exists() or not target.is_file():
            self._send_error(404, "Static file not found")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type = f"{content_type}; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: int, message: str) -> None:
        data = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def summarize_text(text: str, limit: int = 180) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    sentence_end = min(
        [pos for pos in (normalized.find(". "), normalized.find("? "), normalized.find("! ")) if pos != -1]
        or [limit]
    )
    if sentence_end < limit:
        return normalized[: sentence_end + 1].rstrip()
    return normalized[:limit].rstrip() + "..."


def task_summary(task) -> dict:
    active_agents = {
        agent: activity.to_dict()
        for agent, activity in task.agent_activity.items()
    }
    return {
        "id": task.id,
        "goal": task.goal,
        "status": task.status,
        "workspace_path": task.workspace_path,
        "created_at": task.created_at,
        "message_count": len(task.messages),
        "work_count": len(task.work_items),
        "artifact_count": len(task.artifacts),
        "review_count": len(task.reviews),
        "active_agents": active_agents,
        "latest_message": summarize_text(task.messages[-1].content) if task.messages else "",
    }


def append_browser_message(board: MissionBoard, task_id: str, payload: dict):
    content = str(payload.get("content", "")).strip()
    if not content:
        raise ValueError("Message content is required")
    requested_sender = str(payload.get("sender", "human")).strip() or "human"
    if requested_sender != "human":
        raise ValueError("Dashboard messages must be sent as human")
    recipient = str(payload.get("recipient", "orchestrator")).strip() or "orchestrator"
    message_type = str(payload.get("message_type", "chat")).strip() or "chat"
    refs = payload.get("refs", [])
    if not isinstance(refs, list):
        raise ValueError("refs must be a list")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    metadata = {
        **metadata,
        "source": "dashboard",
        "actor": "human",
        "identity_locked": True,
    }
    return board.append_message(
        task_id,
        Message(
            type=message_type,
            sender="human",
            recipient=recipient,
            content=content,
            refs=[str(ref) for ref in refs],
            metadata=metadata,
        ),
    )


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    start_autopilot_watchdog()
    server = ThreadingHTTPServer((host, port), StudioUiHandler)
    print(f"Multi-Agent Studio UI: http://{host}:{port}")
    server.serve_forever()


def start_autopilot_watchdog() -> None:
    global _AUTOPILOT_THREAD_STARTED
    with _AUTOPILOT_THREAD_LOCK:
        if _AUTOPILOT_THREAD_STARTED:
            return
        thread = threading.Thread(target=_autopilot_watchdog_loop, daemon=True)
        thread.start()
        _AUTOPILOT_THREAD_STARTED = True


def _autopilot_watchdog_loop() -> None:
    while True:
        time.sleep(AUTOPILOT_INTERVAL_SECONDS)
        for task in BOARD.list_tasks():
            if not _watchdog_enabled(task):
                continue
            try:
                run_autopilot_once(
                    BOARD,
                    task.id,
                    scan_backlog=True,
                    stale_after_seconds=300,
                    background=True,
                )
            except Exception as exc:
                try:
                    BOARD.append_message(
                        task.id,
                        Message(
                            type="report_result",
                            sender="orchestrator",
                            recipient="human",
                            content=f"Autopilot watchdog failed for {task.id}: {exc}",
                            metadata={"source": "autopilot_watchdog", "actor": "orchestrator"},
                        ),
                    )
                except Exception:
                    pass


def _watchdog_enabled(task) -> bool:
    if task.status != "in_progress":
        return False
    return bool(str(getattr(task, "workspace_path", "")).strip())


if __name__ == "__main__":
    run()
