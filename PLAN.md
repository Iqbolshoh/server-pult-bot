# PLAN.md — Server Pult

> Measured on **2026-08-28** from the source tree, `state.db`, the supervisor log
> and the two engine CLIs themselves. Every "today" number below was read off
> this machine, not remembered. Where a claim came from running a command, the
> command is named so the claim can be re-checked.
>
> **Update, 2026-08-28 (evening): Phases 0, 1 and 2 are built, tested and live.**
> See the progress log in §5 — including three defects that only a live run could
> have found, one of which meant the Antigravity engine had never actually worked
> on the project you pointed it at.

Server Pult is one Telegram bot (`@server_pult_bot`) that drives two AI coding
engines on a server. It works, it is used daily, and it is written for exactly
one operator: its author.

**The goal of this plan is to turn it into a product other developers install on
their own servers and pay a monthly fee for — without losing the thing that
makes it good, which is that it is small, dependency-free and legible.**

The feature it gets sold on is in §4: **when one engine's limit runs out, the job
carries on with the next one.** Two engines already share one queue here, which
is what makes that possible and what makes it hard to copy.

---

## 1. Where it stands today

| | | today (evening) |
|---|---|---|
| Code | 2,483 lines, 14 files, Python 3.14, **stdlib only — no pip dependencies** | 3,511 lines, 16 modules + 320×3 locale strings |
| Tests | none | **122**, plain `unittest`, fixtures recorded from both CLIs, CI on push |
| Engines | `claude` 2.1.250 · `agy` 1.1.19 | `claude` 2.1.251 · `agy` 1.1.22 |
| Process | supervisor `server-pult-bot`, `autorestart=true` | unchanged, plus a systemd path in `install.sh` |
| Repo | 5 commits, latest `0d9edcb` | + this work |
| Jobs run | **90** (2026-08-10 → 08-20) — 84 done, 5 cancelled, 1 failed | **98** — 92 done, 5 cancelled, 1 failed |
| Jobs per engine | claude **90** · agy **0** | claude 93 · **agy 5, all after the fixes below** |
| Token rows | 1 of 90 jobs | every job since the restart |
| Limit gauges | discarded unread | read, stored, and drawn in `/limit` |

The architecture is genuinely good and this plan does not change it: strictly
one-way imports (`core → config → db → telegram → engines → projects →
keyboards → screens → jobs → handlers → localapi → maintenance`), a durable
outbox so a lost signal never loses a result, one worker thread per engine so
the two run side by side, and a per-boot key on the loopback API.

### The one fact that mattered most — now closed

*(2026-08-28: fixed and proven. Five agy jobs have now run through the bot: a
file-writing job, a resume that remembered the earlier conversation, and a
plan-mode job that correctly changed nothing. Two blocking defects were found in
the process; both are in the progress log.)*

**The Antigravity engine has never run a single job through the bot.** All 90
rows in `jobs` are `claude`. Everything on the agy side — the flag builder, the
event reader, the session store, the model catalogue — has been written but
never exercised by a real user.

I ran it by hand today to check. The good news: the event shape in
`_agy_events` is correct. A live run returns

```json
{"event":"result","result":{"conversation_id":"…","status":"SUCCESS",
 "response":"OK\n","num_turns":1,
 "usage":{"input_tokens":13848,"output_tokens":32,"total_tokens":13880}}}
```

which is exactly what the parser expects. The engine is not broken. It is
**unproven**, and "chotkiy ishlasin" on both engines starts with proving it.

---

## 2. What "a product" means here

Self-hosted, license-key, monthly. Not SaaS. This is not a preference — it is
forced by the design: the bot shells out to `claude` and `agy`, which are logged
in as *the buyer's own account* under their `$HOME`. Their subscription, their
server, their code. Nothing can be centrally hosted without asking every buyer
to hand over their Claude login, which is a non-starter.

So the product is:

- a repo the buyer clones,
- one command that installs it,
- a license key that unlocks it and expires monthly,
- and a bot that speaks their language.

Everything in this plan serves one of those four.

---

## 3. Gap audit

Grouped by what blocks what. Each item was verified today.

### 3.1 Blocks selling to anyone (product shell)

> Every row in this section is now ✅ **done** (2026-08-28) except P3, which waits
> on the owner's decisions in §8.

| # | Gap | Evidence |
|---|---|---|
| P1 ✅ | **No installer.** Install is: copy files to `/opt`, hand-write `config.json` and `.env`, hand-write a supervisor conf that lives *outside* the repo | `/etc/supervisor/conf.d/server-pult-bot.conf` is not in the tree |
| P2 ✅ | **Uzbek is hardcoded.** ~122 user-facing Uzbek string literals live in the Python source | `grep` over `pult/*.py` |
| P3 ⏳ | **No license check.** Nothing to sell, nothing to expire | no such module |
| P4 ✅ | **No onboarding.** First run demands a hand-edited `config.json`, then exits 1 | `config.py:load_config` |
| P5 ✅ | **No `/doctor`.** Nothing tells a buyer *why* it will not start | no such command in `BOT_COMMANDS` |
| P6 ✅ | **No update path.** Upgrading is `git pull` and hoping the schema migrated | `db.ensure_columns` is additive-only, which is right, but nothing drives it |
| P7 ✅ | **No tests.** Zero test files | tree listing |

### 3.2 Blocks "chotkiy ishlasin" on both engines

Measured from `claude --help` and `agy --help` today:

| Capability | `claude` | `agy` | Bot used it | **now** |
|---|---|---|---|---|
| Resume a conversation | `--resume <id>` | `--conversation <id>` | ✅ both | ✅ both, proven live on each |
| Plan mode | `--permission-mode plan` | `--mode plan` | ✅ both | ✅ both, proven live on each |
| Model | `--model` | `--model` | ✅ both | ✅ both |
| **Reasoning effort** | `--effort low\|medium\|high\|xhigh\|max` | `--effort low\|medium\|high` | ⚠️ **agy only** | ✅ one `/effort` dial, clamped per engine *and per model* |
| **Live model catalogue** | — | `agy models` | ❌ neither | ✅ read at boot, cached, refreshed daily, one-tap refresh |
| **Stream the model's words** | `--include-partial-messages` | — | ❌ | ✅ the progress card shows the prose |
| **Auto-compact context** | `--autocompact auto\|<tokens>` | — | ❌ | ✅ on by default (`autocompact` in config) |
| **Safe/read-only tier** | `--restricted` | `--sandbox` | ❌ neither | ✅ `/safe`, both engines |
| Extra directories | `--add-dir` | `--add-dir` | ❌ neither | ✅ **required** on agy — see the progress log |
| Named agent preset | `--agent` | `--agent` | ❌ neither | ❌ still unused (no demand yet) |
| Structured output | via `--settings` | `--json-schema` | ❌ neither | ❌ still unused (nothing in the bot consumes JSON) |
| MCP servers | `claude mcp` | `agy mcp` | ❌ neither | ❌ still unused — the buyer configures those in their own CLI |

Concrete defects behind that table:

- **E1 — a failed agy run loses its conversation.** ✅ *fixed 2026-08-28.* `_agy_events` reads
  `conversation_id` only from the `result` event. The `init` event carries it
  too (verified above). If a run times out or is killed, there is no `result`,
  so the resumable id is thrown away even though the bot had it in hand on the
  first line of output. `engines.py:_agy_events`
- **E2 ✅ *fixed 2026-08-28* — the agy model catalogue was a hand-copy that was already drifting.**
  `agy models` returns effort-suffixed ids: `gemini-3.7-flash-high`,
  `gpt-oss-120b-medium`, and `gemini-3.1-pro` has **no medium tier**. The
  hardcoded `AGY_MODELS` list carries unsuffixed ids plus a separate effort
  field. It works today, but it is a copy of someone else's catalogue and it
  will rot silently. `engines.py:AGY_MODELS`
- **E3 ✅ *fixed 2026-08-28* — Claude got no effort control at all.** The CLI has had `--effort`
  since 2.x and it is the single biggest quality/cost dial on the Claude side.
  agy has an effort dial in the bot; Claude does not.
- **E4 ✅ *fixed 2026-08-28* — the Claude model list was missing `fable`.** `claude --help` names the
  aliases as `fable`, `opus`, `sonnet`; the bot offers sonnet/opus/haiku.
  `engines.py:CLAUDE_MODELS_LIST`
- **E5 ✅ *fixed 2026-08-28* — progress showed tool names, never words.** During a 10-minute job the
  user sees `🔧 tahrir: UserController.php` and a step count. Claude can stream
  its actual prose with `--include-partial-messages`. This is the largest
  available upgrade to how the product *feels*.
- **E6 ⚠️ *half done 2026-08-28* — the bot's answer to a long context was to throw it away.** Contexts are
  retired after 15 jobs or 4 hours (`session_max_jobs`, `session_idle_reset_sec`)
  because a `--resume` chain replays its whole transcript. `--autocompact` is
  the CLI's own answer to exactly that problem and costs nothing to try.
- **E7 ✅ *fixed 2026-08-28* — the bot threw away the limit telemetry the CLI hands it.** Claude Code
  emits a `rate_limit_event` on the stream. `_claude_events` only looks at
  `assistant` and `result`, so it is discarded unread. See §4 — this single
  event is what makes automatic failover possible.

### 3.3 Code hygiene, security, correctness

- **C1 ✅ fixed 2026-08-28 — the same 18-line stdlib import header was copy-pasted into all 13
  modules.** `glob`, `http.server`, `mimetypes`, `signal`, `sqlite3`,
  `subprocess`, `urllib.*`, `uuid` are imported everywhere and used almost
  nowhere. It is a leftover from the file split (`0d9edcb`) and it is the first
  thing a paying developer will see when they open the source.
- **C2 ✅ fixed 2026-08-28 — `job_menu()` accepted an `engine` and ignored it.**
  `keyboards.py`
- **C3 ✅ fixed 2026-08-28 — `config["agy_print_timeout"]` was dead config.** The real value is
  computed from `job_timeout_sec` in `agy_print_timeout()`. It sits in
  `DEFAULT_CONFIG` looking meaningful. `config.py`, `engines.py`
- **C4 ✅ fixed 2026-08-28 — `/server` mislabelled its own number.** It prints "🤖 Claude ishlari: N"
  while counting running jobs across *all* engines. `screens.py:server_text`
- **C5 ✅ fixed 2026-08-28 — `/sh` and the engine paths leaked child processes.** `subprocess.run(shell=True, timeout=…)`
  kills the shell on timeout but not its grandchildren. The job path gets this
  right (`killasgroup` in supervisor, `proc.kill()`); the shell path does not.
  `jobs.py:run_shell`
- **C6 ✅ fixed 2026-08-28 — no rate limit on the local API.** A key protects the write endpoints,
  but any local process may hammer `/health`, and a compromised site user on a
  shared box can enumerate the port.
- **Not a bug, do not "fix" it:** `--permission-mode auto` *is* valid.
  `claude --help` lists `acceptEdits, auto, bypassPermissions, manual, dontAsk,
  plan`. The `/mode` command's list is correct as written.

---

## 4. Automatic failover — the headline feature

> ✅ **Built and live, 2026-08-28.** Everything in this section is implemented:
> `rate_limit_event` is read and stored, cooldowns are shared across jobs by
> engine, the chain is walked forward once per job, the handover paragraph is
> written in all three languages, every hop is announced, and `/limit` is built on
> the real gauges. What is *not* yet proven is the hop itself firing in anger: no
> window has run out since it shipped, and no quota-shaped agy failure has been
> captured (§4.4 still holds — the allow-list stays narrow until one is).

**When one engine's limit runs out, the job keeps going on the next one.** Claude
runs out at 3 a.m. mid-deploy → Gemini picks it up. Gemini runs out → the chain
walks on. The developer's night is not over because a quota window closed.

This is the feature worth putting on the box. It is also the one nobody else can
copy easily, because it needs both engines wired into one queue — which this bot
already has.

### 4.1 The signal already exists

Claude Code emits a `rate_limit_event` on the stream-json output. Captured live
today:

```json
{"type":"rate_limit_event","rate_limit_info":{
  "status":"allowed","resetsAt":1787911200,"rateLimitType":"five_hour",
  "overageStatus":"rejected","isUsingOverage":false,
  "unifiedWindows":{
    "five_hour":{"utilization":0.02,"resetsAt":1787911200},
    "seven_day":{"utilization":0.10,"resetsAt":1788134400}}}}
```

That is a live fuel gauge: how much of the 5-hour and 7-day windows is spent, and
the **exact unix timestamp** each one refills. The bot currently reads none of it.

Everything below follows from that one event, plus the `result` event's
`is_error`, `subtype`, `stop_reason`, `terminal_reason` and `api_error_status`
fields — all confirmed present in a real run today.

### 4.2 The chain

An ordered list of steps in `config.json`, each a `(engine, model, effort)`
triple. A sensible default:

| # | Engine | Model | Effort |
|---|---|---|---|
| 1 | claude | opus | high |
| 2 | claude | sonnet | high |
| 3 | agy | gemini-3.7-flash | high |
| 4 | agy | gpt-oss-120b | medium |

Two hop kinds, and the difference matters:

- **Within one engine** (1→2) the conversation survives — `--resume` accepts the
  same session id on a different model. The job continues with full context and
  the user barely notices.
- **Across engines** (2→3) it does not. A Claude session id means nothing to
  `agy`. The new engine starts cold.

### 4.3 The handoff prompt

This is where a naive failover destroys work. When the chain crosses engines, the
first engine may already have edited files. The second engine must be told so,
or it will redo work, or duplicate it, or contradict it.

The continuation prompt is therefore: the original task, plus an explicit
handover note — *another agent already began this task on this working tree and
stopped when its limit ran out; inspect what it changed (`git status`, `git
diff`) before you touch anything, then finish the job.* Nothing else in this
feature is as important as that paragraph.

### 4.4 When to hop

| Trigger | Signal | Action |
|---|---|---|
| **Pre-flight** | the step's engine has a stored `resetsAt` still in the future | skip the step before spending anything |
| **Pre-emptive** | `unifiedWindows.five_hour.utilization ≥ 0.95` | let this job finish, mark the engine nearly-dry, start the *next* job further down the chain |
| **Hard stop** | `rate_limit_info.status != "allowed"`, or `result.is_error` with a 429-shaped `api_error_status` | abandon the step, store `resetsAt`, hand off immediately |
| **Engine missing** | binary absent or not logged in (from `/doctor`) | skip permanently, warn once |

On the agy side the equivalent signal is `result.status != "SUCCESS"` with a
quota-shaped error. **No real sample has been captured yet** — until one is,
match only an explicit allow-list (`RESOURCE_EXHAUSTED`, `QUOTA_EXCEEDED`) and
treat every other non-`SUCCESS` status as an ordinary failure. Guessing wrong in
that direction wastes a chain hop on a genuine bug; guessing wrong in the other
direction hides real errors behind an endless engine shuffle.

### 4.5 Guards

Failover that fires on the wrong thing is worse than no failover.

- **Only limit and quota errors hop.** A user cancel, a timeout, the turn cap, or
  a task that genuinely failed must never walk the chain — otherwise one broken
  prompt burns every engine the buyer owns.
- **Never revisit a step** within one job. The chain is walked once, forward.
- **Cooldowns are shared across jobs**, keyed by engine, stored in `meta` with the
  `resetsAt` timestamp. Once Claude reports exhaustion, every queued job skips it
  until that timestamp — no re-probing the wall.
- **Every hop is announced** in the chat, with the reason and the reset time:
  `🟠 Claude limiti tugadi · 14:30 da tiklanadi → 🔵 Antigravity davom ettirmoqda`.
- **The chain is off by default for a first-time user** and enabled in one tap,
  because a hop spends the buyer's *other* subscription. That is their money.

### 4.6 What this pays for beyond failover

The same event turns `/limit` from a token tally into a real dashboard:
utilization bars for the 5-hour and 7-day windows, the exact refill time, and
which step of the chain is currently live. Today that screen sums a `tokens`
column that is populated on 1 of 90 rows. Reading `rate_limit_event` fixes the
screen and enables the feature in the same change.

### 4.7 Controls

`/fallback` — view the chain, reorder it, toggle it, and see each engine's
cooldown. `/limit` gains the gauges. `/status` shows which step is running when a
job is mid-chain.

---

## 5. Phases

### Progress log

**2026-08-28 — Phase 0, first half done.** Six of the seven defects in §3.3 and
§3.2 are fixed in the working tree. Not committed, and **the running bot is still
on the old code** — supervisor holds the loaded modules in memory, so
`supervisorctl restart server-pult-bot` is required before any of this takes
effect.

| Item | What changed | Verified by |
|---|---|---|
| **C1** | The copy-pasted 18-line stdlib header is gone. Every module now imports only what it uses — `keyboards.py` imports nothing from the stdlib at all, `telegram.py` needs ten. 179 dead import lines removed | AST scan for every referenced name, then importing all 12 modules |
| **E1** | `_agy_events` now takes `conversation_id` from the `init` event as well as from `result`, so a killed or timed-out agy run keeps a resumable conversation | unit check: `init` event → `("session", id)` |
| **C2** | `job_menu()` no longer takes an `engine` it ignored; the three call sites updated | renders |
| **C3** | Dead `agy_print_timeout` key dropped from `DEFAULT_CONFIG`; the value has always come from `agy_print_timeout()` | grep |
| **C4** | `/server` no longer labels an all-engine count "Claude ishlari" — it now prints a per-engine line | rendered the real screen |
| **C5** | `signal_group()` added. Every child (`/sh`, both engines) is spawned with `start_new_session=True`, and the timeout, cancel and turn-cap paths all signal the **group**. A build or dev server the agent started no longer survives its parent | live test: 3 processes in the group before the kill, 0 after |

**2026-08-28 (evening) — Phases 0, 1 and 2 complete, live, and proven on both
engines.** 122 tests, 3,511 lines, 320 locale keys × 3 languages, running under
supervisor since 18:29.

**Phase 0 — the safety net.**

| Item | What landed |
|---|---|
| `SERVER_PULT_HOME` | `core.BASE_DIR` honours it, `config.load_config()` no longer exits at import, `config_problems()` reports instead. The whole package now imports with no config at all — which is what made tests possible, and what makes `/doctor` able to explain a broken install |
| Tests | `tests/`, plain `unittest`, stdlib only: engine event readers against **streams recorded from real runs of both CLIs**, catalogue parsing, effort clamping, model resolution, flag building, session expiry on both limits, cooldowns, chain walking, hop rules, handover text, outbox chunking and button placement, schema migration from a pre-`engine` `state.db` (in a subprocess, against a real old file), the private-chat guard, keyboard-label routing, and a process-group kill that proves a grandchild dies with its job |
| Locale tests | key sets identical across uz/ru/en, every `t()` key in the source present in every file, placeholders identical per key, balanced HTML tags, no locale claiming a reserved placeholder name — the trap `Lang::has` set on the Laravel sites, closed here before it could open |
| CI | `.github/workflows/tests.yml`: `python -m unittest discover` on 3.11 and 3.13, plus `bash -n install.sh`. No pip step, on purpose |
| Fixtures | `tests/fixtures/{claude_stream.jsonl,agy_stream.jsonl,agy_models.txt}` — captured today, including a `rate_limit_event`, a `system/status`, a `text_delta`, a permission denial, and both a `SUCCESS` and a non-`SUCCESS` agy result |

**Phase 1 — both engines, excellent.** Live model catalogue (E2), one `/effort`
dial clamped per engine *and* per model (E3), `fable` (E4), streamed prose in the
progress card (E5), `--autocompact auto` (E6), `/safe` → `--restricted` /
`--sandbox`, and all of §4.

**Three defects that only a live run could find.** This is why "prove agy end to
end" was item 1 of Phase 1, and it earned its place:

1. **agy ignores the process working directory.** The bot has always spawned it
   with `cwd=<project>`; the first real agy job wrote its file into
   `/root/.gemini/antigravity-cli/scratch/`. The project picker — the thing the
   whole bot is organised around — did *nothing* on that engine. Fixed by passing
   `--add-dir <workdir>`; the next job wrote to the right tree and said so.
2. **agy refuses a model that has effort tiers unless `--effort` is given.**
   `--model gemini-3.7-flash` alone exits with *"invalid model selection …
   requires --effort (available: low, medium, high)"*. The old code hid this by
   hard-coding an effort per model; the new dial defaults to "engine decides",
   which on this engine is not a thing. `agy_effort_for()` now supplies the
   strongest tier the model offers when the dial is empty.
3. **`agy models` wedged the bot for minutes under supervisor.**
   `subprocess.run(timeout=…)` kills the child but keeps waiting on a pipe a
   grandchild still holds. Now every short engine command runs in its own process
   group, with stdin closed, and is killed as a group — the same discipline the
   job path already had. The refresh is also serialised behind a lock: start-up
   and housekeeping were both calling it, racing each other at boot.

Also fixed while proving it: an engine that answers *"I failed"* used to be
reported to the user with a green tick, because only a missing result counted as
failure. It is now reported as a failure, with the engine's own words.

**Proven live, through the bot's own queue and workers:** claude writes a file
(#91) · agy writes a file in the right directory (#94) · agy resumes its
conversation and remembers what it did (#95) · agy plan mode changes nothing
(#96) · `rate_limit_event` captured and stored (five-hour 33 %, seven-day 14 %,
with exact reset timestamps) · the live catalogue parsed into seven models,
including `gemini-3.1-pro` correctly showing **no medium tier**.

**Phase 2 — the product shell.** `install.sh` (idempotent; checks Python ≥ 3.11,
warns per missing engine rather than failing, writes `.env` at 0600, registers
**supervisor or systemd**, starts it, prints the pairing code and the bot link) ·
three locales with the engine's own instruction translated too ·
`/doctor` · a four-tap onboarding wizard after pairing · `/update` (dirty-tree
guard, migrate, restart) · version in `/start`.

**Optimisation pass (§6) done in passing:** `/server` forks twice instead of a
dozen times (one shell for the metrics, one `systemctl is-active` for every unit,
service list cached 10 min), `list_projects()` is cached for 30 s instead of
re-globbing on every keyboard render, and token accounting now records on every
job — agy's mid-run `usage` is kept too, so a killed run still accounts for what
it spent.

**Message formatting pass (owner's call, same evening).** The screens were plain
text with a few bold tags; they now share one visual language, and it is enforced
by tests rather than by discipline:

- `core.py` gained `RULE` / `THIN_RULE`, `card()` (join lines, `None` drops one,
  `""` keeps a blank), `quote()` (Telegram `<blockquote>`, expandable when long)
  and `kv()`.
- Every card is head → rule → labelled rows → quoted body: the task on a confirm
  card, the model's answer on a result card, the running prompt on `/status`, and
  the explanatory paragraph at the foot of each settings screen.
- The progress card carries a spinner glyph that turns on every edit, so a long
  job no longer looks frozen, and it puts the model's own words above the tool
  name when it has them.
- Fit is now decided on the *rendered* message rather than the raw answer: a card
  split across two Telegram messages would break its own HTML and get retried as
  plain text, silently losing the formatting.
- `tests/test_screens.py` renders all thirteen screens in all three languages and
  asserts every tag is closed, only Telegram's tags are used, no `⟪missing.key⟫`
  survives, each screen carries its rule, and none is too long for one message.

**Deliberately not done, and why:**

- **Phase 3 (licensing) and Phase 4 (distribution)** wait on §8. Both branch on
  answers only the owner has: price and plan shape decide what the license server
  must store, where it lives decides how the client checks in, and whether the
  repo is public decides whether the installer ships a key at all. Building a
  license client against a server that does not exist would be guesswork.
- **Raising `session_max_jobs`** (E6, second half). `--autocompact auto` is on,
  but the claim "a context now survives past 15 jobs without the cost curve
  bending" needs a week of real use to measure. The dial stays at 15 until the
  numbers say otherwise.
- **A quota-shaped agy failure** has still never been seen. The allow-list stays
  `RESOURCE_EXHAUSTED` / `QUOTA_EXCEEDED` and every other non-`SUCCESS` status is
  an ordinary failure, exactly as §4.4 argues.
- **`--agent`, `--json-schema`, MCP.** Real flags, no demand: nothing in the bot
  consumes structured output, and a buyer configures MCP in their own CLI.

---

Ordered so each phase is shippable on its own and the risky work happens while
there is still exactly one user to break.

### Phase 0 ✅ — A safety net, then fix what is already broken

Nothing else in this plan is safe to do without this. No new features.

- `tests/` with plain `unittest` — stdlib only, matching the project's rule.
  Cover: outbox chunking and the HTML→plain retry, `split_engine_prefix`,
  `resolve_model` across both engines, `take_session` expiry on both limits,
  `_claude_events` and `_agy_events` against **recorded real fixtures** (capture
  them once from a live run of each CLI — including a `rate_limit_event` and a
  `system/status` event, both of which the parser currently ignores), schema
  migration from an old `state.db`, and the private-chat guard.
- Fix **E1** (read `conversation_id` from `init`), **C1** (per-module imports),
  **C2**, **C3**, **C4**, **C5** (`start_new_session=True` + `os.killpg`).
- GitHub Actions: run the tests on push. The repo already has a remote.

*Done when:* `python3 -m unittest discover` is green and CI passes on a clean
clone. — **Done: 122 tests green; CI added (not yet run: the GitHub remote still
does not exist, see below).**

### Phase 1 ✅ — Make both engines excellent

This is the phase the owner actually asked for. Everything here is measured
against: *would a developer paying monthly notice?*

1. **Prove agy end to end.** Run a real job on each engine through the bot,
   including plan mode, cancel, timeout, resume, and a deliberate failure.
   Record the fixtures Phase 0 wants. Until this is done, the "two engines"
   claim on the tin is unearned.
2. **Live model catalogue (E2).** Read `agy models` at start-up, cache it in
   `meta`, refresh daily, fall back to the hardcoded list when the call fails.
   The model picker then shows what the buyer's account can actually reach —
   including models that ship after this bot does.
3. **Effort parity (E3, E4).** One `/effort` control that maps onto
   `--effort` for both engines, clamped per engine (Claude reaches `xhigh` and
   `max`; agy stops at `high`). Add `fable` to the Claude list.
4. **Stream the model's words (E5).** `--include-partial-messages` on the Claude
   side, feeding the same edited progress message that today shows only tool
   names. Rate-limit the edits to `progress_interval_sec` — Telegram will not
   take more. agy has no equivalent flag, so it keeps the tool-name view; the
   two must still *look* like one product.
5. **Autocompact (E6).** Try `--autocompact auto` on the Claude side and measure
   whether a context survives past 15 jobs without the cost curve bending. If it
   does, raise `session_max_jobs` and say so in `/limit`.
6. **A safe tier.** `--restricted` (claude) / `--sandbox` (agy) behind a
   `/safe` toggle, so a buyer can point the bot at production and know it cannot
   run a command. This is a *selling point*, not a nicety.
7. **Engine-aware everything.** The result keyboard, the banner, the progress
   card and `/status` already distinguish the two; `/server`, `/limit` and the
   error paths do not, quite. Finish it.
8. **Automatic failover — all of §4.** Read `rate_limit_event`, store per-engine
   cooldowns, walk the chain, write the handover prompt, announce every hop, and
   rebuild `/limit` around the real gauges. This is the largest single item in
   the plan and the one the product is sold on; it lands last in this phase
   because it depends on 1 (a proven agy path), 2 (a real model catalogue) and
   3 (per-step effort).

*Done when:* a fresh operator can run the same task on both engines, compare the
two results, never see a screen that assumes Claude — and a job that starts on a
Claude window with 2 % left finishes on Gemini without being asked twice. —
**Done except the last clause, which cannot be staged: no window has run out
since it shipped. The mechanism is unit-tested end to end (cooldown → skip →
hop → handover) and every screen is engine-aware; the first real hop will be the
proof.**

### Phase 2 ✅ — The product shell

1. **i18n (P2).** `pult/i18n.py` + `locales/{uz,ru,en}.json`. Keys in English,
   values per language — this repo's code, comments and config stay English;
   only what a human reads in Telegram is translated. Uzbek is the reference
   locale because it is the one that exists; Russian is the market; English is
   the default for anyone else. A test asserts all three files have identical
   key sets, and that no locale is silently falling back (the same trap that
   `Lang::has` sets on the Laravel sites).
2. **`install.sh` (P1).** One command, idempotent: check Python ≥ 3.11, find
   `claude` and `agy` on `PATH` and warn per missing engine rather than failing,
   create `.env` from answers (bot token, admin id, language), generate a
   pairing code, write **and register** the supervisor unit *or* a systemd unit
   depending on what the box has, start it, and print the pairing code plus the
   bot link.
3. **`/doctor` (P5).** Engine binaries and versions, whether each is logged in,
   Telegram reachability, disk and inode headroom, DB writability, local API
   port, license state, and the supervisor/systemd unit's health. One screen
   that answers "why is it not working" without an SSH session.
4. **Onboarding (P4).** First contact after pairing runs a short wizard:
   language → project directory → engine → confirm-before-run on/off. Today it
   is `config.json`, which no buyer will edit correctly.
5. **`/update` (P6).** `git pull` + migrations + restart, guarded by a dirty-tree
   check, with the version shown in `/start`.

### Phase 3 ⏳ — Licensing and subscription *(blocked on §8)*

- `pult/license.py`: key in `.env`, checked against a license API at start-up
  and daily. **Offline grace of 7 days** — a network blip must never take a
  developer's server tooling away from them. Expiry warns in the bot at 7, 3 and
  1 days, then degrades to read-only rather than going silent.
- License server: a small Laravel app (this is the stack that already exists on
  this machine, and the payment integration is already solved there). Keys,
  plans, machine binding, renewal.
- Pricing is the owner's call. Note for that decision: the catalogue on
  templates.uz has never earned, and the margin on this server has always come
  from service, not from listings.

### Phase 4 ⏳ — Distribution *(blocked on §8.3)*

Landing page, README rewritten for a buyer rather than for its author, a 60-second
demo video, install docs in three languages.

---

## 6. The optimization pass

Threaded through the phases above, not left to the end.

**Cost.** Token spend is the buyer's real bill, and today only 1 job in 90
recorded any. Fix the accounting first, then optimize against it: effort
defaults per task size, autocompact instead of context-dropping, and a `/limit`
screen built on the real `rate_limit_event` gauges (§4.6) rather than a summed
column — utilization and refill times, per engine, so the dial has a readout.
The cheapest optimization available is not spending a window at all: with the
chain on, a job that would have queued behind a five-hour reset simply runs
somewhere else.

**Latency.** Callbacks already run off the poller thread (`guarded_callback`) —
that was the right fix. Remaining: `/server` shells out ~10 times serially and
is on the main menu; `list_projects()` re-globs the filesystem on every keyboard
render; `server_text()` runs `systemctl is-active` once per candidate service.
Cache what does not change per second.

**Disk.** `state.db` is 148 KB with 90 jobs and housekeeping already truncates
the WAL, rotates `audit.log` at 5 MB and drops uploads after 7 days. This is in
good shape — leave it alone and keep it that way as job volume grows.

**Startup.** The bot exits 1 on a missing token, which is correct, but it should
say *which* file to fix and offer to run the installer.

**Legibility.** After C1, every module's import block should read as
documentation of what that module actually touches. That is what a buyer flips
through before deciding the thing is trustworthy.

---

## 7. Non-goals

- **Not multi-tenant.** One bot, one server, one owner. Two operators sharing a
  bot means two people's jobs in one queue against one `$HOME`.
- **Not a group bot.** The private-chat guard stays. Server state in a group is
  a leak, not a feature.
- **No pip dependencies.** Stdlib-only is a feature: it installs anywhere, it
  cannot break on a transitive upgrade, and it is auditable in an afternoon.
- **Not a web UI.** Telegram is the product surface.
- **No third engine** until both existing ones are proven.

---

## 8. Open decisions for the owner

> These now block the *only* remaining work. Phases 0–2 are done; Phase 3 and 4
> cannot start until 1–3 below are answered. One more, new: **the GitHub remote
> `git@github.com:Iqbolshoh/server-pult-bot.git` still does not exist** — the
> repo has never been pushed, so CI has never run.

1. **Price and plan shape** — monthly per server? Lifetime with a year of
   updates? This decides how much Phase 3 has to build.
2. **Where the license server lives** — a new subdomain on this machine, or
   folded into an existing Vexa app.
3. **Is the repo public?** A public repo with a licensed binary path is one
   product; a private repo sold by access is another, and it changes Phase 2's
   installer.
4. **Russian first or English first**, if the three locales cannot ship at once.

None of these block Phase 0 or Phase 1, which is why they are ordered first.
