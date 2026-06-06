"""Migrate old TypeScript MemOS memos.db → Hermes Next cache.db.

Usage:
    hermes-next-migrate --old-db ~/.hermes/memos-plugin/data/memos.db
    hermes-next-migrate --old-db ~/.hermes/memos-plugin/data/memos.db --sync-ov
    hermes-next-migrate --old-db path/to/memos.db --new-db path/to/cache.db --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema


def detect_tables(db_path: str) -> list[str]:
    """List user tables in an SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%'"
        " AND name NOT LIKE '%_fts%'"
        " AND name NOT LIKE '%_config%'"
        " AND name NOT LIKE '%_content%'"
        " AND name NOT LIKE '%_data%'"
        " AND name NOT LIKE '%_docsize%'"
        " AND name NOT LIKE '%_idx%'"
        " ORDER BY name"
    ).fetchall()
    conn.close()
    return [r["name"] for r in rows]


def get_columns(db_path: str, table: str) -> list[dict]:
    """Get column info for a table via PRAGMA table_info."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(f"PRAGMA table_info({table!r})").fetchall()
    conn.close()
    return [
        {"cid": r[0], "name": r[1], "type": r[2], "notnull": r[3], "dflt": r[4], "pk": r[5]}
        for r in rows
    ]


def epoch_ms_to_iso(ms: int | None) -> str:
    """Convert epoch milliseconds to ISO8601 string."""
    if ms is None or ms == 0:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def read_blob_embedding(blob: bytes | None) -> list[float] | None:
    """Read embedding from blob — try JSON first, then raw float64 bytes."""
    if blob is None:
        return None
    try:
        return json.loads(blob.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        pass
    try:
        import struct

        n = len(blob) // 8
        if 0 < n <= 4096:
            return [struct.unpack("<d", blob[i * 8 : (i + 1) * 8])[0] for i in range(n)]
    except (struct.error, ValueError):
        pass
    return None


def migrate_traces(
    old_db: str,
    new_db_conn: sqlite3.Connection,
    dry_run: bool = False,
) -> dict:
    """Migrate traces from old memos.db into Hermes Next cache.db."""
    stats = {"old_rows": 0, "migrated": 0, "errors": 0}

    old_conn = sqlite3.connect(old_db)
    old_conn.row_factory = sqlite3.Row

    cols = {c["name"] for c in get_columns(old_db, "traces")}
    has_mapping = {
        k: k in cols
        for k in [
            "episode_id", "summary", "reflection", "tool_calls_json",
            "vec_summary", "vec_action", "agent_thinking", "error_signatures_json",
            "tags_json", "priority", "turn_id", "owner_agent_kind", "share_scope",
            "alpha", "r_human",
        ]
    }

    select_cols = ["id", "session_id", "user_text", "agent_text", "ts", "value"]
    extras_map = [
        ("episode_id", "episode_id"),
        ("summary", "summary"),
        ("reflection", "reflection"),
        ("tool_calls_json", "tool_calls"),
        ("agent_thinking", "agent_thinking"),
        ("error_signatures_json", "error_signatures"),
        ("priority", "priority"),
        ("alpha", "alpha"),
        ("r_human", "r_human"),
    ]
    for col, _ in extras_map:
        if has_mapping[col]:
            select_cols.append(col)
    for col in ["vec_summary", "vec_action", "tags_json", "turn_id"]:
        if has_mapping[col]:
            select_cols.append(col)
    if has_mapping["owner_agent_kind"]:
        select_cols.extend(["owner_agent_kind", "owner_profile_id", "owner_workspace_id"])
    if has_mapping["share_scope"]:
        select_cols.extend(["share_scope", "share_target", "shared_at"])

    query = f"SELECT {', '.join(select_cols)} FROM traces ORDER BY ts ASC"
    rows = old_conn.execute(query).fetchall()
    stats["old_rows"] = len(rows)

    if dry_run:
        old_conn.close()
        return stats

    insert_sql = (
        "INSERT OR REPLACE INTO traces "
        "(id, session_id, turn_index, user_content, assistant_content, "
        "embedding, reward, tags, metadata, created_at, synced) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    batch = []
    for row in rows:
        try:
            meta = {}
            for col, key in extras_map:
                if has_mapping[col] and row[col]:
                    if col in ("tool_calls_json", "error_signatures_json"):
                        try:
                            val = json.loads(row[col])
                            if val:
                                meta[key] = val
                        except (json.JSONDecodeError, TypeError):
                            pass
                    elif col in ("alpha", "r_human"):
                        if row[col] is not None and row[col] != 0:
                            meta[key] = row[col]
                    else:
                        meta[key] = row[col]
            if has_mapping["owner_agent_kind"]:
                meta["owner_agent_kind"] = row["owner_agent_kind"] or "unknown"
                if row.get("owner_profile_id") and row["owner_profile_id"] != "default":
                    meta["owner_profile_id"] = row["owner_profile_id"]
            if has_mapping["share_scope"] and row.get("share_scope") and row["share_scope"] != "private":
                meta["share_scope"] = row["share_scope"]

            # Embedding: prefer vec_summary, fallback to vec_action
            embedding = None
            if has_mapping["vec_summary"]:
                embedding = read_blob_embedding(row["vec_summary"])
            if embedding is None and has_mapping["vec_action"]:
                embedding = read_blob_embedding(row["vec_action"])

            # Tags
            tags = []
            if has_mapping["tags_json"] and row["tags_json"]:
                try:
                    tags = json.loads(row["tags_json"])
                except (json.JSONDecodeError, TypeError):
                    pass

            turn_idx = row["turn_id"] if has_mapping["turn_id"] and row["turn_id"] else 0

            batch.append((
                row["id"],
                row["session_id"] or "unknown",
                turn_idx,
                row["user_text"] or "",
                row["agent_text"] or "",
                json.dumps(embedding) if embedding else None,
                float(row["value"] or 0),
                json.dumps(tags, ensure_ascii=False),
                json.dumps(meta, ensure_ascii=False, default=str),
                epoch_ms_to_iso(row["ts"]),
                1,  # synced=1 for legacy data
            ))

            if len(batch) >= 500:
                new_db_conn.executemany(insert_sql, batch)
                new_db_conn.commit()
                stats["migrated"] += len(batch)
                batch = []

        except Exception as e:
            stats["errors"] += 1
            print(f"  [ERROR] trace {row['id']}: {e}", file=sys.stderr)

    if batch:
        new_db_conn.executemany(insert_sql, batch)
        new_db_conn.commit()
        stats["migrated"] += len(batch)

    old_conn.close()
    return stats


def migrate_policies(old_db: str, new_db_conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """Migrate policies from old memos.db into Hermes Next cache.db."""
    stats = {"old_rows": 0, "migrated": 0, "errors": 0}

    old_conn = sqlite3.connect(old_db)
    old_conn.row_factory = sqlite3.Row

    cols = {c["name"] for c in get_columns(old_db, "policies")}
    has_mapping = {
        k: k in cols
        for k in [
            "verification", "boundary", "source_trace_ids_json", "source_episodes_json",
            "gain", "support", "salience", "skill_eligible", "decision_guidance_json",
            "verifier_meta_json", "status", "experience_type", "evidence_polarity",
            "created_at", "updated_at", "owner_agent_kind", "share_scope", "vec", "confidence",
        ]
    }

    select_cols = ["id", "title", "trigger", "procedure"]
    for col in ["verification", "boundary", "source_trace_ids_json", "source_episodes_json",
                 "gain", "support", "salience", "skill_eligible", "decision_guidance_json",
                 "verifier_meta_json", "status", "experience_type", "evidence_polarity",
                 "created_at", "updated_at", "vec", "confidence"]:
        if has_mapping[col]:
            select_cols.append(col)
    if has_mapping["owner_agent_kind"]:
        select_cols.extend(["owner_agent_kind", "owner_profile_id", "owner_workspace_id"])
    if has_mapping["share_scope"]:
        select_cols.extend(["share_scope", "share_target", "shared_at"])

    query = f"SELECT {', '.join(select_cols)} FROM policies ORDER BY created_at ASC"
    rows = old_conn.execute(query).fetchall()
    stats["old_rows"] = len(rows)

    if dry_run:
        old_conn.close()
        return stats

    insert_sql = (
        "INSERT OR REPLACE INTO policies "
        "(id, name, description, trigger_pattern, action_template, "
        "embedding, confidence, activation_count, source_trace_ids, "
        "metadata, created_at, synced) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    for row in rows:
        try:
            desc_parts = []
            if row["trigger"]:
                desc_parts.append(f"Trigger: {row['trigger']}")
            if has_mapping["verification"] and row["verification"]:
                desc_parts.append(f"Verification: {row['verification']}")
            if has_mapping["boundary"] and row["boundary"]:
                desc_parts.append(f"Boundary: {row['boundary']}")
            description = "\n".join(desc_parts) if desc_parts else f"Policy: {row['title']}"

            embedding = read_blob_embedding(row["vec"]) if has_mapping["vec"] else None

            source_traces = []
            if has_mapping["source_trace_ids_json"] and row["source_trace_ids_json"]:
                try:
                    source_traces = json.loads(row["source_trace_ids_json"])
                except (json.JSONDecodeError, TypeError):
                    pass

            meta = {}
            for col, key in [
                ("source_episodes_json", "source_episodes"),
                ("decision_guidance_json", "decision_guidance"),
                ("verifier_meta_json", "verifier_meta"),
            ]:
                if has_mapping[col] and row[col] and row[col] not in ("[]", "null"):
                    try:
                        val = json.loads(row[col])
                        if val:
                            meta[key] = val
                    except (json.JSONDecodeError, TypeError):
                        pass
            for col, key in [("gain", "gain"), ("support", "support"), ("salience", "salience")]:
                if has_mapping[col] and row[col]:
                    meta[key] = row[col]
            if has_mapping["skill_eligible"]:
                meta["skill_eligible"] = bool(row["skill_eligible"])
            if has_mapping["status"] and row["status"] and row["status"] != "active":
                meta["status"] = row["status"]
            if has_mapping["experience_type"] and row["experience_type"] and row["experience_type"] != "success_pattern":
                meta["experience_type"] = row["experience_type"]
            if has_mapping["evidence_polarity"] and row["evidence_polarity"] and row["evidence_polarity"] != "positive":
                meta["evidence_polarity"] = row["evidence_polarity"]
            if has_mapping["updated_at"] and row["updated_at"]:
                meta["updated_at"] = epoch_ms_to_iso(row["updated_at"])
            if has_mapping["owner_agent_kind"]:
                meta["owner_agent_kind"] = row["owner_agent_kind"] or "unknown"
            if has_mapping["share_scope"] and row.get("share_scope") and row["share_scope"] != "private":
                meta["share_scope"] = row["share_scope"]

            confidence = float(row["confidence"] if has_mapping["confidence"] and row["confidence"] else 0.5)
            created_at = epoch_ms_to_iso(row["created_at"] if has_mapping["created_at"] and row["created_at"] else None)

            new_db_conn.execute(insert_sql, (
                row["id"],
                row["title"] or "unnamed-policy",
                description,
                row["trigger"] or "",
                row["procedure"] or "",
                json.dumps(embedding) if embedding else None,
                confidence,
                0,
                json.dumps(source_traces, ensure_ascii=False),
                json.dumps(meta, ensure_ascii=False, default=str),
                created_at,
                1,
            ))
            stats["migrated"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"  [ERROR] policy {row['id']}: {e}", file=sys.stderr)

    new_db_conn.commit()
    old_conn.close()
    return stats


def migrate_skills(old_db: str, new_db_conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """Migrate skills from old memos.db into Hermes Next cache.db."""
    stats = {"old_rows": 0, "migrated": 0, "errors": 0}

    old_conn = sqlite3.connect(old_db)
    old_conn.row_factory = sqlite3.Row

    cols = {c["name"] for c in get_columns(old_db, "skills")}
    has_mapping = {
        k: k in cols
        for k in [
            "id", "status", "procedure_json", "source_policies_json",
            "source_world_json", "evidence_anchors_json", "version", "eta",
            "support", "gain", "trials_attempted", "trials_passed",
            "created_at", "updated_at", "owner_agent_kind", "share_scope", "usage_count",
        ]
    }

    select_cols = ["name", "invocation_guide"]
    for col in ["id", "status", "procedure_json", "source_policies_json",
                 "source_world_json", "evidence_anchors_json", "version", "eta",
                 "support", "gain", "trials_attempted", "trials_passed",
                 "created_at", "updated_at", "usage_count"]:
        if has_mapping[col]:
            select_cols.append(col)
    if has_mapping["owner_agent_kind"]:
        select_cols.extend(["owner_agent_kind", "owner_profile_id", "owner_workspace_id"])
    if has_mapping["share_scope"]:
        select_cols.extend(["share_scope", "share_target", "shared_at"])

    query = f"SELECT {', '.join(select_cols)} FROM skills ORDER BY name ASC"
    rows = old_conn.execute(query).fetchall()
    stats["old_rows"] = len(rows)

    if dry_run:
        old_conn.close()
        return stats

    insert_sql = (
        "INSERT OR REPLACE INTO skills "
        "(name, description, usage_guide, source_policy_ids, "
        "version, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )

    for row in rows:
        try:
            guide = row["invocation_guide"] or ""
            first_line = guide.split("\n")[0].strip()
            description = first_line.lstrip("# ").strip() if first_line else f"Skill: {row['name']}"

            source_policies = []
            if has_mapping["source_policies_json"] and row["source_policies_json"] and row["source_policies_json"] != "[]":
                try:
                    source_policies = json.loads(row["source_policies_json"])
                except (json.JSONDecodeError, TypeError):
                    pass

            version = int(row["version"] if has_mapping["version"] and row["version"] else 1)

            meta = {}
            if has_mapping["id"] and row["id"]:
                meta["old_id"] = row["id"]
            if has_mapping["status"] and row["status"] and row["status"] != "active":
                meta["status"] = row["status"]
            for col, key in [("procedure_json", "procedure"), ("source_world_json", "source_world"),
                              ("evidence_anchors_json", "evidence_anchors")]:
                if has_mapping[col] and row[col] and row[col] not in ("[]", "null"):
                    try:
                        val = json.loads(row[col])
                        if val:
                            meta[key] = val
                    except (json.JSONDecodeError, TypeError):
                        pass
            for col, key in [("eta", "eta"), ("support", "support"), ("gain", "gain")]:
                if has_mapping[col] and row[col]:
                    meta[key] = row[col]
            if has_mapping["trials_attempted"]:
                meta["trials_attempted"] = int(row["trials_attempted"] or 0)
                if row.get("trials_passed"):
                    meta["trials_passed"] = int(row["trials_passed"] or 0)
            if has_mapping["updated_at"] and row["updated_at"]:
                meta["updated_at"] = epoch_ms_to_iso(row["updated_at"])
            if has_mapping["owner_agent_kind"] and row["owner_agent_kind"]:
                meta["owner_agent_kind"] = row["owner_agent_kind"]
            if has_mapping["share_scope"] and row.get("share_scope") and row["share_scope"] != "private":
                meta["share_scope"] = row["share_scope"]
            if has_mapping["usage_count"] and row["usage_count"]:
                meta["usage_count"] = int(row["usage_count"] or 0)

            created_at = epoch_ms_to_iso(row["created_at"] if has_mapping["created_at"] and row["created_at"] else None)

            new_db_conn.execute(insert_sql, (
                row["name"],
                description,
                guide,
                json.dumps(source_policies, ensure_ascii=False),
                version,
                json.dumps(meta, ensure_ascii=False, default=str),
                created_at,
            ))
            stats["migrated"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"  [ERROR] skill {row.get('name', '?'):}: {e}", file=sys.stderr)

    new_db_conn.commit()
    old_conn.close()
    return stats


def open_new_db(db_path: str) -> sqlite3.Connection:
    """Open Hermes Next cache.db and ensure schema exists."""
    cache = CacheConnection(db_path)
    ensure_schema(cache)
    return cache.conn


def print_summary(kind: str, stats: dict) -> None:
    """Print migration stats line."""
    total = stats["old_rows"]
    done = stats["migrated"]
    errs = stats["errors"]
    pct = f"({done / total * 100:.1f}%)" if total > 0 else ""
    print(f"  {kind:12s}: {done}/{total} rows migrated {pct}")
    if errs:
        print(f"               {errs} errors")


def main() -> None:
    """CLI entry point for hermes-next-migrate."""
    parser = argparse.ArgumentParser(
        description="Migrate old TypeScript MemOS memos.db → Hermes Next cache.db",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--old-db",
        default=str(Path.home() / ".hermes" / "memos-plugin" / "data" / "memos.db"),
        help="Path to old memos.db (default: ~/.hermes/memos-plugin/data/memos.db)",
    )
    parser.add_argument(
        "--new-db",
        default=str(Path.home() / ".hermes-next" / "cache.db"),
        help="Path to new Hermes Next cache.db (default: ~/.hermes-next/cache.db)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Count rows without writing")
    parser.add_argument("--sync-ov", action="store_true", help="Sync traces to OpenViking")
    parser.add_argument("--ov-url", default="http://localhost:1933", help="OpenViking server URL")
    parser.add_argument("--skip-traces", action="store_true", help="Skip traces migration")
    parser.add_argument("--skip-policies", action="store_true", help="Skip policies migration")
    parser.add_argument("--skip-skills", action="store_true", help="Skip skills migration")
    args = parser.parse_args()

    old_path = os.path.expanduser(args.old_db)
    new_path = os.path.expanduser(args.new_db)

    if not os.path.isfile(old_path):
        print(f"[ERROR] Old memos.db not found: {old_path}", file=sys.stderr)
        sys.exit(1)

    old_size_mb = os.path.getsize(old_path) / (1024 * 1024)
    print(f"Old database: {old_path} ({old_size_mb:.0f} MB)")
    print(f"New database: {new_path}")

    tables = detect_tables(old_path)
    print(f"\nTables detected: {', '.join(tables)}")

    os.makedirs(os.path.dirname(new_path), exist_ok=True)
    new_conn = open_new_db(new_path) if not args.dry_run else None

    # Setup OV client if --sync-ov
    ov_client = None
    if args.sync_ov and not args.dry_run:
        try:
            from cc_star.ov.client import OpenVikingClient
            ov_client = OpenVikingClient(base_url=args.ov_url)
            if ov_client.health():
                print(f"\nOpenViking connected: {args.ov_url}")
            else:
                print(f"\n[WARN] OpenViking unavailable, skipping sync", file=sys.stderr)
                ov_client = None
        except Exception as e:
            print(f"\n[WARN] OpenViking connection failed: {e}", file=sys.stderr)

    total_start = time.time()

    if not args.skip_traces:
        print("\n--- Migrating Traces ---")
        t0 = time.time()
        stats = migrate_traces(old_path, new_conn, args.dry_run)
        print_summary("traces", stats)
        print(f"  Time: {time.time() - t0:.1f}s")
        if ov_client and not args.dry_run:
            _sync_traces_to_ov(new_conn, ov_client)

    if not args.skip_policies:
        print("\n--- Migrating Policies ---")
        t0 = time.time()
        stats = migrate_policies(old_path, new_conn, args.dry_run)
        print_summary("policies", stats)
        print(f"  Time: {time.time() - t0:.1f}s")

    if not args.skip_skills:
        print("\n--- Migrating Skills ---")
        t0 = time.time()
        stats = migrate_skills(old_path, new_conn, args.dry_run)
        print_summary("skills", stats)
        print(f"  Time: {time.time() - t0:.1f}s")

    total_time = time.time() - total_start

    if args.dry_run:
        print(f"\n[Dry-run] Total {total_time:.1f}s. Run without --dry-run to execute.")
    else:
        new_conn.close()
        new_size_mb = os.path.getsize(new_path) / (1024 * 1024)
        print(f"\nMigration complete! Total {total_time:.1f}s")
        print(f"New database size: {new_size_mb:.1f} MB")


def _sync_traces_to_ov(new_db_conn: sqlite3.Connection, ov_client) -> None:
    """Push unsynced traces to OpenViking content store."""
    unsynced = new_db_conn.execute(
        "SELECT id, user_content, assistant_content, tags, metadata, created_at"
        " FROM traces WHERE synced = 0 LIMIT 100"
    ).fetchall()
    if unsynced:
        print(f"  Syncing {len(unsynced)} traces to OpenViking...")
        for row in unsynced:
            try:
                uri = f"viking://resources/hermes/memos/traces/{row['id']}.json"
                content = {
                    "id": row["id"],
                    "user_content": row["user_content"],
                    "assistant_content": row["assistant_content"],
                    "tags": json.loads(row["tags"]) if row["tags"] else [],
                    "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
                    "created_at": row["created_at"],
                }
                ov_client.content_write(uri, content)
                time.sleep(0.01)
            except Exception as e:
                print(f"  [WARN] failed to sync trace {row['id']}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
