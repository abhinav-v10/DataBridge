import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import connections
from app.connectors.registry import supported_types
from app.core.config import get_settings
from app.core.db import init_engine
from app.core.security import SecretKeyError

logger = logging.getLogger("databridge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine()
    yield


app = FastAPI(title="DataBridge", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=get_settings().cors_origins, allow_methods=["*"], allow_headers=["*"]
)
app.include_router(connections.router, prefix="/api")


@app.exception_handler(SecretKeyError)
async def secret_key_error_handler(request: Request, exc: SecretKeyError):
    # Fixed, safe message: never echo the exception text, key material or decrypted values.
    # Log only the route and that a secret problem occurred (no traceback, no details).
    logger.error("Secret key problem while handling %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Stored credentials cannot be used: the server's encryption key is missing, invalid, "
            "or does not match the key the credentials were saved with. Ask an administrator to check "
            "DATABRIDGE_SECRET_KEY, or re-create the connection."
        },
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "supported_db_types": supported_types()}
