"""Print configured AgentCore agents and their ARNs from .bedrock_agentcore.yaml as JSON.

Used by scripts/deploy.ps1. Output: {"<agent name>": "<arn or empty>", ...}
"""

import json
import sys
from pathlib import Path

import yaml


def main():
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".bedrock_agentcore.yaml")
    if not cfg_path.exists():
        print("{}")
        return
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    result = {}
    for name, agent in (cfg.get("agents") or {}).items():
        info = (agent or {}).get("bedrock_agentcore") or {}
        result[name] = info.get("agent_arn") or ""
    print(json.dumps(result))


if __name__ == "__main__":
    main()
