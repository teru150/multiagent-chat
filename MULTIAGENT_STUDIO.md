# Multi-Agent Studio

This is the v2 direction for the project: Claude, Codex, and temporary sub-agents
are treated as peers attached to a neutral mission board.

The old `codex_worker` server is still present for the original chat experiment.
The new server is `easycfd_mcp.multiagent_studio.server`.

## Shape

```text
Human
  |
  v
Neutral mission board
  |
  +-- Claude context packet
  +-- Codex context packet
  +-- sub-agent context packets
  +-- shared artifacts
  +-- shared reviews
  +-- shared decisions
```

No agent owns the frame. Agents submit structured messages and artifacts to the
mission board, then request context packets from the same source.

## Storage

Tasks are persisted as JSON files:

```text
.multiagent/
  tasks/
    task_xxx.json
  memory/
    human.md
    project.md
    agents.md
```

The file-backed design is deliberate. It makes the orchestration state easy to
inspect, diff, repair, and version later if desired.

## Target Workspaces

Multi-Agent Studio can act as a command-center repo for other repositories.
The mission board stays in this repo, while each task can point at a target
workspace:

```text
multiagent-chat/
  .multiagent/tasks/task_xxx.json   # coordination state

other-project/
  src/
  tests/                            # actual implementation target
```

Set `workspace_path` when creating a task:

```json
{
  "goal": "Build the accountability system",
  "workspace_path": "~/github/wt_accountability_system"
}
```

Context packets include both paths:

- mission board root: where orchestration state is stored
- target workspace: where agents should inspect, edit, run commands, and test

Claude is launched with its current working directory set to the target
workspace. Codex is launched with `codex exec -C <target workspace>`. If a task
does not specify `workspace_path`, the current Multi-Agent Studio repo remains
the target workspace for backward compatibility.

## MCP Tools

- `studio_create_task`: create a task and return initial Claude/Codex context.
- `studio_list_tasks`: summarize existing tasks.
- `studio_get_context`: get a role-specific packet for any agent.
- `studio_submit_message`: submit a structured message.
- `studio_assign_work`: add a mission-board work item.
- `studio_update_work`: update work status and artifact links.
- `studio_add_artifact`: register patches, reports, docs, or logs.
- `studio_add_review`: attach a review verdict to an artifact.
- `studio_write_memory`: write shared memory used in future packets.
- `studio_heartbeat`: record that an agent is still active on a task.
- `studio_check_stalled`: detect agents with open work and stale heartbeats.
- `studio_run_orchestrator`: run one deterministic autopilot step, or the LLM policy backend with `mode="policy"`.

## Dashboard UI

Run the local dashboard with:

```text
python -m easycfd_mcp.multiagent_studio.ui_server
```

Then open:

```text
http://127.0.0.1:8765
```

The UI reads `.multiagent/tasks/*.json` and presents the collaboration as a
mission-control view:

- left: task list and latest activity
- center: summarized conversation thread
- right: agent heartbeats, mission board, artifacts, and reviews
- top right: Japanese/English UI switch
- composer: send a human message to `orchestrator`, `claude`, `codex`, or `all`

Conversation messages are summarized by default so the human can track the flow
without reading every token. Each message has a `Full text` button, and the
thread also has a `Show full text` toggle when exact wording matters.

The UI deliberately separates:

- what happened: message timeline
- who owns the next step: mission board
- whether agents are alive: heartbeat panel
- what can be inspected: artifacts and reviews

This is the main Codex-side improvement to Claude's three-column dashboard idea:
the center is not a raw chat log. It is a digest-first control surface with
selective expansion for full provenance.

Browser-submitted messages are written back into the selected task JSON as:

```text
sender=human
recipient=orchestrator | claude | codex | all
metadata.source=dashboard
metadata.actor=human
metadata.identity_locked=true
```

This gives the human a way to steer the orchestrator without editing JSON files
or jumping back into the terminal.

The dashboard endpoint is intentionally human-only. It rejects attempts to set
`sender=claude`, `sender=codex`, or any other agent from the browser API. Agent
messages must enter through the agent runner, MCP tools, or mission-board
helpers that stamp their own `metadata.actor`.

Do not use the dashboard POST endpoint for Codex-side smoke tests unless the
test is explicitly pretending to be the human. If Codex initiates a verification
message, it must be recorded as `sender=codex`.

When a dashboard message is addressed to `orchestrator`, the UI server now
auto-dispatches it:

```text
human -> orchestrator
  -> infer target agent from message text
  -> add orchestrator -> agent handoff
  -> launch agent CLI with task context
  -> save agent response as report_result
```

Direct human messages also wake the addressed agent:

```text
human -> claude  -> launch Claude
human -> codex   -> launch Codex
human -> all     -> launch Claude and Codex
```

This is important for the dashboard composer. A direct message should not merely
be stored as a log entry; it should create an orchestrator handoff and start the
target CLI session.

Agent messages to the orchestrator are also actionable when they are structured
as `proposal`, `request_review`, or `objection`:

```text
claude -> orchestrator (proposal mentioning Codex)
  -> orchestrator decision
  -> orchestrator -> codex handoff
  -> launch Codex
```

This prevents agent proposals from becoming inert log entries.

Agent result reports are now orchestrator inputs too. When an agent writes
`sender=<agent>, recipient=orchestrator, type=report_result`, the runner asks
the autopilot layer whether the report implies a next step:

```text
actionable trigger
  -> autopilot plan
  -> orchestrator decision
  -> mission-board work/status updates
  -> optional auto-dispatch to Claude or Codex
```

The autopilot layer is deterministic and bounded:

- human instructions become tracked work items before agents are launched
- agent proposals become peer work instead of inert log entries
- agent reports update referenced work status
- blocked/error/permission reports become recovery work
- Codex completion reports become Claude review work
- stale open work becomes takeover work for the other agent
- running agents block overlapping dispatch

Each generated decision/work records `metadata.source=autopilot`, and the
orchestrator refuses to handle the same trigger twice. This makes the
orchestrator a task coordinator instead of a passive log router while keeping
the mission board auditable.

Autopilot-generated peer review work is intentionally not allowed to spawn
another autonomous work item from its own result report. This prevents loops
such as:

```text
Codex report -> Claude review -> Codex fix -> Claude review -> ...
```

If deeper iteration is needed, the human or the explicit `mode=policy` LLM
orchestrator step should make that decision.

Run one deterministic step with:

```text
studio_run_orchestrator(task_id="task_xxx", mode="autopilot")
```

Use `dry_run=true` to inspect planned work without applying it. Use
`mode="policy"` only when you intentionally want the LLM JSON policy backend.
Without an explicit `trigger_message_id`, autopilot only considers the latest
message. Pass `scan_backlog=true` only when you intentionally want to process
older unhandled messages.

The dashboard also exposes a top-bar `Stop agents` / `強制停止` button. It calls:

```text
POST /api/stop_agents
```

The backend terminates currently registered auto-invoked Claude/Codex CLI
processes for the selected task and records a stopped heartbeat. This is
different from `Reload MCP`, which only clears saved session ids for the next
invocation.

The current local runners are:

- Claude: `claude --print --output-format text --tools "" --session-id <uuid>`
- Codex: `codex exec -s read-only -C <repo>` and then `codex exec resume <session_id>`

Automatic invocations use a hybrid context model. Claude/Codex keep their own
CLI session when possible, while the mission board remains the authoritative
backup, audit log, and handoff source. Every invocation still receives the full
role-specific context packet, and the prompt explicitly says that the mission
board wins if the resumed CLI context disagrees with it.

Automatic invocations are intentionally read-only. They are instructed not to
edit files. Their output is persisted back into the task JSON as a normal
mission-board message.

Work assignments can also auto-dispatch. When `studio_assign_work` creates a
work item owned by `claude` or `codex`, the server writes an
`orchestrator -> agent` handoff, exports the agent inbox, launches the owner,
and records the reply as `sender=<agent>, type=report_result`.

Codex auto-dispatch uses the native Codex CLI when available:

```text
%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe
```

This avoids accidentally picking up an older global npm `codex.cmd` wrapper that
may not support the configured model.

Agent session ids are stored here:

```text
.multiagent/agent_sessions.json
```

They are scoped by task and agent, for example `task_xxx:codex`. If a stored CLI
session cannot be resumed, the runner falls back to a fresh CLI session and
keeps the mission-board packet as the recovery context.

For agents that cannot call the MCP tool directly, auto-dispatch also writes a
plain Markdown inbox file:

```text
.multiagent/inbox/{task_id}_{agent}.md
```

Claude Code can read that file directly when `studio_get_context` is not
available. The task JSON remains the source of truth; the inbox file is a
readable context export.

## LLM Orchestrator

The optional LLM policy backend still has two parts:

```text
LLM policy brain -> JSON actions -> deterministic validator/applier
```

The LLM does not directly edit task files. It receives the mission-board state,
Claude/Codex context packets, and stalled-agent status. It must return JSON
actions such as:

```json
{
  "actions": [
    {
      "type": "assign_work",
      "params": {
        "owner": "codex",
        "title": "Continue Claude's stalled review",
        "description": "Claude has not heartbeated; inspect the latest artifact and produce a review."
      }
    }
  ]
}
```

Python validates the action type and required fields before applying it. This
keeps the LLM in charge of judgment, while deterministic code owns persistence.

The first policy backend uses the local Codex CLI, matching the legacy worker
style:

```text
codex exec -m gpt-5.4 -s workspace-write <policy prompt>
```

Run one policy step with:

```text
studio_run_orchestrator(task_id="task_xxx", mode="policy")
```

Use `dry_run=true` to inspect the LLM's proposed actions without applying them.

## Heartbeats and Takeover

The main failure mode this version targets is agents stopping while the human is
away. Each active agent should periodically call:

```text
studio_heartbeat(task_id="task_xxx", agent="claude", current_work_id="work_xxx")
```

If Claude has open work and no fresh heartbeat, the orchestrator can assign a
handoff task to Codex. If Codex stalls, it can assign the continuation to Claude.

Staleness is controlled by `stale_after_seconds`; the default is 900 seconds.
The check is intentionally simple:

```text
open work owned by agent + missing/stale heartbeat = stalled
```

This does not force an agent to keep running forever by itself. It gives the
other agent enough structured state to continue without asking the human what
happened.

## Autonomy Boundaries

The orchestrator should not stop merely because Claude and Codex have exchanged
a few messages. Productive progress is allowed to continue:

```text
worker executes -> worker reports -> peer reviews -> orchestrator assigns next TASKS.json item
```

Loop prevention is now aimed at non-productive recovery loops. If agents keep
reporting blockers for the same chain, the orchestrator stops generating more
recovery work and asks for human direction. If a peer review approves work and
the target repository has a `TASKS.json`, the next incomplete item is assigned
automatically.

## Recommended Loop

1. Create a task from the human goal.
2. Ask Claude and Codex for independent proposals from their own packets.
3. Submit both proposals to the mission board.
4. Compare the proposals and assign implementation/review work.
5. Codex implements or inspects locally.
6. Claude reviews intent, risk, and product fit.
7. Summon sub-agents for narrow checks: security, tests, docs, UX, or criticism.
8. Register artifacts and reviews.
9. Resolve objections and record decisions.
10. Finalize only after implementation and review are both represented.

When agents are running unattended, add this loop:

```text
agent heartbeat -> work/report/review -> orchestrator step -> stalled check -> handoff if needed
```

The important pattern is:

```text
independent analysis -> compare -> debate -> decide -> execute -> review
```

## Message Types

Use these values for `message_type` when possible:

- `proposal`
- `plan_update`
- `assign_task`
- `report_result`
- `request_review`
- `review`
- `objection`
- `decision`
- `handoff`
- `heartbeat`
- `chat`

Natural language is still fine, but the message type tells the orchestrator what
kind of contribution the agent is making.

## Sub-Agent Pattern

Sub-agents should be narrow and disposable. Good examples:

- `security-reviewer`: review only auth, command execution, or data handling.
- `test-planner`: identify missing tests and edge cases.
- `repo-cartographer`: map relevant files and dependencies.
- `critic`: argue why the current plan might fail.
- `docs-writer`: draft user-facing docs from accepted behavior.

Sub-agents produce reports. They do not own final decisions.
