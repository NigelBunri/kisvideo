from fastapi import FastAPI

from app.api import jobs, playback, uploads

app = FastAPI(title="KIS Video Service")
app.include_router(uploads.router)
app.include_router(jobs.router)
app.include_router(playback.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "kis-video"}
