"""
dashboard.py - Local web dashboard served on localhost:8087.
"""

import json
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timedelta

from pricing import calc_cost_breakdown, is_billable

DB_PATH = Path.home() / ".claude" / "usage.db"


def get_dashboard_data(db_path=DB_PATH, tz_offset=0):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Build local-day SQL expression: adjust UTC timestamp by tz_offset minutes
    # tz_offset is positive for east-of-UTC (e.g. Moscow UTC+3 → tz_offset=180)
    sign = '+' if tz_offset >= 0 else '-'
    local_day = f"substr(datetime(timestamp, '{sign}{abs(tz_offset)} minutes'), 1, 10)"

    # ── All models (for filter UI) ────────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model
        FROM turns
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [
        {"model": r["model"], "billable": is_billable(r["model"])}
        for r in model_rows
    ]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute(f"""
        SELECT
            {local_day}                as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, model
        ORDER BY day, model
    """).fetchall()

    daily_by_model = []
    for r in daily_rows:
        inp = r["input"]          or 0
        out = r["output"]         or 0
        cr  = r["cache_read"]     or 0
        cc  = r["cache_creation"] or 0
        bd  = calc_cost_breakdown(r["model"], inp, out, cr, cc)
        daily_by_model.append({
            "day":                 r["day"],
            "model":               r["model"],
            "input":               inp,
            "output":              out,
            "cache_read":          cr,
            "cache_creation":      cc,
            "turns":               r["turns"] or 0,
            "billable":            bd["billable"],
            "input_cost":          bd["input_cost"],
            "output_cost":         bd["output_cost"],
            "cache_read_cost":     bd["cache_read_cost"],
            "cache_creation_cost": bd["cache_creation_cost"],
            "cache_savings":       bd["cache_savings"],
            "cost":                bd["cost"],
        })

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()

    tz_delta = timedelta(minutes=tz_offset)
    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
            t2_local = t2 + tz_delta
        except Exception:
            duration_min = 0
            t2_local = None
        sessions_all.append({
            "session_id":   r["session_id"][:8],
            "project":      r["project_name"] or "unknown",
            "last":         t2_local.strftime("%Y-%m-%d %H:%M") if t2_local else "",
            "last_date":    t2_local.strftime("%Y-%m-%d") if t2_local else "",
            "duration_min": duration_min,
            "model":        r["model"] or "unknown",
            "turns":        r["turn_count"] or 0,
        })

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

    session_model_daily = []
    for r in smd_rows:
        inp = r["input"]          or 0
        out = r["output"]         or 0
        cr  = r["cache_read"]     or 0
        cc  = r["cache_creation"] or 0
        bd  = calc_cost_breakdown(r["model"], inp, out, cr, cc)
        session_model_daily.append({
            "session_id":          r["session_id"][:8],
            "model":               r["model"],
            "day":                 r["day"],
            "input":               inp,
            "output":              out,
            "cache_read":          cr,
            "cache_creation":      cc,
            "turns":               r["turns"] or 0,
            "billable":            bd["billable"],
            "input_cost":          bd["input_cost"],
            "output_cost":         bd["output_cost"],
            "cache_read_cost":     bd["cache_read_cost"],
            "cache_creation_cost": bd["cache_creation_cost"],
            "cache_savings":       bd["cache_savings"],
            "cost":                bd["cost"],
        })

    # ── Cache-eviction + compact events (client filters by range + model) ─────
    cache_event_rows = conn.execute(f"""
        SELECT
            session_id,
            timestamp,
            {local_day}      as day,
            gap_min,
            category,
            rewritten_tokens,
            COALESCE(model, '') as model
        FROM cache_events
        ORDER BY timestamp
    """).fetchall()
    cache_events = [
        {
            "session_id":       r["session_id"],
            "day":              r["day"],
            "gap_min":          r["gap_min"],
            "category":         r["category"],
            "rewritten_tokens": r["rewritten_tokens"],
            "model":            r["model"],
        }
        for r in cache_event_rows
    ]

    compact_event_rows = conn.execute(f"""
        SELECT
            session_id,
            timestamp,
            {local_day} as day,
            trigger,
            pre_tokens,
            COALESCE(model, '') as model
        FROM compact_events
        ORDER BY timestamp
    """).fetchall()
    compact_events = [
        {
            "session_id": r["session_id"],
            "day":        r["day"],
            "trigger":    r["trigger"],
            "pre_tokens": r["pre_tokens"],
            "model":      r["model"],
        }
        for r in compact_event_rows
    ]

    conn.close()

    return {
        "all_models":          all_models,
        "daily_by_model":      daily_by_model,
        "sessions_all":        sessions_all,
        "session_model_daily": session_model_daily,
        "cache_events":        cache_events,
        "compact_events":      compact_events,
        "generated_at":        (datetime.utcnow() + tz_delta).strftime("%Y-%m-%d %H:%M:%S"),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #100e0d;
    --card: #211d18;
    --card-hover: #2a2520;
    --border: #2e2824;
    --border-muted: #3a322d;
    --text: #ece5de;
    --muted: #7d6f63;
    --accent: #d97757;
    --blue: #60a5fa;
    --green: #4ade80;
    --yellow: #fcd34d;
    --red: #f87171;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 14px;
    line-height: 1.5;
  }

  header {
    background: var(--card);
    border-bottom: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  header h1 { font-size: 14px; font-weight: 600; color: var(--accent); letter-spacing: -0.01em; }
  .header-right { display: flex; align-items: center; gap: 10px; }
  header .meta { color: var(--muted); font-size: 11px; font-family: 'JetBrains Mono', monospace; }

  #filter-bar {
    background: var(--card);
    border-bottom: 1px solid var(--border);
    padding: 8px 24px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .filter-label { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 18px; background: var(--border-muted); flex-shrink: 0; }
  #model-checkboxes { display: flex; flex-wrap: wrap; gap: 5px; }
  #other-models-details { margin-left: 2px; }
  #other-models-details summary { font-size: 11px; color: var(--muted); cursor: pointer; user-select: none; padding: 3px 0; }
  #other-models-details summary:hover { color: var(--text); }
  #other-models-wrap { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
  .model-cb-label {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 8px; border-radius: 3px;
    cursor: pointer; font-size: 12px; color: var(--muted);
    transition: color 0.12s, background 0.12s;
    user-select: none;
  }
  .model-cb-label .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--border-muted); flex-shrink: 0;
    transition: background 0.12s, opacity 0.12s;
    opacity: 0.5;
  }
  .model-cb-label:hover { color: var(--text); background: rgba(255,255,255,0.05); }
  .model-cb-label:hover .dot { opacity: 0.8; }
  .model-cb-label.checked { color: var(--text); }
  .model-cb-label.checked .dot { opacity: 1; }
  .model-cb-label input { display: none; }
  .filter-btn {
    padding: 3px 9px; border-radius: 4px;
    border: none;
    background: transparent; color: var(--muted);
    font-size: 11px; cursor: pointer; white-space: nowrap;
    transition: background 0.12s, color 0.12s;
  }
  .filter-btn:hover { background: rgba(255,255,255,0.06); color: var(--text); }
  .update-btn {
    padding: 4px 14px; border-radius: 5px;
    border: 1px solid rgba(217,119,87,0.5);
    background: rgba(217,119,87,0.08);
    color: var(--accent); font-size: 11px; font-weight: 500;
    cursor: pointer; white-space: nowrap;
    transition: background 0.15s, border-color 0.15s;
  }
  .update-btn:hover { background: rgba(217,119,87,0.16); border-color: var(--accent); }
  .update-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .auto-update-label {
    display: flex; align-items: center; gap: 5px;
    font-size: 11px; color: var(--muted);
    cursor: pointer; user-select: none; white-space: nowrap;
  }
  .auto-update-label input { accent-color: var(--accent); cursor: pointer; }
  .range-group {
    display: flex;
    background: rgba(255,255,255,0.04);
    border-radius: 5px; overflow: hidden; flex-shrink: 0;
  }
  .range-btn {
    padding: 3px 12px; background: transparent;
    border: none; border-right: 1px solid var(--border);
    color: var(--muted); font-size: 11px; cursor: pointer;
    transition: background 0.15s, color 0.15s;
  }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .range-btn.active { background: rgba(217,119,87,0.12); color: var(--accent); font-weight: 600; }
  .date-inputs { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .date-inputs input[type="date"] {
    background: rgba(255,255,255,0.05); border: none;
    border-radius: 4px; color: var(--text); font-size: 11px;
    padding: 3px 8px; outline: none;
  }
  .date-inputs input[type="date"]:focus { box-shadow: 0 0 0 1px var(--accent); }
  .date-inputs span { color: var(--muted); font-size: 11px; }

  .container { max-width: 1440px; margin: 0 auto; padding: 18px 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 10px; margin-bottom: 18px; }
  .stat-card {
    background: var(--card);
    border-radius: 7px;
    padding: 13px 15px;
  }
  .stat-card .label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 7px; font-weight: 500; }
  .stat-card .value { font-size: 20px; font-weight: 700; font-family: 'JetBrains Mono', monospace; letter-spacing: -0.02em; }
  .stat-card .sub { color: var(--muted); font-size: 10px; margin-top: 5px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 18px; }
  .chart-card { background: var(--card); border-radius: 7px; padding: 16px 18px; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 10px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 14px; }
  .chart-wrap { position: relative; height: 220px; }
  .chart-wrap.tall { height: 270px; }

  table { width: 100%; border-collapse: collapse; }
  thead tr { background: var(--card); }
  th {
    text-align: left; padding: 7px 12px;
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em;
    color: var(--muted); border-bottom: 1px solid var(--border-muted);
    font-weight: 500; white-space: nowrap;
  }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.018); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 500; }
  .cost { color: var(--green); font-family: 'JetBrains Mono', monospace; }
  .cost-sub { color: var(--muted); font-family: 'JetBrains Mono', monospace; font-size: 11px; }
  .cost-na { color: var(--muted); font-family: 'JetBrains Mono', monospace; font-size: 11px; }
  .num { font-family: 'JetBrains Mono', monospace; }
  .muted { color: var(--muted); }
  .section-title {
    font-size: 10px; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.08em;
    margin-bottom: 14px;
    border-left: 2px solid var(--accent); padding-left: 8px;
  }
  .table-card {
    background: var(--card);
    border-radius: 7px; padding: 16px 18px;
    margin-bottom: 18px; overflow-x: auto;
  }

  #top-bar { position: sticky; top: 0; z-index: 100; }

  footer { border-top: 1px solid var(--border); padding: 14px 24px; margin-top: 4px; }
  .footer-content { max-width: 1440px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 11px; line-height: 1.7; margin-bottom: 3px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>
<div id="top-bar">
<header>
  <h1>Claude Code Usage Dashboard</h1>
  <div class="header-right">
    <div class="meta" id="meta">Loading...</div>
    <button class="update-btn" id="update-btn" onclick="triggerUpdate()">Update</button>
    <label class="auto-update-label">
      <input type="checkbox" id="auto-update-cb" onchange="toggleAutoUpdate(this.checked)">
      Auto-update
    </label>
  </div>
</header>

<div id="filter-bar">
  <div class="filter-label">Models</div>
  <div id="model-checkboxes"></div>
  <details id="other-models-details" style="display:none">
    <summary>Other (<span id="other-models-count">0</span>)</summary>
    <div id="other-models-wrap"></div>
  </details>
  <button class="filter-btn" onclick="selectAllModels()">All</button>
  <button class="filter-btn" onclick="clearAllModels()">None</button>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-group">
    <button class="range-btn" data-range="1d"  onclick="setRange('1d')">1d</button>
    <button class="range-btn" data-range="7d"  onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d" onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d" onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all" onclick="setRange('all')">All</button>
  </div>
  <div class="date-inputs">
    <input type="date" id="date-from" onchange="onCustomDateChange()">
    <span>&ndash;</span>
    <input type="date" id="date-to" onchange="onCustomDateChange()">
  </div>
</div>
</div>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>
  <div class="table-card">
    <div class="section-title">Recent Sessions</div>
    <table>
      <thead><tr>
        <th>Session</th><th>Project</th><th>Last Active</th><th>Duration</th>
        <th>Model</th><th>Turns</th><th>Total In</th><th>Input</th><th>Output</th><th>Cache Read</th><th>Cache Write</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-title">Cost by Model</div>
    <table>
      <thead><tr>
        <th>Model</th><th>Turns</th><th>Total In</th><th>Input</th><th>Output</th>
        <th>Cache Read</th><th>Cache Creation</th>
        <th>Cost/Turn</th><th>Eff. $/1M In</th><th>Eff. $/1M Out</th>
        <th>Cache Hit</th><th>Cache Savings</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of April 2026. Only models containing <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/ipogosov/claude-usage" target="_blank">github.com/ipogosov/claude-usage</a>
      &nbsp;&middot;&nbsp;
      Based on <a href="https://github.com/phuryn/claude-usage" target="_blank">phuryn/claude-usage</a>      &nbsp;&middot;&nbsp;
      License: MIT
    </p>
  </div>
</footer>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let selectedRange = '30d';
let charts = {};

// ── Model classification (no pricing — costs are computed server-side) ────────
function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toFixed(4); }
function fmtCostBig(c) { return '$' + c.toFixed(2); }
function fmtGap(min) {
  if (min == null) return '\u2014';
  if (min < 1) return Math.round(min * 60) + 's';
  if (min < 60) return Math.round(min) + 'm';
  const h = Math.floor(min / 60);
  const m = Math.round(min - h * 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

// ── Chart colors ───────────────────────────────────────────────────────────
const TOKEN_COLORS = {
  input:          'rgba(96,165,250,0.85)',
  output:         'rgba(251,146,60,0.85)',
  cache_read:     'rgba(74,222,128,0.75)',
  cache_creation: 'rgba(252,211,77,0.75)',
};
const MODEL_COLORS = ['#d97757','#60a5fa','#4ade80','#fcd34d','#fb923c','#34d399','#38bdf8','#a3e635'];

// ── Model tag styles by family ─────────────────────────────────────────────
function getModelTagStyle(model) {
  const m = (model || '').toLowerCase();
  if (m.includes('opus'))   return 'background:rgba(217,119,87,0.15);color:#d97757;';
  if (m.includes('sonnet')) return 'background:rgba(96,165,250,0.15);color:#60a5fa;';
  if (m.includes('haiku'))  return 'background:rgba(74,222,128,0.12);color:#4ade80;';
  return 'background:rgba(125,111,99,0.15);color:#7d6f63;';
}

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { '1d': 'Today', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time', 'custom': 'Custom Range' };
const RANGE_TICKS  = { '1d': 1, '7d': 7, '30d': 15, '90d': 13, 'all': 12, 'custom': 15 };

// ── Timezone helpers ────────────────────────────────────────────────────────
// Returns YYYY-MM-DD string in browser local time
function getLocalDate(date) {
  return date.toLocaleDateString('sv'); // 'sv' locale gives ISO format YYYY-MM-DD
}

// Offset to pass to backend: minutes east of UTC (opposite of JS getTimezoneOffset)
function getTzOffset() {
  return -new Date().getTimezoneOffset(); // JS returns minutes WEST, we want EAST
}

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

function readURLRange() {
  const params = new URLSearchParams(window.location.search);
  const fromDate = params.get('from');
  const toDate = params.get('to');
  if (fromDate || toDate) {
    if (fromDate) document.getElementById('date-from').value = fromDate;
    if (toDate) document.getElementById('date-to').value = toDate;
    return 'custom';
  }
  const p = params.get('range');
  return ['1d', '7d', '30d', '90d', 'all'].includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  if (range === 'all') {
    document.getElementById('date-from').value = '';
    document.getElementById('date-to').value = '';
  } else if (range !== 'custom') {
    const cutoff = getRangeCutoff(range);
    document.getElementById('date-from').value = cutoff.from || '';
    document.getElementById('date-to').value = getLocalDate(new Date());
  }
  updateURL();
  applyFilter();
}

function onCustomDateChange() {
  selectedRange = 'custom';
  document.querySelectorAll('.range-btn').forEach(btn => btn.classList.remove('active'));
  updateURL();
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('opus'))   return 0;
  if (ml.includes('sonnet')) return 1;
  if (ml.includes('haiku'))  return 2;
  return 3;
}

// allModels here is [{model, billable}] from server
function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) return new Set(allModels.filter(r => r.billable).map(r => r.model));
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.map(r => r.model).filter(m => fromURL.has(m)));
}

// allModels here is string[] from DOM (cb.value)
function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  if (selectedModels.size !== billable.length) return false;
  return billable.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  // allModels is [{model, billable}] from rawData
  const sorted = [...allModels].sort((a, b) => {
    const pa = modelPriority(a.model), pb = modelPriority(b.model);
    return pa !== pb ? pa - pb : a.model.localeCompare(b.model);
  });
  selectedModels = readURLModels(allModels);
  const mainModels  = sorted.filter(r =>  r.billable).map(r => r.model);
  const otherModels = sorted.filter(r => !r.billable).map(r => r.model);

  const dotColor = m => {
    const ml = m.toLowerCase();
    if (ml.includes('opus'))   return '#d97757';
    if (ml.includes('sonnet')) return '#60a5fa';
    if (ml.includes('haiku'))  return '#4ade80';
    return '#7d6f63';
  };
  const makeCb = m => {
    const checked = selectedModels.has(m);
    const dc = dotColor(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${m}">
      <input type="checkbox" value="${m}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      <span class="dot" style="background:${dc}"></span>${m}
    </label>`;
  };

  document.getElementById('model-checkboxes').innerHTML = mainModels.map(makeCb).join('');

  const otherDetails = document.getElementById('other-models-details');
  if (otherModels.length > 0) {
    otherDetails.style.display = '';
    document.getElementById('other-models-count').textContent = otherModels.length;
    document.getElementById('other-models-wrap').innerHTML = otherModels.map(makeCb).join('');
  } else {
    otherDetails.style.display = 'none';
  }
}

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input, #other-models-wrap input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input, #other-models-wrap input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input, #other-models-wrap input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange === 'custom') {
    const f = document.getElementById('date-from').value;
    const t = document.getElementById('date-to').value;
    if (f) params.set('from', f);
    if (t) params.set('to', t);
  } else if (selectedRange !== '30d') {
    params.set('range', selectedRange);
  }
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const range = getRangeCutoff(selectedRange);

  function inRange(day) {
    if (range.from && day < range.from) return false;
    if (range.to && day > range.to) return false;
    return true;
  }

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && inRange(r.day)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns + pre-computed costs from daily data
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = {
      model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0,
      turns: 0, sessions: 0,
      cost: 0, input_cost: 0, output_cost: 0,
      cache_read_cost: 0, cache_creation_cost: 0, cache_savings: 0,
      billable: r.billable,
    };
    const m = modelMap[r.model];
    m.input               += r.input;
    m.output              += r.output;
    m.cache_read          += r.cache_read;
    m.cache_creation      += r.cache_creation;
    m.turns               += r.turns;
    m.cost                += r.cost;
    m.input_cost          += r.input_cost;
    m.output_cost         += r.output_cost;
    m.cache_read_cost     += r.cache_read_cost;
    m.cache_creation_cost += r.cache_creation_cost;
    m.cache_savings       += r.cache_savings;
  }

  // Aggregate session_model_daily by session, filtered by model + date range
  const smdBySession = {};
  for (const r of rawData.session_model_daily) {
    if (!selectedModels.has(r.model) || !inRange(r.day)) continue;
    if (!smdBySession[r.session_id]) {
      smdBySession[r.session_id] = { input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, cost: 0, byModel: {} };
    }
    const s = smdBySession[r.session_id];
    s.input          += r.input;
    s.output         += r.output;
    s.cache_read     += r.cache_read;
    s.cache_creation += r.cache_creation;
    s.turns          += r.turns;
    s.cost           += r.cost;
    if (!s.byModel[r.model]) s.byModel[r.model] = { input: 0, output: 0, cache_read: 0, cache_creation: 0, cost: 0 };
    const m = s.byModel[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.cost           += r.cost;
  }

  // Join with sessions_all metadata; cost already accumulated from enriched smd rows
  const filteredSessions = rawData.sessions_all
    .filter(s => smdBySession[s.session_id])
    .map(s => {
      const agg = smdBySession[s.session_id];
      return {
        ...s,
        input:          agg.input,
        output:         agg.output,
        cache_read:     agg.cache_read,
        cache_creation: agg.cache_creation,
        turns:          agg.turns,
        models:         Object.keys(agg.byModel),
        cost:           agg.cost,
      };
    });

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    for (const model of s.models) {
      if (modelMap[model]) modelMap[model].sessions++;
    }
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, turns: 0 };
    projMap[s.project].input  += s.input;
    projMap[s.project].output += s.output;
    projMap[s.project].turns  += s.turns;
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // Totals
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + m.cost, 0),
  };

  // Cache + compact event aggregates over the current date range + model filter.
  // Events without a model (older rows before the schema migration) pass the
  // model filter so they don't silently disappear.
  const passesModel = e => !e.model || selectedModels.has(e.model);
  const filteredCacheEv = (rawData.cache_events   || []).filter(e => inRange(e.day) && passesModel(e));
  const filteredCompEv  = (rawData.compact_events || []).filter(e => inRange(e.day) && passesModel(e));
  const gaps = filteredCacheEv.map(e => e.gap_min);
  const cacheStats = {
    count:     gaps.length,
    mutation:  gaps.filter(g => g < 5).length,
    save_1h:   gaps.filter(g => g >= 5 && g < 60).length,
    exhausted: gaps.filter(g => g >= 60).length,
  };
  const compactCounts = {
    manual: filteredCompEv.filter(e => e.trigger === 'manual').length,
    auto:   filteredCompEv.filter(e => e.trigger === 'auto').length,
  };
  totals.cache_stats     = cacheStats;
  totals.compact_counts  = compactCounts;

  // Update daily chart title
  let rangeTitle = RANGE_LABELS[selectedRange];
  if (selectedRange === 'custom') {
    const f = document.getElementById('date-from').value;
    const t = document.getElementById('date-to').value;
    rangeTitle = (f || '...') + ' \u2014 ' + (t || '...');
  }
  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + rangeTitle;

  renderStats(totals);
  renderDailyChart(daily);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderSessionsTable(filteredSessions.slice(0, 20));
  renderModelCostTable(byModel);
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = selectedRange === 'custom' ? 'custom range' : RANGE_LABELS[selectedRange].toLowerCase();
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',   value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'API pricing, Apr 2026', color: '#4ade80' },
  ];
  const cs  = t.cache_stats    || { count: 0, mutation: 0, save_1h: 0, exhausted: 0 };
  const cct = t.compact_counts || { manual: 0, auto: 0 };
  stats.push({ label: 'Cache Evict.',     value: cs.count,     sub: 'in range' });
  stats.push({ label: 'Prefix Mutations', value: cs.mutation,  sub: 'cache killed by context change' });
  stats.push({ label: '1h Would Save',    value: cs.save_1h,   sub: '5m too short, 1h alive', color: cs.save_1h > 0 ? '#fcd34d' : undefined });
  stats.push({ label: 'TTL Exhausted',    value: cs.exhausted, sub: 'both tiers dead' });
  stats.push({ label: '/compact',         value: cct.manual,   sub: 'manual' });
  stats.push({ label: 'Auto-compact',     value: cct.auto,     sub: 'context-full', color: cct.auto > 0 ? '#f87171' : undefined });
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${s.value}</div>
      ${s.sub ? `<div class="sub">${s.sub}</div>` : ''}
    </div>
  `).join('');
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens' },
        { label: 'Output',         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens' },
        { label: 'Cache Read',     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#7d6f63', boxWidth: 10, font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: '#7d6f63', maxTicksLimit: RANGE_TICKS[selectedRange], font: { size: 11 } }, grid: { color: '#2e2824' } },
        y: { ticks: { color: '#7d6f63', callback: v => fmt(v), font: { size: 11 } }, grid: { color: '#2e2824' } },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, borderWidth: 2, borderColor: '#211d18' }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#7d6f63', boxWidth: 10, font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project),
      datasets: [
        { label: 'Input',  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input },
        { label: 'Output', data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#7d6f63', boxWidth: 10, font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: '#7d6f63', callback: v => fmt(v), font: { size: 11 } }, grid: { color: '#2e2824' } },
        y: { ticks: { color: '#7d6f63', font: { size: 11 } }, grid: { color: '#2e2824' } },
      }
    }
  });
}

function renderSessionsTable(sessions) {
  document.getElementById('sessions-body').innerHTML = sessions.map(s => {
    const hasBillable = s.cost > 0;
    const costCell = hasBillable
      ? `<td class="cost">${fmtCost(s.cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const modelTags = s.models
      .map(m => `<span class="model-tag" style="${getModelTagStyle(m)}">${m}</span>`)
      .join(' ');
    return `<tr>
      <td class="muted" style="font-family:monospace"><a href="/session/${s.session_id}" style="color:var(--accent);text-decoration:none" onmouseover="this.style.textDecoration='underline'" onmouseout="this.style.textDecoration='none'">${s.session_id}&hellip;</a></td>
      <td>${s.project}</td>
      <td class="muted">${s.last}</td>
      <td class="muted">${s.duration_min}m</td>
      <td>${modelTags}</td>
      <td class="num">${fmt(s.turns)}</td>
      <td class="num">${fmt(s.input + s.cache_read + s.cache_creation)}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      <td class="num muted">${fmt(s.cache_read)}</td>
      <td class="num muted">${fmt(s.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

function renderModelCostTable(byModel) {
  document.getElementById('model-cost-body').innerHTML = byModel.map(m => {
    const billable = m.billable;
    const na = '<td class="cost-na">n/a</td>';

    const totalInputSide = m.input + m.cache_read + m.cache_creation;
    const totalInputCost = m.input_cost + m.cache_read_cost + m.cache_creation_cost;

    const costSub = (v) => billable
      ? `<br><span class="cost-sub">(${fmtCost(v)})</span>`
      : '';

    const costCell    = billable ? `<td class="cost">${fmtCost(m.cost)}</td>` : na;
    const costPerTurn = (billable && m.turns > 0)
      ? `<td class="cost">${fmtCost(m.cost / m.turns)}</td>` : na;

    // Effective $/1M input-side = total input-side spend / total input-side tokens
    let effInCell = na;
    if (billable && totalInputSide > 0) {
      const effIn = totalInputCost / totalInputSide * 1e6;
      effInCell = `<td class="cost">$${effIn.toFixed(2)}</td>`;
    }

    // Effective $/1M output = output_cost / output_tokens (equals model output price)
    let effOutCell = na;
    if (billable && m.output > 0) {
      const effOut = m.output_cost / m.output * 1e6;
      effOutCell = `<td class="cost">$${effOut.toFixed(2)}</td>`;
    }

    const cacheHitCell = totalInputSide > 0
      ? `<td class="num">${(m.cache_read / totalInputSide * 100).toFixed(1)}%</td>`
      : na;

    const cacheSavingsCell = (billable && m.cache_read > 0)
      ? `<td class="cost">${fmtCost(m.cache_savings)}</td>`
      : na;

    return `<tr>
      <td><span class="model-tag" style="${getModelTagStyle(m.model)}">${m.model}</span></td>
      <td class="num">${fmt(m.turns)}${costSub(m.cost)}</td>
      <td class="num">${fmt(totalInputSide)}${costSub(totalInputCost)}</td>
      <td class="num">${fmt(m.input)}${costSub(m.input_cost)}</td>
      <td class="num">${fmt(m.output)}${costSub(m.output_cost)}</td>
      <td class="num">${fmt(m.cache_read)}${costSub(m.cache_read_cost)}</td>
      <td class="num">${fmt(m.cache_creation)}${costSub(m.cache_creation_cost)}</td>
      ${costPerTurn}${effInCell}${effOutCell}
      ${cacheHitCell}${cacheSavingsCell}${costCell}
    </tr>`;
  }).join('');
}

// ── Data loading ───────────────────────────────────────────────────────────
let autoUpdateTimer = null;

async function loadData() {
  try {
    const resp = await fetch('/api/data?tz=' + getTzOffset());
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:40px;color:#f87171">' + d.error + '</div>';
      return;
    }
    document.getElementById('meta').textContent = 'Updated: ' + d.generated_at;

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      buildFilterUI(d.all_models);
    }

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

async function triggerUpdate() {
  const btn = document.getElementById('update-btn');
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  try {
    await fetch('/api/scan', { method: 'POST' });
    await loadData();
  } catch(e) {
    console.error(e);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Update';
  }
}

function toggleAutoUpdate(enabled) {
  if (autoUpdateTimer) { clearInterval(autoUpdateTimer); autoUpdateTimer = null; }
  if (enabled) autoUpdateTimer = setInterval(triggerUpdate, 30000);
}

loadData();
</script>
</body>
</html>
"""

SESSION_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Session Inspector</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #100e0d;
    --card: #211d18;
    --card-hover: #2a2520;
    --border: #2e2824;
    --border-muted: #3a322d;
    --text: #ece5de;
    --muted: #7d6f63;
    --accent: #d97757;
    --blue: #60a5fa;
    --green: #4ade80;
    --yellow: #fcd34d;
    --red: #f87171;
    --user-border: #3a322d;
    --assistant-border: #d97757;
    --thinking-bg: rgba(96,165,250,0.06);
    --tool-bg: rgba(74,222,128,0.06);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 14px;
    line-height: 1.6;
  }

  header {
    background: var(--card);
    border-bottom: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky; top: 0; z-index: 100;
  }
  header h1 { font-size: 14px; font-weight: 600; color: var(--accent); letter-spacing: -0.01em; }
  .back-link {
    color: var(--muted); font-size: 12px; text-decoration: none;
    transition: color 0.15s;
  }
  .back-link:hover { color: var(--text); }
  .header-meta { color: var(--muted); font-size: 11px; font-family: 'JetBrains Mono', monospace; }

  .container { max-width: 960px; margin: 0 auto; padding: 18px 24px; }

  /* Summary cards */
  .summary-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border-radius: 7px; padding: 13px 15px; }
  .stat-card .label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 5px; font-weight: 500; }
  .stat-card .value { font-size: 18px; font-weight: 700; font-family: 'JetBrains Mono', monospace; letter-spacing: -0.02em; }
  .stat-card .sub { color: var(--muted); font-size: 10px; margin-top: 3px; }

  /* Turn cards */
  .turn { margin-bottom: 12px; border-radius: 7px; overflow: hidden; }
  .turn-header {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 14px;
    font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.06em;
  }
  .turn-body { padding: 12px 16px; }

  .turn--user { border-left: 3px solid var(--user-border); background: var(--card); }
  .turn--user .turn-header { color: var(--muted); background: rgba(255,255,255,0.02); }

  .turn--assistant { border-left: 3px solid var(--assistant-border); background: var(--card); }
  .turn--assistant .turn-header { color: var(--accent); background: rgba(217,119,87,0.06); }

  .turn-time { margin-left: auto; font-weight: 400; color: var(--muted); font-family: 'JetBrains Mono', monospace; text-transform: none; }
  .turn-cost { color: var(--green); font-family: 'JetBrains Mono', monospace; font-weight: 500; text-transform: none; }
  .turn-tokens { color: var(--muted); font-family: 'JetBrains Mono', monospace; font-weight: 400; font-size: 10px; text-transform: none; }
  .model-tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 500; text-transform: none; letter-spacing: 0; }

  /* Content blocks */
  .content-block { margin-bottom: 10px; }
  .content-block:last-child { margin-bottom: 0; }

  .text-content {
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 13px;
    line-height: 1.7;
  }

  /* Thinking blocks */
  .thinking-block {
    background: var(--thinking-bg);
    border: 1px solid rgba(96,165,250,0.15);
    border-radius: 5px;
    overflow: hidden;
  }
  .thinking-block summary {
    padding: 6px 12px;
    font-size: 11px;
    color: var(--blue);
    cursor: pointer;
    user-select: none;
    font-weight: 500;
  }
  .thinking-block summary:hover { background: rgba(96,165,250,0.08); }
  .thinking-content {
    padding: 10px 14px;
    font-size: 12px;
    color: var(--muted);
    font-style: italic;
    white-space: pre-wrap;
    word-break: break-word;
    border-top: 1px solid rgba(96,165,250,0.1);
    max-height: 400px;
    overflow-y: auto;
  }

  /* Tool blocks */
  .tool-block {
    background: var(--tool-bg);
    border: 1px solid rgba(74,222,128,0.15);
    border-radius: 5px;
    overflow: hidden;
  }
  .tool-block summary {
    padding: 6px 12px;
    font-size: 11px;
    color: var(--green);
    cursor: pointer;
    user-select: none;
    font-weight: 500;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .tool-block summary:hover { background: rgba(74,222,128,0.08); }
  .tool-name { font-family: 'JetBrains Mono', monospace; }
  .tool-content {
    padding: 10px 14px;
    font-size: 12px;
    font-family: 'JetBrains Mono', monospace;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
    border-top: 1px solid rgba(74,222,128,0.1);
    max-height: 500px;
    overflow-y: auto;
  }

  /* Tool results */
  .tool-result-block {
    background: rgba(252,211,77,0.04);
    border: 1px solid rgba(252,211,77,0.12);
    border-radius: 5px;
    overflow: hidden;
  }
  .tool-result-block summary {
    padding: 6px 12px;
    font-size: 11px;
    color: var(--yellow);
    cursor: pointer;
    user-select: none;
    font-weight: 500;
  }
  .tool-result-block summary:hover { background: rgba(252,211,77,0.06); }
  .tool-result-content {
    padding: 10px 14px;
    font-size: 12px;
    font-family: 'JetBrains Mono', monospace;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
    border-top: 1px solid rgba(252,211,77,0.08);
    max-height: 500px;
    overflow-y: auto;
  }

  .loading { text-align: center; padding: 60px; color: var(--muted); }
  .error { text-align: center; padding: 60px; color: var(--red); }

  /* Cost breakdown bar */
  .cost-bar {
    display: flex; gap: 8px; flex-wrap: wrap;
    padding: 6px 0 2px;
    font-size: 10px; font-family: 'JetBrains Mono', monospace;
  }
  .cost-item {
    display: inline-flex; align-items: center; gap: 3px;
    padding: 2px 6px; border-radius: 3px;
    background: rgba(255,255,255,0.03);
  }
  .cost-item .ci-label { color: var(--muted); }
  .cost-item .ci-val { color: var(--text); }
  .cost-item .ci-pct { color: var(--muted); font-size: 9px; }
  .ci-input { border-left: 2px solid rgba(96,165,250,0.6); }
  .ci-output { border-left: 2px solid rgba(251,146,60,0.6); }
  .ci-cache-read { border-left: 2px solid rgba(74,222,128,0.6); }
  .ci-cache-write { border-left: 2px solid rgba(252,211,77,0.6); }

  /* Collapsed text blocks */
  .text-block { border-radius: 5px; overflow: hidden; }
  .text-block summary {
    padding: 6px 12px; font-size: 11px; color: var(--text);
    cursor: pointer; user-select: none;
  }
  .text-block summary:hover { background: rgba(255,255,255,0.03); }
  .text-block .text-content {
    padding: 10px 14px;
    border-top: 1px solid var(--border);
  }

  /* Summary cost breakdown */
  .cost-summary-card { background: var(--card); border-radius: 7px; padding: 13px 15px; grid-column: 1 / -1; }
  .cost-summary-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  .cost-col .cost-col-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 4px; font-weight: 500; }
  .cost-col .cost-col-value { font-size: 16px; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
  .cost-col .cost-col-pct { color: var(--muted); font-size: 11px; font-family: 'JetBrains Mono', monospace; }

  /* Eviction badge — shown inline in assistant turn header */
  .eviction-badge {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 1px 6px; border-radius: 3px;
    font-size: 10px; font-weight: 500;
    font-family: 'JetBrains Mono', monospace;
    text-transform: none; letter-spacing: 0;
    background: rgba(252,211,77,0.12);
    color: var(--yellow);
    cursor: help;
  }
  .eviction-badge.ev-mutation {
    background: rgba(125,111,99,0.15);
    color: var(--muted);
  }

  /* Compact-boundary separator */
  .turn--compact {
    display: flex; align-items: center; gap: 12px;
    padding: 4px 0; margin: 12px 0;
    color: var(--muted);
    font-size: 10px; font-weight: 600;
    font-family: 'JetBrains Mono', monospace;
    text-transform: uppercase; letter-spacing: 0.08em;
  }
  .turn--compact::before, .turn--compact::after {
    content: ''; flex: 1; height: 1px; background: var(--border);
  }
  .turn--compact.compact-auto { color: var(--red); }
  .turn--compact.compact-auto::before,
  .turn--compact.compact-auto::after {
    background: rgba(248,113,113,0.25);
  }

  /* Filter toolbar above turn list */
  .filter-toolbar {
    display: flex; align-items: center; gap: 10px;
    padding: 4px 0 14px;
    font-size: 11px;
  }
  .filter-toolbar label {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 4px;
    color: var(--muted); cursor: pointer; user-select: none;
    background: rgba(255,255,255,0.03);
    transition: color 0.12s, background 0.12s;
  }
  .filter-toolbar label:hover { color: var(--text); background: rgba(255,255,255,0.06); }
  .filter-toolbar label.active {
    color: var(--accent);
    background: rgba(217,119,87,0.1);
  }
  .filter-toolbar input[type="checkbox"] { accent-color: var(--accent); cursor: pointer; }
  .filter-toolbar .count {
    color: var(--muted);
    font-family: 'JetBrains Mono', monospace;
    margin-left: auto;
  }
</style>
</head>
<body>
<header>
  <div>
    <a href="/" class="back-link">&larr; Dashboard</a>
    <h1 id="page-title">Session Inspector</h1>
  </div>
  <div class="header-meta" id="header-meta">Loading...</div>
</header>
<div class="container">
  <div class="summary-row" id="summary-row"></div>
  <div class="filter-toolbar" id="filter-toolbar" style="display:none">
    <label id="filter-evictions-label">
      <input type="checkbox" id="cb-evictions-only" onchange="applyInspectorFilter()">
      Show only cache expirations
    </label>
    <span class="count" id="filter-count"></span>
  </div>
  <div id="turns-container"><div class="loading">Loading session data...</div></div>
</div>

<script>
const SESSION_ID = window.location.pathname.split('/session/')[1] || '';
let sessionData = null;

function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c) { return '$' + c.toFixed(4); }
function fmtCostBig(c) { return '$' + c.toFixed(2); }
function fmtGap(min) {
  if (min == null) return '\u2014';
  if (min < 1) return Math.round(min * 60) + 's';
  if (min < 60) return Math.round(min) + 'm';
  const h = Math.floor(min / 60);
  const m = Math.round(min - h * 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function getModelTagStyle(model) {
  const m = (model || '').toLowerCase();
  if (m.includes('opus'))   return 'background:rgba(217,119,87,0.15);color:#d97757;';
  if (m.includes('sonnet')) return 'background:rgba(96,165,250,0.15);color:#60a5fa;';
  if (m.includes('haiku'))  return 'background:rgba(74,222,128,0.12);color:#4ade80;';
  return 'background:rgba(125,111,99,0.15);color:#7d6f63;';
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

function formatTime(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  } catch { return ''; }
}

function renderBlock(block) {
  if (block.type === 'text') {
    const preview = block.text.length > 120 ? block.text.slice(0, 120).replace(/\n/g, ' ') + '\u2026' : block.text.replace(/\n/g, ' ');
    return `<div class="content-block"><details class="text-block">
      <summary>${escapeHtml(preview)}</summary>
      <div class="text-content">${escapeHtml(block.text)}</div>
    </details></div>`;
  }
  if (block.type === 'thinking') {
    return `<div class="content-block"><details class="thinking-block">
      <summary>\u{1f9e0} Thinking (${fmt(block.text.length)} chars)</summary>
      <div class="thinking-content">${escapeHtml(block.text)}</div>
    </details></div>`;
  }
  if (block.type === 'tool_use') {
    const inputStr = typeof block.input === 'object' ? JSON.stringify(block.input, null, 2) : String(block.input || '');
    return `<div class="content-block"><details class="tool-block">
      <summary>\u{1f527} <span class="tool-name">${escapeHtml(block.name)}</span></summary>
      <div class="tool-content">${escapeHtml(inputStr)}</div>
    </details></div>`;
  }
  if (block.type === 'tool_result') {
    const content = typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);
    const preview = content.length > 80 ? content.slice(0, 80) + '...' : content;
    return `<div class="content-block"><details class="tool-result-block">
      <summary>\u{1f4e4} Tool Result (${fmt(content.length)} chars)</summary>
      <div class="tool-result-content">${escapeHtml(content)}</div>
    </details></div>`;
  }
  return '';
}

function renderCostBar(bd, total) {
  if (!bd || total <= 0) return '';
  const items = [
    { label: 'Input', val: bd.input, cls: 'ci-input' },
    { label: 'Output', val: bd.output, cls: 'ci-output' },
    { label: 'Cache Read', val: bd.cache_read, cls: 'ci-cache-read' },
    { label: 'Cache Write', val: bd.cache_creation, cls: 'ci-cache-write' },
  ];
  return `<div class="cost-bar">${items.map(i => {
    const pct = total > 0 ? (i.val / total * 100) : 0;
    return `<span class="cost-item ${i.cls}"><span class="ci-label">${i.label}</span> <span class="ci-val">${fmtCost(i.val)}</span> <span class="ci-pct">${pct.toFixed(0)}%</span></span>`;
  }).join('')}</div>`;
}

function renderTurn(turn, index) {
  if (turn.type === 'compact') {
    const cls = turn.trigger === 'auto' ? 'compact-auto' : 'compact-manual';
    const label = turn.trigger === 'auto' ? 'Auto-compacted' : 'Compacted';
    const pre = turn.pre_tokens ? ` \u00b7 ${fmt(turn.pre_tokens)} tok` : '';
    return `<div class="turn--compact ${cls}">${label}${pre}</div>`;
  }
  if (turn.type === 'user') {
    return `<div class="turn turn--user">
      <div class="turn-header">
        <span>User</span>
        <span class="turn-time">${formatTime(turn.timestamp)}</span>
      </div>
      <div class="turn-body">${turn.content.map(renderBlock).join('')}</div>
    </div>`;
  }
  if (turn.type === 'assistant') {
    const u = turn.usage || {};
    const inTok = u.input_tokens || 0;
    const cacheTok = (u.cache_read || 0) + (u.cache_creation || 0);
    const tokenInfo = `in:${fmt(inTok)} cache:${fmt(cacheTok)} out:${fmt(u.output_tokens||0)}`;
    const costStr = turn.cost > 0 ? fmtCost(turn.cost) : '';
    const modelTag = turn.model
      ? `<span class="model-tag" style="${getModelTagStyle(turn.model)}">${turn.model}</span>`
      : '';
    const ev = turn.eviction;
    const evBadge = ev
      ? `<span class="eviction-badge ${ev.category.startsWith('ttl') ? '' : 'ev-mutation'}" title="Cache rewritten after ${fmtGap(ev.gap_minutes)} \u2014 ${ev.category}">\u27f3 ${fmtGap(ev.gap_minutes)}</span>`
      : '';
    const costBar = renderCostBar(turn.cost_breakdown, turn.cost);
    return `<div class="turn turn--assistant">
      <div class="turn-header">
        <span>Assistant</span>
        ${modelTag}
        ${evBadge}
        <span class="turn-tokens">${tokenInfo}</span>
        ${costStr ? `<span class="turn-cost">${costStr}</span>` : ''}
        <span class="turn-time">${formatTime(turn.timestamp)}</span>
      </div>
      ${costBar}
      <div class="turn-body">${turn.content.map(renderBlock).join('')}</div>
    </div>`;
  }
  return '';
}

async function loadSession() {
  try {
    const resp = await fetch(`/api/session/${SESSION_ID}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    if (data.error) {
      document.getElementById('turns-container').innerHTML = `<div class="error">${data.error}</div>`;
      return;
    }

    // Update header
    document.getElementById('page-title').textContent = `Session ${data.session_id.slice(0, 8)}\u2026`;
    document.getElementById('header-meta').textContent = `${data.project} \u00b7 ${data.model} \u00b7 ${data.duration_min}min`;

    // Summary cards
    const stats = [
      { label: 'Turns', value: data.turn_count },
      { label: 'Duration', value: data.duration_min + 'm' },
      { label: 'Input', value: fmt(data.total_input) },
      { label: 'Output', value: fmt(data.total_output) },
      { label: 'Cache Read', value: fmt(data.total_cache_read) },
      { label: 'Cache Write', value: fmt(data.total_cache_creation) },
      { label: 'Est. Cost', value: fmtCostBig(data.total_cost), color: '#4ade80' },
    ];
    const cs = data.cache_stats || {};
    const cct = data.compact_counts || {};
    const hasCache = (cs.count || 0) > 0;
    const hasCompact = (cct.manual || 0) + (cct.auto || 0) > 0;
    if (hasCache) {
      stats.push({ label: 'Cache Evict.',     value: cs.count });
      stats.push({ label: 'Prefix Mut.',      value: cs.mutation  || 0 });
      stats.push({ label: '1h Would Save',    value: cs.save_1h   || 0, color: (cs.save_1h   || 0) > 0 ? '#fcd34d' : undefined });
      stats.push({ label: 'TTL Exhausted',    value: cs.exhausted || 0 });
    }
    if (hasCompact) {
      stats.push({ label: '/compact', value: cct.manual || 0 });
      stats.push({ label: 'Auto-compact', value: cct.auto || 0, color: (cct.auto || 0) > 0 ? '#f87171' : undefined });
    }
    let summaryHtml = stats.map(s =>
      `<div class="stat-card">
        <div class="label">${s.label}</div>
        <div class="value" style="${s.color ? 'color:' + s.color : ''}">${s.value}</div>
      </div>`
    ).join('');

    // Cost breakdown summary card
    const tb = data.total_cost_breakdown || {};
    const tc = data.total_cost || 0;
    if (tc > 0) {
      const cols = [
        { label: 'Input Cost', val: tb.input || 0, color: 'rgba(96,165,250,0.85)' },
        { label: 'Output Cost', val: tb.output || 0, color: 'rgba(251,146,60,0.85)' },
        { label: 'Cache Read Cost', val: tb.cache_read || 0, color: 'rgba(74,222,128,0.75)' },
        { label: 'Cache Write Cost', val: tb.cache_creation || 0, color: 'rgba(252,211,77,0.75)' },
      ];
      summaryHtml += `<div class="cost-summary-card">
        <div class="label" style="margin-bottom:10px">Cost Breakdown</div>
        <div class="cost-summary-grid">${cols.map(c => {
          const pct = tc > 0 ? (c.val / tc * 100) : 0;
          return `<div class="cost-col">
            <div class="cost-col-label">${c.label}</div>
            <div class="cost-col-value" style="color:${c.color}">$${c.val.toFixed(2)}</div>
            <div class="cost-col-pct">${pct.toFixed(1)}%</div>
          </div>`;
        }).join('')}</div>
      </div>`;
    }
    document.getElementById('summary-row').innerHTML = summaryHtml;

    sessionData = data;

    // Show filter toolbar if there's anything to filter on
    const evCount = data.turns.filter(t => t.type === 'assistant' && t.eviction).length;
    const cpCount = data.turns.filter(t => t.type === 'compact').length;
    const hasFilterable = evCount > 0 || cpCount > 0;
    document.getElementById('filter-toolbar').style.display = hasFilterable ? '' : 'none';
    document.getElementById('filter-count').textContent = hasFilterable
      ? `${evCount} eviction${evCount !== 1 ? 's' : ''} \u00b7 ${cpCount} compaction${cpCount !== 1 ? 's' : ''}`
      : '';

    renderTurns();

  } catch(e) {
    document.getElementById('turns-container').innerHTML = `<div class="error">Failed to load session: ${e.message}</div>`;
  }
}

function renderTurns() {
  if (!sessionData) return;
  const cb = document.getElementById('cb-evictions-only');
  const filterOn = cb && cb.checked;
  let turns = sessionData.turns;
  if (filterOn) {
    turns = turns.filter(t =>
      (t.type === 'assistant' && t.eviction) || t.type === 'compact'
    );
  }
  const container = document.getElementById('turns-container');
  if (turns.length === 0) {
    container.innerHTML = filterOn
      ? '<div class="loading">No cache expirations or compactions in this session.</div>'
      : '<div class="loading">No conversation data found for this session.</div>';
    return;
  }
  container.innerHTML = turns.map(renderTurn).join('');
}

function applyInspectorFilter() {
  const cb = document.getElementById('cb-evictions-only');
  document.getElementById('filter-evictions-label').classList.toggle('active', cb.checked);
  renderTurns();
}

loadSession();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif self.path.startswith("/api/data"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            try:
                tz_offset = int(qs.get('tz', ['0'])[0])
                tz_offset = max(-840, min(840, tz_offset))  # clamp to valid range
            except (ValueError, IndexError):
                tz_offset = 0
            data = get_dashboard_data(tz_offset=tz_offset)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path.startswith("/api/session/"):
            from scanner import get_session_transcript
            session_id = self.path.split("/api/session/")[1].split("?")[0]
            data = get_session_transcript(session_id)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path.startswith("/session/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(SESSION_HTML_TEMPLATE.encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/scan":
            from scanner import scan
            scan(verbose=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()


def serve(port=8087):
    server = HTTPServer(("localhost", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
