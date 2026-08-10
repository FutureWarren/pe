"""律师控制台 API：待办队列、线索分案、回复/沉默日志、群开关、团队管理、数据看板。

两级身份（同一个 X-Admin-Token 头承载，前端无感）：
- 管理员令牌（.env 的 RESPONDER_ADMIN_TOKEN）→ 全量视角 + 全部管理操作
- 律师个人令牌（团队管理里签发，库中只存 sha256）→ 只看派给自己的线索/待办/会话

数据隔离在服务端做：律师身份下所有查询都带 scoping，前端只是少画几个入口。
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from responder.compliance import forbidden
from responder.config import persist_setting
from responder.engine import priority
from responder.gateway import wecom_kf as kf_errors
from responder.models import ClientStatus, GroupProfile
from responder.store.db import Store

logger = logging.getLogger(__name__)

# 控制台网页（中文单页，手机友好）。页面本身公开（相当于登录页），
# 页内所有数据请求仍走 /console/* 的令牌鉴权。
ui_router = APIRouter()
_UI_FILE = Path(__file__).parent / "static" / "index.html"


@ui_router.get("/ui", include_in_schema=False)
def console_ui() -> HTMLResponse:
    """整个控制台是这一个文件，所以它**绝不能被缓存**。

    踩过的坑：服务器升级到新版后，律所方手机上的页面仍是旧的——报错文案、
    新按钮全是老样子，看起来就像「升级没生效」。浏览器（尤其 iOS Safari）
    对没有缓存头的 HTML 会自作主张地缓存，而这个页面每次升级都在变。
    自动升级把发版频率抬高之后，这个坑必然天天踩。
    """
    return HTMLResponse(
        _UI_FILE.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )


@ui_router.get("/g/{group_id:path}", include_in_schema=False)
def conversation_deeplink(group_id: str) -> HTMLResponse:
    """交接单里那条「看完整对话」。

    原本用的是 `/ui#g=<会话id>`，在企业微信里点开返回 `{"detail":"Not Found"}`
    ——**片段标识符 `#` 被客户端转义成了 `%23`**，于是服务器看到的路径变成
    `/ui%23g=...`，自然 404。这类问题在浏览器里测不出来（浏览器不会转义 `#`），
    只有在真机上点一次才会现形。

    换成一段普通路径就没有可被转义的特殊字符了。页面本身仍是同一个文件，
    只是把会话 id 交给前端去打开。
    """
    # json.dumps 会转义引号，但**不转义 `</script>`**——URL 里带上它就能
    # 跳出脚本块执行任意代码。这条路径是从企微消息里点进来的，
    # 谁都可以构造一条链接发给律师。`<` 一律转义掉。
    safe = json.dumps(group_id, ensure_ascii=False).replace("<", "\\u003c")
    html = _UI_FILE.read_text(encoding="utf-8").replace(
        "</head>", f"<script>window.__deepLinkGroup = {safe};</script></head>", 1,
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )


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


_LOCALHOST = {"127.0.0.1", "::1", "localhost", "testclient"}
# 客资表上限：抖音一次导出几百行不过几百 KB，10MB 足够宽松又能挡住误传
_MAX_IMPORT_BYTES = 10 * 1024 * 1024


# 连续输错的锁定窗口。有了它，令牌才允许是「记得住的一句话」而不是一串乱码：
# 在线爆破每 15 分钟只能试 8 次，一句十二位的短语就已经远远够用。
# 没有它，为了扛住每秒几千次的猜测，就只能逼律所抄一串随机字符——
# 而那串字符最后一定会被抄进某个记事本里，反而更不安全。
_MAX_FAILS, _LOCK_SECONDS = 8, 900
_fails: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """请求方 IP。**默认不信 X-Forwarded-For。**

    原来无条件采信 XFF 的第一段，于是每次换一个头就换一个「IP」，
    锁定形同虚设——而「令牌可以是一句记得住的话」这个决定，
    正是以这道锁挡住在线爆破为前提的。护栏没了，十二位口令挡不住
    每秒几千次的猜测，而门后是全部客户咨询原文和一个能拉代码重启的接口。

    只有显式配了 `trusted_proxy_hops` 才解析 XFF，且**从右往左数**——
    右边那几跳是自己的代理写的，左边是客户端自称的，采信左边等于不设防。
    """
    s = getattr(request.app.state, "pipeline", None)
    hops = getattr(getattr(s, "settings", None), "trusted_proxy_hops", 0) or 0
    if hops > 0:
        parts = [p.strip() for p in
                 request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.client.host if request.client else "?"


# 全站失败计数：IP 也能换（僵尸网络、代理池），所以另设一道与 IP 无关的闸。
# 正常使用打不到这个数——一个所里的人一天输错几次令牌到头了。
_GLOBAL_MAX_FAILS = 60
_global_fails: list[float] = []


def _check_lock(request: Request) -> None:
    now = time.time()
    hits = [t for t in _fails.get(_client_ip(request), []) if now - t < _LOCK_SECONDS]
    _fails[_client_ip(request)] = hits
    if len(hits) >= _MAX_FAILS:
        wait = int((_LOCK_SECONDS - (now - hits[0])) / 60) + 1
        raise HTTPException(429, f"连续输错太多次，请 {wait} 分钟后再试")
    _global_fails[:] = [t for t in _global_fails if now - t < _LOCK_SECONDS]
    if len(_global_fails) >= _GLOBAL_MAX_FAILS:
        raise HTTPException(429, "登录尝试过于频繁，请稍后再试")


def _note_fail(request: Request) -> None:
    _fails.setdefault(_client_ip(request), []).append(time.time())
    _global_fails.append(time.time())


def get_principal(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> Principal:
    """解析请求身份。管理员令牌未配置时**仅本机**放行为管理员。

    未配置就全放行是 fail-open：公网上任何人都能读全部客户会话原文，
    还能调 /console/update（拉代码重启＝服务器代码执行）。
    """
    supplied = x_admin_token or ""
    admin_token = request.app.state.pipeline.settings.admin_token
    _check_lock(request)
    if not admin_token:
        host = request.client.host if request.client else ""
        if host in _LOCALHOST:
            return Principal(role="admin", name="本机开发")
        raise HTTPException(401, "服务未配置访问令牌，仅允许本机访问")
    if hmac.compare_digest(supplied, admin_token):
        _fails.pop(_client_ip(request), None)
        return Principal(role="admin", name="管理员")
    if supplied:
        law = request.app.state.store.get_lawyer_by_token_hash(_hash_token(supplied))
        if law is not None:
            _fails.pop(_client_ip(request), None)
            return Principal(
                role="admin" if law["role"] == "admin" else "lawyer",
                userid=law["userid"],
                name=law["name"] or law["userid"],
            )
    _note_fail(request)
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
    """律师视角的会话范围：承办律师是自己，或线索派给了自己。

    走 SQL 的 UNION（两条窄查询各吃一个索引）——这个集合在律师身份下
    几乎每个请求都要算一次，不能每次都把群表和 2000 条线索拉进内存。
    """
    return store.own_group_ids(p.userid)


@router.get("/me")
def me(request: Request, p: Principal = Depends(get_principal)):
    """登录探针 + 前端角色开关的唯一依据（数据隔离仍在各端点服务端强制）。"""
    _remember_base_url(request)
    return {"role": p.role, "userid": p.userid, "name": p.name}


def _remember_base_url(request: Request) -> None:
    """首次有人从公网打开控制台时，把这个地址记下来当作对外基础地址。

    为什么要自动记：交接单末尾的「看完整对话」深链、律师登录链接，都需要知道
    服务器的对外地址。它此前只能人工写进 .env——而运维侧未必够得着这台机器，
    结果就是链接一直发不出去。控制台被访问到的那个地址，恰恰就是律师能打开的
    那个地址，直接采信它即可。

    只认非本机地址：管理员从 127.0.0.1 调试时记下来的地址，别人一个都打不开。
    """
    s = request.app.state.pipeline.settings
    if s.public_base_url:
        return
    host = (request.headers.get("host") or "").strip()
    if not host or host.split(":")[0] in ("localhost", "127.0.0.1", "::1"):
        return
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    base = f"{scheme}://{host}"
    s.public_base_url = base
    persist_setting("RESPONDER_PUBLIC_BASE_URL", base)
    logger.info("public_base_url auto-detected: %s", base)


@router.get("/todo")
def todo_queue(
    store: Store = Depends(get_store), p: Principal = Depends(get_principal)
):
    """AI 承接后等待人工跟进的问题，按紧急度排序。律师只看发给自己的。"""
    return store.pending_reminders(None if p.is_admin else p.userid)


def _get_reminder_scoped(store: Store, p: Principal, reminder_id: int) -> dict:
    """待办的写操作同样要判归属：否则任何律师令牌都能关掉别人的加急待办。"""
    row = store.get_reminder(reminder_id)
    if row is None:
        raise HTTPException(404, "待办不存在")
    if not p.is_admin and row.get("to_userid") != p.userid:
        raise HTTPException(404, "待办不存在")  # 越权按不存在处理，不泄露存在性
    return row


@router.post("/todo/{reminder_id}/done")
def resolve_todo(
    reminder_id: int,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    _get_reminder_scoped(store, p, reminder_id)
    store.set_reminder_status(reminder_id, "done")
    return {"ok": True}


@router.post("/todo/{reminder_id}/reopen")
def reopen_todo(
    reminder_id: int,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """撤销「已处理」（控制台 5 秒撤销机制的回滚接口）。"""
    _get_reminder_scoped(store, p, reminder_id)
    store.set_reminder_status(reminder_id, "pending")
    return {"ok": True}


@router.get("/leads")
def leads(
    status: str | None = None, limit: int = 60, assigned: str | None = None,
    q: str | None = None, priority: str | None = None, source: str | None = None,
    offset: int = 0,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """线索队列：按 P0/P1/P2 与评分排序（先打该打的电话）。律师只看派给自己的。

    带 total 供分页 UI 说「共 N 条」——导入一次抖音客资就是 350+ 条，
    没有检索与分页的列表在这个量级上等于不可用。
    """
    if not p.is_admin:
        assigned = p.userid
    kw = dict(q=q, priority=priority, source=source)
    return {
        "total": store.count_leads(status, assigned, **kw),
        "items": store.list_leads(status, limit, assigned, offset=offset, **kw),
    }


def _get_lead_scoped(store: Store, p: Principal, lead_id: int) -> dict:
    row = store.get_lead_by_id(lead_id)
    if row is None:
        raise HTTPException(404, "线索不存在")
    # 越权按不存在处理，不向律师泄露他人线索的存在性
    if not p.is_admin and row.get("assigned_userid") != p.userid:
        raise HTTPException(404, "线索不存在")
    return row


class LeadNotes(BaseModel):
    notes: str


@router.post("/leads/{lead_id}/notes")
def set_lead_notes(
    lead_id: int, body: LeadNotes,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """跟进备注：打完电话记两句，下次跟进不从零开始。律师只能写自己的单。"""
    _get_lead_scoped(store, p, lead_id)
    store.set_lead_notes(lead_id, body.notes.strip()[:2000])
    return {"ok": True}


@router.post("/leads/assign-unrouted")
def assign_unrouted(
    request: Request,
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    """把所有未指派的在办线索按规则批量分派（专长匹配 + 负载均衡）。

    典型场景：先导入了几百条客资、后建的律师名册——存量线索不会自动补派
    （派单只在新消息触达时发生），这里一键补齐。只动 new/contacted，
    已成交/无效不折腾；全程不推送企微消息，律师在工作台里看得到即可。
    """
    from responder import assignment

    done, skipped = 0, 0
    for status in ("new", "contacted"):
        for row in store.list_leads(status=status, limit=5000, assigned_userid=""):
            group = store.get_group(row["group_id"])
            if group is None:
                skipped += 1
                continue
            law = assignment.pick(store, row.get("case_type") or group.case_type)
            if law is None:
                # 名册为空/无人在班：直接结束，不用把剩下几百条都试一遍
                return {"ok": True, "assigned": done, "skipped": skipped,
                        "hint": "名册为空或无人在班，未能分派"}
            assignment.assign(store, group, row["group_id"], law)
            done += 1
    return {"ok": True, "assigned": done, "skipped": skipped, "hint": ""}


class LeadStatus(BaseModel):
    status: str  # new | contacted | converted | invalid


@router.post("/leads/{lead_id}/status")
def set_lead_status(
    lead_id: int, body: LeadStatus,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    if body.status not in ("new", "contacted", "converted", "invalid"):
        raise HTTPException(400, "无效的线索状态")
    row = _get_lead_scoped(store, p, lead_id)
    store.set_lead_status(lead_id, body.status)
    # 待办与线索是同一件事的两个视图：人已经联系过了，还让督办去升级
    # 第二责任人纯属制造无效打扰。标记非「待跟进」时一并了结该会话的待办。
    closed = 0
    if body.status != "new":
        closed = store.resolve_reminders_for_group(row["group_id"])
    return {"ok": True, "todos_closed": closed}


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

    # **改派必须把微信客服会话本身也搬过去。** 原来这里只改了我们库里的
    # assigned_userid 再给新律师推一张单——而企微那边的会话状态一动没动，
    # 于是新律师的「微信客服」工作台里根本看不到这个人，老律师那边倒还在。
    # 单子看着交接完了，客户实际上还挂在原来那个人名下：又一个「看起来正常」。
    moved, move_hint = _move_kf_session(request, store, group, law["userid"])

    sender = request.app.state.pipeline.sender
    fresh = store.get_lead(row["group_id"])
    notified = False
    if sender:
        notified = sender.send_direct_text(
            law["userid"], "【改派给您】\n" + lead_mod.format_notification(fresh, group)
        )
        if notified:
            store.mark_lead_notified(row["group_id"])
    # 明确回报有没有真的通知到：影子模式/发送失败下静默返回 ok
    # 会让管理员以为交接完成，实际上新负责人什么都没收到
    hint = "" if notified else (
        "影子模式未推送交接单，请自行知会" if sender is None
        else "企微推送失败，请自行知会"
    )
    if move_hint:
        hint = (hint + "；" if hint else "") + move_hint
    return {
        "ok": True,
        "assigned_userid": law["userid"],
        "notified": bool(notified),
        "session_moved": moved,
        "hint": hint,
    }


def _move_kf_session(request: Request, store: Store, group, userid: str):
    """把微信客服会话真正转到新律师名下。返回 (是否搬成功, 给管理员看的提示)。

    只对**已经在人工接待中**的会话做：还在 AI 手上的会话本就没有归属，
    强行转过去反而会让 AI 当场闭嘴，而新律师未必这会儿就要接。
    """
    if not (group.is_kf and not group.is_douyin and group.handoff_userid):
        return False, ""
    if group.handoff_userid == userid:
        return True, ""
    pipeline = request.app.state.pipeline
    client = getattr(pipeline, "kf_client", None)
    if client is None or not client.available():
        return False, "微信客服通道未配置，会话未搬动，新律师工作台看不到这个人"
    try:
        if userid not in set(client.servicer_list(group.kf_open_kfid)):
            return False, f"{userid} 不是该客服账号的接待人，会话搬不过去（去「状态」页一键补）"
    except Exception:
        logger.exception("servicer check failed: %s", group.group_id)
        return False, "接待人名单查不到，会话未搬动"
    if not client.transfer(group.kf_open_kfid, group.kf_external_userid, userid):
        return False, "企微拒绝了会话转接，会话仍在原律师名下"
    store.set_handoff(group.group_id, userid)
    logger.info("kf session moved: %s → %s", group.group_id, userid)
    return True, ""


@router.post("/leads/import")
async def import_leads(
    request: Request, filename: str = "", notify: bool = False,
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    """导入平台客资表（抖音来客「客资中心」导出的 CSV/XLSX）。

    文件以原始字节 POST 上传（避开 multipart 依赖），notify 默认 false——
    一次导入几十条历史客资还挨个推律师是骚扰，复核后再按需单条推送。
    """
    from starlette.concurrency import run_in_threadpool

    from responder import importer

    data = await request.body()
    if not data:
        raise HTTPException(400, "没有收到文件内容")
    if len(data) > _MAX_IMPORT_BYTES:
        raise HTTPException(413, "文件过大（上限 10MB），请分批导出后再上传")

    def _run() -> dict:
        table = importer.parse_table(data, filename)
        return importer.import_leads(
            store, table,
            settings=request.app.state.pipeline.settings,
            sender=request.app.state.pipeline.sender,
            notify=notify,
        )

    try:
        # 必须离开事件循环：几百行的解析+逐行入库是纯同步 CPU/IO，
        # 直接在 async 端点里跑会把整个服务（含企微回调）卡住数秒
        result = await run_in_threadpool(_run)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, **result}


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
    changed, errors = [], []
    for row in store.list_groups():
        if not row.get("kf_open_kfid") or row.get("lawyer_userid"):
            continue
        kfid = row["kf_open_kfid"]
        if kfid not in cache:
            # 某个账号查失败不该让整轮回填 500 掉、把已改的一半留在中间态
            try:
                cache[kfid] = kf_client.servicer_list(kfid)
            except Exception:
                logger.exception("servicer_list failed: %s", kfid)
                cache[kfid] = []
                errors.append(kfid)
        target = cache[kfid][0] if cache[kfid] else settings.default_notify_userid
        if not target:
            continue
        g = store.get_group(row["group_id"])
        g.lawyer_userid = target
        if len(cache[kfid]) > 1 and not g.backup_userid:
            g.backup_userid = cache[kfid][1]
        store.upsert_group(g)
        changed.append({"group_id": g.group_id, "to": target})
    return {"ok": True, "updated": changed, "errors": errors}


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
    """全量判断日志，含「AI 判断为无需响应」的沉默日志，便于复盘误判。

    律师范围下推到 SQL：先 LIMIT 再内存过滤会让忙时段的律师翻到整页空白
    （他的记录被别人的挤出了窗口），而且永远翻不到自己的历史。
    """
    own = None if p.is_admin else sorted(_own_group_ids(store, p))
    return store.list_decisions(group_id, limit, group_ids=own)


@router.get("/replies")
def replies(
    group_id: str | None = None, limit: int = 200,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    own = None if p.is_admin else sorted(_own_group_ids(store, p))
    return store.list_replies(group_id, limit, group_ids=own)


class Feedback(BaseModel):
    feedback: str  # good | needs_fix:<备注>


@router.post("/replies/{reply_id}/feedback")
def reply_feedback(
    reply_id: int, body: Feedback,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """话术反馈闭环：标记「好/需修正」，修正样本进入话术迭代库。"""
    fb = body.feedback.strip()
    if fb != "good" and not fb.startswith("needs_fix"):
        raise HTTPException(400, "反馈只能是 good 或 needs_fix[:备注]")
    row = store.get_reply(reply_id)
    if row is None:
        raise HTTPException(404, "回复记录不存在")
    if not p.is_admin and row["group_id"] not in _own_group_ids(store, p):
        raise HTTPException(404, "回复记录不存在")
    store.set_reply_feedback(reply_id, fb[:500])
    return {"ok": True}


@router.get("/groups")
def groups(store: Store = Depends(get_store), p: Principal = Depends(get_principal)):
    if p.is_admin:
        return store.list_groups()
    own = _own_group_ids(store, p)
    return [g for g in store.list_groups() if g["group_id"] in own]


@router.put("/groups/{group_id}")
def upsert_group(
    group_id: str, profile: GroupProfile,
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    if profile.group_id != group_id:
        raise HTTPException(400, "group_id 不一致")
    existing = store.get_group(group_id)
    if existing is not None:
        # **只允许改编辑表单上的那几项，其余一律从库里原样保留。**
        #
        # 这里踩过一个能把会话彻底打死的坑：前端只发 10 个字段，而这个端点
        # 收的是完整 GroupProfile，缺的字段全部取默认空串写回库。于是管理员
        # 在「会话」页改一下承办律师、点保存，就把 kf_open_kfid /
        # kf_external_userid / douyin_open_id / ext_channel / ext_user_id
        # 一起抹了——`is_kf` 变假，发送层退回「往一个不存在的群发消息」，
        # **客户从此一句回复也收不到，而控制台里判断和回复照常入库、看着一切正常**。
        # 建档逻辑对已存在的档案直接 return，所以它永远不会自愈。
        # handoff_userid 被抹掉更直接：正在人工接待的会话，AI 当场回来抢话。
        #
        # 白名单而不是黑名单：以后再加渠道字段，不会又漏一次。
        editable = {
            "name", "client_status", "case_type", "case_stage",
            "lawyer_name", "lawyer_userid", "backup_userid",
            "ai_enabled", "robot_webhook",
        }
        profile = existing.model_copy(
            update={k: v for k, v in profile.model_dump().items() if k in editable}
        )
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
    if body.mode not in ("shadow", "live"):
        raise HTTPException(400, "mode 只能是 shadow 或 live")
    settings = request.app.state.pipeline.settings
    settings.mode = body.mode
    persisted = persist_setting("RESPONDER_MODE", body.mode)
    return {"ok": True, "mode": body.mode, "persisted": persisted}


class TokenChange(BaseModel):
    token: str


# 律所名相关的词：这些是攻击者会试的第一批，长度再够也不能用
_WEAK_WORDS = ("songhu", "松沪", "songhulaw", "lawfirm", "12345678", "password", "admin")


@router.post("/admin-token")
def set_admin_token(
    body: TokenChange, request: Request, _: Principal = Depends(require_admin)
):
    """把访问令牌改成一句记得住的话。即时生效并写回 .env。

    为什么允许改成「短语」而不是坚持一串随机字符：那串随机字符最后一定会被
    抄进某个记事本或聊天记录里，反而更不安全。配合上面的连续输错锁定
    （15 分钟最多 8 次），一句十二位的短语在线上已经猜不动了。

    但两条底线不让：不能取消（这台机器在公网上，控制台里是全部客户咨询原文，
    升级按钮等于服务器代码执行），不能用律所名 + 数字（那是第一个被试的）。
    """
    t = (body.token or "").strip()
    if len(t) < 12:
        raise HTTPException(400, "至少 12 位。可以用一句话，比如 songhu-jiufeng-88")
    low = t.lower()
    if any(w in low for w in _WEAK_WORDS) and len(t) < 16:
        raise HTTPException(400, "含律所名/常见词的令牌请至少 16 位，或换个词")
    if t.isdigit() or t.isalpha():
        raise HTTPException(400, "别用纯数字或纯字母，掺个「-」或数字就行")

    settings = request.app.state.pipeline.settings
    settings.admin_token = t
    persisted = persist_setting("RESPONDER_ADMIN_TOKEN", t)
    _fails.clear()
    logger.info("admin token changed by %s", _client_ip(request))
    return {"ok": True, "persisted": persisted}


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


def _douyin_diag(s, store: Store) -> dict:
    """抖音私信自检。

    这条通道有个别处没有的静默失败：客户超过 24 小时没说话、或本轮配额打满时，
    发送接口会直接拒。真正要暴露的是「有几个会话已经发不出去了」——
    只看凭据配没配，看不出这个。
    """
    convos = [g for g in store.list_groups() if g.get("douyin_open_id")]
    configured = bool(s.douyin_client_key and s.douyin_client_secret)
    return {
        "configured": configured,
        "enabled": s.douyin_enabled,
        "signed_callback": bool(s.douyin_callback_token),
        "conversations": len(convos),
        "hint": (
            "未配置抖音应用凭据（open.douyin.com 权限过审后回填，见 docs/douyin.md）"
            if not configured
            else "回调未配校验 Token：任何人都能伪造客户消息灌进来，公网部署必须配"
            if not s.douyin_callback_token
            else "还没收到过任何抖音私信（确认回调地址已在开发者后台配好并通过验证）"
            if not convos
            else ""
        ),
    }



def _kf_accounts_in_use(store: Store) -> set[str]:
    """真正有客户从这里进来的客服账号。

    企微会把**企业名下所有**客服账号都列出来，其中很可能有跟我们无关的
    （真机 2026-08-09：律所另有一个「上海松沪律师事务所在线客服」，
    人工在接，从没交给自建应用管）。对那种账号做任何管理动作都会拿到
    48007，于是一个碰不着的账号把整块自检染成红色——
    **说错「坏了」和漏报一样贵**，人会跑去修一个没坏的东西。

    还没有任何会话时（刚上线）退回「全都算」，否则第一天什么都检查不了。
    """
    used = {g.get("kf_open_kfid") for g in store.list_groups() if g.get("kf_open_kfid")}
    return used


@router.get("/kf/handoff-probe")
def handoff_probe(request: Request, _: Principal = Depends(require_admin)):
    """会话转接就绪自检（只读，不改任何东西）。

    做成控制台端点而不是命令行脚本，是因为企微 API 受可信 IP 限制、只有服务器
    调得通，而律所侧没有 SSH。点一下按钮即可，返回结果直接贴给开发看。

    最要紧的一项是**名册与接待人的差集**：律师不在客服账号的接待人列表里，
    转接接口会直接失败，而它失败的时机恰恰是 P0 线索来的那一刻。
    """
    store: Store = request.app.state.store
    s = request.app.state.pipeline.settings
    client = getattr(request.app.state.pipeline, "_kf_client", None)
    if client is None or not client.available():
        return {"ready": False, "error": "微信客服未配置（RESPONDER_WECOM_KF_SECRET）"}

    accounts = client.account_list()
    if not accounts:
        return {"ready": False, "error": "取不到客服账号：检查 Secret 与企微可信 IP"}

    roster = {law["userid"] for law in store.list_lawyers(active_only=True)
              if law.get("userid")}
    names = {law["userid"]: law.get("name", "") for law in store.list_lawyers()}
    used = _kf_accounts_in_use(store)
    out, ready = [], bool(roster)
    for a in accounts:
        kfid = a.get("open_kfid", "")
        # 没有任何客户从这个账号进来过 = 不是我们这条链路上的账号，不参与判绿判红
        in_use = kfid in used if used else True
        raw = client.servicer_raw(kfid)
        servicers = {x["userid"] for x in (raw.get("servicer_list") or [])
                     if x.get("userid")}
        missing = sorted(roster - servicers)
        if missing and in_use:
            ready = False
        out.append({
            "open_kfid": kfid,
            "name": a.get("name", ""),
            "servicers": sorted(servicers),
            "missing": [{"userid": u, "name": names.get(u, "")} for u in missing],
            "raw": raw if not servicers else None,  # 取不到接待人时才回原始返回
            "hint": kf_errors.err_hint(raw.get("error", "") or raw),
            "in_use": in_use,
        })
    state = _probe_state_endpoint(store, client, s)
    if state.get("error"):
        ready = False
    return {
        "ready": ready,
        "enabled": s.handoff_enabled,
        "mode": s.mode,
        "triggers": [label for _, label in priority.WANTS_HUMAN],
        "accounts": out,
        "roster_size": len(roster),
        "reclaim_seconds": s.handoff_reclaim_seconds,
        "state_probe": state,
        "hint": (
            "律师名册为空：先在「团队」页添加律师，转接才有对象"
            if not roster
            else "有律师不是接待人，转接会失败——到企微后台把他们加进「接待人员」"
            if not ready and any(a["missing"] for a in out)
            else f"接口路径探测失败：{state.get('error', '')}"
            if state.get("error")
            else "接待人齐、接口通，转接的前置条件已满足"
            if state.get("ok")
            else "接待人齐；接口路径还没验过（等一通真实客服会话进来后再点一次）"
        ),
    }


@router.post("/kf/servicers/add")
def add_kf_servicers(request: Request, _: Principal = Depends(require_admin)):
    """把名册里的律师批量加为客服账号的接待人（转接的硬前置）。

    为什么由程序代劳：这个客服账号是企微应用托管的，kf.weixin.qq.com 顶部横幅
    「正在通过企业微信应用管理相关能力」即指此事——在那个后台点「开始使用」会把
    管理权夺回网页侧，打断消息推送，代价远大于收益。既然程序有权限，就程序来加。

    幂等：已经是接待人的再加一次也无妨；加完立刻回读一次列表，
    以**回读结果**为准报成功与否，不信写接口自己说的话。
    """
    store: Store = request.app.state.store
    client = getattr(request.app.state.pipeline, "_kf_client", None)
    if client is None or not client.available():
        raise HTTPException(400, "微信客服通道未配置")

    userids = [law["userid"] for law in store.list_lawyers(active_only=True)
               if law.get("userid")]
    if not userids:
        raise HTTPException(400, "律师名册为空：先在「团队」页添加律师")
    accounts = client.account_list()
    if not accounts:
        raise HTTPException(400, "取不到客服账号：检查 Secret 与企微可信 IP")

    used = _kf_accounts_in_use(store)
    out, skipped = [], []
    for a in accounts:
        kfid = a.get("open_kfid", "")
        if used and kfid not in used:
            # 别去动一个没有客户从中进来的账号：多半根本没交给我们管，
            # 加不上是正常的，报红反而误导人去修一个没坏的东西
            skipped.append(a.get("name", "") or kfid)
            continue
        raw = client.servicer_add(kfid, userids)
        after = set(client.servicer_list(kfid))
        out.append({
            "open_kfid": kfid,
            "name": a.get("name", ""),
            "added": sorted(set(userids) & after),
            "failed": sorted(set(userids) - after),
            "error": raw.get("error", ""),
            # 48007/60030 这类错误码对律所侧等于乱码，翻成一句能照着点的中文
            "hint": raw.get("hint", "") or kf_errors.err_hint(raw),
        })
    ok = bool(out) and all(not a["failed"] for a in out)
    return {"ok": ok, "accounts": out, "roster": userids, "skipped": skipped}


def _probe_state_endpoint(store: Store, client, s) -> dict:
    """拿一通真实会话读一次会话状态，验证 kf_state_path / kf_trans_path 配得对不对。

    只读，不改任何状态。为什么必须探：企微文档站在开发环境不可达，这两个路径是
    照着记忆写的配置项；写错的话平时一切正常，直到 P0 线索来的那一刻转接失败。
    读接口与转接口同属 `kf/service_state/*`，读得通即可判定前缀正确。
    """
    convo = next(
        (g for g in store.list_groups()
         if g.get("kf_open_kfid") and g.get("kf_external_userid")),
        None,
    )
    if not convo:
        return {"ok": False, "reason": "还没有任何客服会话可供探测"}
    try:
        raw = client.post_raw(s.kf_state_path, {
            "open_kfid": convo["kf_open_kfid"],
            "external_userid": convo["kf_external_userid"],
        })
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "path": s.kf_state_path}
    return {
        "ok": True,
        "path": s.kf_state_path,
        "trans_path": s.kf_trans_path,
        "service_state": raw.get("service_state"),
        "servicer_userid": raw.get("servicer_userid", ""),
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
    store = request.app.state.store
    # 进线事件计数：进线即问候这条链路完全依赖企微推送 enter_session 事件。
    # 事件一条都没收到过，问候就永远不会发——而现象（「客户进来没人打招呼」）
    # 和「代码没生效」长得一模一样。有这个数就能一眼分清是哪一种。
    kf["enter_events"] = store.count_event_messages()
    kf["welcome_on_enter"] = s.kf_welcome_on_enter
    if kf["ok"] and not kf["enter_events"]:
        kf["hint"] = (
            "从没收到过进线事件：企微只在会话被接入智能助手后才推送，"
            "客户扫码那一刻可能不推。开场白改由「客户第一条消息」兜底触发。"
        )
    # 没有提醒接收人的会话＝线索生成了也没人知道，属静默失败，必须显式暴露
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
        "douyin": _douyin_diag(s, store),
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


@router.get("/kf/qrcode")
def kf_qrcode(
    request: Request, open_kfid: str, scene: str = "qrcode",
    _: Principal = Depends(require_admin),
):
    """客户进线二维码（PNG）。本地生码，链接不经过任何第三方服务。

    客户扫这个码直接进咨询窗口——**不是加好友**，无需通过验证、无需占用
    员工个人微信。印名片/易拉宝/朋友圈图都用它。
    """
    import io

    import segno
    from fastapi.responses import Response as RawResponse

    data = kf_contact_link(request, open_kfid, scene, _)
    url = data.get("url") or ""
    if not url:
        raise HTTPException(502, "企微未返回进线链接，请稍后重试")
    buf = io.BytesIO()
    # 高容错等级：印在物料上被 logo 或折痕遮住一角仍可扫
    segno.make(url, error="h").save(
        buf, kind="png", scale=12, border=3, dark="#0F2C4C", light="#FFFFFF"
    )
    return RawResponse(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": 'inline; filename="kf-qrcode.png"'},
    )


@router.get("/conversation")
def conversation(
    group_id: str, limit: int = 60,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """会话原文——人工复核的依据。没有它，律师无法判断 AI 那句话回得对不对。"""
    if not p.is_admin and group_id not in _own_group_ids(store, p):
        raise HTTPException(404, "会话不存在")
    return store.recent_messages(group_id, limit)


def resolve_range(name: str, now: datetime | None = None) -> tuple[str, str, str]:
    """把「今天 / 本月 / 全部」翻成 (since, until, 中文标签)。

    管理员看的是「今天怎么样」「这个月怎么样」。全时段累计数只在开张第一天
    有意义——之后它只会越来越像一个常数，看不出好坏，也就没人再看。
    """
    now = now or datetime.now()
    if name == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat(), now.isoformat(), "今天"
    if name == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat(), now.isoformat(), f"{now.month} 月"
    if name == "7d":
        return (now - timedelta(days=7)).isoformat(), now.isoformat(), "近 7 天"
    return "", "", "全部"


@router.get("/metrics")
def metrics(
    range: str = "today",
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """看板：三分类分布、承接量、合规拦截，以及线索转化漏斗（业务侧最关心）。

    律师身份下所有数字都只统计自己名下的会话与线索；管理员额外拿到分律师负载表。
    """
    own = None if p.is_admin else sorted(_own_group_ids(store, p))
    # SQL 聚合而非拉一万行进 Python：超过一万条后内存聚合会被 LIMIT 静默截断，
    # 合规看板从此说谎（「拦截 0 次」可能只是没统计到）
    since, until, label = resolve_range(range)
    dec = store.decision_stats(group_ids=own)
    rep = store.reply_stats(group_ids=own)
    lead_agg = store.lead_stats(
        assigned_userid=None if p.is_admin else p.userid, since=since, until=until,
    )
    out = {
        "scope": "all" if p.is_admin else "mine",
        "range": range,
        "range_label": label,
        "decisions_total": dec["total"],
        "by_action": dec["by_action"],
        "urgent_count": dec["urgent"],
        "replies_total": rep["total"],
        "compliance_blocked": rep["blocked"],
        "feedback_good": rep["good"],
        "feedback_needs_fix": rep["bad"],
        "leads_total": lead_agg["total"],
        "leads_with_contact": lead_agg["with_contact"],
        "leads_by_status": lead_agg["by_status"],
        "leads_hot": lead_agg["hot"],
        "leads_p0": lead_agg["p0"],
        "leads_by_source": lead_agg["by_source"],
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
        # 只数「在办且未指派」——把已成交/无效也算进来会让批量分派按钮
        # 的数字虚高，点完还不消失
        out["leads_unassigned"] = lead_agg["unassigned"]
        # 管理员真正要看的那张表：谁分到几单、跟进了几单、多久才跟上。
        # 「在办数」只说明此刻手上压着多少，说明不了做得怎么样。
        out["staff"] = store.staff_performance(since=since, until=until)
    return out


# ================================================================ 知识库
class KnowledgeIn(BaseModel):
    question: str
    answer: str
    tags: str = ""
    source: str = "manual"


@router.get("/knowledge")
def list_knowledge(
    status: str = "", store: Store = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """知识库条目。hits 排在前面——用不上的条目该被清掉，而不是越攒越多。

    每条带上 `flagged`＝这条答案踩了哪些禁止事项。抖音那批现成话术里
    「电话咨询免费」之类比比皆是，逐条人眼看是看不出来的（看得出也会看漏）。
    标出来，管理员才知道该先改哪几条。
    """
    items = store.list_knowledge(status=status or None)
    for it in items:
        it["flagged"] = forbidden.check(it.get("answer", ""))
    return {
        "items": items,
        "counts": {
            s: len(store.list_knowledge(status=s))
            for s in ("draft", "approved", "retired")
        },
    }


@router.post("/knowledge")
def add_knowledge(
    body: KnowledgeIn, store: Store = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    kid = store.add_knowledge(
        body.question, body.answer, tags=body.tags, source=body.source,
    )
    if kid is None:
        raise HTTPException(400, "问题和答案都不能为空")
    return {"ok": True, "id": kid}


@router.post("/knowledge/{kid}/status")
def set_knowledge_status(
    kid: int, body: dict, store: Store = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """审核开关。**只有 approved 会被 AI 引用**——导入与机器提炼都落 draft。

    这不是流程洁癖：知识库条目就是话术，而话术须人工审核后才能对客户生效
    （CLAUDE.md 合规护栏）。一条写错的口径会被 AI 一字不差地重复几百遍。
    """
    status = str(body.get("status", "")).strip()
    if status not in ("draft", "approved", "retired"):
        raise HTTPException(400, "状态只能是 draft / approved / retired")
    if status == "approved":
        # 通过审核前先过一遍出口闸门。放行了也不会真发出去（guard 在出口还会拦），
        # 但那时的表现是 AI 的回答被整段丢掉换成兜底话术——客户看到的是
        # 一句答非所问的套话，而没有人知道原因出在知识库某一条上。
        item = store.get_knowledge(kid)
        if item is None:
            raise HTTPException(404, "条目不存在")
        hits = forbidden.check(item.get("answer", ""))
        if hits:
            raise HTTPException(
                400, f"这条答案踩了禁止事项（{'、'.join(hits)}），请先改写措辞再通过",
            )
    store.set_knowledge_status(kid, status)
    return {"ok": True}


class KnowledgeEdit(BaseModel):
    question: str
    answer: str
    tags: str = ""


@router.put("/knowledge/{kid}")
def edit_knowledge(
    kid: int, body: KnowledgeEdit, store: Store = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """改写条目。改完退回 draft（见 `Store.update_knowledge`）。"""
    if not store.update_knowledge(
        kid, question=body.question, answer=body.answer, tags=body.tags,
    ):
        raise HTTPException(400, "条目不存在，或问题与答案不能为空")
    return {"ok": True, "flagged": forbidden.check(body.answer)}


@router.delete("/knowledge/{kid}")
def delete_knowledge(
    kid: int, store: Store = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    store.delete_knowledge(kid)
    return {"ok": True}


@router.post("/knowledge/import")
async def import_knowledge(
    request: Request, source: str = "douyin",
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    """批量导入问答（CSV / TSV / 每行「问题<TAB>答案」）。

    抖音「自动回复—问答知识」导出的就是这个形状。一律落 draft：
    那 70 条是给抖音写的，语气和口径未必适用于微信侧，得人过一遍。
    """
    raw = (await request.body()).decode("utf-8-sig", errors="replace")
    if not raw.strip():
        raise HTTPException(400, "没有收到内容")
    added, skipped, flagged = 0, 0, 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        # 制表符优先：问答里逗号很常见，按逗号切会把答案切碎
        parts = line.split("\t") if "\t" in line else line.split(",", 1)
        if len(parts) < 2:
            skipped += 1
            continue
        q, a = parts[0].strip().strip('"'), parts[1].strip().strip('"')
        if q in ("问题", "标准问题", "question") or not q or not a:
            skipped += 1  # 表头或残行
            continue
        if store.add_knowledge(q, a, source=source, status="draft"):
            added += 1
            if forbidden.check(a):
                flagged += 1
        else:
            skipped += 1
    return {"ok": True, "added": added, "skipped": skipped, "flagged": flagged}


class TakeOver(BaseModel):
    userid: str = ""  # 留空 = 转给操作者自己


@router.post("/groups/{group_id}/takeover")
def take_over(
    group_id: str, body: TakeOver, request: Request,
    store: Store = Depends(get_store), p: Principal = Depends(get_principal),
):
    """把这通会话转给律师人工接待——律师由此进入客户和 AI 聊天的那个窗口。

    自动转接只在 P0/紧急时触发（`service._maybe_handoff`），但律师常常是
    看完交接单**自己判断**这单该接。没有这个按钮，他就只能打电话——
    而打电话正是整条链路上最脆的一环，也正是转接要取代的东西。

    转接之后：会话出现在他企业微信的「微信客服」工作台，历史消息齐全，
    他直接回复即可；AI 由 `gate:handed-off` 自动闭嘴，不会抢话。
    """
    pipeline = request.app.state.pipeline
    group = store.get_group(group_id)
    if group is None:
        raise HTTPException(404, "会话不存在")
    if not p.is_admin and group_id not in _own_group_ids(store, p):
        raise HTTPException(404, "会话不存在")
    target = (body.userid or p.userid or "").strip()
    if not target:
        raise HTTPException(400, "请指定接手的律师（管理员操作需要选人）")
    if not group.kf_open_kfid:
        raise HTTPException(400, "只有微信客服会话支持转接（抖音侧走官方接待）")

    client = getattr(pipeline, "kf_client", None)
    if client is None or not client.available():
        raise HTTPException(400, "微信客服未配置，或当前是影子模式")
    # 接待人校验放在给客户发话之前：话发出去客户就真的在等了
    try:
        if target not in set(client.servicer_list(group.kf_open_kfid)):
            raise HTTPException(
                400, "该律师不是这个客服账号的接待人，企微会拒绝转接。"
                     "请在「状态」面板点「把名册律师加为接待人」",
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("servicer check failed: %s", group_id)
        raise HTTPException(400, "取接待人列表失败，请稍后重试") from None

    from responder.compliance.guard import guard
    from responder.models import Action
    from responder.reply import templates

    checked = guard(
        templates.handing_over(seed=group_id), Action.HANDOFF,
        templates.safe_fallback(group),
    )
    pipeline._send_group(group, group_id, checked.text)
    if not client.transfer(group.kf_open_kfid, group.kf_external_userid, target):
        raise HTTPException(400, "企微拒绝了转接，请检查接待人配置后重试")
    store.set_handoff(group_id, target)
    store.save_reply(
        f"takeover-{group_id}-{int(time.time())}", group_id, checked.text,
        "live", checked.passed, category="handoff",
    )
    logger.info("manual takeover: %s → %s", group_id, target)
    return {"ok": True, "userid": target}


@router.get("/export/leads")
def export_leads(
    request: Request, range: str = "today", fmt: str = "xlsx",
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    """把这一段的客户档案导成 Excel（管理员专用）。

    为什么这是管理员的主视图而不是附加功能：控制台是给「在系统里干活的人」
    用的——律师看自己的单、点已联系。所主任要的是另一样东西：一份能打印、
    能转发、能在会上过一遍的表。让他为了看昨天进了多少客户去点开网页、
    翻列表，他不会每天做。

    AI 对话不进表（律所方原话：「不想看到那么多 AI 对话，那么乱」），
    只留一列深链——要看原文点一下就到。
    """
    from fastapi.responses import Response

    from responder import exporter

    since, until, label = resolve_range(range)
    rows = exporter.build_rows(
        store, store.leads_in_range(since or None, until or None),
        settings=request.app.state.pipeline.settings,
    )
    stamp = datetime.now().strftime("%Y%m%d")
    name = f"客户档案-{label}-{stamp}"
    if fmt == "csv":
        body, mime, ext = exporter.to_csv(rows), "text/csv; charset=utf-8", "csv"
    else:
        try:
            body, mime, ext = (
                exporter.to_xlsx(rows, title=label),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "xlsx",
            )
        except ValueError:
            # 服务器缺 openpyxl 时给 CSV 而不是报错——管理员要的是那份表，
            # 不是一句「导出失败」
            body, mime, ext = exporter.to_csv(rows), "text/csv; charset=utf-8", "csv"
    filename = quote(f"{name}.{ext}")
    return Response(
        content=body, media_type=mime,
        headers={
            # 中文文件名走 RFC 5987，否则浏览器存成一串乱码
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Row-Count": str(max(len(rows) - 1, 0)),
        },
    )


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
    """登录链接的基础地址：显式配置优先，否则按请求本身推断。

    协议一律沿用请求实际用的那个（nginx 反代时读 X-Forwarded-Proto）——
    早先无条件推断 https，在 http://IP 直连部署下会签发出打不开的链接。
    """
    configured = request.app.state.pipeline.settings.public_base_url.rstrip("/")
    if configured:
        return configured
    host = request.headers.get("host", "")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    return f"{scheme}://{host}" if host else ""


def _issue_token(store: Store, userid: str) -> str:
    """签发新令牌并使旧令牌立即失效（轮换即注销）。"""
    token = secrets.token_urlsafe(24)
    store.set_lawyer_token_hash(userid, _hash_token(token))
    return token


@router.get("/diagnose")
def diagnose(
    group_id: str, request: Request,
    store: Store = Depends(get_store), _: Principal = Depends(require_admin),
):
    """这通对话为什么没回复？一句人话说清楚。

    存在的理由：AI 不说话有十几种可能（没收到消息、开关关了、影子模式、
    转人工了、通道断了、发送失败…），而它们**在客户那头长得一模一样**——
    一片空白。没有这个自检，每次都要靠人肉猜 + 来回截图，一轮几分钟。
    """
    group = store.get_group(group_id)
    if group is None:
        return {"ok": False, "verdict": "这个会话在库里根本不存在——消息没进来。"}
    s = request.app.state.pipeline.settings
    msgs = store.recent_messages(group_id, 5)
    last_customer = next(
        (m for m in reversed(msgs) if not m.get("sender_is_staff")), None
    )
    decisions = store.list_decisions(group_id, limit=1)
    last = decisions[0] if decisions else None

    checks, blockers = [], []
    if s.mode != "live":
        blockers.append("现在是**影子模式**，AI 只写草稿不发言。到「状态」页切成正式模式。")
    if not group.ai_enabled:
        blockers.append("这个会话的 **AI 开关是关的**。在「会话」页把它打开。")
    if group.is_kf and not group.is_douyin:
        kf = getattr(request.app.state.pipeline, "kf_client", None)
        if kf is None or not kf.available():
            blockers.append("**微信客服通道没配好**，回复发不出去。")
        else:
            checks.append("微信客服通道正常")
    if group.handoff_userid:
        checks.append(f"这通对话已转给 {group.handoff_userid} 人工接待")
    # 「为什么没自动转给律师」——转接有六个前提，缺一个就静默回落。
    # 静默正是最贵的部分：律所方只能问「怎么会没转呢」，而我们只能一条条猜。
    # `_maybe_handoff._skip` 把原因写在这儿，此处照搬出来。
    skip = store.get_note(f"handoff_skip:{group_id}")
    if skip and not group.handoff_userid:
        checks.append(f"没有自动转给律师，因为：{skip}")
    # 企微那边的会话归属才是决定性的：状态不是「智能助手接待」时，
    # 我们判断得再对、回复生成得再好，客户也看不到。
    if group.is_kf and not group.is_douyin:
        kf = getattr(request.app.state.pipeline, "kf_client", None)
        if kf is not None and kf.available():
            try:
                st = kf.service_state(group.kf_open_kfid, group.kf_external_userid)
            except Exception:
                st = None
            names = {0: "未处理（没人在接！）", 1: "智能助手接待（正常）",
                     2: "待接入人工池", 3: "人工接待中", 4: "已结束"}
            if st is not None:
                line = f"企微会话状态：{names.get(st, st)}"
                if st in (0, 2, 4):
                    blockers.append(
                        f"**{line}** —— 这种状态下客户发什么都没人接，"
                        "点一下上面的「收回给 AI」把它要回来。")
                else:
                    checks.append(line)
    if last_customer is None:
        blockers.append("**库里没有客户消息**——说明消息压根没送到我们这儿，"
                        "多半是回调或拉取断了。")
    # 把两种长得一样、修法完全不同的故障分开：
    # 「这一通卡住了」（别人还在正常进线）vs「整条回调断了」（谁发都收不到）。
    if group.is_kf and not group.is_douyin and group.kf_open_kfid:
        last_any = store.last_inbound_at(f"kf:{group.kf_open_kfid}:")
        if last_any is None:
            blockers.append("**这个客服账号从来没收到过任何客户消息**——"
                            "回调地址、Token、可信 IP 里至少有一项没配通。")
        else:
            mins = (datetime.now() - last_any).total_seconds() / 60
            when = last_any.strftime("%m-%d %H:%M")
            if last_customer is None and mins < 60:
                blockers.append(
                    f"**别人的消息进得来（最近一条 {when}），唯独这通进不来**——"
                    "问题在这一通会话上，不是通道。")
            else:
                checks.append(f"这个客服账号最近收到客户消息：{when}"
                              f"（{int(mins)} 分钟前）")
    if last is None:
        blockers.append("**没有任何判断记录**——消息进来了但没被处理，"
                        "后台工作线程可能已经停了，请点一次「升级到最新版」重启。")

    reasons = []
    if last:
        try:
            reasons = json.loads(last.get("reasons") or "[]")
        except (ValueError, TypeError):
            reasons = []
    why = _explain_reasons(reasons, last)
    return {
        "ok": True,
        "verdict": blockers[0] if blockers else why,
        "blockers": blockers,
        "checks": checks,
        "last_customer_message": (last_customer or {}).get("content", ""),
        "last_decision": {
            "action": (last or {}).get("action", ""),
            "should_speak": bool((last or {}).get("should_speak")),
            "reasons": reasons,
            "at": (last or {}).get("created_at", ""),
        } if last else None,
    }


_REASON_ZH = {
    "gate:ai-disabled": "这个会话的 AI 开关被关了",
    "gate:handed-off": "已转人工接待，AI 主动让开（律师最近说过话）",
    "gate:human-takeover": "律师刚在这通对话里说过话，AI 暂时让开",
    "gate:waiting": "在等承办律师先回（群聊补位策略），到点会自动重判",
    "handoff:no-show": "转过去没人接手，AI 已经接回来继续陪",
    "handoff:reclaimed": "转接超时无人接手，已收回给 AI",
    "staff-message": "这条是我方发的，不需要 AI 回",
    "chitchat-fastpath": "客户只发了句寒暄，AI 按规则不接话",
    "courtesy": "客户在道谢，AI 不接话",
    "default-silence": "规则没认出这是需要回答的问题（这类通常是词表该补了）",
    "non-text-or-empty": "这条不是文字消息（图片/语音），AI 读不了",
    "at-mention": "这条 @ 了具体的人，AI 不插话",
    "send:failed": "**回复生成了但没发出去**——通道有问题",
}


def _explain_reasons(reasons: list, last: dict | None) -> str:
    if not last:
        return "还没有判断记录。"
    for r in reasons:
        for key, zh in _REASON_ZH.items():
            if r.startswith(key):
                return zh
    if last.get("should_speak"):
        return "判断是要回复的——如果客户没收到，说明卡在发送这一步。"
    return f"被这些原因拦下了：{'、'.join(reasons) or '（无）'}"


@router.post("/groups/{group_id}/release")
def release_to_ai(
    group_id: str, request: Request, store: Store = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """把会话收回给 AI（撤销「已转人工」）。

    存在的理由是**手上得有个开关**：转接是自动发生的，而它一旦卡住，
    客户那头看到的是一个死掉的窗口——发什么都没人应。等超时回收是三十分钟，
    对一个正在打字的客户来说太久了。这个按钮让人当场把它救回来。
    """
    group = store.get_group(group_id)
    if group is None:
        raise HTTPException(404, "会话不存在")
    store.set_handoff(group_id, "")
    # 光清我们自己的标记不够——**会话归属在企微那边**。
    # 状态还停在「人工接待」或「已结束」的话，AI 说什么都到不了客户眼前。
    hint = ""
    kf = getattr(request.app.state.pipeline, "kf_client", None)
    if group.is_kf and not group.is_douyin and kf is not None and kf.available():
        if not kf.to_robot(group.kf_open_kfid, group.kf_external_userid):
            hint = "已收回，但企微那边的会话状态没改过来，可能还是接不上"
    logger.info("released back to AI: %s", group_id)
    return {"ok": True, "hint": hint}


@router.post("/groups/{group_id:path}/forget")
def forget_group(
    group_id: str, request: Request, store: Store = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """把这个客户彻底忘掉——像他从没来过一样，用来反复跑测试。

    存在的理由很实在：**回访客户那条路径，用一个新微信号是永远测不到的。**
    每跑一次测试换一个号，测到的全是「新客户」；而跨会话记忆、二次问候、
    再推送判据这些只在老客户身上发生，没有这个按钮就只能等上线以后撞。

    建档保留（案由、承办律师、AI 开关是人配的，不该被一次测试清掉）。
    企微那边的会话归属也要一并要回来——只清我们库里的是假动作，
    会话还挂在「人工接待」或「已结束」上，下次扫码进来照样没人接。
    """
    group = store.get_group(group_id)
    if group is None:
        raise HTTPException(404, "会话不存在")
    # 已委托客户不给清：那是台账，不是测试数据。删掉之后没有任何地方能还原，
    # 而「我以为是测试号」是这类误删最常见的开场白。
    if group.client_status == ClientStatus.SIGNED:
        raise HTTPException(
            400,
            "这是已委托客户，聊天记录属于案件台账，不能清空。"
            "确实要清的话，先在「会话」页把客户状态改成「咨询客户」。",
        )
    counts = store.forget_group(group_id)
    hint = ""
    kf = getattr(request.app.state.pipeline, "kf_client", None)
    if group.is_kf and not group.is_douyin and kf is not None and kf.available():
        if not kf.to_robot(group.kf_open_kfid, group.kf_external_userid):
            hint = "本地记录已清空，但企微那边的会话状态没改回来，下次进来可能还是没人接"
    logger.info("group forgotten (test reset): %s — %s", group_id, counts)
    return {"ok": True, "deleted": counts, "hint": hint}


@router.delete("/lawyers/{userid}")
def delete_lawyer(
    userid: str, store: Store = Depends(get_store),
    _: Principal = Depends(require_admin),
):
    """把律师从名册里移除。

    **名下还有在办线索的不给删** ——删掉之后那些线索的负责人就成了一个
    查无此人的 id：交接单推不出去、督办找不到人、看板上那一列是空的，
    而没有任何地方会告诉你为什么。想让他不再接单，用「停用」；
    真要删，先把他手上的单改派出去。
    """
    if store.get_lawyer(userid) is None:
        raise HTTPException(404, "该律师不存在")
    open_leads = [
        row for row in store.leads_in_range(None, None)
        if row.get("assigned_userid") == userid and row.get("status") != "done"
    ]
    if open_leads:
        raise HTTPException(
            400,
            f"他名下还有 {len(open_leads)} 单在办，删了这些线索就没人认领了。"
            "请先改派，或改用「停用」（保留历史归属，只是不再接新单）",
        )
    store.delete_lawyer(userid)
    return {"ok": True}


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
