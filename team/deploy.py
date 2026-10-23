#!/usr/bin/env python3
"""Deploy the rule-based football agent as 5 AgentCore runtimes (football_p0..p4).

Uses the same pipeline as the official workshop sample teams:
AgentCore CLI (@aws/agentcore) + CDK, one stack, CodeZip runtimes on PYTHON_3_12.

Steps:
  1. Pre-flight: agentcore, aws, node, cdk, uv on PATH; valid AWS credentials.
  2. Offline tests (python -m tests.run) unless --skip-tests.
  3. Stage players/p0..p4: src/main.py (copy of ../agent.py) + pyproject.toml.
  4. agentcore deploy --yes
  5. Print runtime ARNs (and save them to arns.txt in the repo root).

Prerequisites (one-time):
    npm install -g @aws/agentcore aws-cdk
    uv: https://docs.astral.sh/uv/getting-started/installation/

Usage (from the repo root):
    python team/deploy.py
    python team/deploy.py --skip-tests
"""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

TEAM_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEAM_DIR.parent
AGENT_SRC = REPO_ROOT / "agent.py"
PLAYERS_DIR = TEAM_DIR / "players"
RUNTIME_PREFIX = "football_p"
PLAYER_COUNT = 5

PYPROJECT = """[project]
name = "football_p{idx}"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "bedrock-agentcore>=1.17.0",
    "boto3>=1.34.0",
    "aws-opentelemetry-distro>=0.18.0",
]
"""


_NPM_BIN = None


def npm_bin_dir():
    """Directory with global npm CLIs (npm prefix on Windows, <prefix>/bin elsewhere)."""
    global _NPM_BIN
    if _NPM_BIN is None:
        npm = shutil.which("npm")
        prefix = ""
        if npm:
            r = subprocess.run([npm, "config", "get", "prefix"], capture_output=True, text=True)
            prefix = r.stdout.strip() if r.returncode == 0 else ""
        _NPM_BIN = (prefix if os.name == "nt" else os.path.join(prefix, "bin")) if prefix else ""
    return _NPM_BIN


def resolve_exe(name):
    """Resolve a CLI; 'agentcore' must be the npm AgentCore CLI, not the pip starter toolkit."""
    if name == "agentcore" and npm_bin_dir():
        for candidate in ("agentcore.cmd", "agentcore"):
            path = os.path.join(npm_bin_dir(), candidate)
            if os.path.isfile(path):
                return path
        return None
    # npm CLIs are .cmd shims on Windows; shutil.which honors PATHEXT
    return shutil.which(name)


def run(cmd, **kwargs):
    cmd = [resolve_exe(cmd[0]) or cmd[0]] + list(cmd[1:])
    print(f"  $ {' '.join(cmd)}")
    kwargs.setdefault("env", os.environ.copy())
    subprocess.run(cmd, check=True, **kwargs)


def capture(cmd):
    result = subprocess.run([resolve_exe(cmd[0]) or cmd[0]] + list(cmd[1:]), capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def rmtree(path):
    def _onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


def banner(text):
    print("\n==========================================")
    print(f"  {text}")
    print("==========================================")


def preflight():
    banner("Pre-flight checks")
    missing = [t for t in ("node", "npm", "agentcore", "cdk", "uv", "aws") if not resolve_exe(t)]
    if missing:
        print(f"ERROR: missing tools: {', '.join(missing)}")
        print("  Node.js:          winget install -e --id OpenJS.NodeJS.LTS")
        print("  AgentCore + CDK:  npm install -g @aws/agentcore aws-cdk")
        print("  uv:               winget install -e --id astral-sh.uv")
        print("  Then open a new terminal so PATH is refreshed.")
        sys.exit(1)
    print(f"  agentcore: {resolve_exe('agentcore')} ({capture(['agentcore', '--version']) or 'unknown version'})")

    account = capture(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"])
    if not account:
        print("ERROR: no valid AWS credentials (run: aws sts get-caller-identity)")
        sys.exit(1)
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    print(f"  AWS account: {account}")
    print(f"  AWS region:  {region}")
    return account, region


def run_tests():
    banner("Offline tests")
    result = subprocess.run([sys.executable, "-m", "tests.run"], cwd=REPO_ROOT)
    if result.returncode != 0:
        print("ERROR: tests failed, deploy aborted (use --skip-tests to override)")
        sys.exit(1)


def write_targets(account, region):
    targets_file = TEAM_DIR / "agentcore" / "aws-targets.json"
    targets = [{"name": "default", "account": account, "region": region}]
    targets_file.write_text(json.dumps(targets, indent=2) + "\n", encoding="utf-8")


def stage_players():
    banner("Staging player runtimes")
    rmtree(PLAYERS_DIR)
    for idx in range(PLAYER_COUNT):
        src_dir = PLAYERS_DIR / f"p{idx}" / "src"
        src_dir.mkdir(parents=True)
        shutil.copy2(AGENT_SRC, src_dir / "main.py")
        (src_dir.parent / "pyproject.toml").write_text(PYPROJECT.format(idx=idx), encoding="utf-8")
        print(f"  staged {src_dir.parent.relative_to(REPO_ROOT)}")


def print_arns(region):
    banner("Runtime ARNs")
    out = capture(["aws", "bedrock-agentcore-control", "list-agent-runtimes",
                   "--region", region, "--max-results", "100", "--output", "json"])
    found = {}
    if out:
        for rt in json.loads(out).get("agentRuntimes", []):
            name = rt.get("agentRuntimeName", "")
            if name.startswith(RUNTIME_PREFIX):
                found[name] = rt.get("agentRuntimeArn", "")
    if not found:
        print("WARNING: could not list runtimes; check the AgentCore console or 'agentcore status'")
        return
    lines = [f"{name}\t{found[name]}" for name in sorted(found)]
    (REPO_ROOT / "arns.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        print(f"  {line}")
    print("\nRegister football_p0 as player 0 (GK), football_p1..p4 as players 1..4 in the Team Console.")
    print("Saved to arns.txt")


def main():
    parser = argparse.ArgumentParser(description="Deploy the football agent as 5 AgentCore runtimes")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--keep-staged", action="store_true", help="keep team/players after deploy")
    args = parser.parse_args()

    account, region = preflight()
    if not args.skip_tests:
        run_tests()
    write_targets(account, region)

    try:
        cdk_dir = TEAM_DIR / "agentcore" / "cdk"
        if not (cdk_dir / "node_modules").is_dir():
            banner("Installing CDK dependencies (npm install)")
            run(["npm", "install"], cwd=cdk_dir)

        banner("CDK bootstrap (idempotent)")
        run(["cdk", "bootstrap", f"aws://{account}/{region}"])

        stage_players()

        banner("Deploying via AgentCore CLI")
        run(["agentcore", "deploy", "--yes"], cwd=TEAM_DIR,
            env={**os.environ, "AWS_DEFAULT_REGION": region})
    except subprocess.CalledProcessError:
        print("\nERROR: deployment failed, see output above")
        sys.exit(1)
    finally:
        if not args.keep_staged:
            rmtree(PLAYERS_DIR)

    print_arns(region)


if __name__ == "__main__":
    main()
