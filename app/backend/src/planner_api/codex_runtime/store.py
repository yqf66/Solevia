from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ACTIVE = ('queued', 'running', 'waiting')
TERMINAL = ('succeeded', 'failed', 'interrupted')


class ChatStore:
    """Serialize state changes and event cursors in the same SQLite transaction."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock, self.db:
            self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, thread_id TEXT, title TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, client_request_id TEXT NOT NULL,
                turn_id TEXT, status TEXT NOT NULL, created_at REAL NOT NULL,
                completed_at REAL, error TEXT, assistant_message_id TEXT,
                UNIQUE(session_id, client_request_id));
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, type TEXT NOT NULL,
                payload TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, method TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS events_run_id_idx ON events(run_id, id);
            """)
            columns = {row['name'] for row in self.db.execute('PRAGMA table_info(runs)')}
            if 'assistant_message_id' not in columns:
                self.db.execute('ALTER TABLE runs ADD COLUMN assistant_message_id TEXT')
            for row in self.db.execute("SELECT id FROM runs WHERE status IN ('queued','running','waiting')").fetchall():
                self.finish(row['id'], 'interrupted', '本地服务已重新启动；保留已收到的回答，请手动继续。')
            self.db.execute("UPDATE requests SET status='expired' WHERE status='pending'")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def create_session(self) -> dict[str, Any]:
        sid, now = str(uuid.uuid4()), time.time()
        with self.lock, self.db:
            self.db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', (sid, None, '新的 Solevia 对话', now, now))
        return self.session(sid)

    def session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            if not row:
                raise KeyError('没有找到这段对话。')
            return dict(row)

    def sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM sessions ORDER BY updated_at DESC')]

    def set_thread(self, session_id: str, thread_id: str) -> None:
        with self.lock, self.db:
            self.db.execute('UPDATE sessions SET thread_id=? WHERE id=?', (thread_id, session_id))

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise KeyError('没有找到这次回答。')
            return dict(row)

    def create_run(self, session_id: str, content: str, client_request_id: str) -> tuple[dict[str, Any], bool]:
        with self.lock, self.db:
            self.session(session_id)
            prior = self.db.execute('SELECT * FROM runs WHERE session_id=? AND client_request_id=?', (session_id, client_request_id)).fetchone()
            if prior:
                return dict(prior), True
            if self.db.execute("SELECT 1 FROM runs WHERE session_id=? AND status IN ('queued','running','waiting')", (session_id,)).fetchone():
                raise ValueError('该对话仍有一个回答正在进行，请等待或先停止它。')
            now = time.time()
            run_id, user_id, assistant_id = (str(uuid.uuid4()) for _ in range(3))
            self.db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)', (run_id, session_id, client_request_id, None, 'queued', now, None, None, assistant_id))
            self.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', (user_id, session_id, 'user', content, 'succeeded', now))
            self.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', (assistant_id, session_id, 'assistant', '', 'streaming', now))
            title = content.replace('\n', ' ')[:34]
            self.db.execute("UPDATE sessions SET updated_at=?, title=CASE WHEN title='新的 Solevia 对话' THEN ? ELSE title END WHERE id=?", (now, title, session_id))
            return self.get_run(run_id), False

    def _event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        cursor = self.db.execute('INSERT INTO events(run_id,type,payload,created_at) VALUES(?,?,?,?)', (run_id, event_type, json.dumps(payload, ensure_ascii=False), time.time()))
        return int(cursor.lastrowid)

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        with self.lock, self.db:
            return self._event(run_id, event_type, payload)

    def answer(self, run_id: str, content: str, *, delta: str | None = None) -> None:
        with self.lock, self.db:
            run = self.get_run(run_id)
            if run['status'] in TERMINAL:
                return
            self.db.execute('UPDATE messages SET content=? WHERE id=?', (content, run['assistant_message_id']))
            payload = {'message_id': run['assistant_message_id'], 'delta': delta} if delta is not None else {'message_id': run['assistant_message_id'], 'content': content}
            self._event(run_id, 'assistant_delta' if delta is not None else 'assistant_content', payload)

    def update_run(self, run_id: str, **fields: Any) -> None:
        updates = [(key, value) for key, value in fields.items() if key in {'turn_id', 'status', 'error'}]
        if not updates:
            return
        with self.lock, self.db:
            if self.get_run(run_id)['status'] not in TERMINAL:
                self.db.execute(f"UPDATE runs SET {', '.join(f'{key}=?' for key, _ in updates)} WHERE id=?", [value for _, value in updates] + [run_id])

    def finish(self, run_id: str, status: str, error: str | None = None) -> bool:
        if status not in TERMINAL:
            raise ValueError('Invalid terminal state')
        with self.lock, self.db:
            run = self.get_run(run_id)
            if run['status'] in TERMINAL:
                return False
            now = time.time()
            self.db.execute('UPDATE runs SET status=?, completed_at=?, error=? WHERE id=?', (status, now, error, run_id))
            self.db.execute('UPDATE messages SET status=? WHERE id=?', (status, run['assistant_message_id']))
            self.db.execute("UPDATE requests SET status='expired' WHERE run_id=? AND status='pending'", (run_id,))
            self._event(run_id, 'run_completed', {'status': status, 'error': error, 'total_seconds': round(now - run['created_at'], 3)})
            return True

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute('SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id', (run_id, after)).fetchall()
            return [{'id': row['id'], 'type': row['type'], 'payload': json.loads(row['payload']), 'created_at': row['created_at']} for row in rows]

    def request(self, run_id: str, method: str, payload: dict[str, Any]) -> str:
        request_id = str(uuid.uuid4())
        with self.lock, self.db:
            self.db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?)', (request_id, run_id, method, json.dumps(payload, ensure_ascii=False), 'pending', time.time()))
            self.db.execute("UPDATE runs SET status='waiting' WHERE id=?", (run_id,))
            self._event(run_id, 'request', {'request_id': request_id, 'method': method, 'params': payload})
        return request_id

    def pending_request(self, request_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM requests WHERE id=? AND status='pending'", (request_id,)).fetchone()
            return dict(row) if row else None

    def complete_request(self, request_id: str) -> None:
        with self.lock, self.db:
            row = self.pending_request(request_id)
            if row:
                self.db.execute("UPDATE requests SET status='answered' WHERE id=?", (request_id,))
                self.db.execute("UPDATE runs SET status='running' WHERE id=? AND status='waiting'", (row['run_id'],))
                self._event(row['run_id'], 'request_answered', {'request_id': request_id})

    def snapshot(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.session(session_id)
            messages = [dict(row) for row in self.db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY created_at, rowid', (session_id,))]
            row = self.db.execute('SELECT * FROM runs WHERE session_id=? ORDER BY created_at DESC LIMIT 1', (session_id,)).fetchone()
            last_run = dict(row) if row else None
            cursor, requests = 0, []
            if row:
                cursor = self.db.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE run_id=?', (row['id'],)).fetchone()[0]
                requests = [{'request_id': req['id'], 'method': req['method'], 'params': json.loads(req['payload'])} for req in self.db.execute("SELECT * FROM requests WHERE run_id=? AND status='pending'", (row['id'],))]
            return {'session': session, 'messages': messages, 'active_run': last_run if last_run and last_run['status'] in ACTIVE else None, 'last_run': last_run, 'last_event_id': cursor, 'requests': requests}
