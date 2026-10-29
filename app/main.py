from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.auth import router as auth_router
from app.config import Settings, get_settings
from app.db import create_session_factory
from app.ratelimit import RateLimiter
from app.routers.acceso import router as acceso_router
from app.routers.admin import router as admin_router
from app.routers.admin_html import router as admin_html_router
from app.routers.familias import api as familias_api
from app.routers.familias import publico as familias_publico
from app.routers.portal import router as portal_router
from app.routers.porteria import api as porteria_api
from app.routers.porteria import publico_codigos
from app.services import ErrorNegocio
from app.web import FormularioVencido, NoAutenticado, PasoPendiente, SinPermiso, pagina_error

RAIZ = Path(__file__).resolve().parents[1]

METODOS_CON_EFECTO = {"POST", "PUT", "PATCH", "DELETE"}

CABECERAS_BASE = {
    # Todo se sirve desde el propio dominio: sin CDN, sin scripts ni estilos en línea
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; font-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(self), geolocation=(), microphone=()",
}


def create_app(session_factory=None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_for_production()

    app = FastAPI(
        title="Sistema escolar",
        docs_url=None if settings.es_produccion else "/docs",
        redoc_url=None,
        openapi_url=None if settings.es_produccion else "/openapi.json",
    )

    app.state.settings = settings
    app.state.session_factory = session_factory or create_session_factory(settings.database_url)
    # Límites por conexión, además del bloqueo por cuenta que se guarda en la base de datos
    app.state.login_limiter = RateLimiter(max_hits=20, window_seconds=300)
    app.state.totp_limiter = RateLimiter(max_hits=5, window_seconds=300)

    @app.middleware("http")
    async def cabeceras_de_seguridad(request: Request, call_next):
        respuesta = await call_next(request)
        for nombre, valor in CABECERAS_BASE.items():
            respuesta.headers.setdefault(nombre, valor)
        if settings.es_produccion:
            respuesta.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if not request.url.path.startswith("/static/"):
            # Las páginas con datos personales no se guardan en cachés compartidas
            respuesta.headers.setdefault("Cache-Control", "no-store")
        return respuesta

    @app.middleware("http")
    async def proteccion_csrf_api(request: Request, call_next):
        # Un formulario de otro sitio no puede añadir esta cabecera sin una petición previa permitida por CORS.
        if (request.method in METODOS_CON_EFECTO and request.url.path.startswith("/api/")
                and "x-requested-with" not in request.headers):
            return JSONResponse(status_code=403, content={"detail": "Solicitud rechazada."})
        return await call_next(request)

    @app.exception_handler(ErrorNegocio)
    async def error_de_negocio(request: Request, exc: ErrorNegocio):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.mensaje})

    @app.exception_handler(NoAutenticado)
    async def sin_sesion(request: Request, exc: NoAutenticado):
        return RedirectResponse("/ingresar", status_code=303)

    @app.exception_handler(PasoPendiente)
    async def paso_pendiente(request: Request, exc: PasoPendiente):
        return RedirectResponse(exc.destino, status_code=303)

    @app.exception_handler(SinPermiso)
    async def sin_permiso(request: Request, exc: SinPermiso):
        return pagina_error(request, "Su rol actual no tiene acceso a esta sección.", 403, "Acceso restringido")

    @app.exception_handler(FormularioVencido)
    async def formulario_vencido(request: Request, exc: FormularioVencido):
        return pagina_error(request, "El formulario expiró. Vuelva a la página anterior, recárguela e intente de nuevo.",
                            403, "Formulario vencido")

    @app.exception_handler(HTTPException)
    async def error_http(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        mensaje = "La página no existe o no está disponible para su usuario." if exc.status_code == 404 else str(exc.detail)
        return pagina_error(request, mensaje, exc.status_code, "No encontrado" if exc.status_code == 404 else "Aviso")

    app.mount("/static", StaticFiles(directory=RAIZ / "static"), name="static")
    for router in (auth_router, admin_router, familias_api, familias_publico, porteria_api, publico_codigos,
                   acceso_router, portal_router, admin_html_router):
        app.include_router(router)

    @app.get("/", include_in_schema=False)
    def raiz():
        # El sitio público llega en la Fase 2; mientras tanto, la raíz lleva al portal
        return RedirectResponse("/portal", status_code=303)

    @app.get("/admin", include_in_schema=False)
    def admin_raiz():
        return RedirectResponse("/portal", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
