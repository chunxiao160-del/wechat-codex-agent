import json
import socket
import urllib.error
import urllib.request

from .constants import CHANNEL_VERSION, LONG_POLL_TIMEOUT_MS
from .state import load_account
from .util import random_wechat_uin


class WechatClient:
    def __init__(self):
        self._account = None

    def get_account(self):
        self._account = load_account()
        if not self._account or not self._account.get("token"):
            raise RuntimeError("未找到微信登录凭据，请先运行 `npm run setup` 或设置 BOT_TOKEN")
        return self._account

    def _build_headers(self, body):
        account = self.get_account()
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {account['token']}",
            "X-WECHAT-UIN": random_wechat_uin(),
            "Content-Length": str(len(body.encode("utf-8"))),
        }

    def _request_json(self, url, body, timeout_s):
        request = urllib.request.Request(
            url=url,
            method="POST",
            data=body.encode("utf-8"),
            headers=self._build_headers(body),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raise RuntimeError(f"HTTP {err.code}") from err

    def get_updates(self, get_updates_buf=""):
        account = self.get_account()
        body = json.dumps(
            {
                "get_updates_buf": get_updates_buf,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
            ensure_ascii=False,
        )
        try:
            return self._request_json(
                f"{account['baseUrl']}/ilink/bot/getupdates",
                body,
                timeout_s=(LONG_POLL_TIMEOUT_MS + 5_000) / 1000,
            )
        except (TimeoutError, socket.timeout, urllib.error.URLError) as err:
            reason = getattr(err, "reason", err)
            if isinstance(reason, socket.timeout) or isinstance(err, TimeoutError):
                return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}
            raise

    def send_message(self, context_token, text):
        account = self.get_account()
        body = json.dumps({"context_token": context_token, "content": text}, ensure_ascii=False)
        self._request_json(
            f"{account['baseUrl']}/ilink/bot/sendmessage",
            body,
            timeout_s=15,
        )


def extract_text(msg):
    item_list = msg.get("item_list") or []
    for item in item_list:
        if item.get("type") == 1:
            text_item = item.get("text_item") or {}
            text = text_item.get("text")
            if text:
                ref = item.get("ref_msg")
                if not ref:
                    return text
                parts = []
                title = ref.get("title")
                if title:
                    parts.append(title)
                return f"[引用: {' | '.join(parts)}]\n{text}" if parts else text
        if item.get("type") == 3:
            voice_item = item.get("voice_item") or {}
            if voice_item.get("text"):
                return voice_item["text"]
    return ""
