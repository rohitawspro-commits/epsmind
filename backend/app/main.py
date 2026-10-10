"""
OpsMind API entrypoint.

If you already have a main.py, you only need the two `include_router` /
middleware lines: the pre-flight endpoints live in app/api/preflight.py.
"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.preflight import router as preflight_router

app = FastAPI(title="OpsMind API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("OPSMIND_CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# POST /api/inspect/frontend  and  POST /api/autofix/inject
app.include_router(preflight_router)


@app.get("/healthz", tags=["system"])
def healthz() -> dict:
    return {"status": "ok"}
