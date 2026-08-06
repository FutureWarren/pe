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
        # 最近一次发送失败的企微原始错误（码 + 文案）。运维侧够不着服务器日志，
        # 这个字段是「消息没送到」唯一能被远程看见的证据。
        self.last_error: str = ""

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
                # 错误码必须能被人看见。发失败以前只进日志，而律所侧没有服务器——
                # 现象就只剩下「什么都没收到」，跟「功能没上线」分不开。
                # 企微这几个码含义天差地别（60011 没权限 / 81013 不在应用可见范围
                # / 40056 agentid 不对），看到码就知道该改哪儿。
                self.last_error = f"{path}: {resp.get('errcode')} {resp.get('errmsg', '')}"[:200]
                return False
            self.last_error = ""
            return True
        except Exception as e:
            logger.exception("wecom send error: %s", path)
            self.last_error = f"{path}: {str(e)[:160]}"
            return False

    def send_group_text(self, chat_id: str, text: str) -> bool:
        """向客户群发送文本（appchat.send）。"""
        return self._post(
            "appchat/send",
            {"chatid": chat_id, "msgtype": "text", "text": {"content": text}},
        )

    def send_robot_text(self, webhook: str, text: str) -> bool:
        """通过群机器人 webhook 发言（AI 以群成员「销售顾问」身份出现时的首选通道）。

        webhook 可传完整 URL 或仅 key。无需 access_token。
        """
        url = (
            webhook
            if webhook.startswith("http")
            else f"{_API}/webhook/send?key={webhook}"
        )
        try:
            resp = httpx.post(
                url, json={"msgtype": "text", "text": {"content": text}}, timeout=10
            ).json()
            if resp.get("errcode"):
                logger.error("robot send failed: %s", resp)
                return False
            return True
        except Exception:
            logger.exception("robot send error")
            return False

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
