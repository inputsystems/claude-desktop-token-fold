#!/usr/bin/env python3
"""
Differential QUALITY test for the fold hook.

For each case we ask a model the same question twice — once against the FULL
command output, once against the folded/transformed output — and check whether
the answer survives. This empirically tests the "no quality loss" claim instead
of just asserting it structurally.

  ANTHROPIC_API_KEY=... python3 quality-diff.py

Cases are labelled with the expected outcome:
  KEEP    -> folding must NOT change the answer (signal / summary / data fidelity)
  BOUNDARY-> folding is EXPECTED to drop this (a unique non-error line buried in
             the noisy middle) — included to honestly map what folding costs.

Uses Haiku at temperature 0 to keep cost trivial and answers deterministic.
"""

import os
import sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = "claude-haiku-4-5-20251001"


def load_hook():
    spec = importlib.util.spec_from_file_location(
        "foldhook", os.path.join(HERE, "fold-bash-output.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def build_cases():
    import json as J

    # noisy npm install with a buried, NON-signal informational line
    npm = [f"npm http fetch GET 200 https://registry.npmjs.org/pkg-{i}" for i in range(400)]
    npm.insert(120, "npm WARN deprecated har-validator@5.1.5: no longer supported")
    npm.insert(200, "note: build cache stored at /tmp/cache-abc123xyz")  # unique, non-signal, mid
    npm.append("added 400 packages, and audited 401 packages in 11s")
    npm_txt = "\n".join(npm)

    pytest_txt = "\n".join(
        [f"tests/test_{i:03d}.py::test_case PASSED" for i in range(500)] +
        ["=================== FAILURES ===================",
         "____ test_recall_roundtrip ____",
         ">   assert result.score == 0.9",
         "E   AssertionError: assert 0.71 == 0.9",
         "tests/test_new_frontier.py:88: AssertionError",
         "1 failed, 500 passed in 6.42s"])

    go_txt = "\n".join(
        [f"#  github.com/x/mod{i}" for i in range(160)][:157] +
        ["./handler.go:142:6: declared and not used: tmp"] +
        [f"#  github.com/x/mod{i}" for i in range(157, 160)])

    resources = J.dumps({"data": [{"id": i, "name": f"resource-{i}",
                          "tags": ["a", "b"]} for i in range(150)]}, indent=2)
    audit = J.dumps({"vulnerabilities": {f"pkg-{i}": {
        "severity": ["low", "moderate", "high"][i % 3]} for i in range(120)}}, indent=2)

    return [
        dict(label="KEEP", name="npm: buried WARN", command="npm install", text=npm_txt,
             q="Which package has a deprecation warning in this output? Answer just the package name.",
             key="har-validator"),
        dict(label="KEEP", name="npm: install summary", command="npm install", text=npm_txt,
             q="How many packages were added according to this output? Answer just the number.",
             key="400"),
        dict(label="KEEP", name="pytest: which test failed", command="python3 -m pytest tests/ -q",
             text=pytest_txt,
             q="Which test failed? Answer just the test name.", key="recall_roundtrip"),
        dict(label="KEEP", name="pytest: assertion detail", command="python3 -m pytest tests/ -q",
             text=pytest_txt,
             q="What were the two values in the failing assertion? Answer the numbers.", key="0.71"),
        dict(label="KEEP", name="go: compile error", command="go build ./...", text=go_txt,
             q="What variable is declared and not used? Answer just the variable name.", key="tmp"),
        dict(label="KEEP", name="json: lossless lookup", command="curl -s https://api/x",
             text=resources,
             q="What is the 'name' of the item with id 142? Answer just the name.", key="resource-142"),
        dict(label="KEEP", name="json: nested severity", command="npm audit --json", text=audit,
             q="What is the severity of pkg-3? (one word)", key="low"),
        dict(label="BOUNDARY", name="npm: buried non-signal line", command="npm install", text=npm_txt,
             q="What filesystem path was the build cache stored at? If not present, answer NOT IN OUTPUT.",
             key="/tmp/cache-abc123xyz"),
    ]


def main():
    try:
        import anthropic
    except Exception as e:
        print(f"anthropic SDK unavailable: {e}", file=sys.stderr); sys.exit(1)
    client = anthropic.Anthropic()
    hook = load_hook()

    SYS = ("Answer ONLY from the provided command OUTPUT, as briefly as possible. "
           "If the requested information is not present, answer exactly: NOT IN OUTPUT")

    def ask(question, text):
        r = client.messages.create(
            model=MODEL, max_tokens=64, temperature=0,
            system=SYS,
            messages=[{"role": "user", "content": f"{question}\n\nOUTPUT:\n{text}"}])
        return "".join(b.text for b in r.content if b.type == "text").strip()

    print("=" * 78)
    print("DIFFERENTIAL QUALITY TEST  (full output vs folded output, same question)")
    print("=" * 78)

    keep_pass = keep_total = 0
    for c in build_cases():
        folded = hook.transform(c["command"], c["text"])
        folded_txt = folded if folded is not None else c["text"]
        changed = folded is not None

        a_full = ask(c["q"], c["text"])
        a_fold = ask(c["q"], folded_txt)
        key = c["key"].lower()
        in_full = key in a_full.lower()
        in_fold = key in a_fold.lower()

        if c["label"] == "KEEP":
            keep_total += 1
            ok = in_full and in_fold        # answer must survive folding
            keep_pass += ok
            verdict = "PASS (answer survived)" if ok else "FAIL (answer LOST in fold)"
        else:  # BOUNDARY
            # honest expectation: present in full, dropped in folded
            ok = in_full and not in_fold
            verdict = ("AS EXPECTED (dropped — maps the lossy boundary)" if ok
                       else "unexpected")

        print(f"\n• [{c['label']}] {c['name']}   folded={'yes' if changed else 'no'}")
        print(f"    Q: {c['q']}")
        print(f"    full → {a_full!r}")
        print(f"    fold → {a_fold!r}")
        print(f"    {verdict}")

    print("\n" + "=" * 78)
    print(f"KEEP cases (must preserve answer): {keep_pass}/{keep_total} passed")
    print("BOUNDARY case documents the only loss: unique non-error lines in the")
    print("noisy middle of a noise-producer command. Signal/summary/data are kept.")
    print("=" * 78)


if __name__ == "__main__":
    main()
