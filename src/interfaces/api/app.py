"""FastAPI application factory.

Wires middleware, routers, error handlers, and lifespan. On startup it runs the
resume sweep so any document left mid-ingestion by a previous (slept/killed)
process is picked back up.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from src.application.use_cases.appointment_reminders import SendAppointmentReminders
from src.application.use_cases.appointments import ExpireSlotHolds
from src.application.use_cases.follow_up import SendFollowUps
from src.application.use_cases.ingest_document import IngestDocument, ResumePendingIngestions
from src.config.container import get_container
from src.config.settings import get_settings
from src.hiring_agent.routes import router as hiring_router
from src.infrastructure.http_client import close_clients
from src.infrastructure.observability.logging import configure_logging
from src.infrastructure.persistence.database import dispose_engine, get_engine
from src.interfaces.api.errors import register_error_handlers
from src.interfaces.api.middleware import ObservabilityMiddleware, PublicCorsMiddleware
from src.interfaces.api.routers import (
    agent,
    analytics,
    api_keys,
    appointments,
    auth,
    auth_google,
    availability,
    billing,
    broadcasts,
    candidates,
    chat,
    chatbots,
    documents,
    health,
    integrations,
    integrations_catalogue,
    interview_batches,
    interviews,
    locations,
    oauth,
    onboarding,
    post_call,
    public,
    resources,
    services,
    support,
    team,
    uploads,
    voices,
    whatsapp,
    whatsapp_cloud,
    whatsapp_web,
)

_WIDGET_JS = Path(__file__).parent / "static" / "widget.js"
# Built single-page app (admin UI + the public /c/<key> share page). Present in
# production images (the Docker build runs `vite build`); absent in local dev,
# where Vite serves the SPA itself. Override with FRONTEND_DIST_DIR if needed.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND_DIST = Path(
    get_settings().frontend_dist_dir or (_REPO_ROOT / "frontend" / "dist")
)

# Paths owned by the API/ops/widget — never served the SPA fallback.
_NON_SPA_PREFIXES = (
    "api/",
    "healthz",
    "readyz",
    "metrics",
    "widget.js",
    "docs",
    "redoc",
    "openapi.json",
)


def _mount_spa(app: FastAPI) -> None:
    """Serve the built SPA from the API origin so the share page (/c/<key>),
    the admin app, the widget script, and the API all live on ONE domain — which
    is what makes a deployed link work on any visitor's machine. Unknown paths
    fall back to index.html so client-side routing survives a hard refresh."""
    if not _FRONTEND_DIST.is_dir():
        log.info("spa.not_bundled", dist=str(_FRONTEND_DIST))
        return

    assets = _FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    index = _FRONTEND_DIST / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        if full_path.startswith(_NON_SPA_PREFIXES):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = _FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    log.info("spa.mounted", dist=str(_FRONTEND_DIST))

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings = get_settings()
    configure_logging(settings)
    container = get_container()

    # Resume any ingestion interrupted by a previous process shutdown/sleep.
    try:
        ingest = IngestDocument(
            container.unit_of_work(),
            container.storage,
            container.parser,
            container.chunker,
            container.embedder,
            container.llm,
        )
        resumed = await ResumePendingIngestions(container.unit_of_work(), ingest).execute()
        if resumed:
            log.info("startup.resumed_ingestions", count=resumed)
    except Exception:  # noqa: BLE001 - never block startup on the resume sweep
        log.exception("startup.resume_failed")

    # Nudges contacts who have gone quiet. A task rather than a cron job or an
    # external worker: this deployment is a single process, and the schedule
    # itself lives in Postgres, so the loop is only a clock — losing it to a
    # restart delays follow-ups until the next boot rather than dropping them.
    follow_ups = None
    if settings.follow_ups_enabled:
        follow_ups = asyncio.create_task(_follow_up_loop(settings))

    # Housekeeping for abandoned slot holds. Unlike the follow-up sweep this
    # takes no advisory lock and needs none: releasing an already-expired hold
    # is idempotent, so two workers doing it at once reach the same state.
    #
    # It is also not what makes booking correct — the booking path purges
    # expired holds on the resources it is about to claim, inside its own
    # transaction. Losing this loop to a restart delays tidying, never a booking.
    hold_expiry = None
    if settings.appointments_enabled:
        hold_expiry = asyncio.create_task(_hold_expiry_loop(settings))

    # Tells people about the appointment they are about to have. Same shape as
    # the follow-up sweep and for the same reasons — including its advisory
    # lock, because a duplicate reminder to a real customer is exactly the
    # failure that lock exists to prevent.
    reminders = None
    if settings.appointments_enabled and settings.appointment_reminders_enabled:
        reminders = asyncio.create_task(_reminder_loop(settings))

    # Exercise the failover chain once, so a retired model name is a loud line
    # in the boot log rather than a surprise during the outage it exists to
    # absorb. Three trivial completions; skipped with LLM_PROBE_ON_STARTUP=false.
    if settings.llm_probe_on_startup:
        try:
            probe = await container.llm.probe()
            dead = [n for n, r in probe.items() if not r.get("ok")]
            for name in dead:
                log.error(
                    "llm.provider_unreachable",
                    provider=name,
                    model=probe[name].get("model"),
                    error=probe[name].get("error"),
                )
            log.info(
                "llm.chain_probed",
                chain=list(probe),
                healthy=[n for n in probe if n not in dead],
                dead=dead,
            )
        except Exception:  # noqa: BLE001 - a diagnostic must never block startup
            log.exception("llm.probe_failed")

    log.info("app.started", env=settings.app_env)
    yield

    for task in (follow_ups, hold_expiry, reminders):
        if task is None:
            continue
        task.cancel()
        # Awaited so the task is actually finished before the engine is
        # disposed underneath it, rather than logging a connection error on
        # the way out.
        with suppress(asyncio.CancelledError):
            await task

    # Flush buffered telemetry before the engine/process goes away.
    try:
        await container.tracer.flush()
    except Exception:  # noqa: BLE001 - shutdown best-effort
        log.exception("shutdown.tracer_flush_failed")
    # Release the shared outbound connection pools before the loop closes,
    # so a redeploy doesn't leave sockets in TIME_WAIT on the way out.
    await close_clients()
    await dispose_engine()
    log.info("app.stopped")


# Namespaces the advisory lock below. Any constant works as long as nothing
# else in the database picks the same one; this is a fixed arbitrary value so
# every process agrees on it without coordination.
_FOLLOW_UP_LOCK_KEY = 0x5241475F_46555030  # "RAG_FUP0"
# A different key, so a reminder tick and a follow-up tick can run at the same
# time. They touch different tables and neither blocks the other.
_REMINDER_LOCK_KEY = 0x5241475F_52454D30  # "RAG_REM0"


@asynccontextmanager
async def _sweep_lease(key: int = _FOLLOW_UP_LOCK_KEY):  # type: ignore[no-untyped-def]
    """Yield True to exactly one process at a time, across the whole cluster.

    The follow-up sweep reads the due conversations, then sends to each one in
    a later transaction. Two processes ticking together would therefore both
    read the same rows and both send — a duplicate nudge to a real contact,
    which is the one thing this feature must never do. That is what pinned the
    deployment to a single web process.

    A Postgres session-scoped advisory lock removes that constraint: whoever
    takes it sweeps, everyone else skips this tick and tries again on the next
    one. It costs no schema, survives a crash (the lock dies with the
    connection), and leaves the sweep's retry semantics untouched — unlike
    reserving rows up front, which would turn a failed send into a delayed
    retry instead of an immediate one.

    The lock is held on one dedicated connection for the whole tick, not on a
    pooled unit-of-work session, because the sweep spans several transactions
    and a pooled connection would be handed back — releasing the lock — between
    them.
    """
    engine = get_engine()
    conn = await engine.connect()
    try:
        result = await conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
        )
        got = bool(result.scalar())
        try:
            yield got
        finally:
            if got:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": key}
                )
    finally:
        await conn.close()


async def _follow_up_loop(settings) -> None:
    """Run the follow-up sweep forever, on a fixed interval.

    Every failure is swallowed and retried on the next tick: a sweep that
    raises (a database blip, a bridge restart) must not kill the loop and
    silently end follow-ups for the life of the process.
    """
    container = get_container()
    while True:
        await asyncio.sleep(settings.follow_up_sweep_seconds)
        try:
            async with _sweep_lease() as leader:
                if not leader:
                    # Another worker is sweeping this tick. Not an error — this
                    # is how the loop stays correct with several processes.
                    continue
                sent = await SendFollowUps(
                    container.unit_of_work(),
                    bridge=container.whatsapp_bridge,
                    whatsapp_sender=container.whatsapp_sender,
                    after=timedelta(minutes=settings.follow_up_after_minutes),
                    max_follow_ups=settings.max_follow_ups,
                ).execute()
            if sent:
                log.info("followup.sweep", sent=sent)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad sweep must not end the loop
            log.exception("followup.sweep_failed")


async def _reminder_loop(settings) -> None:  # type: ignore[no-untyped-def]
    """Run the appointment reminder sweep forever, on a fixed interval.

    Every failure is swallowed and retried on the next tick, for the same
    reason the follow-up loop swallows its own: a sweep that raises must not
    kill the loop and silently end reminders for the life of the process.
    """
    container = get_container()
    lead = timedelta(minutes=settings.appointment_reminder_minutes)
    while True:
        await asyncio.sleep(settings.appointment_reminder_sweep_seconds)
        try:
            async with _sweep_lease(_REMINDER_LOCK_KEY) as leader:
                if not leader:
                    # Another worker is sweeping this tick. Not an error — it is
                    # how the loop stays correct with several processes.
                    continue
                sent = await SendAppointmentReminders(
                    container.unit_of_work(),
                    bridge=container.whatsapp_bridge,
                    whatsapp_sender=container.whatsapp_sender,
                    lead=lead,
                ).execute()
            if sent:
                log.info("appointment.reminder.sweep", sent=sent)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad sweep must not end the loop
            log.exception("appointment.reminder.sweep_failed")


async def _hold_expiry_loop(settings) -> None:  # type: ignore[no-untyped-def]
    """Release slot holds nobody converted, forever, on a fixed interval.

    Every failure is swallowed and retried on the next tick, for the same reason
    the follow-up sweep does it: a database blip must not silently end
    housekeeping for the life of the process.
    """
    container = get_container()
    interval = max(30, settings.slot_hold_ttl_minutes * 30)
    while True:
        await asyncio.sleep(interval)
        try:
            await ExpireSlotHolds(container.unit_of_work()).execute()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad sweep must not end the loop
            log.exception("scheduling.hold_expiry_failed")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="RAG Platform",
        version="0.1.0",
        description="Multi-tenant RAG — upload documents, get a working AI chatbot.",
        lifespan=lifespan,
        debug=settings.app_debug,
    )

    # Reflective, credential-free CORS for the public widget API + /widget.js.
    # Added before the authenticated CORS layer so it owns those paths; the
    # standard CORSMiddleware governs the credentialed admin/API surface.
    app.add_middleware(PublicCorsMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(ObservabilityMiddleware)
    register_error_handlers(app)

    # Ops endpoints at root; everything else under /api/v1.
    app.include_router(health.router)

    @app.get("/widget.js", include_in_schema=False)
    async def widget_js() -> FileResponse:
        """Serve the embeddable widget script. Cached at the edge for an hour;
        the script itself is keyless — it reads its chatbot key from its tag."""
        return FileResponse(
            _WIDGET_JS,
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    api_prefix = "/api/v1"
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(auth_google.router, prefix=api_prefix)
    app.include_router(documents.router, prefix=api_prefix)
    app.include_router(chatbots.router, prefix=api_prefix)
    app.include_router(analytics.router, prefix=api_prefix)
    app.include_router(chat.router, prefix=api_prefix)
    app.include_router(agent.router, prefix=api_prefix)
    app.include_router(public.router, prefix=api_prefix)
    app.include_router(uploads.router, prefix=api_prefix)
    app.include_router(interviews.router, prefix=api_prefix)
    app.include_router(interview_batches.router, prefix=api_prefix)
    app.include_router(team.router, prefix=api_prefix)
    app.include_router(billing.router, prefix=api_prefix)
    app.include_router(api_keys.router, prefix=api_prefix)
    app.include_router(integrations.router, prefix=api_prefix)
    app.include_router(whatsapp.router, prefix=api_prefix)
    # Shares the /chatbots prefix with `chatbots` — registered after it so the
    # generic /{chatbot_id} route can't shadow /{chatbot_id}/post-call.
    app.include_router(post_call.router, prefix=api_prefix)
    app.include_router(broadcasts.router, prefix=api_prefix)
    app.include_router(candidates.router, prefix=api_prefix)
    app.include_router(integrations_catalogue.router, prefix=api_prefix)
    app.include_router(oauth.router, prefix=api_prefix)
    app.include_router(support.router, prefix=api_prefix)
    app.include_router(onboarding.router, prefix=api_prefix)
    app.include_router(voices.router, prefix=api_prefix)
    app.include_router(whatsapp_web.router, prefix=api_prefix)
    # Meta WhatsApp Cloud API. Registered after `whatsapp` so the more
    # specific /whatsapp/cloud/* paths are matched before that router's
    # generic ones.
    app.include_router(whatsapp_cloud.router, prefix=api_prefix)
    # Scheduling. Behind a flag so the module can be rolled out gradually
    # (spec section 64); on by default, and invisible to a tenant that has
    # configured no locations or services.
    if settings.appointments_enabled:
        app.include_router(locations.router, prefix=api_prefix)
        app.include_router(services.router, prefix=api_prefix)
        app.include_router(resources.router, prefix=api_prefix)
        app.include_router(availability.router, prefix=api_prefix)
        app.include_router(appointments.router, prefix=api_prefix)

    if settings.hiring_agent_enabled:
        app.include_router(hiring_router, prefix=api_prefix)

    # SPA catch-all is mounted LAST so it never shadows the API/ops/widget routes.
    _mount_spa(app)

    return app


app = create_app()
