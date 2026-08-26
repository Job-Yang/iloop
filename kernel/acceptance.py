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

import json
import hashlib
import math
import secrets
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, List, Mapping, Optional

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


def _normalize_verdict(value: str) -> str:
    text = str(value or "pending").strip().lower()
    if text in ("needs_more", "needs_more_context", "uncertain"):
        return "needs_more_context"
    if text in ("pass", "fail"):
        return text
    return "pending"


def aggregate_criteria_verdicts(criteria_count: int, per_criterion: list[str]) -> str:
    """Roll up per-criterion verdicts. Any missing criterion stays pending so an
    overall pass can never be minted without judging every criterion.

    Priority: fail > needs_more_context > pending > pass.
    """
    if criteria_count <= 0:
        return "pending"
    verdicts = [_normalize_verdict(item) for item in per_criterion]
    if len(verdicts) < criteria_count:
        verdicts += ["pending"] * (criteria_count - len(verdicts))
    if any(v == "fail" for v in verdicts):
        return "fail"
    if any(v == "needs_more_context" for v in verdicts):
        return "needs_more_context"
    if any(v == "pending" for v in verdicts):
        return "pending"
    return "pass"


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
    subject_fingerprint: str = ""
    executor_id: str = ""
    review_token: str = ""
    package_id: str = ""
    status: str = "prepared"
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.package_id:
            self.package_id = f"accept-{self.case_id}-{int(self.created_at)}"
        if not self.review_token:
            self.review_token = secrets.token_hex(16)
        if not self.expires_at:
            self.expires_at = self.created_at + 3600
        if (
            not self.case_id.strip()
            or not self.goal.strip()
            or not self.criteria
        ):
            raise ValueError("acceptance package identity is incomplete")
        if (
            not math.isfinite(self.created_at)
            or not math.isfinite(self.expires_at)
            or self.expires_at <= self.created_at
        ):
            raise ValueError(
                "acceptance package expiry must follow creation"
            )

    def to_dict(self) -> dict:
        return {
            "package_id": self.package_id,
            "case_id": self.case_id,
            "goal": self.goal,
            "criteria": list(self.criteria),
            "evidence": [item.to_dict() for item in self.evidence],
            "subject_fingerprint": self.subject_fingerprint,
            "executor_id": self.executor_id,
            "review_token": self.review_token,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "review_rules": [
                "Only observed evidence can justify pass or fail.",
                "Missing context is needs_more_context, not fail.",
                "Do not modify implementation files.",
            ],
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AcceptancePackage":
        return cls(
            case_id=str(payload["case_id"]),
            goal=str(payload["goal"]),
            criteria=[str(item) for item in payload["criteria"]],
            evidence=[
                EvidenceArtifact(**dict(item))
                for item in payload.get("evidence", [])
            ],
            subject_fingerprint=str(
                payload.get("subject_fingerprint", "")
            ),
            executor_id=str(payload.get("executor_id", "")),
            review_token=str(payload["review_token"]),
            package_id=str(payload["package_id"]),
            status=str(payload.get("status", "prepared")),
            created_at=float(payload["created_at"]),
            expires_at=float(payload["expires_at"]),
        )


@dataclass
class AcceptanceResult:
    verdict: Verdict
    reasons: List[str] = field(default_factory=list)
    context_bounces: int = 0
    reviewer: str = ""
    reviewed_at: float = field(default_factory=time.time)
    artifact_path: str = ""
    artifact_sha256: str = ""

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "reasons": self.reasons,
            "context_bounces": self.context_bounces,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
        }


class IndependentReviewer:
    """独立验收裁判。只认证据判 fail；缺上下文退回补一次，不无限往返。"""

    MAX_BOUNCES = 1

    def __init__(
        self,
        verify_attestation: Optional[Callable[[Path, dict], bool]] = None,
    ) -> None:
        self._bounces = 0
        self.verify_attestation = verify_attestation

    def review(self, pkg: AcceptancePackage) -> AcceptanceResult:
        # 每条验收标准都必须有至少一条 observed 证据支撑
        observed = [
            e for e in pkg.evidence
            if e.supports_success(self.verify_attestation)
        ]
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


class AcceptanceStore:
    """Persist packages and external reviewer verdicts.

    The main agent may prepare a package, but only an explicitly named external
    reviewer may record the verdict used by wrap-up.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def prepare(
        self,
        package: AcceptancePackage,
        *,
        batch: Optional["AcceptanceBatch"] = None,
    ) -> AcceptancePackage:
        if (
            batch is not None
            and batch.package.to_dict() != package.to_dict()
        ):
            raise ValueError(
                "acceptance batch must derive from the stored package"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({
                "package": package.to_dict(),
                "batch": batch.to_dict() if batch is not None else None,
                "result": None,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return package

    def record_file(
        self,
        result_path: str | Path,
        *,
        verify_attestation: Optional[Callable[[Path, dict], bool]] = None,
    ) -> AcceptanceResult:
        data = self.load_raw()
        package = data.get("package")
        if not package:
            raise ValueError("acceptance package has not been prepared")
        if float(package.get("expires_at", 0)) <= time.time():
            raise ValueError("acceptance package has expired; prepare a new challenge")
        path = Path(result_path)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("independent reviewer result file is missing or empty")
        row = json.loads(path.read_text(encoding="utf-8"))
        if verify_attestation is None or not verify_attestation(path, row):
            raise ValueError(
                "independent review requires a trusted host attestation verifier"
            )
        for key in ("package_id", "case_id", "review_token", "subject_fingerprint",
                    "reviewer", "verdict", "reasons", "expires_at"):
            if key not in row:
                raise ValueError(f"review result missing {key}")
        if row["package_id"] != package["package_id"]:
            raise ValueError("review result package_id mismatch")
        if row["case_id"] != package["case_id"]:
            raise ValueError("review result case_id mismatch")
        if row["review_token"] != package["review_token"]:
            raise ValueError("review result challenge token mismatch")
        if row["subject_fingerprint"] != package.get("subject_fingerprint", ""):
            raise ValueError("review result subject fingerprint mismatch")
        if float(row["expires_at"]) <= time.time():
            raise ValueError("review result attestation has expired")
        reviewer = str(row["reviewer"]).strip()
        if not reviewer:
            raise ValueError("review result requires reviewer identity")
        executor_id = str(package.get("executor_id", "")).strip()
        if not executor_id:
            raise ValueError("acceptance package lacks a host-attested executor identity")
        if reviewer == executor_id:
            raise ValueError("independent reviewer must differ from task executor")
        reasons = list(row["reasons"])
        if not reasons:
            raise ValueError("review result requires reasons")
        # Per-criterion verdicts are mandatory: an overall pass must never be
        # accepted without judging every criterion in the package.
        criteria = list(package.get("criteria", []))
        per_criterion = row.get("criteria_verdicts")
        if not isinstance(per_criterion, list) or len(per_criterion) != len(criteria):
            raise ValueError(
                "review result must include one criteria_verdicts entry per criterion"
            )
        normalized = [_normalize_verdict(item) for item in per_criterion]
        aggregate = aggregate_criteria_verdicts(len(criteria), normalized)
        if _normalize_verdict(row["verdict"]) != aggregate:
            raise ValueError(
                "overall verdict does not match the per-criterion roll-up"
            )
        batch_payload = data.get("batch")
        if batch_payload:
            AcceptanceBatch.from_dict(
                AcceptancePackage.from_dict(package),
                batch_payload,
            ).validate_aggregate(row)
        result = AcceptanceResult(
            Verdict(row["verdict"]),
            reasons,
            reviewer=reviewer,
            reviewed_at=float(row.get("reviewed_at", time.time())),
            artifact_path=str(path),
            artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        data["result"] = result.to_dict()
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def load_raw(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def result(
        self,
        verify_attestation: Optional[Callable[[Path, dict], bool]] = None,
        *,
        expected_case_id: str = "",
    ) -> AcceptanceResult | None:
        data = self.load_raw()
        row = data.get("result")
        if not row:
            return None
        result = AcceptanceResult(
            verdict=Verdict(row["verdict"]),
            reasons=list(row.get("reasons", [])),
            context_bounces=int(row.get("context_bounces", 0)),
            reviewer=row.get("reviewer", ""),
            reviewed_at=float(row.get("reviewed_at", time.time())),
            artifact_path=row.get("artifact_path", ""),
            artifact_sha256=row.get("artifact_sha256", ""),
        )
        artifact = Path(result.artifact_path)
        if (
            not artifact.is_file()
            or hashlib.sha256(artifact.read_bytes()).hexdigest()
            != result.artifact_sha256
        ):
            return None
        try:
            artifact_row = json.loads(
                artifact.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            return None
        if (
            verify_attestation is None
            or not verify_attestation(artifact, artifact_row)
        ):
            return None
        package = data.get("package") or {}
        if (
            float(package.get("expires_at", 0)) <= time.time()
            or (
                bool(expected_case_id)
                and package.get("case_id") != expected_case_id
            )
            or float(
                artifact_row.get("reviewed_at", result.reviewed_at)
            ) > float(package.get("expires_at", 0))
            or artifact_row.get("package_id")
            != package.get("package_id")
            or artifact_row.get("case_id") != package.get("case_id")
            or artifact_row.get("review_token")
            != package.get("review_token")
            or artifact_row.get("subject_fingerprint")
            != package.get("subject_fingerprint")
            or artifact_row.get("reviewer") != result.reviewer
            or not str(package.get("executor_id", "")).strip()
            or artifact_row.get("reviewer")
            == package.get("executor_id")
            or artifact_row.get("verdict") != result.verdict.value
            or float(artifact_row.get("expires_at", 0)) <= time.time()
        ):
            return None
        criteria = list(package.get("criteria", []))
        per_criterion = artifact_row.get("criteria_verdicts")
        if (
            not isinstance(per_criterion, list)
            or len(per_criterion) != len(criteria)
        ):
            return None
        if _normalize_verdict(
            artifact_row.get("verdict")
        ) != aggregate_criteria_verdicts(
            len(criteria),
            [_normalize_verdict(item) for item in per_criterion],
        ):
            return None
        batch_payload = data.get("batch")
        if batch_payload:
            try:
                AcceptanceBatch.from_dict(
                    AcceptancePackage.from_dict(package),
                    batch_payload,
                ).validate_aggregate(artifact_row)
            except (KeyError, TypeError, ValueError):
                return None
        return result


@dataclass(frozen=True)
class AcceptanceShard:
    shard_id: str
    scope: tuple[str, ...]
    criterion_indexes: tuple[int, ...]
    read_only: bool = True

    def __post_init__(self) -> None:
        if not self.shard_id.strip():
            raise ValueError("acceptance shard_id is required")
        if not self.scope or not self.criterion_indexes:
            raise ValueError(
                "acceptance shard requires scope and criterion indexes"
            )
        if not self.read_only:
            raise ValueError("parallel acceptance shards must be read-only")
        indexes = tuple(int(item) for item in self.criterion_indexes)
        if (
            any(item < 0 for item in indexes)
            or len(set(indexes)) != len(indexes)
        ):
            raise ValueError(
                "acceptance shard criterion indexes must be unique "
                "non-negative integers"
            )
        object.__setattr__(
            self, "scope", tuple(str(item) for item in self.scope)
        )
        object.__setattr__(
            self, "criterion_indexes", indexes
        )


@dataclass(frozen=True)
class AcceptanceBatch:
    """Read-only shards derived from one existing AcceptancePackage."""

    batch_id: str
    package: AcceptancePackage
    shards: tuple[AcceptanceShard, ...]

    def __post_init__(self) -> None:
        if not self.batch_id.strip():
            raise ValueError("acceptance batch_id is required")
        if (
            len(self.package.subject_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.package.subject_fingerprint.lower()
            )
        ):
            raise ValueError(
                "acceptance batch requires a sha256 subject fingerprint"
            )
        if not self.package.executor_id.strip():
            raise ValueError(
                "acceptance batch requires the package executor identity"
            )
        if not self.shards:
            raise ValueError("acceptance batch requires shards")
        ids = [item.shard_id for item in self.shards]
        if len(set(ids)) != len(ids):
            raise ValueError("acceptance batch shard IDs must be unique")
        indexes = [
            index
            for shard in self.shards
            for index in shard.criterion_indexes
        ]
        expected = list(range(len(self.package.criteria)))
        if sorted(indexes) != expected:
            raise ValueError(
                "acceptance shards must partition every package criterion "
                "exactly once"
            )
        object.__setattr__(self, "shards", tuple(self.shards))

    @property
    def case_id(self) -> str:
        return self.package.case_id

    @property
    def subject_fingerprint(self) -> str:
        return self.package.subject_fingerprint

    @property
    def executor_id(self) -> str:
        return self.package.executor_id

    @property
    def created_at(self) -> float:
        return self.package.created_at

    @property
    def expires_at(self) -> float:
        return self.package.expires_at

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "package_id": self.package.package_id,
            "case_id": self.package.case_id,
            "review_token": self.package.review_token,
            "subject_fingerprint": self.package.subject_fingerprint,
            "executor_id": self.package.executor_id,
            "shards": [asdict(item) for item in self.shards],
            "created_at": self.package.created_at,
            "expires_at": self.package.expires_at,
        }

    @classmethod
    def from_dict(
        cls,
        package: AcceptancePackage,
        payload: Mapping[str, object],
    ) -> "AcceptanceBatch":
        expected_identity = {
            "package_id": package.package_id,
            "case_id": package.case_id,
            "review_token": package.review_token,
            "subject_fingerprint": package.subject_fingerprint,
            "executor_id": package.executor_id,
            "created_at": package.created_at,
            "expires_at": package.expires_at,
        }
        if any(
            payload.get(key) != value
            for key, value in expected_identity.items()
        ):
            raise ValueError(
                "acceptance batch does not match its package"
            )
        return cls(
            batch_id=str(payload["batch_id"]),
            package=package,
            shards=tuple(
                AcceptanceShard(**dict(item))
                for item in payload.get("shards", [])
            ),
        )

    def _aggregate_rows(
        self,
        results: List[Mapping[str, object]],
        *,
        verify_attestation: Optional[
            Callable[[Mapping[str, object]], bool]
        ],
        now: float,
    ) -> dict:
        if not math.isfinite(now) or now >= self.package.expires_at:
            raise ValueError("acceptance batch has expired")
        expected_identity = {
            "batch_id": self.batch_id,
            "package_id": self.package.package_id,
            "case_id": self.package.case_id,
            "review_token": self.package.review_token,
            "subject_fingerprint": self.package.subject_fingerprint,
        }
        by_shard = {}
        for source in results:
            row = dict(source)
            if any(
                row.get(key) != value
                for key, value in expected_identity.items()
            ):
                raise ValueError(
                    "acceptance result does not match the frozen package"
                )
            shard_id = str(row.get("shard_id") or "")
            if shard_id in by_shard:
                raise ValueError(
                    f"duplicate acceptance shard result: {shard_id}"
                )
            if (
                verify_attestation is not None
                and not verify_attestation(row)
            ):
                raise ValueError(
                    "parallel acceptance result is not host attested"
                )
            by_shard[shard_id] = row
        expected = {item.shard_id: item for item in self.shards}
        if set(by_shard) != set(expected):
            missing = sorted(set(expected) - set(by_shard))
            extra = sorted(set(by_shard) - set(expected))
            raise ValueError(
                "acceptance batch result set mismatch: "
                f"missing={missing}, extra={extra}"
            )
        criterion_verdicts = ["pending"] * len(self.package.criteria)
        shard_results = []
        reviewers = set()
        reasons = []
        reviewed_at_values = []
        for shard_id in sorted(expected):
            shard = expected[shard_id]
            row = by_shard[shard_id]
            shard_verdicts = [
                _normalize_verdict(item)
                for item in list(row.get("criteria_verdicts") or [])
            ]
            if len(shard_verdicts) != len(shard.criterion_indexes):
                raise ValueError(
                    f"acceptance shard '{shard_id}' must judge every criterion"
                )
            verdict = aggregate_criteria_verdicts(
                len(shard.criterion_indexes), shard_verdicts
            )
            if _normalize_verdict(str(row.get("verdict"))) != verdict:
                raise ValueError(
                    f"acceptance shard '{shard_id}' verdict mismatch"
                )
            reviewer = str(row.get("reviewer") or "").strip()
            reviewed_at = float(row.get("reviewed_at", 0))
            if (
                not reviewer
                or reviewer == self.package.executor_id
                or reviewer in reviewers
                or not math.isfinite(reviewed_at)
                or reviewed_at < self.package.created_at
                or reviewed_at > min(now + 30, self.package.expires_at)
            ):
                raise ValueError(
                    f"acceptance shard '{shard_id}' is not independent"
                )
            shard_reasons = [
                str(item).strip()
                for item in list(row.get("reasons") or [])
                if str(item).strip()
            ]
            if not shard_reasons:
                raise ValueError(
                    f"acceptance shard '{shard_id}' requires reasons"
                )
            for index, criterion_verdict in zip(
                shard.criterion_indexes, shard_verdicts
            ):
                criterion_verdicts[index] = criterion_verdict
            reviewers.add(reviewer)
            reviewed_at_values.append(reviewed_at)
            reasons.extend(
                f"{shard_id}: {reason}" for reason in shard_reasons
            )
            shard_results.append({
                **expected_identity,
                "shard_id": shard_id,
                "reviewer": reviewer,
                "verdict": verdict,
                "criteria_verdicts": shard_verdicts,
                "reasons": shard_reasons,
                "reviewed_at": reviewed_at,
            })
        overall = aggregate_criteria_verdicts(
            len(self.package.criteria), criterion_verdicts
        )
        return {
            **expected_identity,
            "reviewer": f"parallel:{self.batch_id}",
            "verdict": overall,
            "criteria_verdicts": criterion_verdicts,
            "reasons": reasons,
            "reviewed_at": max(reviewed_at_values),
            "expires_at": self.package.expires_at,
            "shards": shard_results,
        }

    def aggregate(
        self,
        results: List[Mapping[str, object]],
        *,
        verify_attestation: Optional[
            Callable[[Mapping[str, object]], bool]
        ] = None,
        now: Optional[float] = None,
    ) -> dict:
        current = time.time() if now is None else float(now)
        if verify_attestation is None:
            raise ValueError(
                "parallel acceptance requires a host attestation verifier"
            )
        return self._aggregate_rows(
            results,
            verify_attestation=verify_attestation,
            now=current,
        )

    def validate_aggregate(
        self,
        row: Mapping[str, object],
        *,
        now: Optional[float] = None,
    ) -> None:
        current = time.time() if now is None else float(now)
        rebuilt = self._aggregate_rows(
            [dict(item) for item in row.get("shards", [])],
            verify_attestation=None,
            now=current,
        )
        for key in (
            "batch_id", "package_id", "case_id", "review_token",
            "subject_fingerprint", "reviewer", "verdict",
            "criteria_verdicts", "reasons", "reviewed_at", "expires_at",
        ):
            if row.get(key) != rebuilt.get(key):
                raise ValueError(
                    f"acceptance batch aggregate mismatch: {key}"
                )
