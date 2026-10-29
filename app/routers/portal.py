"""Portal común a todos los roles: inicio, salones, ficha del alumno, búsqueda y cuenta personal."""

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import security, services, sesiones
from app.audit import registrar
from app.auth import ErrorAcceso, confirmar_enrolamiento, preparar_enrolamiento
from app.models import (
    AdultoAutorizado,
    AlertaInicioSesion,
    Alumno,
    Area,
    Campana,
    DispositivoUsuario,
    Incidente,
    Materia,
    Matricula,
    RepresentanteAlumno,
    Salon,
    SalonMateriaDocente,
    Sesion,
    Usuario,
)
from app.permisos import (
    SinAlcance,
    alumnos_visibles,
    anio_activo,
    exigir_alumno,
    exigir_salon,
    puede_ver_alumnos_del_salon,
    requiere_totp,
    salones_visibles,
)
from app.routers.acceso import qr_svg
from app.tiempo import ahora, aware
from app.web import Web, acceso_vigente, avisar, conceder_acceso, render, verificar_csrf, volver, web

router = APIRouter(prefix="/portal", include_in_schema=False)


def _autor(w: Web) -> services.Autor:
    return services.Autor(usuario_id=w.actor.id, rol=w.actor.rol, ip=w.ip)


# ---------- Inicio por rol ----------

def _tarjetas_salon(db: Session, salon_ids) -> list[dict]:
    salones = db.scalars(select(Salon).where(Salon.id.in_(salon_ids)).order_by(Salon.nombre)).all()
    resultado = []
    for salon in salones:
        alumnos = db.scalar(select(func.count()).select_from(Matricula)
                            .where(Matricula.salon_id == salon.id, Matricula.estado == "activa"))
        resultado.append({"salon": salon, "alumnos": alumnos})
    return resultado


@router.get("", response_class=HTMLResponse)
def inicio(w: Web = Depends(web)):
    db, actor = w.db, w.actor
    anio = anio_activo(db)
    datos: dict = {"anio": anio}

    if actor.rol in ("administrador", "coordinacion"):
        momento = ahora()
        campanas = [c for c in db.scalars(select(Campana).order_by(Campana.fecha_limite)).all()
                    if aware(c.fecha_limite) > momento]
        datos["campanas"] = [{"campana": c, "totales": services.totalizar(services.progreso_campana(db, c.id))}
                             for c in campanas]
        datos["indicadores"] = {
            "alumnos": db.scalar(select(func.count()).select_from(Matricula).where(
                Matricula.estado == "activa", Matricula.anio_lectivo_id == (anio.id if anio else -1))),
            "usuarios": db.scalar(select(func.count()).select_from(Usuario).where(Usuario.activo.is_(True))),
            "por_validar": db.scalar(select(func.count()).select_from(AdultoAutorizado)
                                     .where(AdultoAutorizado.estado == "pendiente")),
            "incidentes": db.scalar(select(func.count()).select_from(Incidente)
                                    .where(Incidente.creado_en >= momento - timedelta(days=7))),
        }
    elif actor.rol == "docente":
        asignaciones = db.execute(
            select(SalonMateriaDocente, Salon).join(Salon, Salon.id == SalonMateriaDocente.salon_id)
            .where(SalonMateriaDocente.docente_id == actor.id, Salon.id.in_(salones_visibles(db, actor)))
            .order_by(Salon.nombre)
        ).all()
        tutorias = db.scalars(select(Salon).where(Salon.tutor_id == actor.id, Salon.id.in_(salones_visibles(db, actor)))).all()
        conteos = {t["salon"].id: t["alumnos"] for t in _tarjetas_salon(db, salones_visibles(db, actor))}
        datos.update(asignaciones=asignaciones, tutorias=tutorias, conteos=conteos)
    elif actor.rol == "directivo":
        tarjetas = _tarjetas_salon(db, salones_visibles(db, actor))
        # Primero se cuenta por id y luego se cargan los usuarios: agrupar por la entidad completa
        # chocaría con la carga automática de la persona en PostgreSQL
        clases = dict(db.execute(
            select(SalonMateriaDocente.docente_id, func.count(SalonMateriaDocente.id))
            .join(Materia, Materia.id == SalonMateriaDocente.materia_id)
            .where(SalonMateriaDocente.salon_id.in_(salones_visibles(db, actor)),
                   SalonMateriaDocente.docente_id.is_not(None),
                   *([Materia.area_id.in_(actor.areas)] if actor.areas is not None else []))
            .group_by(SalonMateriaDocente.docente_id)
        ).all())
        docentes = [(u, clases[u.id]) for u in
                    db.scalars(select(Usuario).where(Usuario.id.in_(clases)).order_by(Usuario.login)).all()]
        nombres_areas = ("Toda la escuela" if actor.areas is None else
                         ", ".join(db.scalars(select(Area.nombre).where(Area.id.in_(actor.areas)).order_by(Area.nombre))))
        datos.update(tarjetas=tarjetas, docentes=docentes, nombres_areas=nombres_areas,
                     alumnos=sum(t["alumnos"] for t in tarjetas))
    elif actor.rol == "estudiante":
        salon = db.scalar(select(Salon).where(Salon.id.in_(salones_visibles(db, actor))))
        materias = []
        if salon is not None:
            materias = db.scalars(select(SalonMateriaDocente).where(SalonMateriaDocente.salon_id == salon.id)
                                  .join(Materia, Materia.id == SalonMateriaDocente.materia_id)
                                  .order_by(Materia.nombre)).all()
        datos.update(salon=salon, materias=materias)
    elif actor.rol == "representante":
        representados = db.execute(
            select(Alumno, RepresentanteAlumno.parentesco, Salon)
            .join(RepresentanteAlumno, RepresentanteAlumno.alumno_id == Alumno.id)
            .outerjoin(Salon, Salon.id == Alumno.salon_id)
            .where(Alumno.id.in_(alumnos_visibles(db, actor)), RepresentanteAlumno.usuario_id == actor.id)
            .order_by(Alumno.nombre)
        ).all()
        datos["representados"] = representados
    return render(w, "portal/inicio.html", datos, seccion="inicio")


# ---------- Salón ----------

@router.get("/salones/{salon_id}", response_class=HTMLResponse)
def ver_salon(salon_id: int, w: Web = Depends(web)):
    try:
        salon = exigir_salon(w.db, w.actor, salon_id)
    except SinAlcance:
        return _no_encontrado(w)
    materias = w.db.scalars(select(SalonMateriaDocente).join(Materia, Materia.id == SalonMateriaDocente.materia_id)
                            .where(SalonMateriaDocente.salon_id == salon.id).order_by(Materia.nombre)).all()
    alumnos = []
    if puede_ver_alumnos_del_salon(w.actor):
        alumnos = w.db.scalars(select(Alumno).join(Matricula, Matricula.alumno_id == Alumno.id)
                               .where(Matricula.salon_id == salon.id, Matricula.estado == "activa")
                               .order_by(Alumno.nombre)).all()
    return render(w, "portal/salon.html", {
        "salon": salon, "materias": materias, "alumnos": alumnos,
        "con_enlace": w.actor.rol != "estudiante",
    }, seccion="inicio")


def _no_encontrado(w: Web) -> HTMLResponse:
    return render(w, "portal/no_encontrado.html", status_code=404)


# ---------- Ficha del alumno ----------

@router.get("/alumnos/{alumno_id}", response_class=HTMLResponse)
def ficha(alumno_id: int, w: Web = Depends(web)):
    try:
        alumno = exigir_alumno(w.db, w.actor, alumno_id)
    except SinAlcance:
        return _no_encontrado(w)
    # El administrador debe justificar cada acceso a datos de un alumno
    if w.actor.rol == "administrador" and not acceso_vigente(w, alumno.id):
        return render(w, "portal/motivo_acceso.html", {"alumno": alumno}, seccion="alumnos")

    salon = w.db.get(Salon, alumno.salon_id) if alumno.salon_id else None
    materias = []
    if salon is not None:
        materias = w.db.scalars(select(SalonMateriaDocente).join(Materia, Materia.id == SalonMateriaDocente.materia_id)
                                .where(SalonMateriaDocente.salon_id == salon.id).order_by(Materia.nombre)).all()
    representantes = w.db.execute(
        select(Usuario, RepresentanteAlumno.parentesco).join(RepresentanteAlumno, RepresentanteAlumno.usuario_id == Usuario.id)
        .where(RepresentanteAlumno.alumno_id == alumno.id, RepresentanteAlumno.validado_en.is_not(None))
    ).all()
    matriculas = w.db.execute(
        select(Matricula, Salon.nombre).join(Salon, Salon.id == Matricula.salon_id)
        .where(Matricula.alumno_id == alumno.id).order_by(Matricula.id.desc())
    ).all()
    return render(w, "portal/ficha.html", {
        "alumno": alumno, "salon": salon, "materias": materias, "representantes": representantes,
        "matriculas": matriculas,
        "salon_visible": salon is not None and salon.id in set(w.db.scalars(salones_visibles(w.db, w.actor))),
    }, seccion="alumnos" if w.actor.rol == "administrador" else "inicio")


@router.post("/alumnos/{alumno_id}/acceso", dependencies=[Depends(verificar_csrf)])
def justificar_acceso(alumno_id: int, motivo: str = Form(""), w: Web = Depends(web)):
    if w.actor.rol != "administrador":
        return volver(f"/portal/alumnos/{alumno_id}")
    try:
        alumno = exigir_alumno(w.db, w.actor, alumno_id)
    except SinAlcance:
        return _no_encontrado(w)
    motivo = motivo.strip()
    if len(motivo) < 10 or len(motivo) > 300:
        avisar(w, "Describa el motivo con al menos 10 caracteres.", "danger")
        return volver(f"/portal/alumnos/{alumno_id}")
    registrar(w.db, usuario_id=w.actor.id, rol=w.actor.rol, accion="acceso_datos_alumno", entidad="alumno",
              entidad_id=alumno.id, motivo=motivo, ip=w.ip)
    conceder_acceso(w, alumno.id)
    return volver(f"/portal/alumnos/{alumno_id}")


# ---------- Búsqueda (respeta el alcance del rol) ----------

@router.get("/buscar", response_class=HTMLResponse)
def buscar(request: Request, q: str = "", w: Web = Depends(web)):
    termino = q.strip()[:60]
    alumnos, salones = [], []
    if len(termino) >= 2:
        alumnos = w.db.scalars(select(Alumno).where(Alumno.id.in_(alumnos_visibles(w.db, w.actor)),
                                                    Alumno.nombre.ilike(f"%{termino}%"))
                               .order_by(Alumno.nombre).limit(8)).all()
        salones = w.db.scalars(select(Salon).where(Salon.id.in_(salones_visibles(w.db, w.actor)),
                                                   Salon.nombre.ilike(f"%{termino}%"))
                               .order_by(Salon.nombre).limit(5)).all()
    plantilla = "portal/_resultados.html" if request.headers.get("hx-request") else "portal/buscar.html"
    return render(w, plantilla, {"q": termino, "alumnos": alumnos, "salones": salones})


# ---------- Rol activo ----------

@router.post("/rol", dependencies=[Depends(verificar_csrf)])
def cambiar_rol(rol: str = Form(""), w: Web = Depends(web)):
    if rol in w.actor.roles and rol != w.actor.rol:
        w.sesion.rol_activo = rol
        registrar(w.db, usuario_id=w.actor.id, rol=rol, accion="cambiar_rol", entidad="sesion",
                  entidad_id=w.sesion.id, ip=w.ip)
        w.db.commit()
    return volver("/portal")


# ---------- Mi cuenta ----------

def _icono_dispositivo(descripcion: str) -> str:
    return "bi-phone" if any(s in descripcion for s in ("Android", "iOS")) else "bi-laptop"


@router.get("/perfil", response_class=HTMLResponse)
def perfil(w: Web = Depends(web)):
    usuario = w.actor.usuario
    dispositivos = {d.id: d for d in w.db.scalars(select(DispositivoUsuario)
                                                  .where(DispositivoUsuario.usuario_id == usuario.id))}
    lista_sesiones = []
    for s in sesiones.activas(w.db, w.settings, usuario.id):
        dispositivo = dispositivos.get(s.dispositivo_id)
        descripcion = dispositivo.descripcion if dispositivo else "Navegador"
        lista_sesiones.append({"sesion": s, "descripcion": descripcion, "icono": _icono_dispositivo(descripcion),
                               "actual": s.id == w.sesion.id,
                               "no_reconocido": dispositivo is not None and not dispositivo.confiable})
    alertas = w.db.scalars(select(AlertaInicioSesion).where(AlertaInicioSesion.usuario_id == usuario.id)
                           .order_by(AlertaInicioSesion.id.desc()).limit(10)).all()
    datos = {
        "sesiones": lista_sesiones,
        "alertas_por_revisar": [a for a in alertas if a.estado == "pendiente"],
        "alertas_resueltas": [a for a in alertas if a.estado != "pendiente"],
        "totp_obligatorio": requiere_totp(usuario),
        "roles": list(usuario.roles),
    }
    if not usuario.totp_secret_cifrado:
        secreto = preparar_enrolamiento(w.db, w.settings, usuario)
        datos.update(qr=qr_svg(usuario, secreto), secreto=secreto)
    return render(w, "portal/perfil.html", datos, seccion="perfil")


@router.post("/perfil/password", dependencies=[Depends(verificar_csrf)])
def cambiar_password(actual: str = Form(""), nueva: str = Form(""), confirmacion: str = Form(""),
                     w: Web = Depends(web)):
    usuario = w.actor.usuario
    if not usuario.password_hash or not security.verify_password(usuario.password_hash, actual):
        avisar(w, "La contraseña actual no es correcta.", "danger")
        return volver("/portal/perfil")
    if nueva != confirmacion:
        avisar(w, "La nueva contraseña y su confirmación no coinciden.", "danger")
        return volver("/portal/perfil")
    errores = security.validar_password(nueva, login=usuario.login, email=usuario.email)
    if errores:
        avisar(w, " ".join(errores), "danger")
        return volver("/portal/perfil")
    usuario.password_hash = security.hash_password(nueva)
    cerradas = sesiones.revocar_todas(w.db, usuario.id, excepto_id=w.sesion.id)
    registrar(w.db, usuario_id=usuario.id, rol=w.actor.rol, accion="cambiar_password", entidad="usuario",
              entidad_id=usuario.id, cambio={"sesiones_cerradas": cerradas}, ip=w.ip)
    w.db.commit()
    avisar(w, "Contraseña actualizada. Se cerraron sus otras sesiones.")
    return volver("/portal/perfil")


@router.post("/perfil/sesiones/{sesion_id}/cerrar", dependencies=[Depends(verificar_csrf)])
def cerrar_sesion(sesion_id: int, w: Web = Depends(web)):
    sesion = w.db.get(Sesion, sesion_id)
    # Solo puede cerrar sus propias sesiones; cualquier otra se trata como inexistente
    if sesion is None or sesion.usuario_id != w.actor.id:
        return _no_encontrado(w)
    sesiones.revocar(sesion)
    registrar(w.db, usuario_id=w.actor.id, rol=w.actor.rol, accion="cerrar_sesion_remota", entidad="sesion",
              entidad_id=sesion.id, ip=w.ip)
    w.db.commit()
    if sesion.id == w.sesion.id:
        respuesta = volver("/ingresar")
        sesiones.borrar_cookie(respuesta)
        return respuesta
    avisar(w, "Sesión cerrada.")
    return volver("/portal/perfil")


@router.post("/perfil/sesiones/cerrar-otras", dependencies=[Depends(verificar_csrf)])
def cerrar_otras(w: Web = Depends(web)):
    cerradas = sesiones.revocar_todas(w.db, w.actor.id, excepto_id=w.sesion.id)
    registrar(w.db, usuario_id=w.actor.id, rol=w.actor.rol, accion="cerrar_otras_sesiones", entidad="usuario",
              entidad_id=w.actor.id, cambio={"cerradas": cerradas}, ip=w.ip)
    w.db.commit()
    avisar(w, f"Se cerraron {cerradas} sesiones en otros dispositivos.")
    return volver("/portal/perfil")


@router.post("/perfil/alertas/{alerta_id}", dependencies=[Depends(verificar_csrf)])
def resolver_alerta(alerta_id: int, accion: str = Form(""), w: Web = Depends(web)):
    alerta = w.db.get(AlertaInicioSesion, alerta_id)
    if alerta is None or alerta.usuario_id != w.actor.id or accion not in ("confirmar", "bloquear"):
        return _no_encontrado(w)
    alerta.resuelta_en = ahora()
    if accion == "confirmar":
        alerta.estado = "confirmada"
        mensaje = "Gracias. Marcamos ese inicio de sesión como reconocido."
    else:
        alerta.estado = "bloqueada"
        sesion = w.db.get(Sesion, alerta.sesion_id) if alerta.sesion_id else None
        if sesion is not None:
            sesiones.revocar(sesion)
            if sesion.dispositivo_id:
                w.db.get(DispositivoUsuario, sesion.dispositivo_id).confiable = False
        mensaje = "Cerramos esa sesión. Le recomendamos cambiar su contraseña ahora."
    registrar(w.db, usuario_id=w.actor.id, rol=w.actor.rol, accion=f"alerta_{alerta.estado}", entidad="alerta",
              entidad_id=alerta.id, ip=w.ip)
    w.db.commit()
    if alerta.estado == "bloqueada" and alerta.sesion_id == w.sesion.id:
        respuesta = volver("/ingresar")
        sesiones.borrar_cookie(respuesta)
        return respuesta
    avisar(w, mensaje, "success" if accion == "confirmar" else "warning")
    return volver("/portal/perfil")


@router.post("/perfil/totp", dependencies=[Depends(verificar_csrf)])
def activar_totp(request: Request, codigo: str = Form(""), w: Web = Depends(web)):
    try:
        confirmar_enrolamiento(w.db, w.settings, request, w.actor.usuario, codigo)
        w.db.commit()
        avisar(w, "Verificación en dos pasos activada.")
    except ErrorAcceso as e:
        avisar(w, e.mensaje, "danger")
    return volver("/portal/perfil")


@router.post("/perfil/totp/desactivar", dependencies=[Depends(verificar_csrf)])
def desactivar_totp(actual: str = Form(""), w: Web = Depends(web)):
    usuario = w.actor.usuario
    if requiere_totp(usuario):
        avisar(w, "Su rol exige la verificación en dos pasos; no puede desactivarse.", "danger")
    elif not usuario.password_hash or not security.verify_password(usuario.password_hash, actual):
        avisar(w, "La contraseña no es correcta.", "danger")
    else:
        usuario.totp_secret_cifrado = None
        registrar(w.db, usuario_id=usuario.id, rol=w.actor.rol, accion="desactivar_totp", entidad="usuario",
                  entidad_id=usuario.id, ip=w.ip)
        w.db.commit()
        avisar(w, "Verificación en dos pasos desactivada.", "warning")
    return volver("/portal/perfil")
