"""Datos ficticios para la demostración: ninguna persona de esta lista existe.

Genera 3 salones, 60 alumnos, sus familias, hasta 2 adultos autorizados por alumno,
4 usuarios de personal (uno por rol) y 2 campañas con respuestas parciales.
La generación es determinista (semilla fija) para que la demo sea reproducible.
"""

import random
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import security, services
from app.audit import registrar
from app.config import Settings
from app.models import (
    AdultoAutorizado,
    Alumno,
    Campana,
    CampanaSalon,
    Familia,
    FamiliaAlumno,
    RespuestaAutorizacion,
    Salon,
    TokenAcceso,
    UsuarioStaff,
)
from app.tiempo import ahora

NOMBRES_NINOS = [
    "Valentina", "Mateo", "Camila", "Santiago", "Isabella", "Sebastián", "Emilia", "Matías",
    "Martina", "Nicolás", "Sofía", "Joaquín", "Renata", "Emiliano", "Luciana", "Benjamín",
    "Antonella", "Thiago", "Victoria", "Gabriel",
]
NOMBRES_MUJERES = ["María", "Carmen", "Rosa", "Ana", "Patricia", "Elena", "Verónica", "Gloria", "Mónica"]
NOMBRES_HOMBRES = ["José", "Luis", "Jorge", "Carlos", "Miguel", "Fernando", "Ricardo", "Andrés", "Diego"]
# El nombre del adulto concuerda con el parentesco
PARENTESCOS = {
    "madre": NOMBRES_MUJERES, "abuela": NOMBRES_MUJERES, "tía": NOMBRES_MUJERES,
    "padre": NOMBRES_HOMBRES, "abuelo": NOMBRES_HOMBRES, "tío": NOMBRES_HOMBRES,
}
APELLIDOS = [
    "Andrade", "Benítez", "Cedeño", "Delgado", "Espinoza", "Franco", "Guerrero", "Herrera",
    "Intriago", "Jaramillo", "León", "Macías", "Navarrete", "Ordóñez", "Peñafiel", "Quiñónez",
    "Rivadeneira", "Salazar", "Torres", "Villacís",
]
SALONES = [("1A", "1ro"), ("2A", "2do"), ("3A", "3ro")]
ALUMNOS_POR_SALON = 20

USUARIOS = [
    ("Sofía (coordinación)", "sofia@demo.test", "coordinadora"),
    ("Administración", "admin@demo.test", "administrador"),
    ("Portería principal", "porteria@demo.test", "porteria"),
    ("Maestra de 1A", "maestra@demo.test", "maestra"),
]

TEXTO_DEMO = (
    "TEXTO DE DEMOSTRACIÓN. En una escuela real, este texto lo redacta y aprueba el abogado de la institución.\n\n"
    "Yo, representante del alumno indicado, autorizo su participación en la actividad descrita, "
    "organizada por la escuela en la fecha señalada, bajo supervisión del personal docente. "
    "Declaro haber leído las condiciones de la actividad y que la información que proporciono es verdadera."
)


def poblar(db: Session, *, settings: Settings, password: str, semilla: int = 2026) -> dict:
    if db.scalar(select(func.count()).select_from(Salon)):
        raise RuntimeError("La base ya tiene datos. Use --reset para borrarla y volver a poblarla.")

    rng = random.Random(semilla)
    momento = ahora()

    # Personal: uno por rol
    personal = []
    for nombre, email, rol in USUARIOS:
        usuario = UsuarioStaff(nombre=nombre, email=email, rol=rol, password_hash=security.hash_password(password))
        db.add(usuario)
        personal.append(usuario)
    db.flush()
    coordinadora = personal[0]

    salones = [Salon(nombre=nombre, grado=grado) for nombre, grado in SALONES]
    db.add_all(salones)
    db.flush()

    # Alumnos y familias. Algunos alumnos son hermanos y comparten familia.
    alumnos: list[Alumno] = []
    familias: list[Familia] = []
    familia_de: dict[int, Familia] = {}
    hijos_por_familia: dict[int, int] = {}
    numero = 0
    for salon in salones:
        for _ in range(ALUMNOS_POR_SALON):
            numero += 1
            apellido_1, apellido_2 = rng.sample(APELLIDOS, 2)
            familia = None
            if familias and rng.random() < 0.12:
                candidata = rng.choice(familias)
                if hijos_por_familia[candidata.id] == 1:
                    familia = candidata
                    apellido_1, apellido_2 = candidata.nombre_familia.removeprefix("Familia ").split(" ", 1)
            alumno = Alumno(
                codigo_interno=f"EST-{numero:03d}",
                nombre=f"{rng.choice(NOMBRES_NINOS)} {apellido_1} {apellido_2}",
                salon_id=salon.id,
            )
            db.add(alumno)
            if familia is None:
                familia = Familia(
                    nombre_familia=f"Familia {apellido_1} {apellido_2}",
                    # Números con formato ecuatoriano pero inventados (prefijo 09000)
                    telefono=f"09000{numero:05d}",
                    consentimiento_mensajes=rng.random() < 0.9,
                )
                db.add(familia)
                db.flush()
                familias.append(familia)
                hijos_por_familia[familia.id] = 0
            db.flush()
            db.add(FamiliaAlumno(familia_id=familia.id, alumno_id=alumno.id))
            hijos_por_familia[familia.id] += 1
            familia_de[alumno.id] = familia
            alumnos.append(alumno)
    db.flush()

    # Dos adultos por alumno: la mayoría ya validados, algunos pendientes de revisión
    adultos_por_alumno: dict[int, list[AdultoAutorizado]] = {}
    for alumno in alumnos:
        familia = familia_de[alumno.id]
        apellido = familia.nombre_familia.removeprefix("Familia ").split(" ")[0]
        lista = []
        for parentesco in rng.sample(list(PARENTESCOS), 2):
            estado = "validado" if rng.random() < 0.85 else "pendiente"
            adulto = AdultoAutorizado(
                alumno_id=alumno.id,
                familia_id=familia.id,
                nombre=f"{rng.choice(PARENTESCOS[parentesco])} {apellido}",
                parentesco=parentesco,
                estado=estado,
                validado_por=coordinadora.id if estado == "validado" else None,
            )
            db.add(adulto)
            lista.append(adulto)
        adultos_por_alumno[alumno.id] = lista
    db.flush()

    # Campañas con respuestas parciales para que el panel muestre avance
    definiciones = [
        ("Salida pedagógica al Parque Histórico", "salida", [salones[0], salones[1]], 7,
         "Visita guiada de medio día. Transporte contratado por la escuela."),
        ("Jornada de vacunación (demostración)", "vacuna", [salones[2]], 3,
         "Vacunación en la escuela con personal de salud acreditado."),
    ]
    campanas = []
    for titulo, tipo, destino, dias, descripcion in definiciones:
        campana = Campana(
            titulo=titulo, descripcion=descripcion, tipo=tipo, texto_legal=TEXTO_DEMO,
            texto_hash=services.hash_texto(TEXTO_DEMO),
            fecha_limite=momento + timedelta(days=dias), creado_por=coordinadora.id,
        )
        db.add(campana)
        db.flush()
        for salon in destino:
            db.add(CampanaSalon(campana_id=campana.id, salon_id=salon.id))
        campanas.append((campana, {s.id for s in destino}))
    db.flush()

    respuestas = 0
    for campana, salon_ids in campanas:
        for alumno in alumnos:
            if alumno.salon_id not in salon_ids or rng.random() > 0.6:
                continue
            firmante = adultos_por_alumno[alumno.id][0]
            db.add(RespuestaAutorizacion(
                campana_id=campana.id,
                alumno_id=alumno.id,
                familia_id=familia_de[alumno.id].id,
                firmante=firmante.nombre,
                parentesco=firmante.parentesco,
                decision="aceptada" if rng.random() < 0.9 else "rechazada",
                canal="enlace",
                user_agent="datos de demostración",
                texto_hash=campana.texto_hash,
                creada_en=momento - timedelta(hours=rng.randint(1, 48)),
            ))
            respuestas += 1

    # Enlaces de ejemplo para recorrer la vista de las familias
    enlaces = []
    for familia in familias[:3]:
        token = security.generate_token()
        db.add(TokenAcceso(
            familia_id=familia.id,
            token_hash=security.hash_secret(token, settings.token_hmac_key),
            expira_en=momento + timedelta(days=settings.family_link_ttl_days),
        ))
        enlaces.append((familia.nombre_familia, f"/f/{token}"))

    registrar(db, usuario_id=None, accion="poblar_demo", entidad="sistema",
              cambio={"alumnos": len(alumnos), "familias": len(familias), "respuestas": respuestas})
    db.commit()

    return {
        "usuarios": [(email, rol) for _, email, rol in USUARIOS],
        "salones": len(salones),
        "alumnos": len(alumnos),
        "familias": len(familias),
        "adultos": sum(len(v) for v in adultos_por_alumno.values()),
        "campanas": len(campanas),
        "respuestas": respuestas,
        "enlaces": enlaces,
    }
