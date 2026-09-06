# KIS Video Service — Architecture & Scope (v1)

Self-hosted replacement for Mux's VOD API: resumable upload + adaptive-bitrate
HLS transcoding. Standalone microservice, its own repo/deploy, called by
Django and Nest over an internal HTTP API — the same trust model already
used between Django and Nest (`DJANGO_INTERNAL_TOKEN`).

**Not in v1 scope** (explicitly deferred, per Nigel's decision 2026-09-06):
live streaming ingest/transcode (stays on Mux), DRM, analytics dashboards,
multi-region delivery. This service ONLY replaces what Mux does for
*uploaded* video today: turn a file into a playable adaptive-bitrate stream.

## Why this exists

Confirmed by reading the actual code today (not assumed):

- `backend/kis/apps/broadcasts/media_pipeline.py`: `configured_transcode_provider()`
  defaults to `"local_stub"` — there has never been a real transcoding
  pipeline for VOD. `ProcessingJob` (pipeline="transcode") exists as a model
  but nothing processes it into real renditions.
- `backend/Nestjs/src/uploads/upload-intent.service.ts`: today's upload is a
  single presigned S3 PUT. One HTTP request, the whole file, no chunking —
  a dropped connection on a large video means starting over from byte 0.
- `backend/kis/apps/broadcasts/media_utils.py` already has real, working
  ffmpeg + Pillow code (thumbnail generation, perceptual video hashing) —
  proof the ffmpeg-subprocess pattern this service needs is proven in this
  codebase already, just not wired to a transcoding pipeline.

## Tech stack (confirmed with Nigel)

- **FastAPI** — the HTTP API (upload endpoints, job status, playback info).
- **Celery + Redis** — the transcoding job queue. Same tool the Django
  backend already runs in production (`kis-celery-worker`/`kis-celery-beat`
  containers, confirmed running on Lightsail today) — operational knowledge
  transfers directly.
- **Postgres** — job/asset state (own database, not shared with Django's).
- **ffmpeg** — the actual transcoding engine, invoked as a subprocess from
  Celery workers (same pattern as `media_utils.py`'s existing thumbnail code).
- **S3** — rendition storage (same bucket family the rest of KIS already
  uses — reuse the existing AWS credentials/bucket naming convention,
  don't invent a new one).

## The two v1 capabilities

### 1. Resumable upload

Implements the **tus resumable upload protocol** (tus.io) rather than a
custom chunking scheme — it's the open standard specifically for this
problem (a client can pause/resume/survive a dropped connection mid-upload
without re-sending already-received bytes), has mature reference server
implementations in Python to build from, and there's no reason to
reinvent it.

- `POST /uploads` — create an upload (declares total size, gets an upload URL).
- `PATCH /uploads/{id}` — append a chunk at a given offset (the core resume
  primitive — client always knows how many bytes the server has via `HEAD`).
- `HEAD /uploads/{id}` — client asks "how much do you have so far."
- On completion: the assembled file lands in a staging area, a
  `TranscodeJob` row is created, and a Celery task is queued.

### 2. Transcoding pipeline (Celery task chain)

1. **Probe** — `ffprobe` the input (duration, resolution, codec) — mirrors
   `media_utils.py`'s existing `_probe_video_duration`.
2. **Transcode renditions** — ffmpeg produces a bitrate ladder (e.g. 1080p/
   720p/480p/360p, only including renditions ≤ source resolution) as HLS
   segments (`.m3u8` + fMP4 or `.ts` segments per rendition).
3. **Master playlist** — a top-level `.m3u8` referencing all renditions,
   the same shape `HlsVideo.tsx` (kistube-website) already expects — so
   the *client* needs zero changes once this service's playback URL is
   substituted for Mux's.
4. **Thumbnail + storyboard** — reuse `media_utils.py`'s existing
   `_create_thumbnail` approach for the poster image; a storyboard/sprite
   sheet (scrubbing preview) is new work, lower priority within v1.
5. **Upload renditions to S3**, then **notify the caller** (Django) via an
   internal webhook — mirrors how `notifyIncomingCall`-style
   internal-service-to-service calls already work between Nest and Django.
6. Job status (`queued` → `transcoding` → `ready`/`failed`) is queryable via
   `GET /jobs/{id}` for polling, in addition to the webhook push.

## What Django/Nest need to change (separate, later workstream)

Once this service's API is stable: `ChannelContentAssetUploadView` (Django)
and kistube-website's `HlsVideo.tsx` need to point at this service's
playback URLs instead of Mux's for VOD content specifically (live streaming
stays on Mux — out of scope, don't touch `live_stream_providers.py`'s
MuxProvider). Not part of the initial build — sequenced after the service
itself has a working, tested API.

## Repo layout

```
kis-video/
  app/
    api/          — FastAPI routers (uploads, jobs, playback)
    workers/       — Celery tasks (probe, transcode, thumbnail, notify)
    models/        — SQLAlchemy models (UploadSession, TranscodeJob, Asset)
    storage/       — S3 client wrapper
    config/        — settings (env-driven, mirrors Django's settings pattern)
  tests/
  Dockerfile
  docker-compose.yml   — api + worker + postgres + redis, for local dev
  requirements.txt
```
