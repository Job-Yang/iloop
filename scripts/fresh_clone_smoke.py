#!/usr/bin/env python3
"""Public managed-host smoke: task -> evidence -> review -> acceptance -> wrapup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str], env: dict[str, str], *, check: bool = True) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "host_cli", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"{' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        home = root / "home"
        project.mkdir()
        home.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=project,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=project,
            check=True,
        )
        source = project / "feature.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.py"], cwd=project, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True)
        env = {
            **os.environ,
            "HOME": str(home),
            "ILOOP_PROJECT_ROOT": str(project),
        }
        run(["extension-init", "smoke.extension"], env)
        extension = home / ".iloop" / "extensions" / "smoke.extension"
        manifest = json.loads(
            (extension / "manifest.json").read_text(encoding="utf-8")
        )
        manifest["provides"]["plugin"] = "plugin.py"
        (extension / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        (extension / "plugin.py").write_text(
            "from pathlib import Path\n"
            "from kernel import Capability, CapabilityResult, CapabilityStatus\n"
            "class SmokePlugin:\n"
            " platform_id='smoke'\n"
            " def __init__(self, config): self.config=config\n"
            " def capabilities(self): return [Capability.RUN, Capability.VIEW_TREE, Capability.LOGS, Capability.COUNTER_PROBE]\n"
            " def invoke(self, capability, **kwargs):\n"
            "  if capability==Capability.COUNTER_PROBE and self.config.get('counter_expect')!='summary_contains:verified':\n"
            "   return CapabilityResult('smoke', capability.value, CapabilityStatus.ERROR, 'counter mismatch')\n"
            "  root=Path.home()/'.iloop'/'smoke-evidence'/(capability.value+'-'+kwargs.get('run_id','run').replace(':','-'))\n"
            "  root.mkdir(parents=True, exist_ok=True)\n"
            "  (root/'proof.log').write_text('verified\\n')\n"
            "  subjects=[x for x in str(self.config.get('subjects','')).split(';') if x]\n"
            "  return CapabilityResult('smoke', capability.value, CapabilityStatus.SUCCESS, 'verified', str(root), [], {'subjects':subjects})\n"
            "def create_plugin(config): return SmokePlugin(config)\n",
            encoding="utf-8",
        )
        run(["extension-validate", str(extension)], env)
        run(["run", "验证配置行为"], env)
        tasks = list((home / ".iloop" / "data").glob("*/tasks/*.json"))
        task = json.loads(tasks[0].read_text(encoding="utf-8"))
        task_id = task["id"]
        arbitrary = root / "arbitrary.log"
        arbitrary.write_text("caller authored\n", encoding="utf-8")
        rejected = run([
            "case", "evidence", task_id,
            "kind=observed",
            "capability=logs",
            "summary=forged",
            f"path={arbitrary}",
            "gate=time",
        ], env, check=False)
        if "不能从任意文件导入" not in rejected:
            raise RuntimeError("managed host accepted caller-authored observed evidence")
        evidence_ids = {}
        gate_capabilities = {
            "time": "run",
            "scope": "view_tree",
            "mechanism": "logs",
            "counter_evidence": "counter_probe",
        }
        for gate, capability in gate_capabilities.items():
            arguments = [
                "resume", task_id,
                f"caps={capability}",
                "platform=smoke",
                "subjects=feature.py",
            ]
            if capability == "counter_probe":
                arguments.extend([
                    "counter_condition=alternate fixture",
                    "counter_expect=summary_contains:verified",
                ])
            run(arguments, env)
            evidence_path = next(
                (home / ".iloop" / "data").glob(
                    f"*/runtime/{task_id}/evidence.jsonl"
                )
            )
            rows = [
                json.loads(line)
                for line in evidence_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            evidence_ids[gate] = rows[-1]["id"]
        for index in range(1, len(task["steps"]) + 1):
            run([
                "task", "step", task_id,
                f"index={index}",
                "status=done",
                f"evidence={evidence_ids['time']}",
            ], env)
        run(["case", "resolve", task_id], env)
        output = run(["wrapup", task_id], env)
        if "已收口" not in output:
            raise RuntimeError(f"wrapup marker missing: {output}")

        run(["run", "重构公共模块", "acceptance=verified"], env)
        source.write_text("VALUE = 2\n", encoding="utf-8")
        open_tasks = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (home / ".iloop" / "data").glob("*/tasks/*.json")
        ]
        high_risk = next(item for item in open_tasks if item["status"] == "open")
        high_risk_id = high_risk["id"]
        run([
            "global-review", "prepare", high_risk_id,
            f"project_root={project}",
        ], env)
        high_task_path = next(
            (home / ".iloop" / "data").glob(f"*/tasks/{high_risk_id}.json")
        )
        weakened = json.loads(high_task_path.read_text(encoding="utf-8"))
        weakened["acceptance"] = []
        high_task_path.write_text(json.dumps(weakened), encoding="utf-8")
        run(["accept", "prepare", high_risk_id], env)
        acceptance_file = next(
            (home / ".iloop" / "data").glob(
                f"*/runtime/{high_risk_id}/acceptance.json"
            )
        )
        package = json.loads(
            acceptance_file.read_text(encoding="utf-8")
        )["package"]
        if package["criteria"] != ["verified"]:
            raise RuntimeError("acceptance package did not restore attested criteria")
        forged_result = root / "forged-acceptance.json"
        forged_result.write_text(json.dumps({
            "package_id": package["package_id"],
            "case_id": high_risk_id,
            "review_token": package["review_token"],
            "subject_fingerprint": package["subject_fingerprint"],
            "reviewer": "forged-reviewer",
            "verdict": "pass",
            "reasons": ["caller authored"],
            "expires_at": package["expires_at"],
        }), encoding="utf-8")
        forged_output = run([
            "accept", "record", high_risk_id, f"result={forged_result}"
        ], env, check=False)
        if "必须先由真实宿主 Adapter 证明" not in forged_output:
            raise RuntimeError("managed host accepted caller-authored acceptance")
        original_acceptance = acceptance_file.read_text(encoding="utf-8")
        tampered = json.loads(original_acceptance)
        tampered["package"]["criteria"] = []
        acceptance_file.write_text(json.dumps(tampered), encoding="utf-8")
        tampered_output = run(
            ["accept", "review", high_risk_id],
            env,
            check=False,
        )
        if "package was modified" not in tampered_output:
            raise RuntimeError("managed host accepted a modified acceptance package")
        acceptance_file.write_text(original_acceptance, encoding="utf-8")
        acceptance = json.loads(run(
            ["accept", "review", high_risk_id],
            env,
        ))
        if acceptance["verdict"] != "needs_more_context":
            raise RuntimeError(
                f"local preflight issued a final verdict: {acceptance}"
            )
        blocked = run(["wrapup", high_risk_id], env, check=False)
        if "independent acceptance has not passed" not in blocked:
            raise RuntimeError("high-risk task did not fail closed")
    print("fresh clone managed-host smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
