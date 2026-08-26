"""Public Git/GitHub Provider and a reference BugFix assistant assembly."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Iterable, Mapping, Optional

from kernel.action import (
    ActionCatalog, ActionRisk, ActionSideEffect, ActionSpec,
)
from kernel.capability import (
    CapabilityId, CapabilityResult, CapabilitySpec, CapabilityStatus,
)
from kernel.recipe import AssistantRecipe, RecipeCatalog
from kernel.runner import CommandOutput, CommandRunner
from kernel.source import (
    CandidateLineage, CandidateRevision, ChangeRequestReceipt,
    ChangeSnapshot, CICheckReceipt,
)
from kernel.storage import atomic_write_json


SOURCE_INSPECT = CapabilityId("source.inspect")
SOURCE_CANDIDATE_PIPELINE = CapabilityId(
    "source.candidate_pipeline"
)
CI_VERIFY_CANDIDATE = CapabilityId("ci.verify_candidate")
REFERENCE_ASSISTANT_ID = "builtin.bugfix"


def capability_specs() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            capability_id=SOURCE_INSPECT,
            description="Inspect one Git repository at a frozen revision",
            inputs={"project_root": "path"},
            outputs={"base_commit": "sha", "branch": "string"},
            side_effect="read",
            required_tools=(("git",),),
        ),
        CapabilitySpec(
            capability_id=SOURCE_CANDIDATE_PIPELINE,
            description=(
                "Freeze a source diff, publish one candidate commit, create "
                "a draft change request, and verify CI for that exact commit"
            ),
            inputs={
                "workspace": "path",
                "base_commit": "sha",
                "allowed_paths": "list[string]",
                "branch": "string",
                "message": "string",
                "target_branch": "string",
            },
            outputs={"lineage": "CandidateLineage"},
            side_effect="external_write",
            required_tools=(("git",), ("gh",)),
        ),
        CapabilitySpec(
            capability_id=CI_VERIFY_CANDIDATE,
            description="Revalidate the stored draft change request and CI",
            outputs={"lineage_fingerprint": "sha256"},
            side_effect="read",
            required_tools=(("git",), ("gh",)),
        ),
    )


def register_reference_bugfix(
    actions: ActionCatalog,
    recipes: RecipeCatalog,
) -> None:
    """Register contracts only; the Git Provider remains an outer adapter."""
    for spec in capability_specs():
        if not actions.capabilities.contains(spec.capability_id):
            actions.capabilities.register(spec)
    if any(
        recipe.assistant_id == REFERENCE_ASSISTANT_ID
        for recipe in recipes.all()
    ):
        return
    actions.register(
        ActionSpec(
            action_id="builtin.bugfix.inspect",
            description="Inspect the source baseline before diagnosis",
            required_capabilities=(SOURCE_INSPECT,),
            allowed_assistants=(REFERENCE_ASSISTANT_ID,),
            lifecycle_stage="diagnosis",
        ),
        lambda payload: {},
    )
    actions.register(
        ActionSpec(
            action_id="builtin.bugfix.publish_candidate",
            description=(
                "Publish an authorized source candidate and draft change "
                "request for the frozen diagnosis"
            ),
            risk=ActionRisk.HIGH,
            side_effects=(ActionSideEffect.EXTERNAL_WRITE,),
            allowed_assistants=(REFERENCE_ASSISTANT_ID,),
            required_capabilities=(SOURCE_CANDIDATE_PIPELINE,),
            disposition_kind="code_change",
            lifecycle_stage="disposition",
        ),
        lambda payload: {},
    )
    actions.register(
        ActionSpec(
            action_id="builtin.bugfix.verify_candidate",
            description="Verify CI for the exact published candidate",
            allowed_assistants=(REFERENCE_ASSISTANT_ID,),
            required_capabilities=(CI_VERIFY_CANDIDATE,),
            lifecycle_stage="verification",
        ),
        lambda payload: {},
    )
    recipes.register(AssistantRecipe(
        assistant_id=REFERENCE_ASSISTANT_ID,
        version="1",
        ingress=("manual", "issue"),
        actions=(
            "builtin.bugfix.inspect",
            "builtin.bugfix.publish_candidate",
            "builtin.bugfix.verify_candidate",
        ),
    ))


class GitNativeProvider:
    platform_id = "git_native"

    def __init__(
        self,
        data_dir: str | Path,
        *,
        runner: Optional[CommandRunner] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runner = runner or CommandRunner()

    def capabilities(self) -> list[CapabilityId]:
        return [
            SOURCE_INSPECT,
            SOURCE_CANDIDATE_PIPELINE,
            CI_VERIFY_CANDIDATE,
        ]

    def runtime_fingerprint(self) -> str:
        runner_fingerprint = getattr(
            self.runner, "runtime_fingerprint", None
        )
        if not callable(runner_fingerprint):
            return ""
        runner_digest = str(runner_fingerprint()).strip().lower()
        if (
            len(runner_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in runner_digest
            )
        ):
            return ""
        payload = {
            "provider": self.platform_id,
            "implementation_sha256": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
            "data_dir": str(self.data_dir.resolve()),
            "runner": (
                f"{type(self.runner).__module__}."
                f"{type(self.runner).__qualname__}"
            ),
            "runner_fingerprint": runner_digest,
        }
        return hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def readiness(self, capability: object) -> dict:
        identifier = CapabilityId(capability)
        spec = next(
            item for item in capability_specs()
            if item.capability_id == identifier
        )
        missing = [
            list(group)
            for group in spec.required_tools
            if not any(shutil.which(tool) for tool in group)
        ]
        gh_authenticated = True
        if (
            identifier
            in {SOURCE_CANDIDATE_PIPELINE, CI_VERIFY_CANDIDATE}
            and not missing
        ):
            auth = self.runner.run(
                ["gh", "auth", "status"],
                timeout=30,
            )
            gh_authenticated = auth.returncode == 0
        return {
            "capability": identifier.value,
            "declared": True,
            "missing_tool_groups": missing,
            "gh_authenticated": gh_authenticated,
            "runtime_ready": not missing and gh_authenticated,
        }

    def invoke(
        self,
        capability: object,
        **kwargs,
    ) -> CapabilityResult:
        identifier = CapabilityId(capability)
        try:
            if identifier == SOURCE_INSPECT:
                output = self._inspect(kwargs)
            elif identifier == SOURCE_CANDIDATE_PIPELINE:
                output = self._publish_candidate(kwargs)
            elif identifier == CI_VERIFY_CANDIDATE:
                output = self._verify_candidate(kwargs)
            else:
                raise ValueError(
                    f"unsupported git capability: {identifier}"
                )
            evidence_dir = self._write_evidence(
                str(kwargs.get("task_id") or "task"),
                identifier,
                output,
            )
            return CapabilityResult(
                platform=self.platform_id,
                capability=identifier.value,
                status=CapabilityStatus.SUCCESS,
                summary=f"{identifier.value} completed",
                evidence_dir=str(evidence_dir),
                metadata={
                    "outputs": output,
                    "subjects": list(output.get("files", [])),
                },
            )
        except Exception as error:
            return CapabilityResult(
                platform=self.platform_id,
                capability=identifier.value,
                status=CapabilityStatus.ERROR,
                summary=str(error),
            )

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Optional[Path] = None,
        timeout: float = 600,
    ) -> CommandOutput:
        result = self.runner.run(argv, cwd=cwd, timeout=timeout)
        if result.returncode:
            raise ValueError(
                (result.stderr or result.stdout or "command failed").strip()
            )
        return result

    def _git(self, root: Path, *args: str) -> str:
        return self._run(
            ["git", "-C", str(root), *args],
            timeout=120,
        ).stdout.strip()

    @staticmethod
    def _paths(value: object) -> list[str]:
        if isinstance(value, str):
            rows = re.split(r"[,;]", value)
        elif isinstance(value, Iterable):
            rows = list(value)
        else:
            rows = []
        return [
            str(item).strip().rstrip("/")
            for item in rows if str(item).strip()
        ]

    def _inspect(self, kwargs: Mapping[str, object]) -> dict:
        root = Path(str(kwargs["project_root"])).expanduser().resolve()
        if not (root / ".git").exists():
            raise ValueError(f"not a Git worktree: {root}")
        return {
            "repository": str(root),
            "base_commit": self._git(root, "rev-parse", "HEAD"),
            "branch": self._git(root, "branch", "--show-current"),
        }

    def prepare_workspace(
        self,
        repository: str | Path,
        *,
        base_commit: str,
        task_id: str,
        branch: str = "",
    ) -> Path:
        root = Path(repository).expanduser().resolve()
        self._git(root, "rev-parse", "--verify", f"{base_commit}^{{commit}}")
        safe_task = re.sub(r"[^A-Za-z0-9_.-]", "-", task_id)[:80]
        target = self.data_dir / "worktrees" / safe_task
        if target.exists():
            raise ValueError(f"candidate workspace already exists: {target}")
        branch = branch or f"iloop/{safe_task}"
        if not branch.startswith("iloop/"):
            raise ValueError("candidate branch must use the iloop/ namespace")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._run([
            "git", "-C", str(root), "worktree", "add",
            "-b", branch, str(target), base_commit,
        ], timeout=120)
        target = target.resolve()
        atomic_write_json(
            target.with_suffix(".json"),
            {
                "repository": str(root),
                "workspace": str(target),
                "base_commit": base_commit,
                "branch": branch,
                "task_id": task_id,
            },
        )
        return target

    def _workspace_metadata(self, workspace: Path) -> dict:
        path = workspace.with_suffix(".json")
        if not path.is_file():
            raise ValueError("candidate workspace ownership metadata missing")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("workspace") != str(workspace):
            raise ValueError("candidate workspace metadata mismatch")
        repository = Path(
            str(payload.get("repository") or "")
        ).expanduser().resolve()
        common_dir = Path(
            self._git(workspace, "rev-parse", "--git-common-dir")
        )
        if not common_dir.is_absolute():
            common_dir = (workspace / common_dir).resolve()
        if common_dir != (repository / ".git").resolve():
            raise ValueError(
                "candidate workspace does not belong to the recorded repository"
            )
        return payload

    def capture_snapshot(
        self,
        workspace: str | Path,
        *,
        base_commit: str,
        allowed_paths: Iterable[str],
    ) -> ChangeSnapshot:
        root = Path(workspace).expanduser().resolve()
        metadata = self._workspace_metadata(root)
        if metadata.get("base_commit") != base_commit:
            raise ValueError("candidate workspace base_commit mismatch")
        if self._git(root, "rev-parse", "HEAD") != base_commit:
            raise ValueError("candidate workspace HEAD moved from base_commit")
        tracked = self._git(
            root, "diff", "--name-only", base_commit, "--"
        ).splitlines()
        untracked = self._git(
            root, "ls-files", "--others", "--exclude-standard"
        ).splitlines()
        files = sorted({
            item.strip() for item in [*tracked, *untracked]
            if item.strip()
        })
        if not files:
            raise ValueError("candidate workspace has no source changes")
        allowed = self._paths(allowed_paths)
        if not allowed:
            raise ValueError("candidate workflow requires allowed_paths")
        outside = [
            path for path in files
            if not any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in allowed
            )
        ]
        if outside:
            raise ValueError(
                "candidate changes exceed allowed_paths: "
                + ", ".join(outside)
            )
        entries = []
        for relative in files:
            path = root / relative
            if not path.exists() and not path.is_symlink():
                entries.append([relative, "deleted", ""])
                continue
            mode = (
                "120000" if path.is_symlink()
                else "100755" if path.stat().st_mode & 0o111
                else "100644"
            )
            content = (
                str(path.readlink()).encode("utf-8")
                if path.is_symlink()
                else path.read_bytes()
            )
            object_id = hashlib.sha1(
                f"blob {len(content)}\0".encode("ascii") + content
            ).hexdigest()
            entries.append([relative, mode, object_id])
        change_digest = hashlib.sha256(json.dumps(
            entries,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return ChangeSnapshot(
            repository=str(metadata["repository"]),
            workspace=str(root),
            base_commit=base_commit,
            files=tuple(files),
            change_digest=change_digest,
        )

    def _publish_candidate(
        self,
        kwargs: Mapping[str, object],
    ) -> dict:
        workspace = Path(
            str(kwargs["workspace"])
        ).expanduser().resolve()
        base_commit = str(kwargs["base_commit"])
        snapshot = self.capture_snapshot(
            workspace,
            base_commit=base_commit,
            allowed_paths=self._paths(kwargs["allowed_paths"]),
        )
        expected = str(kwargs.get("change_digest") or "")
        if expected and expected != snapshot.change_digest:
            raise ValueError("candidate change_digest mismatch")
        branch = str(kwargs["branch"])
        metadata = self._workspace_metadata(workspace)
        if (
            branch != metadata.get("branch")
            or self._git(workspace, "branch", "--show-current") != branch
        ):
            raise ValueError("candidate branch does not own the workspace")
        self._run([
            "git", "-C", str(workspace), "add", "--", *snapshot.files,
        ])
        staged = self._staged_digest(
            workspace, base_commit, snapshot.files
        )
        if staged != snapshot.change_digest:
            raise ValueError(
                "staged candidate differs from the frozen snapshot"
            )
        self._run([
            "git", "-C", str(workspace), "commit",
            "-m", str(kwargs["message"]),
        ])
        commit = self._git(workspace, "rev-parse", "HEAD")
        if self._commit_digest(
            workspace, base_commit, commit, snapshot.files
        ) != snapshot.change_digest:
            raise ValueError(
                "candidate commit differs from the frozen snapshot"
            )
        if self._git(
            workspace, "status", "--porcelain", "--untracked-files=all"
        ):
            raise ValueError(
                "candidate workspace changed after commit"
            )
        self._run([
            "git", "-C", str(workspace), "push",
            "-u", "origin", branch,
        ], timeout=float(kwargs.get("push_timeout") or 600))
        remote = self._git(
            workspace, "ls-remote", "origin", f"refs/heads/{branch}"
        ).split()
        remote_commit = remote[0] if remote else ""
        candidate = CandidateRevision(
            base_commit=base_commit,
            change_digest=snapshot.change_digest,
            branch=branch,
            commit=commit,
            remote_commit=remote_commit,
        )
        pr = self._create_draft_pr(
            workspace,
            candidate,
            target_branch=str(kwargs["target_branch"]),
            title=str(kwargs.get("title") or kwargs["message"]),
            body=str(kwargs.get("body") or "Generated by iLoop"),
        )
        ci = self._read_ci(
            workspace,
            pr,
            timeout_seconds=float(kwargs.get("ci_timeout") or 600),
        )
        remote = self._git(
            workspace,
            "ls-remote",
            "origin",
            f"refs/heads/{branch}",
        ).split()
        if not remote or remote[0] != candidate.commit:
            raise ValueError(
                "remote candidate commit changed during CI"
            )
        pr = self._create_receipt_from_view(workspace, pr)
        lineage = CandidateLineage(
            snapshot=snapshot,
            candidate=candidate,
            change_request=pr,
            ci=ci,
        )
        if not lineage.verified():
            raise ValueError("candidate CI did not pass")
        return {
            "lineage": lineage.to_dict(),
            "lineage_fingerprint": lineage.fingerprint(),
            "files": list(snapshot.files),
        }

    def _staged_digest(
        self,
        workspace: Path,
        base_commit: str,
        files: Iterable[str],
    ) -> str:
        staged_files = self._git(
            workspace, "diff", "--cached", "--name-only",
            base_commit, "--",
        ).splitlines()
        if sorted(staged_files) != sorted(files):
            raise ValueError("staged file list differs from snapshot")
        entries = []
        for relative in sorted(files):
            entry = self._git(
                workspace, "ls-files", "-s", "--", relative
            )
            if not entry:
                entries.append([relative, "deleted", ""])
                continue
            mode, object_id, _ = entry.split(maxsplit=2)
            entries.append([relative, mode, object_id])
        return hashlib.sha256(json.dumps(
            entries,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _commit_digest(
        self,
        workspace: Path,
        base_commit: str,
        commit: str,
        files: Iterable[str],
    ) -> str:
        committed_files = self._git(
            workspace,
            "diff",
            "--name-only",
            base_commit,
            commit,
            "--",
        ).splitlines()
        if sorted(committed_files) != sorted(files):
            raise ValueError(
                "candidate commit file list differs from snapshot"
            )
        entries = []
        for relative in sorted(files):
            entry = self._git(
                workspace, "ls-tree", commit, "--", relative
            )
            if not entry:
                entries.append([relative, "deleted", ""])
                continue
            metadata, _ = entry.split("\t", 1)
            mode, _, object_id = metadata.split()
            entries.append([relative, mode, object_id])
        return hashlib.sha256(json.dumps(
            entries,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _create_draft_pr(
        self,
        workspace: Path,
        candidate: CandidateRevision,
        *,
        target_branch: str,
        title: str,
        body: str,
    ) -> ChangeRequestReceipt:
        created = self._run([
            "gh", "pr", "create", "--draft",
            "--base", target_branch,
            "--head", candidate.branch,
            "--title", title,
            "--body", body,
        ], cwd=workspace, timeout=120)
        url = created.stdout.strip().splitlines()[-1]
        view = json.loads(self._run([
            "gh", "pr", "view", url,
            "--json",
            "number,url,isDraft,headRefName,baseRefName,headRefOid",
        ], cwd=workspace).stdout)
        return ChangeRequestReceipt(
            provider="github",
            request_id=str(view["number"]),
            url=str(view["url"]),
            draft=bool(view["isDraft"]),
            base_commit=candidate.base_commit,
            source_branch=str(view["headRefName"]),
            target_branch=str(view["baseRefName"]),
            candidate_commit=str(view["headRefOid"]),
            change_digest=candidate.change_digest,
        )

    def _read_ci(
        self,
        workspace: Path,
        request: ChangeRequestReceipt,
        *,
        timeout_seconds: float = 600,
        poll_seconds: float = 5,
    ) -> CICheckReceipt:
        deadline = time.monotonic() + timeout_seconds
        repository = json.loads(self._run([
            "gh", "repo", "view", "--json", "nameWithOwner",
        ], cwd=workspace).stdout)
        name_with_owner = str(
            repository.get("nameWithOwner") or ""
        ).strip()
        if not name_with_owner:
            raise ValueError("GitHub repository identity is missing")
        success_conclusions = {"SUCCESS", "SKIPPED", "NEUTRAL"}
        details_url = request.url
        while True:
            result = self._run([
                "gh", "api",
                (
                    f"repos/{name_with_owner}/commits/"
                    f"{request.candidate_commit}/check-runs?per_page=100"
                ),
                "--header", "Accept: application/vnd.github+json",
            ], cwd=workspace, timeout=min(timeout_seconds, 120))
            try:
                payload = json.loads(result.stdout or "{}")
            except json.JSONDecodeError as error:
                raise ValueError(
                    (result.stderr or "GitHub CI result is not JSON").strip()
                ) from error
            rows = payload.get("check_runs", [])
            if not rows:
                raise ValueError(
                    "candidate change request has no CI checks"
                )
            if int(payload.get("total_count", len(rows))) != len(rows):
                raise ValueError(
                    "candidate CI check set is incomplete"
                )
            if any(
                str(row.get("head_sha") or "")
                != request.candidate_commit
                for row in rows
            ):
                raise ValueError(
                    "GitHub CI check is not bound to the candidate commit"
                )
            details_url = str(
                rows[0].get("html_url") or request.url
            )
            statuses = {
                str(row.get("status") or "").upper() for row in rows
            }
            conclusions = {
                str(row.get("conclusion") or "").upper()
                for row in rows
            }
            if statuses == {"COMPLETED"}:
                status = (
                    "success"
                    if conclusions
                    and conclusions <= success_conclusions
                    else "failure"
                )
                break
            if time.monotonic() >= deadline:
                raise ValueError("candidate CI timed out")
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
        return CICheckReceipt(
            provider="github",
            request_id=request.request_id,
            candidate_commit=request.candidate_commit,
            status=status,
            details_url=details_url,
        )

    def _verify_candidate(
        self,
        kwargs: Mapping[str, object],
    ) -> dict:
        task_id = str(kwargs.get("task_id") or "")
        path = self._evidence_dir(
            task_id, SOURCE_CANDIDATE_PIPELINE
        ) / "result.json"
        if not path.is_file():
            raise ValueError("candidate lineage evidence is missing")
        payload = json.loads(path.read_text(encoding="utf-8"))
        lineage = CandidateLineage.from_dict(payload["lineage"])
        if not lineage.verified():
            raise ValueError("stored candidate lineage is not verified")
        workspace = Path(lineage.snapshot.workspace)
        remote = self._git(
            workspace,
            "ls-remote",
            "origin",
            f"refs/heads/{lineage.candidate.branch}",
        ).split()
        if not remote or remote[0] != lineage.candidate.commit:
            raise ValueError("remote candidate commit changed")
        request = self._create_receipt_from_view(
            workspace, lineage.change_request
        )
        ci = self._read_ci(
            workspace,
            request,
            timeout_seconds=float(kwargs.get("ci_timeout") or 600),
        )
        remote = self._git(
            workspace,
            "ls-remote",
            "origin",
            f"refs/heads/{lineage.candidate.branch}",
        ).split()
        if not remote or remote[0] != lineage.candidate.commit:
            raise ValueError("remote candidate commit changed during CI")
        request = self._create_receipt_from_view(
            workspace, lineage.change_request
        )
        verified = CandidateLineage(
            snapshot=lineage.snapshot,
            candidate=lineage.candidate,
            change_request=request,
            ci=ci,
        )
        if not verified.verified():
            raise ValueError("candidate verification failed")
        return {
            "lineage": verified.to_dict(),
            "lineage_fingerprint": verified.fingerprint(),
            "files": list(verified.snapshot.files),
        }

    def _create_receipt_from_view(
        self,
        workspace: Path,
        expected: ChangeRequestReceipt,
    ) -> ChangeRequestReceipt:
        view = json.loads(self._run([
            "gh", "pr", "view", expected.url,
            "--json",
            "number,url,isDraft,headRefName,baseRefName,headRefOid",
        ], cwd=workspace).stdout)
        current = ChangeRequestReceipt(
            provider="github",
            request_id=str(view["number"]),
            url=str(view["url"]),
            draft=bool(view["isDraft"]),
            base_commit=expected.base_commit,
            source_branch=str(view["headRefName"]),
            target_branch=str(view["baseRefName"]),
            candidate_commit=str(view["headRefOid"]),
            change_digest=expected.change_digest,
        )
        if (
            current.request_id != expected.request_id
            or current.url != expected.url
            or current.source_branch != expected.source_branch
            or current.target_branch != expected.target_branch
            or current.candidate_commit != expected.candidate_commit
        ):
            raise ValueError(
                "change request identity changed during verification"
            )
        return current

    def _evidence_dir(
        self,
        task_id: str,
        capability: CapabilityId,
    ) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", task_id)[:100]
        return self.data_dir / safe / capability.value.replace(".", "-")

    def _write_evidence(
        self,
        task_id: str,
        capability: CapabilityId,
        output: dict,
    ) -> Path:
        root = self._evidence_dir(task_id, capability)
        root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(root / "result.json", output)
        return root
