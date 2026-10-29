"""Clases (salón + materia): muro, tareas, entregas, prórrogas, calificaciones y rúbricas.

Reglas de alcance (app.permisos):
- El docente asignado gestiona su clase; nadie más crea tareas ni califica.
- Estudiante y representante ven las clases del salón y solo el trabajo de su alumno.
- El directivo ve las clases de su área en modo lectura.
- El administrador ve las clases, pero para abrir el trabajo de un alumno debe justificar el acceso.
"""

import csv
import io
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import academico, archivos, services
from app.models import (
    TIPOS_TAREA,
    Alumno,
    ArchivoEntrega,
    Entrega,
    Materia,
    PeriodoCalificacion,
    Prorroga,
    Publicacion,
    Salon,
    SalonMateriaDocente,
    Tarea,
)
from app.permisos import (
    Actor,
    SinAlcance,
    alumnos_propios_en_clase,
    exigir_clase,
    gestiona_clase,
)
from app.tiempo import TZ_ESCUELA, ahora, desde_formulario
from app.web import Web, acceso_vigente, avisar, entero, no_encontrado, render, verificar_csrf, volver, web

router = APIRouter(prefix="/portal", include_in_schema=False)

VISTAS = ("tareas", "muro", "matriz", "calificaciones")


class MotivoRequerido(Exception):
    def __init__(self, alumno_id: int):
        super().__init__(alumno_id)
        self.alumno_id = alumno_id


def _autor(w: Web) -> services.Autor:
    return services.Autor(usuario_id=w.actor.id, rol=w.actor.rol, ip=w.ip)


def _ve_todo_el_salon(actor: Actor) -> bool:
    return actor.rol in ("docente", "directivo", "administrador")


def _alumno_contexto(db: Session, actor: Actor, clase: SalonMateriaDocente, pedido: str | None) -> int | None:
    propios = alumnos_propios_en_clase(db, actor, clase)
    if not propios:
        return None
    elegido = entero(pedido)
    return elegido if elegido in propios else propios[0]


def _tarea_visible(w: Web, tarea_id: int) -> tuple[Tarea, SalonMateriaDocente]:
    tarea = w.db.get(Tarea, tarea_id)
    if tarea is None:
        raise SinAlcance()
    clase = exigir_clase(w.db, w.actor, tarea.clase_id)
    if tarea.estado == "borrador" and not gestiona_clase(w.actor, clase):
        raise SinAlcance()
    return tarea, clase


def _entrega_visible(w: Web, entrega_id: int) -> tuple[Entrega, Tarea, SalonMateriaDocente]:
    entrega = w.db.get(Entrega, entrega_id)
    if entrega is None:
        raise SinAlcance()
    tarea, clase = _tarea_visible(w, entrega.tarea_id)
    if not _ve_todo_el_salon(w.actor) and entrega.alumno_id not in alumnos_propios_en_clase(w.db, w.actor, clase):
        raise SinAlcance()
    if w.actor.rol == "administrador" and not acceso_vigente(w, entrega.alumno_id):
        raise MotivoRequerido(entrega.alumno_id)
    return entrega, tarea, clase


def _ejecutar(w: Web, exito: str, funcion, *args, **kwargs):
    try:
        resultado = funcion(*args, **kwargs)
    except services.ErrorNegocio as e:
        avisar(w, e.mensaje, "danger")
        return None
    avisar(w, exito)
    return resultado


# ===================== Clase =====================

@router.get("/clases/{clase_id}", response_class=HTMLResponse)
def ver_clase(clase_id: int, vista: str = "tareas", periodo: str = "", alumno: str = "", w: Web = Depends(web)):
    db, actor = w.db, w.actor
    try:
        clase = exigir_clase(db, actor, clase_id)
    except SinAlcance:
        return no_encontrado(w)
    gestiona = gestiona_clase(actor, clase)
    vista = vista if vista in VISTAS else "tareas"
    if vista == "matriz" and not _ve_todo_el_salon(actor):
        vista = "tareas"
    periodos = academico.periodos_del_anio(db)
    actual = academico.periodo_actual(db)
    periodo_sel = next((p for p in periodos if p.id == entero(periodo)), actual)
    alumno_id = _alumno_contexto(db, actor, clase, alumno)
    datos = {
        "clase": clase, "salon": db.get(Salon, clase.salon_id), "gestiona": gestiona, "vista": vista,
        "periodos": periodos, "periodo": periodo_sel, "alumno_id": alumno_id,
        "propios": [db.get(Alumno, a) for a in alumnos_propios_en_clase(db, actor, clase)],
        "ve_todo": _ve_todo_el_salon(actor), "estados": academico.ETIQUETAS_ESTADO,
    }

    if vista == "muro":
        datos["publicaciones"] = db.scalars(select(Publicacion).where(Publicacion.clase_id == clase.id)
                                            .order_by(Publicacion.id.desc()).limit(30)).all()
    elif vista == "tareas":
        consulta = select(Tarea).where(Tarea.clase_id == clase.id)
        if periodo_sel is not None:
            consulta = consulta.where(Tarea.periodo_id == periodo_sel.id)
        if not gestiona:
            consulta = consulta.where(Tarea.estado == "publicada")
        tareas = db.scalars(consulta.order_by(Tarea.fecha_entrega)).all()
        filas = []
        for tarea in tareas:
            fila = {"tarea": tarea}
            if alumno_id is not None:
                entrega = db.scalar(select(Entrega).where(Entrega.tarea_id == tarea.id, Entrega.alumno_id == alumno_id))
                vence = academico.fecha_efectiva(db, tarea, alumno_id)
                fila.update(entrega=entrega, vence=vence, estado=academico.estado_para(entrega, vence))
            else:
                conteo = dict(db.execute(select(Entrega.estado, func.count()).where(Entrega.tarea_id == tarea.id)
                                         .group_by(Entrega.estado)).all())
                fila.update(entregadas=sum(conteo.values()),
                            por_calificar=conteo.get("entregada", 0) + conteo.get("con_retraso", 0),
                            prorrogas=db.scalar(select(func.count()).select_from(Prorroga).where(
                                Prorroga.tarea_id == tarea.id, Prorroga.estado == "pendiente")))
            filas.append(fila)
        datos["filas"] = filas
        datos["total_alumnos"] = len(academico.alumnos_de_clase(db, clase))
    elif vista == "matriz":
        alumnos, tareas, celdas = academico.matriz(db, clase, periodo_sel.id if periodo_sel else None)
        datos.update(alumnos=alumnos, tareas=tareas, celdas=celdas)
    else:  # calificaciones
        escala = academico.escala_de_clase(db, clase)
        datos["escala"] = escala
        alumnos = (academico.alumnos_de_clase(db, clase) if _ve_todo_el_salon(actor)
                   else [db.get(Alumno, alumno_id)] if alumno_id else [])
        promedios = {}
        for a in alumnos:
            for p in periodos:
                valor = academico.promedio(db, clase, a.id, p.id)
                promedios[(a.id, p.id)] = (valor, academico.equivalencia(escala, valor))
        datos.update(alumnos=alumnos, promedios=promedios)
        if alumno_id is not None:
            datos["calificadas"] = db.execute(
                select(Entrega, Tarea).join(Tarea, Tarea.id == Entrega.tarea_id)
                .where(Tarea.clase_id == clase.id, Entrega.alumno_id == alumno_id,
                       Entrega.estado.in_(("calificada", "devuelta")))
                .order_by(Tarea.fecha_entrega.desc())).all()
    return render(w, "clases/clase.html", datos, seccion="inicio")


@router.get("/clases/{clase_id}/matriz.csv")
def exportar_matriz(clase_id: int, periodo: str = "", w: Web = Depends(web)):
    try:
        clase = exigir_clase(w.db, w.actor, clase_id)
    except SinAlcance:
        return no_encontrado(w)
    if not _ve_todo_el_salon(w.actor):
        return no_encontrado(w)
    alumnos, tareas, celdas = academico.matriz(w.db, clase, entero(periodo))
    salida = io.StringIO()
    escritor = csv.writer(salida, delimiter=";")

    def segura(fila):
        # Una celda que empieza con = + - @ se ejecutaría como fórmula al abrir el CSV en una hoja de cálculo
        return ["'" + str(v) if str(v)[:1] in "=+-@\t\r" else v for v in fila]

    escritor.writerow(segura(["alumno", *[t.titulo for t in tareas]]))
    for a in alumnos:
        fila = [a.nombre]
        for t in tareas:
            celda = celdas[(a.id, t.id)]
            nota = celda.entrega.nota if celda.entrega is not None and celda.entrega.nota is not None else None
            fila.append(f"{nota}".replace(".", ",") if nota is not None else academico.ETIQUETAS_ESTADO[celda.estado][0])
        escritor.writerow(segura(fila))
    return Response("﻿" + salida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="cumplimiento-clase-{clase.id}.csv"'})


@router.post("/clases/{clase_id}/publicaciones", dependencies=[Depends(verificar_csrf)])
def publicar(clase_id: int, titulo: str = Form(""), cuerpo: str = Form(""), w: Web = Depends(web)):
    try:
        clase = exigir_clase(w.db, w.actor, clase_id)
    except SinAlcance:
        return no_encontrado(w)
    if not gestiona_clase(w.actor, clase):
        return no_encontrado(w)
    _ejecutar(w, "Publicado en el muro.", academico.publicar_en_muro, w.db, _autor(w), clase, titulo, cuerpo)
    return volver(f"/portal/clases/{clase_id}?vista=muro")


# ===================== Tareas =====================

@router.get("/clases/{clase_id}/tareas/nueva", response_class=HTMLResponse)
def nueva_tarea(clase_id: int, w: Web = Depends(web)):
    try:
        clase = exigir_clase(w.db, w.actor, clase_id)
    except SinAlcance:
        return no_encontrado(w)
    if not gestiona_clase(w.actor, clase):
        return no_encontrado(w)
    return render(w, "clases/tarea_nueva.html", {
        "clase": clase, "salon": w.db.get(Salon, clase.salon_id), "tipos": TIPOS_TAREA,
        "tipos_archivo": archivos.TIPOS, "periodos": [p for p in academico.periodos_del_anio(w.db) if not p.cerrado],
        "periodo_actual": academico.periodo_actual(w.db),
        "fecha_sugerida": (ahora() + timedelta(days=7)).astimezone(TZ_ESCUELA).strftime("%Y-%m-%dT18:00"),
        "rubricas": academico.rubricas_disponibles(w.db, w.actor.id, clase.materia_id),
    }, seccion="inicio")


@router.post("/clases/{clase_id}/tareas", dependencies=[Depends(verificar_csrf)])
def crear_tarea(clase_id: int, titulo: str = Form(""), instrucciones: str = Form(""), tipo: str = Form("tarea"),
                fecha_entrega: str = Form(""), peso: str = Form("1"), tipos_permitidos: list[str] = Form([]),
                admite_enlace: str = Form(""), admite_prorroga: str = Form(""), periodo_id: str = Form(""),
                rubrica_id: str = Form(""), accion: str = Form("borrador"), w: Web = Depends(web)):
    try:
        clase = exigir_clase(w.db, w.actor, clase_id)
    except SinAlcance:
        return no_encontrado(w)
    if not gestiona_clase(w.actor, clase):
        return no_encontrado(w)
    try:
        fecha = desde_formulario(fecha_entrega)
    except ValueError:
        avisar(w, "Indique la fecha y hora de entrega.", "danger")
        return volver(f"/portal/clases/{clase_id}/tareas/nueva")
    tarea = _ejecutar(
        w, "Tarea publicada." if accion == "publicar" else "Tarea guardada como borrador.",
        academico.crear_tarea, w.db, _autor(w), clase, titulo=titulo, instrucciones=instrucciones, tipo=tipo,
        fecha_entrega=fecha, peso=entero(peso) or 0, tipos_permitidos=tipos_permitidos,
        admite_enlace=admite_enlace == "on", admite_prorroga=admite_prorroga == "on",
        periodo_id=entero(periodo_id) or 0, rubrica_id=entero(rubrica_id), publicar=accion == "publicar")
    if tarea is None:
        return volver(f"/portal/clases/{clase_id}/tareas/nueva")
    return volver(f"/portal/tareas/{tarea.id}")


@router.get("/tareas/{tarea_id}", response_class=HTMLResponse)
def ver_tarea(tarea_id: int, alumno: str = "", w: Web = Depends(web)):
    db, actor = w.db, w.actor
    try:
        tarea, clase = _tarea_visible(w, tarea_id)
    except SinAlcance:
        return no_encontrado(w)
    datos = {"tarea": tarea, "clase": clase, "salon": db.get(Salon, clase.salon_id),
             "gestiona": gestiona_clase(actor, clase), "estados": academico.ETIQUETAS_ESTADO,
             "tipos_archivo": archivos.TIPOS, "periodo": db.get(PeriodoCalificacion, tarea.periodo_id)}
    alumno_id = _alumno_contexto(db, actor, clase, alumno)
    if alumno_id is not None:
        entrega = db.scalar(select(Entrega).where(Entrega.tarea_id == tarea.id, Entrega.alumno_id == alumno_id))
        vence = academico.fecha_efectiva(db, tarea, alumno_id)
        datos.update(
            alumno=db.get(Alumno, alumno_id), entrega=entrega, vence=vence,
            estado_actual=academico.estado_para(entrega, vence),
            propios=[db.get(Alumno, a) for a in alumnos_propios_en_clase(db, actor, clase)],
            prorrogas=db.scalars(select(Prorroga).where(Prorroga.tarea_id == tarea.id, Prorroga.alumno_id == alumno_id)
                                 .order_by(Prorroga.id.desc())).all(),
            puede_entregar=actor.rol == "estudiante" and (entrega is None or entrega.estado != "calificada"),
        )
    elif _ve_todo_el_salon(actor):
        alumnos, _, celdas = academico.matriz(db, clase)
        datos.update(alumnos=alumnos, celdas={a.id: celdas.get((a.id, tarea.id)) for a in alumnos},
                     pendientes=db.scalars(select(Prorroga).where(Prorroga.tarea_id == tarea.id,
                                                                  Prorroga.estado == "pendiente")).all())
    return render(w, "clases/tarea.html", datos, seccion="inicio")


@router.post("/tareas/{tarea_id}/publicar", dependencies=[Depends(verificar_csrf)])
def publicar_tarea(tarea_id: int, w: Web = Depends(web)):
    try:
        tarea, clase = _tarea_visible(w, tarea_id)
    except SinAlcance:
        return no_encontrado(w)
    if not gestiona_clase(w.actor, clase):
        return no_encontrado(w)
    _ejecutar(w, "Tarea publicada: ya la ven los estudiantes.", academico.publicar_tarea, w.db, _autor(w), tarea)
    return volver(f"/portal/tareas/{tarea_id}")


@router.post("/tareas/{tarea_id}/entregar", dependencies=[Depends(verificar_csrf)])
def entregar(tarea_id: int, request: Request, enlace: str = Form(""), comentario: str = Form(""),
             adjuntos: list[UploadFile] = File([]), w: Web = Depends(web)):
    try:
        tarea, clase = _tarea_visible(w, tarea_id)
    except SinAlcance:
        return no_encontrado(w)
    propios = alumnos_propios_en_clase(w.db, w.actor, clase)
    # Solo el propio estudiante entrega; el representante consulta
    if w.actor.rol != "estudiante" or not propios:
        return no_encontrado(w)
    limite = archivos.maximo_lectura()
    contenidos = [(a.filename or "", a.file.read(limite)) for a in adjuntos if a.filename]
    _ejecutar(w, "Entrega recibida.", academico.entregar, w.db, _autor(w), request.app.state.almacen, tarea,
              propios[0], archivos=contenidos, enlace=enlace, comentario=comentario)
    return volver(f"/portal/tareas/{tarea_id}")


@router.post("/tareas/{tarea_id}/prorrogas", dependencies=[Depends(verificar_csrf)])
def pedir_prorroga(tarea_id: int, alumno_id: str = Form(""), motivo: str = Form(""), nueva_fecha: str = Form(""),
                   w: Web = Depends(web)):
    try:
        tarea, clase = _tarea_visible(w, tarea_id)
    except SinAlcance:
        return no_encontrado(w)
    alumno = _alumno_contexto(w.db, w.actor, clase, alumno_id)
    if alumno is None:
        return no_encontrado(w)
    try:
        fecha = desde_formulario(nueva_fecha)
    except ValueError:
        avisar(w, "Indique la nueva fecha que solicita.", "danger")
        return volver(f"/portal/tareas/{tarea_id}?alumno={alumno}")
    _ejecutar(w, "Solicitud enviada al docente.", academico.solicitar_prorroga, w.db, _autor(w), tarea, alumno,
              motivo, fecha)
    return volver(f"/portal/tareas/{tarea_id}?alumno={alumno}")


@router.post("/prorrogas/{prorroga_id}", dependencies=[Depends(verificar_csrf)])
def resolver_prorroga(prorroga_id: int, accion: str = Form(""), w: Web = Depends(web)):
    prorroga = w.db.get(Prorroga, prorroga_id)
    if prorroga is None or accion not in ("aprobar", "rechazar"):
        return no_encontrado(w)
    try:
        _, clase = _tarea_visible(w, prorroga.tarea_id)
    except SinAlcance:
        return no_encontrado(w)
    if not gestiona_clase(w.actor, clase):
        return no_encontrado(w)
    _ejecutar(w, "Prórroga aprobada." if accion == "aprobar" else "Prórroga rechazada.",
              academico.resolver_prorroga, w.db, _autor(w), prorroga, accion == "aprobar")
    return volver(f"/portal/tareas/{prorroga.tarea_id}")


# ===================== Entregas y archivos =====================

@router.get("/entregas/{entrega_id}", response_class=HTMLResponse)
def ver_entrega(entrega_id: int, w: Web = Depends(web)):
    try:
        entrega, tarea, clase = _entrega_visible(w, entrega_id)
    except SinAlcance:
        return no_encontrado(w)
    except MotivoRequerido as e:
        return volver(f"/portal/alumnos/{e.alumno_id}")
    escala = academico.escala_de_clase(w.db, clase)
    return render(w, "clases/entrega.html", {
        "entrega": entrega, "tarea": tarea, "clase": clase, "salon": w.db.get(Salon, clase.salon_id),
        "alumno": w.db.get(Alumno, entrega.alumno_id), "gestiona": gestiona_clase(w.actor, clase),
        "escala": escala, "estados": academico.ETIQUETAS_ESTADO,
        "periodo": w.db.get(PeriodoCalificacion, tarea.periodo_id),
        "vence": academico.fecha_efectiva(w.db, tarea, entrega.alumno_id),
        "rango": academico.equivalencia(escala, entrega.nota),
    }, seccion="inicio")


@router.post("/entregas/{entrega_id}/calificar", dependencies=[Depends(verificar_csrf)])
def calificar(entrega_id: int, nota: str = Form(""), nivel: str = Form(""), comentario: str = Form(""),
              accion: str = Form("calificar"), w: Web = Depends(web)):
    try:
        entrega, _, clase = _entrega_visible(w, entrega_id)
    except (SinAlcance, MotivoRequerido):
        return no_encontrado(w)
    if not gestiona_clase(w.actor, clase):
        return no_encontrado(w)
    _ejecutar(w, "Entrega devuelta para corrección." if accion == "devolver" else "Calificación guardada.",
              academico.calificar, w.db, _autor(w), entrega, nota=nota, nivel=nivel, comentario=comentario,
              devolver=accion == "devolver")
    return volver(f"/portal/entregas/{entrega_id}")


def _archivo_visible(w: Web, archivo_id: int) -> ArchivoEntrega:
    archivo = w.db.get(ArchivoEntrega, archivo_id)
    if archivo is None:
        raise SinAlcance()
    _entrega_visible(w, archivo.entrega_id)
    return archivo


@router.get("/archivos/{archivo_id}")
def descargar(archivo_id: int, request: Request, w: Web = Depends(web)):
    try:
        archivo = _archivo_visible(w, archivo_id)
    except (SinAlcance, MotivoRequerido):
        return no_encontrado(w)
    contenido = request.app.state.almacen.leer(archivo.clave)
    # Imágenes y PDF se pueden ver en el navegador; todo lo demás se descarga. El sandbox impide
    # que un archivo ejecute scripts aunque se abra directamente.
    en_linea = archivo.mime.startswith("image/") or archivo.mime == "application/pdf"
    disposicion = "inline" if en_linea else "attachment"
    return Response(contenido, media_type=archivo.mime if en_linea else "application/octet-stream", headers={
        "Content-Disposition": f"{disposicion}; filename*=UTF-8''{quote(archivo.nombre)}",
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/archivos/{archivo_id}/ver", response_class=HTMLResponse)
def ver_codigo(archivo_id: int, request: Request, w: Web = Depends(web)):
    try:
        archivo = _archivo_visible(w, archivo_id)
    except (SinAlcance, MotivoRequerido):
        return no_encontrado(w)
    if not archivo.mime.startswith("text/"):
        return volver(f"/portal/archivos/{archivo_id}")
    texto = request.app.state.almacen.leer(archivo.clave).decode("utf-8", errors="replace")
    entrega = w.db.get(Entrega, archivo.entrega_id)
    return render(w, "clases/codigo.html", {"archivo": archivo, "texto": texto, "entrega": entrega}, seccion="inicio")


# ===================== Rúbricas =====================

@router.get("/rubricas", response_class=HTMLResponse)
def rubricas(w: Web = Depends(web)):
    if w.actor.rol != "docente":
        return no_encontrado(w)
    materias = w.db.scalars(select(Materia).join(SalonMateriaDocente, SalonMateriaDocente.materia_id == Materia.id)
                            .where(SalonMateriaDocente.docente_id == w.actor.id).distinct().order_by(Materia.nombre)).all()
    return render(w, "clases/rubricas.html", {
        "materias": materias,
        "rubricas": {m.id: academico.rubricas_disponibles(w.db, w.actor.id, m.id) for m in materias},
    }, seccion="rubricas")


@router.post("/rubricas", dependencies=[Depends(verificar_csrf)])
def crear_rubrica(materia_id: str = Form(""), titulo: str = Form(""), compartida: str = Form(""),
                  criterio: list[str] = Form([]), puntos: list[str] = Form([]), w: Web = Depends(web)):
    if w.actor.rol != "docente":
        return no_encontrado(w)
    materia = entero(materia_id)
    dicta = w.db.scalar(select(SalonMateriaDocente.id).where(SalonMateriaDocente.docente_id == w.actor.id,
                                                             SalonMateriaDocente.materia_id == materia))
    if dicta is None:
        avisar(w, "Solo puede crear rúbricas de las materias que dicta.", "danger")
    else:
        _ejecutar(w, "Rúbrica creada.", academico.crear_rubrica, w.db, _autor(w), materia, titulo,
                  list(zip(criterio, puntos, strict=False)), compartida == "on")
    return volver("/portal/rubricas")
