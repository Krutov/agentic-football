"""
Agentic Football Cup - player agent for Amazon Bedrock AgentCore Runtime.

Design:
  - Deterministic rule engine runs every tick (no LLM in the hot path, a few ms).
  - Optional LLM (Amazon Nova Micro) only translates coach instructions (teamChat)
    into a tactical mode; the result is cached per instruction text.
  - The same code serves all 5 players: role comes from payload["myPlayers"][0].
  - All geometry is normalized so that "we" always attack toward +x.

NOTE: the wire format (commandType/parameters, bare list) is an assumption.
      If the workshop template differs, switch it with the CMD_* and
      RESPONSE_WRAPPER env vars (see serialize()) instead of editing the logic.
"""

import json
import logging
import math
import os
import time

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("football")

# Tunables
GOAL_X = 55.0
OWN_GOAL_X = -55.0
GOAL_HALF_WIDTH = 3.5          # TODO: verify from logs
SHOOT_RANGE = 22.0
SPRINT_STAMINA_MIN = 40.0
PRESS_STAMINA_MIN = 25.0
MATCH_LENGTH = 300.0
LOG_RAW_PAYLOAD = os.getenv("LOG_RAW_PAYLOAD", "false").lower() == "true"
USE_LLM_COACH = os.getenv("USE_LLM_COACH", "false").lower() == "true"
COACH_MODEL_ID = os.getenv("COACH_MODEL_ID", "us.amazon.nova-micro-v1:0")

# Output format adapter (switch via env vars without touching the decision logic)
#   CMD_PLAYER_KEY   key for the player id            (default "playerId")
#   CMD_TYPE_KEY     key for the command name         (default "commandType")
#   CMD_PARAMS_KEY   key for the parameters object    (default "parameters"; empty = flat)
#   RESPONSE_WRAPPER wrap the list as {key: [...]}    (default empty = bare list)
CMD_PLAYER_KEY = os.getenv("CMD_PLAYER_KEY", "playerId")
CMD_TYPE_KEY = os.getenv("CMD_TYPE_KEY", "commandType")
CMD_PARAMS_KEY = os.getenv("CMD_PARAMS_KEY", "parameters")
RESPONSE_WRAPPER = os.getenv("RESPONSE_WRAPPER", "")

MODES = ("balanced", "all_out", "press", "defend", "possession")

# Base shape in normalized coords (attacking +x). Index 0 = GK.
# Keep in sync with the formation selected in the Player Portal.
FORMATION = {
    1: (-32.0, -12.0),  # left back
    2: (-32.0, 12.0),   # right back
    3: (-8.0, 0.0),     # central midfielder
    4: (18.0, 0.0),     # striker
}

KEYWORDS = [
    ("all_out", ["all out", "all-out", "need a goal", "push forward", "attack"]),
    ("press", ["press", "win the ball"]),
    ("defend", ["hold", "defend", "protect", "park the bus", "counter"]),
    ("possession", ["possession", "slow"]),
]

_coach_cache = {}
_bedrock = None


# ---------- helpers ----------

def cmd(player_id, command_type, **params):
    """Build one command in the canonical internal shape (see serialize() for output)."""
    return {"playerId": player_id, "commandType": command_type, "parameters": params}


def serialize(commands, player_key=None, type_key=None, params_key=None):
    """Convert canonical commands into the wire format configured via env vars."""
    player_key = CMD_PLAYER_KEY if player_key is None else player_key
    type_key = CMD_TYPE_KEY if type_key is None else type_key
    params_key = CMD_PARAMS_KEY if params_key is None else params_key
    out = []
    for c in commands:
        item = {player_key: c["playerId"], type_key: c["commandType"]}
        if params_key:
            item[params_key] = dict(c["parameters"])
        else:
            for k, v in c["parameters"].items():
                if k in item:
                    log.warning("Flat format: parameter '%s' collides with a reserved key, skipped", k)
                    continue
                item[k] = v
        out.append(item)
    return out


def wrap_response(items, wrapper=None):
    """Return a bare list or {wrapper: list} depending on RESPONSE_WRAPPER."""
    wrapper = RESPONSE_WRAPPER if wrapper is None else wrapper
    return {wrapper: items} if wrapper else items


def dist(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def closest_to_ball(players, bx, by):
    return min(players, key=lambda p: dist(p["x"], p["y"], bx, by)) if players else None


def nearest_opp_dist(x, y, opps):
    return min((dist(x, y, o["x"], o["y"]) for o in opps), default=99.0)


def lane_clear(ax, ay, bx, by, opps, margin=2.5):
    """True if no opponent stands within `margin` of segment A->B."""
    vx, vy = bx - ax, by - ay
    seg2 = (vx * vx + vy * vy) or 1e-6
    for o in opps:
        t = clamp(((o["x"] - ax) * vx + (o["y"] - ay) * vy) / seg2, 0.0, 1.0)
        if dist(o["x"], o["y"], ax + t * vx, ay + t * vy) < margin:
            return False
    return True


def move(s, x, y, sprint=False):
    me = s["me"]
    sprint = bool(sprint and me["stamina"] >= SPRINT_STAMINA_MIN)
    return cmd(
        me["idx"], "MOVE_TO",
        target_x=round(clamp(x, -54, 54) * s["sign"], 2),  # de-normalize x
        target_y=round(clamp(y, -34, 34), 2),
        sprint=sprint,
    )


# ---------- state parsing ----------

def parse_state(payload):
    gs = payload.get("gameState", payload)
    team_id = int(payload.get("teamId", 0))
    sign = 1.0 if team_id == 0 else -1.0
    my_code = "home" if team_id == 0 else "away"
    my_idx = int((payload.get("myPlayers") or [0])[0])

    players = []
    for p in gs.get("players", []):
        try:
            idx = int(str(p.get("agentId", "")).split("_")[-1])
        except ValueError:
            continue
        pos = p.get("position", {}) or {}
        players.append({
            "idx": idx,
            "mine": str(p.get("teamCode", "")).lower() == my_code,
            "x": float(pos.get("x", 0.0)) * sign,
            "y": float(pos.get("y", 0.0)),
            "stamina": float(p.get("stamina", 100.0)),
        })

    ball = gs.get("ball", {}) or {}
    bpos = ball.get("position", {}) or {}
    bx = float(bpos.get("x", 0.0)) * sign
    by = float(bpos.get("y", 0.0))

    mates = [p for p in players if p["mine"]]
    opps = [p for p in players if not p["mine"]]
    me = next((p for p in mates if p["idx"] == my_idx), None)
    if me is None:
        me = {"idx": my_idx, "mine": True, "x": 0.0, "y": 0.0, "stamina": 100.0}
        mates.append(me)

    # possessionAgentId carries no team prefix -> pick the matching player closest to the ball
    owner = None
    owner_id = ball.get("possessionAgentId")
    if not ball.get("isFree", True) and owner_id:
        try:
            own_idx = int(str(owner_id).split("_")[-1])
            cands = [p for p in players if p["idx"] == own_idx]
            if cands:
                owner = min(cands, key=lambda p: dist(p["x"], p["y"], bx, by))
        except ValueError:
            owner = None

    score = gs.get("score", {}) or {}
    opp_code = "away" if my_code == "home" else "home"

    return {
        "sign": sign,
        "tick": gs.get("tick"),
        "time": float(gs.get("gameTime", 0.0)),
        "play_mode": gs.get("playMode", "OPEN_PLAY"),
        "me": me,
        "mates": mates,
        "opps": opps,
        "bx": bx,
        "by": by,
        "owner": owner,
        "goal_diff": int(score.get(my_code, 0)) - int(score.get(opp_code, 0)),
        "team_chat": gs.get("teamChat") or [],
    }


# ---------- tactical mode (coach + scoreline) ----------

def coach_text(team_chat):
    """Return the latest coach instruction as plain text."""
    if not team_chat:
        return ""
    last = team_chat[-1]
    if isinstance(last, dict):
        return str(last.get("message") or last.get("text") or last.get("content") or json.dumps(last))
    return str(last)


def mode_from_keywords(text):
    t = text.lower()
    for mode, words in KEYWORDS:
        if any(w in t for w in words):
            return mode
    return None


def mode_from_llm(text):
    """Map free-form coach text to one of MODES via Nova Micro. Cached per text."""
    global _bedrock
    if text in _coach_cache:
        return _coach_cache[text]
    import boto3
    from botocore.config import Config
    if _bedrock is None:
        _bedrock = boto3.client(
            "bedrock-runtime",
            config=Config(read_timeout=2, connect_timeout=1, retries={"max_attempts": 0}),
        )
    prompt = (
        "You map a football coach instruction to exactly one tactical mode.\n"
        f"Allowed modes: {', '.join(MODES)}.\n"
        "Answer with the mode word only.\n"
        f"Instruction: {text}"
    )
    resp = _bedrock.converse(
        modelId=COACH_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 5, "temperature": 0},
    )
    out = resp["output"]["message"]["content"][0]["text"].strip().lower()
    mode = out if out in MODES else None
    _coach_cache[text] = mode
    return mode


def tactical_mode(s):
    text = coach_text(s["team_chat"])
    if text:
        mode = mode_from_keywords(text)
        if mode is None and USE_LLM_COACH:
            try:
                mode = mode_from_llm(text)
            except Exception:
                log.exception("Coach LLM call failed, falling back to auto mode")
        if mode:
            return mode

    remaining = MATCH_LENGTH - s["time"]
    gd = s["goal_diff"]
    if gd < 0 and remaining < 90:
        return "all_out"
    if gd < 0:
        return "press"
    if gd >= 2 or (gd > 0 and remaining < 60):
        return "defend"
    return "balanced"


# ---------- role behaviors ----------

def best_outlet(s, min_gain=-99.0):
    """Best open outfield teammate with a clear passing lane."""
    me, opps = s["me"], s["opps"]
    best, best_score = None, -1e9
    for m in s["mates"]:
        if m["idx"] in (me["idx"], 0):
            continue
        gain = m["x"] - me["x"]
        if gain < min_gain:
            continue
        space = nearest_opp_dist(m["x"], m["y"], opps)
        if space < 4.0 or not lane_clear(me["x"], me["y"], m["x"], m["y"], opps):
            continue
        score = gain + 1.5 * min(space, 12.0)
        if score > best_score:
            best, best_score = m, score
    return best


def pick_corner(s):
    """Aim low at the side away from the opponent GK.
    Assumption: shooter faces +x (normalized), so 'left' is +y for HOME and -y for AWAY.
    TODO: verify L/R mapping from practice-match logs.
    """
    gk = next((o for o in s["opps"] if o["idx"] == 0), None)
    if gk is None:
        return "BL"
    target_negative_y = gk["y"] > 0
    aim_right = target_negative_y if s["sign"] > 0 else not target_negative_y
    return "BR" if aim_right else "BL"


def goalkeeper(s, mode):
    me, bx, by = s["me"], s["bx"], s["by"]
    if s["owner"] is me:
        target = best_outlet(s)
        if target:
            return cmd(me["idx"], "GK_DISTRIBUTE", target_player_id=target["idx"], method="KICK")
    # Rush a loose ball only when it is close to our goal and we are the nearest player
    if s["owner"] is None and dist(bx, by, OWN_GOAL_X, 0) < 14:
        if closest_to_ball(s["mates"], bx, by) is me:
            return move(s, bx, by, sprint=True)
    return move(s, OWN_GOAL_X + 2.5, clamp(by * 0.35, -GOAL_HALF_WIDTH, GOAL_HALF_WIDTH))


def with_ball(s, mode):
    me, opps = s["me"], s["opps"]
    d_goal = dist(me["x"], me["y"], GOAL_X, 0)
    shoot_range = SHOOT_RANGE + (8 if mode == "all_out" else 0)
    if d_goal < shoot_range:
        return cmd(me["idx"], "SHOOT", aim_location=pick_corner(s), power=0.95 if d_goal > 12 else 0.8)

    pressure = nearest_opp_dist(me["x"], me["y"], opps)
    forward = best_outlet(s, min_gain=6.0)
    if forward and (mode != "possession" or pressure < 6.0):
        space = nearest_opp_dist(forward["x"], forward["y"], opps)
        ptype = "THROUGH" if forward["x"] > 10 and space > 8 else "GROUND"
        return cmd(me["idx"], "PASS", target_player_id=forward["idx"], type=ptype)

    if pressure < 3.5:
        safe = best_outlet(s)
        if safe:
            return cmd(me["idx"], "PASS", target_player_id=safe["idx"], type="GROUND")

    # Carry the ball forward, drifting toward the center
    return move(s, me["x"] + 10, me["y"] * 0.7, sprint=pressure < 8)


def support(s, mode):
    me, bx = s["me"], s["bx"]
    fx, fy = FORMATION.get(me["idx"], (0.0, 0.0))
    push = {"all_out": 14, "press": 8, "balanced": 4, "possession": 2, "defend": -6}[mode]
    tx = fx + bx * 0.4 + push
    ty = fy + s["by"] * 0.3
    if me["idx"] == 4:  # striker stays on the last line, ready for a through ball
        tx = max(tx, bx + 8)
    far = dist(me["x"], me["y"], tx, ty) > 15
    return move(s, tx, ty, sprint=far)


def defend(s, mode):
    me, bx, by = s["me"], s["bx"], s["by"]
    outfield = [m for m in s["mates"] if m["idx"] != 0]
    chaser = closest_to_ball(outfield, bx, by)
    if chaser is me and me["stamina"] >= PRESS_STAMINA_MIN:
        intensity = {"press": 0.9, "all_out": 0.85, "defend": 0.45}.get(mode, 0.7)
        return cmd(me["idx"], "PRESS_BALL", intensity=intensity)

    if me["idx"] in (1, 2):
        # Stay goal-side: on the line between the ball and our goal
        side = -1 if me["idx"] == 1 else 1
        depth = 0.30 if mode == "defend" else 0.45
        tx = OWN_GOAL_X + (bx - OWN_GOAL_X) * depth
        ty = by * 0.5 + side * 6
        return move(s, max(tx, -48), ty, sprint=bx < -20)

    if me["idx"] == 3:
        return cmd(me["idx"], "INTERCEPT", aggressive=mode in ("press", "all_out"))

    # Striker: chase in press modes, otherwise wait high for the counter
    if mode in ("press", "all_out"):
        return cmd(me["idx"], "INTERCEPT", aggressive=True)
    return move(s, max(bx + 12, 0), by * 0.4)


def loose_ball(s, mode):
    me = s["me"]
    outfield = [m for m in s["mates"] if m["idx"] != 0]
    if closest_to_ball(outfield, s["bx"], s["by"]) is me:
        return move(s, s["bx"], s["by"], sprint=True)
    return support(s, mode)


def decide(s):
    mode = tactical_mode(s)
    me, owner = s["me"], s["owner"]
    if me["idx"] == 0:
        c = goalkeeper(s, mode)
    elif owner is me:
        c = with_ball(s, mode)
    elif owner is None:
        c = loose_ball(s, mode)
    elif owner["mine"]:
        c = support(s, mode)
    else:
        c = defend(s, mode)
    return [c], mode


# ---------- AgentCore entrypoint ----------

@app.entrypoint
def invoke(payload):
    started = time.perf_counter()
    if LOG_RAW_PAYLOAD:
        log.info("RAW %s", json.dumps(payload)[:8000])
    try:
        state = parse_state(payload)
        commands, mode = decide(state)
        log.info(json.dumps({
            "tick": state["tick"],
            "player": state["me"]["idx"],
            "mode": mode,
            "commands": commands,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }))
        return wrap_response(serialize(commands))
    except Exception:
        log.exception("Decision failed, returning no commands")
        return wrap_response([])


if __name__ == "__main__":
    app.run()
