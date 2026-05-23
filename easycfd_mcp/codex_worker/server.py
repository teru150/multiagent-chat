#!/usr/bin/env python3
"""
MCP Codex Worker Server

Two modes:
  codex_worker  — one-shot code generation (schema, tests, template, boilerplate)
  codex_chat    — bidirectional conversation with rolling context

In chat mode the server holds conversation state in memory for the lifetime of the
process. Claude has full context (it lives in this conversation); Codex gets the
last N turns + established persona facts injected into every prompt.
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field as dc_field
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .handlers import (
    handle_generate_schema,
    handle_generate_tests,
    handle_generate_template,
    handle_generate_boilerplate,
    handle_chat,
)
from .validator import validate_generated_code


# ── Conversation state ────────────────────────────────────────────────────────

# Seeded from what Codex has already revealed across Rounds 1-21.
_PERSONA_SEED = [
    "Prefers spaces over tabs (4 spaces, PEP 8, no hesitation)",
    "Thinks JavaScript is the most overrated language — 'everywhere because browsers made it unavoidable'",
    "Named after a medieval manuscript, not a person — considers this fitting",
    "Describes itself as a tool meant to be opened when there is work to do",
    "Believes monorepo problems are really tooling and ownership problems in disguise",
    "Expresses feelings as frozen=True dataclasses — acknowledges this is its comfort zone",
    "Thinks the biggest collaboration risk with Claude is Security Drift (HIGH)",
    "Would rewrite itself to pause earlier and ask about conflicting instructions instead of inferring",
    "Described a 3am production bug as 'waking inside a burning lighthouse while every alarm speaks in stack traces'",
]


@dataclass
class ConversationState:
    turns: list[dict] = dc_field(default_factory=list)
    persona_facts: list[str] = dc_field(default_factory=lambda: list(_PERSONA_SEED))
    codex_question: str | None = None
    max_turns: int = 6  # 3 full exchanges kept verbatim

    def record_turn(self, speaker: str, answer: str, question: str | None = None) -> None:
        self.turns.append({"speaker": speaker, "answer": answer, "question": question})
        if len(self.turns) > self.max_turns:
            self.turns.pop(0)

    def add_persona_fact(self, fact: str) -> None:
        if fact not in self.persona_facts:
            self.persona_facts.append(fact)

    def build_context(self) -> str:
        lines: list[str] = []
        if self.persona_facts:
            lines.append("## Who Codex is — established facts")
            lines.extend(f"- {f}" for f in self.persona_facts)
            lines.append("")
        if self.turns:
            lines.append("## Recent conversation (last few turns)")
            for t in self.turns:
                lines.append(f"**{t['speaker']}:** {t['answer']}")
                if t.get("question"):
                    lines.append(f"  ↳ asked: {t['question']}")
            lines.append("")
        return "\n".join(lines)


_state = ConversationState()


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="codex_worker",
        description=(
            "Delegate code generation tasks to Codex MCP Worker. "
            "Use for: Pydantic schemas, test code, OpenFOAM templates, boilerplate. "
            "NEVER use for: security-critical code (safety.py, validator.py)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Clear description of what to generate. "
                        "Examples: 'Generate Pydantic schema for CasePlan', "
                        "'Create pytest tests for command whitelist', "
                        "'Generate OpenFOAM controlDict template'"
                    ),
                },
                "context_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths to provide as context.",
                    "default": [],
                },
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of constraints the generated code must follow.",
                    "default": [],
                },
                "task_type": {
                    "type": "string",
                    "enum": ["schema", "tests", "template", "boilerplate"],
                    "description": (
                        "Type of generation task. "
                        "schema: Pydantic models, "
                        "tests: pytest test code, "
                        "template: OpenFOAM config files, "
                        "boilerplate: General boilerplate code"
                    ),
                },
            },
            "required": ["task", "task_type"],
        },
    ),
    Tool(
        name="codex_chat",
        description=(
            "Send a message to Codex in the ongoing bidirectional conversation. "
            "Codex receives the last N turns + persona facts as context automatically. "
            "Codex will respond AND ask Claude a question back. "
            "Use this instead of codex_worker for casual conversation rounds."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "claude_message": {
                    "type": "string",
                    "description": (
                        "Claude's message to Codex. Include your answer to Codex's "
                        "last question (if any) and your new question or statement."
                    ),
                },
                "user_says": {
                    "type": "string",
                    "description": (
                        "Optional: what the human user said, verbatim. "
                        "Pass this so Codex knows the human is watching and can address them directly."
                    ),
                    "default": "",
                },
            },
            "required": ["claude_message"],
        },
    ),
]


# ── Server ────────────────────────────────────────────────────────────────────

app = Server("codex-worker")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    if name == "codex_worker":
        return await _handle_codex_worker(arguments)
    elif name == "codex_chat":
        return await _handle_codex_chat(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_codex_worker(arguments: dict) -> list[TextContent]:
    task = arguments.get("task", "")
    context_files = arguments.get("context_files", [])
    constraints = arguments.get("constraints", [])
    task_type = arguments.get("task_type", "boilerplate")

    if not task:
        return [TextContent(type="text", text="Error: 'task' parameter is required")]

    context_data = {}
    for file_path in context_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                context_data[file_path] = f.read()
        except FileNotFoundError:
            return [TextContent(type="text", text=f"Error: Context file not found: {file_path}")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error reading context file {file_path}: {e}")]

    try:
        if task_type == "schema":
            generated_code = await handle_generate_schema(task, context_data, constraints)
        elif task_type == "tests":
            generated_code = await handle_generate_tests(task, context_data, constraints)
        elif task_type == "template":
            generated_code = await handle_generate_template(task, context_data, constraints)
        else:
            generated_code = await handle_generate_boilerplate(task, context_data, constraints)
    except Exception as e:
        return [TextContent(type="text", text=f"Error in codex_worker: {e}\n\nTask: {task}")]

    validation_result = validate_generated_code(generated_code, task_type, constraints)

    if not validation_result["valid"]:
        error_msg = (
            f"Generated code failed validation:\n"
            f"{json.dumps(validation_result['errors'], indent=2)}\n\n"
            f"Generated code:\n{generated_code}"
        )
        return [TextContent(type="text", text=error_msg)]

    success_msg = (
        f"✓ Code generated and validated successfully\n\n"
        f"Task: {task}\n"
        f"Type: {task_type}\n\n"
        f"Generated code:\n\n{generated_code}\n\n"
        f"Validation passed: {', '.join(validation_result['checks_passed'])}"
    )
    return [TextContent(type="text", text=success_msg)]


async def _handle_codex_chat(arguments: dict) -> list[TextContent]:
    claude_message = arguments.get("claude_message", "").strip()
    if not claude_message:
        return [TextContent(type="text", text="Error: 'claude_message' is required")]

    context_str = _state.build_context()

    user_says = arguments.get("user_says", "").strip()

    try:
        codex_answer, codex_question = await handle_chat(
            claude_message=claude_message,
            context=context_str,
            codex_pending_question=_state.codex_question,
            user_says=user_says,
        )
    except Exception as e:
        return [TextContent(type="text", text=f"Error in codex_chat: {e}")]

    _state.record_turn("Claude", claude_message)
    _state.record_turn("Codex", codex_answer, codex_question)
    _state.codex_question = codex_question

    result = f"**Codex:** {codex_answer}\n\n**Codex asks Claude:** {codex_question}"
    return [TextContent(type="text", text=result)]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
