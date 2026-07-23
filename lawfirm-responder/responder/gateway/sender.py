"""企微发送通道：群内回复（应用消息到群聊）与律师单聊提醒。

access_token 内存缓存；发送失败不抛出到回调链路（记录后由控制台待办兜底）。
"""

import logging
import time

import httpx

from responder.config import Settings, get_settings

logger = logging.getLogger(__name__)

_API = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComSender:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._token: str = ""
        self._token_expiry: float = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        resp = httpx.get(
            f"{_API}/gettoken",
            params={
                "corpid": self.settings.wecom_corp_id,
                "corpsecret": self.settings.wecom_corp_secret,
            },
            timeout=10,
        ).json()
        if resp.get("errcode"):
            raise RuntimeError(f"gettoken failed: {resp}")
        self._token = resp["access_token"]
        self._token_expiry = time.time() + resp.get("expires_in", 7200) - 120
        return self._token

    def _post(self, path: str, payload: dict) -> bool:
        try:
            resp = httpx.post(
                f"{_API}/{path}",
                params={"access_token": self._access_token()},
                json=payload,
                timeout=10,
            ).json()
            if resp.get("errcode"):
                logger.error("wecom send failed: %s %s", path, resp)
                return False
            return True
        except Exception:
            logger.exception("wecom send error: %s", path)
            return False

    def send_group_text(self, chat_id: str, text: str) -> bool:
        """向客户群发送文本（appchat.send）。"""
        return self._post(
            "appchat/send",
            {"chatid": chat_id, "msgtype": "text", "text": {"content": text}},
        )

    def send_direct_text(self, userid: str, text: str) -> bool:
        """单聊推送承办律师/客服（message.send）。"""
        return self._post(
            "message/send",
            {
                "touser": userid,
                "msgtype": "text",
                "agentid": self.settings.wecom_agent_id,
                "text": {"content": text},
            },
        )
