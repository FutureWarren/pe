"""微信客服通道：拉取客户消息（sync_msg）与回复客户（send_msg）。

与自建应用回调的区别（决定了本模块存在的必要性）：
  - 自建应用回调收不到客户群聊内容，微信客服会话则「每条都推」，客户无需 @ 触发。
  - 客服接口用的是**独立的「微信客服」Secret**（后台 → 客户与上下游 → 微信客服 → API），
    与应用 Secret 不同，故本模块自持一份 access_token 缓存。

收消息是两段式：企微回调只给一个 Token（10 分钟有效）→ 用它调 sync_msg 拉真实消息，
游标 next_cursor 必须持久化（见 store.kf_cursors），否则重启后会重复拉取历史消息。

任何异常都不抛给回调链路：失败记日志并返回空，由控制台待办与人工兜底。
"""

import logging
import re
import time

import httpx

from responder.config import Settings, get_settings

logger = logging.getLogger(__name__)

_API = "https://qyapi.weixin.qq.com/cgi-bin"

# sync_msg 返回的 origin 语义
ORIGIN_CUSTOMER = 3  # 客户发的
ORIGIN_SYSTEM = 4  # 系统推送（欢迎语/事件等）
ORIGIN_SERVICER = 5  # 接待人员（真人客服/律师）发的 → 触发人工接管

# 客户扫码/点链接进入会话的事件类型。企微在不同版本里用过这几个名字，
# 全都认——认漏一个的后果是客户进来后对着空窗口，没人打招呼。
ENTER_EVENTS = ("enter_session", "user_enter_session", "enter_chat")

# 会话状态机（见 docs/kf-handoff.md）。AI 工作时会话停在 1，
# 转成 3 并指定 servicer_userid，这通会话就出现在那位律师的客服工作台里。
STATE_UNHANDLED = 0
STATE_ROBOT = 1
STATE_POOL = 2
STATE_HUMAN = 3
STATE_ENDED = 4



# 企微错误码 → 一句能照着做的中文。
# 存在的理由：律所侧看到的是「48007 api forbidden for no kfid privilege」，
# 那对他们等于一串乱码——于是每一次都变成「发截图给开发」。
# 这几个码是这条链上真正会撞到的，每一个都对应一个具体的后台动作。
ERR_HINTS: dict[int, str] = {
    48007: (
        "这个客服账号还没交给我们的应用管理。去企业微信管理后台 → 应用管理 → "
        "「微信客服」→ 右上角「API」→ 在「通过 API 管理微信客服账号-企业内部开发」里"
        "勾上这个客服账号。（勾了之后就不能再用微信客服网页后台新建账号了）"
    ),
    48002: (
        "应用没有微信客服的接口权限。管理后台 → 应用管理 → 「微信客服」→ "
        "「API」→ 「可调用接口的应用」里选中我们的自建应用。"
    ),
    60011: "没有操作这个成员的权限——他多半不在自建应用的可见范围里。",
    60030: (
        "接待人不在应用的可见范围里。管理后台 → 应用管理 → 我们的自建应用 → "
        "「可见范围」把这个人加进去。"
    ),
    301002: "没有该成员的权限（可见范围问题）。",
}


def err_hint(payload: dict | str) -> str:
    """从企微返回里挑出 errcode，翻成一句能照着做的中文；认不出就空串。"""
    code = None
    if isinstance(payload, dict):
        code = payload.get("errcode")
    else:
        m = re.search(r"'errcode':\s*(\d+)", str(payload))
        code = int(m.group(1)) if m else None
    try:
        return ERR_HINTS.get(int(code), "") if code is not None else ""
    except (TypeError, ValueError):
        return ""


class KfClient:
    """微信客服 API 客户端。sync 在任何模式下都要工作（收），send 由管道按模式门控（发）。"""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._token: str = ""
        self._token_expiry: float = 0.0

    def available(self) -> bool:
        return bool(self.settings.wecom_corp_id and self.settings.wecom_kf_secret)

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        resp = httpx.get(
            f"{_API}/gettoken",
            params={
                "corpid": self.settings.wecom_corp_id,
                "corpsecret": self.settings.wecom_kf_secret,
            },
            timeout=10,
        ).json()
        if resp.get("errcode"):
            raise RuntimeError(f"kf gettoken failed: {resp}")
        self._token = resp["access_token"]
        self._token_expiry = time.time() + resp.get("expires_in", 7200) - 120
        return self._token

    def _post(self, path: str, payload: dict) -> dict:
        resp = httpx.post(
            f"{_API}/{path}",
            params={"access_token": self._access_token()},
            json=payload,
            timeout=15,
        ).json()
        if resp.get("errcode"):
            raise RuntimeError(f"kf {path} failed: {resp}")
        return resp

    def _get(self, path: str, params: dict) -> dict:
        """部分客服接口是 GET + 查询参数（如 servicer/list），不能用 POST body。"""
        resp = httpx.get(
            f"{_API}/{path}",
            params={"access_token": self._access_token(), **params},
            timeout=15,
        ).json()
        if resp.get("errcode"):
            raise RuntimeError(f"kf {path} failed: {resp}")
        return resp

    def post_raw(self, path: str, payload: dict) -> dict:
        """直调客服接口（控制台自检/生成入口链接等用途），异常向上抛。"""
        return self._post(path, payload)

    # ------------------------------------------------------------ 收
    def sync_msg(self, token: str, open_kfid: str, cursor: str = "", limit: int = 1000) -> dict:
        """拉取一批消息。返回 {msg_list, next_cursor, has_more}；失败返回空批次。"""
        payload = {"token": token, "limit": limit}
        if cursor:
            payload["cursor"] = cursor
        if open_kfid:
            payload["open_kfid"] = open_kfid
        try:
            data = self._post("kf/sync_msg", payload)
        except Exception:
            logger.exception("kf sync_msg error (open_kfid=%s)", open_kfid)
            return {"msg_list": [], "next_cursor": cursor, "has_more": 0}
        return {
            "msg_list": data.get("msg_list") or [],
            "next_cursor": data.get("next_cursor") or cursor,
            "has_more": data.get("has_more", 0),
        }

    def servicer_raw(self, open_kfid: str) -> dict:
        """接待人接口的原始返回（诊断用：接待人取不到时要能看清是为什么）。"""
        try:
            return self._get("kf/servicer/list", {"open_kfid": open_kfid})
        except Exception as e:
            logger.exception("kf servicer/list error (open_kfid=%s)", open_kfid)
            return {"error": str(e)[:200]}

    def servicer_list(self, open_kfid: str) -> list[str]:
        """该客服账号的接待人 userid 列表——即「谁该收到这个会话的提醒」。

        status 字段各版本语义不一（0/1 都出现过表示可接待），因此只要有 userid
        就收下——宁可多一个候选人，也不要因为字段语义变化导致提醒无人可收。
        """
        data = self.servicer_raw(open_kfid)
        return [s["userid"] for s in (data.get("servicer_list") or []) if s.get("userid")]

    def servicer_add(self, open_kfid: str, userids: list[str]) -> dict:
        """把律师加为该客服账号的接待人。返回原始结果（含逐人 errcode）。

        为什么要做成 API 而不是让律所自己去后台点：账号一旦交给应用托管，
        kf.weixin.qq.com 顶部就会挂「正在通过企业微信应用管理相关能力」，
        接待人在那个网页后台反而点不了；而点「开始使用」会把管理权夺回网页侧，
        打断消息推送。既然程序有权限，就由程序加，律所侧一个按钮的事。

        **前提是那个账号真的托管给了应用**（管理后台 → 应用管理 → 微信客服 →
        API → 「通过 API 管理微信客服账号-企业内部开发」勾上它）。没勾的话
        这里会拿到 48007 `no kfid privilege`——`err_hint` 会把它翻成
        那句能照着点的中文，别让人对着错误码发愣。
        """
        if not userids:
            return {"error": "没有可添加的 userid"}
        try:
            return self._post(
                "kf/servicer/add", {"open_kfid": open_kfid, "userid_list": userids}
            )
        except Exception as e:
            logger.exception("kf servicer/add error (open_kfid=%s)", open_kfid)
            return {"error": str(e)[:300], "hint": err_hint(str(e))}

    def account_list(self) -> list[dict]:
        """客服账号列表（部署自检用）。"""
        try:
            return self._post("kf/account/list", {"offset": 0, "limit": 100}).get(
                "account_list", []
            )
        except Exception:
            logger.exception("kf account/list error")
            return []

    # ------------------------------------------------------------ 转接
    def service_state(self, open_kfid: str, external_userid: str) -> int | None:
        """读会话当前状态；取不到返回 None（不阻断业务，按「未知」处理）。"""
        try:
            data = self._post(
                self.settings.kf_state_path,
                {"open_kfid": open_kfid, "external_userid": external_userid},
            )
        except Exception:
            logger.exception("kf service_state get error")
            return None
        v = data.get("service_state")
        return int(v) if v is not None else None

    def to_robot(self, open_kfid: str, external_userid: str) -> bool:
        """把会话要回给「智能助手接待」（service_state=1）。

        **这是整条链上一个长期缺失的动作。** 我们只会把会话转给人工
        （`transfer` → state 3），却从来没有把它要回来过：
        一旦客户被转过人工、或者会话被企微判成「已结束」，
        状态就停在 3 或 4，新消息进来是「未处理」（state 0）——
        而未处理的会话没有任何人在接，**客户发什么都石沉大海**。

        真机现象正是如此：同一个号码，转过一次人工之后，
        无论隔多久、发多少句「你好」，AI 一个字都不回。
        我们这边的判断日志看着一切正常——因为问题根本不在判断层，
        在企微那边的会话归属上。
        """
        try:
            self._post(
                self.settings.kf_trans_path,
                {
                    "open_kfid": open_kfid,
                    "external_userid": external_userid,
                    "service_state": STATE_ROBOT,
                },
            )
            return True
        except Exception:
            logger.exception("kf to_robot failed (%s)", open_kfid)
            return False

    def transfer(
        self, open_kfid: str, external_userid: str, servicer_userid: str
    ) -> bool:
        """把会话转给指定律师人工接待。失败返回 False，由调用方回落原链路。

        失败最常见的原因是**该律师不是这个客服账号的接待人**——企微直接拒绝。
        所以调用方应先校验名册与接待人的交集（控制台自检会把差集报出来）。
        """
        if not servicer_userid:
            return False
        try:
            self._post(
                self.settings.kf_trans_path,
                {
                    "open_kfid": open_kfid,
                    "external_userid": external_userid,
                    "service_state": STATE_HUMAN,
                    "servicer_userid": servicer_userid,
                },
            )
            return True
        except Exception:
            logger.exception(
                "kf transfer failed (%s → %s)", open_kfid, servicer_userid
            )
            return False

    # ------------------------------------------------------------ 发
    def send_text(self, open_kfid: str, external_userid: str, text: str) -> bool:
        try:
            self._post(
                "kf/send_msg",
                {
                    "touser": external_userid,
                    "open_kfid": open_kfid,
                    "msgtype": "text",
                    "text": {"content": text},
                },
            )
            return True
        except Exception:
            logger.exception("kf send_msg error (open_kfid=%s)", open_kfid)
            return False
