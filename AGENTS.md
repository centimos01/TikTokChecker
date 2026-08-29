# AGENTS.md — TikTokChecker

TikTok unfollower auditor: a Docker container that compares following/followers
(SQLite WAL) and alerts via Discord. The project's user-facing content is in
Spanish (README, logs, messages); keep that language in output.

> IMPORTANT: the end user speaks Spanish. Always reply to the user in Spanish,
> even though this file is written in English. Code comments, log messages and
> Discord alerts are also in Spanish — keep them that way.

## Two alert types (distinct semantics)

`main.py` distinguishes two kinds of unfollow, notified differently:

- **"Baja de vuelta"** (te dejan de seguir pero TÚ sigues = `following - followers`):
  always active, not configurable. Red embed, `0xED4245`.
- **"Baja total"** (te dejan de seguir Y ya no los sigues): **configurable** via
  Discord slash command `/notificaciones on|off`. Fuchsia embed, `0xEB459E`.

Toggling is NOT env-var based — it is persisted in the SQLite `settings` table
(key `full_alerts_enabled`), so it survives restarts. When OFF, detections are
still recorded to the DB but no embed is sent. "Baja total" detection needs
`followers_history` (per-cycle snapshot) to compare against the previous cycle:
the first run only establishes a baseline, so no "baja total" alert fires until
the second cycle. Keep both tables (`settings`, `followers_history`,
`fully_unfollowed`) intact when editing the schema.

Slash commands live in `DiscordGateway.SLASH_COMMANDS` and are registered only
on startup. After adding/changing a command you must rebuild the image on the
server (`docker compose up -d --build`) or the bot won't see the change.

## Work machine vs. deploy target

- This machine (Windows) is NOT the deploy target: Docker is not installed,
  and the local Python (3.11.9) does not have the project's dependencies.
  Only edit code here.
- The service runs on a remote Debian 13 (Trixie) server. Image
  `python:3.13-slim` (Trixie-based, same family as the host).
- Only local verification possible: `python -m py_compile main.py`. No tests,
  lint or CI configured. Do NOT try to run `main.py` directly on this machine.

## Deployment (on the target server)

1. Copy the project from here: `scp -r TikTokChecker user@host:~/`.
2. On the server: `cp .env.example .env`. Mandatory: `docker-compose.yml`
   loads `.env` via `env_file`; without it `docker compose up` fails.
3. First start: copy the cookies file into the project dir, then
   `docker compose up -d --build`, copy cookies into the volume, restart.
4. Manual debugging inside the container:
   `docker compose exec tiktok-checker python main.py --once --debug`.

## TikTok authentication (cookies)

- TikTok has NO usable Python API client for follower/following lists.
  Authentication is 100% cookie-based: the user exports browser cookies
  (after logging in to tiktok.com) as a JSON or Netscape file.
- The cookies file MUST contain `sessionid` (and ideally `ttcsrf`/`csrf_token_id`).
  Without sessionid the checker cannot work.
- Cookies expire; when that happens, the user must re-export from the browser
  and copy the new file to the volume.
- There is NO password login, NO instagrapi equivalent — all API calls go
  through requests + cookies to TikTok's internal web API.

## TikTok internal API

- `GET /api/user/detail/` — fetch authenticated user's profile (secUid, id).
- `GET /api/following/list` — paginated list of accounts the user follows
  (cursor-based: `max_cursor` param, `has_more` in response).
- `GET /api/follower/list` — paginated list of followers (same pagination).
- All endpoints require valid cookies and CSRF token (sent as `x-csrf-token`
  header). Rate limiting is enforced via random delays between requests.
- If TikTok returns `status_code: 8`, it means CAPTCHA/bot detection. The
  checker logs the issue and sends a Discord error alert.

## Data, compose and operation

- Persistent state in named volume `checker-data:/data` (cookies file +
  `audit.db` WAL), NOT a bind mount. The image runs as user `app`; switching
  to a bind mount breaks permissions.
- `docker-compose.yml` uses service-level `cpus`/`mem_limit` keys
  (compose v2, not `deploy.resources`). `read_only: true` + `tmpfs /tmp`:
  any write outside `/data` or `/tmp` fails.
- main.py: single entry point, 100% env-var config; only flags `--once`
  and `--debug`. In loop mode it sleeps `CHECK_INTERVAL_HOURS` + jitter.
- Do not lower `CHECK_INTERVAL_HOURS` below ~4 h (risk of TikTok rate-limit/ban).
- Never commit `.env` or cookie files (`.dockerignore` already excludes them).
