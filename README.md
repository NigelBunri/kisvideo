# KIS Video

Self-hosted video processing microservice — resumable upload + adaptive-bitrate
HLS transcoding, replacing Mux's VOD API for KISTube.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for scope, design decisions, and
why this exists.

## Local dev

```
cp .env.example .env   # fill in real values
docker compose up --build
```

API: http://localhost:8010/health

## Production deployment

```
cp .env.production.example .env.production   # fill in real values, see that file's own comments
docker compose -f docker-compose.prod.yml up -d --build
```

`docker-compose.prod.yml` is a separate, standalone file (not a dev
override) - restart policies, a named volume for the shared upload
staging directory, no bind-mounted source, no exposed postgres/redis
ports, and healthcheck-gated startup ordering. See that file's own header
comment for the full list of differences from the dev compose file and
why each exists.

**`INTERNAL_TOKEN`**: generate with `openssl rand -base64 48`. Same trust
model and rotation posture as `DJANGO_INTERNAL_TOKEN` in the KIS ops
runbook - every caller (Django, Nest) needs the exact same value
configured on their own side, and rotating it means updating this service
and every caller together, not independently.

**One image, three commands**: `api`, `worker`, and `beat` all build from
the same `Dockerfile`/build context and differ only in their `command:` -
deliberate, not an oversight. All three run the exact same `app/` package
(shared models, shared ffmpeg/S3 code, shared settings); the only
difference between "serve HTTP" and "process a queued job" is which
process the container runs, the same pattern most Django+Celery or
FastAPI+Celery services use rather than maintaining two near-identical
images.

**Not yet done** (tracked, not silently skipped): this service has never
run end-to-end against real ffmpeg + a real S3 bucket, only unit-tested
per-component and reviewed. That's the next real milestone before this
is genuinely production-ready, not just "the code exists and compiles."
