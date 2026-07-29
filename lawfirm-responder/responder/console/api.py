"""律师控制台 API：待办队列、线索分案、回复/沉默日志、群开关、团队管理、数据看板。

两级身份（同一个 X-Admin-Token 头承载，前端无感）：
- 管理员令牌（.env 的 RESPONDER_ADMIN_TOKEN）→ 全量视角 + 全部管理操作
- 律师个人令牌（团队管理里签发，库中只存 sha256）→ 只看派给自己的线索/待办/会话

数据隔离在服务端做：律师身份下所有查询都带 scoping，前端只是少画几个入口。
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from responder.models import GroupProfile
from responder.store.db import Store

# 控制台网页（中文单页，手机友好）。页面本身公开（相当于登录页），
# 页内所有数据请求仍走 /console/* 的令牌鉴权。
ui_router = APIRouter()
_UI_FILE = Path(__file__).parent / "static" / "index.html"


@ui_router.get("/ui", include_in_schema=False)
def console_ui() -> HTMLResponse:
    return HTMLResponse(_UI_FILE.read_text(encoding="utf-8"))


@dataclass
class Principal:
    role: str  # admin | lawyer
    userid: str = ""
    name: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def get_principal(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> Principal:
    """解析请求身份。管理员令牌未配置时视为本机开发，放行为管理员。"""
    supplied = x_admin_token or ""
    admin_token = request.app.state.pipeline.settings.admin_token
    if not admin_token:
        return Principal(role="admin", name="本机开发")
    if hmac.compare_digest(supplied, admin_token):
        return Principal(role="admin", name="管理员")
    if supplied:
        law = request.app.state.store.get_lawyer_by_token_hash(_hash_token(supplied))
        if law is not None:
            return Principal(
                role="admin" if law["role"] == "admin" else "lawyer",
                userid=law["userid"],
                name=law["name"] or law["userid"],
            )
    raise HTTPException(401, "missing or invalid X-Admin-Token")


def require_admin(request: Request, x_admin_token: str | None = Header(default=None)):
    """管理员专属操作的守门（含 /ingest 复用）；律师令牌在此被拒。"""
    p = get_principal(request, x_admin_token)
    if not p.is_admin:
        raise HTTPException(403, "该操作需要管理员权限")
    return p


def _admin_only(p: "Principal") -> None:
    if not p.is_admin:
        raise HTTPException(403, "该操作需要管理员权限")


router = APIRouter(prefix="/console", dependencies=[Depends(get_principal)])


def get_store(request: Request) -> Store:
    return request.app.state.store


def _own_group_ids(store: Store, p: Principal) -> set[str]:
    """律师视角的会话范围：承办律师是自己，或线索派给了自己。"""
    gids = {
        g["group_id"] for g in store.list_groups()
        if g.get("lawyer_userid") == p.userid
    }
    gids.update(
        x["group_id"] for x in store.list_leads(limit=2000, assigned_userid=p.userid)
    )
    return gids


@router.get("/me")
def me(p: Principal = Depends(get_principal)):
    """登录探针 + 前端角色开关的唯一依据（数据隔离仍在各端点服务端强制）。"""
    return {"role": p.role, "userid": p.userid, "name": p.name}


@router.get("/todo")
def todo_queue(
    store: Store = Depends(get_store), p: Principal = Depends(get_principal)
):
    """AI 承接后等待人工跟进的问题，按紧急度排序。律师只看发给自己的。"""
    return store.pending_reminders(None if p.is_admin else p.userid)


@router.post("/todo/{reminder_id}/done")
def resolve_todo(reminder_id: int, store: Store = Depends(get_store)):
    store.set_reminder_status(reminder_id, "done")
    return {"ok": True}


@router.post("/todo/{reminder_id}/reopen")
def reopen_todo(reminder_id: int, store: Store = Depends(get_store)):
    """撤销「已处理」（控制台 5 秒撤销机制的回滚接口）。"""
    store.set_reminder_status(reminder_id, "pending")
    return {"ok": True}


@router.get("/leads")
def leads(
    status: str | None = None, limit: int = 200, assigned: str | None = None,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """线索队列：按 P0/P1/P2 与评分排序（先打该打的电话）。律师只看派给自己的。"""
    if not p.is_admin:
        assigned = p.userid
    return store.list_leads(status, limit, assigned_userid=assigned)


def _get_lead_scoped(store: Store, p: Principal, lead_id: int) -> dict:
    row = next((x for x in store.list_leads(limit=5000) if x["id"] == lead_id), None)
    if row is None:
        raise HTTPException(404, "线索不存在")
    # 越权按不存在处理，不向律师泄露他人线索的存在性
    if not p.is_admin and row.get("assigned_userid") != p.userid:
        raise HTTPException(404, "线索不存在")
    return row


class LeadStatus(BaseModel):
    status: str  # new | contacted | converted | invalid


@router.post("/leads/{lead_id}/status")
def set_lead_status(
    lead_id: int, body: LeadStatus,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    if body.status not in ("new", "contacted", "converted", "invalid"):
        raise HTTPException(400, "无效的线索状态")
    _get_lead_scoped(store, p, lead_id)
    store.set_lead_status(lead_id, body.status)
    return {"ok": True}


class LeadAssign(BaseModel):
    userid: str


@router.post("/leads/{lead_id}/assign")
def reassign_lead(
    lead_id: int, body: LeadAssign, request: Request,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """手动改派（管理员）：写线索 + 同步会话承办律师 + 给新负责人推交接单。"""
    from responder import assignment
    from responder import lead as lead_mod

    _admin_only(p)
    row = _get_lead_scoped(store, p, lead_id)
    law = store.get_lawyer(body.userid)
    if law is None or not law["active"]:
        raise HTTPException(400, "该律师不存在或已停用")
    group = store.get_group(row["group_id"])
    if group is None:
        raise HTTPException(404, "对应会话档案不存在")
    assignment.assign(store, group, row["group_id"], law)
    sender = request.app.state.pipeline.sender
    fresh = store.get_lead(row["group_id"])
    if sender:
        sender.send_direct_text(
            law["userid"], "【改派给您】\n" + lead_mod.format_notification(fresh, group)
        )
        store.mark_lead_notified(row["group_id"])
    return {"ok": True, "assigned_userid": law["userid"]}


@router.post("/kf/sync-servicers")
def sync_kf_servicers(
    request: Request, store: Store = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """按企微后台配置的接待人，回填所有客服会话档案的提醒接收人。

    线索简报是人工复核的入口，收不到＝监督链断了，因此提供随时可执行的补齐动作。
    """
    pipeline = request.app.state.pipeline
    kf_client = getattr(pipeline, "_kf_client", None)
    if kf_client is None or not kf_client.available():
        raise HTTPException(400, "微信客服通道未配置")
    settings = pipeline.settings
    cache: dict[str, list[str]] = {}
    changed = []
    for row in store.list_groups():
        if not row.get("kf_open_kfid") or row.get("lawyer_userid"):
            continue
        kfid = row["kf_open_kfid"]
        if kfid not in cache:
            cache[kfid] = kf_client.servicer_list(kfid)
        target = cache[kfid][0] if cache[kfid] else settings.default_notify_userid
        if not target:
            continue
        g = store.get_group(row["group_id"])
        g.lawyer_userid = target
        if len(cache[kfid]) > 1 and not g.backup_userid:
            g.backup_userid = cache[kfid][1]
        store.upsert_group(g)
        changed.append({"group_id": g.group_id, "to": target})
    return {"ok": True, "updated": changed}


@router.post("/leads/{lead_id}/notify")
def notify_lead(
    lead_id: int, request: Request,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """（重新）把线索交接单推给负责人——人工复核收不到时的补救入口。

    目标顺序与自动派发一致：指派律师 → 会话承办人 → 全局兜底。
    """
    from responder import lead as lead_mod

    row = _get_lead_scoped(store, p, lead_id)
    group = store.get_group(row["group_id"])
    if group is None:
        raise HTTPException(404, "对应会话档案不存在")
    pipeline = request.app.state.pipeline
    to = (
        row.get("assigned_userid")
        or group.lawyer_userid
        or pipeline.settings.default_notify_userid
    )
    if not to:
        raise HTTPException(400, "该会话没有提醒接收人，请先执行接待人回填")
    sender = pipeline.sender
    if sender is None:
        raise HTTPException(400, "影子模式不对外发送；切到正式模式后再试")
    if not sender.send_direct_text(to, lead_mod.format_notification(row, group)):
        raise HTTPException(502, "企微推送失败，请稍后重试")
    store.mark_lead_notified(row["group_id"])
    return {"ok": True, "to": to}


@router.get("/decisions")
def decisions(
    group_id: str | None = None, limit: int = 200,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """全量判断日志，含「AI 判断为无需响应」的沉默日志，便于复盘误判。"""
    rows = store.list_decisions(group_id, limit)
    if not p.is_admin:
        own = _own_group_ids(store, p)
        rows = [r for r in rows if r["group_id"] in own]
    return rows


@router.get("/replies")
def replies(
    group_id: str | None = None, limit: int = 200,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    rows = store.list_replies(group_id, limit)
    if not p.is_admin:
        own = _own_group_ids(store, p)
        rows = [r for r in rows if r["group_id"] in own]
    return rows


class Feedback(BaseModel):
    feedback: str  # good | needs_fix:<备注>


@router.post("/replies/{reply_id}/feedback")
def reply_feedback(reply_id: int, body: Feedback, store: Store = Depends(get_store)):
    """话术反馈闭环：标记「好/需修正」，修正样本进入话术迭代库。"""
    store.set_reply_feedback(reply_id, body.feedback)
    return {"ok": True}


@router.get("/groups")
def groups(store: Store = Depends(get_store), p: Principal = Depends(get_principal)):
    rows = store.list_groups()
    if not p.is_admin:
        own = _own_group_ids(store, p)
        rows = [g for g in rows if g["group_id"] in own]
    return rows


@router.put("/groups/{group_id}")
def upsert_group(
    group_id: str, profile: GroupProfile,
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    if profile.group_id != group_id:
        raise HTTPException(400, "group_id 不一致")
    # bot_webhook 由机器人回调自动写入、不在编辑表单里，整档覆盖时要保留，
    # 否则改一次「承办律师」就把群聊通道的发送地址抹掉了
    existing = store.get_group(group_id)
    if existing is not None and not profile.bot_webhook:
        profile.bot_webhook = existing.bot_webhook
        profile.bot_webhook_at = existing.bot_webhook_at
    store.upsert_group(profile)
    return {"ok": True}


@router.delete("/groups/{group_id}")
def delete_group(
    group_id: str,
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    """删除群档案（群 ID 填错的唯一出口；消息/判断/回复留痕不受影响）。"""
    store.delete_group(group_id)
    return {"ok": True}


class AiSwitch(BaseModel):
    enabled: bool


@router.post("/groups/{group_id}/ai")
def toggle_ai(
    group_id: str, body: AiSwitch,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """一键开关某群的 AI。律师可以关自己会话里的 AI（这是他的客户），别人的不行。"""
    if not p.is_admin and group_id not in _own_group_ids(store, p):
        raise HTTPException(404, "会话不存在")
    store.set_group_ai(group_id, body.enabled)
    return {"ok": True}


class ModeSwitch(BaseModel):
    mode: str  # shadow | live


@router.post("/mode")
def set_mode(body: ModeSwitch, request: Request, _: Principal = Depends(require_admin)):
    """切换运行模式（影子/正式），即时生效并写回 .env（重启后保持）。

    正式模式下 AI 会真的向客户发言，属重大操作——前端须二次确认。
    """
    from responder.config import persist_setting

    if body.mode not in ("shadow", "live"):
        raise HTTPException(400, "mode 只能是 shadow 或 live")
    settings = request.app.state.pipeline.settings
    settings.mode = body.mode
    persisted = persist_setting("RESPONDER_MODE", body.mode)
    return {"ok": True, "mode": body.mode, "persisted": persisted}


@router.post("/update")
def self_update(request: Request, _: Principal = Depends(require_admin)):
    """拉取配置分支的最新代码并重启服务（命令写死，不接受请求参数）。"""
    from responder import ops

    return ops.start_update(request.app.state.pipeline.settings)


@router.get("/update/log")
def update_log(request: Request, lines: int = 40, _: Principal = Depends(require_admin)):
    from responder import ops

    settings = request.app.state.pipeline.settings
    return {
        "commit": ops.current_commit(settings.update_repo_dir),
        "log": ops.update_log_tail(settings, lines),
    }


def _bot_diag(s, store: Store) -> dict:
    """群聊助手自检：光有凭据不算通——还得真有群把消息推进来。

    `chats` 为 0 说明机器人还没被拉进任何群（或回调没到）；`sendable` 为 0 说明
    有群但拿不到发送地址，AI 只会出草稿不会发言，是必须暴露的静默失败。
    """
    groups = [g for g in store.list_groups() if not g.get("kf_open_kfid")]
    sendable = [g for g in groups if g.get("bot_webhook") or g.get("robot_webhook")]
    return {
        "configured": bool(s.wecom_bot_token and s.wecom_bot_aes_key),
        "enabled": s.bot_enabled,
        "chats": len(groups),
        "sendable": len(sendable),
        "hint": (
            "未配置智能机器人凭据（后台创建机器人后回填 Token/EncodingAESKey）"
            if not (s.wecom_bot_token and s.wecom_bot_aes_key)
            else "机器人还没收到过任何群消息（确认已拉入群且群内 @ 过它）"
            if not groups
            else "群里拿不到发送地址，AI 只会出草稿：等下一条 @ 消息刷新，"
            "或在群档案里手工填 robot_webhook"
            if not sendable
            else ""
        ),
    }


@router.get("/diagnostics")
def diagnostics(request: Request, _: Principal = Depends(require_admin)):
    """远程自检：模型连通性、微信客服通道与客服账号列表。

    企微 API 受可信 IP 限制，只有服务器本身能调；此端点让运维/Claude 无需登录
    服务器即可确认三条外部依赖是否健康。
    """
    from responder.engine import llm

    pipeline = request.app.state.pipeline
    s = pipeline.settings
    provider = llm.resolve(s)
    llm_ok, llm_err = llm.ping(s)

    kf_client = getattr(pipeline, "kf_client", None) or getattr(
        request.app.state.worker, "kf_client", None
    )
    kf: dict = {"configured": bool(s.wecom_kf_secret), "accounts": [], "ok": False}
    if kf_client is not None and kf_client.available():
        accounts = kf_client.account_list()
        kf["accounts"] = [
            {
                "open_kfid": a.get("open_kfid", ""),
                "name": a.get("name", ""),
                # 接待人＝提醒接收人；取不到时要能看清原始返回，否则是静默失败
                "servicers": kf_client.servicer_raw(a.get("open_kfid", "")),
            }
            for a in accounts
        ]
        kf["ok"] = bool(accounts)
    # 没有提醒接收人的会话＝线索生成了也没人知道，属静默失败，必须显式暴露
    store = request.app.state.store
    orphan = [
        g["group_id"] for g in store.list_groups()
        if g.get("ai_enabled") and not (g.get("lawyer_userid") or s.default_notify_userid)
    ]
    return {
        "mode": s.mode,
        "llm": {
            "provider": f"{provider.name}:{provider.model}" if provider else "",
            "ok": llm_ok,
            "error": llm_err,
        },
        "kf": kf,
        "bot": _bot_diag(s, store),
        "notify": {
            "ok": not orphan,
            "groups_without_target": orphan[:10],
            "hint": "" if not orphan else "这些会话没有提醒接收人，线索与紧急提醒推不出去",
        },
    }


@router.get("/kf/contact-link")
def kf_contact_link(
    request: Request, open_kfid: str, scene: str = "",
    _: Principal = Depends(require_admin),
):
    """生成客服会话入口链接（贴官网/名片/朋友圈，客户点开即进线）。"""
    pipeline = request.app.state.pipeline
    kf_client = getattr(pipeline, "kf_client", None) or getattr(
        request.app.state.worker, "kf_client", None
    )
    if kf_client is None or not kf_client.available():
        raise HTTPException(400, "微信客服通道未配置")
    payload = {"open_kfid": open_kfid}
    if scene:
        payload["scene"] = scene
    try:
        data = kf_client.post_raw("kf/add_contact_way", payload)
    except Exception as e:
        raise HTTPException(502, f"生成失败：{e}") from e
    return {"url": data.get("url", "")}


@router.get("/conversation")
def conversation(
    group_id: str, limit: int = 60,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """会话原文——人工复核的依据。没有它，律师无法判断 AI 那句话回得对不对。"""
    if not p.is_admin and group_id not in _own_group_ids(store, p):
        raise HTTPException(404, "会话不存在")
    return store.recent_messages(group_id, limit)


@router.get("/metrics")
def metrics(store: Store = Depends(get_store), p: Principal = Depends(get_principal)):
    """看板：三分类分布、承接量、合规拦截，以及线索转化漏斗（业务侧最关心）。

    律师身份下所有数字都只统计自己名下的会话与线索；管理员额外拿到分律师负载表。
    """
    decisions = store.list_decisions(limit=10000)
    replies = store.list_replies(limit=10000)
    leads = store.list_leads(limit=10000)
    if not p.is_admin:
        own = _own_group_ids(store, p)
        decisions = [d for d in decisions if d["group_id"] in own]
        replies = [r for r in replies if r["group_id"] in own]
        leads = [x for x in leads if x.get("assigned_userid") == p.userid]
    by_action: dict[str, int] = {}
    for d in decisions:
        by_action[d["action"]] = by_action.get(d["action"], 0) + 1
    by_status: dict[str, int] = {}
    for lead_row in leads:
        by_status[lead_row["status"]] = by_status.get(lead_row["status"], 0) + 1
    out = {
        "scope": "all" if p.is_admin else "mine",
        "decisions_total": len(decisions),
        "by_action": by_action,
        "urgent_count": sum(1 for d in decisions if d["urgent"]),
        "replies_total": len(replies),
        "compliance_blocked": sum(1 for r in replies if not r["compliance_passed"]),
        "feedback_good": sum(1 for r in replies if r["feedback"] == "good"),
        "feedback_needs_fix": sum(1 for r in replies if r["feedback"].startswith("needs_fix")),
        "leads_total": len(leads),
        "leads_with_contact": sum(1 for x in leads if x["contact"]),
        "leads_by_status": by_status,
        "leads_hot": sum(1 for x in leads if x["intent"] == "hot"),
        "leads_p0": sum(1 for x in leads if x.get("priority") == "P0"),
    }
    if p.is_admin:
        load = store.lawyer_load()
        out["by_lawyer"] = [
            {
                "userid": law["userid"],
                "name": law["name"] or law["userid"],
                "on_duty": bool(law["on_duty"]),
                "active": bool(law["active"]),
                "open": load.get(law["userid"], {}).get("open", 0),
                "p0": load.get(law["userid"], {}).get("p0", 0),
            }
            for law in store.list_lawyers()
        ]
        out["leads_unassigned"] = sum(1 for x in leads if not x.get("assigned_userid"))
    return out


# ================================================================ 团队管理
class LawyerIn(BaseModel):
    name: str = ""
    specialties: str = ""  # 顿号/逗号分隔，如「劳动仲裁、工伤」
    role: str = "lawyer"  # lawyer | admin
    on_duty: bool = True
    active: bool = True


@router.get("/lawyers")
def list_lawyers(
    store: Store = Depends(get_store), _: Principal = Depends(require_admin)
):
    """名册 + 实时负载。令牌哈希不出接口。"""
    load = store.lawyer_load()
    out = []
    for law in store.list_lawyers():
        row = {k: v for k, v in law.items() if k != "token_hash"}
        row["has_token"] = bool(law["token_hash"])
        row.update(load.get(law["userid"], {"open": 0, "p0": 0}))
        out.append(row)
    return out


@router.put("/lawyers/{userid}")
def upsert_lawyer(
    userid: str, body: LawyerIn,
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    if body.role not in ("lawyer", "admin"):
        raise HTTPException(400, "role 只能是 lawyer 或 admin")
    store.upsert_lawyer(userid.strip(), body.model_dump())
    return {"ok": True}


def _login_base(request: Request) -> str:
    """登录链接的基础地址：显式配置优先，否则按请求 Host 推断（nginx 透传）。"""
    configured = request.app.state.pipeline.settings.public_base_url.rstrip("/")
    if configured:
        return configured
    host = request.headers.get("host", "")
    scheme = "http" if host.startswith(("127.", "localhost")) else "https"
    return f"{scheme}://{host}" if host else ""


def _issue_token(store: Store, userid: str) -> str:
    """签发新令牌并使旧令牌立即失效（轮换即注销）。"""
    token = secrets.token_urlsafe(24)
    store.set_lawyer_token_hash(userid, _hash_token(token))
    return token


@router.post("/lawyers/{userid}/token")
def issue_lawyer_token(
    userid: str, request: Request,
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    """生成登录链接给管理员复制转发。令牌仅此一次可见，库中只存哈希。"""
    if store.get_lawyer(userid) is None:
        raise HTTPException(404, "律师不存在，请先保存档案")
    token = _issue_token(store, userid)
    base = _login_base(request)
    # 令牌放在 # 片段里：浏览器不会把它发给服务器，nginx 日志里不会留痕
    return {"ok": True, "login_url": f"{base}/ui#t={token}" if base else "", "token": token}


@router.post("/lawyers/{userid}/send-login")
def send_lawyer_login(
    userid: str, request: Request,
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    """把登录链接直接企微单聊发给该律师本人——令牌不经过任何中间人。

    发送走未门控的原始通道：这是运维动作而非客户触达，影子模式也该能开通账号。
    每次发送都轮换令牌（旧链接立即失效），转发出去的旧消息不构成长期风险。
    """
    law = store.get_lawyer(userid)
    if law is None:
        raise HTTPException(404, "律师不存在，请先保存档案")
    pipeline = request.app.state.pipeline
    sender = pipeline._sender
    if sender is None:
        raise HTTPException(400, "企微发送通道未配置")
    base = _login_base(request)
    if not base:
        raise HTTPException(400, "无法确定控制台地址，请配置 RESPONDER_PUBLIC_BASE_URL")
    token = _issue_token(store, userid)
    text = (
        f"{law['name'] or userid}律师您好，这是您的 AI 助手工作台入口：\n"
        f"{base}/ui#t={token}\n"
        "点开即自动登录（请勿转发）。工作台里能看到派给您的客户线索、"
        "待跟进事项与完整咨询记录。"
    )
    if not sender.send_direct_text(userid, text):
        raise HTTPException(502, "企微推送失败：请确认该 userid 在应用可见范围内")
    return {"ok": True}
