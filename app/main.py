from fastapi import FastAPI

app = FastAPI(title="KIS Video Service")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "kis-video"}


# Routers land here as each workstream builds them:
# from app.api import uploads, jobs, playback
# app.include_router(uploads.router)
# app.include_router(jobs.router)
# app.include_router(playback.router)
