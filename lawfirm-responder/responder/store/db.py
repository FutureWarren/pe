"""SQLite 存储：群档案、消息、判断日志（含沉默日志）、回复记录、提醒队列。

单文件本地存储，Phase 1 影子模式足够；Phase 3 扩容再评估 Postgres。[待定]
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from responder.models import Decision, GroupProfile, IncomingMessage, Reminder

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    client_status TEXT DEFAULT 'signed',
    case_type TEXT DEFAULT '',
    case_stage TEXT DEFAULT '',
    lawyer_name TEXT DEFAULT '',
    lawyer_userid TEXT DEFAULT '',
    backup_userid TEXT DEFAULT '',
    ai_enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS messages (
    msg_id TEXT PRIMARY KEY,
    group_id TEXT,
    sender_id TEXT,
    sender_is_staff INTEGER DEFAULT 0,
    content TEXT,
    msg_type TEXT DEFAULT 'text',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT,
    group_id TEXT,
    action TEXT,
    category TEXT,
    urgent INTEGER DEFAULT 0,
    should_speak INTEGER DEFAULT 0,
    reasons TEXT DEFAULT '[]',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT,
    group_id TEXT,
    text TEXT,
    mode TEXT DEFAULT 'shadow',
    compliance_passed INTEGER DEFAULT 1,
    feedback TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT,
    group_id TEXT,
    to_userid TEXT,
    urgent INTEGER DEFAULT 0,
    summary TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    created_at TEXT,
    escalated_at TEXT
);
"""


class Store:
    def __init__(self, path: str = "responder.db"):
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ groups
    def upsert_group(self, g: GroupProfile) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO groups VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(group_id) DO UPDATE SET
                   name=excluded.name, client_status=excluded.client_status,
                   case_type=excluded.case_type, case_stage=excluded.case_stage,
                   lawyer_name=excluded.lawyer_name, lawyer_userid=excluded.lawyer_userid,
                   backup_userid=excluded.backup_userid, ai_enabled=excluded.ai_enabled""",
                (
                    g.group_id, g.name, g.client_status.value, g.case_type, g.case_stage,
                    g.lawyer_name, g.lawyer_userid, g.backup_userid, int(g.ai_enabled),
                ),
            )

    def get_group(self, group_id: str) -> GroupProfile | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM groups WHERE group_id=?", (group_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["ai_enabled"] = bool(d["ai_enabled"])
        return GroupProfile(**d)

    def set_group_ai(self, group_id: str, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE groups SET ai_enabled=? WHERE group_id=?", (int(enabled), group_id)
            )

    def list_groups(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM groups").fetchall()]

    # ------------------------------------------------------------ messages
    def save_message(self, m: IncomingMessage) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?)",
                (
                    m.msg_id, m.group_id, m.sender_id, int(m.sender_is_staff),
                    m.content, m.msg_type, m.created_at.isoformat(),
                ),
            )

    def last_staff_reply_at(self, group_id: str) -> datetime | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) AS t FROM messages WHERE group_id=? AND sender_is_staff=1",
                (group_id,),
            ).fetchone()
        return datetime.fromisoformat(row["t"]) if row and row["t"] else None

    # ------------------------------------------------------------ decisions
    def save_decision(self, d: Decision) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO decisions (msg_id,group_id,action,category,urgent,"
                "should_speak,reasons,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    d.msg_id, d.group_id, d.action.value, d.category.value,
                    int(d.urgent), int(d.should_speak), json.dumps(d.reasons, ensure_ascii=False),
                    datetime.now().isoformat(),
                ),
            )

    def list_decisions(self, group_id: str | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT * FROM decisions"
        args: tuple = ()
        if group_id:
            q += " WHERE group_id=?"
            args = (group_id,)
        q += " ORDER BY id DESC LIMIT ?"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, args + (limit,)).fetchall()]

    # ------------------------------------------------------------ replies
    def save_reply(self, msg_id: str, group_id: str, text: str, mode: str, passed: bool) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO replies (msg_id,group_id,text,mode,compliance_passed,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (msg_id, group_id, text, mode, int(passed), datetime.now().isoformat()),
            )
            return cur.lastrowid

    def set_reply_feedback(self, reply_id: int, feedback: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE replies SET feedback=? WHERE id=?", (feedback, reply_id))

    def list_replies(self, group_id: str | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT * FROM replies"
        args: tuple = ()
        if group_id:
            q += " WHERE group_id=?"
            args = (group_id,)
        q += " ORDER BY id DESC LIMIT ?"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, args + (limit,)).fetchall()]

    # ------------------------------------------------------------ reminders
    def save_reminder(self, r: Reminder) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO reminders (msg_id,group_id,to_userid,urgent,summary,status,created_at)"
                " VALUES (?,?,?,?,?, 'pending', ?)",
                (r.msg_id, r.group_id, r.to_userid, int(r.urgent), r.summary,
                 datetime.now().isoformat()),
            )
            return cur.lastrowid

    def pending_reminders(self) -> list[dict]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM reminders WHERE status IN ('pending','sent','escalated')"
                    " ORDER BY urgent DESC, id ASC"
                ).fetchall()
            ]

    def set_reminder_status(self, reminder_id: int, status: str) -> None:
        with self._conn() as conn:
            field = ", escalated_at=?" if status == "escalated" else ""
            args = (
                (status, datetime.now().isoformat(), reminder_id)
                if status == "escalated"
                else (status, reminder_id)
            )
            conn.execute(f"UPDATE reminders SET status=?{field} WHERE id=?", args)
