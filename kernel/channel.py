"""通知与事件接口 —— oncall 的通用抽取。

把飞书剥掉后，oncall 剩下的通用骨架就是：
    事件源(EventSource) → 病例建档 → 证据驱动诊断 → 处置 → 通知渠道(Notifier)

飞书只占 EventSource 和 Notifier 两个接口的一种实现。开源版提供 stdout/webhook
参考实现；企业内部 IM 挂对应实现；社区挂 GitHub Issues / PagerDuty / Sentry。
抽出来的不是 oncall，是"事件驱动的诊断闭环"。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import List, Protocol, runtime_checkable


@dataclass
class Event:
    """一个待诊断的事件（告警/工单/crash 聚类等）。"""
    id: str
    title: str
    body: str = ""
    source: str = ""
    meta: dict = field(default_factory=dict)


@runtime_checkable
class EventSource(Protocol):
    """事件源接口：拉取待处理事件。"""
    def poll(self) -> List[Event]: ...


@runtime_checkable
class Notifier(Protocol):
    """通知渠道接口：把结论发出去。"""
    def send(self, title: str, body: str) -> bool: ...


# ---- 开源参考实现 ----

class StdoutNotifier:
    """最简通知：打到 stdout。任何环境都能用，零依赖。"""
    def send(self, title: str, body: str) -> bool:
        print(f"\n== 通知：{title} ==\n{body}\n", file=sys.stdout)
        return True


class WebhookNotifier:
    """通用 webhook 通知。Slack/Discord/自建都能接。"""
    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout

    def send(self, title: str, body: str) -> bool:
        payload = json.dumps({"title": title, "text": body}).encode("utf-8")
        req = urllib.request.Request(self.url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False


class StaticEventSource:
    """参考事件源：从一个静态列表出事件（测试/演示/CI 用）。"""
    def __init__(self, events: List[Event]) -> None:
        self._events = list(events)

    def poll(self) -> List[Event]:
        out, self._events = self._events, []
        return out
