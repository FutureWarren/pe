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
    ai_enabled INTEGER DEFAULT 1,
    robot_webhook TEXT DEFAULT '',
    kf_open_kfid TEXT DEFAULT '',
    kf_external_userid TEXT DEFAULT ''
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
    category TEXT DEFAULT '',
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
    question TEXT DEFAULT '',
    ai_reply TEXT DEFAULT '',
    created_at TEXT,
    escalated_at TEXT
);
CREATE TABLE IF NOT EXISTS pending_checks (
    msg_id TEXT PRIMARY KEY,
    group_id TEXT,
    due_at TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT UNIQUE,
    intent TEXT DEFAULT 'cold',
    contact TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    case_type TEXT DEFAULT '',
    key_facts TEXT DEFAULT '[]',
    urgency TEXT DEFAULT 'low',
    suggested_action TEXT DEFAULT '',
    opening_line TEXT DEFAULT '',
    signals TEXT DEFAULT '[]',
    status TEXT DEFAULT 'new',
    notified_at TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS kf_cursors (
    open_kfid TEXT PRIMARY KEY,
    cursor TEXT DEFAULT '',
    updated_at TEXT
);
"""

# 旧库平滑升级：新增列在此登记，启动时按需 ALTER（SQLite 无 IF NOT EXISTS 列语法）
_ADDED_COLUMNS = {
    "groups": {
        "robot_webhook": "TEXT DEFAULT ''",
        "kf_open_kfid": "TEXT DEFAULT ''",
        "kf_external_userid": "TEXT DEFAULT ''",
    },
    "replies": {"category": "TEXT DEFAULT ''"},
    # 待办卡片要把「客户问的什么」当主角展示，不能让控制台去解析摘要文本
    "reminders": {"question": "TEXT DEFAULT ''", "ai_reply": "TEXT DEFAULT ''"},
}


class Store:
    def __init__(self, path: str = "responder.db"):
        self.path = path
        with self._conn() as conn:
            # WAL：API 线程与后台工作线程并发读写不互斥（对文件持久生效）
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn) -> None:
        """为已存在的旧库补齐新增列（幂等，升级部署不丢数据）。"""
        for table, columns in _ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ groups
    def upsert_group(self, g: GroupProfile) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO groups (group_id,name,client_status,case_type,case_stage,
                   lawyer_name,lawyer_userid,backup_userid,ai_enabled,robot_webhook,
                   kf_open_kfid,kf_external_userid)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(group_id) DO UPDATE SET
                   name=excluded.name, client_status=excluded.client_status,
                   case_type=excluded.case_type, case_stage=excluded.case_stage,
                   lawyer_name=excluded.lawyer_name, lawyer_userid=excluded.lawyer_userid,
                   backup_userid=excluded.backup_userid, ai_enabled=excluded.ai_enabled,
                   robot_webhook=excluded.robot_webhook,
                   kf_open_kfid=excluded.kf_open_kfid,
                   kf_external_userid=excluded.kf_external_userid""",
                (
                    g.group_id, g.name, g.client_status.value, g.case_type, g.case_stage,
                    g.lawyer_name, g.lawyer_userid, g.backup_userid, int(g.ai_enabled),
                    g.robot_webhook, g.kf_open_kfid, g.kf_external_userid,
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

    def delete_group(self, group_id: str) -> None:
        """仅删档案；该群的消息/判断/回复留痕保持不动。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM groups WHERE group_id=?", (group_id,))

    def set_group_ai(self, group_id: str, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE groups SET ai_enabled=? WHERE group_id=?", (int(enabled), group_id)
            )

    def list_groups(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM groups").fetchall()]

    # ------------------------------------------------------------ messages
    def save_message(self, m: IncomingMessage) -> bool:
        """返回是否新消息；False = msg_id 已存在（企微超时重发的重复回调）。"""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?)",
                (
                    m.msg_id, m.group_id, m.sender_id, int(m.sender_is_staff),
                    m.content, m.msg_type, m.created_at.isoformat(),
                ),
            )
            return cur.rowcount > 0

    def get_message(self, msg_id: str) -> IncomingMessage | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE msg_id=?", (msg_id,)
            ).fetchone()
        if not row:
            return None
        return IncomingMessage(
            msg_id=row["msg_id"], group_id=row["group_id"], sender_id=row["sender_id"],
            sender_is_staff=bool(row["sender_is_staff"]), content=row["content"],
            msg_type=row["msg_type"], created_at=datetime.fromisoformat(row["created_at"]),
        )

    def recent_messages(self, group_id: str, limit: int = 10) -> list[dict]:
        """最近 N 条群消息，按时间正序（注入 LLM 上下文用）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT sender_id, sender_is_staff, content, created_at FROM messages"
                " WHERE group_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def idle_conversations(self, since: datetime, until: datetime) -> list[str]:
        """最后一条消息落在 [since, until] 内的会话——即刚安静下来的对话。

        用于「聊完了但没留电话」的咨询补一份线索简报归档（冷线索也有跟进价值）。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT group_id, MAX(created_at) AS last_at FROM messages"
                " GROUP BY group_id HAVING last_at BETWEEN ? AND ?",
                (since.isoformat(), until.isoformat()),
            ).fetchall()
        return [r["group_id"] for r in rows]

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
    def save_reply(
        self, msg_id: str, group_id: str, text: str, mode: str, passed: bool,
        category: str = "",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO replies (msg_id,group_id,text,mode,category,"
                "compliance_passed,created_at) VALUES (?,?,?,?,?,?,?)",
                (msg_id, group_id, text, mode, category, int(passed),
                 datetime.now().isoformat()),
            )
            return cur.lastrowid

    def count_recent_live(self, group_id: str, category: str, since_seconds: int) -> int:
        """时间窗内该群同一问题类别已实际发出的回复数（追问去重/二次安抚用）。"""
        cutoff = datetime.fromtimestamp(
            datetime.now().timestamp() - since_seconds
        ).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM replies WHERE group_id=? AND category=?"
                " AND mode='live' AND created_at>=?",
                (group_id, category, cutoff),
            ).fetchone()
            return row["n"]

    def set_reply_feedback(self, reply_id: int, feedback: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE replies SET feedback=? WHERE id=?", (feedback, reply_id))

    def list_replies(self, group_id: str | None = None, limit: int = 200) -> list[dict]:
        # 带上客户原话：复核一句回复是否得当，前提是能看到它在回什么
        q = (
            "SELECT r.*, m.content AS question FROM replies r"
            " LEFT JOIN messages m ON m.msg_id = r.msg_id"
        )
        args: tuple = ()
        if group_id:
            q += " WHERE r.group_id=?"
            args = (group_id,)
        q += " ORDER BY r.id DESC LIMIT ?"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, args + (limit,)).fetchall()]

    # ------------------------------------------------------------ 线索
    def upsert_lead(self, group_id: str, fields: dict) -> None:
        """一个会话一条线索：反复更新而非追加，避免同一客户刷屏。"""
        now = datetime.now().isoformat()
        cols = ["intent", "contact", "summary", "case_type", "key_facts", "urgency",
                "suggested_action", "opening_line", "signals"]
        vals = [fields.get(c, "") for c in cols]
        with self._conn() as conn:
            conn.execute(
                f"""INSERT INTO leads (group_id,{','.join(cols)},created_at,updated_at)
                    VALUES ({','.join('?' * (len(cols) + 3))})
                    ON CONFLICT(group_id) DO UPDATE SET
                    {','.join(f'{c}=excluded.{c}' for c in cols)},
                    updated_at=excluded.updated_at""",
                (group_id, *vals, now, now),
            )

    def get_lead(self, group_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM leads WHERE group_id=?", (group_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_leads(self, status: str | None = None, limit: int = 200) -> list[dict]:
        """按意向热度、时间倒序——律师先看最该打电话的那个。"""
        q = "SELECT * FROM leads"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += (
            " ORDER BY CASE intent WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 ELSE 2 END,"
            " updated_at DESC LIMIT ?"
        )
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, args + (limit,)).fetchall()]

    def set_lead_status(self, lead_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE leads SET status=?, updated_at=? WHERE id=?",
                (status, datetime.now().isoformat(), lead_id),
            )

    def mark_lead_notified(self, group_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE leads SET notified_at=? WHERE group_id=?",
                (datetime.now().isoformat(), group_id),
            )

    # ------------------------------------------------------------ 微信客服游标
    def get_kf_cursor(self, open_kfid: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cursor FROM kf_cursors WHERE open_kfid=?", (open_kfid,)
            ).fetchone()
        return row["cursor"] if row else ""

    def set_kf_cursor(self, open_kfid: str, cursor: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO kf_cursors (open_kfid,cursor,updated_at) VALUES (?,?,?)"
                " ON CONFLICT(open_kfid) DO UPDATE SET"
                " cursor=excluded.cursor, updated_at=excluded.updated_at",
                (open_kfid, cursor, datetime.now().isoformat()),
            )

    # ------------------------------------------------------------ pending checks
    def add_pending_check(self, msg_id: str, group_id: str, due_at: datetime) -> None:
        """登记补位等待到点复评任务（重入以最新 due 为准）。"""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending_checks (msg_id,group_id,due_at,created_at)"
                " VALUES (?,?,?,?)",
                (msg_id, group_id, due_at.isoformat(), datetime.now().isoformat()),
            )

    def due_pending_checks(self, now: datetime) -> list[dict]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM pending_checks WHERE due_at<=? ORDER BY due_at",
                    (now.isoformat(),),
                ).fetchall()
            ]

    def delete_pending_check(self, msg_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM pending_checks WHERE msg_id=?", (msg_id,))

    # ------------------------------------------------------------ reminders
    def has_reminder(self, msg_id: str) -> bool:
        """该消息是否已提醒过（复评二次处理同一条消息时不再重复打扰律师）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM reminders WHERE msg_id=? LIMIT 1", (msg_id,)
            ).fetchone()
        return row is not None

    def save_reminder(self, r: Reminder) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO reminders (msg_id,group_id,to_userid,urgent,summary,"
                "question,ai_reply,status,created_at) VALUES (?,?,?,?,?,?,?, 'pending', ?)",
                (r.msg_id, r.group_id, r.to_userid, int(r.urgent), r.summary,
                 r.question, r.ai_reply, datetime.now().isoformat()),
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
