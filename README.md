# agentic-football

Rule-based player agent for the AWS Agentic Football Cup (Amazon Bedrock AgentCore Runtime).
One codebase serves all 5 players; the role comes from `myPlayers[0]` in the payload.

## Layout

| Path | Purpose |
|------|---------|
| `agent.py` | AgentCore entrypoint, state parsing, rule engine, output adapter |
| `tests/` | Offline validator, mirrored scenarios for both teams, fuzzing |
| `scripts/deploy.ps1` | Windows deploy: checks, tests gate, configure/deploy, ARNs |
| `scripts/agent_arns.py` | Reads ARNs from `.bedrock_agentcore.yaml` |
| `sample_payload.json` | Example invocation payload |
| `.kiro/steering/football.md` | Project context for Kiro |

## Setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt bedrock-agentcore-starter-toolkit
```

## Offline tests

```powershell
python -m tests.run              # scenarios x2 teams, 500 fuzz payloads, malformed input, serializer
python -m tests.run --fuzz 3000  # heavier fuzzing
python -m tests.run -v           # print every decision
```

## Local run

```powershell
python agent.py
# second terminal:
Invoke-RestMethod -Uri http://localhost:8080/invocations -Method Post -ContentType "application/json" -InFile .\sample_payload.json | ConvertTo-Json -Depth 5
```

## Deploy

Set workshop credentials first (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`), then:

```powershell
.\scripts\deploy.ps1                                   # one agent, register its ARN 5 times
.\scripts\deploy.ps1 -AgentEnv "LOG_RAW_PAYLOAD=true"  # first deploy: log raw payloads to CloudWatch
.\scripts\deploy.ps1 -Players 5                        # five identical agents if the portal rejects duplicates
.\scripts\deploy.ps1 -DeploymentType direct_code_deploy  # zip deploy, no container build
```

ARNs are printed and saved to `arns.txt`.

## Runtime env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `LOG_RAW_PAYLOAD` | `false` | Log raw payloads (first 8 KB) for format discovery |
| `USE_LLM_COACH` | `false` | Parse free-form coach text with Nova Micro when keywords miss |
| `COACH_MODEL_ID` | `us.amazon.nova-micro-v1:0` | Bedrock model for coach parsing |
| `CMD_PLAYER_KEY` | `playerId` | Wire key for the player id |
| `CMD_TYPE_KEY` | `commandType` | Wire key for the command name |
| `CMD_PARAMS_KEY` | `parameters` | Wire key for parameters; empty string = flat parameters |
| `RESPONSE_WRAPPER` | empty | Wrap response as `{"<value>": [...]}`; empty = bare list |

Example: platform expects `{"commands": [{"playerId": 2, "type": "PASS", "params": {...}}]}`:

```powershell
.\scripts\deploy.ps1 -AgentEnv "CMD_TYPE_KEY=type","CMD_PARAMS_KEY=params","RESPONSE_WRAPPER=commands"
```

## Open questions to settle at the event

1. Exact command wire format and response wrapper (fix via env vars above).
2. `TL/TR/BL/BR` mapping to the y axis (`pick_corner()` in `agent.py`).
3. Goal width (`GOAL_HALF_WIDTH`) and whether the portal accepts one ARN for all 5 players.
