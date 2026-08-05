"""SQLite 存储：群档案、消息、判断日志（含沉默日志）、回复记录、提醒队列。

单文件本地存储，Phase 1 影子模式足够；Phase 3 扩容再评估 Postgres。[待定]
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from responder.models import Decision, GroupProfile, IncomingMessage, Reminder


def _escape_like(text: str) -> str:
    """转义 LIKE 通配符。客户搜「100%赔偿」时 % 是字面量，不是「匹配任意」。"""
    return (
        (text or "").strip()
        .replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _in_clause(column: str, values: list[str]) -> tuple[str, list]:
    """生成 `column IN (?,?,…)`；空集合返回恒假条件，避免退化成「全放行」。"""
    if not values:
        return "1=0", []
    return f"{column} IN ({','.join('?' * len(values))})", list(values)


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
    bot_webhook TEXT DEFAULT '',
    bot_webhook_at TEXT,
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
CREATE TABLE IF NOT EXISTS lawyers (
    userid TEXT PRIMARY KEY,          -- 企微 userid，同时是登录身份
    name TEXT DEFAULT '',
    specialties TEXT DEFAULT '',      -- 顿号/逗号分隔的专长领域，派单匹配用
    role TEXT DEFAULT 'lawyer',       -- lawyer | admin（个人令牌也可拥有管理权限）
    on_duty INTEGER DEFAULT 1,        -- 停诊/休假时关掉，不再接新派单
    active INTEGER DEFAULT 1,         -- 停用即禁止登录；不物理删除，保留分案历史归属
    token_hash TEXT DEFAULT '',       -- 登录令牌只存 sha256，泄库不泄令牌
    last_assigned_at TEXT,            -- 负载均衡的平局裁决：最久没接单的先接
    created_at TEXT
);
"""

# 旧库平滑升级：新增列在此登记，启动时按需 ALTER（SQLite 无 IF NOT EXISTS 列语法）
_ADDED_COLUMNS = {
    "groups": {
        "robot_webhook": "TEXT DEFAULT ''",
        # 智能机器人回调下发的会话 webhook（短期有效）；NULL 而非 ''，
        # 因为 bot_webhook_at 要按 datetime | None 回填模型
        "bot_webhook": "TEXT DEFAULT ''",
        "bot_webhook_at": "TEXT",
        "kf_open_kfid": "TEXT DEFAULT ''",
        "kf_external_userid": "TEXT DEFAULT ''",
        # 抖音私信会话的对方标识（open_id）
        "douyin_open_id": "TEXT DEFAULT ''",
        # 会话已转给哪位律师人工接待（见 docs/kf-handoff.md）。
        # 转接发生在律师「发言之前」，靠发言触发的 human-takeover 兜不住，
        # 必须显式记状态，否则 AI 会抢在律师前面回话。
        "handoff_userid": "TEXT DEFAULT ''",
        "handoff_at": "TEXT",
    },
    # parts：这条回复实际拆成了几条平台消息。抖音按**条**限额（同一窗口最多 6 条），
    # 一行 replies 可能对应 3 条真实消息，不记下来就算不准配额。
    "replies": {"category": "TEXT DEFAULT ''", "parts": "INTEGER DEFAULT 1"},
    # 待办卡片要把「客户问的什么」当主角展示，不能让控制台去解析摘要文本
    "reminders": {"question": "TEXT DEFAULT ''", "ai_reply": "TEXT DEFAULT ''"},
    # 分案系统：指派对象 + 优先级评分（factors 是评分依据清单，控制台与推送共用）
    "leads": {
        "assigned_userid": "TEXT DEFAULT ''",
        "assigned_at": "TEXT",
        "priority": "TEXT DEFAULT ''",
        "score": "INTEGER DEFAULT 0",
        "factors": "TEXT DEFAULT '[]'",
        "sla_nudged": "INTEGER DEFAULT 0",
        # 律师跟进备注：打完电话记两句，下次跟进不从零开始
        "notes": "TEXT DEFAULT ''",
    },
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
        # 升级前的存量线索没有优先级：按旧意向估一个，让排序不至于把热线索排到最后。
        # 只填 priority='' 的行，幂等；下次消息触达时会被真实评分覆盖。
        conn.execute(
            "UPDATE leads SET"
            " priority = CASE intent WHEN 'hot' THEN 'P1' ELSE 'P2' END,"
            " score = CASE intent WHEN 'hot' THEN 40 WHEN 'warm' THEN 15 ELSE 0 END"
            " WHERE priority = '' OR priority IS NULL"
        )
        # 索引必须建在补列之后：assigned_userid 等列由上面的 ALTER 引入，
        # 放进建表 SCHEMA 会在新库上因「列不存在」直接炸掉
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_messages_group_time"
            " ON messages(group_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_decisions_group ON decisions(group_id)",
            "CREATE INDEX IF NOT EXISTS idx_replies_group ON replies(group_id)",
            "CREATE INDEX IF NOT EXISTS idx_leads_assigned"
            " ON leads(assigned_userid, status)",
            "CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status)",
        ):
            conn.execute(stmt)

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
                   bot_webhook,bot_webhook_at,kf_open_kfid,kf_external_userid,
                   douyin_open_id,handoff_userid,handoff_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(group_id) DO UPDATE SET
                   name=excluded.name, client_status=excluded.client_status,
                   case_type=excluded.case_type, case_stage=excluded.case_stage,
                   lawyer_name=excluded.lawyer_name, lawyer_userid=excluded.lawyer_userid,
                   backup_userid=excluded.backup_userid, ai_enabled=excluded.ai_enabled,
                   robot_webhook=excluded.robot_webhook,
                   bot_webhook=excluded.bot_webhook,
                   bot_webhook_at=excluded.bot_webhook_at,
                   kf_open_kfid=excluded.kf_open_kfid,
                   kf_external_userid=excluded.kf_external_userid,
                   douyin_open_id=excluded.douyin_open_id,
                   handoff_userid=excluded.handoff_userid,
                   handoff_at=excluded.handoff_at""",
                (
                    g.group_id, g.name, g.client_status.value, g.case_type, g.case_stage,
                    g.lawyer_name, g.lawyer_userid, g.backup_userid, int(g.ai_enabled),
                    g.robot_webhook, g.bot_webhook,
                    g.bot_webhook_at.isoformat() if g.bot_webhook_at else None,
                    g.kf_open_kfid, g.kf_external_userid, g.douyin_open_id,
                    g.handoff_userid,
                    g.handoff_at.isoformat() if g.handoff_at else None,
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

    def set_handoff(self, group_id: str, userid: str) -> None:
        """标记会话已转给该律师人工接待（userid 为空 = 收回给 AI）。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE groups SET handoff_userid=?, handoff_at=? WHERE group_id=?",
                (userid, datetime.now().isoformat() if userid else None, group_id),
            )

    def delete_group(self, group_id: str) -> None:
        """仅删档案；该群的消息/判断/回复留痕保持不动。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM groups WHERE group_id=?", (group_id,))

    def set_group_ai(self, group_id: str, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE groups SET ai_enabled=? WHERE group_id=?", (int(enabled), group_id)
            )

    def list_groups(self, lawyer_userid: str | None = None) -> list[dict]:
        q = "SELECT * FROM groups"
        args: tuple = ()
        if lawyer_userid is not None:
            q += " WHERE lawyer_userid=?"
            args = (lawyer_userid,)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, args).fetchall()]

    def own_group_ids(self, userid: str) -> set[str]:
        """某位律师名下的会话集合：承办人是他，或线索派给了他。

        两条窄查询（各吃一个索引）代替「拉全部群 + 拉 2000 条线索再内存筛」——
        这个集合在律师身份的几乎每个请求上都要算一次。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT group_id FROM groups WHERE lawyer_userid=?"
                " UNION SELECT group_id FROM leads WHERE assigned_userid=?",
                (userid, userid),
            ).fetchall()
        return {r["group_id"] for r in rows}

    # ------------------------------------------------------------ 聚合统计
    def decision_stats(self, group_ids: list[str] | None = None) -> dict:
        """看板口径的判断统计。SQL 聚合而非拉一万行进 Python——
        超过一万条之后内存聚合会静默漏计（LIMIT 截断），看板从此说谎。"""
        where, args = "", []
        if group_ids is not None:
            clause, extra = _in_clause("group_id", group_ids)
            where, args = f" WHERE {clause}", extra
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT action, COUNT(*) AS n, SUM(urgent) AS urgent"
                f" FROM decisions{where} GROUP BY action",
                args,
            ).fetchall()
        by_action = {r["action"]: r["n"] for r in rows}
        return {
            "total": sum(by_action.values()),
            "by_action": by_action,
            "urgent": sum((r["urgent"] or 0) for r in rows),
        }

    def reply_stats(self, group_ids: list[str] | None = None) -> dict:
        where, args = "", []
        if group_ids is not None:
            clause, extra = _in_clause("group_id", group_ids)
            where, args = f" WHERE {clause}", extra
        with self._conn() as conn:
            r = conn.execute(
                f"SELECT COUNT(*) AS total,"
                f" SUM(CASE WHEN compliance_passed=0 THEN 1 ELSE 0 END) AS blocked,"
                f" SUM(CASE WHEN feedback='good' THEN 1 ELSE 0 END) AS good,"
                f" SUM(CASE WHEN feedback LIKE 'needs_fix%' THEN 1 ELSE 0 END) AS bad"
                f" FROM replies{where}",
                args,
            ).fetchone()
        return {k: (r[k] or 0) for k in ("total", "blocked", "good", "bad")}

    def lead_stats(self, assigned_userid: str | None = None) -> dict:
        where, args = "", []
        if assigned_userid is not None:
            where, args = " WHERE assigned_userid=?", [assigned_userid]
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT status, COUNT(*) AS n FROM leads{where} GROUP BY status", args
            ).fetchall()
            agg = conn.execute(
                f"SELECT COUNT(*) AS total,"
                f" SUM(CASE WHEN contact<>'' THEN 1 ELSE 0 END) AS with_contact,"
                f" SUM(CASE WHEN intent='hot' THEN 1 ELSE 0 END) AS hot,"
                f" SUM(CASE WHEN priority='P0' THEN 1 ELSE 0 END) AS p0,"
                f" SUM(CASE WHEN group_id LIKE 'dy:%' THEN 1 ELSE 0 END) AS src_dy,"
                f" SUM(CASE WHEN group_id LIKE 'kf:%' THEN 1 ELSE 0 END) AS src_kf,"
                f" SUM(CASE WHEN assigned_userid='' AND status IN ('new','contacted')"
                f"      THEN 1 ELSE 0 END) AS unassigned"
                f" FROM leads{where}",
                args,
            ).fetchone()
        total = agg["total"] or 0
        return {
            "total": total,
            "by_status": {r["status"]: r["n"] for r in rows},
            "with_contact": agg["with_contact"] or 0,
            "hot": agg["hot"] or 0,
            "p0": agg["p0"] or 0,
            "unassigned": agg["unassigned"] or 0,
            "by_source": {
                "dy": agg["src_dy"] or 0,
                "kf": agg["src_kf"] or 0,
                "group": total - (agg["src_dy"] or 0) - (agg["src_kf"] or 0),
            },
        }

    # ------------------------------------------------------------ messages
    def save_message(self, m: IncomingMessage) -> bool:
        """返回是否新消息；False = msg_id 已存在（企微超时重发的重复回调）。"""
        with self._conn() as conn:
            cur = conn.execute(
                # 显式列名：位置式 INSERT 依赖 messages 永远保持 7 列，加一列就错位
                "INSERT OR IGNORE INTO messages"
                " (msg_id,group_id,sender_id,sender_is_staff,content,msg_type,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    m.msg_id, m.group_id, m.sender_id, int(m.sender_is_staff),
                    m.content, m.msg_type, m.created_at.isoformat(),
                ),
            )
            return cur.rowcount > 0

    def upsert_message(self, m: IncomingMessage) -> bool:
        """写入或更新消息内容（导入路径用：二次导出里补充的留资内容不该被丢弃）。

        返回是否为新消息，语义与 save_message 一致，便于调用方统计新增/更新。
        """
        with self._conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM messages WHERE msg_id=?", (m.msg_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO messages"
                " (msg_id,group_id,sender_id,sender_is_staff,content,msg_type,created_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(msg_id) DO UPDATE SET"
                " content=excluded.content, msg_type=excluded.msg_type",
                (
                    m.msg_id, m.group_id, m.sender_id, int(m.sender_is_staff),
                    m.content, m.msg_type, m.created_at.isoformat(),
                ),
            )
            return exists is None

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
        """最近 N 条群消息，按时间正序（注入 LLM 上下文用）。

        msg_type 必须带出来：进线事件占位（msg_type='event'）与真实发言长得一样
        （content 都可能为空），少了这一列就分不清「客户第一次进来」和「老客户回访」。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT sender_id, sender_is_staff, content, msg_type, created_at"
                " FROM messages"
                " WHERE group_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def count_event_messages(self) -> int:
        """收到过多少条进线事件（msg_type='event'）。

        「进线即问候」整条链路挂在企微推送这个事件上。一条都没有，问候就永远
        不会发——而这个现象跟「新版没部署」在客户那边看起来一模一样。
        自检里报出这个数，就能一眼分清是哪一种。
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE msg_type='event'"
            ).fetchone()
        return int(row["n"] or 0)

    def idle_conversations(self, since: datetime, until: datetime) -> list[str]:
        """最后一条消息落在 [since, until] 内的会话——即刚安静下来的对话。

        用于「聊完了但没留电话」的咨询补一份线索简报归档（冷线索也有跟进价值）。
        """
        # 先按时间范围收窄候选群（吃 idx_messages_group_time），再对候选算 MAX。
        # 直接对全表 GROUP BY 会随消息量线性变慢，而这个查询每 10 秒跑一次。
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT group_id, MAX(created_at) AS last_at FROM messages"
                " WHERE group_id IN ("
                "   SELECT DISTINCT group_id FROM messages WHERE created_at BETWEEN ? AND ?"
                " ) GROUP BY group_id HAVING last_at BETWEEN ? AND ?",
                (since.isoformat(), until.isoformat(),
                 since.isoformat(), until.isoformat()),
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

    def list_decisions(
        self, group_id: str | None = None, limit: int = 200,
        group_ids: list[str] | None = None,
    ) -> list[dict]:
        """group_ids 用于律师视角：范围过滤必须下推到 SQL。

        先 LIMIT 再在内存里筛本人的群，会让忙时段律师翻到一整页空白——
        他名下的记录被别人的记录挤出了那 200 条窗口。
        """
        q = "SELECT * FROM decisions"
        where, args = [], []
        if group_id:
            where.append("group_id=?")
            args.append(group_id)
        if group_ids is not None:
            clause, extra = _in_clause("group_id", group_ids)
            where.append(clause)
            args += extra
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY id DESC LIMIT ?"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, (*args, limit)).fetchall()]

    # ------------------------------------------------------------ replies
    def save_reply(
        self, msg_id: str, group_id: str, text: str, mode: str, passed: bool,
        category: str = "", parts: int = 1,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO replies (msg_id,group_id,text,mode,category,parts,"
                "compliance_passed,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (msg_id, group_id, text, mode, category, max(1, parts), int(passed),
                 datetime.now().isoformat()),
            )
            return cur.lastrowid

    def last_message_at(self) -> datetime | None:
        """全库最后一条消息的时间。自动升级用来判断「现在忙不忙」。"""
        with self._conn() as conn:
            row = conn.execute("SELECT MAX(created_at) AS t FROM messages").fetchone()
        return datetime.fromisoformat(row["t"]) if row and row["t"] else None

    def last_message_at_in(self, group_id: str) -> datetime | None:
        """该会话最后一条真实消息的时间（事件占位不算）。

        用来分辨「回访」和「同一次对话」：刚聊完又点回会话页的人不需要再被
        打一次招呼，隔了几小时再来的才算回访。
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) AS t FROM messages"
                " WHERE group_id=? AND msg_type!='event'",
                (group_id,),
            ).fetchone()
        return datetime.fromisoformat(row["t"]) if row and row["t"] else None

    def last_customer_message_at(self, group_id: str) -> datetime | None:
        """该会话最后一条**客户**发言的时间。

        抖音只允许在客户发言后的 24 小时内回复，超时接口直接拒——发送前必须按这个
        时间算窗口，而不是按「我们上次说话」算。
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) AS t FROM messages"
                " WHERE group_id=? AND sender_is_staff=0 AND msg_type!='event'",
                (group_id,),
            ).fetchone()
        return datetime.fromisoformat(row["t"]) if row and row["t"] else None

    def sent_parts_since(self, group_id: str, since: datetime) -> int:
        """自 since 起该会话实际发出的**平台消息条数**（分条后的，不是回复条数）。

        抖音的 6 条限额算的是真实消息数，我们一条回复可能拆成 2~3 条，
        按 replies 行数算会低估一倍以上，等发现时配额已经打满了。

        mode='failed' 也要计入：那是「发了一半失败」，前半截平台已经收下并计了数。
        只数 live 会漏掉这部分，越是发送不稳的时候漏得越多。影子模式不外发，不计。
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(parts), 0) AS n FROM replies"
                " WHERE group_id=? AND mode!='shadow' AND created_at>=?",
                (group_id, since.isoformat()),
            ).fetchone()
        return int(row["n"] or 0)

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

    def has_reply_category(self, group_id: str, category: str) -> bool:
        """这通对话是否已经实发过某一类回复。用于「一通对话只做一次」的动作。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM replies WHERE group_id=? AND category=?"
                " AND mode='live' LIMIT 1",
                (group_id, category),
            ).fetchone()
        return row is not None

    def has_greeting(self, group_id: str) -> bool:
        """这通对话是否已经发过开场白（进线问候或引导型开场）。

        一通对话只该有一次自我介绍。客户扫码进来被问候过、接着把情况打出来，
        若规则判不出那是不是法律问题而再回一遍「请您说说什么情况」，
        就是当着客户的面复读——这个查询就是用来拦住第二次的。
        """
        return self.has_reply_category(group_id, "greeting")

    def set_reply_feedback(self, reply_id: int, feedback: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE replies SET feedback=? WHERE id=?", (feedback, reply_id))

    def list_replies(
        self, group_id: str | None = None, limit: int = 200,
        group_ids: list[str] | None = None,
    ) -> list[dict]:
        # 带上客户原话：复核一句回复是否得当，前提是能看到它在回什么
        q = (
            "SELECT r.*, m.content AS question FROM replies r"
            " LEFT JOIN messages m ON m.msg_id = r.msg_id"
        )
        where, args = [], []
        if group_id:
            where.append("r.group_id=?")
            args.append(group_id)
        if group_ids is not None:  # 律师视角：范围下推 SQL，理由同 list_decisions
            clause, extra = _in_clause("r.group_id", group_ids)
            where.append(clause)
            args += extra
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY r.id DESC LIMIT ?"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, (*args, limit)).fetchall()]

    def get_reply(self, reply_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM replies WHERE id=?", (reply_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ 线索
    # 客户一旦留下就不该被后续会话抹掉的字段：空值一律保留旧值。
    # 回头客隔周再问一句「在吗」，重算出来的 contact 是空的——直接覆盖等于把
    # 上次辛苦拿到的电话删了，律师再也打不通。
    _LEAD_STICKY = ("contact", "case_type", "summary")

    def upsert_lead(self, group_id: str, fields: dict) -> None:
        """一个会话一条线索：反复更新而非追加，避免同一客户刷屏。

        两条保护：① 上面 _LEAD_STICKY 的字段空值不覆盖；
        ② score/priority 取历史最高——同一个客户表达过的最强意愿不因为
        后来一句闲聊就被降级（真要降级由人工在控制台标状态）。
        """
        now = datetime.now().isoformat()
        # 只覆盖内容字段；status / assigned_userid 有专用方法，内容更新不得触碰
        defaults = {
            "intent": "", "contact": "", "summary": "", "case_type": "",
            "key_facts": "", "urgency": "", "suggested_action": "",
            "opening_line": "", "signals": "",
            "score": 0, "priority": "", "factors": "[]",
        }
        cols = list(defaults)
        vals = [fields.get(c, defaults[c]) for c in cols]
        sets = []
        for c in cols:
            if c in self._LEAD_STICKY:
                sets.append(
                    f"{c}=CASE WHEN excluded.{c}='' OR excluded.{c} IS NULL"
                    f" THEN leads.{c} ELSE excluded.{c} END"
                )
            elif c == "score":
                sets.append("score=MAX(leads.score, excluded.score)")
            elif c == "priority":
                # 分数取高了，层级必须跟着走同一条线，否则卡片自相矛盾
                sets.append(
                    "priority=CASE WHEN excluded.score >= leads.score"
                    " THEN excluded.priority ELSE leads.priority END"
                )
            elif c == "factors":
                sets.append(
                    "factors=CASE WHEN excluded.score >= leads.score"
                    " THEN excluded.factors ELSE leads.factors END"
                )
            else:
                sets.append(f"{c}=excluded.{c}")
        with self._conn() as conn:
            conn.execute(
                f"""INSERT INTO leads (group_id,{','.join(cols)},created_at,updated_at)
                    VALUES ({','.join('?' * (len(cols) + 3))})
                    ON CONFLICT(group_id) DO UPDATE SET
                    {','.join(sets)},
                    updated_at=excluded.updated_at""",
                (group_id, *vals, now, now),
            )

    def find_lead_by_contact(self, contact: str, exclude_group: str = "") -> dict | None:
        """按手机号跨渠道找已有线索。

        同一个客户可能先在抖音留过号（dy:138…）、后又扫码进微信客服（kf:…），
        那是两条会话档案但同一个人——派单要粘住原律师，不能两位律师各打一遍。
        """
        if not contact:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM leads WHERE contact=? AND group_id<>?"
                " AND assigned_userid<>'' ORDER BY updated_at DESC LIMIT 1",
                (contact, exclude_group),
            ).fetchone()
        return dict(row) if row else None

    def get_lead(self, group_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM leads WHERE group_id=?", (group_id,)
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _lead_filters(
        status: str | None, assigned_userid: str | None,
        q: str | None, priority: str | None, source: str | None,
    ) -> tuple[list[str], list]:
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if assigned_userid is not None:
            where.append("assigned_userid=?")
            args.append(assigned_userid)
        if priority:
            where.append("priority=?")
            args.append(priority)
        if source == "dy":
            where.append("group_id LIKE 'dy:%'")
        elif source == "kf":
            where.append("group_id LIKE 'kf:%'")
        elif source == "group":  # 企微群/机器人会话：除上述两种前缀外的一切
            where.append("group_id NOT LIKE 'dy:%' AND group_id NOT LIKE 'kf:%'")
        if q:
            # 搜索：电话直搜 contact；关键词搜摘要/案由/备注/会话名（客户姓名在群档案上）
            like = f"%{_escape_like(q)}%"
            where.append(
                "(contact LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\'"
                " OR case_type LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\'"
                " OR group_id IN (SELECT group_id FROM groups WHERE name LIKE ? ESCAPE '\\'))"
            )
            args += [like] * 5
        return where, args

    def list_leads(
        self, status: str | None = None, limit: int = 200,
        assigned_userid: str | None = None, *,
        q: str | None = None, priority: str | None = None,
        source: str | None = None, offset: int = 0,
    ) -> list[dict]:
        """按优先级、评分、时间排序——律师先看最该打电话的那个。

        assigned_userid 用于律师个人视角；q/priority/source/offset 供控制台
        在几百条线索规模下检索与分页（导入一次抖音客资就是 350+ 条）。
        """
        where, args = self._lead_filters(status, assigned_userid, q, priority, source)
        sql = "SELECT * FROM leads"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # 未评分的旧行按意向近似归位（hot≈P1、warm≈P2、cold 殿后），不至于沉底
        sql += (
            " ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1"
            " WHEN 'P2' THEN 2 ELSE CASE intent WHEN 'hot' THEN 1"
            " WHEN 'warm' THEN 2 ELSE 3 END END,"
            " score DESC, updated_at DESC LIMIT ? OFFSET ?"
        )
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, (*args, limit, offset)).fetchall()]

    def count_leads(
        self, status: str | None = None, assigned_userid: str | None = None, *,
        q: str | None = None, priority: str | None = None, source: str | None = None,
    ) -> int:
        """与 list_leads 同一套过滤条件的总数——分页 UI 要能说「共 N 条」。"""
        where, args = self._lead_filters(status, assigned_userid, q, priority, source)
        sql = "SELECT COUNT(*) AS n FROM leads"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._conn() as conn:
            return conn.execute(sql, args).fetchone()["n"]

    def set_lead_notes(self, lead_id: int, notes: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE leads SET notes=?, updated_at=? WHERE id=?",
                (notes, datetime.now().isoformat(), lead_id),
            )

    def get_lead_by_id(self, lead_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return dict(row) if row else None

    def assign_lead(self, group_id: str, userid: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE leads SET assigned_userid=?, assigned_at=?, updated_at=?"
                " WHERE group_id=?",
                (userid, datetime.now().isoformat(), datetime.now().isoformat(), group_id),
            )

    def overdue_p0_leads(self, notified_before: datetime) -> list[dict]:
        """通知已发、超时仍停在 new 的强意愿线索——SLA 升级的扫描对象。"""
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    # 时钟用 COALESCE(notified_at, assigned_at)：批量导入的线索
                    # notify=False 从不写 notified_at，只认 notified_at 会让
                    # 导入进来的 P0 永远等不到督办（文档却承诺了有兜底）
                    "SELECT * FROM leads WHERE priority='P0' AND status='new'"
                    " AND sla_nudged=0"
                    " AND COALESCE(notified_at, assigned_at) IS NOT NULL"
                    " AND COALESCE(notified_at, assigned_at)<=?",
                    (notified_before.isoformat(),),
                ).fetchall()
            ]

    def mark_lead_nudged(self, group_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE leads SET sla_nudged=1 WHERE group_id=?", (group_id,))

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

    # ------------------------------------------------------------ 律师名册
    def upsert_lawyer(self, userid: str, fields: dict) -> None:
        """新建/更新律师档案。fields 只含要改的列；token_hash 走专用方法。"""
        allowed = {"name", "specialties", "role", "on_duty", "active"}
        cols = [c for c in fields if c in allowed]
        vals = [
            int(fields[c]) if c in ("on_duty", "active") else fields[c] for c in cols
        ]
        with self._conn() as conn:
            conn.execute(
                f"""INSERT INTO lawyers (userid,{','.join(cols)},created_at)
                    VALUES ({','.join('?' * (len(cols) + 2))})
                    ON CONFLICT(userid) DO UPDATE SET
                    {','.join(f'{c}=excluded.{c}' for c in cols)}""",
                (userid, *vals, datetime.now().isoformat()),
            )

    def get_lawyer(self, userid: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM lawyers WHERE userid=?", (userid,)
            ).fetchone()
        return dict(row) if row else None

    def list_lawyers(self, active_only: bool = False) -> list[dict]:
        q = "SELECT * FROM lawyers"
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY created_at ASC"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q).fetchall()]

    def get_lawyer_by_token_hash(self, token_hash: str) -> dict | None:
        """登录鉴权入口：库里只有哈希，比对同样只用哈希。"""
        if not token_hash:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM lawyers WHERE token_hash=? AND active=1", (token_hash,)
            ).fetchone()
        return dict(row) if row else None

    def set_lawyer_token_hash(self, userid: str, token_hash: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE lawyers SET token_hash=? WHERE userid=?", (token_hash, userid)
            )

    def touch_lawyer_assigned(self, userid: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE lawyers SET last_assigned_at=? WHERE userid=?",
                (datetime.now().isoformat(), userid),
            )

    def lawyer_load(self) -> dict[str, dict]:
        """每位律师手上的在办量：{userid: {open, p0}}。派单负载均衡与团队看板共用。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT assigned_userid AS u,"
                " SUM(CASE WHEN status IN ('new','contacted') THEN 1 ELSE 0 END) AS open,"
                " SUM(CASE WHEN status='new' AND priority='P0' THEN 1 ELSE 0 END) AS p0"
                " FROM leads WHERE assigned_userid != '' GROUP BY assigned_userid"
            ).fetchall()
        return {r["u"]: {"open": r["open"] or 0, "p0": r["p0"] or 0} for r in rows}

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

    def pending_reminders(
        self, to_userid: str | None = None, limit: int = 200,
    ) -> list[dict]:
        q = "SELECT * FROM reminders WHERE status IN ('pending','sent','escalated')"
        args: list = []
        if to_userid is not None:
            q += " AND to_userid=?"
            args.append(to_userid)
        q += " ORDER BY urgent DESC, id ASC LIMIT ?"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, (*args, limit)).fetchall()]

    def get_reminder(self, reminder_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM reminders WHERE id=?", (reminder_id,)
            ).fetchone()
        return dict(row) if row else None

    def overdue_urgent_reminders(self, before: datetime) -> list[dict]:
        """升级扫描专用：只取超时未升级的加急提醒，不再把整张待办表拉出来筛。"""
        with self._conn() as conn:
            return [
                dict(r) for r in conn.execute(
                    "SELECT * FROM reminders WHERE urgent=1"
                    " AND status IN ('pending','sent') AND created_at<=?"
                    " ORDER BY id ASC LIMIT 100",
                    (before.isoformat(),),
                ).fetchall()
            ]

    def resolve_reminders_for_group(self, group_id: str) -> int:
        """线索标记已联系/成交时，把同一会话的未决提醒一并了结。

        待办与线索是同一件事的两个视图，人已经联系过了还让督办去升级
        第二责任人，是在制造无效打扰。
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE reminders SET status='done' WHERE group_id=?"
                " AND status IN ('pending','sent','escalated')",
                (group_id,),
            )
            return cur.rowcount

    def set_reminder_status(self, reminder_id: int, status: str) -> None:
        with self._conn() as conn:
            field = ", escalated_at=?" if status == "escalated" else ""
            args = (
                (status, datetime.now().isoformat(), reminder_id)
                if status == "escalated"
                else (status, reminder_id)
            )
            conn.execute(f"UPDATE reminders SET status=?{field} WHERE id=?", args)
