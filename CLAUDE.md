# CLAUDE.md

## Language Policy

Communicate in Russian **only** when the operator writes in Russian. All documentation, code comments, agent-to-agent communication, and internal reasoning must be in English.

## Keeping This File Up To Date

Update this file whenever you change: architecture, data flow, file roles, or non-trivial design decisions. Do it in the same commit as the code change. If something is obvious from reading the code, don't document it here — only capture what isn't.

---

## Architecture

**What it is:** Local dashboard for Claude Code usage. Reads Claude's JSONL session transcripts, stores them in SQLite, serves a single-page dashboard at `localhost:8087`. No third-party dependencies — stdlib only.

### Data Flow

```
~/.claude/projects/**/*.jsonl   (Claude Code writes one file per session)
  └─> scanner.py                incremental parse: tracks mtime + line count per file
        └─> ~/.claude/usage.db  SQLite: turns, sessions, processed_files
              └─> dashboard.py  queries DB, enriches with costs via pricing.py
                    └─> /api/data (JSON)
                          └─> browser SPA (Chart.js, client-side filtering)
```

### File Roles

| File | Role |
|------|------|
| `scanner.py` | Parses JSONL → SQLite. Incremental: skips unchanged files, appends only new lines. |
| `pricing.py` | Single source of truth for Anthropic API prices. `calc_cost_breakdown()` returns per-component costs. |
| `dashboard.py` | HTTP server + embedded SPA. Applies `tz_offset` server-side so all dates (chart labels, session timestamps, `generated_at`) are in browser local time. |
| `cli.py` | Entry point: `scan`, `today`, `stats`, `reconcile`, `dashboard`. |

### Non-Trivial Decisions

- **Timezone handling is server-side.** Frontend sends `tz_offset` (minutes east of UTC) via `?tz=N`. Backend applies it uniformly: SQL `datetime(timestamp, '+N minutes')` for day grouping, `timedelta` for session timestamps and `generated_at`. No timezone logic in JS.
- **SPA is embedded in `dashboard.py`** as a Python string (`HTML_TEMPLATE`). No build step, no separate files.
- **`reconcile` command** rebuilds session aggregates from the `turns` table — use it if session totals drift out of sync.

---

## Dmitry (MCP Server)

ALL commands and investigations go through Dmitry. When you execute commands or read files directly, raw output accumulates in your context for the rest of the session — every subsequent turn becomes more expensive. Dmitry returns only the filtered result, keeping your context lean.

- **`dmitry_exec`** — any shell command (grep, git, find, cargo, npm, cat, wc, ls...). Common commands return instantly at zero LLM cost. Long output is filtered before reaching you. **This is your primary tool.**
- **`dmitry_ask`** — one-shot investigation when the task requires reading 2+ files or >100 lines of source. You receive a compact answer, not raw file contents. Parallel-safe: multiple asks can run simultaneously. **Write tasks in English.**
- **`dmitry_research`** — same as ask, but remembers all previous calls in the session. Use when you have follow-up questions that build on earlier findings. **Do NOT kill between tasks** — let it accumulate context.
- **`dmitry_research_kill`** — only if research agent gives clearly wrong answers or is stuck on stale context.

**When to use what:**
- Any shell command → `dmitry_exec` (grep, git, cargo check, npm ls, find, cat, wc...)
- "Where is X defined?" → `dmitry_exec("grep -rn 'X' src/")`
- Investigate how a module works, trace call chain, compare files → `dmitry_ask`
- Follow-up on a prior investigation → `dmitry_research`
- Need exact file content before Edit → `Read` directly (only exception)

**Also delegate to `dmitry_ask` / `dmitry_research`:**
- Screenshots and images — analyze UI, find visual bugs, read text from image
- Large documents (PDF, DOCX, HTML) — find specific information without loading the full document into your context
- E2E and integration tests — run Playwright scenarios, get pass/fail result
- Web pages and documentation — fetch a page, extract the specific section you need
- Any task with large input but compact answer that doesn't require your reasoning quality
