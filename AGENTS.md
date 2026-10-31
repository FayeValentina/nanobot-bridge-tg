This file provides guidance to AI coding agents working with this repository.

## Owner's migration priority (confirmed 2026-10-31)

- [V5: expression and jargon learning](docs/MIGRATION_V5_PLAN.md) is the next planned work: learn contextual expression from real chat, research and revise jargon meanings, select relevant expressions/stickers before replying, and reduce unrelated catchphrases without hard word bans. The owner's goal is natural meme-aware banter without a catchphrase in every sentence. V5-A/B/C/D/E are planning only, not implemented or deployed; the 2026-10-31 request authorized this document, not runtime changes. V5 takes over V2-E G15; do not implement a duplicate learning pipeline. Preserve V3 broad admission, V4 initiative, shared tool permissions and the no-hash preference.
- [V4: proactive conversation restoration](docs/MIGRATION_V4_PLAN.md) A/B/C is implemented, validated and deployed; see [V4-C](docs/V4C_IMPLEMENTATION.md) for final behavior, costs, limits and release evidence. The subsequent [participation fix](docs/PARTICIPATION_FIX.md) admits open chat invitations without requiring a mention; the owner's idle preset now uses 0.6 base probability and nightMultiplier=1.0, retaining 30–90 minute checks. The deployed [broad V3 admission](docs/BROAD_V3_ADMISSION.md) now defaults fresh public conversation to the main model, leaving it to choose speech or silence. Schema 13, social-judge-v8, main social protocol 5 (sticker-pack learning). Do not start V2-C as a separate duplicate batch.
- After V5, review the remaining V2-D/E/F and unmerged G09 statistics when requested; M5 remains deferred. Earlier V4 planning-only notes describe that historical planning task, not the later authorized implementation.
- The preceding [group image participation fix](docs/IMAGE_WAKE_FIX.md) is preserved: V3 sees bounded native images before deciding; ordinary conversation waits do not inherit standing question-only preferences.
- V4 covers idle autonomous wakes without new messages or unconsumed memories, proactive conversation prompts, pending questions/thoughts with motivation, pacing, reply continuity and at most two proactive attempts per matter.
- Bring the necessary V2-C statistics/scheduling and V2-E follow-up memory fields into V4; defer unrelated V2 scope and M5. V3 remains the semantic entry for incoming messages; the planned V4 idle timer event goes directly to the existing main-agent execution path as documented.
- The 2026-10-30 V4 planning task is documentation only. Record implementation and deployment status separately when later implementation is requested; do not treat planned behavior as already implemented.

## Owner's local workflow (confirmed 2026-10-28)

- This migration is a personal project. The actual local Docker instance is used only by the owner, not a commercial production environment. Prioritize quick feedback from that instance.
- After each runnable batch of behavior changes or fixes, perform the necessary checks, commit the changes, and promptly deploy to the actual local instance before finishing the task. Routine local releases and their necessary service restarts are already authorized; do not ask for approval again unless the owner requests a different scope.
- The default target is `/home/ubuntu/.local/share/nanobot-bridge-tg-deploy/`, WebUI port `8765`, health port `18790`. The staging instance is optional, not a release prerequisite.
- Do not require a dedicated test group, test Bot, a staging soak period, or commercial release approval steps. Use the owner's existing local configuration and actual usage for feedback. Keep only one update receiver per Telegram Bot Token.
- Reuse the existing build, automatic backup, health check, and failure rollback pipeline. Scale checks to the change and avoid adding defensive process that delays local iteration without a concrete need.
- Documentation-only changes, including recording this preference, do not need a runtime rebuild or deployment. Do not remove application permissions or data isolation merely to simplify the release workflow.
- Current deployment instructions: [`docs/LOCAL_PIPELINE.md`](docs/LOCAL_PIPELINE.md). This owner-confirmed workflow supersedes older requirements in migration or deployment records to validate in a separate instance/group first.

## Owner's bot conversation preference (confirmed 2026-10-31)

- The owner wants autonomous sticker-pack learning and contextual sticker replies. The main model can import a Telegram pack link/name, view numbered sheets, annotate visible text/emotion/uses, then search and send. Preserve the owner's fewer-suggestions preference and the distinction between importing metadata and actually viewing images. See `docs/STICKER_PACK_LEARNING.md`; static stickers only, existing group/topic libraries and delivery budgets apply.
- For banter, teasing and emotional reactions, the owner explicitly prefers frequent sticker use and choosing a fitting learned sticker before composing text. Search the library proactively, allow sticker-only replies, and avoid restating its meaning in text or waiting to be reminded to send one.
- The bound group persona should give fewer unsolicited suggestions and favor contextual banter and teasing. Unless members explicitly ask a question, seek help, or request an action, do not turn their updates, complaints, photos, short replies or emoji into assistant-style advice, plans or reminders. Explicit requests are semantic, not dependent on question marks. This governs how the main model replies, not whether V3 admits ordinary conversation; preserve broad admission and the owner's short-message bubble preference.
- V3 must broadly admit fresh group conversation, including unaddressed memes/repetition, laughter, moods, quotes, media and ordinary public exchanges between other members. Do not require an explicit call, task/question or a proven valuable opening. Let the main model decide whether to participate or stay silent; uncertainty about relevance should generally wake. Retain explicit selective waits, explicitly private/no-interjection exchanges and host self/duplicate/stale protections. This supersedes older narrow fixture expectations based solely on another addressee, quotation or low information.
- Other bots are group participants, including without an @mention. Send their authorized fresh messages through the same V3 semantic assessment as human messages; do not add a bot-only mention gate or special mutual-chat cap/cooldown.
- Correct account identity is needed to understand the conversation, not to prohibit joining it. Naming another participant is not an automatic refusal when the target can naturally join an ongoing shared exchange or members invite bots to chat together. Still distinguish closed questions to other members from tasks addressed to this bot.
- Keep V3 wake/observe, voluntary silence, self-message/duplicate/stale protections and existing publishing limits. See `docs/BOT_CONVERSATION_PARTICIPATION.md`; this supersedes the earlier explicit-address-only peer-bot stage.

## Owner's no-hash migration preference (confirmed 2026-10-30)

- The owner explicitly rejects using hashes or fingerprints in this migration's implementation and acceptance checks. Do not introduce hash-based content comparisons, deduplication, configuration versions, input signatures, or validation reports.
- Prefer existing message/record IDs, explicit revision numbers, and direct comparison of the relevant source values. Do not add hash layers as a default correctness measure.
- The follow-up cleanup removes migration-owned content/input hashes, derived configuration/persona signatures, hashed memory/media keys and release checksums. Use message revisions, batch IDs, direct settings/value comparisons and ordinary release metadata. Do not reuse older fingerprint-based acceptance probes; compare source values directly.

## Owner's shared-tool behavior (confirmed 2026-10-28)

- The owner explicitly requests identical tool permissions in social and ordinary assistant modes. Do not add a social-only tool allowlist, role editing prohibition, MCP/skill restriction, or ban on ordinary cross-chat/background delivery.
- Both modes use the ordinary session defaults for enabled tools, profile, skills, memory, archiving and commands. Global tool switches, workspace/sandbox rules and container permissions apply equally.
- Keep the social role binding and per-conversation history. Social mode differs through message observation, coalescing, explicit public delivery, waking, waiting and sleeping.
- Ordinary message tools must actually deliver text and attachments to social topics. Social-specific tools remain available in the shared registry; their message/scope/timer operations still require the relevant runtime data.
- `social_send` takes exactly one of `text` (one message) or `parts` (the complete ordered list of 2–3 messages, including the first). Do not restore the ambiguous `[text, *parts]` behavior; see the deployed [multipart send fix](docs/SOCIAL_SEND_PARTS_FIX.md).
- This owner request supersedes the earlier M0–M3 social permission/isolation design. See `docs/SHARED_TOOL_PERMISSIONS.md`; do not reinstate the retired limits while implementing M4/M5.

## Project Overview

nanobot is a lightweight, open-source AI agent framework written in Python with a React/TypeScript WebUI. It centers around a small agent loop that receives messages from chat channels, invokes an LLM provider, executes tools, and manages session memory.

## Development Commands

```bash
# Python: run single test / lint
pytest tests/test_openai_api.py::test_function -v
ruff check nanobot/

# Strict type checking (matches CI)
uv sync --all-extras --dev
uv run --no-sync python -m scripts.install_channel_dependencies --all-channels
uv run --no-sync basedpyright

# WebUI: dev server (proxies API/WS to gateway :8765), build, test
# Build outputs to ../nanobot/web/dist (bundled into the Python wheel)
cd webui && bun run dev      # or NANOBOT_API_URL=... bun run dev
cd webui && bun run build
cd webui && bun run test

# Gateway
nanobot gateway
```

## High-Level Architecture

### Core Data Flow

Messages flow through an async `MessageBus` (`nanobot/bus/queue.py`) that decouples chat channels from the agent core:

1. **Channels** (`nanobot/channels/`) receive messages from external platforms and publish `InboundMessage` events to the bus.
2. **`AgentLoop`** (`nanobot/agent/loop.py`) consumes inbound messages, builds context, and coordinates the turn.
3. **`AgentRunner`** (`nanobot/agent/runner.py`) handles the actual LLM conversation loop: send messages to the provider, receive tool calls, execute tools, and stream responses.
4. Responses are published as `OutboundMessage` events back to the appropriate channel.

### Key Subsystems

- **Agent Loop** (`nanobot/agent/loop.py`, `runner.py`): The core processing engine. `AgentLoop` manages session keys, hooks, and context building. `AgentRunner` executes the multi-turn LLM conversation with tool execution.
- **LLM Providers** (`nanobot/providers/`): Provider implementations (Anthropic, OpenAI-compatible, OpenAI Responses API, Azure, Bedrock, GitHub Copilot, OpenAI Codex, etc.) built on a common base (`base.py`). Includes image generation (`image_generation.py`) and audio transcription (`transcription.py`). `factory.py` and `registry.py` handle instantiation and model discovery.
- **Channels** (`nanobot/channels/`): Platform integrations (Telegram, Discord, Slack, Feishu, Matrix, WhatsApp, QQ, WeChat, WeCom, DingTalk, Email, MoChat, MS Teams, WebSocket, Mattermost). `manager.py` discovers and coordinates them. Channels are self-contained packages auto-discovered via `pkgutil` scanning.
- **Tools** (`nanobot/agent/tools/`): Agent capabilities exposed to the LLM: filesystem (read/write/edit/list), shell execution (with sandbox backends), web search/fetch, MCP servers, cron, notebook editing, subagent spawning, long-running tasks / sustained goals (`long_task.py`), image generation, and self-modification. Tools are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Memory** (`nanobot/agent/memory.py`): Session history persistence with Dream two-phase memory consolidation. Uses atomic writes with fsync for durability.
- **Session Management** (`nanobot/session/`): Per-session history, context compaction, TTL-based auto-compaction (`manager.py`), and sustained goal state tracking (`goal_state.py`).
- **Config** (`nanobot/config/schema.py`, `loader.py`): Pydantic-based configuration loaded from `~/.nanobot/config.json`. Supports camelCase aliases for JSON compatibility.
- **WebUI** (`webui/`): Vite-based React SPA that talks to the gateway over a WebSocket multiplex protocol. The dev server proxies `/api`, `/webui`, `/auth`, and WebSocket traffic to the gateway.
- **API Server** (`nanobot/api/server.py`): OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) for programmatic access.
- **Command Router** (`nanobot/command/`): Slash command routing and built-in command handlers.
- **Heartbeat** (`nanobot/templates/HEARTBEAT.md`): Periodic task list checked via `cron` jobs (legacy dedicated service removed).
- **Pairing** (`nanobot/pairing/`): DM sender approval store with persistent pairing codes per channel.
- **Skills** (`nanobot/skills/`): Built-in skill definitions (cron, github, image-generation, etc.) loaded into agent context.
- **Security** (`nanobot/security/`): PTH file guard and other security measures activated at CLI entry.

### Entry Points

- **CLI**: `nanobot/cli/commands.py`
- **Python SDK**: `nanobot/nanobot.py`

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

## Contribution Flow

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for contribution flow and PR guidelines.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored).
- pytest with `asyncio_mode = "auto"`.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Channel base / new channel template: `nanobot/channels/base.py`
- Tool registry: `nanobot/agent/tools/registry.py`
- WebUI dev proxy config: `webui/vite.config.ts`
- Tests mirror the `nanobot/` package structure.
