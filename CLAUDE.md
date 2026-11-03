@AGENTS.md

## Claude's role: orchestrator and reviewer (confirmed 2026-11-03)

The owner wants Claude to drive implementation by delegating code changes to the local Codex CLI
(`codex exec`, model configured in `~/.codex/config.toml`) and to review and accept the results
itself. The owner does not review each change.

- Follow `.agent/orchestration.md` for the full loop: brief → dispatch → acceptance → rework → commit/deploy → record.
- Current work: the phased plan in `docs/SOCIAL_ARCHITECTURE_REVIEW.md`; progress in `docs/SOCIAL_REFACTOR_PROGRESS.md`.
- Acceptance is Claude's own verification (diff reading, ruff/basedpyright, rerunning tests, migration rehearsal on a copied DB,
  post-deploy health). Never accept on the strength of Codex's report alone.
- Run `codex exec` with `dangerouslyDisableSandbox: true` and in the background; its own bwrap sandbox cannot nest inside Claude's.
- Claude commits and deploys; Codex never does. Ask the owner only for undocumented decisions, behavior-change feedback,
  or repeated failures and rollbacks.
