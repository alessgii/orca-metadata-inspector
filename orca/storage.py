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
