"""File-backed mission board for multi-agent collaboration."""

from __future__ import annotations

import json
import threading
from json import JSONDecodeError
from pathlib import Path
from uuid import uuid4

from .protocol import AgentActivity, Artifact, Message, Review, TaskState, WorkItem, now_iso


class MissionBoard:
    """Persist tasks as debuggable JSON files under `.multiagent/tasks`."""

    def __init__(self, root: str | Path = ".multiagent") -> None:
        self.root = Path(root)
        self.tasks_dir = self.root / "tasks"
        self.memory_dir = self.root / "memory"
        self._lock = threading.RLock()
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def create_task(
        self,
        goal: str,
        constraints: list[str] | None = None,
        user_message: str = "",
        workspace_path: str = "",
    ) -> TaskState:
        task = TaskState(
            goal=goal,
            constraints=constraints or [],
            workspace_path=workspace_path,
        )
        task.messages.append(
            Message(
                type="user_goal",
                sender="human",
                content=user_message or goal,
                metadata={"actor": "human", "source": "task_create"},
            )
        )
        self.save(task)
        return task

    def save(self, task: TaskState) -> None:
        with self._lock:
            path = self.task_path(task.id)
            temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            temp_path.write_text(
                json.dumps(task.to_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temp_path.replace(path)

    def load(self, task_id: str) -> TaskState:
        with self._lock:
            path = self.task_path(task_id)
            if not path.exists():
                raise FileNotFoundError(f"Task not found: {task_id}")
            return TaskState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_tasks(self) -> list[TaskState]:
        tasks = []
        with self._lock:
            for path in sorted(self.tasks_dir.glob("*.json")):
                try:
                    tasks.append(TaskState.from_dict(json.loads(path.read_text(encoding="utf-8"))))
                except (OSError, JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
        return tasks

    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def append_message(self, task_id: str, message: Message) -> TaskState:
        with self._lock:
            task = self.load(task_id)
            task.messages.append(message)
            self.save(task)
            return task

    def add_work_item(self, task_id: str, item: WorkItem) -> TaskState:
        with self._lock:
            task = self.load(task_id)
            task.work_items.append(item)
            task.messages.append(
                Message(
                    type="assign_task",
                    sender="orchestrator",
                    recipient=item.owner,
                    content=f"{item.title}\n\n{item.description}".strip(),
                    refs=[item.id],
                    metadata={"actor": "orchestrator", "source": "mission_board"},
                )
            )
            self.save(task)
            return task

    def update_work_item(
        self,
        task_id: str,
        work_id: str,
        status: str | None = None,
        artifact_ids: list[str] | None = None,
    ) -> TaskState:
        with self._lock:
            task = self.load(task_id)
            for item in task.work_items:
                if item.id == work_id:
                    if status:
                        item.status = status
                    if artifact_ids:
                        item.artifacts.extend(a for a in artifact_ids if a not in item.artifacts)
                    self.save(task)
                    return task
        raise FileNotFoundError(f"Work item not found: {work_id}")

    def add_artifact(self, task_id: str, artifact: Artifact) -> TaskState:
        with self._lock:
            task = self.load(task_id)
            task.artifacts.append(artifact)
            task.messages.append(
                Message(
                    type="report_result",
                    sender=artifact.owner or "orchestrator",
                    content=f"Artifact submitted: {artifact.title}",
                    refs=[artifact.id],
                    metadata={
                        "actor": artifact.owner or "orchestrator",
                        "source": "artifact_submit",
                    },
                )
            )
            self.save(task)
            return task

    def add_review(self, task_id: str, review: Review) -> TaskState:
        with self._lock:
            task = self.load(task_id)
            task.reviews.append(review)
            task.messages.append(
                Message(
                    type="review",
                    sender=review.reviewer,
                    content=f"{review.verdict}: {review.notes}",
                    refs=[review.artifact_id, review.id],
                    metadata={"actor": review.reviewer, "source": "review_submit"},
                )
            )
            self.save(task)
            return task

    def record_heartbeat(
        self,
        task_id: str,
        agent: str,
        status: str = "active",
        current_work_id: str = "",
        note: str = "",
    ) -> TaskState:
        with self._lock:
            task = self.load(task_id)
            task.agent_activity[agent] = AgentActivity(
                agent=agent,
                last_seen_at=now_iso(),
                status=status,
                current_work_id=current_work_id,
                note=note,
            )
            task.messages.append(
                Message(
                    type="heartbeat",
                    sender=agent,
                    recipient="orchestrator",
                    content=note or f"{agent} is {status}",
                    refs=[current_work_id] if current_work_id else [],
                    metadata={"actor": agent, "source": "heartbeat"},
                )
            )
            self.save(task)
            return task

    def read_memory(self, name: str) -> str:
        path = self.memory_dir / f"{name}.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def write_memory(self, name: str, content: str) -> None:
        path = self.memory_dir / f"{name}.md"
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
