# Server Pult

Your server, in your pocket. One Telegram bot that drives **two AI coding
engines** — Claude Code and Antigravity — on a machine you own, runs them side by
side, and **carries a job on with the next engine when one runs out of limit**.

Self-hosted, Python stdlib only, no pip dependencies.

```
you (Telegram)  ->  Server Pult  ->  claude   (your Claude subscription)
                                 ->  agy      (your Antigravity account)
```

| Engine | CLI | Models | Effort |
| --- | --- | --- | --- |
| 🟠 Claude | `claude` (Claude Code) | fable · opus · sonnet · haiku | low → max |
| 🔵 Antigravity | `agy` | read live from `agy models` | low → high |

## Install

```sh
git clone <this repo> /opt/server-pult-bot
cd /opt/server-pult-bot
./install.sh
```

The installer asks for a bot token (from [@BotFather](https://t.me/BotFather)),
your Telegram user id and a language, writes `.env` and `config.json`, registers a
**supervisor or systemd** unit, starts it, and prints a pairing code. Send that
code to your bot and it is yours — it answers nobody else, ever.

Then the bot walks you through language → project directory → engine →
confirm-before-run, and you are working.

If anything is wrong, send **`/doctor`**: engine binaries and versions, whether
each is logged in, Telegram reachability, disk and inode headroom, database
writability, the local API, the service unit, and the model catalogue — one
screen instead of an SSH session.

## What it does

- **Write a task, it runs on the server.** Your phone can lose signal; the job
  does not stop, and the result arrives when the connection comes back.
- **Two engines, one queue.** `c:` runs it on Claude, `a:` on Antigravity, `b:`
  on both at once — each engine has its own worker, so "both" is genuinely
  parallel. Every result carries a one-tap button to try the same task on the
  other engine.
- **Automatic failover.** When a limit runs out mid-job, the job continues on the
  next engine in the chain instead of dying. See below.
- **Live progress.** The progress card streams what the model is actually saying,
  not just a list of tool names.
- **Plan first.** Every task can be planned before it runs — the plan changes
  nothing, and one button turns it into work.
- **Safe mode.** `/safe` removes every command-running tool and confines file
  access to the working directory (`--restricted` / `--sandbox`), so you can point
  the bot at production and know it cannot run anything.
- **Three languages.** Uzbek, Russian, English — the bot's screens and the
  instruction the engine is given, both.
- **Photos and files.** Send a screenshot with a caption; the engine sees it.
- **No tokens spent on server questions.** `/server`, `/ls`, `/sh`, `/limit` and
  `/doctor` are plain shell and SQL.

## Automatic failover

`/fallback` holds an ordered chain of `(engine, model, effort)` steps:

| # | Engine | Model | Effort |
|---|---|---|---|
| 1 | claude | opus | high |
| 2 | claude | sonnet | high |
| 3 | agy | gemini-3.7-flash | high |
| 4 | agy | gpt-oss-120b | medium |

Claude Code reports its own fuel gauge on the output stream — how much of the
five-hour and seven-day windows is spent, and the exact moment each refills. Server
Pult reads it, shows it in `/limit`, and acts on it:

- an engine with a stored reset time in the future is **skipped before a single
  token is spent**;
- an engine that is nearly dry **steps aside for the next job** while finishing
  the one it has;
- a job that stops because a window ran out **hops to the next step and carries
  on**, and every hop is announced in the chat with the reason and the reset time.

Crossing engines, the new one is told in as many words that another agent already
edited this working tree, and to run `git status` and `git diff` before touching
anything. That paragraph is the difference between a failover and a mess.

**Only a limit or an overloaded model hops.** A cancel, a timeout, the turn cap or
an ordinary failure never walks the chain — otherwise one broken prompt would burn
every subscription you own. The chain is off until you turn it on, because a hop
spends your *other* account.

## Buttons

The bottom keyboard is the menu. It is always on screen and carries the eleven
things worth one tap, so no message repeats it: a screen that needs no buttons of
its own is sent with none. Inline buttons are for what belongs to *that* message
— approve or drop a task, stop a running job, pick a model, flip a setting.

## Commands

```
/status /jobs /history /get N /stop /new       jobs
/engine /both /fallback /model /effort         engines
/projects /cd /pwd /ls /file                   files
/server /sh /limit /doctor                     the machine
/mode /safe /confirm /language /settings       behaviour
/menu /keyboard /ping /update /restart         the bot
```

## Layout

```
bot.py               entrypoint: wiring and thread start-up only
pult/core.py         paths, shared runtime state, formatting helpers
pult/config.py       config.json + .env, and the local-API key
pult/i18n.py         locales/<lang>.json -- everything a human reads
pult/db.py           SQLite schema and the meta table
pult/telegram.py     API client, IPv4-first pooled transport, durable outbox
pult/engines.py      the engines: flags, event dialects, catalogue, limits
pult/failover.py     the chain: cooldowns, hops, the handover prompt
pult/projects.py     which directories a job may run in
pult/keyboards.py    inline and reply keyboards
pult/screens.py      every screen, rendered as HTML
pult/jobs.py         the queue: one worker per engine, result delivery
pult/handlers.py     updates -> commands, callbacks, jobs
pult/localapi.py     loopback helper the agents call through curl
pult/maintenance.py  periodic disk, database and catalogue upkeep
locales/{uz,ru,en,tj}.json
tests/               plain unittest, stdlib only; fixtures recorded from the CLIs
```

Imports run strictly one way down that list, so no module can import a module
below it and no cycle can form.

## State

```
config.json   settings the bot rewrites as you change them (untracked)
.env          BOT_TOKEN, ADMIN_CHAT_ID, AGY_FLAGS (untracked, chmod 600)
state.db      SQLite: jobs, outbox, conversations, limits, cooldowns
uploads/      files received from Telegram, dropped after 7 days
audit.log     append-only record of every instruction given, rotated at 5 MB
```

`SERVER_PULT_HOME` moves all of that out of the source tree if you want the
checkout read-only.

## Running and updating

```sh
supervisorctl restart server-pult-bot    # or: systemctl restart server-pult-bot
tail -f /var/log/server-pult-bot.log
python3 -m unittest discover             # the test suite, no dependencies
```

`/update` inside the bot does `git pull`, migrates the database and restarts —
refusing to touch a working tree with uncommitted changes.

`HOME` must point at the account the engines are logged in as: `claude` reads its
login from `~/.claude`, `agy` from `~/.gemini`.

## Design notes

- **Durable jobs.** A task is stored before it runs and results wait in an outbox
  until Telegram accepts them, so nothing is lost to a restart or a flaky uplink.
- **One worker per engine.** Parallel across engines, serialised within one — two
  runs of the same CLI over one tree would fight over the files.
- **Context is retired on purpose.** Resuming replays the whole transcript, so a
  context is dropped after 15 jobs or 4 hours idle; each engine keeps a separate
  one per project. `--autocompact` is on for Claude.
- **Runaway guard.** The `claude` CLI has no `--max-turns`, so the bot counts
  assistant turns in the stream and kills the child past `max_turns`.
- **Everything dies with its job.** Every child runs in its own process group and
  is signalled as a group, so a build or a dev server an agent started cannot
  outlive it.
- **Private chats only.** Message and callback paths both require
  `chat_id == user_id`: added to a group, the bot stays mute.
- **Local API.** A loopback helper on `127.0.0.1:7799` lets a running agent push a
  file or a progress note into the chat. Write endpoints need a per-boot key that
  only the agents are told; every endpoint is rate limited.
