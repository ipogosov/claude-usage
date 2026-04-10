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

        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
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


if __name__ == "__main__":
    print(f"Scanning {PROJECTS_DIR} ...")
    scan()
