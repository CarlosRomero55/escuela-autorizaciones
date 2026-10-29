from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import router as auth_router
from app.config import Settings, get_settings
from app.db import create_session_factory
from app.ratelimit import RateLimiter
from app.routers.admin import router as admin_router
from app.routers.familias import api as familias_api
from app.routers.familias import publico as familias_publico
from app.routers.porteria import api as porteria_api
from app.routers.porteria import publico_codigos

RAIZ = Path(__file__).resolve().parents[1]

METODOS_CON_EFECTO = {"POST", "PUT", "PATCH", "DELETE"}

CABECERAS_BASE = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def create_app(session_factory=None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_for_production()
    es_produccion = settings.app_env == "production"

    app = FastAPI(
        title="escuela-autorizaciones",
        docs_url=None if es_produccion else "/docs",
        redoc_url=None,
        openapi_url=None if es_produccion else "/openapi.json",
    )

    app.state.settings = settings
    app.state.session_factory = session_factory or create_session_factory(settings.database_url)
    app.state.login_limiter = RateLimiter(max_hits=5, window_seconds=300)
    app.state.totp_limiter = RateLimiter(max_hits=5, window_seconds=300)

    # Sesiones firmadas en cookie. SameSite estricto mitiga CSRF desde otros sitios.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="sid",
        max_age=8 * 3600,
        same_site="strict",
        https_only=es_produccion,
    )

    @app.middleware("http")
    async def cabeceras_de_seguridad(request: Request, call_next):
        respuesta = await call_next(request)
        for nombre, valor in CABECERAS_BASE.items():
            respuesta.headers.setdefault(nombre, valor)
        if es_produccion:
            respuesta.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return respuesta

    @app.middleware("http")
    async def proteccion_csrf(request: Request, call_next):
        # Un formulario de otro sitio no puede añadir esta cabecera sin una petición previa permitida por CORS.
        if (
            request.method in METODOS_CON_EFECTO
            and request.url.path.startswith("/api/")
            and "x-requested-with" not in request.headers
        ):
            return JSONResponse(status_code=403, content={"detail": "Solicitud rechazada."})
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=RAIZ / "static"), name="static")
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(familias_api)
    app.include_router(familias_publico)
    app.include_router(porteria_api)
    app.include_router(publico_codigos)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
