"""Datos ficticios de la demostración: ninguna persona de esta lista existe.

Genera la estructura de una escuela primaria (régimen Costa, Ecuador): 3 niveles, 6 salones,
120 alumnos con sus representantes, personal de cada rol, asignaciones de docentes, y el módulo de
autorizaciones con campañas y respuestas. La generación es determinista (semilla fija).
"""

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import crypto, security, services
from app.audit import registrar
from app.config import Settings
from app.models import (
    AdultoAutorizado,
    Alumno,
    AnioLectivo,
    Area,
    Campana,
    CampanaSalon,
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
from app.tiempo import ahora

NOMBRES_NINAS = ["Valentina", "Camila", "Isabella", "Emilia", "Martina", "Sofía", "Renata", "Luciana", "Antonella", "Victoria"]
NOMBRES_NINOS = ["Mateo", "Santiago", "Sebastián", "Matías", "Nicolás", "Joaquín", "Emiliano", "Benjamín", "Thiago", "Gabriel"]
NOMBRES_MUJERES = ["María", "Carmen", "Rosa", "Ana", "Patricia", "Elena", "Verónica", "Gloria", "Mónica", "Lucía"]
NOMBRES_HOMBRES = ["José", "Luis", "Jorge", "Carlos", "Miguel", "Fernando", "Ricardo", "Andrés", "Diego", "Pablo"]
APELLIDOS = [
    "Andrade", "Benítez", "Cedeño", "Delgado", "Espinoza", "Franco", "Guerrero", "Herrera", "Intriago", "Jaramillo",
    "León", "Macías", "Navarrete", "Ordóñez", "Peñafiel", "Quiñónez", "Rivadeneira", "Salazar", "Torres", "Villacís",
]
PARENTESCOS = {
    "madre": NOMBRES_MUJERES, "abuela": NOMBRES_MUJERES, "tía": NOMBRES_MUJERES,
    "padre": NOMBRES_HOMBRES, "abuelo": NOMBRES_HOMBRES, "tío": NOMBRES_HOMBRES,
}

NIVELES = [
    ("Preparatoria", [("1.º", 1)]),
    ("Básica Elemental", [("2.º", 2), ("3.º", 3), ("4.º", 4)]),
    ("Básica Media", [("5.º", 5), ("6.º", 6), ("7.º", 7)]),
]
SALONES = [("1.º", "A"), ("3.º", "A"), ("4.º", "A"), ("5.º", "A"), ("5.º", "B"), ("7.º", "A")]
ALUMNOS_POR_SALON = 20
AREAS = {
    "Lengua y Literatura": ["Lengua y Literatura"],
    "Matemática": ["Matemática"],
    "Ciencias Naturales": ["Ciencias Naturales"],
    "Estudios Sociales": ["Estudios Sociales"],
    "Educación Cultural y Artística": ["Educación Cultural y Artística"],
    "Educación Física": ["Educación Física"],
    "Lengua Extranjera": ["Inglés"],
}

TEXTO_DEMO = (
    "TEXTO DE DEMOSTRACIÓN. En una escuela real, este texto lo redacta y aprueba el abogado de la institución.\n\n"
    "Yo, representante del alumno indicado, autorizo su participación en la actividad descrita, organizada por la "
    "escuela en la fecha señalada, bajo supervisión del personal docente. Declaro haber leído las condiciones de la "
    "actividad y que la información que proporciono es verdadera."
)


@dataclass
class _Contexto:
    db: Session
    settings: Settings
    password_hash: str
    rng: random.Random
    resumen: dict = field(default_factory=dict)


def _usuario(c: _Contexto, nombres: str, apellidos: str, login: str, roles: list[tuple[str, int | None]],
             email: str | None = None, totp: bool = False, persona_id: int | None = None) -> Usuario:
    if persona_id is None:
        persona = Persona(nombres=nombres, apellidos=apellidos)
        c.db.add(persona)
        c.db.flush()
        persona_id = persona.id
    usuario = Usuario(persona_id=persona_id, login=login, email=email, password_hash=c.password_hash,
                      activado_en=ahora())
    if totp:
        # Secreto pre-configurado: en la demo, el código vigente se muestra en la pantalla de acceso
        usuario.totp_secret_cifrado = crypto.cifrar(security.new_totp_secret(), c.settings.encryption_key)
    c.db.add(usuario)
    c.db.flush()
    for rol, area_id in roles:
        c.db.add(UsuarioRol(usuario_id=usuario.id, rol=rol, area_id=area_id))
    return usuario


def poblar(db: Session, *, settings: Settings, password: str, semilla: int = 2026) -> dict:
    if db.scalar(select(func.count()).select_from(Salon)):
        raise RuntimeError("La base ya tiene datos. Use --reset para borrarla y volver a poblarla.")
    if not settings.encryption_key:
        raise RuntimeError("Defina ENCRYPTION_KEY antes de poblar la base.")

    c = _Contexto(db=db, settings=settings, password_hash=security.hash_password(password), rng=random.Random(semilla))
    momento = ahora()
    rng = c.rng

    # ---------- Estructura académica ----------
    anio = AnioLectivo(nombre="2026-2027", inicio=date(2026, 5, 4), fin=date(2027, 2, 5), activo=True)
    db.add(anio)
    db.flush()
    for nombre, inicio, fin in [("Primer trimestre", date(2026, 5, 4), date(2026, 8, 14)),
                                ("Segundo trimestre", date(2026, 8, 17), date(2026, 11, 13)),
                                ("Tercer trimestre", date(2026, 11, 16), date(2027, 2, 5))]:
        db.add(PeriodoCalificacion(anio_lectivo_id=anio.id, nombre=nombre, inicio=inicio, fin=fin))

    grados: dict[str, Grado] = {}
    for orden_nivel, (nombre_nivel, lista) in enumerate(NIVELES, start=1):
        nivel = Nivel(nombre=nombre_nivel, orden=orden_nivel)
        db.add(nivel)
        db.flush()
        for nombre_grado, orden in lista:
            grado = Grado(nivel_id=nivel.id, nombre=nombre_grado, orden=orden)
            db.add(grado)
            grados[nombre_grado] = grado
    paralelos = {n: Paralelo(nombre=n) for n in ("A", "B")}
    db.add_all(paralelos.values())

    areas: dict[str, Area] = {}
    materias: list[Materia] = []
    for nombre_area, lista in AREAS.items():
        area = Area(nombre=nombre_area)
        db.add(area)
        db.flush()
        areas[nombre_area] = area
        for nombre_materia in lista:
            materia = Materia(nombre=nombre_materia, area_id=area.id)
            db.add(materia)
            materias.append(materia)
    db.flush()

    # ---------- Personal ----------
    _usuario(c, "Administración", "General", "admin", [("administrador", None)], "admin@demo.test", totp=True)
    sofia = _usuario(c, "Sofía", "Andrade Ruiz", "sofia", [("coordinacion", None)], "sofia@demo.test", totp=True)
    _usuario(c, "Ricardo", "Salazar Mora", "rector", [("directivo", None)], "rector@demo.test", totp=True)
    _usuario(c, "Gloria", "Franco Vera", "dir.matematica", [("directivo", areas["Matemática"].id)],
             "dir.matematica@demo.test", totp=True)
    _usuario(c, "Patricia", "Ordóñez Lima", "consejeria", [("consejeria", None)], "consejeria@demo.test", totp=True)
    _usuario(c, "Andrés", "Benítez Paz", "auditor", [("auditor", None)], "auditor@demo.test", totp=True)
    _usuario(c, "Jorge", "Cedeño Alava", "porteria", [("porteria", None)], "porteria@demo.test")

    # La docente principal de la demo también es representante (muestra el cambio de rol)
    docentes = [_usuario(c, "Lucía", "Herrera Paz", "docente", [("docente", None), ("representante", None)],
                         "docente@demo.test", totp=True)]
    for i, (nombres, apellidos) in enumerate([
        ("Carmen", "León Ortiz"), ("Miguel", "Torres Vega"), ("Elena", "Macías Ruiz"), ("Pablo", "Intriago Sol"),
        ("Mónica", "Navarrete Gil"), ("Diego", "Quiñónez Mar"), ("Verónica", "Villacís Luna"),
    ], start=2):
        docentes.append(_usuario(c, nombres, apellidos, f"docente{i}", [("docente", None)],
                                 f"docente{i}@demo.test", totp=True))

    # ---------- Salones y asignaciones ----------
    salones: list[Salon] = []
    for i, (grado, paralelo) in enumerate(SALONES):
        salon = Salon(anio_lectivo_id=anio.id, grado_id=grados[grado].id, paralelo_id=paralelos[paralelo].id,
                      nombre=f"{grado} {paralelo}", tutor_id=docentes[i].id)
        db.add(salon)
        salones.append(salon)
    db.flush()
    principal = docentes[0]  # tutora de 1.º A en la lista; se reasigna a 5.º A abajo
    salones[0].tutor_id, salones[3].tutor_id = salones[3].tutor_id, principal.id
    for salon in salones:
        for j, materia in enumerate(materias):
            if materia.nombre in ("Matemática", "Ciencias Naturales") and salon.nombre in ("5.º A", "5.º B"):
                docente = principal
            else:
                docente = docentes[1 + (salon.id + j) % (len(docentes) - 1)]
            db.add(SalonMateriaDocente(salon_id=salon.id, materia_id=materia.id, docente_id=docente.id))
    db.flush()

    # ---------- Alumnos, familias y representantes ----------
    familias: list[Familia] = []
    hijos: dict[int, list[Alumno]] = {}
    representante_de: dict[int, Usuario] = {}
    alumnos: list[Alumno] = []
    numero = 0
    for salon in salones:
        for _ in range(ALUMNOS_POR_SALON):
            numero += 1
            apellido_1, apellido_2 = rng.sample(APELLIDOS, 2)
            familia = None
            if familias and rng.random() < 0.1:
                candidata = rng.choice(familias)
                if len(hijos[candidata.id]) == 1 and hijos[candidata.id][0].salon_id != salon.id:
                    familia = candidata
                    apellido_1, apellido_2 = candidata.nombre_familia.removeprefix("Familia ").split(" ", 1)
            nombre = rng.choice(NOMBRES_NINAS if rng.random() < 0.5 else NOMBRES_NINOS)
            persona = Persona(nombres=nombre, apellidos=f"{apellido_1} {apellido_2}")
            db.add(persona)
            db.flush()
            alumno = Alumno(persona_id=persona.id, codigo_interno=f"EST-{numero:03d}", salon_id=salon.id)
            db.add(alumno)
            db.flush()
            db.add(Matricula(alumno_id=alumno.id, salon_id=salon.id, anio_lectivo_id=anio.id))
            if familia is None:
                familia = Familia(nombre_familia=f"Familia {apellido_1} {apellido_2}",
                                  telefono=f"09000{numero:05d}", consentimiento_mensajes=rng.random() < 0.9)
                db.add(familia)
                db.flush()
                familias.append(familia)
                hijos[familia.id] = []
            db.add(FamiliaAlumno(familia_id=familia.id, alumno_id=alumno.id))
            hijos[familia.id].append(alumno)
            alumnos.append(alumno)
    db.flush()

    # Un representante con cuenta por familia; el primero es el usuario «representante» de la demo
    con_hermanos = [f for f in familias if len(hijos[f.id]) > 1]
    orden_familias = con_hermanos[:1] + [f for f in familias if f not in con_hermanos[:1]]
    for indice, familia in enumerate(orden_familias):
        apellido = familia.nombre_familia.removeprefix("Familia ").split(" ")[0]
        parentesco = rng.choice(["madre", "padre"])
        nombres = rng.choice(PARENTESCOS[parentesco])
        login = "representante" if indice == 0 else services.sugerir_login(db, nombres, apellido)
        rep = _usuario(c, nombres, f"{apellido} {rng.choice(APELLIDOS)}", login, [("representante", None)],
                       f"{login}@demo.test")
        representante_de[familia.id] = rep
        for alumno in hijos[familia.id]:
            db.add(RepresentanteAlumno(usuario_id=rep.id, alumno_id=alumno.id, parentesco=parentesco,
                                       validado_por=sofia.id, validado_en=momento))
    # La docente principal también representa a un alumno de 1.º A
    hijo_docente = next(a for a in alumnos if a.salon_id == salones[0].id)
    db.add(RepresentanteAlumno(usuario_id=principal.id, alumno_id=hijo_docente.id, parentesco="madre",
                               validado_por=sofia.id, validado_en=momento))

    # Cuentas de estudiante desde 5.º (decisión de la dirección)
    con_cuenta = [a for a in alumnos if db.get(Salon, a.salon_id).grado.orden >= settings.grado_minimo_cuenta_estudiante]
    for indice, alumno in enumerate(con_cuenta):
        persona = db.get(Persona, alumno.persona_id)
        login = "estudiante" if indice == 0 else services.sugerir_login(db, persona.nombres, persona.apellidos)
        _usuario(c, persona.nombres, persona.apellidos, login, [("estudiante", None)], persona_id=persona.id)
    db.flush()

    # ---------- Autorizaciones y recogida ----------
    adultos_de: dict[int, list[AdultoAutorizado]] = {}
    for alumno in alumnos:
        familia = next(f for f in familias if alumno in hijos[f.id])
        apellido = familia.nombre_familia.removeprefix("Familia ").split(" ")[0]
        lista = []
        for parentesco in rng.sample(list(PARENTESCOS), 2):
            estado = "validado" if rng.random() < 0.88 else "pendiente"
            adulto = AdultoAutorizado(alumno_id=alumno.id, familia_id=familia.id, estado=estado,
                                      nombre=f"{rng.choice(PARENTESCOS[parentesco])} {apellido}", parentesco=parentesco,
                                      validado_por=sofia.id if estado == "validado" else None)
            db.add(adulto)
            lista.append(adulto)
        adultos_de[alumno.id] = lista
    db.flush()

    campanas = []
    for titulo, tipo, destino, dias, descripcion in [
        ("Salida pedagógica al Parque Histórico", "salida", [s for s in salones if s.grado.orden >= 5], 7,
         "Visita guiada de medio día. Transporte contratado por la escuela."),
        ("Jornada de vacunación (demostración)", "vacuna", [s for s in salones if s.grado.orden < 5], 3,
         "Vacunación en la escuela con personal de salud acreditado."),
    ]:
        campana = Campana(titulo=titulo, descripcion=descripcion, tipo=tipo, texto_legal=TEXTO_DEMO,
                          texto_hash=services.hash_texto(TEXTO_DEMO), fecha_limite=momento + timedelta(days=dias),
                          creado_por=sofia.id)
        db.add(campana)
        db.flush()
        for salon in destino:
            db.add(CampanaSalon(campana_id=campana.id, salon_id=salon.id))
        campanas.append((campana, {s.id for s in destino}))

    respuestas = 0
    for campana, salon_ids in campanas:
        for alumno in alumnos:
            if alumno.salon_id not in salon_ids or rng.random() > 0.6:
                continue
            familia = next(f for f in familias if alumno in hijos[f.id])
            firmante = adultos_de[alumno.id][0]
            db.add(RespuestaAutorizacion(
                campana_id=campana.id, alumno_id=alumno.id, familia_id=familia.id, firmante=firmante.nombre,
                parentesco=firmante.parentesco, decision="aceptada" if rng.random() < 0.9 else "rechazada",
                canal="enlace", user_agent="datos de demostración", texto_hash=campana.texto_hash,
                creada_en=momento - timedelta(hours=rng.randint(1, 48)),
            ))
            respuestas += 1

    enlaces = []
    for familia in familias[:3]:
        token = security.generate_token()
        db.add(TokenAcceso(familia_id=familia.id, token_hash=security.hash_secret(token, settings.token_hmac_key),
                           expira_en=momento + timedelta(days=settings.family_link_ttl_days)))
        enlaces.append((familia.nombre_familia, f"/f/{token}"))

    registrar(db, usuario_id=None, accion="poblar_demo", entidad="sistema",
              cambio={"alumnos": len(alumnos), "familias": len(familias), "respuestas": respuestas})
    db.commit()

    return {
        "salones": len(salones),
        "alumnos": len(alumnos),
        "familias": len(familias),
        "usuarios": db.scalar(select(func.count()).select_from(Usuario)),
        "estudiantes_con_cuenta": len(con_cuenta),
        "docentes": len(docentes),
        "campanas": len(campanas),
        "respuestas": respuestas,
        "enlaces": enlaces,
    }
