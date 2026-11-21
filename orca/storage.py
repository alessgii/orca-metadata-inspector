import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "base_history_and_hashes",
        """
        CREATE TABLE IF NOT EXISTS analysis (
            id TEXT PRIMARY KEY,
            created TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS document (
            id TEXT PRIMARY KEY,
            analysis_id TEXT NOT NULL REFERENCES analysis(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS document_analysis ON document(analysis_id, position);
        CREATE TABLE IF NOT EXISTS hash_record (
            id TEXT PRIMARY KEY,
            created TEXT NOT NULL,
            name TEXT NOT NULL,
            size INTEGER NOT NULL,
            algorithm TEXT NOT NULL,
            hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS hash_record_created ON hash_record(created DESC);
        CREATE INDEX IF NOT EXISTS hash_record_algorithm ON hash_record(algorithm);
        """,
    ),
    (
        2,
        "investigations_module",
        """
        CREATE TABLE IF NOT EXISTS investigation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            responsible TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'document_analysis'
                CHECK (kind IN ('document_analysis', 'integrity_verification', 'exploratory_forensic')),
            classification TEXT NOT NULL DEFAULT 'private'
                CHECK (classification IN ('private', 'confidential')),
            status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'paused', 'closed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT,
            origin_uuid TEXT,
            origin_note TEXT NOT NULL DEFAULT '',
            schema_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS investigation_status ON investigation(status);
        CREATE INDEX IF NOT EXISTS investigation_created ON investigation(created_at DESC);
        CREATE INDEX IF NOT EXISTS investigation_updated ON investigation(updated_at DESC);

        CREATE TABLE IF NOT EXISTS investigation_file (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            investigation_uuid TEXT NOT NULL REFERENCES investigation(uuid) ON DELETE CASCADE,
            name TEXT NOT NULL,
            relative_path TEXT,
            extension TEXT NOT NULL DEFAULT '',
            format TEXT,
            size INTEGER NOT NULL CHECK (size >= 0),
            sha256 TEXT,
            status TEXT NOT NULL CHECK (status IN ('ok', 'empty', 'unsupported', 'error')),
            duplicate_of_uuid TEXT,
            manual_order INTEGER NOT NULL DEFAULT 0,
            internal_created TEXT,
            internal_modified TEXT,
            added_at TEXT NOT NULL,
            removed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS inv_file_investigation ON investigation_file(investigation_uuid, added_at);
        CREATE INDEX IF NOT EXISTS inv_file_sha256 ON investigation_file(investigation_uuid, sha256);

        CREATE TABLE IF NOT EXISTS investigation_analysis (
            file_uuid TEXT PRIMARY KEY REFERENCES investigation_file(uuid) ON DELETE CASCADE,
            engine_name TEXT,
            engine_version TEXT,
            analyzer_version TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            normalized_json TEXT NOT NULL,
            findings_json TEXT NOT NULL,
            errors_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            structure_json TEXT,
            analyzed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS investigation_comparison (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            investigation_uuid TEXT NOT NULL REFERENCES investigation(uuid) ON DELETE CASCADE,
            file_a_uuid TEXT NOT NULL REFERENCES investigation_file(uuid) ON DELETE CASCADE,
            file_b_uuid TEXT NOT NULL REFERENCES investigation_file(uuid) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            algorithms_json TEXT NOT NULL,
            analyzer_version TEXT NOT NULL,
            identical_bytes INTEGER,
            similarity_overall REAL,
            similarity_available INTEGER NOT NULL DEFAULT 0,
            result_json TEXT NOT NULL,
            limitations_json TEXT NOT NULL,
            CHECK (file_a_uuid <> file_b_uuid),
            UNIQUE (investigation_uuid, file_a_uuid, file_b_uuid)
        );
        CREATE INDEX IF NOT EXISTS inv_comp_investigation ON investigation_comparison(investigation_uuid, created_at DESC);

        CREATE TABLE IF NOT EXISTS investigation_relation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            investigation_uuid TEXT NOT NULL REFERENCES investigation(uuid) ON DELETE CASCADE,
            source_file_uuid TEXT NOT NULL REFERENCES investigation_file(uuid) ON DELETE CASCADE,
            target_file_uuid TEXT NOT NULL REFERENCES investigation_file(uuid) ON DELETE CASCADE,
            relation_type TEXT NOT NULL CHECK (relation_type IN (
                'identical_hash', 'equivalent_content', 'structural_similarity',
                'shared_media', 'manual_confirmed', 'manual_hypothesis'
            )),
            origin TEXT NOT NULL CHECK (origin IN ('automatic', 'manual')),
            confidence_label TEXT NOT NULL CHECK (confidence_label IN ('confirmada', 'sugerida', 'hipotesis')),
            label TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (source_file_uuid <> target_file_uuid)
        );
        CREATE INDEX IF NOT EXISTS inv_rel_investigation ON investigation_relation(investigation_uuid);

        CREATE TABLE IF NOT EXISTS investigation_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            investigation_uuid TEXT NOT NULL REFERENCES investigation(uuid) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            description TEXT NOT NULL,
            related_uuids_json TEXT NOT NULL DEFAULT '[]',
            details_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS inv_event_investigation ON investigation_event(investigation_uuid, id ASC);

        CREATE TABLE IF NOT EXISTS investigation_note (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            investigation_uuid TEXT NOT NULL REFERENCES investigation(uuid) ON DELETE CASCADE,
            target_type TEXT NOT NULL CHECK (target_type IN ('investigation', 'file', 'comparison', 'event', 'relation')),
            target_uuid TEXT,
            author TEXT NOT NULL,
            text TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS inv_note_investigation ON investigation_note(investigation_uuid, created_at DESC);

        CREATE TABLE IF NOT EXISTS investigation_note_version (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_uuid TEXT NOT NULL REFERENCES investigation_note(uuid) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            author TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (note_uuid, version)
        );

        CREATE TABLE IF NOT EXISTS investigation_closure (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            investigation_uuid TEXT NOT NULL REFERENCES investigation(uuid) ON DELETE CASCADE,
            action TEXT NOT NULL CHECK (action IN ('closed', 'reopened')),
            actor TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            conclusion TEXT NOT NULL DEFAULT '',
            final_notes TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS inv_closure_investigation ON investigation_closure(investigation_uuid, id DESC);
        """,
    ),
]


class History:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA secure_delete = ON")
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        with self.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
            """)
            applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations").fetchall()}
            for version, name, ddl in MIGRATIONS:
                if version not in applied:
                    for statement in ddl.split(";"):
                        stmt = statement.strip()
                        if stmt:
                            db.execute(stmt)
                    db.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, _utc_now()),
                    )

    def save(self, session, files):
        with self.connection() as db:
            db.execute("INSERT INTO analysis VALUES (?, ?, ?)",
                       (session["id"], session["created"], json.dumps(session, ensure_ascii=False)))
            db.executemany("INSERT INTO document VALUES (?, ?, ?, ?)", (
                (file["id"], session["id"], index, json.dumps(file, ensure_ascii=False))
                for index, file in enumerate(files)
            ))

    def list(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in
                    db.execute("SELECT payload FROM analysis ORDER BY created DESC")]

    def session(self, session_id):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM analysis WHERE id = ?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def files(self, session_id, file_id=None):
        query = "SELECT payload FROM document WHERE analysis_id = ?"
        params = (session_id,)
        if file_id:
            query += " AND id = ?"
            params += (file_id,)
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute(query + " ORDER BY position", params)]

    def delete(self, session_id=None):
        with self.connection() as db:
            if session_id:
                db.execute("DELETE FROM analysis WHERE id = ?", (session_id,))
            else:
                db.execute("DELETE FROM analysis")

    def save_hash(self, record: dict) -> dict:
        with self.connection() as db:
            db.execute(
                "INSERT INTO hash_record (id, created, name, size, algorithm, hash) VALUES (?, ?, ?, ?, ?, ?)",
                (record["id"], record["created"], record["name"], record["size"],
                 record["algorithm"], record["hash"]),
            )
        return record

    def list_hashes(self, query: str | None = None, algorithm: str | None = None) -> list[dict]:
        clauses = []
        params = []
        if algorithm:
            clauses.append("algorithm = ?")
            params.append(algorithm)
        if query:
            clauses.append("LOWER(name) LIKE ? ESCAPE '\\'")
            escaped = query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT id, created, name, size, algorithm, hash FROM hash_record{where} ORDER BY created DESC, rowid DESC"
        with self.connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [
            {"id": row[0], "created": row[1], "name": row[2], "size": row[3],
             "algorithm": row[4], "hash": row[5]}
            for row in rows
        ]

    def get_hash(self, record_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT id, created, name, size, algorithm, hash FROM hash_record WHERE id = ?",
                (record_id,),
            ).fetchone()
        if not row:
            return None
        return {"id": row[0], "created": row[1], "name": row[2], "size": row[3],
                "algorithm": row[4], "hash": row[5]}

    def delete_hash(self, record_id: str | None = None) -> bool:
        with self.connection() as db:
            if record_id:
                cursor = db.execute("DELETE FROM hash_record WHERE id = ?", (record_id,))
                return cursor.rowcount > 0
            db.execute("DELETE FROM hash_record")
            return True
