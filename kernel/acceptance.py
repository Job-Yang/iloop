"""独立验收 —— "谁做的不能由谁判"（VDD 守则 2）。

按改动影响面/风险触发，不按任务大小：
  - 低影响面：主 Agent 自核，不起独立验收
  - 拿不准：建议用户上验收
  - 风险极高（支付/下单/鉴权/崩溃热点/数据写入/签名）：必须起独立挑错角色

防踢皮球三约束：
  1. 只认证据判 fail
  2. needs_more_context ≠ fail（退回补一次上下文）
  3. 一次性判定，不无限往返
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from .evidence import EvidenceArtifact


class RiskLevel(str, Enum):
    LOW = "low"        # 文案/局部 UI/一次性脚本
    UNSURE = "unsure"  # 跨模块/改公共逻辑
    HIGH = "high"      # 支付/下单/鉴权/崩溃热点/数据写入/签名


# 风险极高的关键词，命中即强制独立验收
HIGH_RISK_KEYWORDS = [
    "支付", "下单", "订单", "鉴权", "登录", "token", "签名", "证书",
    "启动", "崩溃", "crash", "数据写入", "删除", "迁移", "payment", "auth",
]


def assess_risk(change_desc: str, touches_shared: bool = False) -> RiskLevel:
    d = change_desc.lower()
    if any(kw.lower() in d for kw in HIGH_RISK_KEYWORDS):
        return RiskLevel.HIGH
    if touches_shared:
        return RiskLevel.UNSURE
    return RiskLevel.LOW


@dataclass
class ChangeScore:
    """改动影响量化分（对应内部版 change_score）。

    不是拿指标当门槛（VDD 反对），而是把"改了多少、碰了什么"变成一个
    客观粗筛信号，辅助决定验证强度。真正算不算过仍回到证据本身。
    """
    lines: int = 0
    files: int = 0
    hit_keywords: List[str] = field(default_factory=list)
    score: int = 0
    level: RiskLevel = RiskLevel.LOW

    def to_dict(self) -> dict:
        return {"lines": self.lines, "files": self.files,
                "hit_keywords": self.hit_keywords, "score": self.score, "level": self.level.value}


def score_change(*, lines_changed: int = 0, files_changed: int = 0,
                 change_desc: str = "") -> ChangeScore:
    """量化改动影响：行数/40 + 文件数×3 + 命中核心关键词×10。

    命中高危关键词直接 HIGH；否则 >=30 分 HIGH、>=10 分 UNSURE、否则 LOW。
    阈值是粗筛线索，不是达标门槛。
    """
    d = change_desc.lower()
    hits = [kw for kw in HIGH_RISK_KEYWORDS if kw.lower() in d]
    score = lines_changed // 40 + files_changed * 3 + len(hits) * 10
    if hits or score >= 30:
        level = RiskLevel.HIGH
    elif score >= 10:
        level = RiskLevel.UNSURE
    else:
        level = RiskLevel.LOW
    return ChangeScore(lines=lines_changed, files=files_changed,
                       hit_keywords=hits, score=score, level=level)


def needs_independent_review(risk: RiskLevel) -> bool:
    return risk == RiskLevel.HIGH


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_MORE_CONTEXT = "needs_more_context"


@dataclass
class AcceptancePackage:
    """喂给独立验收角色的验收包：目标 + 验收标准 + 证据。"""
    case_id: str
    goal: str
    criteria: List[str]                       # 可验证硬指标
    evidence: List[EvidenceArtifact] = field(default_factory=list)


@dataclass
class AcceptanceResult:
    verdict: Verdict
    reasons: List[str] = field(default_factory=list)
    context_bounces: int = 0

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value, "reasons": self.reasons, "context_bounces": self.context_bounces}


class IndependentReviewer:
    """独立验收裁判。只认证据判 fail；缺上下文退回补一次，不无限往返。"""

    MAX_BOUNCES = 1

    def __init__(self) -> None:
        self._bounces = 0

    def review(self, pkg: AcceptancePackage) -> AcceptanceResult:
        # 每条验收标准都必须有至少一条 observed 证据支撑
        observed = [e for e in pkg.evidence if e.is_observed()]
        if not observed:
            if self._bounces < self.MAX_BOUNCES:
                self._bounces += 1
                return AcceptanceResult(Verdict.NEEDS_MORE_CONTEXT,
                                        ["无 observed 证据，退回补充一次"], self._bounces)
            return AcceptanceResult(Verdict.FAIL,
                                    ["补充后仍无 observed 证据，判 fail"], self._bounces)

        uncovered = []
        haystack = " ".join(e.summary for e in observed)
        for c in pkg.criteria:
            # 极简覆盖判定：验收标准的关键词是否在证据摘要里出现
            key = c.split()[0] if c.split() else c
            if key and key not in haystack:
                uncovered.append(c)
        if uncovered:
            return AcceptanceResult(Verdict.FAIL,
                                    [f"验收标准未被证据覆盖：{c}" for c in uncovered], self._bounces)
        return AcceptanceResult(Verdict.PASS, ["全部验收标准均有 observed 证据支撑"], self._bounces)
