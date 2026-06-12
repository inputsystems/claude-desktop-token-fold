#!/usr/bin/env python3
"""
Offline benchmark for the Bash-output fold hook.

Runs representative SYNTHETIC command outputs (we don't care about the content)
through the hook's real classify()/fold() logic and reports how many tokens
folding saves — by characters/lines (free, model-invariant) and, with
--anthropic, by exact token counts across Claude model families (Opus 4.8,
Sonnet 4.6, Haiku 4.5, and Fable 5, the flagship) via the count_tokens API.

  python3 .claude/hooks/benchmark-fold.py            # char/line proxy (no key)
  python3 .claude/hooks/benchmark-fold.py --anthropic # exact across Claude families

No telemetry, no data at rest, no hook overhead. Repeatable anytime.

----------------------------------------------------------------------------
Recorded results (2026-06-12, 16 fixtures, exact count_tokens across families).
Per-fixture char saving: min 39%, median 71%, max 100%.

Three findings worth remembering:
  1. TWO Claude tokenizers — confirmed on 10/10 diverse text samples. Opus 4.8
     == Fable 5 exactly; Sonnet 4.6 == Haiku 4.5 exactly. Opus/Fable count up to
     1.65x MORE tokens for the same text. Gap is content-dependent: ~1.65x prose,
     ~1.30x URLs, ~1.19x code, 1.00x (identical) on structured pretty-JSON/CSV.
  2. LINE-REMOVAL fold (installs/builds/tests) savings ARE model-invariant —
     every Claude family (two distinct tokenizers) + chars within ~1 point
     (59-84%). The free char proxy predicts these accurately.
  3. JSON-MINIFY savings are NOT model-invariant (correction to an earlier
     claim). Whitespace removal saves Opus/Fable less (~29-38%) than chars /
     Sonnet / Haiku (~48-53%), because the families tokenize whitespace
     differently. Measure per-family for whitespace compression.
  Effort/thinking levels do NOT affect any of this — they change output
  generation, not how a fixed block of input text tokenizes.
----------------------------------------------------------------------------
"""

import sys
import os
import argparse
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))


def load_hook():
    spec = importlib.util.spec_from_file_location(
        "foldhook", os.path.join(HERE, "fold-bash-output.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --- synthetic, generic fixtures (content is throwaway) --------------------
def fx_npm_install(n=400):
    lines = [f"npm http fetch GET 200 https://registry.npmjs.org/pkg-{i} 1{i}ms (cache miss)"
             for i in range(n)]
    lines.insert(n // 2, "npm WARN deprecated har-validator@5.1.5: this library is no longer supported")
    lines.append(f"added {n} packages, and audited {n + 1} packages in 11s")
    return "npm install", "\n".join(lines)


def fx_pip_freeze(n=180):
    lines = [f"some-package-name-{i:03d}=={i % 9}.{i % 7}.{i % 5}" for i in range(n)]
    return "python3 -m pip freeze", "\n".join(lines)


def fx_go_build(n=160):
    lines = [f"#  github.com/vibe-mesh/host/orchestrator/internal/mod{i}" for i in range(n)]
    lines.insert(n - 3, "./handler.go:142:6: declared and not used: tmp")
    return "go build ./...", "\n".join(lines)


def fx_pytest(n=520):
    lines = [f"tests/test_module_{i:03d}.py::test_behaviour_{i} PASSED [{i*100//n:>3}%]"
             for i in range(n)]
    lines += [
        "",
        "=================================== FAILURES ===================================",
        "____________________________ test_recall_roundtrip _____________________________",
        "    result = recall(query)",
        ">   assert result.score == 0.9",
        "E   AssertionError: assert 0.71 == 0.9",
        "tests/test_new_frontier.py:88: AssertionError",
        "=========================== short test summary info ============================",
        "FAILED tests/test_new_frontier.py::test_recall_roundtrip - AssertionError",
        "1 failed, 519 passed in 6.42s",
    ]
    return "python3 -m pytest tests/ -q", "\n".join(lines)


def fx_curl_json(n=150):
    import json as J
    obj = {"data": [{"id": i, "name": f"resource-{i}", "active": i % 2 == 0,
                     "tags": ["alpha", "beta", "gamma"], "meta": {"rev": i, "owner": "svc"}}
                    for i in range(n)]}
    return "curl -s https://api.example.com/v1/resources", J.dumps(obj, indent=2)


def fx_yarn_install(n=350):
    lines = ["[1/4] Resolving packages...", "[2/4] Fetching packages..."]
    lines += [f"info fsevents@2.3.2: The platform \"linux\" is incompatible (dep {i})." for i in range(n)]
    lines += ["[3/4] Linking dependencies...", "[4/4] Building fresh packages...",
              "warning Workspaces can only be enabled in private projects.",
              "Done in 18.44s."]
    return "yarn install", "\n".join(lines)


def fx_docker_build(n=300):
    lines = [f"#{i} [build {i}/{n}] RUN apt-get install -y libfoo{i}" for i in range(n)]
    lines.insert(n - 5, "debconf: delaying package configuration, since apt-utils is not installed")
    lines += ["#42 exporting to image", "#42 writing image sha256:deadbeef done",
              "#42 naming to docker.io/library/app:latest done"]
    return "docker build -t app .", "\n".join(lines)


def fx_webpack_build(n=260):
    lines = [f"asset chunk-{i}.js {i*3}.{i} KiB [emitted] (name: chunk-{i})" for i in range(n)]
    lines.insert(n - 2, "WARNING in ./src/big.js 2.1 MiB - asset size limit exceeded")
    lines += ["webpack 5.89.0 compiled with 1 warning in 9214 ms"]
    return "npx webpack --mode production", "\n".join(lines)


def fx_cargo_build(n=220):
    lines = [f"   Compiling crate-{i} v0.{i}.0" for i in range(n)]
    lines.insert(n - 3, "warning: unused variable: `ctx` --> src/lib.rs:88:9")
    lines += ["    Finished release [optimized] target(s) in 1m 12s"]
    return "cargo build --release", "\n".join(lines)


def fx_gradle_build(n=240):
    lines = [f"> Task :module{i}:compileJava" for i in range(n)]
    lines.insert(n - 2, "> Task :app:lint FAILED")
    lines += ["BUILD FAILED in 31s", "42 actionable tasks: 40 executed, 2 up-to-date"]
    return "./gradlew build", "\n".join(lines)


def fx_pip_install_compile(n=200):
    lines = []
    for i in range(n):
        lines += [f"  gcc -pthread -B compile -fno-strict-overflow -c src/mod{i}.c -o build/mod{i}.o",
                  f"  In file included from src/mod{i}.c:12:"]
    lines.insert(2, "  Building wheel for numpy (pyproject.toml): started")
    lines += ["Successfully built numpy scipy", "Successfully installed numpy-2.1.0 scipy-1.14.0"]
    return "pip install numpy scipy", "\n".join(lines)


def fx_pytest_verbose(n=300):
    lines = [f"tests/suite_{i//30}/test_{i:03d}.py::test_case PASSED" for i in range(n)]
    for k in range(3):
        lines += [
            f"tests/suite_x/test_fail_{k}.py::test_thing FAILED",
            "    def test_thing():",
            f"        x = compute({k})",
            f">       assert x == {k+100}",
            f"E       assert {k} == {k+100}",
            f"tests/suite_x/test_fail_{k}.py:45: AssertionError",
        ]
    lines += ["==== 3 failed, 300 passed in 12.01s ===="]
    return "python3 -m pytest tests/ -v", "\n".join(lines)


def fx_npm_audit_json(n=120):
    import json as J
    obj = {"vulnerabilities": {f"pkg-{i}": {"severity": ["low", "moderate", "high"][i % 3],
            "via": [f"adv-{i}"], "range": f">=1.0.0 <2.{i}.0", "fixAvailable": i % 2 == 0}
            for i in range(n)}, "metadata": {"vulnerabilities": {"total": n}}}
    return "npm audit --json", J.dumps(obj, indent=2)


def fx_terraform_plan(n=180):
    lines = [f"  # module.svc[{i}].aws_instance.node will be created" for i in range(n)]
    lines += ["Plan: 180 to add, 0 to change, 0 to destroy.",
              "Warning: Argument is deprecated"]
    return "terraform plan", "\n".join(lines)


def fx_dup_progress(n=500):
    # heavy consecutive duplication -> exercises lossless run-collapse
    lines = ["Downloading [==============================>] 100%"] * n
    lines.append("Download complete: 1 file, 412 MB")
    return "wget https://example.com/big.tar.gz", "\n".join(lines)


def fx_deep_json(depth=6, breadth=4):
    import json as J
    def build(d):
        if d == 0:
            return {"leaf": "x" * 20, "n": d, "tags": list(range(breadth))}
        return {f"child_{i}": build(d - 1) for i in range(breadth)}
    return "curl -s https://api.example.com/v1/tree", J.dumps(build(depth), indent=2)


FIXTURES = [
    fx_npm_install, fx_pip_freeze, fx_go_build, fx_pytest, fx_curl_json,
    fx_yarn_install, fx_docker_build, fx_webpack_build, fx_cargo_build,
    fx_gradle_build, fx_pip_install_compile, fx_pytest_verbose,
    fx_npm_audit_json, fx_terraform_plan, fx_dup_progress, fx_deep_json,
]


# --- raw text samples for tokenizer characterization (NOT folded) ----------
# Used to test whether the two-tokenizer pairing (Opus==Fable, Sonnet==Haiku)
# holds across diverse content, and to measure the cross-family ratio.
def probe_samples():
    import json as J
    py = "\n".join(
        f"def handler_{i}(req: Request) -> Response:\n"
        f"    # process item {i}\n"
        f"    return Response(status=200, body=json.dumps({{'id': {i}}}))"
        for i in range(40))
    ts = "\n".join(
        f"export const Component{i} = ({{ id }}: Props) => <div className=\"box-{i}\">{{id}}</div>;"
        for i in range(60))
    prose = (("The quick brown fox jumps over the lazy dog. " * 3 +
              "Compression preserves meaning while reducing surface area. ") * 40)
    pretty_json = J.dumps({"users": [{"id": i, "email": f"u{i}@x.io"} for i in range(80)]}, indent=2)
    mini_json = J.dumps({"users": [{"id": i, "email": f"u{i}@x.io"} for i in range(80)]},
                        separators=(",", ":"))
    trace = "\n".join(
        f'  File "/app/services/agent/src/agent/mod_{i}.py", line {i*7}, in handler\n'
        f"    raise ValueError(f'bad input {i}')" for i in range(40))
    hexblob = "\n".join(" ".join(f"{(i*j) % 256:02x}" for j in range(24)) for i in range(60))
    urls = "\n".join(f"https://cdn.example.com/assets/v2/bundle-{i:04d}.min.js?hash=ab{i}cd{i}ef"
                     for i in range(80))
    csv = "id,name,score,active\n" + "\n".join(
        f"{i},user_{i},{i*1.5:.2f},{'true' if i%2 else 'false'}" for i in range(120))
    md = "\n".join(f"| row{i} | value {i} | {'yes' if i%2 else 'no'} | {i*100} |" for i in range(80))
    return {
        "python source": py, "typescript/jsx": ts, "english prose": prose,
        "json (pretty)": pretty_json, "json (minified)": mini_json,
        "stack trace": trace, "hex blob": hexblob, "url list": urls,
        "csv data": csv, "markdown table": md,
    }


# --- tokenizers (graceful degradation) -------------------------------------
def get_anthropic(models):
    """Return {label: count_fn} for each Claude model that count_tokens accepts."""
    if not models:
        return {}
    try:
        import anthropic
    except Exception as e:
        print(f"  (Claude exact disabled: {e})", file=sys.stderr)
        return {}
    client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY

    def make(model):
        def count(text):
            r = client.messages.count_tokens(
                model=model, messages=[{"role": "user", "content": text}])
            return r.input_tokens
        return count

    out = {}
    for model in models:
        fn = make(model)
        try:
            fn("probe")               # validate the model id once, cheaply
            out[f"Claude {model}"] = fn
        except Exception as e:
            msg = str(e).splitlines()[0][:90]
            print(f"  (skipping {model}: {msg})", file=sys.stderr)
    return out


def pct(orig, new):
    return 0.0 if orig == 0 else 100.0 * (orig - new) / orig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anthropic", action="store_true",
                    help="also measure exact Claude tokens via count_tokens API")
    ap.add_argument("--models", default="claude-opus-4-8,claude-sonnet-4-6,"
                    "claude-haiku-4-5-20251001,claude-fable-5",
                    help="comma-separated Claude model ids to count across families")
    args = ap.parse_args()

    hook = load_hook()
    models = [m.strip() for m in args.models.split(",") if m.strip()] if args.anthropic else []
    claude = get_anthropic(models)

    # accumulators
    tot = {"chars": [0, 0], "lines": [0, 0]}
    per_fixture_pct = []
    for name in claude:
        tot[name] = [0, 0]

    print("=" * 78)
    print("FOLD HOOK TOKEN BENCHMARK  (synthetic outputs; content is throwaway)")
    print("=" * 78)

    folded_any = False
    for fn in FIXTURES:
        command, text = fn()
        profile = hook.classify(command)
        new = hook.transform(command, text)      # real path: json-minify / dedup / fold
        out = new if new is not None else text
        did = new is not None
        folded_any = folded_any or did
        if did:
            per_fixture_pct.append(pct(len(text), len(out)))

        print(f"\n• {command}")
        print(f"    profile: {profile or 'none/lossless'}    changed: {'YES' if did else 'no (passthrough)'}")

        def row(label, o, n):
            tot[label][0] += o
            tot[label][1] += n
            print(f"    {label:<26} {o:>9,} -> {n:>9,}   ({pct(o,n):4.0f}% saved)")

        row("chars", len(text), len(out))
        row("lines", text.count("\n") + 1, out.count("\n") + 1)
        for label, count in claude.items():
            row(label, count(text), count(out))

    # --- totals ---
    print("\n" + "=" * 78)
    print(f"TOTAL across {len(FIXTURES)} fixtures")
    print("=" * 78)
    for label, (o, n) in tot.items():
        bar = "█" * int(pct(o, n) / 5)
        print(f"  {label:<26} {o:>9,} -> {n:>9,}   {pct(o,n):4.0f}% saved  {bar}")

    # --- per-fixture spread (robustness of the savings) ---
    savings = sorted(per_fixture_pct)
    if savings:
        mid = savings[len(savings) // 2]
        print(f"\n  per-fixture saving (chars): min {savings[0]:.0f}%  "
              f"median {mid:.0f}%  max {savings[-1]:.0f}%   (n={len(savings)})")

    # --- tokenizer characterization (only with --anthropic) ---
    if claude:
        print("\n" + "=" * 78)
        print("TOKENIZER CHARACTERIZATION  (raw varied text, NOT folded)")
        print("=" * 78)
        samples = probe_samples()
        model_labels = list(claude.keys())
        short = [m.replace("Claude claude-", "").replace("-20251001", "") for m in model_labels]
        print(f"\n  {'sample':<18}" + "".join(f"{s:>14}" for s in short))
        ratios = []
        pair_ok = True
        per_model_tot = {m: 0 for m in model_labels}
        for name, txt in samples.items():
            counts = [claude[m](txt) for m in model_labels]
            for m, c in zip(model_labels, counts):
                per_model_tot[m] += c
            print(f"  {name:<18}" + "".join(f"{c:>14,}" for c in counts))
            # verify the observed pairing holds for THIS sample
            cmap = dict(zip(model_labels, counts))
            o = cmap.get("Claude claude-opus-4-8")
            f = cmap.get("Claude claude-fable-5")
            s = cmap.get("Claude claude-sonnet-4-6")
            h = cmap.get("Claude claude-haiku-4-5-20251001")
            if o and f and o != f:
                pair_ok = False
            if s and h and s != h:
                pair_ok = False
            if o and s:
                ratios.append(o / s)
        print("  " + "-" * (18 + 14 * len(short)))
        print(f"  {'TOTAL':<18}" + "".join(f"{per_model_tot[m]:>14,}" for m in model_labels))
        if ratios:
            print(f"\n  Opus==Fable AND Sonnet==Haiku on every sample: "
                  f"{'CONFIRMED' if pair_ok else 'NOT confirmed'}")
            print(f"  Opus/Sonnet token ratio: {min(ratios):.2f}x – {max(ratios):.2f}x "
                  f"across {len(ratios)} diverse samples")

    print("\nNotes:")
    if not claude:
        print("  • Char/line proxy only. Re-run with --anthropic for exact token counts")
        print("    across Claude families incl. Fable 5 (needs ANTHROPIC_API_KEY).")
    print("  • Line-removal fold (installs/builds/tests): savings ARE ~model-invariant.")
    print("  • JSON-minify (whitespace removal): savings VARY by family — Opus/Fable realize")
    print("    less (~29-38%) than chars/Sonnet/Haiku (~48-53%); whitespace tokenizes differently.")
    print("  • Real sessions save MORE: folded output is re-sent every turn it stays in context.")
    print("  • Totals are weighted by fixture size; per-fixture median is the honest headline.")
    if not folded_any:
        print("  • Nothing folded — fixtures may be below the size gates.")


if __name__ == "__main__":
    main()
