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

**Resource sizing**: `docker-compose.prod.yml`'s `worker` memory limit
(512M) is grounded in a real measurement, not a guess. dev-b0 wrapped the
actual ffmpeg binary in `/usr/bin/time -l` (macOS's peak-RSS reporter)
against the real pipeline's fixed libx264/veryfast settings, on a 3s
1280x720 test clip:

| stage | peak RSS |
|---|---|
| 720p rendition | 375.7 MB |
| 480p rendition | 239.6 MB |
| 360p rendition | 185.9 MB |
| thumbnail (`-frames:v 1`) | 82.8 MB |
| worker Python process (parent) | ~9 MB, stable |

Renditions run sequentially inside one task (never concurrently), so peak
worker memory is baseline + the single highest-resolution rendition in
flight - about 386 MB for a source capped at 720p. 512M leaves roughly
30% headroom over that.

Caveats that keep this an estimate, not a guarantee, on the actual
Lightsail target:
- Measured on Apple Silicon macOS ffmpeg 9.0.1 (NEON/DotProd/I8MM), not
  the target's CPU/OS/ffmpeg build. x264 memory is driven far more by
  resolution/encoder settings than CPU architecture, so this should
  transfer reasonably well, but hasn't been confirmed on that hardware.
- Only tested a 3-second source. x264's memory footprint is frame-buffer-
  bound, not duration-bound, so a multi-minute upload shouldn't change
  this materially - but that assumption itself hasn't been verified
  against a real multi-minute file.
- Assumes source video tops out at 720p. If uploads can be 1080p+, the
  decode + encode footprint will scale up with the source's own pixel
  count (roughly proportional, unmeasured) - revisit the limit before
  allowing higher-resolution sources.

**Known issue (non-fatal)**: the thumbnail ffmpeg command
(`-frames:v 1 -vf scale=320:-1 <file>.jpg`, no `-update`) prints a benign
"does not contain an image sequence pattern" warning on newer ffmpeg
versions. Confirmed non-fatal today - the thumbnail is still written
correctly - but add `-update 1` to silence it and avoid it becoming a
hard error on a future ffmpeg version.

**Not yet done** (tracked, not silently skipped): this service has never
run end-to-end against a real S3 bucket, only unit-tested per-component,
reviewed, and (as of the smoke test in `docs/DEPLOYMENT.md`'s history)
verified for the upload -> staging -> queue handoff across the real
container boundary. Real ffmpeg transcode correctness and memory have
now been measured directly (see above); full real-S3 upload/serve of the
transcoded output is the next real milestone before this is genuinely
production-ready end to end.
