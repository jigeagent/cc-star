"""Context graph schema — entities, relations, timeline with FTS5."""

from __future__ import annotations

from cc_star.cache.connection import CacheConnection


def ensure_graph_schema(conn_or_cache: CacheConnection) -> None:
    """Create graph tables and indexes if they don't exist."""
    conn = (
        conn_or_cache.conn
        if isinstance(conn_or_cache, CacheConnection)
        else conn_or_cache
    )

    conn.executescript("""
        -- Entity nodes
        CREATE TABLE IF NOT EXISTS entities (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            type        TEXT NOT NULL CHECK(type IN (
                            'project', 'file', 'decision', 'source',
                            'person', 'task', 'skill', 'other'
                        )),
            description TEXT DEFAULT '',
            metadata    TEXT DEFAULT '{}',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_entities_type
            ON entities(type);
        CREATE INDEX IF NOT EXISTS idx_entities_name
            ON entities(name);

        -- Relations between entities
        CREATE TABLE IF NOT EXISTS relations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            target_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            type        TEXT NOT NULL CHECK(type IN (
                            'references', 'depends_on', 'produces',
                            'decided_by', 'cites', 'related_to'
                        )),
            weight      REAL DEFAULT 1.0,
            metadata    TEXT DEFAULT '{}',
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_relations_source
            ON relations(source_id);
        CREATE INDEX IF NOT EXISTS idx_relations_target
            ON relations(target_id);
        CREATE INDEX IF NOT EXISTS idx_relations_type
            ON relations(type);

        -- Timeline events (time-ordered entity life cycle)
        CREATE TABLE IF NOT EXISTS timeline (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id   INTEGER REFERENCES entities(id) ON DELETE SET NULL,
            session_id  TEXT DEFAULT '',
            event_type  TEXT NOT NULL,
            payload     TEXT DEFAULT '{}',
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_timeline_entity
            ON timeline(entity_id);
        CREATE INDEX IF NOT EXISTS idx_timeline_session
            ON timeline(session_id);
        CREATE INDEX IF NOT EXISTS idx_timeline_created
            ON timeline(created_at);
        CREATE INDEX IF NOT EXISTS idx_timeline_event_type
            ON timeline(event_type);

        -- FTS5 index for entity name and description search
        CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts
            USING fts5(
                name,
                description,
                content='entities',
                content_rowid='rowid'
            );

        CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
            INSERT INTO entities_fts(rowid, name, description)
            VALUES (new.rowid, new.name, new.description);
        END;

        CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
            INSERT INTO entities_fts(entities_fts, rowid, name, description)
            VALUES ('delete', old.rowid, old.name, old.description);
        END;

        CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
            INSERT INTO entities_fts(entities_fts, rowid, name, description)
            VALUES ('delete', old.rowid, old.name, old.description);
            INSERT INTO entities_fts(rowid, name, description)
            VALUES (new.rowid, new.name, new.description);
        END;
    """)
    conn.commit()


def drop_graph_schema(conn_or_cache: CacheConnection) -> None:
    """Drop all graph tables (for testing)."""
    conn = (
        conn_or_cache.conn
        if isinstance(conn_or_cache, CacheConnection)
        else conn_or_cache
    )
    conn.executescript("""
        DROP TABLE IF EXISTS entities_fts;
        DROP TABLE IF EXISTS timeline;
        DROP TABLE IF EXISTS relations;
        DROP TABLE IF EXISTS entities;
    """)
    conn.commit()
