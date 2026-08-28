# Deployment — launchd agents (macOS)

Runs the **backend** (`uvicorn` on `127.0.0.1:8000`) and the **Telegram bot**
as user LaunchAgents: they start at login, restart on crash, and survive
reboots. The daily 20:00 reminder scheduler lives inside the backend, so
keeping the backend agent up is what makes reminders fire unattended.

The Streamlit dashboard is **not** managed here — start it when you want it:

```bash
uv run streamlit run src/app.py   # talks to the always-on backend
```

## Install / update

```bash
./deploy/launchd/manage.sh install
```

Renders the plist templates in this directory (filling in the `uv` path and
project directory), copies them to `~/Library/LaunchAgents/`, and loads both.
Re-run after editing a template or moving the repo.

## Everyday commands

| Command | Effect |
|---|---|
| `manage.sh status`  | launchd state / pid / last exit code for both |
| `manage.sh logs`    | `tail -F` the four log files under `logs/` |
| `manage.sh restart` | restart both (after a code change) |
| `manage.sh stop`    | stop both — do this before `./run.sh` or a `--reload` dev server |
| `manage.sh start`   | bring both back after `stop` |
| `manage.sh uninstall` | stop and remove both agents |

## Developing with hot-reload

The agent backend has no `--reload`. To hack on the backend:

```bash
./deploy/launchd/manage.sh stop
./run.sh                          # or: uv run uvicorn src.main:app --reload
# ... when done ...
./deploy/launchd/manage.sh start
```

`run.sh` frees port 8000 on startup, so running it while the backend agent is
up would kill the agent and launchd would immediately restart it into a port
fight. Always `stop` first.

## Notes

- Requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HABIT_TRACKER_USERNAME`,
  `HABIT_TRACKER_PASSWORD`, `JWT_SECRET_KEY`, `ANTHROPIC_API_KEY` in `.env` at
  the project root (the agents run with the repo as their working directory).
- The bot exits if the backend is unreachable at startup; launchd retries
  every 10s (`ThrottleInterval`) until the backend is up.
- Logs rotate nowhere — truncate `logs/*.log` by hand if they grow.
