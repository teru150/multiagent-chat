import tempfile
import unittest
from pathlib import Path

from easycfd_mcp.multiagent_studio.orchestrator import (
    bootstrap_work_items,
    create_context_packet,
)
from easycfd_mcp.multiagent_studio.agent_runner import (
    _extract_codex_session_id,
    _get_agent_session,
    _is_claude_session_busy_error,
    _set_agent_session,
    _wrap_command,
    dispatch_commander_followup,
    dispatch_message,
    dispatch_orchestrator_message,
    dispatch_work_item,
    reset_agent_sessions,
    task_workspace_path,
    write_agent_inbox,
)
from easycfd_mcp.multiagent_studio.autopilot import run_autopilot_once
from easycfd_mcp.multiagent_studio.policy import (
    PolicyAction,
    apply_policy_actions,
    find_stalled_agents,
    parse_policy_actions,
)
from easycfd_mcp.multiagent_studio.progress import build_progress_report
from easycfd_mcp.multiagent_studio.protocol import Artifact, Message, Review, WorkItem
from easycfd_mcp.multiagent_studio.store import MissionBoard
from easycfd_mcp.multiagent_studio.ui_server import (
    append_browser_message,
    summarize_text,
    task_summary,
)


class MultiAgentStudioTests(unittest.TestCase):
    def test_task_round_trip_and_context_packet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(
                goal="Build a peer multi-agent workflow",
                constraints=["Claude and Codex must receive symmetric task state"],
            )

            for item in bootstrap_work_items(task.goal):
                task.work_items.append(item)
            board.save(task)

            task = board.append_message(
                task.id,
                Message(
                    type="proposal",
                    sender="claude",
                    content="Define success criteria before implementation.",
                ),
            )
            task = board.append_message(
                task.id,
                Message(
                    type="proposal",
                    sender="codex",
                    content="Inspect repo and add a file-backed protocol layer.",
                ),
            )

            packet = create_context_packet(board, task, "codex")

            self.assertIn("Build a peer multi-agent workflow", packet)
            self.assertIn("Claude and Codex must receive symmetric task state", packet)
            self.assertIn("Define success criteria before implementation.", packet)
            self.assertIn("Inspect repo and add a file-backed protocol layer.", packet)
            self.assertIn("> [todo]", packet)

    def test_task_workspace_path_is_exported_to_context_packet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "target_repo"
            workspace.mkdir()
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(
                goal="Build another repository",
                workspace_path=str(workspace),
            )

            packet = create_context_packet(board, task, "codex")

            self.assertEqual(board.load(task.id).workspace_path, str(workspace))
            self.assertIn("## Workspace", packet)
            self.assertIn(str(workspace.resolve()), packet)
            self.assertEqual(task_workspace_path(board, task.id), workspace.resolve())

    def test_artifact_review_and_work_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Review artifacts")

            item = WorkItem(title="Implement protocol", owner="codex")
            task = board.add_work_item(task.id, item)

            artifact = Artifact(
                title="Protocol patch",
                kind="patch",
                owner="codex",
                path="easycfd_mcp/multiagent_studio/protocol.py",
            )
            task = board.add_artifact(task.id, artifact)
            task = board.update_work_item(
                task_id=task.id,
                work_id=item.id,
                status="done",
                artifact_ids=[artifact.id],
            )
            task = board.add_review(
                task.id,
                Review(
                    artifact_id=artifact.id,
                    reviewer="claude",
                    verdict="approve",
                    notes="The artifact matches the intended peer model.",
                ),
            )

            saved = board.load(task.id)

            self.assertEqual(saved.work_items[0].status, "done")
            self.assertEqual(saved.work_items[0].artifacts, [artifact.id])
            self.assertEqual(saved.reviews[0].verdict, "approve")

    def test_stalled_agent_detection_and_policy_handoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Keep progress moving")
            task = board.add_work_item(
                task.id,
                WorkItem(title="Review implementation", owner="claude"),
            )

            stalled = find_stalled_agents(task, stale_after_seconds=1)

            self.assertEqual(stalled, ["claude"])

            task, applied, errors = apply_policy_actions(
                board,
                task.id,
                [
                    PolicyAction(
                        type="message",
                        params={
                            "sender": "orchestrator",
                            "recipient": "codex",
                            "message_type": "handoff",
                            "content": "Claude is stale; continue the review.",
                        },
                    ),
                    PolicyAction(
                        type="assign_work",
                        params={
                            "owner": "codex",
                            "title": "Take over stalled Claude review",
                            "description": "Inspect the pending work and produce a review report.",
                        },
                    ),
                ],
            )

            self.assertEqual(errors, [])
            self.assertEqual(applied, ["message", "assign_work"])
            self.assertEqual(task.messages[-2].recipient, "codex")
            self.assertEqual(task.work_items[-1].owner, "codex")

    def test_policy_json_parser_accepts_fenced_output(self):
        raw = """```json
{
  "actions": [
    {
      "type": "assign_work",
      "params": {
        "owner": "claude",
        "title": "Review risk",
        "description": "Check whether the plan matches the goal."
      }
    }
  ]
}
```"""

        actions = parse_policy_actions(raw)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "assign_work")
        self.assertEqual(actions[0].params["owner"], "claude")

    def test_ui_summarizes_messages_without_dropping_full_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Summarize conversation")
            task = board.append_message(
                task.id,
                Message(
                    type="proposal",
                    sender="claude",
                    content=(
                        "This is the first sentence. This second sentence contains "
                        "more detail than the dashboard should show by default."
                    ),
                ),
            )

            summary = summarize_text(task.messages[-1].content, limit=40)
            overview = task_summary(task)

            self.assertEqual(summary, "This is the first sentence.")
            self.assertEqual(overview["message_count"], 2)
            self.assertEqual(overview["latest_message"], task.messages[-1].content)

    def test_browser_message_is_saved_for_orchestrator(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Accept dashboard input")

            task = append_browser_message(
                board,
                task.id,
                {
                    "recipient": "orchestrator",
                    "message_type": "chat",
                    "content": "次はClaudeにレビューを依頼して",
                },
            )

            saved = board.load(task.id)
            message = saved.messages[-1]

            self.assertEqual(task.id, saved.id)
            self.assertEqual(message.sender, "human")
            self.assertEqual(message.recipient, "orchestrator")
            self.assertEqual(message.type, "chat")
            self.assertEqual(message.metadata["source"], "dashboard")
            self.assertEqual(message.metadata["actor"], "human")
            self.assertTrue(message.metadata["identity_locked"])
            self.assertIn("Claude", message.content)

    def test_progress_report_reads_workspace_tasks_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "target"
            workspace.mkdir()
            (workspace / "backend").mkdir()
            (workspace / "backend" / "main.py").write_text("app = object()\n", encoding="utf-8")
            (workspace / "TASKS.json").write_text(
                """
{
  "phases": [
    {
      "id": "phase_1",
      "name": "Core",
      "goal": "Boot the app",
      "tasks": [
        {
          "id": "p1_t1",
          "title": "Initialize backend",
          "worker": "codex",
          "files_to_create": ["backend/main.py"],
          "acceptance": "Backend file exists"
        }
      ]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Build target", workspace_path=str(workspace))

            report = build_progress_report(board.load(task.id))

            self.assertEqual(report["source"], "TASKS.json")
            self.assertEqual(report["summary"]["overall_percent"], 100)
            self.assertEqual(report["phases"][0]["status"], "done")
            self.assertEqual(report["phases"][0]["checklist"][0]["status"], "done")

    def test_progress_report_ignores_resolved_historical_blockers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "target"
            workspace.mkdir()
            (workspace / "backend").mkdir()
            (workspace / "backend" / "main.py").write_text("app = object()\n", encoding="utf-8")
            (workspace / "TASKS.json").write_text(
                """
{
  "phases": [
    {
      "id": "phase_1",
      "name": "Core",
      "goal": "Boot the app",
      "tasks": [
        {
          "id": "p1_t1",
          "title": "Initialize backend",
          "worker": "codex",
          "files_to_create": ["backend/main.py"],
          "acceptance": "Backend file exists"
        }
      ]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Build target", workspace_path=str(workspace))
            task = board.add_work_item(
                task.id,
                WorkItem(
                    title="Resolve blocker from claude report",
                    owner="codex",
                    status="blocked",
                    description="Old blocker mentioned p1_t1 before the human clarified direction.",
                    metadata={"source": "autopilot", "purpose": "blocker_recovery"},
                ),
            )
            task = board.add_work_item(
                task.id,
                WorkItem(
                    title="Execute TASKS.json item p1_t1",
                    owner="codex",
                    status="done",
                    metadata={
                        "source": "autopilot",
                        "purpose": "workspace_task",
                        "target_task_id": "p1_t1",
                    },
                ),
            )
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="codex",
                    recipient="orchestrator",
                    content="p1_t1 implemented and verified. No blocker remains.",
                    refs=[task.work_items[-1].id],
                    metadata={"actor": "codex", "source": "auto_agent"},
                ),
            )

            report = build_progress_report(board.load(task.id))

            self.assertEqual(report["summary"]["overall_percent"], 100)
            self.assertFalse(report["blockers"])
            self.assertEqual(report["phases"][0]["checklist"][0]["status"], "done")

    def test_progress_report_includes_orchestrator_notifications(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Watch attention")
            task = board.append_message(
                task.id,
                Message(
                    type="decision",
                    sender="orchestrator",
                    recipient="human",
                    content="Autonomy depth limit reached; no further agent work generated.",
                    metadata={"actor": "orchestrator", "source": "autopilot"},
                ),
            )

            report = build_progress_report(board.load(task.id))

            self.assertTrue(report["summary"]["needs_attention"])
            self.assertEqual(report["notifications"][0]["title"], "Autonomy limit reached")

    def test_dashboard_cannot_impersonate_agent_sender(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Prevent impersonation")

            with self.assertRaises(ValueError):
                append_browser_message(
                    board,
                    task.id,
                    {
                        "sender": "codex",
                        "recipient": "orchestrator",
                        "message_type": "chat",
                        "content": "This must not be accepted as a dashboard human message.",
                    },
                )

    def test_orchestrator_message_auto_invokes_target_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Review dashboard")
            task = append_browser_message(
                board,
                task.id,
                {
                    "recipient": "orchestrator",
                    "message_type": "chat",
                    "content": "claudeにUIを確認してもらって",
                },
            )
            trigger_id = task.messages[-1].id

            calls = []

            def fake_invoker(agent, prompt):
                calls.append((agent, prompt))
                return "Claude review: the summary/full-text split is clear."

            targets = dispatch_orchestrator_message(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )

            saved = board.load(task.id)

            self.assertEqual(targets, ["claude"])
            self.assertEqual(calls[0][0], "claude")
            self.assertIn("Review dashboard", calls[0][1])
            self.assertIn(trigger_id, calls[0][1])
            self.assertTrue(
                any(
                    message.sender == "claude"
                    and message.type == "report_result"
                    and "summary/full-text" in message.content
                    for message in saved.messages
                )
            )

    def test_direct_human_message_auto_invokes_recipient_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Wake direct recipient")
            task = append_browser_message(
                board,
                task.id,
                {
                    "recipient": "claude",
                    "message_type": "chat",
                    "content": "claudeはどう思う？",
                },
            )
            trigger_id = task.messages[-1].id
            calls = []

            def fake_invoker(agent, prompt):
                calls.append((agent, prompt))
                return "Claude direct response."

            targets = dispatch_message(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )
            saved = board.load(task.id)

            self.assertEqual(targets, ["claude"])
            self.assertEqual(calls[0][0], "claude")
            self.assertTrue(
                any(
                    message.sender == "claude"
                    and message.type == "report_result"
                    and "direct response" in message.content
                    for message in saved.messages
                )
            )

    def test_all_human_message_auto_invokes_claude_and_codex(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Wake everyone")
            task = append_browser_message(
                board,
                task.id,
                {
                    "recipient": "all",
                    "message_type": "chat",
                    "content": "二人とも意見をください",
                },
            )
            trigger_id = task.messages[-1].id
            calls = []

            def fake_invoker(agent, prompt):
                calls.append(agent)
                return f"{agent} response."

            targets = dispatch_message(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )

            self.assertEqual(targets, ["claude", "codex"])
            self.assertEqual(calls, ["claude", "codex"])

    def test_agent_proposal_to_orchestrator_wakes_peer_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Route agent proposal")
            task = board.append_message(
                task.id,
                Message(
                    type="proposal",
                    sender="claude",
                    recipient="orchestrator",
                    content="Codex should implement the backend API for session reset.",
                    metadata={"actor": "claude", "source": "feature_proposal"},
                ),
            )
            trigger_id = task.messages[-1].id
            calls = []

            def fake_invoker(agent, prompt):
                calls.append((agent, prompt))
                return "Codex reviewed Claude proposal."

            targets = dispatch_message(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )
            saved = board.load(task.id)

            self.assertEqual(targets, ["codex"])
            self.assertEqual(calls[0][0], "codex")
            self.assertTrue(
                any(
                    message.sender == "orchestrator"
                    and message.type == "decision"
                    and trigger_id in message.refs
                    for message in saved.messages
                )
            )
            self.assertTrue(
                any(
                    message.sender == "codex"
                    and message.type == "report_result"
                    and "proposal" in message.content
                    for message in saved.messages
                )
            )

    def test_autopilot_turns_human_instruction_into_tracked_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Plan and execute")
            task = board.append_message(
                task.id,
                Message(
                    type="chat",
                    sender="human",
                    recipient="orchestrator",
                    content="Codex implement the backend and Claude review the risk.",
                    metadata={"actor": "human", "source": "dashboard"},
                ),
            )
            trigger_id = task.messages[-1].id

            calls = []

            def fake_invoker(agent, prompt):
                calls.append(agent)
                return f"{agent} report."

            result = run_autopilot_once(
                board,
                task.id,
                trigger_message_id=trigger_id,
                invoker=fake_invoker,
                background=False,
            )
            saved = board.load(task.id)

            self.assertEqual(result.dispatched, ["claude", "codex"])
            self.assertEqual(calls, ["claude", "codex"])
            self.assertTrue(
                any(
                    item.metadata.get("source") == "autopilot"
                    and item.metadata.get("trigger_message_id") == trigger_id
                    for item in saved.work_items
                )
            )

    def test_human_workspace_go_ahead_assigns_next_tasks_json_item(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "target"
            workspace.mkdir()
            (workspace / "TASKS.json").write_text(
                """
{
  "phases": [
    {
      "id": "phase_1",
      "name": "Core",
      "tasks": [
        {
          "id": "p1_t1",
          "title": "Initialize project structure",
          "worker": "codex",
          "files_to_create": ["backend/main.py"],
          "acceptance": "Runs"
        }
      ]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Build target", workspace_path=str(workspace))
            task = board.append_message(
                task.id,
                Message(
                    type="chat",
                    sender="human",
                    recipient="orchestrator",
                    content="we are making SEKRET. Start TASKS.json phase 1.",
                    metadata={"actor": "human", "source": "dashboard"},
                ),
            )

            run_autopilot_once(
                board,
                task.id,
                trigger_message_id=task.messages[-1].id,
                dispatch=False,
                background=False,
            )
            saved = board.load(task.id)

            self.assertTrue(
                any(
                    item.metadata.get("purpose") == "workspace_task"
                    and item.metadata.get("target_task_id") == "p1_t1"
                    for item in saved.work_items
                )
            )

    def test_autopilot_does_not_dispatch_while_agent_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Avoid overlapping invocations")
            task = board.record_heartbeat(task.id, "codex", status="running", note="Busy")
            task = board.append_message(
                task.id,
                Message(
                    type="chat",
                    sender="human",
                    recipient="orchestrator",
                    content="Codex implement another thing.",
                    metadata={"actor": "human", "source": "dashboard"},
                ),
            )

            result = run_autopilot_once(
                board,
                task.id,
                trigger_message_id=task.messages[-1].id,
                background=False,
            )

            self.assertEqual(result.skipped_reason, "agent_running")
            self.assertEqual(result.dispatched, [])

    def test_autopilot_backlog_skips_handled_latest_trigger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Catch up backlog")
            task = board.append_message(
                task.id,
                Message(
                    type="chat",
                    sender="human",
                    recipient="orchestrator",
                    content="Codex implement the first thing.",
                    metadata={"actor": "human", "source": "dashboard"},
                ),
            )
            unhandled_id = task.messages[-1].id
            task = board.append_message(
                task.id,
                Message(
                    type="chat",
                    sender="human",
                    recipient="orchestrator",
                    content="Claude review the second thing.",
                    metadata={"actor": "human", "source": "dashboard"},
                ),
            )
            handled_id = task.messages[-1].id
            task = board.append_message(
                task.id,
                Message(
                    type="decision",
                    sender="orchestrator",
                    recipient="all",
                    content="Already handled latest.",
                    refs=[handled_id],
                    metadata={"actor": "orchestrator", "source": "autopilot"},
                ),
            )

            result = run_autopilot_once(
                board,
                task.id,
                scan_backlog=True,
                dispatch=False,
                background=False,
            )

            self.assertEqual(result.trigger_id, unhandled_id)

    def test_autopilot_backlog_ignores_task_create_user_goal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Initial goal only")

            result = run_autopilot_once(
                board,
                task.id,
                scan_backlog=True,
                dispatch=False,
                background=False,
            )

            self.assertEqual(result.skipped_reason, "no_actionable_trigger")

    def test_periodic_tick_assigns_next_workspace_task_without_message_trigger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "target"
            workspace.mkdir()
            (workspace / "backend").mkdir()
            (workspace / "backend" / "database.py").write_text("# existing scaffold\n", encoding="utf-8")
            (workspace / "TASKS.json").write_text(
                """
{
  "phases": [
    {
      "id": "phase_1",
      "name": "Core",
      "tasks": [
        {
          "id": "p1_t2",
          "title": "SQLite schema + SQLModel models",
          "worker": "codex",
          "files_to_modify": ["backend/database.py"],
          "acceptance": "CRUD operations work"
        }
      ]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Keep building", workspace_path=str(workspace))

            result = run_autopilot_once(
                board,
                task.id,
                scan_backlog=True,
                dispatch=False,
                background=False,
            )
            saved = board.load(task.id)

            self.assertIn("assign_work", result.applied)
            self.assertTrue(
                any(
                    item.metadata.get("purpose") == "workspace_task"
                    and item.metadata.get("target_task_id") == "p1_t2"
                    for item in saved.work_items
                )
            )

    def test_autopilot_marks_referenced_work_done_from_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Track work status")
            task = board.add_work_item(
                task.id,
                WorkItem(
                    title="Implement endpoint",
                    owner="codex",
                    metadata={"source": "autopilot", "autonomy_depth": 0},
                ),
            )
            work_id = task.work_items[-1].id
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="codex",
                    recipient="orchestrator",
                    content="Implemented and verified tests pass.",
                    refs=[work_id],
                    metadata={"actor": "codex", "source": "auto_agent"},
                ),
            )

            run_autopilot_once(
                board,
                task.id,
                trigger_message_id=task.messages[-1].id,
                dispatch=False,
                background=False,
            )
            saved = board.load(task.id)

            self.assertEqual(
                next(item for item in saved.work_items if item.id == work_id).status,
                "done",
            )

    def test_autopilot_creates_stalled_takeover_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Recover stale work")
            task = board.add_work_item(
                task.id,
                WorkItem(title="Review implementation", owner="claude"),
            )
            task = board.append_message(
                task.id,
                Message(
                    type="chat",
                    sender="human",
                    recipient="orchestrator",
                    content="keep going",
                    metadata={"actor": "human", "source": "dashboard"},
                ),
            )

            result = run_autopilot_once(
                board,
                task.id,
                trigger_message_id=task.messages[-1].id,
                stale_after_seconds=0,
                dispatch=False,
                background=False,
            )
            saved = board.load(task.id)

            self.assertIn("assign_work", result.applied)
            self.assertTrue(
                any(
                    item.owner == "codex"
                    and item.metadata.get("purpose") == "stalled_takeover"
                    for item in saved.work_items
                )
            )

    def test_report_result_generates_peer_review_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Commander should continue after reports")
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="codex",
                    recipient="orchestrator",
                    content="Implemented the heartbeat UI and verified focused tests pass.",
                    metadata={"actor": "codex", "source": "auto_agent"},
                ),
            )
            trigger_id = task.messages[-1].id
            calls = []

            def fake_invoker(agent, prompt):
                calls.append((agent, prompt))
                return "Claude review: implementation matches the requested behavior."

            owner = dispatch_commander_followup(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )
            saved = board.load(task.id)

            self.assertEqual(owner, "claude")
            self.assertEqual(calls[0][0], "claude")
            self.assertTrue(
                any(
                    item.owner == "claude"
                    and item.title == "Review Codex result"
                    for item in saved.work_items
                )
            )
            self.assertTrue(
                any(
                    message.sender == "orchestrator"
                    and message.type == "decision"
                    and message.metadata.get("source") == "autopilot"
                    and trigger_id in message.refs
                    for message in saved.messages
                )
            )

    def test_commander_does_not_duplicate_followup_for_same_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Avoid duplicate commander work")
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="codex",
                    recipient="orchestrator",
                    content="Implemented the requested fix.",
                    metadata={"actor": "codex", "source": "auto_agent"},
                ),
            )
            trigger_id = task.messages[-1].id

            def fake_invoker(agent, prompt):
                return "Review complete."

            first = dispatch_commander_followup(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )
            second = dispatch_commander_followup(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )
            saved = board.load(task.id)

            self.assertEqual(first, "claude")
            self.assertEqual(second, "")
            self.assertEqual(
                sum(
                    1
                    for message in saved.messages
                    if message.metadata.get("source") == "autopilot"
                    and trigger_id in message.refs
                ),
                1,
            )

    def test_commander_generated_work_does_not_chain_followup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Stop commander loops")
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="codex",
                    recipient="orchestrator",
                    content="Implemented the requested UI fix.",
                    metadata={"actor": "codex", "source": "auto_agent"},
                ),
            )
            trigger_id = task.messages[-1].id

            def fake_invoker(agent, prompt):
                return "Claude review: please fix the implementation details."

            owner = dispatch_commander_followup(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )
            saved = board.load(task.id)
            generated_work = next(
                item for item in saved.work_items if item.title == "Review Codex result"
            )
            saved = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="claude",
                    recipient="orchestrator",
                    content="Please fix the implementation details in Codex's result.",
                    refs=[generated_work.id],
                    metadata={"actor": "claude", "source": "auto_agent"},
                ),
            )
            chained_id = saved.messages[-1].id

            second_owner = dispatch_commander_followup(
                board,
                task.id,
                chained_id,
                invoker=fake_invoker,
                background=False,
            )

            self.assertEqual(owner, "claude")
            self.assertEqual(second_owner, "")

    def test_peer_review_completion_assigns_next_workspace_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "target"
            workspace.mkdir()
            (workspace / "TASKS.json").write_text(
                """
{
  "phases": [
    {
      "id": "phase_1",
      "name": "Core",
      "tasks": [
        {
          "id": "p1_t1",
          "title": "Create backend",
          "worker": "codex",
          "files_to_create": ["backend/main.py"],
          "acceptance": "File exists"
        }
      ]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Keep building", workspace_path=str(workspace))
            task = board.add_work_item(
                task.id,
                WorkItem(
                    title="Review Codex result",
                    owner="claude",
                    metadata={"source": "autopilot", "purpose": "peer_review"},
                ),
            )
            review_work_id = task.work_items[-1].id
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="claude",
                    recipient="orchestrator",
                    content="Approved: Codex result matches the request.",
                    refs=[review_work_id],
                    metadata={"actor": "claude", "source": "auto_agent"},
                ),
            )

            result = run_autopilot_once(
                board,
                task.id,
                trigger_message_id=task.messages[-1].id,
                dispatch=False,
                background=False,
            )
            saved = board.load(task.id)

            self.assertIn("assign_work", result.applied)
            self.assertTrue(
                any(
                    item.owner == "codex"
                    and item.metadata.get("purpose") == "workspace_task"
                    and item.metadata.get("target_task_id") == "p1_t1"
                    for item in saved.work_items
                )
            )

    def test_report_completion_assigns_uncovered_constraint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(
                goal="Design unified UI",
                constraints=[
                    "Show real-time agent activity",
                    "Make artifacts and reviews easy to browse",
                ],
            )
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="claude",
                    recipient="orchestrator",
                    content="Completed the conversation-flow review.",
                    metadata={"actor": "claude", "source": "auto_agent"},
                ),
            )

            result = run_autopilot_once(
                board,
                task.id,
                trigger_message_id=task.messages[-1].id,
                dispatch=False,
                background=False,
            )
            saved = board.load(task.id)

            self.assertIn("assign_work", result.applied)
            self.assertTrue(
                any(
                    item.owner == "codex"
                    and item.metadata.get("purpose") == "constraint_followup"
                    and item.metadata.get("target_constraint") == "Show real-time agent activity"
                    for item in saved.work_items
                )
            )

    def test_constraint_followup_does_not_duplicate_existing_constraint_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(
                goal="Design unified UI",
                constraints=[
                    "Show real-time agent activity",
                    "Make artifacts and reviews easy to browse",
                ],
            )
            task = board.add_work_item(
                task.id,
                WorkItem(
                    title="Address constraint: Show real-time agent activity",
                    owner="codex",
                    metadata={
                        "source": "autopilot",
                        "purpose": "constraint_followup",
                        "target_constraint": "Show real-time agent activity",
                    },
                ),
            )
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="claude",
                    recipient="orchestrator",
                    content="Completed the dashboard review.",
                    metadata={"actor": "claude", "source": "auto_agent"},
                ),
            )

            run_autopilot_once(
                board,
                task.id,
                trigger_message_id=task.messages[-1].id,
                dispatch=False,
                background=False,
            )
            saved = board.load(task.id)

            self.assertTrue(
                any(
                    item.metadata.get("purpose") == "constraint_followup"
                    and item.metadata.get("target_constraint") == "Make artifacts and reviews easy to browse"
                    for item in saved.work_items
                )
            )
            self.assertEqual(
                sum(
                    1
                    for item in saved.work_items
                    if item.metadata.get("target_constraint") == "Show real-time agent activity"
                ),
                1,
            )

    def test_blocker_recovery_depth_limit_does_not_assign_more_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Stop repeated blocker loops")
            task = board.add_work_item(
                task.id,
                WorkItem(
                    title="Resolve blocker",
                    owner="codex",
                    metadata={
                        "source": "autopilot",
                        "purpose": "blocker_recovery",
                        "autonomy_depth": 2,
                    },
                ),
            )
            work_id = task.work_items[-1].id
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="codex",
                    recipient="orchestrator",
                    content="Blocked: still cannot proceed without a product decision.",
                    refs=[work_id],
                    metadata={"actor": "codex", "source": "auto_agent"},
                ),
            )

            result = run_autopilot_once(
                board,
                task.id,
                trigger_message_id=task.messages[-1].id,
                dispatch=False,
                background=False,
            )
            saved = board.load(task.id)

            self.assertNotIn("assign_work", result.applied)
            self.assertTrue(
                any(
                    message.sender == "orchestrator"
                    and "Blocker recovery limit reached" in message.content
                    for message in saved.messages
                )
            )

    def test_blocked_report_generates_codex_fix_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Recover from blocked agent")
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="claude",
                    recipient="orchestrator",
                    content="Blocked: the UI route returns an error and needs a code fix.",
                    metadata={"actor": "claude", "source": "auto_agent"},
                ),
            )
            trigger_id = task.messages[-1].id
            calls = []

            def fake_invoker(agent, prompt):
                calls.append(agent)
                return "Codex fix report."

            owner = dispatch_commander_followup(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )
            saved = board.load(task.id)

            self.assertEqual(owner, "codex")
            self.assertEqual(calls, ["codex"])
            self.assertTrue(
                any(
                    item.owner == "codex"
                    and item.title == "Resolve blocker from claude report"
                    for item in saved.work_items
                )
            )

    def test_no_blocker_analysis_does_not_generate_recovery_loop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Avoid false blocker loops")
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="claude",
                    recipient="orchestrator",
                    content=(
                        "実行しません: 編集権限なしのため実装はできません。\n\n"
                        "## Blocker 分析\n"
                        "Codex の報告には blocker の記述がありません。"
                        "Blocker は存在しません。"
                    ),
                    metadata={"actor": "claude", "source": "auto_agent"},
                ),
            )

            result = run_autopilot_once(
                board,
                task.id,
                trigger_message_id=task.messages[-1].id,
                dispatch=False,
                background=False,
            )
            saved = board.load(task.id)

            self.assertNotIn("assign_work", result.applied)
            self.assertFalse(
                any(
                    item.metadata.get("purpose") == "blocker_recovery"
                    for item in saved.work_items
                )
            )

    def test_no_blocker_report_advances_to_next_workspace_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "target"
            workspace.mkdir()
            (workspace / "TASKS.json").write_text(
                """
{
  "phases": [
    {
      "id": "phase_1",
      "name": "Core",
      "tasks": [
        {
          "id": "p1_t1",
          "title": "Initialize project structure",
          "worker": "codex",
          "files_to_create": ["backend/main.py"],
          "acceptance": "Runs"
        }
      ]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Build target", workspace_path=str(workspace))
            task = board.add_work_item(
                task.id,
                WorkItem(
                    title="Resolve blocker",
                    owner="claude",
                    metadata={"source": "autopilot", "purpose": "blocker_recovery"},
                ),
            )
            work_id = task.work_items[-1].id
            task = board.append_message(
                task.id,
                Message(
                    type="report_result",
                    sender="claude",
                    recipient="orchestrator",
                    content=(
                        "No blocker remains. Product decision is now clear. "
                        "Human confirmed build target is SEKRET. Next action: start p1_t1."
                    ),
                    refs=[work_id],
                    metadata={"actor": "claude", "source": "auto_agent"},
                ),
            )

            result = run_autopilot_once(
                board,
                task.id,
                trigger_message_id=task.messages[-1].id,
                dispatch=False,
                background=False,
            )
            saved = board.load(task.id)

            self.assertIn("assign_work", result.applied)
            self.assertEqual(
                next(item for item in saved.work_items if item.id == work_id).status,
                "done",
            )
            self.assertTrue(
                any(
                    item.metadata.get("purpose") == "workspace_task"
                    and item.metadata.get("target_task_id") == "p1_t1"
                    for item in saved.work_items
                )
            )

    def test_codex_implementation_request_allows_auto_edits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Allow implementation")
            task = append_browser_message(
                board,
                task.id,
                {
                    "recipient": "codex",
                    "message_type": "chat",
                    "content": "UIを修正して",
                },
            )
            trigger_id = task.messages[-1].id
            prompts = []

            def fake_invoker(agent, prompt):
                prompts.append(prompt)
                return "実行します: UIを修正しました。"

            dispatch_message(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )
            saved = board.load(task.id)

            self.assertIn("Edit permission for this invocation: allowed.", prompts[0])
            self.assertTrue(
                any(
                    message.sender == "codex"
                    and message.metadata.get("auto_edit_allowed") is True
                    for message in saved.messages
                )
            )

    def test_non_implementation_request_stays_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Explain only")
            task = append_browser_message(
                board,
                task.id,
                {
                    "recipient": "codex",
                    "message_type": "chat",
                    "content": "現状を説明して",
                },
            )
            trigger_id = task.messages[-1].id
            prompts = []

            def fake_invoker(agent, prompt):
                prompts.append(prompt)
                return "実行しません: 説明のみの依頼です。"

            dispatch_message(
                board,
                task.id,
                trigger_id,
                invoker=fake_invoker,
                background=False,
            )

            self.assertIn("Edit permission for this invocation: not allowed.", prompts[0])

    def test_agent_inbox_exports_context_for_non_mcp_access(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Export context")

            inbox = write_agent_inbox(board, task.id, "claude")

            self.assertTrue(inbox.exists())
            text = inbox.read_text(encoding="utf-8")
            self.assertIn("Export context", text)
            self.assertIn("Recipient: claude", text)

    def test_work_assignment_auto_invokes_codex_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Run assigned Codex work")
            task = board.add_work_item(
                task.id,
                WorkItem(
                    title="Inspect UI filter",
                    owner="codex",
                    description="Check the message filter behavior.",
                ),
            )
            work_id = task.work_items[-1].id
            calls = []

            def fake_invoker(agent, prompt):
                calls.append((agent, prompt))
                return "Codex report: filter behavior reviewed."

            dispatched = dispatch_work_item(
                board,
                task.id,
                work_id,
                invoker=fake_invoker,
                background=False,
            )
            saved = board.load(task.id)

            self.assertEqual(dispatched, "codex")
            self.assertEqual(calls[0][0], "codex")
            self.assertIn("Inspect UI filter", calls[0][1])
            self.assertTrue(
                any(
                    message.sender == "codex"
                    and message.type == "report_result"
                    and "filter behavior" in message.content
                    for message in saved.messages
                )
            )

    def test_codex_runner_prefers_native_cli_when_available(self):
        command = _wrap_command("codex", ["--version"])

        self.assertTrue(command[0].endswith("codex.exe") or command[0] in {"cmd", "codex"})

    def test_codex_session_id_is_parsed_from_json_events(self):
        output = '\n'.join(
            [
                '{"type":"thread.started","thread_id":"session_123"}',
                '{"type":"item.completed","item":{"text":"ok"}}',
            ]
        )

        self.assertEqual(_extract_codex_session_id(output), "session_123")

    def test_claude_session_busy_error_is_detected(self):
        self.assertTrue(
            _is_claude_session_busy_error(
                "Error: Session ID d507d132-07df-4f83-a07a-50765adc4db2 is already in use."
            )
        )
        self.assertFalse(_is_claude_session_busy_error("Authentication failed"))

    def test_agent_session_registry_is_task_and_agent_scoped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Keep agent sessions separate")

            _set_agent_session(
                board,
                task.id,
                "codex",
                {"session_id": "codex-session", "backend": "codex exec"},
            )

            self.assertEqual(
                _get_agent_session(board, task.id, "codex")["session_id"],
                "codex-session",
            )
            self.assertEqual(_get_agent_session(board, task.id, "claude"), {})

    def test_reset_agent_sessions_removes_selected_task_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = MissionBoard(Path(temp_dir) / ".multiagent")
            task = board.create_task(goal="Reset selected sessions")
            other_task = board.create_task(goal="Keep other sessions")

            _set_agent_session(board, task.id, "codex", {"session_id": "codex-session"})
            _set_agent_session(board, task.id, "claude", {"session_id": "claude-session"})
            _set_agent_session(board, other_task.id, "codex", {"session_id": "other-session"})

            removed = reset_agent_sessions(board, task_id=task.id, agent="codex")

            self.assertEqual(removed, [f"{task.id}:codex"])
            self.assertEqual(_get_agent_session(board, task.id, "codex"), {})
            self.assertEqual(_get_agent_session(board, task.id, "claude")["session_id"], "claude-session")
            self.assertEqual(_get_agent_session(board, other_task.id, "codex")["session_id"], "other-session")


if __name__ == "__main__":
    unittest.main()
