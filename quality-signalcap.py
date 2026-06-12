#!/usr/bin/env python3
"""
Signal-cap stress test for the fold hook.

Builds a CATASTROPHIC pytest run with far more distinct failures than the
fold's MAX_SIGNAL_LINES rescue cap (120) and the test-profile tail (150), then
asks a model — against the folded output — whether specific failures survived.

The honest question: when a test run has more failures than the cap, which
ones get lost? We probe an EARLY failure (most at risk — deep in the dropped
middle), a MIDDLE one, a LATE one (should be in the tail), and the final
summary count.

  ANTHROPIC_API_KEY=... python3 quality-signalcap.py
"""

import os
import sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = "claude-haiku-4-5-20251001"
N_FAIL = 90          # 90 distinct failures, each 5 lines -> 450 failure lines
N_PASS = 600         # plus lots of PASS noise in front


def load_hook():
    spec = importlib.util.spec_from_file_location(
        "foldhook", os.path.join(HERE, "fold-bash-output.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def build_run():
    lines = [f"tests/pass_{i:03d}.py::test_ok PASSED" for i in range(N_PASS)]
    lines.append("=================== FAILURES ===================")
    for k in range(N_FAIL):
        lines += [
            f"____ test_feature_{k:03d} ____",
            f"    def test_feature_{k:03d}():",
            f">       assert compute({k}) == {k + 1000}",
            f"E       AssertionError: assert {k} == {k + 1000}",
            f"tests/fail_{k:03d}.py:{k + 10}: AssertionError",
        ]
    lines.append(f"=========== {N_FAIL} failed, {N_PASS} passed in 42.10s ===========")
    return "\n".join(lines)


def main():
    try:
        import anthropic
    except Exception as e:
        print(f"anthropic SDK unavailable: {e}", file=sys.stderr); sys.exit(1)
    client = anthropic.Anthropic()
    hook = load_hook()

    full = build_run()
    folded = hook.transform("python3 -m pytest tests/ -q", full)
    folded_txt = folded if folded is not None else full

    print("=" * 78)
    print(f"SIGNAL-CAP STRESS: {N_FAIL} distinct failures (cap=120 signal, tail=150)")
    print("=" * 78)
    print(f"full: {len(full):,} chars / {full.count(chr(10))+1} lines  ->  "
          f"folded: {len(folded_txt):,} chars / {folded_txt.count(chr(10))+1} lines")

    SYS = ("Answer ONLY from the provided pytest OUTPUT, as briefly as possible. "
           "If the requested information is not present, answer exactly: NOT IN OUTPUT")

    def ask(q, text):
        r = client.messages.create(
            model=MODEL, max_tokens=48, temperature=0, system=SYS,
            messages=[{"role": "user", "content": f"{q}\n\nOUTPUT:\n{text}"}])
        return "".join(b.text for b in r.content if b.type == "text").strip()

    probes = [
        ("EARLY  failure 002", "Did test_feature_002 fail? Answer yes or no.", "yes"),
        ("MIDDLE failure 045", "Did test_feature_045 fail? Answer yes or no.", "yes"),
        ("LATE   failure 088", "Did test_feature_088 fail? Answer yes or no.", "yes"),
        ("SUMMARY count", "How many tests failed in total? Answer just the number.", str(N_FAIL)),
    ]

    print()
    surfaced_in_full = []
    for name, q, key in probes:
        a_full = ask(q, full)
        a_fold = ask(q, folded_txt)
        f_ok = key.lower() in a_full.lower()
        d_ok = key.lower() in a_fold.lower()
        surfaced_in_full.append((name, f_ok, d_ok))
        status = "kept" if d_ok else ("DROPPED" if f_ok else "n/a")
        print(f"  {name:<20} full={a_full!r:<16} fold={a_fold!r:<16} -> {status}")

    print("\n" + "=" * 78)
    kept = [n for n, f, d in surfaced_in_full if d]
    dropped = [n for n, f, d in surfaced_in_full if f and not d]
    print(f"kept in folded: {kept}")
    print(f"dropped in folded: {dropped}")
    print("Expectation: SUMMARY + late failures are always kept (tail); the cap may")
    print("drop some MIDDLE/EARLY individual failures when failures exceed ~120+tail.")
    print("=" * 78)


if __name__ == "__main__":
    main()
