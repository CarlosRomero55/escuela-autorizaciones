from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import router as auth_router
from app.config import Settings, get_settings
from app.db import create_session_factory
from app.ratelimit import RateLimiter
from app.services import ErrorNegocio
from app.routers.admin import router as admin_router
from app.routers.familias import api as familias_api
from app.routers.familias import publico as familias_publico
from app.routers.porteria import api as porteria_api
from app.routers.porteria import publico_codigos
from app.routers.admin_html import FormularioVencido, NoAutenticado, SinPermiso, pagina_error
from app.routers.admin_html import router as admin_html_router

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

    @app.exception_handler(ErrorNegocio)
    async def error_de_negocio(request: Request, exc: ErrorNegocio):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.mensaje})

    @app.exception_handler(NoAutenticado)
    async def sin_sesion(request: Request, exc: NoAutenticado):
        return RedirectResponse("/admin/login", status_code=303)

    @app.exception_handler(SinPermiso)
    async def sin_permiso(request: Request, exc: SinPermiso):
        return pagina_error(request, "Su usuario no tiene acceso al panel de administración.", 403)

    @app.exception_handler(FormularioVencido)
    async def formulario_vencido(request: Request, exc: FormularioVencido):
        return pagina_error(request, "El formulario expiró. Vuelva a la página anterior, recárguela e intente de nuevo.", 403)

    app.mount("/static", StaticFiles(directory=RAIZ / "static"), name="static")
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(familias_api)
    app.include_router(familias_publico)
    app.include_router(porteria_api)
    app.include_router(publico_codigos)
    app.include_router(admin_html_router)

    @app.get("/", include_in_schema=False)
    def raiz():
        return RedirectResponse("/admin", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
