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
