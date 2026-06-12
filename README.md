# Native token compression for the Claude **desktop** app

A `PostToolUse` hook that folds noisy command output **automatically, in-band, inside the
Claude desktop/web app** — no CLI wrapping, no proxy, no env hacking, no account risk.
Lossless-first by design.

It cuts ~40–85% of the tokens off noise-producing command output (installs, builds, test
runs, large JSON), measured exactly across Claude and GPT tokenizers. See
[Benchmarks](#benchmarks).

---

## Why this exists (the desktop-vs-CLI gap)

Context-compression tools for coding agents are having a moment. The catch: the popular ones
are **CLI-shaped**, and their *automatic* integration paths don't work in the Claude desktop
or web app:

| Integration | Works automatically in the desktop app? |
|---|---|
| `wrap <cli>` (wraps the `claude` binary) | ❌ the app runs its own bundled engine — nothing to wrap |
| Local proxy (`ANTHROPIC_BASE_URL` redirect) | ❌ the app manages its own endpoint/auth — and proxying subscription traffic is an account-risk path |
| Compression **MCP server** | ⚠️ loads, but only when the model *explicitly calls* the tool — rarely fires |

So inside the GUI, the thing everyone's excited about mostly **can't run automatically.**

This fills that gap using the app's **native** extension model. `PostToolUse` hooks fire
automatically on every tool call — and they work identically in the desktop app, the web app,
and the CLI. If you code in the GUI and were told these tricks "need the CLI," this is for you.

> **Honest scope:** the hook works in the CLI too (same engine). The point isn't that it's
> desktop-*exclusive* — it's that it needs **none** of the CLI-only wrap/proxy machinery, which
> is exactly what makes it viable in the GUI.

---

## Design: lossless-first, default-deny

The guiding rule is **zero quality degradation**. The hook only ever shrinks output it can do
so safely, via three tiers:

1. **Lossless JSON minify** — if a command's output is one large pretty-printed JSON value,
   it's re-emitted compact. `json.loads(min) == json.loads(original)`, so the model parses the
   exact same data.
2. **Lossless run collapse** — runs of ≥3 identical consecutive lines collapse to one line
   tagged `[×N identical lines]`. The count is preserved, so nothing is lost.
3. **Lossy-on-noise fold** — only for **known noise-producer commands** (package installs,
   builds, test runs). Keeps a head + tail and *rescues* every error/warning/failure/test-summary
   line from the dropped middle. Test runs keep a large tail so the failure section survives.

Safety properties:

- **Default-deny.** Only recognized noise-producers are folded. It resolves the effective tool
  through wrappers (`python -m pytest`, `go test`, `uv run …`, `npx …`, `npm run test:*`), so it
  classifies correctly instead of by first word.
- **Reads are sacred.** Any `cat`/`grep`/`rg`/`sed`/`awk`/`find`/`jq`/`git diff`/etc. — anywhere
  in a pipeline — disables folding entirely. If the model ran a command *to read*, it sees every
  line. Unknown commands also pass through untouched (the only exception: provably-lossless JSON
  minify).
- **Fail-open.** Any parse error or ambiguity → emit nothing → the original output is preserved
  byte-for-byte. A bug in the hook can never blank or corrupt a tool result.
- **Shrink-only.** A replacement is used only if it saves ≥25% (fold) / ≥15% (JSON); otherwise
  the original passes through.

---

## Install

1. Copy `fold-bash-output.py` into your project (e.g. `.claude/hooks/`).
2. Add to `.claude/settings.json` (or `settings.local.json`):

   ```json
   {
     "env": { "FOLD_BASH_OUTPUT": "1" },
     "hooks": {
       "PostToolUse": [
         {
           "matcher": "Bash",
           "hooks": [
             {
               "type": "command",
               "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/fold-bash-output.py\""
             }
           ]
         }
       ]
     }
   }
   ```

3. **Restart the app/session** (hooks load at startup).

**Toggle:** set `FOLD_BASH_OUTPUT` to `0` / `off` / `false` / `no` to disable instantly — all
output passes through untouched. (Script edits take effect immediately; only settings/env
changes need a restart.)

---

## Benchmarks

`benchmark-fold.py` runs **synthetic** generic outputs (content is throwaway) through the real
`transform()` and counts tokens before/after — by chars/lines (free, model-invariant) and, when
available, exact tokens via `tiktoken` (GPT) and the Anthropic `count_tokens` API (Claude).

```
python3 benchmark-fold.py             # char proxy + tiktoken if installed
python3 benchmark-fold.py --anthropic # also exact Claude across model families (needs ANTHROPIC_API_KEY)
```

Measured over **16 synthetic fixtures** and verified with exact `count_tokens` across four Claude
model families plus `tiktoken` (GPT). Per-fixture char saving: **min 39%, median 71%, max 100%**.

Representative line-removal folds (savings identical across every tokenizer):

| Output | Opus 4.8 | Sonnet 4.6 | GPT-4o | chars | Saved |
|---|---|---|---|---|---|
| `npm install` | 15,954→2,621 | 11,145→1,834 | 9,533→1,560 | 31,108→5,142 | **84%** |
| `pip install` (compile) | 11,464→1,859 | 9,243→1,490 | 7,834→1,261 | — | **84%** |
| `yarn install` | 10,938→1,945 | 9,171→1,626 | 8,104→1,429 | — | **82%** |
| `docker build` | 7,873→1,687 | 6,959→1,483 | 5,743→1,220 | — | **79%** |
| `cargo build` | 3,794→1,153 | 3,565→1,074 | 2,893→870 | — | **70%** |
| `pytest` (with failures) | 16,236→9,773 | 12,616→7,613 | 10,032→6,073 | — | **40%** |

Three findings — including one we had to correct after the robust run:

1. **There are two Claude tokenizers — confirmed on 10/10 diverse samples.** Opus 4.8 == Fable 5
   exactly; Sonnet 4.6 == Haiku 4.5 exactly. Opus/Fable count **up to 65% more** tokens for the
   same text. The gap is content-dependent: ~1.65× on prose, ~1.30× on URLs, ~1.19× on code,
   but **1.00× (identical)** on structured pretty-JSON and CSV.

2. **Line-removal fold savings ARE model-invariant.** For installs/builds/test spew, every model +
   both GPT tokenizers + raw chars agree within ~1 point. The free char proxy predicts these
   accurately — no API key needed.

3. **JSON-minify savings are NOT model-invariant** *(correction to an earlier claim)*. Because
   minification removes whitespace, and the two families tokenize whitespace differently,
   Opus/Fable realize **less** saving (~29–38%) than the char count or Sonnet/Haiku suggest
   (~48–53%) on the same JSON. So: trust the char proxy for *fold*, but measure per-family for
   *whitespace* compression.

---

## Quality verification (no-loss test)

`quality-diff.py` empirically tests the "no quality loss" claim instead of just asserting it. For
each case it asks a model the same question twice — once against the **full** output, once against
the **folded** output — and checks whether the answer survives.

```
ANTHROPIC_API_KEY=... python3 quality-diff.py   # ~16 Haiku calls, temp 0
```

Result: **7/7** signal/summary/data questions returned identical answers from the folded output —
buried deprecation warnings, which test failed and its assertion values, compile errors, and
exact JSON field lookups all survived. One deliberate **boundary** case (a unique, *non-error*
line buried in the noisy middle of an install) was dropped — and that maps the exact and only
thing folding costs you.

`quality-signalcap.py` stress-tests the worst case: a catastrophic pytest run with **90 distinct
failures** (far more than any line cap). An earlier version dropped some deep-middle failures; the
test caught it, and the fold is now **test-aware** — it folds the leading PASS spam but keeps the
entire failures/summary section verbatim. Re-verified: **all 90 failures survive** while still
saving ~52%.

> **The lossy boundary, precisely.** Folding's only loss is a *unique, non-signal line in the
> dropped middle of a noise-producer command*. Errors, warnings, test failures, summaries, and all
> structured/JSON data are preserved. Read commands and unknown commands are never folded at all.

## Honest caveats

- **These percentages are for *folded commands only*, not for a whole session.** A session that's
  mostly small reads and edits will see a much smaller overall reduction. Don't read "84%" as
  "84% off your bill."
- **The savings compound per turn.** Context is re-sent every turn, so a folded output saves its
  delta on *every* subsequent turn it stays in context — the one-shot number is a floor.
- **Effort/thinking levels don't change this.** They affect output generation, not how a fixed
  block of folded input text tokenizes. The savings is identical at every effort level.
- **There's a ceiling.** The app's own large-output handling intercepts very large outputs
  (~20–30KB) and persists them to a file before the hook gets a clean shot. So this adds value in
  the *mid-size band* above the fold gates and below that ceiling — it complements the app's native
  behavior rather than replacing it.
- **This is not a novel concept.** Output truncation for agents is established prior art —
  Anthropic's own hook docs list it as a canonical `updatedToolOutput` use case, and projects like
  [Headroom](https://github.com/chopratejas/headroom) do far more (ML-based + AST compression,
  history compression via proxy). The contribution here is a *careful, lossless-first
  implementation that runs natively in the desktop app* plus a reproducible cross-model benchmark.

---

## Files

| File | Purpose |
|---|---|
| `fold-bash-output.py` | The `PostToolUse` hook |
| `settings.example.json` | Drop-in hook + toggle config |
| `benchmark-fold.py` | Token savings across tokenizers / Claude families (`--anthropic`) |
| `quality-diff.py` | Differential no-loss test: full vs folded answers (needs API key) |
| `quality-signalcap.py` | Stress test: catastrophic test run, confirms no failure dropped |
| `LICENSE` | MIT |

---

## License

MIT © [Input Systems](https://www.inputsystems.ai)
