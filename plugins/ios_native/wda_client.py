"""WebDriverAgent 客户端 —— 真机 UI 自动化（Appium 社区版 WDA，纯开源栈）。

WDA 在设备上跑一个 HTTP server，经 iproxy 转发到 127.0.0.1:8100。
本客户端只用标准库 urllib，零第三方依赖。

真机 UI 层级 / 截图 / 点击 / 滑动 / 输入 全部走这里的 REST 调用。
签名走本机 Xcode（Apple Development + -allowProvisioningUpdates），无私有服务。
"""

from __future__ import annotations

import base64
import json
import urllib.request
from typing import Optional, Tuple


class WDAClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8100", timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._session: Optional[str] = None

    def _req(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def status(self) -> dict:
        """健康检查：WDA 是否在线。"""
        return self._req("GET", "/status")

    def session(self, bundle_id: Optional[str] = None) -> str:
        cap = {"capabilities": {"alwaysMatch": {}}}
        if bundle_id:
            cap["capabilities"]["alwaysMatch"]["bundleId"] = bundle_id
        resp = self._req("POST", "/session", cap)
        self._session = resp.get("sessionId") or resp.get("value", {}).get("sessionId")
        return self._session

    def _sid(self) -> str:
        if not self._session:
            self.session()
        return self._session or ""

    def source(self) -> dict:
        """UI 层级树（view hierarchy），JSON 格式。"""
        return self._req("GET", f"/session/{self._sid()}/source?format=json")

    def screenshot_png(self) -> bytes:
        """截图，返回 PNG 字节。"""
        resp = self._req("GET", "/screenshot")
        return base64.b64decode(resp.get("value", ""))

    def tap(self, x: float, y: float) -> dict:
        return self._req("POST", f"/session/{self._sid()}/wda/tap/0", {"x": x, "y": y})

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration: float = 0.5) -> dict:
        return self._req("POST", f"/session/{self._sid()}/wda/dragfromtoforduration",
                         {"fromX": x1, "fromY": y1, "toX": x2, "toY": y2, "duration": duration})

    def type_text(self, text: str) -> dict:
        return self._req("POST", f"/session/{self._sid()}/wda/keys", {"value": list(text)})


def element_center(bounds: dict) -> Tuple[float, float]:
    """从 WDA 元素 bounds(rect) 算几何中心，供 tap 定位。"""
    x = bounds.get("x", 0)
    y = bounds.get("y", 0)
    w = bounds.get("width", 0)
    h = bounds.get("height", 0)
    return (x + w / 2.0, y + h / 2.0)
