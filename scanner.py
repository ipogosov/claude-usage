"""
scanner.py - Scans Claude Code JSONL transcript files and stores data in SQLite.
"""

import json
import os
import glob
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

PROJECTS_DIR = Path.home() / ".claude" / "projects"
DB_PATH = Path.home() / ".claude" / "usage.db"


def get_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    # 1. Tables (CREATE IF NOT EXISTS). Old DBs have event tables without
    #    source_file — we add the column below before any index touches it.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            tool_name               TEXT,
            cwd                     TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );

        CREATE TABLE IF NOT EXISTS cache_events (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id          TEXT,
            timestamp           TEXT,
            gap_min             REAL,
            category            TEXT,
            rewritten_tokens    INTEGER,
            source_file         TEXT,
            model               TEXT
        );

        CREATE TABLE IF NOT EXISTS compact_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT,
            timestamp   TEXT,
            trigger     TEXT,
            pre_tokens  INTEGER,
            source_file TEXT,
            model       TEXT
        );
    """)
    # 2. Migrations: add columns added after the first shipped schema. Each try
    #    is a no-op if the column already exists.
    for tbl in ("cache_events", "compact_events"):
        for col in ("source_file TEXT", "model TEXT"):
            try:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
    # 3. Indexes (now safe — source_file exists everywhere).
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
        CREATE INDEX IF NOT EXISTS idx_cache_events_ts ON cache_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_cache_events_session ON cache_events(session_id);
        CREATE INDEX IF NOT EXISTS idx_cache_events_file ON cache_events(source_file);
        CREATE INDEX IF NOT EXISTS idx_compact_events_ts ON compact_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_compact_events_session ON compact_events(session_id);
        CREATE INDEX IF NOT EXISTS idx_compact_events_file ON compact_events(source_file);
    """)
    conn.commit()


def project_name_from_cwd(cwd):
    """Derive a friendly project name from cwd path."""
    if not cwd:
        return "unknown"
    # Normalize to forward slashes, take last 2 components
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def parse_jsonl_file(filepath, turns_offset=0):
    """Parse a JSONL file in a single pass.

    Collects session metadata from ALL lines (needed for accurate first/last timestamps).
    Collects turns only from lines >= turns_offset (0 = all lines, N = incremental update).
    Returns: (session_metas, turns, total_line_count)
    """
    turns = []
    session_meta = {}  # session_id -> dict
    line_count = 0

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for i, raw_line in enumerate(f):
                line_count += 1
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rtype = record.get("type")
                if rtype not in ("assistant", "user"):
                    continue

                session_id = record.get("sessionId")
                if not session_id:
                    continue

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch", "")

                # Update session metadata from ALL records
                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "session_id": session_id,
                        "project_name": project_name_from_cwd(cwd),
                        "first_timestamp": timestamp,
                        "last_timestamp": timestamp,
                        "git_branch": git_branch,
                        "model": None,
                    }
                else:
                    meta = session_meta[session_id]
                    if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                        meta["first_timestamp"] = timestamp
                    if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                        meta["last_timestamp"] = timestamp
                    if git_branch and not meta["git_branch"]:
                        meta["git_branch"] = git_branch

                # Collect turns only from new lines
                if rtype == "assistant" and i >= turns_offset:
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")

                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

                    if input_tokens + output_tokens + cache_read + cache_creation == 0:
                        continue

                    tool_name = None
                    for item in msg.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_name = item.get("name")
                            break

                    if model:
                        session_meta[session_id]["model"] = model

                    turns.append({
                        "session_id": session_id,
                        "timestamp": timestamp,
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_tokens": cache_read,
                        "cache_creation_tokens": cache_creation,
                        "tool_name": tool_name,
                        "cwd": cwd,
                    })

    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")

    return list(session_meta.values()), turns, line_count


def compute_events_for_file(filepath):
    """Full-scan one JSONL and return per-session cache-eviction events and
    compact-boundary events. Idempotent — safe to call repeatedly.

    Returns:
        (cache_events_by_sid, compact_events_by_sid)
    where each event is a plain dict suitable for direct DB insertion.
    """
    sessions_main_turns = {}   # sid -> [{ts, cr, cc, cache_1h, cache_5m}, ...]
    sessions_compacts = {}     # sid -> [{ts, trigger, pre_tokens}, ...]

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                sid = record.get("sessionId")
                if not sid:
                    continue
                ts = record.get("timestamp", "") or ""
                rtype = record.get("type")

                if rtype == "assistant" and not record.get("isSidechain"):
                    msg = record.get("message") or {}
                    usage = msg.get("usage") or {}
                    model = msg.get("model", "") or ""
                    cc_obj = usage.get("cache_creation") or {}
                    if isinstance(cc_obj, dict):
                        c1h = cc_obj.get("ephemeral_1h_input_tokens", 0) or 0
                        c5m = cc_obj.get("ephemeral_5m_input_tokens", 0) or 0
                    else:
                        c1h = c5m = 0
                    sessions_main_turns.setdefault(sid, []).append({
                        "ts": ts,
                        "model": model,
                        "cr": usage.get("cache_read_input_tokens", 0) or 0,
                        "cc": usage.get("cache_creation_input_tokens", 0) or 0,
                        "cache_1h": c1h,
                        "cache_5m": c5m,
                    })
                elif rtype == "system" and record.get("subtype") == "compact_boundary":
                    cm = record.get("compactMetadata") or {}
                    trig = cm.get("trigger", "manual")
                    if trig not in ("manual", "auto"):
                        trig = "manual"
                    sessions_compacts.setdefault(sid, []).append({
                        "ts": ts,
                        "trigger": trig,
                        "pre_tokens": cm.get("preTokens", 0) or 0,
                    })
    except Exception:
        return {}, {}

    cache_by_sid = {}
    for sid, turns_list in sessions_main_turns.items():
        turns_list.sort(key=lambda x: x["ts"])
        compact_ts = sorted(c["ts"] for c in sessions_compacts.get(sid, []))
        events = []
        for i in range(1, len(turns_list)):
            prev = turns_list[i - 1]
            curr = turns_list[i]
            if not (prev["cr"] >= 5000 and curr["cr"] < prev["cr"] * 0.2 and curr["cc"] >= 5000):
                continue
            if any(prev["ts"] < x < curr["ts"] for x in compact_ts):
                continue
            try:
                ta = datetime.fromisoformat(prev["ts"].replace("Z", "+00:00"))
                tb = datetime.fromisoformat(curr["ts"].replace("Z", "+00:00"))
                gap_min = (tb - ta).total_seconds() / 60
            except Exception:
                continue
            if gap_min > 60:
                category = "ttl-1h"
            elif gap_min > 5 and curr["cache_5m"] > curr["cache_1h"]:
                category = "ttl-5m"
            else:
                category = "mutation"
            events.append({
                "timestamp": curr["ts"],
                "gap_min": round(gap_min, 2),
                "category": category,
                "rewritten_tokens": curr["cc"],
                "model": curr.get("model") or "",
            })
        if events:
            cache_by_sid[sid] = events

    # For each compact event, assign the model of the nearest preceding
    # assistant turn in the same session (compactions don't carry a model
    # field themselves).
    compact_by_sid = {}
    for sid, clist in sessions_compacts.items():
        turns_list = sessions_main_turns.get(sid, [])
        turns_list_sorted = sorted(turns_list, key=lambda x: x["ts"])
        out = []
        for c in clist:
            model = ""
            for t in turns_list_sorted:
                if t["ts"] <= c["ts"] and t.get("model"):
                    model = t["model"]
                elif t["ts"] > c["ts"]:
                    break
            out.append({
                "timestamp":  c["ts"],
                "trigger":    c["trigger"],
                "pre_tokens": c["pre_tokens"],
                "model":      model,
            })
        compact_by_sid[sid] = out
    return cache_by_sid, compact_by_sid


def upsert_events_for_file(conn, filepath, cache_by_sid, compact_by_sid):
    """Replace all events that originated from one JSONL file. Events from
    other files (even for the same session) are left alone — a session may
    be split across multiple files after `--resume`."""
    conn.execute("DELETE FROM cache_events WHERE source_file = ?", (filepath,))
    conn.execute("DELETE FROM compact_events WHERE source_file = ?", (filepath,))
    for sid, events in cache_by_sid.items():
        conn.executemany(
            "INSERT INTO cache_events (session_id, timestamp, gap_min, category, rewritten_tokens, source_file, model) VALUES (?,?,?,?,?,?,?)",
            [(sid, e["timestamp"], e["gap_min"], e["category"], e["rewritten_tokens"], filepath, e.get("model", "")) for e in events],
        )
    for sid, events in compact_by_sid.items():
        conn.executemany(
            "INSERT INTO compact_events (session_id, timestamp, trigger, pre_tokens, source_file, model) VALUES (?,?,?,?,?,?)",
            [(sid, e["timestamp"], e["trigger"], e["pre_tokens"], filepath, e.get("model", "")) for e in events],
        )


def aggregate_sessions(session_metas, turns):
    """Aggregate turn data back into session-level stats."""
    from collections import defaultdict

    session_stats = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "turn_count": 0,
        "model": None,
    })

    for t in turns:
        s = session_stats[t["session_id"]]
        s["total_input_tokens"] += t["input_tokens"]
        s["total_output_tokens"] += t["output_tokens"]
        s["total_cache_read"] += t["cache_read_tokens"]
        s["total_cache_creation"] += t["cache_creation_tokens"]
        s["turn_count"] += 1
        if t["model"]:
            s["model"] = t["model"]

    # Merge into session_metas
    result = []
    for meta in session_metas:
        sid = meta["session_id"]
        stats = session_stats[sid]
        result.append({**meta, **stats})
    return result


def upsert_sessions(conn, sessions):
    for s in sessions:
        # Check if session exists
        existing = conn.execute(
            "SELECT total_input_tokens, total_output_tokens, total_cache_read, "
            "total_cache_creation, turn_count FROM sessions WHERE session_id = ?",
            (s["session_id"],)
        ).fetchone()

        if existing is None:
            conn.execute("""
                INSERT INTO sessions
                    (session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, model, turn_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["session_id"], s["project_name"], s["first_timestamp"],
                s["last_timestamp"], s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["model"], s["turn_count"]
            ))
        else:
            # Update: add new tokens on top of existing (since we only insert new turns)
            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read = total_cache_read + ?,
                    total_cache_creation = total_cache_creation + ?,
                    turn_count = turn_count + ?,
                    model = COALESCE(?, model)
                WHERE session_id = ?
            """, (
                s["last_timestamp"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["turn_count"], s["model"],
                s["session_id"]
            ))


def insert_turns(conn, turns):
    conn.executemany("""
        INSERT INTO turns
            (session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, tool_name, cwd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (t["session_id"], t["timestamp"], t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t["tool_name"], t["cwd"])
        for t in turns
    ])


def scan(projects_dir=PROJECTS_DIR, db_path=DB_PATH, verbose=True):
    conn = get_db(db_path)
    init_db(conn)

    jsonl_files = glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True)
    jsonl_files.sort()

    new_files = 0
    updated_files = 0
    skipped_files = 0
    total_turns = 0
    total_sessions = set()

    for filepath in jsonl_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ?",
            (filepath,)
        ).fetchone()

        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            continue

        is_new = row is None
        old_lines = row["lines"] if row else 0

        if verbose:
            status = "NEW" if is_new else "UPD"
            print(f"  [{status}] {os.path.relpath(filepath, projects_dir)}")

        # Single pass: metadata from all lines, turns from new lines only
        session_metas, turns, line_count = parse_jsonl_file(filepath, turns_offset=old_lines)

        if line_count <= old_lines and not is_new:
            # mtime changed but no new lines (e.g. touch)
            conn.execute("UPDATE processed_files SET mtime = ? WHERE path = ?", (mtime, filepath))
            conn.commit()
            skipped_files += 1
            continue

        if turns or session_metas:
            sessions = aggregate_sessions(session_metas, turns)
            upsert_sessions(conn, sessions)
            insert_turns(conn, turns)

            for s in sessions:
                total_sessions.add(s["session_id"])
            total_turns += len(turns)

        # Recompute cache/compact events for this file (idempotent)
        cache_by_sid, compact_by_sid = compute_events_for_file(filepath)
        upsert_events_for_file(conn, filepath, cache_by_sid, compact_by_sid)

        conn.execute("""
            INSERT OR REPLACE INTO processed_files (path, mtime, lines)
            VALUES (?, ?, ?)
        """, (filepath, mtime, line_count))
        conn.commit()

        if is_new:
            new_files += 1
        else:
            updated_files += 1

    if verbose:
        print(f"\nScan complete:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        print(f"  Turns added:   {total_turns}")
        print(f"  Sessions seen: {len(total_sessions)}")

    conn.close()
    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(total_sessions)}


def rebuild_events_all(projects_dir=PROJECTS_DIR, db_path=DB_PATH, verbose=True):
    """Force-recompute cache_events + compact_events across all JSONL files.
    Wipes the events tables first — leaves turns and sessions untouched."""
    conn = get_db(db_path)
    init_db(conn)
    conn.execute("DELETE FROM cache_events")
    conn.execute("DELETE FROM compact_events")
    jsonl_files = glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True)
    total_cache = 0
    total_compact = 0
    for filepath in jsonl_files:
        cache_by_sid, compact_by_sid = compute_events_for_file(filepath)
        if not cache_by_sid and not compact_by_sid:
            continue
        upsert_events_for_file(conn, filepath, cache_by_sid, compact_by_sid)
        total_cache += sum(len(v) for v in cache_by_sid.values())
        total_compact += sum(len(v) for v in compact_by_sid.values())
    conn.commit()
    conn.close()
    if verbose:
        print(f"Rebuilt events: {total_cache} cache, {total_compact} compact across {len(jsonl_files)} files.")
    return {"cache": total_cache, "compact": total_compact, "files": len(jsonl_files)}


def reconcile_sessions(db_path=DB_PATH):
    """Recompute session-level token aggregates from the turns table."""
    conn = get_db(db_path)
    conn.execute("""
        UPDATE sessions SET
            total_input_tokens   = (SELECT COALESCE(SUM(input_tokens), 0)          FROM turns t WHERE t.session_id = sessions.session_id),
            total_output_tokens  = (SELECT COALESCE(SUM(output_tokens), 0)         FROM turns t WHERE t.session_id = sessions.session_id),
            total_cache_read     = (SELECT COALESCE(SUM(cache_read_tokens), 0)     FROM turns t WHERE t.session_id = sessions.session_id),
            total_cache_creation = (SELECT COALESCE(SUM(cache_creation_tokens), 0) FROM turns t WHERE t.session_id = sessions.session_id),
            turn_count           = (SELECT COUNT(*)                                 FROM turns t WHERE t.session_id = sessions.session_id)
    """)
    conn.commit()
    affected = conn.execute("SELECT changes()").fetchone()[0]
    conn.close()
    print(f"Reconciled {affected} sessions from turns table.")

def resolve_session_id(session_prefix, db_path=DB_PATH):
    """Resolve an 8-char session ID prefix to the full session ID."""
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? LIMIT 1",
        (session_prefix + "%",)
    ).fetchone()
    conn.close()
    return row["session_id"] if row else None


def get_session_transcript(session_id, projects_dir=PROJECTS_DIR, db_path=DB_PATH):
    """Read raw JSONL files and extract full conversation for a session.

    Returns a list of turn dicts with full message content, ordered by timestamp.
    The DB only stores token counts — this reads the original JSONL for content.
    """
    from pricing import calc_cost, calc_cost_breakdown

    # Resolve short prefix to full ID if needed
    if len(session_id) < 36:
        full_id = resolve_session_id(session_id, db_path)
        if not full_id:
            return []
        session_id = full_id

    # Find all JSONL files (the session could appear in any of them,
    # though typically each file contains one session)
    jsonl_files = glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True)

    turns = []
    session_meta = {
        "session_id": session_id,
        "project": "unknown",
        "model": None,
        "cwd": None,
    }

    for filepath in jsonl_files:
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if record.get("sessionId") != session_id:
                        continue

                    rtype = record.get("type")
                    timestamp = record.get("timestamp", "")
                    cwd = record.get("cwd", "")

                    if cwd and not session_meta["cwd"]:
                        session_meta["cwd"] = cwd
                        session_meta["project"] = project_name_from_cwd(cwd)

                    if rtype == "user":
                        msg = record.get("message", {})
                        raw_content = msg.get("content", "")

                        # Normalize content to a list of blocks
                        content_blocks = []
                        if isinstance(raw_content, str):
                            if raw_content.strip():
                                content_blocks.append({
                                    "type": "text",
                                    "text": raw_content,
                                })
                        elif isinstance(raw_content, list):
                            for item in raw_content:
                                if isinstance(item, dict):
                                    itype = item.get("type")
                                    if itype == "tool_result":
                                        result_content = item.get("content", "")
                                        if isinstance(result_content, list):
                                            # Flatten nested content blocks
                                            parts = []
                                            for rc in result_content:
                                                if isinstance(rc, dict) and rc.get("type") == "text":
                                                    parts.append(rc.get("text", ""))
                                                elif isinstance(rc, str):
                                                    parts.append(rc)
                                            result_content = "\n".join(parts) if parts else str(result_content)
                                        content_blocks.append({
                                            "type": "tool_result",
                                            "tool_use_id": item.get("tool_use_id", ""),
                                            "content": result_content,
                                        })
                                    elif itype == "text":
                                        content_blocks.append({
                                            "type": "text",
                                            "text": item.get("text", ""),
                                        })

                        if content_blocks:
                            turns.append({
                                "type": "user",
                                "timestamp": timestamp,
                                "content": content_blocks,
                            })

                    elif rtype == "assistant":
                        msg = record.get("message", {})
                        usage = msg.get("usage", {})
                        model = msg.get("model", "")
                        raw_content = msg.get("content", [])

                        if model:
                            session_meta["model"] = model

                        input_tokens = usage.get("input_tokens", 0) or 0
                        output_tokens = usage.get("output_tokens", 0) or 0
                        cache_read = usage.get("cache_read_input_tokens", 0) or 0
                        cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                        cc_obj = usage.get("cache_creation") or {}
                        if isinstance(cc_obj, dict):
                            cache_1h = cc_obj.get("ephemeral_1h_input_tokens", 0) or 0
                            cache_5m = cc_obj.get("ephemeral_5m_input_tokens", 0) or 0
                        else:
                            cache_1h = cache_5m = 0

                        content_blocks = []
                        for item in raw_content:
                            if not isinstance(item, dict):
                                continue
                            itype = item.get("type")
                            if itype == "thinking":
                                thinking_text = item.get("thinking", "")
                                if thinking_text:
                                    content_blocks.append({
                                        "type": "thinking",
                                        "text": thinking_text,
                                    })
                            elif itype == "text":
                                text = item.get("text", "")
                                if text:
                                    content_blocks.append({
                                        "type": "text",
                                        "text": text,
                                    })
                            elif itype == "tool_use":
                                content_blocks.append({
                                    "type": "tool_use",
                                    "name": item.get("name", ""),
                                    "id": item.get("id", ""),
                                    "input": item.get("input", {}),
                                })

                        cost = calc_cost(model, input_tokens, output_tokens,
                                         cache_read, cache_creation)
                        bd = calc_cost_breakdown(model, input_tokens, output_tokens,
                                                 cache_read, cache_creation)

                        turns.append({
                            "type": "assistant",
                            "timestamp": timestamp,
                            "model": model,
                            "sidechain": bool(record.get("isSidechain")),
                            "content": content_blocks,
                            "usage": {
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "cache_read": cache_read,
                                "cache_creation": cache_creation,
                                "cache_1h": cache_1h,
                                "cache_5m": cache_5m,
                            },
                            "cost": cost,
                            "cost_breakdown": {
                                "input": bd["input_cost"],
                                "output": bd["output_cost"],
                                "cache_read": bd["cache_read_cost"],
                                "cache_creation": bd["cache_creation_cost"],
                            },
                        })

                    elif rtype == "system" and record.get("subtype") == "compact_boundary":
                        cm = record.get("compactMetadata") or {}
                        trig = cm.get("trigger", "manual")
                        if trig not in ("manual", "auto"):
                            trig = "manual"
                        turns.append({
                            "type": "compact",
                            "timestamp": timestamp,
                            "trigger": trig,
                            "pre_tokens": cm.get("preTokens", 0) or 0,
                        })

        except Exception as e:
            continue

    # Sort by timestamp
    turns.sort(key=lambda t: t.get("timestamp", ""))

    assistant_turns = [t for t in turns if t["type"] == "assistant"]
    compact_turns = [t for t in turns if t["type"] == "compact"]
    compact_timestamps = sorted(t["timestamp"] for t in compact_turns if t.get("timestamp"))

    # Detect cache-eviction events on consecutive main-thread assistant turns.
    # Rule: prev had ≥5000 cache_read, current cache_read collapsed (<20% of prev),
    # current cache_creation ≥5000. Pairs separated by a compact_boundary are skipped
    # (post-compact rewrites are guaranteed, not "evictions"). Sidechain (subagent)
    # turns are excluded — they belong to a different prompt/context and their
    # cache_read isn't comparable to the main conversation.
    main_thread = [t for t in assistant_turns if not t.get("sidechain")]
    eviction_gaps = []
    for i in range(1, len(main_thread)):
        prev = main_thread[i - 1]
        curr = main_thread[i]
        pu = prev.get("usage", {})
        cu = curr.get("usage", {})
        prev_cr = pu.get("cache_read", 0)
        curr_cr = cu.get("cache_read", 0)
        curr_cc = cu.get("cache_creation", 0)
        if not (prev_cr >= 5000 and curr_cr < prev_cr * 0.2 and curr_cc >= 5000):
            continue
        p_ts = prev.get("timestamp", "")
        c_ts = curr.get("timestamp", "")
        if any(p_ts < x < c_ts for x in compact_timestamps):
            continue
        try:
            ta = datetime.fromisoformat(p_ts.replace("Z", "+00:00"))
            tb = datetime.fromisoformat(c_ts.replace("Z", "+00:00"))
            gap_min = (tb - ta).total_seconds() / 60
        except Exception:
            continue
        tier_1h = cu.get("cache_1h", 0)
        tier_5m = cu.get("cache_5m", 0)
        if gap_min > 60:
            category = "ttl-1h"
        elif gap_min > 5 and tier_5m > tier_1h:
            category = "ttl-5m"
        else:
            category = "mutation"
        curr["eviction"] = {
            "gap_minutes": round(gap_min, 2),
            "category": category,
            "rewritten_tokens": curr_cc,
        }
        eviction_gaps.append(gap_min)

    ev_count = len(eviction_gaps)
    # Bucket events by gap to answer "would 1h tier help?":
    #   <5m   = prefix mutation, TTL irrelevant
    #   5-60m = 5m tier died but 1h would still be alive
    #   >=60m = both tiers dead, no TTL setting would save it
    cache_stats = {
        "count":     ev_count,
        "mutation":  sum(1 for g in eviction_gaps if g < 5),
        "save_1h":   sum(1 for g in eviction_gaps if 5 <= g < 60),
        "exhausted": sum(1 for g in eviction_gaps if g >= 60),
    }

    compact_counts = {"manual": 0, "auto": 0}
    for t in compact_turns:
        compact_counts[t.get("trigger", "manual")] += 1

    # Build token/cost summary
    total_cost = sum(t.get("cost", 0) for t in assistant_turns)
    total_input = sum(t["usage"].get("input_tokens", 0) for t in assistant_turns)
    total_output = sum(t["usage"].get("output_tokens", 0) for t in assistant_turns)
    total_cache_read = sum(t["usage"].get("cache_read", 0) for t in assistant_turns)
    total_cache_creation = sum(t["usage"].get("cache_creation", 0) for t in assistant_turns)
    total_cost_breakdown = {
        "input": sum(t.get("cost_breakdown", {}).get("input", 0) for t in assistant_turns),
        "output": sum(t.get("cost_breakdown", {}).get("output", 0) for t in assistant_turns),
        "cache_read": sum(t.get("cost_breakdown", {}).get("cache_read", 0) for t in assistant_turns),
        "cache_creation": sum(t.get("cost_breakdown", {}).get("cache_creation", 0) for t in assistant_turns),
    }

    first_ts = turns[0]["timestamp"] if turns else ""
    last_ts = turns[-1]["timestamp"] if turns else ""
    try:
        t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        duration_min = round((t2 - t1).total_seconds() / 60, 1)
    except Exception:
        duration_min = 0

    return {
        "session_id": session_id,
        "project": session_meta["project"],
        "model": session_meta["model"] or "unknown",
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "duration_min": duration_min,
        "total_cost": total_cost,
        "total_cost_breakdown": total_cost_breakdown,
        "total_input": total_input,
        "total_output": total_output,
        "total_cache_read": total_cache_read,
        "total_cache_creation": total_cache_creation,
        "turn_count": len(assistant_turns),
        "cache_stats": cache_stats,
        "compact_counts": compact_counts,
        "turns": turns,
    }


if __name__ == "__main__":
    print(f"Scanning {PROJECTS_DIR} ...")
    scan()
