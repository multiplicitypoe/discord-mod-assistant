# Discord Incident Assistant

Mod-only `/mod` command that summarizes the last N messages (default 50), reads images, and drafts a polite, firm de-escalation message with evidence. Includes lightweight, opt-in memory so the bot can learn server tone and repeated behavioral patterns over time.

## What it does

- `/mod` analyzes the last N messages in a channel (text + images).
- Produces an evidence-backed summary, rule references, and a de-escalation draft.
- Uses a rules channel to build a compact, cached rules memory.
- Supports lightweight server memory + gradual per-user memory (requires repeated observations).
- Mod-only by default; public posting is an explicit confirmation.
 - Includes a "Save Memory" button for suggested notes.

## Quick start

1) Install dependencies:

```bash
poetry install
```

2) Create `.env` (see `.env.example`):

```bash
cp .env.example .env
```

3) Run the bot:

```bash
make run-bot
```

## Running with Docker (WSL + Raspberry Pi OS 64-bit)

1) Create `.env` (see `.env.example`):

```bash
cp .env.example .env
```

2) Build and run:

```bash
docker build -t discord-incident-assistant .
docker run --rm --env-file .env \
  -v "$(pwd)/data:/app/data" \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --memory 512m \
  --cpus 1.0 \
  --user "$(id -u):$(id -g)" \
  discord-incident-assistant
```

Stop with Ctrl+C.

If you prefer Compose, `docker-compose up --build` also works (uses `docker-compose.yml`; override UID/GID if needed).

## Slash commands

- `/mod limit:50`

## Message context menu

- Right click / long-press a message -> `Apps` -> `Mod` (analyzes messages around that specific message)
- `/incident_config rules_channel:#rules mod_role:@Moderators`
- `/incident_rules_sync`
- `/incident_memory_add text:...`
- `/incident_memory_list`
- `/incident_memory_reset`
- `/incident_auto_route_set role:@Role channel:#mod-channel`
- `/incident_auto_route_clear role:@Role`
- `/incident_auto_route_list`
- `/incident_auto_ignore_add category:Moderation`
- `/incident_auto_ignore_remove category:Moderation`
- `/incident_auto_ignore_list`

## Environment variables

Required:

- `DISCORD_TOKEN`
- `OPENAI_API_KEY`

Optional:

- `OPENAI_MODEL` (default: `gpt-4.1-mini`)
- `OPENAI_IMAGE_DETAIL` (`low`, `high`, or `auto`, default: `low`)
- `OPENAI_MAX_IMAGE_DIM` (default: `512`)
- `DB_PATH` (default: `data/incident_bot.sqlite3`)
- `DEFAULT_LIMIT` (default: `50`)
- `MAX_LIMIT` (default: `200`)
- `MAX_IMAGES_TO_ANALYZE` (default: `8`)
- `MOD_ROLE_ID` (optional; restricts usage to a specific role)
- `DEBUG_LOGS` (default: `false`)
- `AUTO_MOD_DEFAULT_CHANNEL_ID` (if set, enables auto brief posting on non-mod role pings)

Debug-only:

- `OPENAI_DEBUG_DUMP_DIR` (default: `data/openai_debug`) - where to write raw model outputs when JSON parsing fails (only used when `DEBUG_LOGS=true`).

## OpenAI usage

This bot makes two OpenAI calls per `/mod`:

1) **Image notes**: low-detail summary of images in the last N messages.
2) **Incident analysis**: summary + evidence + rules mapping + draft response.

Image notes are short and only used to reduce cost and provide evidence.
Images are resized before analysis to control cost.

## Memory model

- **Rules memory**: summarized from a configured rules channel; cached.
- **Server memory**: explicit notes about tone/preferences; opt-in.
- **User memory**: built gradually from repeated observations (requires 2+ occurrences).
- Memory is opt-in via the "Save Memory" button or `/incident_memory_add`.

No full transcripts are stored by default.

## Permissions

- `/mod` is mod-only. If `MOD_ROLE_ID` is set, only that role can use it.
- The bot also checks `Moderate Members` permission.

## Contributing

PRs welcome. If you add new features, keep the system prompt conservative and evidence-driven.
