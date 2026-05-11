import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, parse, request


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "stats.db")))

HOST = os.getenv("HOST", "0.0.0.0")
try:
    PORT = int(os.getenv("PORT", "8088"))
except Exception:
    PORT = 8088

refresh_lock = threading.Lock()
scheduler_state = {
    "last_run_at": None,
    "last_ok": None,
    "last_message": "",
    "next_run_at": None,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def to_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def pick_text(*vals, default="-"):
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return default


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_has_column(conn, table_name, col_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(r["name"] == col_name for r in rows)


def ensure_column(conn, table_name, col_name, col_def):
    if not table_has_column(conn, table_name, col_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")


def init_db():
    conn = get_conn()
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS app_config (
              id INTEGER PRIMARY KEY CHECK (id=1),
              active_profile_id INTEGER,
              refresh_interval_sec INTEGER NOT NULL DEFAULT 60,
              auto_refresh_enabled INTEGER NOT NULL DEFAULT 0,
              lookback_hours INTEGER NOT NULL DEFAULT 24,
              retention_days INTEGER NOT NULL DEFAULT 30
            );
            INSERT OR IGNORE INTO app_config (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS profiles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              base_url TEXT NOT NULL DEFAULT '',
              token TEXT NOT NULL DEFAULT '',
              endpoint_mode TEXT NOT NULL DEFAULT 'auto',
              queue_count INTEGER NOT NULL DEFAULT 300,
              is_enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fetched_at TEXT NOT NULL,
              profile_id INTEGER NOT NULL DEFAULT 0,
              profile_name TEXT NOT NULL DEFAULT '',
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              alias TEXT NOT NULL,
              source TEXT NOT NULL,
              auth_account TEXT NOT NULL DEFAULT '',
              external_key TEXT NOT NULL DEFAULT '',
              requests INTEGER NOT NULL,
              success INTEGER NOT NULL,
              failed INTEGER NOT NULL,
              input_tokens INTEGER NOT NULL,
              output_tokens INTEGER NOT NULL,
              reasoning_tokens INTEGER NOT NULL,
              total_tokens INTEGER NOT NULL,
              avg_latency_ms REAL NOT NULL,
              min_latency_ms REAL NOT NULL,
              max_latency_ms REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_fetched_at ON usage_records(fetched_at);

            CREATE TABLE IF NOT EXISTS pull_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fetched_at TEXT NOT NULL,
              profile_id INTEGER NOT NULL DEFAULT 0,
              profile_name TEXT NOT NULL DEFAULT '',
              source_endpoint TEXT NOT NULL,
              model_groups INTEGER NOT NULL,
              total_requests INTEGER NOT NULL,
              total_success INTEGER NOT NULL,
              total_failed INTEGER NOT NULL,
              total_input_tokens INTEGER NOT NULL,
              total_output_tokens INTEGER NOT NULL,
              total_reasoning_tokens INTEGER NOT NULL,
              total_tokens INTEGER NOT NULL,
              avg_latency_ms REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_fetched_at ON pull_snapshots(fetched_at);

            CREATE TABLE IF NOT EXISTS pull_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fetched_at TEXT NOT NULL,
              profile_id INTEGER NOT NULL DEFAULT 0,
              profile_name TEXT NOT NULL DEFAULT '',
              ok INTEGER NOT NULL,
              source_endpoint TEXT NOT NULL,
              message TEXT NOT NULL,
              trace_json TEXT NOT NULL
            );
            """
        )

        # Backward-compatible migration from old schema if old columns still exist.
        ensure_column(conn, "app_config", "active_profile_id", "INTEGER")
        ensure_column(conn, "app_config", "retention_days", "INTEGER NOT NULL DEFAULT 30")
        ensure_column(conn, "app_config", "refresh_interval_sec", "INTEGER NOT NULL DEFAULT 60")
        ensure_column(conn, "app_config", "auto_refresh_enabled", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "app_config", "lookback_hours", "INTEGER NOT NULL DEFAULT 24")
        conn.execute(
            """
            UPDATE app_config SET
              refresh_interval_sec = COALESCE(refresh_interval_sec, 60),
              auto_refresh_enabled = COALESCE(auto_refresh_enabled, 0),
              lookback_hours = COALESCE(lookback_hours, 24),
              retention_days = COALESCE(retention_days, 30)
            WHERE id=1
            """
        )

        ensure_column(conn, "usage_records", "profile_id", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "usage_records", "profile_name", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "usage_records", "auth_account", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "usage_records", "external_key", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "pull_snapshots", "profile_id", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "pull_snapshots", "profile_name", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "pull_logs", "profile_id", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "pull_logs", "profile_name", "TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_profile_model_source ON usage_records(profile_id, model, source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_profile_model_auth_key ON usage_records(profile_id, model, auth_account, external_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_profile_time ON pull_snapshots(profile_id, fetched_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_profile_time ON pull_logs(profile_id, fetched_at)")

        # Bootstrap profile from legacy app_config fields if profiles empty.
        profile_count = conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()["c"]
        if profile_count == 0:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(app_config)").fetchall()}
            base_url = ""
            token = ""
            endpoint_mode = "auto"
            queue_count = 300
            if "base_url" in cols:
                row = conn.execute("SELECT base_url, token, endpoint_mode, queue_count FROM app_config WHERE id=1").fetchone()
                if row:
                    base_url = str(row["base_url"] or "").strip()
                    token = str(row["token"] or "").strip()
                    mode = str(row["endpoint_mode"] or "auto").strip().lower()
                    endpoint_mode = mode if mode in ("auto", "queue", "legacy") else "auto"
                    queue_count = max(1, min(10000, to_int(row["queue_count"], 300)))
            now = now_iso()
            conn.execute(
                """
                INSERT INTO profiles (name, base_url, token, endpoint_mode, queue_count, is_enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                ("default", base_url, token, endpoint_mode, queue_count, now, now),
            )

        # Ensure active profile exists.
        active = conn.execute("SELECT active_profile_id FROM app_config WHERE id=1").fetchone()["active_profile_id"]
        if not active:
            first = conn.execute("SELECT id FROM profiles ORDER BY id ASC LIMIT 1").fetchone()
            if first:
                conn.execute("UPDATE app_config SET active_profile_id=? WHERE id=1", (first["id"],))

        conn.commit()
    finally:
        conn.close()


def read_config():
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM app_config WHERE id=1").fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def write_config(payload):
    cfg = read_config()
    merged = dict(cfg)
    for k in ("active_profile_id", "refresh_interval_sec", "auto_refresh_enabled", "lookback_hours", "retention_days"):
        if k in payload:
            merged[k] = payload[k]

    merged["active_profile_id"] = to_int(merged.get("active_profile_id"), 0) or None
    merged["refresh_interval_sec"] = max(5, min(86400, to_int(merged.get("refresh_interval_sec", 60), 60)))
    merged["auto_refresh_enabled"] = 1 if to_int(merged.get("auto_refresh_enabled", 0), 0) else 0
    merged["lookback_hours"] = max(1, min(24 * 90, to_int(merged.get("lookback_hours", 24), 24)))
    merged["retention_days"] = max(1, min(3650, to_int(merged.get("retention_days", 30), 30)))

    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE app_config SET
              active_profile_id=?,
              refresh_interval_sec=?,
              auto_refresh_enabled=?,
              lookback_hours=?,
              retention_days=?
            WHERE id=1
            """,
            (
                merged["active_profile_id"],
                merged["refresh_interval_sec"],
                merged["auto_refresh_enabled"],
                merged["lookback_hours"],
                merged["retention_days"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return merged


def sanitize_profile_payload(payload):
    p = dict(payload or {})
    p["name"] = str(p.get("name", "")).strip()
    p["base_url"] = str(p.get("base_url", "")).strip().rstrip("/")
    if "token" in p:
        p["token"] = str(p.get("token", "")).strip()
    mode = str(p.get("endpoint_mode", "auto")).strip().lower()
    p["endpoint_mode"] = mode if mode in ("auto", "queue", "legacy") else "auto"
    p["queue_count"] = max(1, min(10000, to_int(p.get("queue_count", 300), 300)))
    p["is_enabled"] = 1 if to_int(p.get("is_enabled", 1), 1) else 0
    return p


def list_profiles():
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, name, base_url, endpoint_mode, queue_count, is_enabled, created_at, updated_at,
                   CASE WHEN token <> '' THEN 1 ELSE 0 END AS has_token
            FROM profiles
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_profile(profile_id):
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT id, name, base_url, token, endpoint_mode, queue_count, is_enabled, created_at, updated_at
            FROM profiles WHERE id=?
            """,
            (profile_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_active_profile():
    cfg = read_config()
    active_id = cfg.get("active_profile_id")
    profile = get_profile(active_id) if active_id else None
    if profile:
        return profile
    profiles = list_profiles()
    if profiles:
        set_active_profile(profiles[0]["id"])
        return get_profile(profiles[0]["id"])
    return None


def upsert_profile(payload):
    p = sanitize_profile_payload(payload)
    if not p["name"]:
        raise RuntimeError("profile name is required")

    pid = to_int(payload.get("id"), 0) if isinstance(payload, dict) else 0
    now = now_iso()
    conn = get_conn()
    try:
        if pid > 0:
            old = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
            if not old:
                raise RuntimeError("profile not found")
            token_to_save = old["token"]
            if "token" in payload and p.get("token", ""):
                token_to_save = p["token"]
            elif payload.get("force_clear_token"):
                token_to_save = ""

            conn.execute(
                """
                UPDATE profiles SET
                  name=?, base_url=?, token=?, endpoint_mode=?, queue_count=?, is_enabled=?, updated_at=?
                WHERE id=?
                """,
                (p["name"], p["base_url"], token_to_save, p["endpoint_mode"], p["queue_count"], p["is_enabled"], now, pid),
            )
            conn.commit()
            return pid

        token = p.get("token", "")
        conn.execute(
            """
            INSERT INTO profiles (name, base_url, token, endpoint_mode, queue_count, is_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (p["name"], p["base_url"], token, p["endpoint_mode"], p["queue_count"], p["is_enabled"], now, now),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return new_id
    except sqlite3.IntegrityError:
        raise RuntimeError("profile name already exists")
    finally:
        conn.close()


def set_active_profile(profile_id):
    pid = to_int(profile_id, 0)
    if pid <= 0:
        raise RuntimeError("invalid profile id")
    p = get_profile(pid)
    if not p:
        raise RuntimeError("profile not found")
    conn = get_conn()
    try:
        conn.execute("UPDATE app_config SET active_profile_id=? WHERE id=1", (pid,))
        conn.commit()
    finally:
        conn.close()
    return pid


def delete_profile(profile_id):
    pid = to_int(profile_id, 0)
    if pid <= 0:
        raise RuntimeError("invalid profile id")
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM profiles WHERE id=?", (pid,)).fetchone()
        if not row:
            raise RuntimeError("profile not found")
        count = conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()["c"]
        if count <= 1:
            raise RuntimeError("cannot delete last profile")

        conn.execute("DELETE FROM profiles WHERE id=?", (pid,))
        active_id = conn.execute("SELECT active_profile_id FROM app_config WHERE id=1").fetchone()["active_profile_id"]
        if active_id == pid:
            first = conn.execute("SELECT id FROM profiles ORDER BY id ASC LIMIT 1").fetchone()
            conn.execute("UPDATE app_config SET active_profile_id=? WHERE id=1", (first["id"],))
        conn.commit()
    finally:
        conn.close()


def request_json(url, token, label, traces):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, headers=headers, method="GET")
    t0 = time.time()
    try:
        with request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        status = e.code
        ms = int((time.time() - t0) * 1000)
        traces.append({"label": label, "url": url, "ok": False, "status": status, "ms": ms, "preview": " ".join(body[:220].split())})
        raise RuntimeError(f"{label} {status}: {body[:200]}")
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        traces.append({"label": label, "url": url, "ok": False, "status": "FETCH_FAIL", "ms": ms, "preview": str(e)})
        raise RuntimeError(f"{label} request failed: {e}")

    ms = int((time.time() - t0) * 1000)
    traces.append({"label": label, "url": url, "ok": 200 <= status < 300, "status": status, "ms": ms, "preview": " ".join(body[:220].split())})
    try:
        return json.loads(body) if body else None
    except Exception:
        raise RuntimeError(f"{label} returned non-JSON: {body[:200]}")


def unwrap_array(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "result"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def normalize_queue(item):
    tokens = item.get("tokens") if isinstance(item.get("tokens"), dict) else {}
    input_tokens = to_int(tokens.get("input_tokens", tokens.get("prompt", item.get("input_tokens", item.get("prompt_tokens", item.get("prompt", 0))))), 0)
    output_tokens = to_int(
        tokens.get("output_tokens", tokens.get("completion", item.get("output_tokens", item.get("completion_tokens", item.get("completion", 0))))), 0
    )
    reasoning_tokens = to_int(tokens.get("reasoning_tokens", tokens.get("reasoning", item.get("reasoning_tokens", 0))), 0)
    total_tokens = to_int(tokens.get("total_tokens", tokens.get("total", item.get("total_tokens", input_tokens + output_tokens + reasoning_tokens))), 0)
    failed = 1 if item.get("failed", item.get("is_failed", False)) else 0
    lat = to_float(item.get("latency_ms", item.get("latency", 0)), 0.0)
    auth_account = pick_text(
        item.get("auth_account"),
        item.get("authAccount"),
        item.get("authorized_account"),
        item.get("authorizedAccount"),
        item.get("account"),
        item.get("auth_name"),
        item.get("source_account"),
        item.get("source"),
        default="-",
    )
    external_key = pick_text(
        item.get("external_key"),
        item.get("externalKey"),
        item.get("api_key"),
        item.get("apiKey"),
        item.get("key"),
        item.get("client_key"),
        item.get("consumer_key"),
        item.get("x_api_key"),
        default="-",
    )
    return {
        "provider": pick_text(item.get("provider"), item.get("provider_name"), default="-"),
        "model": pick_text(item.get("model"), item.get("model_name"), default="-"),
        "alias": pick_text(item.get("alias"), item.get("model_alias"), default="-"),
        "source": pick_text(item.get("source"), auth_account, default="-"),
        "auth_account": auth_account,
        "external_key": external_key,
        "requests": 1,
        "success": 0 if failed else 1,
        "failed": failed,
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "reasoning_tokens": max(0, reasoning_tokens),
        "total_tokens": max(0, total_tokens),
        "avg_latency_ms": max(0.0, lat),
        "min_latency_ms": max(0.0, lat),
        "max_latency_ms": max(0.0, lat),
    }


def normalize_usage(item):
    requests_count = max(1, to_int(item.get("requests", item.get("request_count", 1)), 1))
    failed = max(0, to_int(item.get("failed", item.get("fail_count", 0)), 0))
    success = max(0, requests_count - failed)
    input_tokens = max(0, to_int(item.get("input_tokens", item.get("prompt_tokens", item.get("prompt", 0))), 0))
    output_tokens = max(0, to_int(item.get("output_tokens", item.get("completion_tokens", item.get("completion", 0))), 0))
    reasoning_tokens = max(0, to_int(item.get("reasoning_tokens", 0), 0))
    total_tokens = max(0, to_int(item.get("total_tokens", item.get("total", input_tokens + output_tokens + reasoning_tokens)), 0))
    avg_latency = max(0.0, to_float(item.get("avg_latency_ms", item.get("latency_ms", item.get("latency", 0))), 0.0))
    auth_account = pick_text(
        item.get("auth_account"),
        item.get("authAccount"),
        item.get("authorized_account"),
        item.get("authorizedAccount"),
        item.get("account"),
        item.get("auth_name"),
        item.get("source_account"),
        item.get("source"),
        default="-",
    )
    external_key = pick_text(
        item.get("external_key"),
        item.get("externalKey"),
        item.get("api_key"),
        item.get("apiKey"),
        item.get("key"),
        item.get("client_key"),
        item.get("consumer_key"),
        item.get("x_api_key"),
        default="-",
    )
    return {
        "provider": pick_text(item.get("provider"), item.get("provider_name"), default="-"),
        "model": pick_text(item.get("model"), item.get("model_name"), default="-"),
        "alias": pick_text(item.get("alias"), item.get("model_alias"), default="-"),
        "source": pick_text(item.get("source"), auth_account, default="-"),
        "auth_account": auth_account,
        "external_key": external_key,
        "requests": requests_count,
        "success": success,
        "failed": failed,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "avg_latency_ms": avg_latency,
        "min_latency_ms": max(0.0, to_float(item.get("min_latency_ms", avg_latency), avg_latency)),
        "max_latency_ms": max(0.0, to_float(item.get("max_latency_ms", avg_latency), avg_latency)),
    }


def normalize_legacy_payload(payload):
    direct = unwrap_array(payload)
    if direct:
        return [normalize_usage(x) for x in direct if isinstance(x, dict)]

    out = []
    if isinstance(payload, dict):
        apis = payload.get("usage", {}).get("apis") if isinstance(payload.get("usage"), dict) else payload.get("apis")
        if isinstance(apis, dict):
            for name, val in apis.items():
                if not isinstance(val, dict):
                    continue
                out.append(
                    normalize_usage(
                        {
                            "provider": val.get("provider", "-"),
                            "model": val.get("model", name),
                            "alias": val.get("alias", "-"),
                            "source": val.get("source", val.get("account", val.get("auth_name", "-"))),
                            "auth_account": val.get("auth_account", val.get("account", val.get("auth_name", val.get("source", "-")))),
                            "external_key": val.get("external_key", val.get("api_key", val.get("key", "-"))),
                            "requests": val.get("requests", val.get("request_count", val.get("count", 1))),
                            "failed": val.get("failed", val.get("fail_count", 0)),
                            "input_tokens": val.get("input_tokens", val.get("prompt_tokens", val.get("input", 0))),
                            "output_tokens": val.get("output_tokens", val.get("completion_tokens", val.get("output", 0))),
                            "reasoning_tokens": val.get("reasoning_tokens", 0),
                            "total_tokens": val.get("total_tokens", val.get("total", 0)),
                            "avg_latency_ms": val.get("avg_latency_ms", val.get("latency_ms", 0)),
                            "min_latency_ms": val.get("min_latency_ms", val.get("latency_ms", 0)),
                            "max_latency_ms": val.get("max_latency_ms", val.get("latency_ms", 0)),
                        }
                    )
                )
    return out


def aggregate_rows(rows):
    grouped = {}
    for r in rows:
        key = (r["provider"], r["model"], r["alias"], r["source"], r.get("auth_account", "-"), r.get("external_key", "-"))
        if key not in grouped:
            grouped[key] = {
                "provider": r["provider"],
                "model": r["model"],
                "alias": r["alias"],
                "source": r["source"],
                "auth_account": pick_text(r.get("auth_account"), r.get("source"), default="-"),
                "external_key": pick_text(r.get("external_key"), default="-"),
                "requests": 0,
                "success": 0,
                "failed": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "lat_sum": 0.0,
                "lat_weight": 0,
                "min_latency_ms": 0.0,
                "max_latency_ms": 0.0,
            }
        g = grouped[key]
        req = max(1, to_int(r["requests"], 1))
        g["requests"] += req
        g["success"] += max(0, to_int(r["success"], 0))
        g["failed"] += max(0, to_int(r["failed"], 0))
        g["input_tokens"] += max(0, to_int(r["input_tokens"], 0))
        g["output_tokens"] += max(0, to_int(r["output_tokens"], 0))
        g["reasoning_tokens"] += max(0, to_int(r["reasoning_tokens"], 0))
        g["total_tokens"] += max(0, to_int(r["total_tokens"], 0))
        lat = max(0.0, to_float(r["avg_latency_ms"], 0.0))
        if lat > 0:
            g["lat_sum"] += lat * req
            g["lat_weight"] += req
            min_lat = max(0.0, to_float(r.get("min_latency_ms", lat), lat))
            max_lat = max(0.0, to_float(r.get("max_latency_ms", lat), lat))
            g["min_latency_ms"] = min_lat if g["min_latency_ms"] == 0 else min(g["min_latency_ms"], min_lat)
            g["max_latency_ms"] = max(g["max_latency_ms"], max_lat)

    out = []
    for g in grouped.values():
        avg_latency = g["lat_sum"] / g["lat_weight"] if g["lat_weight"] > 0 else 0.0
        out.append(
            {
                "provider": g["provider"],
                "model": g["model"],
                "alias": g["alias"],
                "source": g["source"],
                "auth_account": g["auth_account"],
                "external_key": g["external_key"],
                "requests": g["requests"],
                "success": g["success"],
                "failed": g["failed"],
                "input_tokens": g["input_tokens"],
                "output_tokens": g["output_tokens"],
                "reasoning_tokens": g["reasoning_tokens"],
                "total_tokens": g["total_tokens"],
                "avg_latency_ms": avg_latency,
                "min_latency_ms": g["min_latency_ms"],
                "max_latency_ms": g["max_latency_ms"],
            }
        )
    out.sort(key=lambda x: (x["total_tokens"], x["requests"]), reverse=True)
    return out


def persist_pull(fetched_at, profile, endpoint, traces, aggregated_rows, ok, message):
    profile_id = to_int(profile.get("id", 0), 0) if profile else 0
    profile_name = profile.get("name", "") if profile else ""
    conn = get_conn()
    try:
        if ok and aggregated_rows:
            conn.executemany(
                """
                INSERT INTO usage_records (
                  fetched_at, profile_id, profile_name, provider, model, alias, source, auth_account, external_key,
                  requests, success, failed,
                  input_tokens, output_tokens, reasoning_tokens, total_tokens,
                  avg_latency_ms, min_latency_ms, max_latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        fetched_at,
                        profile_id,
                        profile_name,
                        r["provider"],
                        r["model"],
                        r["alias"],
                        r["source"],
                        r.get("auth_account", "-"),
                        r.get("external_key", "-"),
                        r["requests"],
                        r["success"],
                        r["failed"],
                        r["input_tokens"],
                        r["output_tokens"],
                        r["reasoning_tokens"],
                        r["total_tokens"],
                        r["avg_latency_ms"],
                        r["min_latency_ms"],
                        r["max_latency_ms"],
                    )
                    for r in aggregated_rows
                ],
            )

            total_requests = sum(x["requests"] for x in aggregated_rows)
            total_success = sum(x["success"] for x in aggregated_rows)
            total_failed = sum(x["failed"] for x in aggregated_rows)
            total_input = sum(x["input_tokens"] for x in aggregated_rows)
            total_output = sum(x["output_tokens"] for x in aggregated_rows)
            total_reasoning = sum(x["reasoning_tokens"] for x in aggregated_rows)
            total_tokens = sum(x["total_tokens"] for x in aggregated_rows)
            lat_weight = sum(x["requests"] for x in aggregated_rows if x["avg_latency_ms"] > 0)
            lat_sum = sum(x["avg_latency_ms"] * x["requests"] for x in aggregated_rows if x["avg_latency_ms"] > 0)
            avg_latency = lat_sum / lat_weight if lat_weight > 0 else 0.0

            conn.execute(
                """
                INSERT INTO pull_snapshots (
                  fetched_at, profile_id, profile_name, source_endpoint, model_groups,
                  total_requests, total_success, total_failed,
                  total_input_tokens, total_output_tokens, total_reasoning_tokens,
                  total_tokens, avg_latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fetched_at,
                    profile_id,
                    profile_name,
                    endpoint,
                    len(aggregated_rows),
                    total_requests,
                    total_success,
                    total_failed,
                    total_input,
                    total_output,
                    total_reasoning,
                    total_tokens,
                    avg_latency,
                ),
            )

        conn.execute(
            """
            INSERT INTO pull_logs (fetched_at, profile_id, profile_name, ok, source_endpoint, message, trace_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (fetched_at, profile_id, profile_name, 1 if ok else 0, endpoint, message, json.dumps(traces, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_old_data(retention_days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))).isoformat(timespec="seconds")
    conn = get_conn()
    try:
        c1 = conn.execute("DELETE FROM usage_records WHERE fetched_at < ?", (cutoff,)).rowcount
        c2 = conn.execute("DELETE FROM pull_snapshots WHERE fetched_at < ?", (cutoff,)).rowcount
        c3 = conn.execute("DELETE FROM pull_logs WHERE fetched_at < ?", (cutoff,)).rowcount
        conn.commit()
        return {"cutoff": cutoff, "usage_records": c1, "pull_snapshots": c2, "pull_logs": c3}
    finally:
        conn.close()


def perform_refresh(trigger="manual"):
    if not refresh_lock.acquire(blocking=False):
        return {"ok": False, "message": "refresh already running"}

    fetched_at = now_iso()
    traces = []
    endpoint_used = "none"
    profile = get_active_profile()
    try:
        if not profile:
            msg = "no active profile"
            persist_pull(fetched_at, profile, endpoint_used, traces, [], False, msg)
            scheduler_state["last_run_at"] = fetched_at
            scheduler_state["last_ok"] = False
            scheduler_state["last_message"] = msg
            return {"ok": False, "message": msg}

        base = str(profile.get("base_url", "")).rstrip("/")
        token = profile.get("token", "")
        mode = profile.get("endpoint_mode", "auto")
        count = max(1, to_int(profile.get("queue_count", 300), 300))
        if not base:
            msg = "active profile base_url is empty"
            persist_pull(fetched_at, profile, endpoint_used, traces, [], False, msg)
            scheduler_state["last_run_at"] = fetched_at
            scheduler_state["last_ok"] = False
            scheduler_state["last_message"] = msg
            return {"ok": False, "message": msg}

        def load_queue():
            url = f"{base}/v0/management/usage-queue?count={count}"
            payload = request_json(url, token, "usage-queue", traces)
            return [normalize_queue(x) for x in unwrap_array(payload) if isinstance(x, dict)]

        def load_legacy():
            url = f"{base}/v0/management/usage"
            payload = request_json(url, token, "legacy-usage", traces)
            return normalize_legacy_payload(payload)

        rows = []
        if mode == "queue":
            endpoint_used = "usage-queue"
            rows = load_queue()
        elif mode == "legacy":
            endpoint_used = "legacy-usage"
            rows = load_legacy()
        else:
            queue_error = None
            try:
                endpoint_used = "usage-queue"
                rows = load_queue()
                if not rows:
                    try:
                        fallback_rows = load_legacy()
                        if fallback_rows:
                            endpoint_used = "legacy-usage-fallback"
                            rows = fallback_rows
                    except Exception:
                        pass
            except Exception as e_queue:
                queue_error = e_queue
                try:
                    endpoint_used = "legacy-usage"
                    rows = load_legacy()
                except Exception as e_legacy:
                    raise RuntimeError(f"usage-queue failed: {e_queue}; legacy-usage failed: {e_legacy}")

        aggregated = aggregate_rows(rows)
        msg = f"{trigger}: profile={profile['name']} source={endpoint_used}, raw={len(rows)}, groups={len(aggregated)}"
        persist_pull(fetched_at, profile, endpoint_used, traces, aggregated, True, msg)

        cfg = read_config()
        cleanup_old_data(max(1, to_int(cfg.get("retention_days", 30), 30)))

        scheduler_state["last_run_at"] = fetched_at
        scheduler_state["last_ok"] = True
        scheduler_state["last_message"] = msg
        return {
            "ok": True,
            "message": msg,
            "profile": {"id": profile["id"], "name": profile["name"]},
            "source": endpoint_used,
            "raw_count": len(rows),
            "group_count": len(aggregated),
            "traces": traces,
        }
    except Exception as e:
        msg = str(e)
        if "1010" in msg:
            msg += " | Cloudflare 1010: 站点基于客户端签名拦截，请优先使用 endpoint_mode=queue，并确认该域名允许当前服务器 IP/UA 访问。"
        persist_pull(fetched_at, profile, endpoint_used, traces, [], False, msg)
        scheduler_state["last_run_at"] = fetched_at
        scheduler_state["last_ok"] = False
        scheduler_state["last_message"] = msg
        return {"ok": False, "message": msg, "traces": traces}
    finally:
        refresh_lock.release()


def query_stats(hours, keyword, profile_id):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_iso = since.isoformat(timespec="seconds")
    conn = get_conn()
    try:
        where = "WHERE fetched_at >= ?"
        params = [since_iso]
        if profile_id:
            where += " AND profile_id = ?"
            params.append(profile_id)
        if keyword:
            where += " AND (provider LIKE ? OR model LIKE ? OR alias LIKE ? OR source LIKE ? OR auth_account LIKE ? OR external_key LIKE ?)"
            like = f"%{keyword}%"
            params.extend([like, like, like, like, like, like])

        rows = conn.execute(
            f"""
            SELECT
              provider, model, alias, source, auth_account, external_key,
              SUM(requests) AS requests,
              SUM(success) AS success,
              SUM(failed) AS failed,
              SUM(input_tokens) AS input_tokens,
              SUM(output_tokens) AS output_tokens,
              SUM(reasoning_tokens) AS reasoning_tokens,
              SUM(total_tokens) AS total_tokens,
              SUM(CASE WHEN avg_latency_ms > 0 THEN avg_latency_ms * requests ELSE 0 END) AS lat_sum,
              SUM(CASE WHEN avg_latency_ms > 0 THEN requests ELSE 0 END) AS lat_weight,
              MIN(CASE WHEN min_latency_ms > 0 THEN min_latency_ms ELSE NULL END) AS min_latency_ms,
              MAX(max_latency_ms) AS max_latency_ms
            FROM usage_records
            {where}
            GROUP BY provider, model, alias, source, auth_account, external_key
            ORDER BY total_tokens DESC, requests DESC
            """,
            params,
        ).fetchall()

        out = []
        for r in rows:
            req = to_int(r["requests"], 0)
            succ = to_int(r["success"], 0)
            lat_weight = to_int(r["lat_weight"], 0)
            avg_latency = to_float(r["lat_sum"], 0.0) / lat_weight if lat_weight > 0 else 0.0
            out.append(
                {
                    "provider": r["provider"],
                    "model": r["model"],
                    "alias": r["alias"],
                    "source": r["source"],
                    "auth_account": r["auth_account"],
                    "external_key": r["external_key"],
                    "requests": req,
                    "success": succ,
                    "failed": to_int(r["failed"], 0),
                    "success_rate": (succ / req) if req else 0.0,
                    "input_tokens": to_int(r["input_tokens"], 0),
                    "output_tokens": to_int(r["output_tokens"], 0),
                    "reasoning_tokens": to_int(r["reasoning_tokens"], 0),
                    "total_tokens": to_int(r["total_tokens"], 0),
                    "avg_latency_ms": avg_latency,
                    "min_latency_ms": to_float(r["min_latency_ms"], 0.0),
                    "max_latency_ms": to_float(r["max_latency_ms"], 0.0),
                }
            )

        summary = {
            "models": len(out),
            "requests": sum(x["requests"] for x in out),
            "success": sum(x["success"] for x in out),
            "failed": sum(x["failed"] for x in out),
            "input_tokens": sum(x["input_tokens"] for x in out),
            "output_tokens": sum(x["output_tokens"] for x in out),
            "reasoning_tokens": sum(x["reasoning_tokens"] for x in out),
            "total_tokens": sum(x["total_tokens"] for x in out),
            "avg_latency_ms": 0.0,
            "success_rate": 0.0,
        }
        summary["success_rate"] = (summary["success"] / summary["requests"]) if summary["requests"] else 0.0
        lat_weight = sum(x["requests"] for x in out if x["avg_latency_ms"] > 0)
        lat_sum = sum(x["avg_latency_ms"] * x["requests"] for x in out if x["avg_latency_ms"] > 0)
        summary["avg_latency_ms"] = lat_sum / lat_weight if lat_weight > 0 else 0.0
        return {"hours": hours, "since": since_iso, "summary": summary, "rows": out}
    finally:
        conn.close()


def query_trend(hours, limit, profile_id):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_iso = since.isoformat(timespec="seconds")
    conn = get_conn()
    try:
        where = "WHERE fetched_at >= ?"
        params = [since_iso]
        if profile_id:
            where += " AND profile_id = ?"
            params.append(profile_id)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT fetched_at, source_endpoint, total_requests, total_success, total_failed, total_tokens, avg_latency_ms
            FROM pull_snapshots
            {where}
            ORDER BY fetched_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        items = []
        for r in reversed(rows):
            req = to_int(r["total_requests"], 0)
            succ = to_int(r["total_success"], 0)
            items.append(
                {
                    "fetched_at": r["fetched_at"],
                    "source": r["source_endpoint"],
                    "requests": req,
                    "tokens": to_int(r["total_tokens"], 0),
                    "success_rate": (succ / req) if req else 0.0,
                    "avg_latency_ms": to_float(r["avg_latency_ms"], 0.0),
                }
            )
        return {"hours": hours, "points": items}
    finally:
        conn.close()


def query_logs(limit, profile_id):
    conn = get_conn()
    try:
        if profile_id:
            rows = conn.execute(
                """
                SELECT id, fetched_at, ok, source_endpoint, message, trace_json
                FROM pull_logs
                WHERE profile_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (profile_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, fetched_at, ok, source_endpoint, message, trace_json
                FROM pull_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        out = []
        for r in rows:
            try:
                traces = json.loads(r["trace_json"]) if r["trace_json"] else []
            except Exception:
                traces = []
            out.append(
                {
                    "id": r["id"],
                    "fetched_at": r["fetched_at"],
                    "ok": bool(r["ok"]),
                    "source": r["source_endpoint"],
                    "message": r["message"],
                    "traces": traces,
                }
            )
        return out
    finally:
        conn.close()


def query_records(hours, profile_id, keyword, limit):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_iso = since.isoformat(timespec="seconds")
    conn = get_conn()
    try:
        where = "WHERE fetched_at >= ?"
        params = [since_iso]
        if profile_id:
            where += " AND profile_id = ?"
            params.append(profile_id)
        if keyword:
            where += " AND (provider LIKE ? OR model LIKE ? OR alias LIKE ? OR source LIKE ? OR auth_account LIKE ? OR external_key LIKE ? OR profile_name LIKE ?)"
            like = f"%{keyword}%"
            params.extend([like, like, like, like, like, like, like])
        params.append(limit)

        rows = conn.execute(
            f"""
            SELECT
              id, fetched_at, profile_id, profile_name,
              provider, model, alias, source, auth_account, external_key,
              requests, success, failed,
              input_tokens, output_tokens, reasoning_tokens, total_tokens,
              avg_latency_ms, min_latency_ms, max_latency_ms
            FROM usage_records
            {where}
            ORDER BY fetched_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def clear_cache():
    conn = get_conn()
    try:
        conn.execute("DELETE FROM usage_records")
        conn.execute("DELETE FROM pull_snapshots")
        conn.execute("DELETE FROM pull_logs")
        conn.commit()
    finally:
        conn.close()


class RefreshScheduler(threading.Thread):
    daemon = True

    def run(self):
        while True:
            try:
                cfg = read_config()
                enabled = bool(to_int(cfg.get("auto_refresh_enabled", 0), 0))
                interval_sec = max(5, to_int(cfg.get("refresh_interval_sec", 60), 60))
                now = time.time()
                nxt = scheduler_state.get("next_run_at")
                if not enabled:
                    scheduler_state["next_run_at"] = None
                else:
                    if nxt is None:
                        scheduler_state["next_run_at"] = now + 1
                    elif now >= nxt:
                        perform_refresh("auto")
                        scheduler_state["next_run_at"] = time.time() + interval_sec
            except Exception as e:
                scheduler_state["last_ok"] = False
                scheduler_state["last_message"] = str(e)
            time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    server_version = "RouterStatsSQLite/2.0"

    def _json(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = to_int(self.headers.get("Content-Length", "0"), 0)
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return json.loads(body) if body else {}

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        u = parse.urlparse(self.path)
        if u.path.startswith("/api/"):
            self.handle_api_get(u)
            return
        self.serve_static(u.path)

    def do_POST(self):
        u = parse.urlparse(self.path)
        if not u.path.startswith("/api/"):
            self._json(404, {"ok": False, "message": "not found"})
            return
        self.handle_api_post(u)

    def handle_api_get(self, u):
        q = parse.parse_qs(u.query or "")
        if u.path == "/api/health":
            cfg = read_config()
            ap = get_active_profile()
            self._json(
                200,
                {
                    "ok": True,
                    "now": now_iso(),
                    "scheduler": scheduler_state,
                    "active_profile": {"id": ap["id"], "name": ap["name"]} if ap else None,
                    "config_brief": {
                        "refresh_interval_sec": cfg.get("refresh_interval_sec", 60),
                        "auto_refresh_enabled": bool(cfg.get("auto_refresh_enabled", 0)),
                        "lookback_hours": cfg.get("lookback_hours", 24),
                        "retention_days": cfg.get("retention_days", 30),
                    },
                },
            )
            return

        if u.path == "/api/config":
            cfg = read_config()
            ap = get_active_profile()
            self._json(200, {"ok": True, "config": cfg, "active_profile": {"id": ap["id"], "name": ap["name"]} if ap else None})
            return

        if u.path == "/api/profiles":
            cfg = read_config()
            self._json(200, {"ok": True, "active_profile_id": cfg.get("active_profile_id"), "profiles": list_profiles()})
            return

        if u.path == "/api/stats":
            cfg = read_config()
            hours = max(1, min(24 * 90, to_int((q.get("hours") or [str(cfg.get("lookback_hours", 24))])[0], 24)))
            keyword = (q.get("keyword") or [""])[0].strip()
            profile_id = to_int((q.get("profile_id") or [str(cfg.get("active_profile_id") or 0)])[0], 0)
            self._json(200, {"ok": True, "data": query_stats(hours, keyword, profile_id)})
            return

        if u.path == "/api/trend":
            cfg = read_config()
            hours = max(1, min(24 * 90, to_int((q.get("hours") or [str(cfg.get("lookback_hours", 24))])[0], 24)))
            limit = max(10, min(5000, to_int((q.get("limit") or ["200"])[0], 200)))
            profile_id = to_int((q.get("profile_id") or [str(cfg.get("active_profile_id") or 0)])[0], 0)
            self._json(200, {"ok": True, "data": query_trend(hours, limit, profile_id)})
            return

        if u.path == "/api/logs":
            cfg = read_config()
            limit = max(1, min(200, to_int((q.get("limit") or ["20"])[0], 20)))
            profile_id = to_int((q.get("profile_id") or [str(cfg.get("active_profile_id") or 0)])[0], 0)
            self._json(200, {"ok": True, "data": query_logs(limit, profile_id)})
            return

        if u.path == "/api/records":
            cfg = read_config()
            hours = max(1, min(24 * 90, to_int((q.get("hours") or [str(cfg.get("lookback_hours", 24))])[0], 24)))
            profile_id = to_int((q.get("profile_id") or [str(cfg.get("active_profile_id") or 0)])[0], 0)
            keyword = (q.get("keyword") or [""])[0].strip()
            limit = max(10, min(5000, to_int((q.get("limit") or ["300"])[0], 300)))
            self._json(200, {"ok": True, "data": query_records(hours, profile_id, keyword, limit)})
            return

        self._json(404, {"ok": False, "message": "not found"})

    def handle_api_post(self, u):
        if u.path == "/api/config":
            payload = self._read_json()
            cfg = write_config(payload)
            scheduler_state["next_run_at"] = None
            self._json(200, {"ok": True, "config": cfg})
            return

        if u.path == "/api/profiles/upsert":
            payload = self._read_json()
            pid = upsert_profile(payload)
            if payload.get("set_active"):
                set_active_profile(pid)
            self._json(200, {"ok": True, "profile_id": pid})
            return

        if u.path == "/api/profiles/select":
            payload = self._read_json()
            pid = set_active_profile(payload.get("id"))
            scheduler_state["next_run_at"] = None
            self._json(200, {"ok": True, "active_profile_id": pid})
            return

        if u.path == "/api/profiles/delete":
            payload = self._read_json()
            delete_profile(payload.get("id"))
            self._json(200, {"ok": True})
            return

        if u.path == "/api/refresh":
            result = perform_refresh("manual")
            self._json(200 if result.get("ok") else 500, result)
            return

        if u.path == "/api/cache/clear":
            clear_cache()
            self._json(200, {"ok": True, "message": "cache cleared"})
            return

        if u.path == "/api/cache/prune":
            payload = self._read_json()
            days = max(1, min(3650, to_int(payload.get("retention_days"), to_int(read_config().get("retention_days", 30), 30))))
            stats = cleanup_old_data(days)
            self._json(200, {"ok": True, "message": "pruned", "data": stats})
            return

        self._json(404, {"ok": False, "message": "not found"})

    def serve_static(self, path):
        req_path = "/" if path in ("", "/") else path
        if req_path == "/":
            target = WEB_ROOT / "index.html"
        else:
            safe = Path(req_path.lstrip("/"))
            target = (WEB_ROOT / safe).resolve()
            if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
                self.send_error(403)
                return
        if not target.exists() or not target.is_file():
            self.send_error(404)
            return

        ext = target.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # Avoid stale cached HTML/JS/CSS that may carry previous garbled text.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    init_db()
    scheduler = RefreshScheduler()
    scheduler.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Server: http://{HOST}:{PORT}")
    print(f"DB: {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
