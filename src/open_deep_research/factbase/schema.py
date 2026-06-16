"""Fact-base schema migration steps.

Exposes :data:`STEPS`, an ordered list of ``(version, sql)`` tuples consumed by
:func:`open_deep_research.factbase.migrations.apply`. Each step's SQL is split
on ``;`` and executed statement-by-statement inside a transaction, so every
statement is terminated by ``;`` and contains no embedded semicolons.
"""
from __future__ import annotations

_V1 = """
CREATE TABLE IF NOT EXISTS run_source (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    source_url TEXT,
    capture_status TEXT CHECK (capture_status IN ('raw_text','summarized','skipped')),
    text TEXT,
    content_hash TEXT,
    retrieved_at TEXT,
    soft_deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS entity_type (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE,
    profile_json TEXT
);

CREATE TABLE IF NOT EXISTS entity_instance (
    id INTEGER PRIMARY KEY,
    type_id INTEGER REFERENCES entity_type(id),
    canonical_key TEXT,
    name TEXT,
    aliases_json TEXT,
    UNIQUE (type_id, canonical_key)
);

CREATE TABLE IF NOT EXISTS unresolved_instance (
    id INTEGER PRIMARY KEY,
    type_id INTEGER,
    raw_name TEXT,
    run_id TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS property_def (
    id INTEGER PRIMARY KEY,
    type_id INTEGER REFERENCES entity_type(id),
    name TEXT,
    value_kind TEXT,
    identity_qualifiers_json TEXT,
    validation_json TEXT,
    trust_threshold REAL,
    UNIQUE (type_id, name)
);

CREATE TABLE IF NOT EXISTS source (
    id INTEGER PRIMARY KEY,
    url_or_domain TEXT,
    registry_version TEXT,
    tier TEXT,
    flags_json TEXT,
    soft_deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS fact (
    id INTEGER PRIMARY KEY,
    instance_id INTEGER,
    property_id INTEGER,
    tuple_key TEXT,
    qualifiers_json TEXT,
    as_of INTEGER,
    value TEXT,
    unit TEXT,
    source_id INTEGER,
    admission TEXT CHECK (admission IN ('provisional','trusted')),
    lifecycle TEXT CHECK (lifecycle IN ('current','stale','superseded')),
    confidence REAL,
    run_id TEXT,
    soft_deleted_at TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_fact_tuple_key_as_of ON fact (tuple_key, as_of);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER REFERENCES fact(id),
    quoted_span TEXT,
    run_source_id INTEGER REFERENCES run_source(id),
    doc_identity TEXT,
    retrieved_at TEXT
);

CREATE TABLE IF NOT EXISTS fact_revision (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER REFERENCES fact(id),
    change TEXT,
    cause TEXT,
    why TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS conflict (
    id INTEGER PRIMARY KEY,
    tuple_key TEXT,
    as_of INTEGER,
    status TEXT CHECK (status IN ('open','resolved')),
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS conflict_member (
    conflict_id INTEGER REFERENCES conflict(id),
    fact_id INTEGER REFERENCES fact(id)
);
"""

STEPS: list[tuple[int, str]] = [
    (1, _V1),
    (2, """
    ALTER TABLE run_source ADD COLUMN thread_id TEXT;
    CREATE INDEX IF NOT EXISTS ix_run_source_thread ON run_source(thread_id);
    ALTER TABLE research_runs ADD COLUMN status TEXT;
    ALTER TABLE research_runs ADD COLUMN coverage_incomplete INTEGER DEFAULT 0;
    ALTER TABLE research_runs ADD COLUMN last_heartbeat TEXT;
    """),
    (3, """
    ALTER TABLE fact ADD COLUMN instance_key TEXT;
    ALTER TABLE fact ADD COLUMN property_name TEXT;
    CREATE INDEX IF NOT EXISTS ix_fact_instance ON fact(instance_key);
    CREATE INDEX IF NOT EXISTS ix_fact_property ON fact(property_name);
    """),
    (4, """
    ALTER TABLE run_source ADD COLUMN reason TEXT;
    """),
    (5, """
    ALTER TABLE fact ADD COLUMN canonical_value TEXT;
    ALTER TABLE fact ADD COLUMN canonical_unit TEXT;
    CREATE INDEX IF NOT EXISTS ix_fact_canonical ON fact(tuple_key, as_of, canonical_value);
    """),
    (6, """
    ALTER TABLE research_runs ADD COLUMN profile_name TEXT;
    ALTER TABLE research_runs ADD COLUMN profile_version TEXT;
    ALTER TABLE research_runs ADD COLUMN profile_hash TEXT;
    """),
    (7, """
    CREATE TABLE IF NOT EXISTS batch_run (
        batch_id TEXT PRIMARY KEY,
        profile_name TEXT,
        profile_hash TEXT,
        list_spec TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS batch_item (
        batch_id TEXT,
        instance_key TEXT,
        country_name TEXT,
        status TEXT,
        run_id TEXT,
        error TEXT,
        updated_at TEXT,
        PRIMARY KEY (batch_id, instance_key)
    );
    """),
]
