"""律师控制台 API：待办队列、回复/沉默日志、群开关、话术反馈、数据看板。

前端界面 [待定]（方案建议 React）；Phase 1 影子模式先以 API + 任意 HTTP 客户端复核。
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from responder.models import GroupProfile
from responder.store.db import Store


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


class AiSwitch(BaseModel):
    enabled: bool


@router.post("/groups/{group_id}/ai")
def toggle_ai(group_id: str, body: AiSwitch, store: Store = Depends(get_store)):
    """一键开关某群的 AI（律师群内发言的自动接管在判断引擎内处理）。"""
    store.set_group_ai(group_id, body.enabled)
    return {"ok": True}


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
