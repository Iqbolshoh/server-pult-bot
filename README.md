# Server Pult

One Telegram bot (`@server_pult_bot`) that drives **two AI coding engines** on this
server, and can run both of them at the same time.

| Engine | CLI | Models |
| --- | --- | --- |
| 🤖 Claude | `claude` (Claude Code) | sonnet · opus · haiku |
| 🛸 Antigravity | `agy` | gemini 3.x · claude 4.6 · gpt-oss-120b |

## Why two workers

Each engine gets its own worker thread and its own job queue, so a Claude job and
an Antigravity job genuinely run side by side. Within one engine jobs stay
serialised — two runs of the same CLI over the same tree would fight over the
files.

Send `b: <task>` (or `/both <task>`) to hand the same task to both engines at
once and compare what they do.

## Layout

```
bot.py               entrypoint: wiring and thread start-up only
pult/core.py         paths, shared runtime state, formatting helpers
pult/config.py       config.json + .env, and the local-API key
pult/db.py           SQLite schema and the meta table
pult/telegram.py     API client and the durable outbox
pult/engines.py      the two engines and their per-project contexts
pult/projects.py     which directories a job may run in
pult/keyboards.py    inline and reply keyboards
pult/screens.py      every screen, rendered as HTML
pult/jobs.py         the queue: one worker per engine, result delivery
pult/handlers.py     updates -> commands, callbacks, jobs
pult/localapi.py     loopback helper the agents call through curl
pult/maintenance.py  periodic disk and database upkeep
```

Imports run strictly one way down that list, so no module can import a module
below it and no cycle can form.

## Files

```
bot.py               the whole bot (python stdlib only, no pip deps)
config.example.json  template — copy to config.json and edit
config.json          this machine's settings (untracked)
.env                 BOT_TOKEN, ADMIN_CHAT_ID, AGY_FLAGS (untracked)
state.db             SQLite: job queue, outbox, per-engine conversation ids
uploads/             files received from Telegram
audit.log            append-only record of every instruction given
```

## Setup

```sh
cp config.example.json config.json
cp .env.example .env      # then fill in BOT_TOKEN and ADMIN_CHAT_ID
```

Nothing environment-specific is tracked: `config.json` is rewritten whenever a
setting changes in the bot, so it stays out of git along with `.env`.

## Running

Managed by supervisor as `server-pult-bot`:

```sh
supervisorctl restart server-pult-bot
tail -f /var/log/server-pult-bot.log
```

`HOME=/root` is required: the `claude` CLI reads its login from there and `agy`
reads `/root/.gemini`.

## Design notes

- **Durable jobs.** A task is stored before it runs, so it survives a phone
  losing signal or the bot restarting; results wait in an outbox until Telegram
  accepts them.
- **Context is retired on purpose.** Resuming a conversation replays its whole
  transcript, so a context is dropped after 15 jobs or 4 hours idle. Each engine
  keeps a separate context per project.
- **Runaway guard.** The `claude` CLI has no `--max-turns`, so the bot counts
  assistant turns in the stream and kills the child past `max_turns`.
- **Private chats only.** Both the message and callback paths require
  `chat_id == user_id`, so the bot stays mute if it is ever added to a group.
- **Local API.** Loopback-only helper on `127.0.0.1:7799` lets a running agent
  push a file or a progress note into the chat. Write endpoints require a
  per-boot key that only the agents are told about.
