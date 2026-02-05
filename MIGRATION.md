# Migration Guide (kzpo-report-bot)

This guide prepares a clean migration so a new operator can attach a domain, change bot API endpoints in `.env`, and deploy.

## What to copy
1. Project directory `kzpo-report-bot/`.
2. `.env` from the current server (keep it private).
3. `.secrets/` if used.
4. Database dump file (recommended: `eniq_dump.sql` or a fresh `pg_dump`).

## Prerequisites on the new server
1. Docker Engine and Docker Compose v2.
2. Public DNS A record pointing the domain to the new server.
3. If using Traefik, a public docker network already created (example: `traefik`).

## Step 1. Transfer code and config
Example layout on the new server:
- `/opt/kzpo-report-bot/` (project)
- `/opt/kzpo-report-bot/.env` (env vars)
- `/opt/kzpo-report-bot/eniq_dump.sql` (database dump)

## Step 2. Update `.env`
Required keys to review:
- `ENIQ_BASE_URL` (change bot API base URL)
- `ENIQ_TOKEN`
- `ENIQ_PROJECT_ID`
- `DATABASE_URL` (should point to container `db:5432` when using docker-compose)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_IDS`
- `RINGOSTAT_TOKEN`
- `RINGOSTAT_PROJECT_ID`
- `RINGOSTAT_WEBHOOK_USER`
- `RINGOSTAT_WEBHOOK_PASSWORD`
- `RINGOSTAT_WEBHOOK_PORT` (default `8080`)

Optional keys:
- `OPENAI_API_KEY`
- `GOOGLE_SHEETS_*`
- `REPORT_TZ`

## Step 3. Configure domain routing
Two supported patterns are documented below. Choose one.

### Option A. Traefik
1. Copy `deploy.env.example` to `deploy.env` and set:
   - `PUBLIC_DOMAIN` (domain for webhook routes)
   - `TRAEFIK_PUBLIC_NETWORK` (docker network used by Traefik)
   - `TRAEFIK_ENTRYPOINT` and `TRAEFIK_RESOLVER` (if TLS is enabled)
2. Use `docker-compose.deploy.yml` for deployment.

### Option B. Nginx (no Traefik)
1. Map port `8080` from the container to the host (already in compose file).
2. Use `nginx.example.conf` as a starting point and proxy `/webhooks/ringostat/` to `http://127.0.0.1:8080`.
3. Ensure HTTPS termination on the proxy.

## Step 4. Deploy
Run from the project directory on the new server:

```bash
cp deploy.env.example deploy.env
# Edit deploy.env and .env

docker compose --env-file deploy.env -f docker-compose.deploy.yml up -d --build
```

## Step 5. Restore database
If you have a dump file:

```bash
docker compose --env-file deploy.env -f docker-compose.deploy.yml exec -T db psql -U eniq -d eniq < eniq_dump.sql
```

## Step 6. Verify
1. Check containers:
   ```bash
   docker compose --env-file deploy.env -f docker-compose.deploy.yml ps
   ```
2. Health endpoint (through domain):
   - `GET https://<domain>/webhooks/ringostat/health`
3. Telegram bot should respond to `/start`.

## Notes
- Webhook endpoints are:
  - `POST /webhooks/ringostat/after_call`
  - `POST /webhooks/ringostat/answer_call`
- The webhook server runs inside the bot process. If the bot is down, webhooks are down too.
