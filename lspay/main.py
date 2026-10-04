import asyncio
import hmac
import logging
import pathlib
import secrets
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import callbacks, db, services, telegram
from .api.merchant import router as merchant_router
from .config import settings
from .web.auth import LoginRequired
from .web.routes import router as web_router

log = logging.getLogger("lspay")


def sweep_once() -> None:
    """Time out stale deposits and send due merchant callbacks."""
    with db.session_scope() as s:
        expired = services.expire_receive_orders(s)
    for oid in expired:
        telegram.notify_safe(oid)
    s = db.SessionLocal()
    try:
        callbacks.process_due(s)
    finally:
        s.close()


async def sweeper() -> None:
    while True:
        try:
            await run_in_threadpool(sweep_once)
        except Exception:
            log.exception("sweep failed")
        await asyncio.sleep(settings.sweep_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = asyncio.create_task(sweeper()) if settings.sweep_interval_seconds > 0 else None
    yield
    if task:
        task.cancel()


def create_app() -> FastAPI:
    if not settings.session_secret:
        log.warning("SESSION_SECRET is not set; using a random one (sessions reset on restart)")
    app = FastAPI(title="LSPay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret or secrets.token_hex(32),
        session_cookie="lspay_session",
        same_site="strict",
        https_only=settings.cookie_secure,
        max_age=12 * 3600,
    )
    static_dir = pathlib.Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(merchant_router)
    app.include_router(web_router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/admin/login", status_code=303)

    @app.get("/")
    def root():
        return RedirectResponse("/admin", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request, tasks: BackgroundTasks):
        secret = settings.telegram_webhook_secret
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret or not hmac.compare_digest(sent, secret):
            return JSONResponse({"ok": False}, status_code=403)
        update = await request.json()

        def work():
            s = db.SessionLocal()
            try:
                changed = telegram.handle_update(s, update)
            finally:
                s.close()
            for oid in changed:
                telegram.notify_safe(oid)

        tasks.add_task(work)
        return {"ok": True}

    return app


app = create_app()
