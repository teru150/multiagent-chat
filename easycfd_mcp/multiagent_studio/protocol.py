"""Shared protocol objects for a neutral multi-agent workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def now_iso() -> str:
    """Return a stable UTC timestamp for persisted records."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Agent(str, Enum):
    HUMAN = "human"
    CLAUDE = "claude"
    CODEX = "codex"
    ORCHESTRATOR = "orchestrator"
    SUBAGENT = "subagent"


class MessageType(str, Enum):
    USER_GOAL = "user_goal"
    PROPOSAL = "proposal"
    PLAN_UPDATE = "plan_update"
    ASSIGN_TASK = "assign_task"
    REPORT_RESULT = "report_result"
    REQUEST_REVIEW = "request_review"
    REVIEW = "review"
    OBJECTION = "objection"
    DECISION = "decision"
    HANDOFF = "handoff"
    HEARTBEAT = "heartbeat"
    CHAT = "chat"


class TaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"


class ArtifactKind(str, Enum):
    PATCH = "patch"
    DOC = "doc"
    REPORT = "report"
    TEST_LOG = "test_log"
    OTHER = "other"


@dataclass
class Message:
    type: str
    sender: str
    content: str
    recipient: str = "all"
    id: str = field(default_factory=lambda: new_id("msg"))
    created_at: str = field(default_factory=now_iso)
    refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "type": self.type,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "refs": self.refs,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            id=data["id"],
            created_at=data["created_at"],
            type=data["type"],
            sender=data["sender"],
            recipient=data.get("recipient", "all"),
            content=data["content"],
            refs=list(data.get("refs", [])),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class WorkItem:
    title: str
    owner: str
    description: str = ""
    status: str = TaskStatus.TODO.value
    id: str = field(default_factory=lambda: new_id("work"))
    created_at: str = field(default_factory=now_iso)
    artifacts: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "title": self.title,
            "owner": self.owner,
            "description": self.description,
            "status": self.status,
            "artifacts": self.artifacts,
            "depends_on": self.depends_on,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkItem":
        return cls(
            id=data["id"],
            created_at=data["created_at"],
            title=data["title"],
            owner=data["owner"],
            description=data.get("description", ""),
            status=data.get("status", TaskStatus.TODO.value),
            artifacts=list(data.get("artifacts", [])),
            depends_on=list(data.get("depends_on", [])),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class Artifact:
    title: str
    kind: str
    path: str = ""
    content: str = ""
    owner: str = ""
    id: str = field(default_factory=lambda: new_id("artifact"))
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "title": self.title,
            "kind": self.kind,
            "path": self.path,
            "content": self.content,
            "owner": self.owner,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Artifact":
        return cls(
            id=data["id"],
            created_at=data["created_at"],
            title=data["title"],
            kind=data["kind"],
            path=data.get("path", ""),
            content=data.get("content", ""),
            owner=data.get("owner", ""),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class Review:
    artifact_id: str
    reviewer: str
    verdict: str
    notes: str
    id: str = field(default_factory=lambda: new_id("review"))
    created_at: str = field(default_factory=now_iso)
    required_changes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "artifact_id": self.artifact_id,
            "reviewer": self.reviewer,
            "verdict": self.verdict,
            "notes": self.notes,
            "required_changes": self.required_changes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Review":
        return cls(
            id=data["id"],
            created_at=data["created_at"],
            artifact_id=data["artifact_id"],
            reviewer=data["reviewer"],
            verdict=data["verdict"],
            notes=data["notes"],
            required_changes=list(data.get("required_changes", [])),
        )


@dataclass
class AgentActivity:
    agent: str
    last_seen_at: str = field(default_factory=now_iso)
    status: str = "active"
    current_work_id: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "last_seen_at": self.last_seen_at,
            "status": self.status,
            "current_work_id": self.current_work_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentActivity":
        return cls(
            agent=data["agent"],
            last_seen_at=data.get("last_seen_at", now_iso()),
            status=data.get("status", "active"),
            current_work_id=data.get("current_work_id", ""),
            note=data.get("note", ""),
        )


@dataclass
class TaskState:
    goal: str
    id: str = field(default_factory=lambda: new_id("task"))
    created_at: str = field(default_factory=now_iso)
    status: str = TaskStatus.IN_PROGRESS.value
    workspace_path: str = ""
    constraints: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    work_items: list[WorkItem] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    agent_activity: dict[str, AgentActivity] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "goal": self.goal,
            "status": self.status,
            "workspace_path": self.workspace_path,
            "constraints": self.constraints,
            "assumptions": self.assumptions,
            "open_questions": self.open_questions,
            "work_items": [item.to_dict() for item in self.work_items],
            "messages": [message.to_dict() for message in self.messages],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "reviews": [review.to_dict() for review in self.reviews],
            "decisions": self.decisions,
            "agent_activity": {
                agent: activity.to_dict()
                for agent, activity in self.agent_activity.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskState":
        return cls(
            id=data["id"],
            created_at=data["created_at"],
            goal=data["goal"],
            status=data.get("status", TaskStatus.IN_PROGRESS.value),
            workspace_path=data.get("workspace_path", ""),
            constraints=list(data.get("constraints", [])),
            assumptions=list(data.get("assumptions", [])),
            open_questions=list(data.get("open_questions", [])),
            work_items=[WorkItem.from_dict(item) for item in data.get("work_items", [])],
            messages=[Message.from_dict(message) for message in data.get("messages", [])],
            artifacts=[Artifact.from_dict(artifact) for artifact in data.get("artifacts", [])],
            reviews=[Review.from_dict(review) for review in data.get("reviews", [])],
            decisions=list(data.get("decisions", [])),
            agent_activity={
                agent: AgentActivity.from_dict(activity)
                for agent, activity in data.get("agent_activity", {}).items()
            },
        )
