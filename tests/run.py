"""Offline test runner for the football agent.

Usage (from the repo root, venv active):
    python -m tests.run                 # scenarios + fuzz + malformed + serializer
    python -m tests.run --fuzz 2000     # more random payloads
    python -m tests.run -v              # print every decision

Exit code 0 = all checks passed, 1 = at least one failure.
"""

import argparse
import asyncio
import json
import logging
import random
import statistics
import sys
import time

import agent
from tests.scenarios import MALFORMED_PAYLOADS, SCENARIOS, build_payload, random_payload
from tests.validator import validate_command

LATENCY_BUDGET_MS = 50.0


class Report:
    def __init__(self):
        self.passed = 0
        self.failures = []
        self.latencies = []

    def ok(self):
        self.passed += 1

    def fail(self, where, msg):
        self.failures.append(f"{where}: {msg}")


def decide_timed(payload):
    started = time.perf_counter()
    state = agent.parse_state(payload)
    commands, mode = agent.decide(state)
    ms = (time.perf_counter() - started) * 1000
    return state, commands, mode, ms


def check_decision(rep, where, state, commands, ms, verbose):
    rep.latencies.append(ms)
    errors = []
    if not isinstance(commands, list) or len(commands) != 1:
        errors.append(f"expected exactly one command, got {commands!r}")
    else:
        errors.extend(validate_command(commands[0], state))
    if ms > LATENCY_BUDGET_MS:
        errors.append(f"decision took {ms:.1f} ms (> {LATENCY_BUDGET_MS} ms)")
    if verbose:
        print(f"  {where}: {commands} ({ms:.2f} ms)")
    for e in errors:
        rep.fail(where, e)
    return not errors


def run_scenarios(rep, verbose):
    for sc in SCENARIOS:
        for team_id in (0, 1):
            where = f"[scenario] {sc['name']} (team {team_id})"
            payload = build_payload(team_id, sc["me"], **sc["kw"])
            state, commands, mode, ms = decide_timed(payload)
            if not check_decision(rep, where, state, commands, ms, verbose):
                continue
            if sc["expect"]:
                err = sc["expect"](commands[0], state, mode)
                if err:
                    rep.fail(where, err)
                    continue
            rep.ok()


def run_fuzz(rep, n, seed, verbose):
    rng = random.Random(seed)
    for i in range(n):
        payload = random_payload(rng)
        state, commands, mode, ms = decide_timed(payload)
        if check_decision(rep, f"[fuzz #{i} seed={seed}]", state, commands, ms, verbose):
            rep.ok()


def run_malformed(rep):
    for i, payload in enumerate(MALFORMED_PAYLOADS):
        where = f"[malformed #{i}]"
        try:
            out = agent.handle(payload)
        except Exception as e:  # handle must never raise
            rep.fail(where, f"handle raised {type(e).__name__}: {e}")
            continue
        if not isinstance(out, (list, dict)):
            rep.fail(where, f"unexpected response type {type(out).__name__}")
            continue
        rep.ok()


def _check_wire(rep, where, out, team_id, player_id, ctype):
    ok = (isinstance(out, list) and len(out) == 1 and isinstance(out[0], dict)
          and out[0].get("commandType") == ctype
          and out[0].get("playerId") == player_id
          and out[0].get("teamId") == team_id
          and isinstance(out[0].get("parameters"), dict)
          and isinstance(out[0].get("duration"), int))
    if ok:
        rep.ok()
    else:
        rep.fail(where, f"unexpected response {out!r}")


def run_invoke_roundtrip(rep):
    """Wire contract of the official sample agents: prompt-wrapped input, streamed JSON string."""
    for team_id in (0, 1):
        inner = build_payload(team_id, 4, mine={4: (40.0, 0.0)}, owner=("mine", 4))
        variants = {
            "bare payload": inner,
            "prompt as JSON string": {"prompt": json.dumps(inner)},
            "prompt as object": {"prompt": inner},
        }
        for name, payload in variants.items():
            _check_wire(rep, f"[handle] {name} (team {team_id})", agent.handle(payload),
                        team_id, 4, "SHOOT")

        async def collect():
            return [chunk async for chunk in agent.invoke({"prompt": json.dumps(inner)}, None)]

        where = f"[invoke] streamed response (team {team_id})"
        chunks = asyncio.run(collect())
        if len(chunks) != 1 or not isinstance(chunks[0], str):
            rep.fail(where, f"expected one JSON string chunk, got {chunks!r}")
            continue
        try:
            decoded = json.loads(chunks[0])
        except ValueError as e:
            rep.fail(where, f"chunk is not valid JSON: {e}")
            continue
        _check_wire(rep, where, decoded, team_id, 4, "SHOOT")


def run_serializer(rep):
    canonical = [agent.cmd(2, "PASS", target_player_id=4, type="THROUGH"),
                 agent.cmd(2, "PRESS_BALL", intensity=0.7)]
    params_pass = {"target_player_id": 4, "type": "THROUGH"}
    cases = [
        ("default (sample contract)", dict(team_id=1), None,
         [{"commandType": "PASS", "playerId": 2, "teamId": 1, "parameters": params_pass, "duration": 0},
          {"commandType": "PRESS_BALL", "playerId": 2, "teamId": 1, "parameters": {"intensity": 0.7},
           "duration": 3}]),
        ("flat, no team", dict(params_key="", team_key=""), None,
         [{"commandType": "PASS", "playerId": 2, "target_player_id": 4, "type": "THROUGH", "duration": 0},
          {"commandType": "PRESS_BALL", "playerId": 2, "intensity": 0.7, "duration": 3}]),
        ("custom keys", dict(team_id=0, player_key="player_id", type_key="action", params_key="args",
                             team_key="team"), None,
         [{"action": "PASS", "player_id": 2, "team": 0, "args": params_pass, "duration": 0},
          {"action": "PRESS_BALL", "player_id": 2, "team": 0, "args": {"intensity": 0.7}, "duration": 3}]),
        ("wrapped", dict(team_id=0), "commands",
         {"commands": [
             {"commandType": "PASS", "playerId": 2, "teamId": 0, "parameters": params_pass, "duration": 0},
             {"commandType": "PRESS_BALL", "playerId": 2, "teamId": 0, "parameters": {"intensity": 0.7},
              "duration": 3}]}),
    ]
    for name, kwargs, wrapper, expected in cases:
        got = agent.wrap_response(agent.serialize(canonical, **kwargs), wrapper=wrapper)
        if got != expected:
            rep.fail(f"[serializer] {name}", f"got {got!r}, expected {expected!r}")
        else:
            rep.ok()


def main():
    parser = argparse.ArgumentParser(description="Offline checks for the football agent")
    parser.add_argument("--fuzz", type=int, default=500, help="number of random payloads")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Silence per-decision and expected exception logs from the agent
    logging.getLogger("football").setLevel(logging.CRITICAL)

    rep = Report()
    run_scenarios(rep, args.verbose)
    run_fuzz(rep, args.fuzz, args.seed, args.verbose)
    run_malformed(rep)
    run_invoke_roundtrip(rep)
    run_serializer(rep)

    lat = rep.latencies
    print(f"Scenarios: {len(SCENARIOS)} x 2 teams | fuzz: {args.fuzz} | "
          f"malformed: {len(MALFORMED_PAYLOADS)} | serializer: 4 | wire contract: 8")
    if lat:
        p95 = sorted(lat)[int(len(lat) * 0.95) - 1]
        print(f"Decision latency: median {statistics.median(lat):.3f} ms, "
              f"p95 {p95:.3f} ms, max {max(lat):.3f} ms (budget {LATENCY_BUDGET_MS:.0f} ms)")
    print(f"Passed: {rep.passed} | Failed: {len(rep.failures)}")

    if rep.failures:
        print("\nFAILURES:")
        for f in rep.failures[:50]:
            print(f"  - {f}")
        if len(rep.failures) > 50:
            print(f"  ... and {len(rep.failures) - 50} more")
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
