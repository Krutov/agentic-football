"""
Agentic Football Cup - player agent for Amazon Bedrock AgentCore Runtime.

Design:
  - Deterministic rule engine runs every tick (no LLM in the hot path, a few ms).
  - Optional LLM (Amazon Nova Micro) only translates coach instructions (teamChat)
    into a tactical mode; the result is cached per instruction text.
  - The same code serves all 5 players: role comes from payload["myPlayers"][0].
  - All geometry is normalized so that "we" always attack toward +x.

Wire format (matches the official workshop sample agents, lib/agent_base.py):
  - input:  {"prompt": "<json string or object with gameState/teamId/myPlayers>"}
            (an unwrapped payload is accepted too)
  - output: streamed JSON string of
            [{"commandType", "playerId", "teamId", "parameters", "duration"}]
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

# Pitch scale. All decision logic works in "doc units" (x -55..55, y -35..35, stamina 0..100).
# The live match server uses a much smaller pitch (observed: goal lines near x = +-6.5,
# stamina 0..1). parse_state() detects the scale per payload and converts in and out.
DOC_HALF_X, DOC_HALF_Y = 55.0, 35.0
FIELD_HALF_X = float(os.getenv("FIELD_HALF_X", "6.5"))  # observed GK 6.4, ball 6.9
FIELD_HALF_Y = float(os.getenv("FIELD_HALF_Y", "3.6"))  # observed max |y| 3.5
EXTENT_LOG_EVERY = int(os.getenv("EXTENT_LOG_EVERY", "25"))

# Tunables (doc units)
GOAL_X = 55.0
OWN_GOAL_X = -55.0
GOAL_HALF_WIDTH = 3.5          # TODO: verify from logs
SHOOT_RANGE = 22.0
SPRINT_STAMINA_MIN = 40.0
PRESS_STAMINA_MIN = 25.0
MATCH_LENGTH = float(os.getenv("MATCH_LENGTH", "120"))  # seconds; draws go to Golden Goal after this
# "first" = log the first RAW_LOG_LIMIT payloads per container, "true" = all, "false" = none
LOG_RAW_PAYLOAD = os.getenv("LOG_RAW_PAYLOAD", "first").lower()
RAW_LOG_LIMIT = int(os.getenv("RAW_LOG_LIMIT", "3"))
USE_LLM_COACH = os.getenv("USE_LLM_COACH", "false").lower() == "true"
COACH_MODEL_ID = os.getenv("COACH_MODEL_ID", "us.amazon.nova-micro-v1:0")

# Output format adapter (switch via env vars without touching the decision logic)
#   CMD_TEAM_KEY     key for the team id              (default "teamId"; empty = omit)
#   CMD_PLAYER_KEY   key for the player id            (default "playerId")
#   CMD_TYPE_KEY     key for the command name         (default "commandType")
#   CMD_PARAMS_KEY   key for the parameters object    (default "parameters"; empty = flat)
#   RESPONSE_WRAPPER wrap the list as {key: [...]}    (default empty = bare list)
CMD_TEAM_KEY = os.getenv("CMD_TEAM_KEY", "teamId")
CMD_PLAYER_KEY = os.getenv("CMD_PLAYER_KEY", "playerId")
CMD_TYPE_KEY = os.getenv("CMD_TYPE_KEY", "commandType")
CMD_PARAMS_KEY = os.getenv("CMD_PARAMS_KEY", "parameters")
RESPONSE_WRAPPER = os.getenv("RESPONSE_WRAPPER", "")

# Ticks a maintained command stays active (same values as the official sample fallback)
COMMAND_DURATION = {"PRESS_BALL": 3, "MARK": 3}

MODES = ("balanced", "all_out", "press", "defend", "possession")

# Base shape in normalized coords (attacking +x). Index 0 = GK.
# Keep in sync with the formation selected in the Player Portal.
# 1-1-1-2, matches the Team Console positions: GK, DEF, MID, FWD, FWD
FORMATION = {
    1: (-30.0, 0.0),    # centre back (DEF)
    2: (-6.0, 0.0),     # central midfielder (MID)
    3: (16.0, -11.0),   # left forward (FWD)
    4: (16.0, 11.0),    # right forward (FWD)
}
DEF_IDX, MID_IDX, FWD_IDXS = 1, 2, (3, 4)
GK_RUSH_RADIUS = 9.0     # GK leaves the line only for a loose ball this close to goal
GK_LINE_X = OWN_GOAL_X + 2.0

KEYWORDS = [
    ("all_out", ["all out", "all-out", "need a goal", "push forward", "attack"]),
    ("press", ["press", "win the ball"]),
    ("defend", ["hold", "defend", "protect", "park the bus", "counter"]),
    ("possession", ["possession", "slow"]),
]

_coach_cache = {}
_bedrock = None
_raw_logged = 0
_extent = {"x": 0.0, "y": 0.0, "bx": 0.0, "by": 0.0, "n": 0}


# ---------- helpers ----------

def cmd(player_id, command_type, **params):
    """Build one command in the canonical internal shape (see serialize() for output)."""
    return {"playerId": player_id, "commandType": command_type, "parameters": params}


def serialize(commands, team_id=0, player_key=None, type_key=None, params_key=None, team_key=None):
    """Convert canonical commands into the wire format configured via env vars."""
    team_key = CMD_TEAM_KEY if team_key is None else team_key
    player_key = CMD_PLAYER_KEY if player_key is None else player_key
    type_key = CMD_TYPE_KEY if type_key is None else type_key
    params_key = CMD_PARAMS_KEY if params_key is None else params_key
    out = []
    for c in commands:
        item = {type_key: c["commandType"], player_key: c["playerId"]}
        if team_key:
            item[team_key] = team_id
        if params_key:
            item[params_key] = dict(c["parameters"])
        else:
            for k, v in c["parameters"].items():
                if k in item:
                    log.warning("Flat format: parameter '%s' collides with a reserved key, skipped", k)
                    continue
                item[k] = v
        item["duration"] = COMMAND_DURATION.get(c["commandType"], 0)
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
    """MOVE_TO in doc units; converted back to the server's pitch scale and attack direction."""
    me = s["me"]
    sprint = bool(sprint and me["stamina"] >= SPRINT_STAMINA_MIN)
    return cmd(
        me["idx"], "MOVE_TO",
        target_x=round(clamp(x, -54, 54) / s["kx"] * s["sign"], 3),
        target_y=round(clamp(y, -34, 34) / s["ky"], 3),
        sprint=sprint,
    )


def detect_scale(raw_xs, raw_ys):
    """Return (kx, ky): multipliers from server units to doc units.

    Doc-scale payloads (portal fitness checks, docs) have coordinates far beyond the live
    pitch, so anything clearly larger than FIELD_HALF_X is treated as doc scale.
    """
    max_x = max((abs(v) for v in raw_xs), default=0.0)
    max_y = max((abs(v) for v in raw_ys), default=0.0)
    if max_x > FIELD_HALF_X * 1.6 or max_y > FIELD_HALF_Y * 1.6:
        return 1.0, 1.0
    return DOC_HALF_X / FIELD_HALF_X, DOC_HALF_Y / FIELD_HALF_Y


def normalize_stamina(v):
    """Server sends 0..1; docs and fitness checks send 0..100. Return 0..100."""
    v = float(v)
    return v * 100.0 if v <= 1.0 else v


def _track_extent(raw_players, raw_ball):
    """Log the largest coordinates seen so far, to calibrate FIELD_HALF_X/Y."""
    for x, y in raw_players:
        _extent["x"] = max(_extent["x"], abs(x))
        _extent["y"] = max(_extent["y"], abs(y))
    _extent["bx"] = max(_extent["bx"], abs(raw_ball[0]))
    _extent["by"] = max(_extent["by"], abs(raw_ball[1]))
    _extent["n"] += 1
    if EXTENT_LOG_EVERY and _extent["n"] % EXTENT_LOG_EVERY == 0:
        log.info("EXTENT %s", json.dumps({k: round(v, 3) for k, v in _extent.items()}))


# ---------- state parsing ----------

def unwrap_payload(payload):
    """Return the inner request: payload["prompt"] (JSON string or dict) or the payload itself."""
    if not isinstance(payload, dict):
        return {}
    if "prompt" in payload and "gameState" not in payload:
        inner = payload["prompt"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                return {}
        return inner if isinstance(inner, dict) else {}
    return payload


def parse_state(payload):
    payload = unwrap_payload(payload)
    gs = payload.get("gameState", payload)
    team_id = int(payload.get("teamId", 0))
    sign = 1.0 if team_id == 0 else -1.0
    my_code = "home" if team_id == 0 else "away"
    my_idx = int((payload.get("myPlayers") or [0])[0])

    raw = []
    for p in gs.get("players", []):
        try:
            idx = int(str(p.get("agentId", "")).split("_")[-1])
        except ValueError:
            continue
        pos = p.get("position", {}) or {}
        raw.append((idx, p, float(pos.get("x", 0.0)), float(pos.get("y", 0.0))))

    ball = gs.get("ball", {}) or {}
    bpos = ball.get("position", {}) or {}
    raw_bx, raw_by = float(bpos.get("x", 0.0)), float(bpos.get("y", 0.0))

    kx, ky = detect_scale([r[2] for r in raw] + [raw_bx], [r[3] for r in raw] + [raw_by])
    _track_extent([(r[2], r[3]) for r in raw], (raw_bx, raw_by))

    players = [{
        "idx": idx,
        "mine": str(p.get("teamCode", "")).lower() == my_code,
        "x": x * kx * sign,
        "y": y * ky,
        "stamina": normalize_stamina(p.get("stamina", 100.0)),
    } for idx, p, x, y in raw]

    bx = raw_bx * kx * sign
    by = raw_by * ky

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
        "kx": kx,
        "ky": ky,
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
    if remaining <= 0 and gd == 0:
        return "press"  # Golden Goal: next goal wins, win the ball high but keep a back line
    if gd < 0 and remaining < MATCH_LENGTH * 0.35:
        return "all_out"
    if gd < 0:
        return "press"
    if gd >= 2 or (gd > 0 and remaining < MATCH_LENGTH * 0.25):
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
        # Always release the ball immediately; never dribble out of the goal
        target = best_outlet(s)
        if target is None:
            outfield = [m for m in s["mates"] if m["idx"] != 0]
            target = max(outfield, key=lambda m: m["x"]) if outfield else None
        if target:
            return cmd(me["idx"], "GK_DISTRIBUTE", target_player_id=target["idx"], method="KICK")
    # Leave the line only for a loose ball right in front of goal that we reach first
    if s["owner"] is None and dist(bx, by, OWN_GOAL_X, 0) < GK_RUSH_RADIUS:
        if closest_to_ball(s["mates"], bx, by) is me:
            return move(s, bx, by, sprint=True)
    # Stay on the line, shading toward the ball side; sprint back if pulled out
    ty = clamp(by * 0.35, -GOAL_HALF_WIDTH, GOAL_HALF_WIDTH)
    out_of_position = dist(me["x"], me["y"], GK_LINE_X, ty) > 4
    return move(s, GK_LINE_X, ty, sprint=out_of_position)


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
    if me["idx"] == DEF_IDX:  # centre back never joins the attack
        tx = min(tx, -12.0 if mode != "all_out" else 0.0)
    if me["idx"] in FWD_IDXS:  # forwards stay ahead of the ball, ready for a through ball
        tx = max(tx, bx + 8)
    far = dist(me["x"], me["y"], tx, ty) > 15
    return move(s, tx, ty, sprint=far)


DANGER_X = -18.0  # ball beyond this (our defensive third, doc units) triggers recovery runs


def defend(s, mode):
    me, bx, by = s["me"], s["bx"], s["by"]
    outfield = [m for m in s["mates"] if m["idx"] != 0]
    chaser = closest_to_ball(outfield, bx, by)
    if chaser is me and me["stamina"] >= PRESS_STAMINA_MIN:
        intensity = {"press": 0.9, "all_out": 0.85, "defend": 0.45}.get(mode, 0.7)
        return cmd(me["idx"], "PRESS_BALL", intensity=intensity)

    danger = bx < DANGER_X

    if me["idx"] == DEF_IDX:
        # Single centre back: goal-side of the ball, between it and the goal centre
        depth = 0.35 if (mode == "defend" or danger) else 0.5
        tx = OWN_GOAL_X + (bx - OWN_GOAL_X) * depth
        return move(s, clamp(tx, -46, -10), by * 0.6, sprint=bx < -20)

    if me["idx"] == MID_IDX:
        if danger:
            # Recovery run: drop goal-side, screening the centre in front of the centre back
            tx = OWN_GOAL_X + (bx - OWN_GOAL_X) * 0.6
            return move(s, clamp(tx, -44, -8), by * 0.4, sprint=True)
        return cmd(me["idx"], "INTERCEPT", aggressive=mode in ("press", "all_out"))

    # Forwards
    if danger:
        # The forward nearer the ball tracks back; the other stays up as the counter outlet
        fwds = [m for m in s["mates"] if m["idx"] in FWD_IDXS]
        tracker = closest_to_ball(fwds, bx, by) if fwds else None
        if tracker is me and mode != "all_out":
            return move(s, clamp(bx + 8, -38, -6), by * 0.5 + (2 if me["y"] >= 0 else -2), sprint=True)
        _, fy = FORMATION[me["idx"]]
        return move(s, -2.0, fy * 0.8)
    if mode in ("press", "all_out"):
        return cmd(me["idx"], "INTERCEPT", aggressive=True)
    _, fy = FORMATION[me["idx"]]
    return move(s, max(bx + 12, 0), fy * 0.8 + by * 0.2)


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

def _log_raw(payload):
    global _raw_logged
    if LOG_RAW_PAYLOAD == "true" or (LOG_RAW_PAYLOAD == "first" and _raw_logged < RAW_LOG_LIMIT):
        _raw_logged += 1
        try:
            log.info("RAW %s", json.dumps(payload, default=str)[:8000])
        except Exception:
            log.info("RAW (unserializable) %r", payload)


def handle(payload):
    """Pure request handler: payload -> wire-format response object. Never raises."""
    started = time.perf_counter()
    _log_raw(payload)
    team_id = 0
    try:
        inner = unwrap_payload(payload)
        team_id = int(inner.get("teamId", 0) or 0)
        state = parse_state(inner)
        commands, mode = decide(state)
        log.info(json.dumps({
            "tick": state["tick"],
            "player": state["me"]["idx"],
            "mode": mode,
            "commands": commands,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }))
        return wrap_response(serialize(commands, team_id))
    except Exception:
        log.exception("Decision failed, returning no commands")
        return wrap_response([])


@app.entrypoint
async def invoke(payload, context=None):
    """AgentCore entrypoint; streams one JSON string like the official sample agents."""
    yield json.dumps(handle(payload))


if __name__ == "__main__":
    app.run()
