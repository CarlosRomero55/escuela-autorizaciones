"""Aplicación de portería (PWA): la página, su manifiesto y el service worker.

El service worker se sirve desde /porteria/sw.js para que su alcance sea /porteria/ y no controle
el resto del portal. Los datos sin conexión (padrón y cola de registros) viven en IndexedDB y se
borran al salir; ver static/porteria/porteria.js.
"""

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.routers.porteria import PASOS_NO_AUTORIZADO, PORTERIA
from app.web import Web, render, requiere_rol

RAIZ = Path(__file__).resolve().parents[2] / "static" / "porteria"

router = APIRouter(include_in_schema=False)


@router.get("/porteria")
def sin_barra():
    return RedirectResponse("/porteria/", status_code=307)


@router.get("/porteria/", response_class=HTMLResponse)
def aplicacion(w: Web = Depends(requiere_rol(*PORTERIA))):
    return render(w, "porteria/app.html", {
        "pasos": PASOS_NO_AUTORIZADO,
        "horas_padron": w.settings.porteria_padron_horas,
    })


@router.get("/porteria/sw.js")
def service_worker():
    # Sin caché HTTP: así el navegador detecta enseguida una versión nueva del service worker
    return FileResponse(RAIZ / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@router.get("/porteria/manifest.webmanifest")
def manifiesto():
    return FileResponse(RAIZ / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})
