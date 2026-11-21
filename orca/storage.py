import json
import sqlite3
from contextlib import contextmanager


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
            db.executescript("""
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
            """)

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
