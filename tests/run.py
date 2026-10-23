"""Offline test runner for the football agent.

Usage (from the repo root, venv active):
    python -m tests.run                 # scenarios + fuzz + malformed + serializer
    python -m tests.run --fuzz 2000     # more random payloads
    python -m tests.run -v              # print every decision

Exit code 0 = all checks passed, 1 = at least one failure.
"""

import argparse
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
            out = agent.invoke(payload)
        except Exception as e:  # invoke must never raise
            rep.fail(where, f"invoke raised {type(e).__name__}: {e}")
            continue
        if not isinstance(out, (list, dict)):
            rep.fail(where, f"unexpected response type {type(out).__name__}")
            continue
        rep.ok()


def run_invoke_roundtrip(rep):
    payload = build_payload(0, 4, mine={4: (40.0, 0.0)}, owner=("mine", 4))
    out = agent.invoke(payload)
    expected_default = [{"playerId": 4, "commandType": "SHOOT"}]
    if not (isinstance(out, list) and len(out) == 1
            and all(out[0].get(k) == v for k, v in expected_default[0].items())
            and "parameters" in out[0]):
        rep.fail("[invoke] default wire format", f"unexpected response {out!r}")
    else:
        rep.ok()


def run_serializer(rep):
    canonical = [agent.cmd(2, "PASS", target_player_id=4, type="THROUGH")]
    cases = [
        ("nested default", dict(), None,
         [{"playerId": 2, "commandType": "PASS", "parameters": {"target_player_id": 4, "type": "THROUGH"}}]),
        ("flat", dict(params_key=""), None,
         [{"playerId": 2, "commandType": "PASS", "target_player_id": 4, "type": "THROUGH"}]),
        ("custom keys", dict(player_key="player_id", type_key="action", params_key="args"), None,
         [{"player_id": 2, "action": "PASS", "args": {"target_player_id": 4, "type": "THROUGH"}}]),
        ("wrapped", dict(), "commands",
         {"commands": [{"playerId": 2, "commandType": "PASS",
                        "parameters": {"target_player_id": 4, "type": "THROUGH"}}]}),
        ("flat collision keeps command name", dict(type_key="type", params_key=""), None,
         [{"playerId": 2, "type": "PASS", "target_player_id": 4}]),
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
          f"malformed: {len(MALFORMED_PAYLOADS)} | serializer: 5 | invoke: 1")
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
