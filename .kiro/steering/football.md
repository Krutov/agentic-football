---
inclusion: always
---

# Agentic Football Cup - project context

## Goal
Python agent deployed to Amazon Bedrock AgentCore Runtime (us-east-1) that controls
one footballer in a 5v5 match. The same code serves all 5 players.

## Runtime contract
- Invoked ~every 2 s per player; hard timeout 5 s (on timeout the player keeps its last command).
- Entry point: `agent.py`, `BedrockAgentCoreApp` + `@app.entrypoint`.
- Input: `{"prompt": <JSON string or object>}` wrapping `{"gameState": {...}, "teamId": 0|1, "myPlayers": [idx]}`; idx 0 is always the GK.
- `teamId` 0 (HOME) attacks +x, 1 (AWAY) attacks -x. Pitch x -55..55, y -35..35.
- `gameState.teamChat` holds the latest coach instructions from the Player Portal.
- Output: streamed JSON string of `[{commandType, playerId, teamId, parameters, duration}]` (same as official sample lib/agent_base.py).

## Commands
- MOVE_TO(target_x, target_y, sprint)
- FOLLOW_PLAYER(target_player_id, target_team HOME|AWAY, distance)
- SHOOT(aim_location TL|TR|BL|BR|CENTER, power 0..1) - requires possession
- PASS(target_player_id, type GROUND|AERIAL|THROUGH) - requires possession
- GK_DISTRIBUTE(target_player_id, method THROW|KICK) - GK only
- PRESS_BALL(intensity 0..1) - >0.5 sprints, >0.3 tackles
- MARK(target_player_id, tightness LOOSE|TIGHT)
- INTERCEPT(aggressive)
- SET_STANCE(stance 0 balanced | 1 attacking | 2 defensive)
- CLEAR_OVERRIDE, RESET

## Rules
- 2-minute matches (MATCH_LENGTH env), draw -> Golden Goal (next goal wins). No outs, offsides, cards or fouls.
- After a goal: kickoff from center, conceding team kicks off.

## Engineering rules
- No LLM call in the per-tick hot path; decisions must take < 50 ms.
- LLM (Amazon Nova Micro via bedrock-runtime converse) only for parsing coach text, cached, timeout <= 2 s.
- Never raise from the entrypoint; on error return [] and log.
- Code comments, log messages and output strings in English only.
- Log one JSON line per decision: tick, player, mode, commands, ms.
- Wire format is produced only by serialize()/wrap_response(); change it via CMD_* / RESPONSE_WRAPPER env vars, not in the decision logic.
- After any change run `python -m tests.run` and keep it green; add a scenario in tests/scenarios.py for every new behavior (scenarios are mirrored for both teams).
- Deploy with `.\scripts\deploy.ps1` (wraps team/deploy.py: AgentCore CLI + CDK, 5 runtimes football_p0..p4); it runs the tests first.
