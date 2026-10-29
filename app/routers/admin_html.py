"""Panel de administración (HTML). Cada ruta declara la función que exige; el alcance de los datos
lo aplica app.permisos y la bitácora la escribe app.services."""

import csv
import io
import re
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import services, sesiones
from app.audit import registrar
from app.models import (
    ROLES,
    AdultoAutorizado,
    Alumno,
    AnioLectivo,
    Area,
    BitacoraAuditoria,
    Campana,
    CampanaSalon,
    EnvioSaliente,
    EscalaCalificacion,
    Familia,
    FamiliaAlumno,
    Grado,
    Materia,
    Matricula,
    Nivel,
    Paralelo,
    PeriodoCalificacion,
    Persona,
    RepresentanteAlumno,
    RespuestaAutorizacion,
    Salon,
    SalonMateriaDocente,
    TokenAcceso,
    Usuario,
    UsuarioRol,
)
from app.permisos import anio_activo
from app.tiempo import ahora, aware, desde_formulario, local
from app.web import Web, avisar, entero, render, requiere, verificar_csrf, volver

router = APIRouter(prefix="/admin", include_in_schema=False)

POR_PAGINA = 25
ESTADOS_ADULTO = {"pendiente": "Pendientes", "validado": "Validados", "rechazado": "Rechazados"}


def _autor(w: Web) -> services.Autor:
    return services.Autor(usuario_id=w.actor.id, rol=w.actor.rol, ip=w.ip)


def _ejecutar(w: Web, exito: str, funcion, *args, **kwargs):
    """Ejecuta una operación de negocio y convierte el resultado en un aviso."""
    try:
        resultado = funcion(*args, **kwargs)
    except services.ErrorNegocio as e:
        avisar(w, e.mensaje, "danger")
        return None
    avisar(w, exito)
    return resultado if resultado is not None else True


def _pagina(valor: str) -> int:
    numero = entero(valor)
    return numero if numero and numero > 0 else 1


def enmascarar_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    local_, dominio = email.split("@", 1)
    return f"{local_[0]}{'•' * max(len(local_) - 1, 2)}@{dominio}"


def _docentes(db: Session) -> list[Usuario]:
    return db.scalars(select(Usuario).join(UsuarioRol, UsuarioRol.usuario_id == Usuario.id)
                      .where(UsuarioRol.rol == "docente", Usuario.activo.is_(True))
                      .order_by(Usuario.login)).unique().all()


def _fecha(valor: str) -> date | None:
    try:
        return date.fromisoformat(valor)
    except ValueError:
        return None


# ===================== Estructura académica =====================

@router.get("/estructura", response_class=HTMLResponse)
def estructura(w: Web = Depends(requiere("estructura.gestionar"))):
    db = w.db
    anio = anio_activo(db)
    return render(w, "admin/estructura.html", {
        "anio": anio,
        "anios": db.scalars(select(AnioLectivo).order_by(AnioLectivo.inicio.desc())).all(),
        "periodos": db.scalars(select(PeriodoCalificacion).where(
            PeriodoCalificacion.anio_lectivo_id == (anio.id if anio else -1)).order_by(PeriodoCalificacion.inicio)).all(),
        "grados": db.scalars(select(Grado).order_by(Grado.orden)).all(),
        "paralelos": db.scalars(select(Paralelo).order_by(Paralelo.nombre)).all(),
        "areas": db.scalars(select(Area).order_by(Area.nombre)).all(),
        "materias": db.scalars(select(Materia).order_by(Materia.nombre)).all(),
        "escalas": db.scalars(select(EscalaCalificacion).order_by(EscalaCalificacion.nombre)).all(),
        "niveles": db.scalars(select(Nivel).order_by(Nivel.orden)).all(),
    }, seccion="estructura")


@router.post("/estructura/anios", dependencies=[Depends(verificar_csrf)])
def crear_anio(nombre: str = Form(""), inicio: str = Form(""), fin: str = Form(""),
               w: Web = Depends(requiere("estructura.gestionar"))):
    desde, hasta = _fecha(inicio), _fecha(fin)
    if desde is None or hasta is None:
        avisar(w, "Indique las fechas de inicio y fin.", "danger")
    else:
        _ejecutar(w, f"Año lectivo {nombre.strip()} creado. Actívelo cuando corresponda.",
                  services.crear_anio, w.db, _autor(w), nombre, desde, hasta)
    return volver("/admin/estructura")


@router.post("/estructura/anios/{anio_id}/activar", dependencies=[Depends(verificar_csrf)])
def activar_anio(anio_id: int, w: Web = Depends(requiere("estructura.gestionar"))):
    _ejecutar(w, "Año lectivo activado.", services.activar_anio, w.db, _autor(w), anio_id)
    return volver("/admin/estructura")


@router.post("/estructura/periodos", dependencies=[Depends(verificar_csrf)])
def crear_periodo(nombre: str = Form(""), inicio: str = Form(""), fin: str = Form(""),
                  w: Web = Depends(requiere("estructura.gestionar"))):
    desde, hasta = _fecha(inicio), _fecha(fin)
    if desde is None or hasta is None:
        avisar(w, "Indique las fechas del periodo.", "danger")
    else:
        _ejecutar(w, "Periodo creado.", services.crear_periodo, w.db, _autor(w), nombre, desde, hasta)
    return volver("/admin/estructura")


@router.post("/estructura/paralelos", dependencies=[Depends(verificar_csrf)])
def crear_paralelo(nombre: str = Form(""), w: Web = Depends(requiere("estructura.gestionar"))):
    _ejecutar(w, "Paralelo creado.", services.crear_paralelo, w.db, _autor(w), nombre)
    return volver("/admin/estructura")


@router.post("/estructura/areas", dependencies=[Depends(verificar_csrf)])
def crear_area(nombre: str = Form(""), w: Web = Depends(requiere("estructura.gestionar"))):
    _ejecutar(w, "Área creada.", services.crear_area, w.db, _autor(w), nombre)
    return volver("/admin/estructura")


@router.post("/estructura/materias", dependencies=[Depends(verificar_csrf)])
def crear_materia(nombre: str = Form(""), area_id: str = Form(""), w: Web = Depends(requiere("estructura.gestionar"))):
    area = entero(area_id)
    if area is None:
        avisar(w, "Elija el área de la materia.", "danger")
    else:
        _ejecutar(w, "Materia creada.", services.crear_materia, w.db, _autor(w), nombre, area)
    return volver("/admin/estructura")


# ===================== Salones =====================

@router.get("/salones", response_class=HTMLResponse)
def salones(w: Web = Depends(requiere("estructura.gestionar"))):
    db = w.db
    anio = anio_activo(db)
    filas = []
    if anio is not None:
        total_materias = db.scalar(select(func.count()).select_from(Materia))
        for salon in db.scalars(select(Salon).where(Salon.anio_lectivo_id == anio.id).join(Grado)
                                .order_by(Grado.orden, Salon.nombre)).all():
            filas.append({
                "salon": salon,
                "alumnos": db.scalar(select(func.count()).select_from(Matricula).where(
                    Matricula.salon_id == salon.id, Matricula.estado == "activa")),
                "asignadas": db.scalar(select(func.count()).select_from(SalonMateriaDocente).where(
                    SalonMateriaDocente.salon_id == salon.id, SalonMateriaDocente.docente_id.is_not(None))),
                "total_materias": total_materias,
            })
    return render(w, "admin/salones.html", {
        "anio": anio, "filas": filas, "docentes": _docentes(db),
        "grados": db.scalars(select(Grado).order_by(Grado.orden)).all(),
        "paralelos": db.scalars(select(Paralelo).order_by(Paralelo.nombre)).all(),
    }, seccion="salones")


@router.post("/salones", dependencies=[Depends(verificar_csrf)])
def crear_salon(grado_id: str = Form(""), paralelo_id: str = Form(""), tutor_id: str = Form(""),
                w: Web = Depends(requiere("estructura.gestionar"))):
    grado, paralelo = entero(grado_id), entero(paralelo_id)
    if grado is None or paralelo is None:
        avisar(w, "Elija el grado y el paralelo.", "danger")
        return volver("/admin/salones")
    salon = _ejecutar(w, "Salón creado.", services.crear_salon, w.db, _autor(w), grado, paralelo, entero(tutor_id))
    return volver(f"/admin/salones/{salon.id}" if isinstance(salon, Salon) else "/admin/salones")


@router.get("/salones/{salon_id}", response_class=HTMLResponse)
def ver_salon(salon_id: int, w: Web = Depends(requiere("estructura.gestionar"))):
    db = w.db
    salon = db.get(Salon, salon_id)
    if salon is None:
        raise HTTPException(status_code=404)
    asignadas = {a.materia_id: a for a in db.scalars(select(SalonMateriaDocente)
                                                       .where(SalonMateriaDocente.salon_id == salon.id))}
    alumnos = db.scalars(select(Alumno).join(Matricula, Matricula.alumno_id == Alumno.id).where(
        Matricula.salon_id == salon.id, Matricula.estado == "activa").order_by(Alumno.nombre)).all()
    con_matricula = select(Matricula.alumno_id).where(Matricula.anio_lectivo_id == salon.anio_lectivo_id,
                                                      Matricula.estado == "activa")
    sin_salon = db.scalars(select(Alumno).where(Alumno.id.not_in(con_matricula)).order_by(Alumno.nombre).limit(200)).all()
    return render(w, "admin/salon.html", {
        "salon": salon, "materias": db.scalars(select(Materia).order_by(Materia.nombre)).all(),
        "asignadas": asignadas, "docentes": _docentes(db), "alumnos": alumnos, "sin_salon": sin_salon,
    }, seccion="salones")


@router.post("/salones/{salon_id}/tutor", dependencies=[Depends(verificar_csrf)])
def asignar_tutor(salon_id: int, tutor_id: str = Form(""), w: Web = Depends(requiere("estructura.gestionar"))):
    _ejecutar(w, "Tutor actualizado.", services.asignar_tutor, w.db, _autor(w), salon_id, entero(tutor_id))
    return volver(f"/admin/salones/{salon_id}")


@router.post("/salones/{salon_id}/asignaciones", dependencies=[Depends(verificar_csrf)])
def asignar_docente(salon_id: int, materia_id: str = Form(""), docente_id: str = Form(""),
                    w: Web = Depends(requiere("estructura.gestionar"))):
    materia = entero(materia_id)
    if materia is None:
        avisar(w, "Materia no válida.", "danger")
    else:
        _ejecutar(w, "Asignación guardada.", services.asignar_docente, w.db, _autor(w), salon_id, materia,
                  entero(docente_id))
    return volver(f"/admin/salones/{salon_id}")


@router.post("/salones/{salon_id}/matriculas", dependencies=[Depends(verificar_csrf)])
def matricular(salon_id: int, alumno_id: str = Form(""), w: Web = Depends(requiere("matriculas.gestionar"))):
    alumno = entero(alumno_id)
    if alumno is None:
        avisar(w, "Elija un alumno.", "danger")
    else:
        _ejecutar(w, "Alumno matriculado.", services.matricular, w.db, _autor(w), alumno, salon_id)
    return volver(f"/admin/salones/{salon_id}")


# ===================== Alumnos =====================

@router.get("/alumnos", response_class=HTMLResponse)
def alumnos(salon_id: str = "", q: str = "", pagina: str = "1", w: Web = Depends(requiere("alumnos.gestionar"))):
    db = w.db
    filtro_salon, numero = entero(salon_id), _pagina(pagina)
    consulta = select(Alumno, Salon.nombre).outerjoin(Salon, Salon.id == Alumno.salon_id)
    if filtro_salon is not None:
        consulta = consulta.where(Alumno.salon_id == filtro_salon)
    if q.strip():
        termino = f"%{q.strip()[:60]}%"
        consulta = consulta.where(or_(Alumno.nombre.ilike(termino), Alumno.codigo_interno.ilike(termino)))
    total = db.scalar(select(func.count()).select_from(consulta.subquery()))
    filas = db.execute(consulta.order_by(Salon.nombre, Alumno.nombre)
                       .offset((numero - 1) * POR_PAGINA).limit(POR_PAGINA)).all()
    con_cuenta = set(db.scalars(select(Usuario.persona_id).where(
        Usuario.persona_id.in_([a.persona_id for a, _ in filas]))))
    elegibles = set(db.scalars(select(Alumno.id).join(Salon, Salon.id == Alumno.salon_id).join(Grado)
                               .where(Alumno.id.in_([a.id for a, _ in filas]),
                                      Grado.orden >= w.settings.grado_minimo_cuenta_estudiante)))
    anio = anio_activo(db)
    return render(w, "admin/alumnos.html", {
        "filas": filas, "con_cuenta": con_cuenta, "elegibles": elegibles, "total": total, "pagina": numero,
        "paginas": max((total + POR_PAGINA - 1) // POR_PAGINA, 1),
        "salones": db.scalars(select(Salon).where(Salon.anio_lectivo_id == (anio.id if anio else -1))
                              .order_by(Salon.nombre)).all(),
        "salon_id": filtro_salon, "q": q,
    }, seccion="alumnos")


@router.post("/alumnos", dependencies=[Depends(verificar_csrf)])
def crear_alumno(codigo_interno: str = Form(""), nombres: str = Form(""), apellidos: str = Form(""),
                 salon_id: str = Form(""), w: Web = Depends(requiere("alumnos.gestionar"))):
    salon = entero(salon_id)
    if salon is None:
        avisar(w, "Elija el salón.", "danger")
    else:
        _ejecutar(w, "Alumno agregado y matriculado.", services.crear_alumno, w.db, _autor(w),
                  codigo_interno=codigo_interno, nombres=nombres, apellidos=apellidos, salon_id=salon)
    return volver("/admin/alumnos")


@router.post("/alumnos/{alumno_id}/activo", dependencies=[Depends(verificar_csrf)])
def cambiar_activo_alumno(alumno_id: int, activo: str = Form(""), w: Web = Depends(requiere("alumnos.gestionar"))):
    _ejecutar(w, "Estado actualizado.", services.cambiar_activo_alumno, w.db, _autor(w), alumno_id, activo == "1")
    return volver("/admin/alumnos")


@router.post("/alumnos/{alumno_id}/cuenta", dependencies=[Depends(verificar_csrf)], response_class=HTMLResponse)
def crear_cuenta_estudiante(alumno_id: int, w: Web = Depends(requiere("usuarios.gestionar"))):
    alumno = w.db.get(Alumno, alumno_id)
    if alumno is None:
        raise HTTPException(status_code=404)
    salon = w.db.get(Salon, alumno.salon_id) if alumno.salon_id else None
    if salon is None or salon.grado.orden < w.settings.grado_minimo_cuenta_estudiante:
        avisar(w, "Por decisión de la dirección, los estudiantes de grados menores acceden a través de su representante.",
               "danger")
        return volver("/admin/alumnos")
    try:
        usuario, token = services.crear_usuario(
            w.db, w.settings, _autor(w), nombres=alumno.persona.nombres, apellidos=alumno.persona.apellidos,
            email=None, roles=[("estudiante", None)], persona_id=alumno.persona_id)
    except services.ErrorNegocio as e:
        avisar(w, e.mensaje, "danger")
        return volver("/admin/alumnos")
    return _pantalla_enlace(w, usuario, "activacion", token)


@router.get("/alumnos/importar", response_class=HTMLResponse)
def ver_importar(w: Web = Depends(requiere("alumnos.gestionar"))):
    return render(w, "admin/importar.html", {"columnas": services.COLUMNAS_CSV}, seccion="alumnos")


@router.post("/alumnos/importar/vista-previa", dependencies=[Depends(verificar_csrf)], response_class=HTMLResponse)
def vista_previa(archivo: UploadFile = File(...), w: Web = Depends(requiere("alumnos.gestionar"))):
    try:
        texto = services.decodificar_csv(archivo.file.read(services.MAX_BYTES_CSV + 1))
        filas = services.analizar_csv(w.db, texto)
    except services.ErrorNegocio as e:
        avisar(w, e.mensaje, "danger")
        return volver("/admin/alumnos/importar")
    conteo = {estado: sum(1 for f in filas if f.estado == estado) for estado in ("valida", "duplicada", "error")}
    return render(w, "admin/importar_vista.html", {"filas": filas, "conteo": conteo, "texto": texto},
                  seccion="alumnos")


@router.post("/alumnos/importar", dependencies=[Depends(verificar_csrf)])
def confirmar_importacion(texto: str = Form(""), w: Web = Depends(requiere("alumnos.gestionar"))):
    # El archivo se vuelve a analizar: no se confía en lo que se mostró en la vista previa
    try:
        creados, omitidos = services.importar_alumnos(w.db, _autor(w), texto)
    except services.ErrorNegocio as e:
        avisar(w, e.mensaje, "danger")
        return volver("/admin/alumnos/importar")
    avisar(w, f"Se importaron y matricularon {creados} alumnos. Filas omitidas: {omitidos}.")
    return volver("/admin/alumnos")


# ===================== Usuarios =====================

def _estado_usuario(usuario: Usuario) -> tuple[str, str]:
    if not usuario.activo:
        return "Inactivo", "secondary"
    if usuario.password_hash is None:
        return "Pendiente de activar", "warning"
    if usuario.bloqueado_hasta is not None and aware(usuario.bloqueado_hasta) > ahora():
        return "Bloqueado temporalmente", "danger"
    return "Activo", "success"


@router.get("/usuarios", response_class=HTMLResponse)
def usuarios(rol: str = "", q: str = "", pagina: str = "1", w: Web = Depends(requiere("usuarios.gestionar"))):
    db = w.db
    numero = _pagina(pagina)
    consulta = select(Usuario).join(Usuario.persona)
    if rol in ROLES:
        consulta = consulta.where(Usuario.id.in_(select(UsuarioRol.usuario_id).where(UsuarioRol.rol == rol)))
    if q.strip():
        termino = f"%{q.strip()[:60]}%"
        consulta = consulta.where(or_(Usuario.login.ilike(termino), Persona.nombres.ilike(termino),
                                      Persona.apellidos.ilike(termino)))
    total = db.scalar(select(func.count()).select_from(consulta.subquery()))
    lista = db.scalars(consulta.order_by(Usuario.login).offset((numero - 1) * POR_PAGINA).limit(POR_PAGINA)).unique().all()
    return render(w, "admin/usuarios.html", {
        "usuarios": [(u, _estado_usuario(u), enmascarar_email(u.email)) for u in lista],
        "total": total, "pagina": numero, "paginas": max((total + POR_PAGINA - 1) // POR_PAGINA, 1),
        "rol": rol, "q": q, "roles": ROLES,
    }, seccion="usuarios")


@router.get("/usuarios/nuevo", response_class=HTMLResponse)
def nuevo_usuario(w: Web = Depends(requiere("usuarios.gestionar"))):
    return render(w, "admin/usuario_nuevo.html", {
        "roles": [r for r in ROLES if r != "estudiante"],
        "areas": w.db.scalars(select(Area).order_by(Area.nombre)).all(),
    }, seccion="usuarios")


@router.post("/usuarios", dependencies=[Depends(verificar_csrf)], response_class=HTMLResponse)
def crear_usuario(nombres: str = Form(""), apellidos: str = Form(""), email: str = Form(""), login: str = Form(""),
                  roles: list[str] = Form([]), area_id: str = Form(""),
                  w: Web = Depends(requiere("usuarios.gestionar"))):
    if "estudiante" in roles:
        avisar(w, "Las cuentas de estudiante se crean desde la lista de alumnos.", "danger")
        return volver("/admin/usuarios/nuevo")
    try:
        usuario, token = services.crear_usuario(
            w.db, w.settings, _autor(w), nombres=nombres, apellidos=apellidos, email=email, login=login,
            roles=[(r, entero(area_id) if r == "directivo" else None) for r in roles])
    except services.ErrorNegocio as e:
        avisar(w, e.mensaje, "danger")
        return volver("/admin/usuarios/nuevo")
    return _pantalla_enlace(w, usuario, "activacion", token)


def _pantalla_enlace(w: Web, usuario: Usuario, tipo: str, token: str) -> HTMLResponse:
    ruta = "activar" if tipo == "activacion" else "restablecer"
    url = f"{str(w.request.base_url).rstrip('/')}/cuenta/{ruta}/{token}"
    mensaje = (f"Hola, {usuario.persona.nombres}. La escuela creó su acceso al portal. Su usuario es "
               f"«{usuario.login}». Para elegir su contraseña abra este enlace (sirve una sola vez): {url}"
               if tipo == "activacion" else
               f"Hola, {usuario.persona.nombres}. Para elegir una nueva contraseña del portal abra este enlace "
               f"(sirve una sola vez): {url}")
    horas = w.settings.activacion_horas if tipo == "activacion" else None
    return render(w, "admin/enlace_cuenta.html", {"usuario_cuenta": usuario, "url": url, "mensaje": mensaje,
                                                  "tipo": tipo, "horas": horas,
                                                  "minutos": w.settings.recuperacion_minutos}, seccion="usuarios")


@router.get("/usuarios/{usuario_id}", response_class=HTMLResponse)
def ver_usuario(usuario_id: int, w: Web = Depends(requiere("usuarios.gestionar"))):
    db = w.db
    usuario = db.get(Usuario, usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404)
    representados = db.execute(
        select(RepresentanteAlumno, Alumno).join(Alumno, Alumno.id == RepresentanteAlumno.alumno_id)
        .where(RepresentanteAlumno.usuario_id == usuario.id).order_by(Alumno.nombre)).all()
    return render(w, "admin/usuario.html", {
        "u": usuario, "estado_cuenta": _estado_usuario(usuario), "email": enmascarar_email(usuario.email),
        "roles": ROLES, "areas": db.scalars(select(Area).order_by(Area.nombre)).all(),
        "representados": representados,
        "es_representante": any(r.rol == "representante" for r in usuario.roles),
        "alumnos": db.execute(select(Alumno, Salon.nombre).outerjoin(Salon, Salon.id == Alumno.salon_id)
                              .where(Alumno.activo.is_(True)).order_by(Salon.nombre, Alumno.nombre)).all(),
        "sesiones_activas": len(sesiones.activas(db, w.settings, usuario.id)),
    }, seccion="usuarios")


@router.post("/usuarios/{usuario_id}/roles", dependencies=[Depends(verificar_csrf)])
def asignar_rol(usuario_id: int, rol: str = Form(""), area_id: str = Form(""),
                w: Web = Depends(requiere("usuarios.gestionar"))):
    _ejecutar(w, "Rol asignado.", services.asignar_rol, w.db, _autor(w), usuario_id, rol, entero(area_id))
    return volver(f"/admin/usuarios/{usuario_id}")


@router.post("/usuarios/{usuario_id}/roles/{rol_id}/quitar", dependencies=[Depends(verificar_csrf)])
def quitar_rol(usuario_id: int, rol_id: int, w: Web = Depends(requiere("usuarios.gestionar"))):
    registro = w.db.get(UsuarioRol, rol_id)
    if registro is None or registro.usuario_id != usuario_id:
        raise HTTPException(status_code=404)
    _ejecutar(w, "Rol quitado.", services.quitar_rol, w.db, _autor(w), rol_id)
    return volver(f"/admin/usuarios/{usuario_id}")


@router.post("/usuarios/{usuario_id}/activo", dependencies=[Depends(verificar_csrf)])
def cambiar_activo_usuario(usuario_id: int, activo: str = Form(""), w: Web = Depends(requiere("usuarios.gestionar"))):
    _ejecutar(w, "Estado actualizado.", services.cambiar_activo_usuario, w.db, _autor(w), usuario_id, activo == "1")
    return volver(f"/admin/usuarios/{usuario_id}")


@router.post("/usuarios/{usuario_id}/enlace", dependencies=[Depends(verificar_csrf)], response_class=HTMLResponse)
def enlace_cuenta(usuario_id: int, w: Web = Depends(requiere("usuarios.gestionar"))):
    try:
        usuario, tipo, token = services.generar_enlace_cuenta(w.db, w.settings, _autor(w), usuario_id)
    except services.ErrorNegocio as e:
        avisar(w, e.mensaje, "danger")
        return volver(f"/admin/usuarios/{usuario_id}")
    return _pantalla_enlace(w, usuario, tipo, token)


@router.post("/usuarios/{usuario_id}/sesiones/cerrar", dependencies=[Depends(verificar_csrf)])
def cerrar_sesiones(usuario_id: int, w: Web = Depends(requiere("usuarios.gestionar"))):
    cerradas = services.cerrar_sesiones_usuario(w.db, _autor(w), usuario_id)
    avisar(w, f"Se cerraron {cerradas} sesiones.")
    return volver(f"/admin/usuarios/{usuario_id}")


@router.post("/usuarios/{usuario_id}/representados", dependencies=[Depends(verificar_csrf)])
def vincular_representado(usuario_id: int, alumno_id: str = Form(""), parentesco: str = Form(""),
                          w: Web = Depends(requiere("representantes.gestionar"))):
    alumno = entero(alumno_id)
    if alumno is None:
        avisar(w, "Elija un alumno.", "danger")
    else:
        _ejecutar(w, "Representado vinculado y validado.", services.vincular_representante, w.db, _autor(w),
                  usuario_id, alumno, parentesco)
    return volver(f"/admin/usuarios/{usuario_id}")


@router.post("/usuarios/{usuario_id}/representados/{vinculo_id}/quitar", dependencies=[Depends(verificar_csrf)])
def desvincular_representado(usuario_id: int, vinculo_id: int, w: Web = Depends(requiere("representantes.gestionar"))):
    vinculo = w.db.get(RepresentanteAlumno, vinculo_id)
    if vinculo is None or vinculo.usuario_id != usuario_id:
        raise HTTPException(status_code=404)
    _ejecutar(w, "Vínculo eliminado.", services.desvincular_representante, w.db, _autor(w), vinculo_id)
    return volver(f"/admin/usuarios/{usuario_id}")


# ===================== Autorizaciones y recogida =====================

GESTION = "autorizaciones.gestionar"


@router.get("/campanas", response_class=HTMLResponse)
def campanas(w: Web = Depends(requiere(GESTION))):
    momento = ahora()
    anio = anio_activo(w.db)
    filas = [{"campana": c, "vigente": aware(c.fecha_limite) > momento,
              "totales": services.totalizar(services.progreso_campana(w.db, c.id))}
             for c in w.db.scalars(select(Campana).order_by(Campana.fecha_limite.desc())).all()]
    return render(w, "admin/campanas.html", {
        "filas": filas,
        "salones": w.db.scalars(select(Salon).where(Salon.anio_lectivo_id == (anio.id if anio else -1))
                                .order_by(Salon.nombre)).all(),
    }, seccion="campanas")


@router.post("/campanas", dependencies=[Depends(verificar_csrf)])
def crear_campana(titulo: str = Form(""), descripcion: str = Form(""), tipo: str = Form(""),
                  texto_legal: str = Form(""), fecha_limite: str = Form(""), salon_ids: list[int] = Form([]),
                  w: Web = Depends(requiere(GESTION))):
    try:
        limite = desde_formulario(fecha_limite)
    except ValueError:
        avisar(w, "Indique la fecha y hora límite.", "danger")
        return volver("/admin/campanas")
    if tipo not in ("salida", "vacuna", "evento", "otro"):
        avisar(w, "Elija el tipo de autorización.", "danger")
        return volver("/admin/campanas")
    if limite <= ahora():
        avisar(w, "La fecha límite debe ser futura.", "danger")
        return volver("/admin/campanas")
    try:
        campana, _ = services.crear_campana(w.db, _autor(w), titulo=titulo, descripcion=descripcion, tipo=tipo,
                                            texto_legal=texto_legal, fecha_limite=limite, salon_ids=salon_ids)
    except services.ErrorNegocio as e:
        avisar(w, e.mensaje, "danger")
        return volver("/admin/campanas")
    avisar(w, "Campaña creada. Ya aparece en los enlaces de las familias de esos salones.")
    return volver(f"/admin/campanas/{campana.id}")


def _alumnos_de_campana(db: Session, campana_id: int):
    return db.execute(
        select(Alumno, Salon.nombre, RespuestaAutorizacion)
        .join(Salon, Salon.id == Alumno.salon_id)
        .join(CampanaSalon, (CampanaSalon.salon_id == Alumno.salon_id) & (CampanaSalon.campana_id == campana_id))
        .outerjoin(RespuestaAutorizacion, (RespuestaAutorizacion.alumno_id == Alumno.id)
                   & (RespuestaAutorizacion.campana_id == campana_id))
        .where(Alumno.activo.is_(True)).order_by(Salon.nombre, Alumno.nombre)
    ).all()


@router.get("/campanas/{campana_id}", response_class=HTMLResponse)
def ver_campana(campana_id: int, estado: str = "", w: Web = Depends(requiere(GESTION))):
    campana = w.db.get(Campana, campana_id)
    if campana is None:
        raise HTTPException(status_code=404)
    progreso = services.progreso_campana(w.db, campana_id)
    filas = _alumnos_de_campana(w.db, campana_id)
    if estado == "pendiente":
        filas = [f for f in filas if f[2] is None]
    elif estado in ("aceptada", "rechazada"):
        filas = [f for f in filas if f[2] is not None and f[2].decision == estado]
    return render(w, "admin/campana.html", {
        "campana": campana, "progreso": progreso, "totales": services.totalizar(progreso), "filas": filas,
        "estado": estado, "vigente": aware(campana.fecha_limite) > ahora(),
    }, seccion="campanas")


def _celda_segura(valor) -> str:
    """Evita que Excel interprete como fórmula un texto escrito por una familia (inyección CSV)."""
    texto = "" if valor is None else str(valor)
    return "'" + texto if texto[:1] in ("=", "+", "-", "@", "\t", "\r") else texto


@router.get("/campanas/{campana_id}/csv")
def exportar_campana(campana_id: int, w: Web = Depends(requiere(GESTION))):
    if w.db.get(Campana, campana_id) is None:
        raise HTTPException(status_code=404)
    salida = io.StringIO()
    escritor = csv.writer(salida, delimiter=";")
    escritor.writerow(["salon", "codigo_interno", "alumno", "estado", "firmante", "parentesco",
                       "fecha_hora_ecuador", "hash_texto"])
    for alumno, salon, respuesta in _alumnos_de_campana(w.db, campana_id):
        escritor.writerow([_celda_segura(v) for v in (
            salon, alumno.codigo_interno, alumno.nombre,
            respuesta.decision if respuesta else "pendiente",
            respuesta.firmante if respuesta else "", respuesta.parentesco if respuesta else "",
            local(respuesta.creada_en) if respuesta else "", respuesta.texto_hash if respuesta else "",
        )])
    registrar(w.db, usuario_id=w.actor.id, rol=w.actor.rol, accion="exportar", entidad="campana",
              entidad_id=campana_id, ip=w.ip)
    w.db.commit()
    return Response(content="﻿" + salida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="campana-{campana_id}.csv"'})


@router.get("/adultos", response_class=HTMLResponse)
def adultos(estado: str = "pendiente", w: Web = Depends(requiere(GESTION))):
    if estado not in ESTADOS_ADULTO:
        estado = "pendiente"
    filas = w.db.execute(
        select(AdultoAutorizado, Alumno.nombre, Salon.nombre)
        .join(Alumno, Alumno.id == AdultoAutorizado.alumno_id).outerjoin(Salon, Salon.id == Alumno.salon_id)
        .where(AdultoAutorizado.estado == estado)
        .order_by(Salon.nombre, Alumno.nombre, AdultoAutorizado.nombre).limit(500)
    ).all()
    return render(w, "admin/adultos.html", {"filas": filas, "estado": estado, "estados": ESTADOS_ADULTO},
                  seccion="adultos")


@router.post("/adultos/{adulto_id}/estado", dependencies=[Depends(verificar_csrf)])
def decidir_adulto(adulto_id: int, estado: str = Form(""), volver_a: str = Form("pendiente"),
                   w: Web = Depends(requiere(GESTION))):
    try:
        adulto = services.decidir_adulto(w.db, _autor(w), adulto_id, estado)
        # El aviso nombra a la persona, para que sea claro en una lista larga
        avisar(w, f"{adulto.nombre}: {'validado' if adulto.estado == 'validado' else 'rechazado'}.")
    except services.ErrorNegocio as e:
        avisar(w, e.mensaje, "danger")
    destino = volver_a if volver_a in ESTADOS_ADULTO else "pendiente"
    return volver(f"/admin/adultos?estado={destino}")


def _enlace_whatsapp(telefono: str | None, mensaje: str) -> str | None:
    """Enlace wa.me para enviar el mensaje desde el WhatsApp de la escuela. Asume números de Ecuador."""
    digitos = re.sub(r"\D", "", telefono or "")
    if not digitos:
        return None
    if digitos.startswith("0"):
        digitos = "593" + digitos[1:]
    return f"https://wa.me/{digitos}?text={quote(mensaje)}"


@router.get("/familias", response_class=HTMLResponse)
def familias(pagina: str = "1", q: str = "", w: Web = Depends(requiere(GESTION))):
    db = w.db
    numero = _pagina(pagina)
    consulta = select(Familia)
    if q.strip():
        consulta = consulta.where(Familia.nombre_familia.ilike(f"%{q.strip()[:60]}%"))
    total = db.scalar(select(func.count()).select_from(consulta.subquery()))
    lista = db.scalars(consulta.order_by(Familia.nombre_familia).offset((numero - 1) * POR_PAGINA)
                       .limit(POR_PAGINA)).all()
    ids = [f.id for f in lista]
    vinculos: dict[int, list[str]] = {}
    for familia_id, nombre in db.execute(select(FamiliaAlumno.familia_id, Alumno.nombre)
                                         .join(Alumno, Alumno.id == FamiliaAlumno.alumno_id)
                                         .where(FamiliaAlumno.familia_id.in_(ids)).order_by(Alumno.nombre)).all():
        vinculos.setdefault(familia_id, []).append(nombre)
    momento = ahora()
    vencimientos = {fid: exp for fid, exp in db.execute(
        select(TokenAcceso.familia_id, func.max(TokenAcceso.expira_en)).where(TokenAcceso.familia_id.in_(ids))
        .group_by(TokenAcceso.familia_id)).all() if aware(exp) > momento}
    return render(w, "admin/familias.html", {
        "familias": lista, "vinculos": vinculos, "vencimientos": vencimientos, "q": q,
        "total": total, "pagina": numero, "paginas": max((total + POR_PAGINA - 1) // POR_PAGINA, 1),
        "alumnos": db.execute(select(Alumno, Salon.nombre).join(Salon, Salon.id == Alumno.salon_id)
                              .where(Alumno.activo.is_(True)).order_by(Salon.nombre, Alumno.nombre)).all(),
    }, seccion="familias")


@router.post("/familias", dependencies=[Depends(verificar_csrf)])
def crear_familia(nombre_familia: str = Form(""), telefono: str = Form(""), email: str = Form(""),
                  consentimiento: str = Form(""), w: Web = Depends(requiere(GESTION))):
    _ejecutar(w, "Familia agregada.", services.crear_familia, w.db, _autor(w), nombre_familia=nombre_familia,
              telefono=telefono, email=email, consentimiento_mensajes=consentimiento == "on")
    return volver("/admin/familias")


@router.post("/familias/{familia_id}/alumnos", dependencies=[Depends(verificar_csrf)])
def vincular(familia_id: int, alumno_id: str = Form(""), w: Web = Depends(requiere(GESTION))):
    alumno = entero(alumno_id)
    if alumno is None:
        avisar(w, "Elija un alumno.", "danger")
    else:
        _ejecutar(w, "Alumno vinculado a la familia.", services.vincular_alumno, w.db, _autor(w), familia_id, alumno)
    return volver("/admin/familias")


@router.post("/familias/{familia_id}/enlace", dependencies=[Depends(verificar_csrf)], response_class=HTMLResponse)
def generar_enlace(familia_id: int, w: Web = Depends(requiere(GESTION))):
    try:
        token, expira = services.generar_enlace(w.db, w.settings, _autor(w), familia_id)
    except services.ErrorNegocio as e:
        avisar(w, e.mensaje, "danger")
        return volver("/admin/familias")
    familia = w.db.get(Familia, familia_id)
    url = f"{str(w.request.base_url).rstrip('/')}/f/{token}"
    mensaje = (f"Hola, {familia.nombre_familia}. La escuela le comparte su enlace personal para revisar "
               f"autorizaciones y registrar a las personas que pueden recoger a sus hijos: {url}")
    return render(w, "admin/enlace.html", {"familia": familia, "url": url, "expira": expira, "mensaje": mensaje,
                                           "whatsapp": _enlace_whatsapp(familia.telefono, mensaje)},
                  seccion="familias")


# ===================== Sistema =====================

@router.get("/bitacora", response_class=HTMLResponse)
def bitacora(accion: str = "", pagina: str = "1", w: Web = Depends(requiere("bitacora.ver"))):
    numero = _pagina(pagina)
    consulta = select(BitacoraAuditoria, Usuario).outerjoin(Usuario, Usuario.id == BitacoraAuditoria.usuario_id)
    if accion.strip():
        consulta = consulta.where(BitacoraAuditoria.accion == accion.strip()[:60])
    filas = w.db.execute(consulta.order_by(BitacoraAuditoria.id.desc()).offset((numero - 1) * 50).limit(51)).all()
    if w.actor.rol == "auditor":
        # Los accesos del auditor también quedan auditados
        registrar(w.db, usuario_id=w.actor.id, rol=w.actor.rol, accion="consultar_bitacora", entidad="bitacora",
                  cambio={"pagina": numero, "accion": accion or None}, ip=w.ip)
        w.db.commit()
    acciones = w.db.scalars(select(BitacoraAuditoria.accion).distinct().order_by(BitacoraAuditoria.accion)).all()
    return render(w, "admin/bitacora.html", {"filas": filas[:50], "hay_mas": len(filas) > 50, "pagina": numero,
                                              "accion": accion, "acciones": acciones}, seccion="bitacora")


@router.get("/envios", response_class=HTMLResponse)
def envios(w: Web = Depends(requiere("envios.ver"))):
    if not w.settings.demo_mode:
        raise HTTPException(status_code=404)
    lista = w.db.scalars(select(EnvioSaliente).order_by(EnvioSaliente.id.desc()).limit(50)).all()
    return render(w, "admin/envios.html", {"envios": lista}, seccion="envios")
