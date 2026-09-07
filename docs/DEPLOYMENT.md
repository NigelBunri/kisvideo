# KIS Video — Deployment & Operations Guide

Mirrors the structure of the KIS AWS/Lightsail ops runbook (Django/Nest)
so the same muscle memory applies here — same golden rules, same
build → tag → push → pull → recreate → health-check shape, adapted for
this service's api/worker/beat trio and its lean, shared-infra setup.

**Target:** the same Lightsail box as Django/Nest/chat (`/opt/kis`) — not
separate infra. This service does **not** run its own Postgres or Redis;
it reuses the existing `kis-postgres` (a dedicated `kis_video` database
inside that same instance) and `kis-redis` (DB index 1, not the shared
default 0). See `docker-compose.prod.yml`'s own header comment for the
full reasoning.

**Images:** `ghcr.io/nigelbunri/kis-video-api`, `ghcr.io/nigelbunri/kis-video-worker`,
`ghcr.io/nigelbunri/kis-video-beat` — three tags built from the *same*
image (api/worker/beat share one Dockerfile, differing only in `command:`,
see README.md), pushed under three names so each service can be recreated
independently with `--no-deps` the same way Django/Nest already are.

**Rollback tag format:** `YYYY-MM-DD-HHMM`, same convention as
kis-django/kis-nest.

---

## Golden safety rules

Identical to the Django/Nest runbook - repeated here, not assumed:

- Always run `git status` before pulling, building, committing, or pushing.
- Never commit or paste `.env` files, passwords, internal tokens, AWS
  credentials, or database passwords.
- Back up `.env.production` before editing it
  (`cp .env.production .env.production.before-edit.$(date +%F-%H%M%S)`).
- Use `--no-deps` when recreating a single service, to avoid restarting
  the shared kis-postgres/kis-redis containers this service doesn't own.
- Run public health checks after every restart.
- Do not delete the `kis_video_uploads` volume, or the `kis_video`
  database inside the shared kis-postgres instance, during routine
  cleanup — a video mid-transcode or a not-yet-expired upload session
  lives there.

**One thing this service adds beyond the Django/Nest rules:** because it
shares `kis-postgres`/`kis-redis` rather than owning them, a
`docker compose down` here must **never** include `-v` (would not remove
the shared containers, since they're not part of this compose project -
but would remove `kis_video_uploads`, silently deleting every in-flight
upload's staged bytes) and must never touch `kis-postgres`/`kis-redis`
directly - this service's compose file doesn't define them, so it has no
way to accidentally stop/remove them, but a manual `docker stop
kis-postgres` while debugging something else on the box would also take
down Django/Nest/chat's database. Treat those two containers as belonging
to the whole server, not to this service.

---

## Daily health check

```
cd /opt/kis-video
docker compose -f docker-compose.prod.yml ps
docker stats --no-stream api worker beat   # this box is tight on RAM - worth a daily glance, not just at deploy time
curl -sS http://localhost:8010/health
```

Healthy baseline: all three of `api`/`worker`/`beat` running (`Up`), no
restart-looping. `api`'s health check doesn't touch the database, so a
`200 {"status":"ok",...}` only confirms the process is alive - it does
not confirm the database/Redis connection is good (there's no `/health`
DB check today - worth flagging as a possible follow-up, not something
this pass adds).

---

## Full deployment (all three services)

Use only after code is committed and pushed. Django/Nest build from a
Dockerfile checked out on the server itself; this service does the same.

```
cd /opt/kis-video
git status
git log --oneline -3
git pull origin main
git log --oneline -3
```

### Build once, tag three ways

All three services share one build context/Dockerfile (see README.md) -
build the image once, tag it three times under the per-service names so
each can be pulled/recreated independently.

```
cd /opt/kis-video
docker build -t kis-video:production .

DATE_TAG=$(date +%F-%H%M)
for name in api worker beat; do
  docker tag kis-video:production ghcr.io/nigelbunri/kis-video-$name:production
  docker tag kis-video:production ghcr.io/nigelbunri/kis-video-$name:$DATE_TAG
  docker push ghcr.io/nigelbunri/kis-video-$name:production
  docker push ghcr.io/nigelbunri/kis-video-$name:$DATE_TAG
done
echo "Rollback tag: $DATE_TAG"
```

### Deploy

No dependency ordering concern between api/worker/beat the way Django
must deploy before Nest - all three run the exact same code, so there's
no "which one depends on the other's new endpoint" question. Recreate all
three together:

```
cd /opt/kis-video
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d --force-recreate
```

### Migrate

Unlike Django, this service has no separate `manage.py migrate` - run
Alembic directly inside the recreated `api` container:

```
docker compose -f docker-compose.prod.yml exec api alembic upgrade head
```

### Validate

```
cd /opt/kis-video
docker compose -f docker-compose.prod.yml ps
curl -i http://localhost:8010/health
docker compose -f docker-compose.prod.yml logs api --tail=100
docker compose -f docker-compose.prod.yml logs worker --tail=100
docker compose -f docker-compose.prod.yml logs beat --tail=50
```

Confirm in the worker log: `Connected to redis://:**@kis-redis:6379/1`
(index **1**, not 0 - a wrong index here means this service's tasks are
silently mixing with Django/Nest's live queue, or going nowhere).

---

## Service-specific deployment (one of api/worker/beat only)

Same build-once-tag-three-ways step above, then recreate just the one
service with `--no-deps` (there's nothing this compose file manages for
it to avoid restarting, since kis-postgres/kis-redis aren't part of this
project - `--no-deps` here mainly guards against an accidental
`up -d --force-recreate` with no service name recreating all three):

```
cd /opt/kis-video
docker compose -f docker-compose.prod.yml pull worker
docker compose -f docker-compose.prod.yml up -d --force-recreate --no-deps worker
docker compose -f docker-compose.prod.yml logs worker --tail=100
```

---

## Rollback

Same shape as Django/Nest - GHCR is the durable rollback source, since
routine cleanup (below) removes old *local* tags without touching the
registry copies.

```
cd /opt/kis-video
ROLLBACK_TAG="YYYY-MM-DD-HHMM"
for name in api worker beat; do
  docker pull ghcr.io/nigelbunri/kis-video-$name:$ROLLBACK_TAG
  docker tag ghcr.io/nigelbunri/kis-video-$name:$ROLLBACK_TAG ghcr.io/nigelbunri/kis-video-$name:production
done
docker compose -f docker-compose.prod.yml up -d --force-recreate
docker compose -f docker-compose.prod.yml ps
curl -i http://localhost:8010/health
```

Do not blindly reverse an Alembic migration on rollback - review whether
the rolled-back code version is actually compatible with the current
schema first, same caution as the Django/Nest runbook's migration-rollback
warning.

---

## Weekly

```
cd /opt/kis-video
git status
git log --oneline -5
docker compose -f docker-compose.prod.yml logs api --since=168h 2>&1 | grep -iE 'error|exception|traceback' | tail -100
docker compose -f docker-compose.prod.yml logs worker --since=168h 2>&1 | grep -iE 'error|exception|traceback|failed' | tail -100
docker exec kis-postgres psql -U kis_user -d kis_video -c "SELECT status, count(*) FROM upload_sessions GROUP BY status;"
docker exec kis-postgres psql -U kis_user -d kis_video -c "SELECT status, count(*) FROM transcode_jobs GROUP BY status;"
```

Watch specifically for a growing count of `uploading` sessions well past
`UPLOAD_TTL_HOURS` (the hourly cleanup sweep should keep this near zero -
a growing backlog means the sweep itself is failing, not just that
uploads are being abandoned) and `failed` transcode jobs (check
`error_message` on those rows for a pattern - e.g. every failure being
the same codec/format issue would point at a real bug, not just bad
input).

---

## Monthly cleanup

This service doesn't manage its own Postgres/Redis, so there's no
separate data-cleanup step beyond what the shared kis-postgres/kis-redis
maintenance already covers (see the main KIS ops guide). Image/build-cache
cleanup is the same routine, same warnings:

```
docker builder prune --filter "until=168h"
docker image prune -a --filter "until=168h"
```

**Never** as routine cleanup: `docker system prune --volumes` (would
remove `kis_video_uploads`), or anything touching `kis-postgres`/
`kis-redis` directly - those are shared with Django/Nest/chat and outside
this service's ownership.

---

## Troubleshooting

**api returns 500/502:**
1. `docker compose -f docker-compose.prod.yml ps` — healthy or restarting?
2. `docker compose -f docker-compose.prod.yml logs api --tail=200`
3. Confirm `DATABASE_URL`/`REDIS_URL` point at `kis-postgres`/`kis-redis`
   (container names, not `localhost` - a `.env.production` copied from
   local dev and not edited is the most likely cause of this specific
   failure).

**Uploads complete but nothing ever transcodes:**
1. Check `worker` is actually running and connected:
   `docker compose -f docker-compose.prod.yml logs worker --tail=50` -
   look for `Connected to redis://:**@kis-redis:6379/1`.
2. Check `beat` is running - without it, nothing schedules the periodic
   cleanup, but *manually*-triggered transcode jobs (every real upload)
   don't depend on beat at all, so this specifically points at `worker`
   being down or stuck, not `beat`.
3. `docker exec kis-postgres psql -U kis_user -d kis_video -c "SELECT id, status, error_message FROM transcode_jobs ORDER BY created_at DESC LIMIT 5;"`
   - `queued` forever means the message never reached a live worker
   (check Redis DB index again); a `failed` row has `error_message` set.

**Worker OOM-killed / restarting under load:**
See `README.md`'s "Resource sizing" section - the memory limit on
`worker` is an estimate, not yet validated against a real multi-minute
video on the actual target hardware. If this happens, that's the first
place to look, not a code bug by default.

**SSH disconnects mid-command:** same as Django/Nest - "Connection reset
by peer" usually just means the SSH session ended, not that the service
stopped. Reconnect and re-run the daily health check.
