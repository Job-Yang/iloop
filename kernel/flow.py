"""协议 3：flow schema —— 任务路由与自治分级。

内核按 when_keywords 匹配任务，按 autonomy 决定放权。
插件 flow 只增不覆盖内核 flow（flow_id 必须带命名空间前缀防覆盖）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


class Autonomy(str, Enum):
    L1 = "L1"  # 只看不改
    L2 = "L2"  # 动手改：最小改动+编译+验证+可回滚
    L3 = "L3"  # 放手干：需授权+清单+验收标准


@dataclass
class Flow:
    flow_id: str
    name: str
    autonomy: Autonomy
    when_keywords: List[str] = field(default_factory=list)
    guidance: str = ""
    required_docs: List[str] = field(default_factory=list)
    evidence_strategy: str = ""
    escalate_when: str = ""
    next_suggest: str = ""   # 收口时"主动引导下一步"的建议话术

    def __post_init__(self) -> None:
        if isinstance(self.autonomy, str):
            self.autonomy = Autonomy(self.autonomy)

    def score(self, task: str) -> int:
        t = task.lower()
        return sum(1 for kw in self.when_keywords if kw.lower() in t)


class FlowRegistry:
    """内置 flow + 插件 flow 合并加载。插件不得覆盖已存在的 flow_id。"""

    def __init__(self) -> None:
        self._flows: dict[str, Flow] = {}

    def register(self, flow: Flow, *, allow_override: bool = False) -> None:
        if flow.flow_id in self._flows and not allow_override:
            raise ValueError(
                f"flow_id '{flow.flow_id}' already registered; "
                "plugin flows must use a namespace prefix to avoid clobbering core"
            )
        self._flows[flow.flow_id] = flow

    def load_json(self, path: str | Path, *, allow_override: bool = False) -> int:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("flows", [])
        for item in items:
            self.register(Flow(**item), allow_override=allow_override)
        return len(items)

    def all(self) -> List[Flow]:
        return list(self._flows.values())

    def plan(self, task: str) -> Optional[Flow]:
        """按 when_keywords 命中数选最匹配的 flow；没命中返回 None。"""
        ranked = sorted(self._flows.values(), key=lambda f: f.score(task), reverse=True)
        if ranked and ranked[0].score(task) > 0:
            return ranked[0]
        return None
