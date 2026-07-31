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

    def account_list(self) -> list[dict]:
        """客服账号列表（部署自检用）。"""
        try:
            return self._post("kf/account/list", {"offset": 0, "limit": 100}).get(
                "account_list", []
            )
        except Exception:
            logger.exception("kf account/list error")
            return []

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
