"""Platform-neutral source candidate and change-request lineage contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Mapping


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChangeSnapshot:
    repository: str
    workspace: str
    base_commit: str
    files: tuple[str, ...]
    change_digest: str

    def __post_init__(self) -> None:
        if not self.repository or not self.workspace or not self.base_commit:
            raise ValueError(
                "change snapshot requires repository, workspace and base_commit"
            )
        if not self.files or len(set(self.files)) != len(self.files):
            raise ValueError(
                "change snapshot files must be non-empty and unique"
            )
        if len(self.change_digest) != 64:
            raise ValueError("change snapshot requires a sha256 digest")
        try:
            int(self.change_digest, 16)
        except ValueError as error:
            raise ValueError(
                "change snapshot digest must be hexadecimal"
            ) from error
        object.__setattr__(
            self, "files", tuple(str(item) for item in self.files)
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CandidateRevision:
    base_commit: str
    change_digest: str
    branch: str
    commit: str
    remote_commit: str

    def __post_init__(self) -> None:
        for field_name in (
            "base_commit", "change_digest", "branch", "commit",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(
                    f"candidate revision requires {field_name}"
                )
        if self.remote_commit and self.remote_commit != self.commit:
            raise ValueError(
                "candidate remote_commit must match the local commit"
            )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ChangeRequestReceipt:
    provider: str
    request_id: str
    url: str
    draft: bool
    base_commit: str
    source_branch: str
    target_branch: str
    candidate_commit: str
    change_digest: str

    def __post_init__(self) -> None:
        if not all((
            self.provider, self.request_id, self.url, self.base_commit,
            self.source_branch, self.target_branch, self.candidate_commit,
            self.change_digest,
        )):
            raise ValueError("change request receipt is incomplete")
        if not self.draft:
            raise ValueError("automated change requests must remain draft")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CICheckReceipt:
    provider: str
    request_id: str
    candidate_commit: str
    status: str
    details_url: str = ""

    def __post_init__(self) -> None:
        if not self.provider or not self.request_id or not self.candidate_commit:
            raise ValueError("CI receipt identity is incomplete")
        if self.status not in {"success", "failure", "pending"}:
            raise ValueError("CI receipt status is invalid")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CandidateLineage:
    snapshot: ChangeSnapshot
    candidate: CandidateRevision
    change_request: ChangeRequestReceipt
    ci: CICheckReceipt

    def __post_init__(self) -> None:
        if (
            self.snapshot.base_commit != self.candidate.base_commit
            or self.snapshot.change_digest != self.candidate.change_digest
            or self.change_request.base_commit != self.candidate.base_commit
            or self.change_request.change_digest
            != self.candidate.change_digest
            or self.change_request.source_branch != self.candidate.branch
            or self.change_request.candidate_commit != self.candidate.commit
            or self.ci.request_id != self.change_request.request_id
            or self.ci.candidate_commit != self.candidate.commit
        ):
            raise ValueError(
                "candidate lineage does not bind one exact source revision"
            )

    def verified(self) -> bool:
        return (
            self.candidate.remote_commit == self.candidate.commit
            and self.change_request.draft
            and self.ci.status == "success"
        )

    def fingerprint(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict:
        return {
            "snapshot": self.snapshot.to_dict(),
            "candidate": self.candidate.to_dict(),
            "change_request": self.change_request.to_dict(),
            "ci": self.ci.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "CandidateLineage":
        return cls(
            snapshot=ChangeSnapshot(
                **dict(payload["snapshot"])
            ),
            candidate=CandidateRevision(
                **dict(payload["candidate"])
            ),
            change_request=ChangeRequestReceipt(
                **dict(payload["change_request"])
            ),
            ci=CICheckReceipt(**dict(payload["ci"])),
        )
