# Timezone & Session Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix day boundary calculations to use browser local timezone, and make session cost/model display accurate by using turn-level data instead of pre-aggregated session totals.

**Architecture:** Backend accepts `tz` offset parameter (minutes) and adjusts all SQL day groupings accordingly; adds new `session_model_daily` dataset grouped by (session_id, model, local_day); frontend uses local dates for filter cutoffs and rebuilds session display from the new granular dataset.

**Tech Stack:** Python (stdlib HTTPServer, SQLite), vanilla JS in HTML template (single-file dashboard.py)

---

## Problem Summary

Two independent bugs, one root cause (UTC assumption):

**Bug 1 — Timezone**: Day boundaries use `substr(timestamp, 1, 10)` on UTC timestamps. For Moscow (UTC+3), day starts at 03:00 local time. Filter "today" misses turns from 00:00–03:00 Moscow.

**Bug 2 — Session accuracy**: Sessions table calculates cost from `sessions.total_*_tokens` — pre-aggregated lifetime totals, not filtered by selected date/model. 46% of sessions use multiple models; `sessions.model` is just the last-used model (COALESCE artifact), so session filtering and cost are both wrong.

---

## Data Flow After Fix

```
Browser → GET /api/data?tz=-180      (Moscow = UTC+3, offset = -180min west = +180min east)
       ↓
Python: tz_minutes = +180
SQL:    datetime(timestamp, '+180 minutes') → local timestamp
        substr(local_ts, 1, 10)            → local day string

Returns:
  daily_by_model     — existing, now local-day-grouped
  session_model_daily — new: (session_id, model, local_day, tokens...)
  sessions_all        — existing, metadata only (no token totals used for cost anymore)

Frontend:
  getRangeCutoff() → local date strings (new Date().toLocaleDateString('sv'))
  applyFilter():
    filteredSmd = session_model_daily filtered by local date + model
    smdBySession = aggregate filteredSmd per session (keeps per-model breakdown)
    filteredSessions = sessions_all metadata + smdBySession tokens
    session cost = sum of calcCost() per model in smdBySession[session_id].byModel
```

---

## File Map

Only one file changes: `dashboard.py`

| Section | Lines (approx) | What changes |
|---|---|---|
| `get_dashboard_data(db_path, tz_offset)` | 14–94 | Add `tz_offset` param, inject into SQL as `datetime(timestamp, '+N minutes')` |
| New SQL query `session_model_daily` | new ~15 lines | Group turns by session_id + model + local_day |
| `DashboardHandler.do_GET` | 660–680 | Parse `?tz=` query param, pass to `get_dashboard_data` |
| JS: `getLocalDate()` | new helper | Returns `YYYY-MM-DD` in local time |
| JS: `getRangeCutoff()` | ~319–330 | Use `getLocalDate()` instead of `toISOString()` |
| JS: `loadData()` | ~619–650 | Append `?tz=` to fetch URL |
| JS: `applyFilter()` | ~474–546 | Rebuild session aggregation from `session_model_daily` |
| JS: `renderSessionsTable()` | ~656–675 | Show all models per session, cost from per-model breakdown |

---

## Task 1: Add tz_offset param to backend — SQL day calculations

**Files:**
- Modify: `dashboard.py` — function `get_dashboard_data()` and all SQL day expressions

- [ ] **Step 1: Update `get_dashboard_data` signature and add SQL helper**

Replace the function signature and add a local day expression:

```python
def get_dashboard_data(db_path=DB_PATH, tz_offset=0):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Build local-day SQL expression: adjust UTC timestamp by tz_offset minutes
    # tz_offset is negative for west-of-UTC, positive for east-of-UTC
    # e.g. Moscow UTC+3 → tz_offset=180 → '+180 minutes'
    sign = '+' if tz_offset >= 0 else '-'
    local_day = f"substr(datetime(timestamp, '{sign}{abs(tz_offset)} minutes'), 1, 10)"
```

- [ ] **Step 2: Replace UTC day expression in `daily_rows` query**

Find this SQL in `get_dashboard_data`:
```python
    daily_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
```

Replace with:
```python
    daily_rows = conn.execute(f"""
        SELECT
            {local_day}                as day,
```

- [ ] **Step 3: Verify Python parses OK**

```bash
python3 -c "from dashboard import get_dashboard_data; d = get_dashboard_data(tz_offset=180); print('days sample:', d['daily_by_model'][:2])"
```

Expected: runs without error, days look like `2026-04-08`.

- [ ] **Step 4: Commit**

```bash
git add dashboard.py
git commit -m "feat: add tz_offset param to get_dashboard_data, use local-day in daily SQL"
```

---

## Task 2: Add session_model_daily query to backend

**Files:**
- Modify: `dashboard.py` — `get_dashboard_data()`, add new SQL query and return field

- [ ] **Step 1: Add `session_model_daily` query after `session_rows` query**

Insert after the `session_rows` block (after line `sessions_all = []` loop closes):

```python
    # ── Per-session per-model per-day token breakdown ─────────────────────────
    smd_rows = conn.execute(f"""
        SELECT
            session_id,
            COALESCE(model, 'unknown') as model,
            {local_day}                as day,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY session_id, model, day
        ORDER BY session_id, day
    """).fetchall()

    session_model_daily = [{
        "session_id": r["session_id"],
        "model":      r["model"],
        "day":        r["day"],
        "input":      r["input"] or 0,
        "output":     r["output"] or 0,
        "cache_read": r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":      r["turns"] or 0,
    } for r in smd_rows]
```

- [ ] **Step 2: Add `session_model_daily` to the returned dict**

Find the return statement at the end of `get_dashboard_data`:
```python
    return {
        "all_models":     all_models,
        "daily_by_model": daily_by_model,
        "sessions_all":   sessions_all,
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
```

Replace with:
```python
    return {
        "all_models":          all_models,
        "daily_by_model":      daily_by_model,
        "sessions_all":        sessions_all,
        "session_model_daily": session_model_daily,
        "generated_at":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
```

- [ ] **Step 3: Verify data shape**

```bash
python3 -c "
from dashboard import get_dashboard_data
d = get_dashboard_data(tz_offset=180)
smd = d['session_model_daily']
print(f'session_model_daily rows: {len(smd)}')
print('sample:', smd[:3])
# Check mixed-model session
from collections import defaultdict
by_session = defaultdict(set)
for r in smd:
    by_session[r['session_id']].add(r['model'])
multi = {k: v for k, v in by_session.items() if len(v) > 1}
print(f'sessions with multiple models: {len(multi)}')
"
```

Expected: ~400–700 rows, ~10+ sessions with multiple models.

- [ ] **Step 4: Commit**

```bash
git add dashboard.py
git commit -m "feat: add session_model_daily to API response"
```

---

## Task 3: HTTP handler — parse tz param and pass to backend

**Files:**
- Modify: `dashboard.py` — `DashboardHandler.do_GET`

- [ ] **Step 1: Parse `tz` query parameter in `do_GET`**

Find the `/api/data` handler:
```python
        elif self.path == "/api/data":
            data = get_dashboard_data()
```

Replace with:
```python
        elif self.path.startswith("/api/data"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            try:
                tz_offset = int(qs.get('tz', ['0'])[0])
                tz_offset = max(-840, min(840, tz_offset))  # clamp to valid range
            except (ValueError, IndexError):
                tz_offset = 0
            data = get_dashboard_data(tz_offset=tz_offset)
```

- [ ] **Step 2: Verify manually**

```bash
python3 -c "
from http.server import HTTPServer
# Just check the module loads without error
import dashboard
print('OK')
"
```

- [ ] **Step 3: Commit**

```bash
git add dashboard.py
git commit -m "feat: parse tz query param in HTTP handler, pass to get_dashboard_data"
```

---

## Task 4: Frontend — local date helper and filter cutoff fix

**Files:**
- Modify: `dashboard.py` — JS section, `getRangeCutoff()` and `loadData()`

- [ ] **Step 1: Add `getLocalDate()` helper in JS**

Insert after `const RANGE_TICKS = ...` line:

```javascript
// ── Timezone helpers ────────────────────────────────────────────────────────
// Returns YYYY-MM-DD string in browser local time
function getLocalDate(date) {
  return date.toLocaleDateString('sv'); // 'sv' locale gives ISO format YYYY-MM-DD
}

// Offset to pass to backend: minutes east of UTC (opposite of JS getTimezoneOffset)
function getTzOffset() {
  return -new Date().getTimezoneOffset(); // JS returns minutes WEST, we want EAST
}
```

- [ ] **Step 2: Fix `getRangeCutoff` to use local dates**

Replace the current `getRangeCutoff` function:
```javascript
function getRangeCutoff(range) {
  if (range === 'all') return { from: null, to: null };
  if (range === 'custom') {
    const f = document.getElementById('date-from').value || null;
    const t = document.getElementById('date-to').value || null;
    return { from: f, to: t };
  }
  const days = range === '1d' ? 0 : range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return { from: d.toISOString().slice(0, 10), to: null };
}
```

With:
```javascript
function getRangeCutoff(range) {
  if (range === 'all') return { from: null, to: null };
  if (range === 'custom') {
    const f = document.getElementById('date-from').value || null;
    const t = document.getElementById('date-to').value || null;
    return { from: f, to: t };
  }
  const days = range === '1d' ? 0 : range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return { from: getLocalDate(d), to: null };
}
```

- [ ] **Step 3: Update `loadData()` to pass tz to API**

Find:
```javascript
    const resp = await fetch('/api/data');
```

Replace with:
```javascript
    const resp = await fetch('/api/data?tz=' + getTzOffset());
```

- [ ] **Step 4: Commit**

```bash
git add dashboard.py
git commit -m "feat: use local timezone for filter cutoffs, pass tz offset to API"
```

---

## Task 5: Frontend — rebuild applyFilter to use session_model_daily

**Files:**
- Modify: `dashboard.py` — JS `applyFilter()` function

- [ ] **Step 1: Replace the session filtering block in `applyFilter()`**

Find this block (after `const byModel = ...`):
```javascript
  // Filter sessions by model + date range
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && inRange(s.last_date)
  );

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }
```

Replace with:
```javascript
  // Aggregate session_model_daily by session, filtered by model + date range
  const smdBySession = {};
  for (const r of rawData.session_model_daily) {
    if (!selectedModels.has(r.model) || !inRange(r.day)) continue;
    if (!smdBySession[r.session_id]) {
      smdBySession[r.session_id] = { input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, byModel: {} };
    }
    const s = smdBySession[r.session_id];
    s.input          += r.input;
    s.output         += r.output;
    s.cache_read     += r.cache_read;
    s.cache_creation += r.cache_creation;
    s.turns          += r.turns;
    if (!s.byModel[r.model]) s.byModel[r.model] = { input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const m = s.byModel[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
  }

  // Join with sessions_all metadata; cost from per-model breakdown
  const filteredSessions = rawData.sessions_all
    .filter(s => smdBySession[s.session_id])
    .map(s => {
      const agg = smdBySession[s.session_id];
      const cost = Object.entries(agg.byModel).reduce(
        (total, [model, t]) => total + calcCost(model, t.input, t.output, t.cache_read, t.cache_creation), 0
      );
      return {
        ...s,
        input:      agg.input,
        output:     agg.output,
        cache_read: agg.cache_read,
        cache_creation: agg.cache_creation,
        turns:      agg.turns,
        models:     Object.keys(agg.byModel),
        cost,
      };
    });

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    for (const model of s.models) {
      if (modelMap[model]) modelMap[model].sessions++;
    }
  }
```

- [ ] **Step 2: Update totals cost calculation**

The `totals.cost` is currently summed from `byModel`. This is correct — leave it as-is. But verify it still compiles by running:

```bash
python3 -c "import dashboard; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add dashboard.py
git commit -m "feat: rebuild session filtering from session_model_daily — fixes cost accuracy and date filtering"
```

---

## Task 6: Frontend — fix renderSessionsTable for multiple models

**Files:**
- Modify: `dashboard.py` — `renderSessionsTable()` function

- [ ] **Step 1: Update renderSessionsTable to use pre-computed cost and models list**

Replace the full `renderSessionsTable` function:
```javascript
function renderSessionsTable(sessions) {
  document.getElementById('sessions-body').innerHTML = sessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td class="muted" style="font-family:monospace">${s.session_id}&hellip;</td>
      <td>${s.project}</td>
      <td class="muted">${s.last}</td>
      <td class="muted">${s.duration_min}m</td>
      <td><span class="model-tag">${s.model}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}
```

With:
```javascript
function renderSessionsTable(sessions) {
  document.getElementById('sessions-body').innerHTML = sessions.map(s => {
    // cost is pre-computed in applyFilter from per-model breakdown
    const hasBillable = s.models.some(m => isBillable(m));
    const costCell = hasBillable
      ? `<td class="cost">${fmtCost(s.cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const modelTags = s.models
      .map(m => `<span class="model-tag">${m}</span>`)
      .join(' ');
    return `<tr>
      <td class="muted" style="font-family:monospace">${s.session_id}&hellip;</td>
      <td>${s.project}</td>
      <td class="muted">${s.last}</td>
      <td class="muted">${s.duration_min}m</td>
      <td>${modelTags}</td>
      <td class="num">${fmt(s.turns)}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}
```

- [ ] **Step 2: Manual smoke test**

Start dashboard and verify:
```bash
python3 cli.py dashboard
```
- Sessions table shows multiple model tags for mixed sessions
- Est. Cost in Sessions and total in Cost by Model are close (will differ for sessions spanning outside the filter window, which is correct)
- Filter "1d" shows only today's sessions (local time)
- Switching to Moscow user: verify no off-by-3h issues

- [ ] **Step 3: Commit**

```bash
git add dashboard.py
git commit -m "feat: show all models per session, use pre-computed cost from per-model breakdown"
```

---

## Self-Review

**Spec coverage:**
- ✅ Timezone fix: Tasks 1, 4 — backend SQL and frontend filter cutoffs
- ✅ session_model_daily: Tasks 2, 5 — new query + frontend rebuild
- ✅ Mixed model sessions: Tasks 5, 6 — per-model aggregation + multiple tags
- ✅ Session cost accuracy: Task 5 — cost computed from filtered turn data

**Placeholder scan:** None found — all steps have concrete code.

**Type consistency:**
- `smdBySession[session_id].byModel[model]` used consistently in Tasks 5 and 6
- `s.models` (array) set in Task 5, consumed in Task 6
- `s.cost` set in Task 5, consumed in Task 6
- `getLocalDate()` defined Task 4, used Task 4 only — ✅
- `getTzOffset()` defined Task 4, used Task 4 — ✅

**Edge cases covered:**
- `tz_offset` clamped to ±840 minutes (valid timezone range) in Task 3
- Sessions with zero turns in filter window excluded (`.filter(s => smdBySession[s.session_id])`)
- Sessions with only non-billable models: `hasBillable` check in Task 6
