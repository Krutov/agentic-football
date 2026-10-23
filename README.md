# agentic-football

Rule-based player agent for the AWS Agentic Football Cup (Amazon Bedrock AgentCore Runtime).
One codebase serves all 5 players; the role comes from `myPlayers[0]` in the payload.

## Layout

| Path | Purpose |
|------|---------|
| `agent.py` | AgentCore entrypoint, state parsing, rule engine, output adapter |
| `tests/` | Offline validator, mirrored scenarios for both teams, fuzzing |
| `scripts/deploy.ps1` | Windows wrapper for `team/deploy.py` |
| `team/` | AgentCore CLI + CDK project (5 runtimes) and `deploy.py` |
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

Same pipeline as the official workshop sample teams: AgentCore CLI + CDK, five runtimes
`football_p0..p4` (same code; role comes from `myPlayers`).

Prerequisites (one-time): Node.js 20+, `npm install -g @aws/agentcore aws-cdk`, [uv](https://docs.astral.sh/uv/).
Clone to a short path outside OneDrive.

Set workshop credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`), then:

```powershell
.\scripts\deploy.ps1              # tests -> npm install/cdk bootstrap -> agentcore deploy -> ARNs
.\scripts\deploy.ps1 -SkipTests
```

ARNs are printed and saved to `arns.txt`: register `football_p0` as player 0 (GK), `p1..p4` as players 1..4.

## Wire contract

Matches `lib/agent_base.py` of the official sample agents:

- input: `{"prompt": "<JSON string or object with gameState / teamId / myPlayers>"}`
- output: streamed JSON string `[{"commandType", "playerId", "teamId", "parameters", "duration"}]`

## Runtime env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `LOG_RAW_PAYLOAD` | `first` | `first` = log first `RAW_LOG_LIMIT` (3) payloads per container, `true` = all, `false` = none |
| `USE_LLM_COACH` | `false` | Parse free-form coach text with Nova Micro when keywords miss |
| `COACH_MODEL_ID` | `us.amazon.nova-micro-v1:0` | Bedrock model for coach parsing |
| `CMD_PLAYER_KEY` | `playerId` | Wire key for the player id |
| `CMD_TYPE_KEY` | `commandType` | Wire key for the command name |
| `CMD_PARAMS_KEY` | `parameters` | Wire key for parameters; empty string = flat parameters |
| `RESPONSE_WRAPPER` | empty | Wrap response as `{"<value>": [...]}`; empty = bare list |


## Open questions

1. `TL/TR/BL/BR` mapping to the y axis (`pick_corner()` in `agent.py`).
2. Goal width (`GOAL_HALF_WIDTH`).
