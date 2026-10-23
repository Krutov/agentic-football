"""Schema and game-rule validation for canonical agent commands."""

import math

AIM_LOCATIONS = ("TL", "TR", "BL", "BR", "CENTER")
PASS_TYPES = ("GROUND", "AERIAL", "THROUGH")


def _num(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return f"expected number, got {v!r}"
        if not lo <= v <= hi:
            return f"{v} outside [{lo}, {hi}]"
        return None
    return check


def _int(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int):
            return f"expected int, got {v!r}"
        if not lo <= v <= hi:
            return f"{v} outside [{lo}, {hi}]"
        return None
    return check


def _enum(*values):
    def check(v):
        return None if v in values else f"{v!r} not in {values}"
    return check


def _bool(v):
    return None if isinstance(v, bool) else f"expected bool, got {v!r}"


SPEC = {
    "MOVE_TO": {"target_x": _num(-55, 55), "target_y": _num(-35, 35), "sprint": _bool},
    "FOLLOW_PLAYER": {"target_player_id": _int(0, 4), "target_team": _enum("HOME", "AWAY"),
                      "distance": _num(0, 120)},
    "SHOOT": {"aim_location": _enum(*AIM_LOCATIONS), "power": _num(0.0, 1.0)},
    "PASS": {"target_player_id": _int(0, 4), "type": _enum(*PASS_TYPES)},
    "GK_DISTRIBUTE": {"target_player_id": _int(0, 4), "method": _enum("THROW", "KICK")},
    "SLIDE_TACKLE": {"target_player_id": _int(-1, 4), "sprint": _bool, "distance": _num(0, 120)},
    "PRESS_BALL": {"intensity": _num(0.0, 1.0)},
    "MARK": {"target_player_id": _int(0, 4), "tightness": _enum("LOOSE", "TIGHT")},
    "INTERCEPT": {"aggressive": _bool},
    "SET_STANCE": {"stance": _int(0, 2)},
    "CLEAR_OVERRIDE": {},
    "RESET": {},
}

POSSESSION_REQUIRED = ("SHOOT", "PASS", "GK_DISTRIBUTE")


def validate_command(c, state):
    """Return a list of error strings for one canonical command (empty = valid)."""
    errors = []
    if not isinstance(c, dict):
        return [f"command is not an object: {c!r}"]
    for key in ("playerId", "commandType", "parameters"):
        if key not in c:
            errors.append(f"missing key '{key}'")
    if errors:
        return errors

    me = state["me"]
    ctype, params = c["commandType"], c["parameters"]

    if c["playerId"] != me["idx"]:
        errors.append(f"playerId {c['playerId']} != invoked player {me['idx']}")
    if ctype not in SPEC:
        return errors + [f"unknown commandType {ctype!r}"]
    if not isinstance(params, dict):
        return errors + [f"parameters is not an object: {params!r}"]

    spec = SPEC[ctype]
    missing = set(spec) - set(params)
    unknown = set(params) - set(spec)
    if missing:
        errors.append(f"{ctype}: missing params {sorted(missing)}")
    if unknown:
        errors.append(f"{ctype}: unknown params {sorted(unknown)}")
    for name, check in spec.items():
        if name in params:
            err = check(params[name])
            if err:
                errors.append(f"{ctype}.{name}: {err}")

    # Live pitch: MOVE_TO must stay inside the server's small pitch
    if ctype == "MOVE_TO" and state.get("kx", 1.0) > 1.0:
        import agent
        if abs(params.get("target_x", 0)) > agent.FIELD_HALF_X + 1e-6:
            errors.append(f"MOVE_TO.target_x {params['target_x']} outside live pitch +-{agent.FIELD_HALF_X}")
        if abs(params.get("target_y", 0)) > agent.FIELD_HALF_Y + 1e-6:
            errors.append(f"MOVE_TO.target_y {params['target_y']} outside live pitch +-{agent.FIELD_HALF_Y}")

    # Game rules
    has_ball = state["owner"] is me
    if ctype in POSSESSION_REQUIRED and not has_ball:
        errors.append(f"{ctype} issued without possession")
    if ctype == "GK_DISTRIBUTE" and me["idx"] != 0:
        errors.append("GK_DISTRIBUTE issued by an outfield player")
    if ctype == "SHOOT" and me["idx"] == 0:
        errors.append("goalkeeper shooting (policy violation)")
    if ctype in ("PASS", "GK_DISTRIBUTE") and params.get("target_player_id") == me["idx"]:
        errors.append(f"{ctype} to self")
    return errors
