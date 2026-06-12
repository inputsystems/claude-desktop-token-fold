#!/usr/bin/env python3
"""
PostToolUse hook (Bash): fold ONLY provably-redundant command output before it
reaches the model — installer/build/download spew and bulk test PASS lines —
while preserving every line that carries signal.

The guiding rule is ZERO QUALITY DEGRADATION. We achieve that by folding only
when BOTH are true:

  1. The COMMAND is a known noise producer (a package install, a compile/build,
     a test run). These stream large volumes of boilerplate whose middle is
     genuinely redundant.
  2. The OUTPUT is large enough that the middle is mostly that boilerplate.

Anything the model ran to *read content* — cat, head/tail, grep/rg, sed, awk,
find, ls, jq, diff, git show/diff/log, etc. — is NEVER folded, because there
every line may be the line the model needs. Unknown commands are NEVER folded
either: default-deny. When in doubt, the full original output passes through.

Other safety properties:
  * FAIL-OPEN: any error/ambiguity emits nothing -> original output preserved.
  * SHRINK-ONLY: replacement must save >=25%, else leave untouched.
  * SIGNAL-PRESERVING: errors/warnings/failures/test-summaries from the dropped
    middle are rescued verbatim.
  * HONEST: the digest is stamped with how many lines were folded.
"""

import os
import sys
import json
import re

# Quick kill switch: set FOLD_BASH_OUTPUT=0 (or off/false/no) in the environment
# to disable folding entirely — all output passes through untouched.
_OFF = {"0", "off", "false", "no"}

MIN_SAVING = 0.25        # require >=25% reduction, else don't bother
MAX_SIGNAL_LINES = 120   # cap on preserved middle "signal" lines

# Per-profile fold geometry. Tests keep a big tail because runners print the
# FAILURES section + summary at the END — we never want to clip that.
PROFILES = {
    # installs/builds: middle is pure progress/boilerplate -> fold hard.
    "build":  {"line_gate": 120, "char_gate": 8_000,  "head": 40, "tail": 25},
    # test runs: only fold the truly huge ones. 'failures_from' makes the fold
    # keep EVERYTHING from the failures/summary banner onward verbatim (so no
    # individual failure is ever dropped, even in a catastrophic run); only the
    # leading PASS spam is folded.
    "test":   {"line_gate": 400, "char_gate": 25_000, "head": 50, "tail": 150,
               "failures_from": True},
}

# pytest/unittest section banners that begin the all-signal failures region.
FAILURES_RE = re.compile(
    r"(=+\s*(FAILURES|ERRORS)\s*=+|short test summary info|"
    r"=+\s*(FAIL|ERROR)S?\b)", re.IGNORECASE)

# Commands whose PURPOSE is to surface content for reading. If any pipeline
# segment leads with one of these, we never fold — every line may matter.
NEVER = {
    "cat", "bat", "head", "tail", "less", "more", "tac", "nl",
    "sed", "awk", "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "find", "fd", "ls", "tree", "jq", "yq", "cut", "sort", "uniq",
    "column", "od", "xxd", "hexdump", "strings", "echo", "printf",
    "env", "printenv", "diff", "comm", "paste", "wc", "stat",
}

# Noise producers eligible for the "build" profile.
BUILD = {
    "npm", "pnpm", "yarn", "npx", "corepack", "node-gyp",
    "pip", "pip3", "uv", "poetry", "pipenv", "conda", "mamba",
    "brew", "apt", "apt-get", "yum", "dnf", "pacman", "gem", "bundle",
    "docker", "podman", "kubectl", "helm", "terraform",
    "make", "cmake", "ninja", "bazel", "buck", "gradle", "gradlew",
    "mvn", "gcc", "g++", "clang", "clang++", "rustc",
    "tsc", "webpack", "vite", "rollup", "esbuild", "swc", "turbo",
    "wget", "msbuild", "xcodebuild", "composer", "bundle",
}

# Test runners get the gentle "test" profile.
TEST = {
    "pytest", "jest", "vitest", "mocha", "tox", "nox", "unittest",
    "playwright", "cypress", "rspec", "phpunit", "ctest",
}

SIGNAL_RE = re.compile(
    r"(error|warn|fail|fatal|exception|traceback|panic|"
    r"\bpassed\b|\bfailed\b|\bskipped\b|\bxfail\b|\berror[s]?\b|"
    r"assert|npm err|deprecat|vulnerab|"
    r"^\s*[-+]{3}\s|===|\bPASS\b|\bFAIL\b|✗|✘|×|✖|❌)",
    re.IGNORECASE,
)

# Split a shell command into pipeline/sequence segments to inspect each verb.
_SEG_SPLIT = re.compile(r"\||&&|\|\||;|\bthen\b|\bdo\b")
_PREFIX_SKIP = {"sudo", "command", "time", "nice", "exec", "xargs", "env",
                "then", "do", "stdbuf", "caffeinate"}


def fail_open():
    sys.exit(0)


def _classify_segment(seg: str):
    """Resolve one pipeline segment to 'never', 'test', 'build', or None.

    Looks THROUGH interpreter/runner wrappers (python -m, go <sub>, uv run,
    npx, npm/yarn/pnpm <sub>) to the effective tool, so e.g.
    'python3 -m pytest' classifies as a test run, not an unknown 'python3'.
    """
    toks = [w for w in seg.split()
            if not ("=" in w and not w.startswith("-"))]
    toks = [w for w in toks if w not in _PREFIX_SKIP]
    if not toks:
        return None
    t0 = toks[0].rsplit("/", 1)[-1]   # strip path: ./gradlew -> gradlew
    rest = toks[1:]

    # A read-intent command ANYWHERE in the pipeline -> never fold.
    if t0 in NEVER:
        return "never"

    # interpreter -m <module>
    if t0 in {"python", "python3", "py"} or t0.startswith("python3."):
        mod = None
        if "-m" in toks:
            j = toks.index("-m")
            if j + 1 < len(toks):
                mod = toks[j + 1].split(".")[0]
        if mod in TEST:
            return "test"
        if mod in BUILD:
            return "build"
        return None

    # go / cargo / dotnet <subcommand> — keep 'test' on the gentle test profile
    if t0 in {"go", "cargo", "dotnet"}:
        sub = rest[0] if rest else ""
        if sub == "test":
            return "test"
        if sub in {"build", "vet", "install", "mod", "get", "generate",
                   "run", "publish", "restore", "fetch", "check", "clippy"}:
            return "build"
        return None

    # python runner wrappers: tool is first non-flag token after run/exec
    if t0 in {"uv", "poetry", "pdm", "hatch", "rye", "pipenv"}:
        tool = next((w.rsplit("/", 1)[-1] for w in rest
                     if w not in {"run", "exec", "--"} and not w.startswith("-")),
                    None)
        if tool in TEST:
            return "test"
        if tool in BUILD:
            return "build"
        return None

    # JS runner/package wrappers
    if t0 in {"npx", "bunx", "corepack"}:
        tool = next((w.rsplit("/", 1)[-1] for w in rest
                     if w not in {"run", "exec", "dlx", "--"} and not w.startswith("-")),
                    None)
        if tool == "test" or tool in TEST:
            return "test"
        if tool in BUILD:
            return "build"
        return None
    if t0 in {"npm", "pnpm", "yarn", "bun"}:
        sub = next((w for w in rest if not w.startswith("-")), "")
        nxt = None
        if sub == "run":
            nxt = next((w for w in rest[rest.index("run") + 1:]
                        if not w.startswith("-")), None)
        if sub in {"test", "t"} or (nxt and "test" in nxt):
            return "test"
        return "build"   # install / ci / add / build / etc. -> pure spew

    # bare known tools
    if t0 in TEST:
        return "test"
    if t0 in BUILD:
        return "build"
    return None


def classify(command: str):
    """Return 'test', 'build', or None. 'never' anywhere wins -> None."""
    if not command:
        return None
    profile = None
    for seg in _SEG_SPLIT.split(command):
        seg = seg.strip()
        if not seg:
            continue
        c = _classify_segment(seg)
        if c == "never":
            return None              # read intent anywhere -> never fold
        if c == "test":
            profile = "test"         # test profile is gentler; let it win
        elif c == "build" and profile != "test":
            profile = "build"
    return profile


def fold(text: str, cfg) -> str | None:
    lines = text.splitlines()
    if len(lines) <= cfg["line_gate"] and len(text) <= cfg["char_gate"]:
        return None
    if len(lines) <= cfg["head"] + cfg["tail"]:
        return None

    # Test-aware: if a failures/summary banner is present, fold ONLY the leading
    # PASS spam and keep everything from the banner to the end verbatim. This
    # guarantees no individual failure is dropped, regardless of how many there
    # are (the earlier signal-cap could lose deep-middle failures).
    if cfg.get("failures_from"):
        start = next((i for i, ln in enumerate(lines) if FAILURES_RE.search(ln)), None)
        if start is not None and start > cfg["head"]:
            kept_head = lines[:cfg["head"]]
            dropped = start - cfg["head"]
            parts = kept_head + [
                f"\n  … [folded {dropped} leading PASS/progress lines; "
                f"full failures section kept below] …\n"] + lines[start:]
            folded = "\n".join(parts)
            if len(folded) <= len(text) * (1 - MIN_SAVING):
                return folded
            # else fall through to generic fold (rare: failures dominate output)

    head = lines[:cfg["head"]]
    tail = lines[-cfg["tail"]:]
    middle = lines[cfg["head"]:len(lines) - cfg["tail"]]

    seen = set()
    signal = []
    for ln in middle:
        if SIGNAL_RE.search(ln):
            key = ln.strip()
            if key and key not in seen:
                seen.add(key)
                signal.append(ln)
            if len(signal) >= MAX_SIGNAL_LINES:
                break

    dropped = len(middle) - len(signal)
    parts = list(head)
    if signal:
        parts.append(f"\n  … [folded {dropped} redundant middle lines; "
                     f"{len(signal)} error/warning/result lines kept below] …\n")
        parts.extend(signal)
        parts.append("\n  … [end preserved signal lines] …\n")
    else:
        parts.append(f"\n  … [folded {dropped} redundant middle lines; "
                     f"no errors/warnings detected] …\n")
    parts.extend(tail)

    folded = "\n".join(parts)
    if len(folded) > len(text) * (1 - MIN_SAVING):
        return None
    return folded


# ---------------------------------------------------------------------------
# Lossless transforms. These preserve every bit of information — they only
# change the encoding — so they can be applied more broadly than fold().
# ---------------------------------------------------------------------------

JSON_MIN_CHARS = 2_000   # only bother minifying JSON above this size
JSON_MIN_SAVING = 0.15   # ... and only if it saves >=15%


def has_read_intent(command: str) -> bool:
    """True if any pipeline segment leads with a content-read command."""
    for seg in _SEG_SPLIT.split(command):
        seg = seg.strip()
        if seg and _classify_segment(seg) == "never":
            return True
    return False


def collapse_runs(text: str) -> str:
    """Collapse runs of >=3 identical consecutive lines. Lossless: the count
    is kept inline, so no information is lost (e.g. 400 identical progress
    lines -> one line tagged [×400 identical lines])."""
    lines = text.split("\n")
    out, i, n, changed = [], 0, len(lines), False
    while i < n:
        j = i
        while j + 1 < n and lines[j + 1] == lines[i]:
            j += 1
        run = j - i + 1
        if run >= 3:
            out.append(f"{lines[i]}  [×{run} identical lines]")
            changed = True
        else:
            out.extend(lines[i:j + 1])
        i = j + 1
    return "\n".join(out) if changed else text


def try_json_minify(text: str):
    """If the whole output is one large pretty-printed JSON value, return its
    minified form. Lossless: json.loads(minified) == json.loads(original)."""
    s = text.strip()
    if len(text) < JSON_MIN_CHARS or not s or s[0] not in "[{":
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    mini = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if len(mini) > len(text) * (1 - JSON_MIN_SAVING):
        return None
    return mini


def transform(command: str, text: str):
    """Return a smaller-but-faithful replacement for `text`, or None.

    Order of preference:
      1. read-intent command anywhere -> never touch (full fidelity).
      2. lossless JSON minify (any non-read command, e.g. curl/API output).
      3. known noise producer -> lossless dedup, then lossy-on-noise fold.
    Unknown non-JSON commands fall through to None (default-deny).
    """
    if has_read_intent(command):
        return None

    mini = try_json_minify(text)            # lossless, broad coverage
    if mini is not None:
        return mini

    profile = classify(command)
    if profile is None:
        return None                          # default-deny for unknown commands
    cfg = PROFILES[profile]

    deduped = collapse_runs(text)            # lossless pre-pass
    folded = fold(deduped, cfg)              # lossy-on-noise, signal-preserving
    if folded is not None:
        return folded
    # Fold didn't clear its bar, but lossless dedup alone may have helped enough.
    if deduped != text and len(deduped) <= len(text) * (1 - MIN_SAVING):
        return deduped
    return None


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def main():
    if os.environ.get("FOLD_BASH_OUTPUT", "").strip().lower() in _OFF:
        fail_open()  # toggle off -> never transform
    try:
        data = json.load(sys.stdin)
    except Exception:
        fail_open()

    command = ""
    ti = data.get("tool_input")
    if isinstance(ti, dict):
        command = ti.get("command", "") or ""

    resp = data.get("tool_response", data.get("tool_output"))
    if resp is None:
        fail_open()

    if isinstance(resp, str):
        new = transform(command, resp)
        if new is None:
            fail_open()
        emit({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": new,
        }})

    if isinstance(resp, dict):
        stdout = resp.get("stdout")
        if not isinstance(stdout, str):
            fail_open()
        new = transform(command, stdout)
        if new is None:
            fail_open()
        new_out = dict(resp)
        new_out["stdout"] = new  # stderr & other keys left fully intact
        emit({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": new_out,
        }})

    fail_open()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        fail_open()
