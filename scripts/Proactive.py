"""Keep the existing Windows launcher aligned with the owner's QQ switches."""

import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

base = "http://127.0.0.1:6185/api/v1"
credentials = json.loads(Path("D:/QQBot/config/bootstrap.json").read_text(encoding="utf-8-sig"))


def call(method, path, body=None, token=""):
    """Call the authenticated local dashboard without printing credentials.

    Args:
        method: HTTP method.
        path: API path relative to the dashboard API.
        body: Optional JSON body.
        token: Local dashboard bearer token.

    Returns:
        Parsed response data.
    """
    request = Request(base + path, method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


try:
    auth = call("POST", "/auth/login", {"username": "astrbot", "password": credentials["astrbot_password"]})["data"]["token"]
    action = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    prefix = "/plugins/extensions/feiyu_steward/"
    if action in {"on", "enable", "off", "disable"}:
        result = call("POST", prefix + "settings", {"proactive": action in {"on", "enable"}}, auth)
        print(result.get("message", "设置已更新。"))
    else:
        state = call("GET", prefix + "status", token=auth)["data"]
        print("主动私聊：" + ("开" if state["proactive"] else "关"))
        print("有趣才分享，主动消息合计至少间隔30分钟；02:00–08:00、忙碌和最近连续聊天时安静。")
        print("空间：" + ("自动" if state["auto_publish"] else "审核草稿"))
        print("当前勿扰：" + ("开，QQ发 /继续聊 恢复" if state["dnd"] else "关"))
except (OSError, KeyError, ValueError):
    print("机器人面板暂未就绪，请先启动机器人后再试。")
    sys.exit(1)
