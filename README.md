# Diabase

*Read this in: [Italiano](README.it.md)*

**Agent-safe control plane for Supabase, self-hosted or cloud.**

Diabase lets AI agents (Claude Code, or any LLM via API) operate and manage your entire Supabase backend — schema, edge functions, auth workflows, storage — while you watch every move from a modern GUI and stay in charge of the decisions that matter.

> ⚠️ **Status: early development.** Nothing here is ready for production use yet. Follow along or star the repo — the first usable release will be announced.

## Why

Supabase's official MCP executes directly against your production project: no plan, no diff, no staged approval, no audit trail. Diabase is the opinionated layer that makes agent-driven backend management safe enough for a company to allow:

- **Plan & Approve** — large operations become a readable plan you approve, reject, or amend; destructive steps are highlighted
- **Audit trail** — every action, human or AI, recorded append-only
- **Adapters** — the same experience against Supabase self-hosted or Supabase Cloud
- **Projects** — Claude-style workspaces with their own system prompt, context files and autonomy level
- **Your LLM, your choice** — Claude Code CLI (subscription) or pay-per-use API, configured in settings

## License

[Apache 2.0](LICENSE)
