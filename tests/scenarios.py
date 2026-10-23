"""Synthetic game situations.

Every scenario is described from OUR perspective (we attack +x) and is run
for both teamId 0 (HOME) and teamId 1 (AWAY). The payload builder mirrors the
x axis for AWAY, so one scenario checks direction handling on both sides.
"""

import random

BASE_MINE = {0: (-52.0, 0.0), 1: (-30.0, 0.0), 2: (-6.0, 0.0), 3: (16.0, -11.0), 4: (16.0, 11.0)}
BASE_OPP = {0: (52.0, 0.0), 1: (30.0, 0.0), 2: (6.0, 0.0), 3: (-16.0, -11.0), 4: (-16.0, 11.0)}


# Live server pitch (see agent.FIELD_HALF_X/Y): positions are divided by these factors
LIVE_KX = 55.0 / 6.5
LIVE_KY = 35.0 / 3.6


def build_payload(team_id, me_idx, mine=None, opp=None, ball=None, owner=None,
                  chat=None, score=(0, 0), game_time=60.0, stamina=None,
                  play_mode="OPEN_PLAY", live=False):
    """Build a platform-shaped payload.

    mine/opp: {idx: (x, y)} overrides in normalized coords.
    owner:    None (free ball) or ("mine" | "opp", idx).
    score:    (ours, theirs).
    stamina:  {("mine" | "opp", idx): value} overrides, default 100.
    """
    sign = 1.0 if team_id == 0 else -1.0
    kx, ky, ks = (LIVE_KX, LIVE_KY, 100.0) if live else (1.0, 1.0, 1.0)
    my_code, opp_code = ("home", "away") if team_id == 0 else ("away", "home")
    mine_pos = {**BASE_MINE, **(mine or {})}
    opp_pos = {**BASE_OPP, **(opp or {})}
    stamina = stamina or {}

    players = []
    for side, code, positions in (("mine", my_code, mine_pos), ("opp", opp_code, opp_pos)):
        for idx, (x, y) in sorted(positions.items()):
            players.append({
                "agentId": f"agentId_{idx}",
                "teamCode": code,
                "position": {"x": x * sign / kx, "y": y / ky},
                "velocity": {"x": 0.0, "y": 0.0},
                "stamina": stamina.get((side, idx), 100.0) / ks,
                "currentAction": "IDLE",
            })

    if ball is None:
        if owner:
            ball = (mine_pos if owner[0] == "mine" else opp_pos)[owner[1]]
        else:
            ball = (0.0, 0.0)

    return {
        "gameState": {
            "tick": 1,
            "gameTime": game_time,
            "playMode": play_mode,
            "score": {my_code: score[0], opp_code: score[1]},
            "ball": {
                "position": {"x": ball[0] * sign / kx, "y": ball[1] / ky, "z": 0.0},
                "velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
                "isFree": owner is None,
                "possessionAgentId": f"agentId_{owner[1]}" if owner else None,
            },
            "players": players,
            "teamChat": chat or [],
        },
        "teamId": team_id,
        "myPlayers": [me_idx],
    }


# ---------- expectation helpers (all in normalized coords) ----------

def nx(c, s):
    """Normalized target_x of a MOVE_TO command, in doc units."""
    return c["parameters"]["target_x"] * s["sign"] * s["kx"]


def ny(c, s):
    return c["parameters"]["target_y"] * s["ky"]


def expect_types(*types):
    def check(c, s, mode):
        return None if c["commandType"] in types else f"expected {types}, got {c['commandType']}"
    return check


def expect_move(pred, text):
    def check(c, s, mode):
        if c["commandType"] != "MOVE_TO":
            return f"expected MOVE_TO, got {c['commandType']}"
        return None if pred(c, s) else f"MOVE_TO target ({nx(c, s):.1f}, {ny(c, s):.1f}) fails: {text}"
    return check


def expect_mode(expected, inner=None):
    def check(c, s, mode):
        if mode != expected:
            return f"expected mode {expected}, got {mode}"
        return inner(c, s, mode) if inner else None
    return check


def expect_not(*types):
    def check(c, s, mode):
        return f"{c['commandType']} is not allowed here" if c["commandType"] in types else None
    return check


def expect_forward_progress(c, s, mode):
    """With the ball outside shooting range: pass or carry toward the opponent goal."""
    if c["commandType"] == "PASS":
        return None
    if c["commandType"] == "MOVE_TO":
        return None if nx(c, s) > s["me"]["x"] else "carried the ball backwards"
    return f"expected PASS or forward MOVE_TO, got {c['commandType']}"


def expect_pass_to(target):
    def check(c, s, mode):
        if c["commandType"] != "PASS":
            return f"expected PASS, got {c['commandType']}"
        got = c["parameters"]["target_player_id"]
        return None if got == target else f"passed to {got}, expected {target}"
    return check


# ---------- scenarios ----------

SCENARIOS = [
    dict(name="striker in box shoots", me=4,
         kw=dict(mine={4: (40.0, -2.0)}, owner=("mine", 4)),
         expect=expect_types("SHOOT")),
    dict(name="striker in midfield moves ball forward", me=4,
         kw=dict(mine={4: (0.0, 0.0)}, opp={3: (-20.0, 20.0)}, owner=("mine", 4)),
         expect=expect_forward_progress),
    dict(name="midfielder finds open striker", me=3,
         kw=dict(mine={3: (-5.0, 0.0), 4: (22.0, 14.0)},
                 opp={1: (30.0, -14.0), 2: (35.0, 25.0), 3: (-10.0, -20.0), 4: (-25.0, 0.0)},
                 owner=("mine", 3)),
         expect=expect_pass_to(4)),
    dict(name="defender under pressure releases ball", me=1,
         kw=dict(mine={1: (-30.0, -12.0), 3: (-15.0, 0.0)},
                 opp={4: (-28.5, -12.0), 3: (10.0, 20.0)}, owner=("mine", 1)),
         expect=expect_types("PASS", "MOVE_TO")),
    dict(name="goalkeeper with ball distributes", me=0,
         kw=dict(owner=("mine", 0)),
         expect=expect_types("GK_DISTRIBUTE", "MOVE_TO")),
    dict(name="goalkeeper holds goal line", me=0,
         kw=dict(owner=("opp", 3), opp={3: (10.0, 5.0)}),
         expect=expect_move(lambda c, s: nx(c, s) <= -50 and abs(ny(c, s)) <= 4, "stay on own goal line")),
    dict(name="goalkeeper rushes loose ball near goal", me=0,
         kw=dict(ball=(-47.0, 2.0), mine={1: (-30.0, -12.0), 2: (-30.0, 12.0)}),
         expect=expect_move(lambda c, s: abs(nx(c, s) - s["bx"]) < 0.5 and c["parameters"]["sprint"],
                            "sprint to the ball")),
    dict(name="nearest player presses ball carrier", me=2,
         kw=dict(owner=("opp", 3), opp={3: (0.0, 0.0)}),
         expect=expect_types("PRESS_BALL")),
    dict(name="exhausted nearest player does not press", me=2,
         kw=dict(owner=("opp", 3), opp={3: (0.0, 0.0)}, stamina={("mine", 2): 10.0}),
         expect=expect_not("PRESS_BALL")),
    dict(name="defender stays goal-side", me=1,
         kw=dict(owner=("opp", 4), opp={4: (-20.0, 5.0)}, mine={3: (-21.0, 4.0)}),
         expect=expect_move(lambda c, s: nx(c, s) < s["bx"], "between ball and own goal")),
    dict(name="nearest player chases loose ball", me=2,
         kw=dict(ball=(0.0, 5.0)),
         expect=expect_move(lambda c, s: abs(nx(c, s) - s["bx"]) < 0.5, "run to the ball")),
    dict(name="far player keeps shape on loose ball", me=1,
         kw=dict(ball=(20.0, 0.0)),
         expect=expect_move(lambda c, s: abs(nx(c, s) - s["bx"]) > 5, "do not chase")),
    dict(name="striker stays high when defending", me=4,
         kw=dict(owner=("opp", 3), opp={3: (-30.0, 0.0)}, mine={1: (-33.0, 0.0)}),
         expect=expect_move(lambda c, s: nx(c, s) >= s["bx"], "stay ahead of the ball")),
    dict(name="out of range without coach: no shot", me=4,
         kw=dict(mine={4: (27.0, 0.0)}, owner=("mine", 4)),
         expect=expect_not("SHOOT")),
    dict(name="coach 'need a goal' extends shooting range", me=4,
         kw=dict(mine={4: (27.0, 0.0)}, owner=("mine", 4), chat=["Push forward, we need a goal"]),
         expect=expect_mode("all_out", expect_types("SHOOT"))),
    dict(name="coach dict message parsed", me=3,
         kw=dict(chat=[{"message": "Hold position, defend the lead"}]),
         expect=expect_mode("defend")),
    dict(name="coach press instruction", me=3,
         kw=dict(chat=["Press higher, win the ball back quickly"]),
         expect=expect_mode("press")),
    dict(name="losing late switches to all_out", me=3,
         kw=dict(score=(0, 1), game_time=100.0),
         expect=expect_mode("all_out")),
    dict(name="losing early switches to press", me=3,
         kw=dict(score=(1, 2), game_time=30.0),
         expect=expect_mode("press")),
    dict(name="two-goal lead switches to defend", me=3,
         kw=dict(score=(2, 0), game_time=40.0),
         expect=expect_mode("defend")),
    dict(name="narrow lead late switches to defend", me=3,
         kw=dict(score=(1, 0), game_time=100.0),
         expect=expect_mode("defend")),
    dict(name="narrow lead early stays balanced", me=3,
         kw=dict(score=(1, 0), game_time=40.0),
         expect=expect_mode("balanced")),
    dict(name="draw stays balanced", me=3,
         kw=dict(score=(1, 1), game_time=60.0),
         expect=expect_mode("balanced")),
    dict(name="golden goal presses", me=3,
         kw=dict(score=(1, 1), game_time=135.0),
         expect=expect_mode("press")),
    dict(name="goalkeeper ignores loose ball 12 units out", me=0,
         kw=dict(ball=(-43.0, 0.0), mine={1: (-20.0, 0.0), 2: (-5.0, 0.0)}),
         expect=expect_move(lambda c, s: nx(c, s) <= -50, "stay on the line")),
    dict(name="goalkeeper pulled out sprints back", me=0,
         kw=dict(mine={0: (-40.0, 10.0)}, owner=("opp", 2), opp={2: (5.0, 0.0)}),
         expect=expect_move(lambda c, s: nx(c, s) <= -50 and c["parameters"]["sprint"], "sprint back to line")),
    dict(name="goalkeeper without outlet still distributes", me=0,
         kw=dict(owner=("mine", 0), opp={1: (-45.0, -3.0), 2: (-45.0, 3.0), 3: (-30.0, 0.0), 4: (-20.0, 0.0)}),
         expect=expect_types("GK_DISTRIBUTE")),
    dict(name="centre back does not join attack", me=1,
         kw=dict(owner=("mine", 4), mine={4: (40.0, 5.0)}),
         expect=expect_move(lambda c, s: nx(c, s) <= -11.9, "stay in own half")),
    dict(name="only one outfield player defends deep", me=3,
         kw=dict(owner=("opp", 3), opp={3: (-35.0, -5.0)}, mine={1: (-38.0, -4.0), 2: (-30.0, 0.0)}),
         expect=expect_move(lambda c, s: nx(c, s) >= -25, "forward stays high")),
    dict(name="kickoff does not break decisions", me=4,
         kw=dict(play_mode="KICK_OFF", owner=("mine", 4), mine={4: (0.0, 0.0)}),
         expect=None),
]


# ---------- random payloads ----------

CHAT_POOL = [
    [], [], ["Push forward, we need a goal"], ["Hold position, wait for counter-attack"],
    ["Press higher"], ["Focus on possession, slow the game down"], ["play better"],
    [{"message": "attack now"}], [{"text": "defend"}], [{"weird": 1}],
]


def random_payload(rng: random.Random, live=False):
    """Random but structurally valid payload, for fuzzing."""
    def pos():
        return (rng.uniform(-55, 55), rng.uniform(-35, 35))

    mine = {i: pos() for i in range(5)}
    opp = {i: pos() for i in range(5)}
    roll = rng.random()
    if roll < 0.3:
        owner = None
    elif roll < 0.65:
        owner = ("mine", rng.randint(0, 4))
    else:
        owner = ("opp", rng.randint(0, 4))
    stamina = {(side, i): rng.uniform(0, 100) for side in ("mine", "opp") for i in range(5)}
    return build_payload(
        team_id=rng.randint(0, 1),
        me_idx=rng.randint(0, 4),
        mine=mine,
        opp=opp,
        ball=pos() if owner is None else None,
        owner=owner,
        chat=rng.choice(CHAT_POOL),
        score=(rng.randint(0, 4), rng.randint(0, 4)),
        game_time=rng.uniform(0, 180),
        stamina=stamina,
        play_mode=rng.choice(["OPEN_PLAY", "OPEN_PLAY", "KICK_OFF", "FREE_KICK"]),
        live=live,
    )


MALFORMED_PAYLOADS = [
    {},
    {"prompt": "not json"},
    {"prompt": "[1, 2, 3]"},
    {"prompt": None},
    "not a dict",
    {"gameState": {}},
    {"gameState": {"players": []}, "teamId": 1, "myPlayers": [2]},
    {"gameState": {"players": "broken"}, "teamId": 0, "myPlayers": [1]},
    {"gameState": {"ball": None, "players": None, "score": None}, "myPlayers": []},
    {"gameState": {"players": [{"agentId": "bad"}, {"agentId": "agentId_x"}]}, "myPlayers": [0]},
]
