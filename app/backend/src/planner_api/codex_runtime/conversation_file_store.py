"""Durable conversation file requests; preserves the existing database format."""

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class ConversationFileStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / "projects.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT, project_id TEXT, created REAL, data TEXT)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS records_project ON records(project_id, kind)")
        self.db.commit()

    def create(self, kind, project_id=None, **data):
        item = {"id": uuid.uuid4().hex, "created": time.time(), **data}
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO records VALUES (?,?,?,?,?)",
                (
                    item["id"],
                    kind,
                    project_id,
                    item["created"],
                    json.dumps(item, ensure_ascii=False),
                ),
            )
        return item

    def get(self, identity, kind=None, project_id=None):
        with self.lock:
            row = self.db.execute("SELECT * FROM records WHERE id=?", (identity,)).fetchone()
            if (
                row is None
                or (kind and row["kind"] != kind)
                or (project_id and row["project_id"] != project_id)
            ):
                raise ValueError("未找到当前对话的文件记录。")
            return json.loads(row["data"])

    def list(self, kind, project_id=None):
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM records WHERE kind=?"
                + (" AND project_id=?" if project_id else "")
                + " ORDER BY created",
                (kind, project_id) if project_id else (kind,),
            ).fetchall()
            return [json.loads(row["data"]) for row in rows]

    def update(self, identity, **changes):
        with self.lock, self.db:
            item = {**self.get(identity), **changes}
            self.db.execute(
                "UPDATE records SET data=? WHERE id=?",
                (json.dumps(item, ensure_ascii=False), identity),
            )
        return item
