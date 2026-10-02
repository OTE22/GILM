from __future__ import annotations

import asyncio
import hmac
import ipaddress
import ssl
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

import httpx
from anyio import CancelScope
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .adapter import AdapterError, Forbidden, ReportingAdapter
from .config import Principal, Settings
from .engine import Engine
from .evaluation import Evaluator
from .models import Candidate, ChatRequest, LiveEvaluation
from .models import ReportRequest as ReportingRequest
from .providers import HTTPProvider, MockProvider, ProviderError
from .store import NotFound, PlanConflict, Store


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class ClosingStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


def error_body(request_id, code, message, details=None):
    return {"error": {"code": code, "message": message, "request_id": request_id, "details": details}}


class BoundaryMiddleware:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = "req_" + uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        headers = dict(scope.get("headers", []))

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", rid.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'",
                    ),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)

        body = bytearray()
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            try:
                length = int(headers.get(b"content-length", b"0"))
            except ValueError:
                length = -1
            if length < 0 or length > self.settings.max_body_bytes:
                response = JSONResponse(
                    error_body(rid, "payload_too_large", "Request exceeds payload limit"), status_code=413
                )
                return await response(scope, receive, secure_send)
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.settings.max_body_bytes:
                    response = JSONResponse(
                        error_body(rid, "payload_too_large", "Request exceeds payload limit"), status_code=413
                    )
                    return await response(scope, receive, secure_send)
                if not message.get("more_body", False):
                    break
            if body and headers.get(b"content-type", b"").split(b";", 1)[0] != b"application/json":
                response = JSONResponse(
                    error_body(rid, "unsupported_media_type", "Use application/json"), status_code=415
                )
                return await response(scope, receive, secure_send)
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if scope["method"] in {"POST", "PUT", "PATCH"} and not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, secure_send)


async def cancellable(request, operation):
    task = asyncio.create_task(operation)
    returned = False
    try:
        while not task.done():
            if await request.is_disconnected():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise APIError(499, "client_disconnected", "Request cancelled before completion")
            await asyncio.sleep(0.01)
        result = await task
        returned = True
        return result
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if not returned and task.done() and not task.cancelled() and task.exception() is None:
            result = task.result()
            if hasattr(result, "aclose"):
                with CancelScope(shield=True):
                    await result.aclose()


def create_app(settings=None, providers=None, http_client=None):
    settings = settings or Settings.from_env()
    store = Store(
        settings.data_dir / "state.sqlite",
        settings.default_provider,
        settings.http_models[0] if settings.default_provider == "http" else "mock-report-v1",
    )
    adapter = ReportingAdapter(settings.data_dir / "reporting.sqlite")
    owns_client = http_client is None
    verification = ssl.create_default_context(cafile=str(settings.http_ca_file)) if settings.http_ca_file else True
    http_client = http_client or httpx.AsyncClient(
        trust_env=False, verify=verification, limits=httpx.Limits(max_connections=20)
    )
    provider_registry = {"mock": MockProvider(), "http": HTTPProvider(settings, http_client)}
    provider_registry.update(providers or {})
    engine = Engine(settings, store, adapter, provider_registry)
    evaluator = Evaluator(engine)

    @asynccontextmanager
    async def lifespan(app):
        store.prune(settings.retention_days)
        try:
            yield
        finally:
            if owns_client:
                await http_client.aclose()

    app = FastAPI(title="GILM", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.evaluator = evaluator
    app.add_middleware(BoundaryMiddleware, settings=settings)

    async def principal(request: Request):
        origin = request.headers.get("origin")
        parsed_url = urlparse(str(request.url))
        if origin and origin != f"{parsed_url.scheme}://{parsed_url.netloc}":
            raise APIError(403, "origin_denied", "Cross-origin requests are not permitted")
        authorization = request.headers.get("authorization")
        if authorization:
            supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
            for secret, identity in settings.keys.items():
                if hmac.compare_digest(supplied.encode(), secret.encode()):
                    return identity
            raise APIError(401, "unauthorized", "Invalid API key")
        local = False
        if request.client:
            with suppress(ValueError):
                local = ipaddress.ip_address(request.client.host).is_loopback
        host = urlparse(str(request.url)).hostname
        if settings.dev_mode and local and host in {"127.0.0.1", "localhost", "::1"}:
            return Principal("demo", ("North", "South"), True)
        raise APIError(401, "unauthorized", "Bearer authentication is required")

    async def manager(identity: Principal = Depends(principal)):
        if not identity.can_manage:
            raise APIError(403, "management_denied", "Plan management permission required")
        return identity

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        # Never include Pydantic's input or exception context, which can contain prompts/secrets.
        details = [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
        return JSONResponse(
            error_body(request.state.request_id, "validation_error", "Unsupported or invalid request fields", details),
            status_code=422,
        )

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return JSONResponse(error_body(request.state.request_id, exc.code, exc.message), status_code=exc.status)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse(error_body(request.state.request_id, exc.code, str(exc)), status_code=exc.status)

    @app.exception_handler(NotFound)
    async def not_found(request, exc):
        return JSONResponse(error_body(request.state.request_id, "not_found", "Resource not found"), status_code=404)

    @app.exception_handler(Forbidden)
    async def forbidden(request, exc):
        return JSONResponse(
            error_body(request.state.request_id, "forbidden", "Authorization scope denied"), status_code=403
        )

    @app.exception_handler(PlanConflict)
    async def conflict(request, exc):
        return JSONResponse(error_body(request.state.request_id, "plan_conflict", str(exc)), status_code=409)

    @app.exception_handler(AdapterError)
    async def adapter_error(request, exc):
        return JSONResponse(error_body(request.state.request_id, "adapter_limit", str(exc)), status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return JSONResponse(
            error_body(request.state.request_id, "internal_error", "Internal request failure"), status_code=500
        )

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "default_provider": settings.default_provider,
            "dev_mode": settings.dev_mode,
            "openrouter_free_only": settings.openrouter_free_only,
        }

    @app.get("/ready")
    async def ready(identity=Depends(principal)):
        with store.connection() as db:
            db.execute("SELECT 1").fetchone()
        adapter.snapshot(identity)
        return {"status": "ready"}

    @app.post("/v1/chat/completions")
    async def completion(body: ChatRequest, request: Request, identity=Depends(principal)):
        if body.gilm.workflow != "chat":
            raise APIError(422, "adapter_required", "Use /api/reports/sales for the approved reporting adapter")
        if body.stream:
            iterator = await cancellable(request, engine.stream(identity, body, request.state.request_id))
            return ClosingStreamingResponse(
                iterator, media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
            )
        return await cancellable(request, engine.chat(identity, body, request.state.request_id))

    @app.post("/api/reports/sales")
    async def report(body: ReportingRequest, request: Request, identity=Depends(principal)):
        return await cancellable(request, engine.report(identity, body, request.state.request_id))

    @app.get("/api/plans")
    async def plans(workflow: str = "sales_report", identity=Depends(principal)):
        if workflow not in {"chat", "sales_report"}:
            raise APIError(422, "invalid_workflow", "Unknown workflow")
        return store.plans(identity, workflow)

    @app.post("/api/plans", status_code=201)
    async def candidate(body: Candidate, identity=Depends(manager)):
        if body.config.provider == "http" and (not settings.http_url or body.config.model not in settings.http_models):
            raise APIError(422, "provider_not_configured", "Configure the HTTPS provider and model allowlist first")
        if body.source_versions and body.source_versions != {"sales": adapter.snapshot(identity)}:
            raise APIError(422, "source_not_approved", "Use the current approved sales snapshot")
        return store.create(identity, body)

    @app.get("/api/plans/{ident}")
    async def plan(ident: str, identity=Depends(principal)):
        return store.plan(identity, ident)

    @app.get("/api/plans/{ident}/diff")
    async def difference(ident: str, identity=Depends(principal)):
        selected = store.plan(identity, ident)
        previous = store.plan(identity, selected["parent"]) if selected["parent"] else selected
        return {
            "parent": selected["parent"],
            "version": ident,
            "changes": {
                key: {"before": previous["config"].get(key), "after": value}
                for key, value in selected["config"].items()
                if previous["config"].get(key) != value
            },
            "source_changes": {"before": previous["sources"], "after": selected["sources"]},
            "expiry_changes": {"before": previous["expires_at"], "after": selected["expires_at"]},
        }

    @app.post("/api/plans/{ident}/evaluate")
    async def evaluate(ident: str, request: Request, identity=Depends(manager)):
        return await cancellable(request, evaluator.evaluate(identity, ident))

    @app.post("/api/plans/{ident}/evaluate-live")
    async def evaluate_live(ident: str, body: LiveEvaluation, request: Request, identity=Depends(manager)):
        return await cancellable(request, evaluator.evaluate(identity, ident, live=body))

    @app.get("/api/plans/{ident}/evaluations")
    async def evaluations(ident: str, identity=Depends(principal)):
        return store.evaluations(identity, ident)

    @app.post("/api/plans/{ident}/activate")
    async def activate(ident: str, identity=Depends(manager)):
        selected = store.plan(identity, ident)
        source = adapter.snapshot(identity) if selected["workflow"] == "sales_report" else "none"
        return store.activate(identity, ident, source)

    @app.post("/api/plans/{ident}/rollback")
    async def rollback(ident: str, identity=Depends(manager)):
        selected = store.plan(identity, ident)
        source = adapter.snapshot(identity) if selected["workflow"] == "sales_report" else "none"
        return store.activate(identity, ident, source, rollback=True)

    @app.get("/api/source")
    async def source(identity=Depends(principal)):
        return {
            "source": "sales",
            "version": adapter.snapshot(identity),
            "synthetic": True,
            "authorized_branches": identity.branches,
        }

    @app.get("/api/traces")
    async def traces(identity=Depends(principal)):
        store.prune(settings.retention_days)
        return store.traces(identity)

    @app.post("/api/cache/invalidate")
    async def invalidate(identity=Depends(manager)):
        return {"invalidated": store.invalidate(identity)}

    @app.get("/")
    async def dashboard():
        return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text())

    @app.get("/static/{name}")
    async def asset(name: str):
        media = {"dashboard.js": "text/javascript", "dashboard.css": "text/css"}
        if name not in media:
            raise NotFound()
        return Response((Path(__file__).parent / "static" / name).read_text(), media_type=media[name])

    return app


app = create_app()
