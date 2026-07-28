"""律师控制台 API：待办队列、回复/沉默日志、群开关、话术反馈、数据看板。

前端界面 [待定]（方案建议 React）；Phase 1 影子模式先以 API + 任意 HTTP 客户端复核。
"""

import hmac
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from responder.models import GroupProfile
from responder.store.db import Store

# 控制台网页（中文单页，手机友好）。页面本身公开（相当于登录页），
# 页内所有数据请求仍走 /console/* 的 X-Admin-Token 鉴权。
ui_router = APIRouter()
_UI_FILE = Path(__file__).parent / "static" / "index.html"


@ui_router.get("/ui", include_in_schema=False)
def console_ui() -> HTMLResponse:
    return HTMLResponse(_UI_FILE.read_text(encoding="utf-8"))


def require_admin(request: Request, x_admin_token: str | None = Header(default=None)):
    """公网部署必须配置 RESPONDER_ADMIN_TOKEN；为空时不鉴权（仅限本机开发）。"""
    token = request.app.state.pipeline.settings.admin_token
    if token and not hmac.compare_digest(x_admin_token or "", token):
        raise HTTPException(401, "missing or invalid X-Admin-Token")


router = APIRouter(prefix="/console", dependencies=[Depends(require_admin)])


def get_store(request: Request) -> Store:
    return request.app.state.store


@router.get("/todo")
def todo_queue(store: Store = Depends(get_store)):
    """AI 承接后等待人工跟进的问题，按紧急度排序。"""
    return store.pending_reminders()


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
def leads(status: str | None = None, limit: int = 200, store: Store = Depends(get_store)):
    """线索看板：AI 筛查后的交接单，按意向热度排序（先打该打的电话）。"""
    return store.list_leads(status, limit)


class LeadStatus(BaseModel):
    status: str  # new | contacted | converted | invalid


@router.post("/leads/{lead_id}/status")
def set_lead_status(lead_id: int, body: LeadStatus, store: Store = Depends(get_store)):
    if body.status not in ("new", "contacted", "converted", "invalid"):
        raise HTTPException(400, "无效的线索状态")
    store.set_lead_status(lead_id, body.status)
    return {"ok": True}


@router.get("/decisions")
def decisions(group_id: str | None = None, limit: int = 200, store: Store = Depends(get_store)):
    """全量判断日志，含「AI 判断为无需响应」的沉默日志，便于复盘误判。"""
    return store.list_decisions(group_id, limit)


@router.get("/replies")
def replies(group_id: str | None = None, limit: int = 200, store: Store = Depends(get_store)):
    return store.list_replies(group_id, limit)


class Feedback(BaseModel):
    feedback: str  # good | needs_fix:<备注>


@router.post("/replies/{reply_id}/feedback")
def reply_feedback(reply_id: int, body: Feedback, store: Store = Depends(get_store)):
    """话术反馈闭环：标记「好/需修正」，修正样本进入话术迭代库。"""
    store.set_reply_feedback(reply_id, body.feedback)
    return {"ok": True}


@router.get("/groups")
def groups(store: Store = Depends(get_store)):
    return store.list_groups()


@router.put("/groups/{group_id}")
def upsert_group(group_id: str, profile: GroupProfile, store: Store = Depends(get_store)):
    if profile.group_id != group_id:
        raise HTTPException(400, "group_id 不一致")
    store.upsert_group(profile)
    return {"ok": True}


@router.delete("/groups/{group_id}")
def delete_group(group_id: str, store: Store = Depends(get_store)):
    """删除群档案（群 ID 填错的唯一出口；消息/判断/回复留痕不受影响）。"""
    store.delete_group(group_id)
    return {"ok": True}


class AiSwitch(BaseModel):
    enabled: bool


@router.post("/groups/{group_id}/ai")
def toggle_ai(group_id: str, body: AiSwitch, store: Store = Depends(get_store)):
    """一键开关某群的 AI（律师群内发言的自动接管在判断引擎内处理）。"""
    store.set_group_ai(group_id, body.enabled)
    return {"ok": True}


class ModeSwitch(BaseModel):
    mode: str  # shadow | live


@router.post("/mode")
def set_mode(body: ModeSwitch, request: Request):
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
def self_update(request: Request):
    """拉取配置分支的最新代码并重启服务（命令写死，不接受请求参数）。"""
    from responder import ops

    return ops.start_update(request.app.state.pipeline.settings)


@router.get("/update/log")
def update_log(request: Request, lines: int = 40):
    from responder import ops

    settings = request.app.state.pipeline.settings
    return {
        "commit": ops.current_commit(settings.update_repo_dir),
        "log": ops.update_log_tail(settings, lines),
    }


@router.get("/diagnostics")
def diagnostics(request: Request):
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
            {"open_kfid": a.get("open_kfid", ""), "name": a.get("name", "")}
            for a in accounts
        ]
        kf["ok"] = bool(accounts)
    return {
        "mode": s.mode,
        "llm": {
            "provider": f"{provider.name}:{provider.model}" if provider else "",
            "ok": llm_ok,
            "error": llm_err,
        },
        "kf": kf,
    }


@router.get("/kf/contact-link")
def kf_contact_link(request: Request, open_kfid: str, scene: str = ""):
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


@router.get("/metrics")
def metrics(store: Store = Depends(get_store)):
    """看板雏形：三分类分布、承接量、合规拦截数。首响时长依赖上线后真实时间戳。"""
    decisions = store.list_decisions(limit=10000)
    replies = store.list_replies(limit=10000)
    by_action: dict[str, int] = {}
    for d in decisions:
        by_action[d["action"]] = by_action.get(d["action"], 0) + 1
    return {
        "decisions_total": len(decisions),
        "by_action": by_action,
        "urgent_count": sum(1 for d in decisions if d["urgent"]),
        "replies_total": len(replies),
        "compliance_blocked": sum(1 for r in replies if not r["compliance_passed"]),
        "feedback_good": sum(1 for r in replies if r["feedback"] == "good"),
        "feedback_needs_fix": sum(1 for r in replies if r["feedback"].startswith("needs_fix")),
    }
