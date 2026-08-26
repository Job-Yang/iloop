"""Focused zero-dependency regression checks for iLoop M5-M7."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path

from installer import Installer
from acceptance_batch_runner import run_acceptance_batch
from kernel import (
    AcceptanceBatch, AcceptancePackage, AcceptanceShard, AcceptanceStore,
    ActionCatalog, ActionSideEffect, ActionSpec, AssistantRecipe,
    AssistantSuite, AuthorizationGrant,
    CapabilityCatalog, CapabilityResult, CapabilitySpec, CapabilityStatus,
    Case, CaseStatus, DeploymentProfile, DiagnosisStatus, FlowRegistry,
    EvidenceArtifact, HMACAuthorizationAuthority, HostTrustStore, Ledger,
    ProviderRegistry,
    RecipeCatalog, Runtime, SmokeCheck, SuiteManifest, analyze_global_impact,
    SmokeReceipt, SuiteMember, authorize_tool_use, load_installed_application,
    load_installed_plugins, scaffold_extension,
)
from plugins.git_native import (
    GitNativeProvider, REFERENCE_ASSISTANT_ID, register_reference_bugfix,
)


ROOT = Path(__file__).resolve().parent


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class _DynamicProvider:
    platform_id = "dynamic"

    def __init__(
        self,
        root: Path,
        capability: str,
        *,
        side_effect=None,
        version: str = "1",
    ):
        self.root = root
        self.capability = capability
        self.calls = 0
        self.side_effect = side_effect
        self.version = version

    def capabilities(self):
        return [self.capability]

    def runtime_fingerprint(self):
        return hashlib.sha256(json.dumps({
            "provider": self.platform_id,
            "capability": self.capability,
            "root": str(self.root.resolve()),
            "version": self.version,
        }, sort_keys=True).encode("utf-8")).hexdigest()

    def readiness(self, capability):
        return {"runtime_ready": True}

    def invoke(self, capability, **kwargs):
        self.calls += 1
        if self.side_effect is not None:
            self.side_effect()
        evidence = self.root / f"evidence-{self.calls}"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "proof.txt").write_text("ok", encoding="utf-8")
        return CapabilityResult(
            self.platform_id,
            str(capability),
            CapabilityStatus.SUCCESS,
            "ok",
            str(evidence),
            metadata={"outputs": {"result": "ok"}},
        )


class _FakeGitHubRunner:
    def __init__(self):
        from kernel import CommandRunner
        self.base = CommandRunner()
        self.pr = {}
        self.ci_head_sha_override = ""
        self.on_checks = None

    def run(self, argv, *, cwd=None, timeout=600, allow_dangerous=False):
        from kernel import CommandOutput
        if argv[0] != "gh":
            return self.base.run(
                argv,
                cwd=cwd,
                timeout=timeout,
                allow_dangerous=allow_dangerous,
            )
        started = 0.0
        if argv[1:3] == ["pr", "create"]:
            self.pr = {
                "number": 17,
                "url": "https://github.example/pr/17",
                "isDraft": True,
                "headRefName": argv[argv.index("--head") + 1],
                "baseRefName": argv[argv.index("--base") + 1],
                "headRefOid": _git(Path(cwd), "rev-parse", "HEAD"),
            }
            output = self.pr["url"] + "\n"
        elif argv[1:3] == ["pr", "view"]:
            output = json.dumps(self.pr)
        elif argv[1:3] == ["repo", "view"]:
            output = json.dumps({
                "nameWithOwner": "example/iloop",
            })
        elif argv[1] == "api":
            requested_sha = argv[2].split("/commits/", 1)[1].split("/", 1)[0]
            if self.on_checks is not None:
                self.on_checks()
            output = json.dumps({
                "check_runs": [{
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.example/check/1",
                    "head_sha": (
                        self.ci_head_sha_override or requested_sha
                    ),
                }],
            })
        else:
            return CommandOutput(
                list(argv), 2, "", "unsupported fake gh command", started
            )
        return CommandOutput(list(argv), 0, output, "", started)


def _flow_registry() -> FlowRegistry:
    registry = FlowRegistry()
    registry.load_json(ROOT / "workflow" / "flows.json")
    return registry


def _initialize_remote(root: Path) -> tuple[Path, Path, str]:
    remote = root / "remote.git"
    seed = root / "seed"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True)
    _git(seed, "config", "user.email", "selftest@example.com")
    _git(seed, "config", "user.name", "Selftest")
    (seed / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(seed, "add", "feature.py")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    return remote, seed, _git(seed, "rev-parse", "HEAD")


def run_checks(check) -> None:
    from kernel import __version__
    check(
        "M5-M7: 稳定入口版本与 VERSION 一致",
        (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        == __version__
        == "0.4.0",
    )
    dynamic = CapabilitySpec(
        capability_id="example.dynamic_probe",
        description="Dynamic probe",
        inputs={"value": "string"},
        outputs={"result": "string"},
        side_effect="read",
    )
    capabilities = CapabilityCatalog()
    capabilities.register(dynamic)
    actions = ActionCatalog(capabilities)
    actions.register(ActionSpec(
        action_id="example.dynamic_action",
        description="Use a dynamic capability",
        required_capabilities=("example.dynamic_probe",),
    ))
    recipes = RecipeCatalog(actions)
    recipes.register(AssistantRecipe(
        assistant_id="example.dynamic_assistant",
        actions=("example.dynamic_action",),
    ))
    with tempfile.TemporaryDirectory() as directory:
        provider = _DynamicProvider(
            Path(directory), "example.dynamic_probe"
        )
        providers = ProviderRegistry(
            [provider], capability_catalog=capabilities
        )
        providers.validate_capabilities(["example.dynamic_probe"])
        result = providers.invoke(
            "example.dynamic_probe", value="ok"
        )
        check(
            "M5: 扩展可注册并调用动态 Driver Capability",
            result.ok() and provider.calls == 1,
        )
    unknown_rejected = False
    try:
        actions.register(ActionSpec(
            action_id="example.unknown_capability",
            description="Unknown capability",
            required_capabilities=("example.missing",),
        ))
    except ValueError:
        unknown_rejected = True
    check(
        "M5: Action 引用未知动态 Capability 时 fail closed",
        unknown_rejected,
    )
    built_in_effect_rejected = False
    try:
        ActionCatalog().register(ActionSpec(
            action_id="example.undeclared_install",
            description="Install without declaring its external write",
            required_capabilities=("install",),
        ))
    except ValueError:
        built_in_effect_rejected = True
    check(
        "M5: 内置副作用 Capability 不能被 Action 漏报",
        built_in_effect_rejected,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        extension = scaffold_extension("example.dynamic", root)
        manifest_path = extension.root / "manifest.json"
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
        manifest["provides"]["plugin"] = "plugin.py"
        manifest["provides"]["application"] = "application.py"
        manifest["provides"]["provider_bindings"] = {
            "example.dynamic.fetch": "dynamic_ext"
        }
        manifest_path.write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (extension.root / "capabilities.json").write_text(
            json.dumps([{
                "capability_id": "example.dynamic.fetch",
                "description": "Fetch example data",
                "side_effect": "read",
            }]),
            encoding="utf-8",
        )
        (extension.root / "actions.json").write_text(
            json.dumps([{
                "action_id": "example.dynamic.collect",
                "description": "Collect example data",
                "required_capabilities": ["example.dynamic.fetch"],
            }]),
            encoding="utf-8",
        )
        (extension.root / "recipes.json").write_text(
            json.dumps([{
                "assistant_id": "example.dynamic.agent",
                "actions": ["example.dynamic.collect"],
            }]),
            encoding="utf-8",
        )
        (extension.root / "application.py").write_text(
            "def create_action_handlers(config):\n"
            " return {'example.dynamic.collect': lambda payload: {}}\n",
            encoding="utf-8",
        )
        (extension.root / "plugin.py").write_text(
            "from kernel import CapabilityResult, CapabilityStatus\n"
            "class Provider:\n"
            " platform_id='dynamic_ext'\n"
            " def capabilities(self): return ['example.dynamic.fetch']\n"
            " def invoke(self, capability, **kwargs):\n"
            "  return CapabilityResult(self.platform_id, str(capability), "
            "CapabilityStatus.SUCCESS, 'ok')\n"
            "def create_plugin(config): return Provider()\n",
            encoding="utf-8",
        )
        extension_actions = ActionCatalog()
        extension_recipes = RecipeCatalog(extension_actions)
        counts, bindings, issues = load_installed_application(
            root, extension_actions, extension_recipes
        )
        extension_providers = ProviderRegistry(
            load_installed_plugins(root),
            capability_catalog=extension_actions.capabilities,
        )
        for capability, provider_id in bindings.items():
            extension_providers.bind(capability, provider_id)
        extension_providers.validate_capabilities(
            extension_recipes.assemble(
                "example.dynamic.agent"
            ).required_capabilities
        )
        check(
            "M5: 扩展 manifest 可声明 Capability/Action/Recipe/Provider",
            counts == (1, 1)
            and not issues
            and extension_providers.resolve(
                "example.dynamic.fetch"
            ).platform_id == "dynamic_ext",
        )
        bad = scaffold_extension("example.badbinding", root)
        bad_manifest_path = bad.root / "manifest.json"
        bad_manifest = json.loads(
            bad_manifest_path.read_text(encoding="utf-8")
        )
        bad_manifest["provides"]["provider_bindings"] = {
            "example.missing": "dynamic_ext"
        }
        bad_manifest_path.write_text(
            json.dumps(bad_manifest), encoding="utf-8"
        )
        reloaded_actions = ActionCatalog()
        reloaded_recipes = RecipeCatalog(reloaded_actions)
        _, bad_bindings, bad_issues = load_installed_application(
            root, reloaded_actions, reloaded_recipes
        )
        check(
            "M5: 未声明 Capability 的 Provider binding 被隔离",
            "example.missing" not in bad_bindings
            and any(
                "unknown capability 'example.missing'" in item.message
                for item in bad_issues
            )
            and reloaded_recipes.get(
                "example.dynamic.agent"
            ).assistant_id == "example.dynamic.agent",
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        writes = {"count": 0}
        write_capabilities = CapabilityCatalog()
        write_capabilities.register(CapabilitySpec(
            capability_id="example.write",
            description="External write",
            side_effect="external_write",
        ))
        write_actions = ActionCatalog(write_capabilities)
        write_actions.register(
            ActionSpec(
                action_id="example.authorized_write",
                description="Authorized write",
                side_effects=(ActionSideEffect.EXTERNAL_WRITE,),
                inputs={"ticket_id": "string"},
                required_capabilities=("example.write",),
            ),
            lambda payload: {},
        )
        write_recipes = RecipeCatalog(write_actions)
        write_recipes.register(AssistantRecipe(
            assistant_id="example.authorized_assistant",
            actions=("example.authorized_write",),
        ))
        provider = _DynamicProvider(
            root / "provider",
            "example.write",
            side_effect=lambda: writes.__setitem__(
                "count", writes["count"] + 1
            ),
        )
        providers = ProviderRegistry(
            [provider], capability_catalog=write_capabilities
        )
        trust = HostTrustStore(root / "trust")
        authority = HMACAuthorizationAuthority(
            b"m5-runtime-authorization-secret-32"
        )
        runtime = Runtime(
            root / "data",
            _flow_registry(),
            project_root=ROOT,
            recipe_catalog=write_recipes,
            provider_registry=providers,
            attestation_recorder=trust.attest,
            attestation_verifier=trust.verify,
            authorization_verifier=authority,
        )
        task = runtime.start(
            "实现功能",
            assistant_id="example.authorized_assistant",
            executor_id="selftest",
        )
        frozen_catalog_rejected = False
        try:
            write_capabilities.register(CapabilitySpec(
                capability_id="example.late",
                description="Late mutation",
            ))
        except ValueError:
            frozen_catalog_rejected = True
        missing_rejected = False
        try:
            runtime.execute_assistant(
                task, {"ticket_id": "T-1"}
            )
        except ValueError:
            missing_rejected = True
        grant = authority.issue(
            subject="selftest",
            kind="automation",
            allowed_actions=("example.authorized_write",),
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
        )
        wrong = AuthorizationGrant.from_dict({
            **grant.to_dict(),
            "task_id": "other-task",
        })
        wrong_rejected = False
        try:
            runtime.execute_assistant(
                task,
                {"ticket_id": "T-1"},
                authorization=wrong,
            )
        except ValueError:
            wrong_rejected = True
        policy_digest = runtime._policy_digest(task.id)
        expired = authority.issue(
            subject="selftest",
            kind="automation",
            allowed_actions=("example.authorized_write",),
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=policy_digest,
            now=1,
            ttl_seconds=1,
        )
        grant_boundaries_hold = (
            not authority.verify(
                expired,
                action_id="example.authorized_write",
                task_id=task.id,
                case_id=task.id,
                diagnosis_revision=0,
                policy_digest=policy_digest,
                now=time.time(),
            )
            and not authority.verify(
                grant,
                action_id="example.other_write",
                task_id=task.id,
                case_id=task.id,
                diagnosis_revision=0,
                policy_digest=policy_digest,
            )
            and not authority.verify(
                grant,
                action_id="example.authorized_write",
                task_id=task.id,
                case_id="other-case",
                diagnosis_revision=0,
                policy_digest=policy_digest,
            )
            and not authority.verify(
                grant,
                action_id="example.authorized_write",
                task_id=task.id,
                case_id=task.id,
                diagnosis_revision=1,
                policy_digest=policy_digest,
            )
        )
        missing_input_result = runtime.execute_assistant(
            task, authorization=grant
        )
        missing_input_rejected = (
            missing_input_result.status.value == "blocked"
            and provider.calls == 0
        )
        task = runtime.execute_assistant(
            task,
            {"ticket_id": "T-1"},
            authorization=grant,
        )
        direct_task = runtime.start(
            "实现功能",
            capabilities=["example.write"],
            executor_id="selftest",
        )
        direct_calls_before = provider.calls
        direct_missing_rejected = False
        try:
            runtime.execute_capabilities(
                direct_task, ["example.write"]
            )
        except ValueError:
            direct_missing_rejected = True
        action_grant_rejected = False
        try:
            runtime.execute_capabilities(
                direct_task,
                ["example.write"],
                authorization=grant,
            )
        except ValueError:
            action_grant_rejected = True
        direct_grant = authority.issue(
            subject="selftest",
            kind="automation",
            allowed_actions=(),
            allowed_capabilities=("example.write",),
            task_id=direct_task.id,
            case_id=direct_task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(direct_task.id),
        )
        direct_task = runtime.execute_capabilities(
            direct_task,
            ["example.write"],
            authorization=direct_grant,
        )
        check(
            "M5: 副作用 Action 在宿主验签前 Provider 调用为零",
            missing_rejected
            and wrong_rejected
            and missing_input_rejected
            and grant_boundaries_hold
            and frozen_catalog_rejected
            and direct_missing_rejected
            and action_grant_rejected
            and direct_calls_before == 1
            and writes["count"] == 2
            and task.steps[0].status.value == "done",
        )
        allowed, _ = authorize_tool_use(
            {
                "tool_name": "exec_command",
                "tool_input": {"command": "git push origin branch"},
            },
            action_id="example.authorized_write",
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
            grant=None,
            verifier=authority,
            writable_roots=[root],
            cwd=root,
        )
        permitted, _ = authorize_tool_use(
            {
                "tool_name": "exec_command",
                "tool_input": {"command": "git push origin branch"},
            },
            action_id="example.authorized_write",
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
            grant=grant,
            verifier=authority,
            writable_roots=[root],
            cwd=root,
        )
        read_allowed, _ = authorize_tool_use(
            {
                "tool_name": "exec_command",
                "tool_input": {
                    "command": f"git -C {root} status --short"
                },
            },
            action_id="",
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
            grant=None,
            verifier=authority,
            writable_roots=[root],
            cwd=root,
        )
        redirect_blocked, _ = authorize_tool_use(
            {
                "tool_name": "exec_command",
                "tool_input": {"command": "echo unsafe > result.txt"},
            },
            action_id="",
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
            grant=None,
            verifier=authority,
            writable_roots=[root],
            cwd=root,
        )
        unknown_blocked, _ = authorize_tool_use(
            {
                "tool_name": "delete_database",
                "tool_input": {"resource_id": "prod"},
            },
            action_id="example.authorized_write",
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
            grant=grant,
            verifier=authority,
            writable_roots=[root],
            cwd=root,
        )
        unknown_command_blocked, _ = authorize_tool_use(
            {
                "tool_name": "delete_database",
                "tool_input": {"command": "git status"},
            },
            action_id="example.authorized_write",
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
            grant=grant,
            verifier=authority,
            writable_roots=[root],
            cwd=root,
        )
        branch_write_blocked, _ = authorize_tool_use(
            {
                "tool_name": "exec_command",
                "tool_input": {"command": "git branch unauthorized-ref"},
            },
            action_id="example.authorized_write",
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
            grant=None,
            verifier=authority,
            writable_roots=[root],
            cwd=root,
        )
        remote_write_blocked, _ = authorize_tool_use(
            {
                "tool_name": "exec_command",
                "tool_input": {
                    "command": "git remote set-url origin /tmp/attacker"
                },
            },
            action_id="example.authorized_write",
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
            grant=None,
            verifier=authority,
            writable_roots=[root],
            cwd=root,
        )
        sort_output_blocked, _ = authorize_tool_use(
            {
                "tool_name": "exec_command",
                "tool_input": {
                    "command": "sort /dev/null -o /tmp/owned"
                },
            },
            action_id="example.authorized_write",
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
            grant=None,
            verifier=authority,
            writable_roots=[root],
            cwd=root,
        )
        git_output_blocked, _ = authorize_tool_use(
            {
                "tool_name": "exec_command",
                "tool_input": {
                    "command": "git diff --output=/tmp/owned"
                },
            },
            action_id="example.authorized_write",
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=0,
            policy_digest=runtime._policy_digest(task.id),
            grant=None,
            verifier=authority,
            writable_roots=[root],
            cwd=root,
        )
        check(
            "M5: 宿主 Tool Guard 阻断绕过 Action 的直接写命令",
            not allowed
            and permitted
            and read_allowed
            and not redirect_blocked
            and not unknown_blocked
            and not unknown_command_blocked
            and not branch_write_blocked
            and not remote_write_blocked
            and not sort_output_blocked
            and not git_output_blocked,
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        remote, seed, base_commit = _initialize_remote(root)
        runner = _FakeGitHubRunner()
        provider = GitNativeProvider(
            root / "evidence", runner=runner
        )
        workspace = provider.prepare_workspace(
            seed,
            base_commit=base_commit,
            task_id="candidate-task",
        )
        (workspace / "feature.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        result = provider.invoke(
            "source.candidate_pipeline",
            task_id="candidate-task",
            workspace=str(workspace),
            base_commit=base_commit,
            allowed_paths=["feature.py"],
            branch="iloop/candidate-task",
            message="fix: candidate",
            target_branch="main",
        )
        verified = provider.invoke(
            "ci.verify_candidate",
            task_id="candidate-task",
        )
        runner.ci_head_sha_override = "0" * 40
        wrong_ci_commit = provider.invoke(
            "ci.verify_candidate",
            task_id="candidate-task",
        )
        runner.ci_head_sha_override = ""
        race_workspace = provider.prepare_workspace(
            seed,
            base_commit=base_commit,
            task_id="race-task",
            branch="iloop/race-task",
        )
        (race_workspace / "feature.py").write_text(
            "VALUE = 4\n", encoding="utf-8"
        )

        def move_remote_during_ci():
            subprocess.run([
                "git", "--git-dir", str(remote), "update-ref",
                "refs/heads/iloop/race-task", base_commit,
            ], check=True)
            runner.on_checks = None

        runner.on_checks = move_remote_during_ci
        raced = provider.invoke(
            "source.candidate_pipeline",
            task_id="race-task",
            workspace=str(race_workspace),
            base_commit=base_commit,
            allowed_paths=["feature.py"],
            branch="iloop/race-task",
            message="fix: race candidate",
            target_branch="main",
        )
        pr_race_workspace = provider.prepare_workspace(
            seed,
            base_commit=base_commit,
            task_id="pr-race-task",
            branch="iloop/pr-race-task",
        )
        (pr_race_workspace / "feature.py").write_text(
            "VALUE = 5\n", encoding="utf-8"
        )

        def move_pr_target_during_ci():
            runner.pr["baseRefName"] = "release/attacker"
            runner.on_checks = None

        runner.on_checks = move_pr_target_during_ci
        pr_raced = provider.invoke(
            "source.candidate_pipeline",
            task_id="pr-race-task",
            workspace=str(pr_race_workspace),
            base_commit=base_commit,
            allowed_paths=["feature.py"],
            branch="iloop/pr-race-task",
            message="fix: pr race candidate",
            target_branch="main",
        )
        payload_path = (
            root / "evidence" / "candidate-task"
            / "source-candidate_pipeline" / "result.json"
        )
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        payload["lineage"]["change_request"]["candidate_commit"] = "0" * 40
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        tampered = provider.invoke(
            "ci.verify_candidate",
            task_id="candidate-task",
        )
        check(
            "M5: Git 候选、Draft PR 与 CI 绑定同一精确提交",
            result.ok()
            and verified.ok()
            and not wrong_ci_commit.ok()
            and not raced.ok()
            and not pr_raced.ok()
            and not tampered.ok(),
        )
        from kernel import CommandRunner
        runner_a = CommandRunner(
            environment_overrides={"GH_CONFIG_DIR": "one"},
            enforce_redline=True,
        )
        runner_b = CommandRunner(
            environment_overrides={"GH_CONFIG_DIR": "two"},
            enforce_redline=False,
        )
        fingerprint_a = GitNativeProvider(
            root / "fingerprint", runner=runner_a
        ).runtime_fingerprint()
        fingerprint_b = GitNativeProvider(
            root / "fingerprint", runner=runner_b
        ).runtime_fingerprint()

        def runner_one(self, argv, **kwargs):
            return ("one", argv, kwargs)

        def runner_two(self, argv, **kwargs):
            return ("two", argv, kwargs)

        runner_type_one = type(
            "SameRunner",
            (CommandRunner,),
            {"run": runner_one},
        )
        runner_type_two = type(
            "SameRunner",
            (CommandRunner,),
            {"run": runner_two},
        )
        implementation_a = GitNativeProvider(
            root / "fingerprint",
            runner=runner_type_one(),
        ).runtime_fingerprint()
        implementation_b = GitNativeProvider(
            root / "fingerprint",
            runner=runner_type_two(),
        ).runtime_fingerprint()

        def runner_type_with_default(value):
            def run(self, argv, result=value, **kwargs):
                return (result, argv, kwargs)
            return type(
                "DefaultRunner",
                (CommandRunner,),
                {"run": run},
            )

        default_a = GitNativeProvider(
            root / "fingerprint",
            runner=runner_type_with_default("one")(),
        ).runtime_fingerprint()
        default_b = GitNativeProvider(
            root / "fingerprint",
            runner=runner_type_with_default("two")(),
        ).runtime_fingerprint()

        def runner_type_with_closure(value):
            def run(self, argv, **kwargs):
                return (value, argv, kwargs)
            return type(
                "ClosureRunner",
                (CommandRunner,),
                {"run": run},
            )

        closure_a = GitNativeProvider(
            root / "fingerprint",
            runner=runner_type_with_closure("one")(),
        ).runtime_fingerprint()
        closure_b = GitNativeProvider(
            root / "fingerprint",
            runner=runner_type_with_closure("two")(),
        ).runtime_fingerprint()
        check(
            "M6: Provider 指纹覆盖 Runner 的运行配置",
            fingerprint_a != fingerprint_b
            and implementation_a != implementation_b
            and default_a != default_b
            and closure_a != closure_b,
        )
        ref_capabilities = CapabilityCatalog()
        ref_actions = ActionCatalog(ref_capabilities)
        ref_recipes = RecipeCatalog(ref_actions)
        register_reference_bugfix(ref_actions, ref_recipes)
        ref_providers = ProviderRegistry(
            [provider], capability_catalog=ref_capabilities
        )
        assembly = ref_recipes.assemble(REFERENCE_ASSISTANT_ID)
        ref_providers.validate_capabilities(
            assembly.required_capabilities
        )
        check(
            "M5: 开源 BugFix 参考 Recipe 可在无内部平台环境装配",
            assembly.recipe.actions == (
                "builtin.bugfix.inspect",
                "builtin.bugfix.publish_candidate",
                "builtin.bugfix.verify_candidate",
            ),
        )
        trust = HostTrustStore(root / "runtime-trust")
        authority = HMACAuthorizationAuthority(
            b"reference-bugfix-authorization-key-32"
        )
        runtime = Runtime(
            root / "runtime",
            _flow_registry(),
            project_root=seed,
            recipe_catalog=ref_recipes,
            provider_registry=ref_providers,
            attestation_recorder=trust.attest,
            attestation_verifier=trust.verify,
            authorization_verifier=authority,
        )
        task = runtime.start(
            "修复问题",
            assistant_id=REFERENCE_ASSISTANT_ID,
            executor_id="selftest",
        )
        task = runtime.execute_assistant(
            task, project_root=str(seed)
        )
        case = Case.load(task.case_path)
        case.status = CaseStatus.RESOLVED
        case.diagnosis_status = DiagnosisStatus.FROZEN
        case.diagnosis_revision = 1
        plan = case.route_disposition(
            assembly.disposition_actions()
        )
        case.save(task.case_path)
        runtime_workspace = provider.prepare_workspace(
            seed,
            base_commit=base_commit,
            task_id=task.id,
            branch="iloop/runtime-candidate",
        )
        (runtime_workspace / "feature.py").write_text(
            "VALUE = 3\n", encoding="utf-8"
        )
        grant = authority.issue(
            subject="selftest",
            kind="automation",
            allowed_actions=(
                "builtin.bugfix.publish_candidate",
            ),
            task_id=task.id,
            case_id=task.id,
            diagnosis_revision=1,
            policy_digest=runtime._policy_digest(task.id),
        )
        task = runtime.execute_assistant(
            task,
            authorization=grant,
            workspace=str(runtime_workspace),
            base_commit=base_commit,
            allowed_paths=["feature.py"],
            branch="iloop/runtime-candidate",
            message="fix: runtime candidate",
            target_branch="main",
        )
        case = Case.load(task.case_path)
        check(
            "M5: 参考 BugFix Recipe 跑通受控候选与 CI 验证阶段",
            [item.status.value for item in task.steps]
            == ["done", "done", "done"]
            and case.disposition_progress[plan.plan_id].value
            == "completed",
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        suite_capabilities = CapabilityCatalog()
        suite_capabilities.register(CapabilitySpec(
            capability_id="example.smoke",
            description="Suite smoke",
            side_effect="read",
        ))
        suite_actions = ActionCatalog(suite_capabilities)
        suite_actions.register(
            ActionSpec(
                action_id="example.smoke_action",
                description="Smoke action",
                required_capabilities=("example.smoke",),
            ),
            lambda payload: {},
        )
        suite_recipes = RecipeCatalog(suite_actions)
        suite_recipes.register(AssistantRecipe(
            assistant_id="example.smoke_assistant",
            actions=("example.smoke_action",),
        ))
        provider = _DynamicProvider(
            root / "evidence", "example.smoke"
        )
        providers = ProviderRegistry(
            [provider], capability_catalog=suite_capabilities
        )
        profile = DeploymentProfile(
            deployment_id="example.local",
            target_node="local",
            provider_ids=("dynamic",),
        )
        manifest = SuiteManifest(
            suite_id="example.suite",
            members=(
                SuiteMember(
                    "example.smoke_assistant", "example.local"
                ),
            ),
            smoke_checks=(
                SmokeCheck(
                    "smoke",
                    "example.smoke_assistant",
                    "example.local",
                    "example.smoke",
                ),
            ),
            smoke_ttl_seconds=60,
        )
        suite = AssistantSuite(
            manifest,
            suite_recipes,
            {"example.local": profile},
            providers,
            state_dir=root / "suite",
            secret=b"suite-readiness-secret-with-32-bytes",
        )
        installed = suite.install()
        receipt = suite.smoke()
        ready = suite.status()
        replacement_providers = ProviderRegistry(
            [
                _DynamicProvider(
                    root / "evidence",
                    "example.smoke",
                    version="2",
                )
            ],
            capability_catalog=suite_capabilities,
        )
        replacement_suite = AssistantSuite(
            manifest,
            suite_recipes,
            {"example.local": profile},
            replacement_providers,
            state_dir=root / "suite",
            secret=b"suite-readiness-secret-with-32-bytes",
        )
        provider_change_blocked = not replacement_suite.status()[
            "production_ready"
        ]
        smoke_artifact = Path(
            str(receipt.checks[0]["artifact_path"])
        ) / "proof.txt"
        smoke_artifact.write_text("changed", encoding="utf-8")
        changed_artifact_blocked = not suite.status()[
            "production_ready"
        ]
        smoke_artifact.write_text("ok", encoding="utf-8")
        smoke_path = root / "suite" / "smoke.json"
        tampered = json.loads(smoke_path.read_text(encoding="utf-8"))
        tampered["suite_fingerprint"] = "0" * 64
        smoke_path.write_text(json.dumps(tampered), encoding="utf-8")
        blocked = suite.status()
        stale = receipt.verify(
            suite_id=manifest.suite_id,
            suite_fingerprint=suite.compile()["fingerprint"],
            required_checks=("smoke",),
            secret=b"suite-readiness-secret-with-32-bytes",
            now=receipt.expires_at + 1,
        )
        invalid_row = receipt.to_dict()
        invalid_row["signature"] = "0" * 64
        artifact_read = {"value": False}
        original_digest = EvidenceArtifact._artifact_digest

        def unexpected_artifact_read(path):
            artifact_read["value"] = True
            raise AssertionError("artifact accessed before HMAC")

        EvidenceArtifact._artifact_digest = staticmethod(
            unexpected_artifact_read
        )
        try:
            signature_first = not SmokeReceipt.from_dict(
                invalid_row
            ).verify(
                suite_id=manifest.suite_id,
                suite_fingerprint=suite.compile()["fingerprint"],
                required_checks=("smoke",),
                secret=b"suite-readiness-secret-with-32-bytes",
            )
        finally:
            EvidenceArtifact._artifact_digest = staticmethod(
                original_digest
            )
        check(
            "M6: production_ready 只认同配置的新鲜真实 smoke",
            installed["runtime_ready"]
            and ready["production_ready"]
            and changed_artifact_blocked
            and provider_change_blocked
            and not blocked["production_ready"]
            and not stale
            and signature_first
            and not artifact_read["value"],
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        remote, seed, _ = _initialize_remote(root)
        (seed / "AGENT_PROMPT.md").write_text(
            "# iLoop v1\n", encoding="utf-8"
        )
        _git(seed, "add", "AGENT_PROMPT.md")
        _git(seed, "commit", "-m", "add prompt")
        _git(seed, "push")
        home = root / "home"
        installer = Installer(
            home=home,
            smoke_runner=lambda candidate: (
                None if (candidate / "AGENT_PROMPT.md").is_file()
                else (_ for _ in ()).throw(
                    RuntimeError("prompt missing")
                )
            ),
        )
        first = installer.install(str(remote))
        (seed / "AGENT_PROMPT.md").write_text(
            "# iLoop v2\n", encoding="utf-8"
        )
        _git(seed, "add", "AGENT_PROMPT.md")
        _git(seed, "commit", "-m", "update prompt")
        _git(seed, "push")
        second = installer.update()
        old_commit = second["commit"]
        state_path = home / ".iloop" / "install-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["commit"] = "0" * 40
        state_path.write_text(json.dumps(state), encoding="utf-8")
        tampered_state_blocked = not installer.status()["ready"]
        state["commit"] = old_commit
        state_path.write_text(json.dumps(state), encoding="utf-8")
        (seed / "AGENT_PROMPT.md").write_text(
            "# iLoop broken\n", encoding="utf-8"
        )
        _git(seed, "add", "AGENT_PROMPT.md")
        _git(seed, "commit", "-m", "broken candidate")
        _git(seed, "push")
        failing = Installer(
            home=home,
            smoke_runner=lambda candidate: (_ for _ in ()).throw(
                RuntimeError("candidate smoke failed")
            ),
        )
        rollback_kept = False
        try:
            failing.update()
        except RuntimeError:
            rollback_kept = (
                _git(home / ".iloop" / "iloop", "rev-parse", "HEAD")
                == old_commit
            )
        check(
            "M6: 空 HOME 安装注册宿主且失败更新保留旧版本",
            first["status"] == "ready"
            and installer.status()["ready"]
            and tampered_state_blocked
            and rollback_kept,
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        _git(root, "config", "user.email", "selftest@example.com")
        _git(root, "config", "user.name", "Selftest")
        shared = root / "Common" / "Theme.swift"
        shared.parent.mkdir()
        shared.write_text(
            "public let color = 1\n", encoding="utf-8"
        )
        ui = root / "Feature" / "PageView.swift"
        ui.parent.mkdir()
        ui.write_text("struct PageView {}\n", encoding="utf-8")
        web = root / "src" / "views" / "checkout.ts"
        web.parent.mkdir(parents=True)
        web.write_text("export const title = 'old'\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "base")
        shared.write_text(
            "public let color = 2\n", encoding="utf-8"
        )
        ui.write_text(
            "struct PageView { let changed = true }\n",
            encoding="utf-8",
        )
        web.write_text(
            "export const title = 'new'\n",
            encoding="utf-8",
        )
        review = analyze_global_impact(
            root,
            scope_rules={
                "global_shared": ["Common/"],
                "ui_hint": ["View"],
            },
            symptom_is_ui=True,
        )
        attempted_downgrade = analyze_global_impact(
            root,
            scope_rules={"ignore": ["Common/"]},
        )
        automatic_ui_review = analyze_global_impact(root)
        automatic_ui_impact = next(
            item for item in automatic_ui_review.impacts
            if item.target == "Feature/PageView.swift"
        )
        automatic_web_impact = next(
            item for item in automatic_ui_review.impacts
            if item.target == "src/views/checkout.ts"
        )
        visual_floor_rejected = False
        try:
            review.verify(
                review.impacts[0].target,
                ["evidence"],
                resolution="logs only",
                evidence_capabilities=["logs"],
            )
        except ValueError:
            visual_floor_rejected = True
        accepted_visual_rejected = False
        try:
            automatic_ui_review.verify(
                automatic_ui_impact.target,
                ["other-screenshot"],
                accepted=True,
                resolution="accept visual risk",
                user_confirmation_id="human-confirmation",
                evidence_capabilities=["screenshot"],
                evidence_subjects={
                    "other-screenshot": ["Feature/OtherView.swift"]
                },
            )
        except ValueError:
            accepted_visual_rejected = True
        automatic_ui_review.verify(
            automatic_ui_impact.target,
            ["target-screenshot"],
            accepted=True,
            resolution="accept visual risk with target evidence",
            user_confirmation_id="human-confirmation",
            evidence_capabilities=["screenshot"],
            evidence_subjects={
                "target-screenshot": [automatic_ui_impact.target]
            },
        )
        review_state = tempfile.TemporaryDirectory()
        review_path = Path(review_state.name) / "review.json"
        review.save(review_path)
        restored_review = type(review).load(review_path)
        refreshed_review = analyze_global_impact(
            root,
            symptom_is_ui=restored_review.symptom_is_ui,
            scope_rules=restored_review.scope_rules,
        )
        review_state.cleanup()
        check(
            "M7: GlobalReview 由完整影响图给出 R3 且保留视觉地板",
            review.verification_scope == "R3"
            and review.verification_mode == "full"
            and review.visual_required
            and visual_floor_rejected
            and attempted_downgrade.verification_scope == "R3"
            and automatic_ui_impact.visual_required
            and automatic_web_impact.visual_required
            and accepted_visual_rejected
            and automatic_ui_impact.status == "accepted"
            and refreshed_review.fingerprint == review.fingerprint,
        )

    with tempfile.TemporaryDirectory() as directory:
        ledger = Ledger(directory)
        one = ledger.start_timing(
            "capability",
            action_id="a",
            capability_id="logs",
            provider_id="p",
            started_at=100,
        )
        two = ledger.start_timing(
            "capability",
            action_id="b",
            capability_id="screenshot",
            provider_id="p",
            started_at=105,
        )
        ledger.end_timing(
            one.event_id, "success", ended_at=110
        )
        ledger.end_timing(
            two.event_id,
            "failed",
            blocked_seconds=2,
            ended_at=115,
        )
        metrics = ledger.timing_metrics()
        ledger.flush()
        restored = Ledger.load(directory).timing_metrics()
        check(
            "M7: 时间账本可复算耗时、阻塞、重试与并发",
            metrics["wall_seconds"] == 15
            and metrics["work_seconds"] == 20
            and metrics["blocked_seconds"] == 2
            and metrics["retry_events"] == 1
            and metrics["max_concurrency"] == 2
            and restored == metrics,
        )

    fingerprint = hashlib.sha256(b"subject").hexdigest()
    package = AcceptancePackage(
        case_id="case-1",
        goal="Review the frozen M5-M7 change",
        criteria=["core passes", "adapter passes"],
        subject_fingerprint=fingerprint,
        executor_id="implementer",
    )
    batch = AcceptanceBatch(
        batch_id="batch-1",
        package=package,
        shards=(
            AcceptanceShard("core", ["kernel"], [0]),
            AcceptanceShard("adapter", ["plugins"], [1]),
        ),
    )
    rows = [
        {
            "batch_id": "batch-1",
            "package_id": package.package_id,
            "case_id": "case-1",
            "review_token": package.review_token,
            "subject_fingerprint": fingerprint,
            "shard_id": shard,
            "reviewer": reviewer,
            "verdict": "pass",
            "criteria_verdicts": ["pass"],
            "reasons": ["observed evidence passes"],
            "reviewed_at": time.time(),
        }
        for shard, reviewer in (
            ("core", "reviewer-a"),
            ("adapter", "reviewer-b"),
        )
    ]
    by_shard = {row["shard_id"]: row for row in rows}
    from threading import Barrier
    batch_barrier = Barrier(2)

    def review_shard(shard, frozen):
        batch_barrier.wait()
        return by_shard[shard.shard_id]

    aggregated = run_acceptance_batch(
        batch,
        review_shard,
        verify_attestation=lambda row: bool(row["reviewer"]),
    )
    wrong_fingerprint_rejected = False
    try:
        batch.aggregate(
            [{**rows[0], "subject_fingerprint": "0" * 64}, rows[1]],
            verify_attestation=lambda row: True,
        )
    except ValueError:
        wrong_fingerprint_rejected = True
    with tempfile.TemporaryDirectory() as directory:
        store = AcceptanceStore(Path(directory) / "acceptance.json")
        store.prepare(package, batch=batch)
        aggregate_path = Path(directory) / "aggregate.json"
        aggregate_path.write_text(
            json.dumps(aggregated),
            encoding="utf-8",
        )
        recorded = store.record_file(
            aggregate_path,
            verify_attestation=lambda path, row: True,
        )
        restored_result = store.result(
            lambda path, row: True,
            expected_case_id=package.case_id,
        )
    check(
        "M7: 并行验收接入同一 AcceptancePackage 与收口存储",
        aggregated["verdict"] == "pass"
        and wrong_fingerprint_rejected
        and recorded.verdict.value == "pass"
        and restored_result is not None
        and restored_result.verdict.value == "pass",
    )
