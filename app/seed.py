"""Datos ficticios de la demostración: ninguna persona de esta lista existe.

Genera la estructura de una escuela primaria (régimen Costa, Ecuador): 3 niveles, 6 salones,
120 alumnos con sus representantes, personal de cada rol, asignaciones de docentes, y el módulo de
autorizaciones con campañas y respuestas. La generación es determinista (semilla fija).
"""

import hashlib
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import crypto, security, services
from app.archivos import Almacen
from app.audit import registrar
from app.config import Settings
from app.models import (
    AdultoAutorizado,
    Alumno,
    AnioLectivo,
    ArchivoEntrega,
    Area,
    Campana,
    CampanaSalon,
    CriterioRubrica,
    Entrega,
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
    Prorroga,
    Publicacion,
    RangoEscala,
    RepresentanteAlumno,
    RespuestaAutorizacion,
    Rubrica,
    Salon,
    SalonMateriaDocente,
    Tarea,
    TokenAcceso,
    Usuario,
    UsuarioRol,
)
from app.tiempo import TZ_ESCUELA, ahora

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


FUENTE_ESCALAS = ("Instructivo de Evaluación Estudiantil, Ministerio de Educación del Ecuador (2025). "
                  "VERIFICAR con la normativa vigente antes de usarla con alumnos reales.")

# Escala numérica 1,00–10,00 con equivalencias cualitativas; 7,00 es el mínimo para aprobar.
RANGOS_NUMERICOS = [
    ("A+", "Domina los aprendizajes requeridos", "10.00", "10.00"),
    ("A-", "Domina los aprendizajes requeridos", "9.00", "9.99"),
    ("B+", "Alcanza los aprendizajes requeridos", "8.00", "8.99"),
    ("B-", "Alcanza los aprendizajes requeridos", "7.00", "7.99"),
    ("C+", "Está próximo a alcanzar los aprendizajes requeridos", "6.00", "6.99"),
    ("C-", "Está próximo a alcanzar los aprendizajes requeridos", "5.00", "5.99"),
    ("D+", "Está próximo a alcanzar los aprendizajes requeridos", "4.00", "4.99"),
    ("D-", "No alcanza los aprendizajes requeridos", "3.00", "3.99"),
    ("E+", "No alcanza los aprendizajes requeridos", "2.00", "2.99"),
    ("E-", "No alcanza los aprendizajes requeridos", "1.00", "1.99"),
]
RANGOS_CUALITATIVOS = [("A", "Adquirido"), ("EP", "En proceso"), ("I", "Iniciado")]

TAREAS_DEMO = {
    "Matemática": [
        ("Fracciones equivalentes", "tarea", 1, "Resuelva los ejercicios 1 al 12 de la página 34 y suba una foto clara del cuaderno."),
        ("Problemas con decimales", "tarea", 1, "Resuelva los cinco problemas de la guía. Muestre el procedimiento completo."),
        ("Proyecto: presupuesto de la lonchera", "proyecto", 3, "En grupos, calculen el costo semanal de una lonchera saludable con precios reales del mercado. Entreguen un documento o una presentación."),
        ("Geometría: perímetros del aula", "actividad", 1, "Midan tres objetos del aula y calculen su perímetro. Suban una foto de las mediciones."),
    ],
    "Ciencias Naturales": [
        ("El ciclo del agua", "tarea", 1, "Dibuje el ciclo del agua y explique cada etapa con sus propias palabras."),
        ("Germinación de una semilla", "proyecto", 3, "Registre durante dos semanas el crecimiento de una semilla de fréjol. Incluya fotos y una tabla con las medidas."),
        ("Cadenas alimenticias de la Costa", "tarea", 1, "Elabore dos cadenas alimenticias con especies del manglar."),
    ],
    "Lengua y Literatura": [
        ("Lectura: «El cuento de la selva»", "tarea", 1, "Lea el cuento y responda las preguntas de comprensión de la guía."),
        ("Mi autobiografía", "proyecto", 2, "Escriba una autobiografía de una página con introducción, desarrollo y cierre."),
    ],
    "Inglés": [
        ("My daily routine", "tarea", 1, "Write ten sentences about your daily routine using the simple present."),
    ],
}
COMENTARIOS = ["Muy buen trabajo, el procedimiento es claro.", "Revise la presentación; el resultado es correcto.",
               "Le faltó justificar dos respuestas.", "Excelente organización.", "Buen esfuerzo, repase el tema en casa."]


def _escalas(db: Session) -> dict[str, EscalaCalificacion]:
    numerica = EscalaCalificacion(
        nombre="Numérica 1–10 (Básica)", tipo="numerica", minimo_aprobacion=Decimal("7.00"), fuente=FUENTE_ESCALAS,
        rangos=[RangoEscala(codigo=c, descripcion=d, minimo=Decimal(mi), maximo=Decimal(ma), orden=i)
                for i, (c, d, mi, ma) in enumerate(RANGOS_NUMERICOS)])
    cualitativa = EscalaCalificacion(
        nombre="Cualitativa (Preparatoria)", tipo="cualitativa", fuente=FUENTE_ESCALAS,
        rangos=[RangoEscala(codigo=c, descripcion=d, orden=i) for i, (c, d) in enumerate(RANGOS_CUALITATIVOS)])
    db.add_all([numerica, cualitativa])
    db.flush()
    return {"numerica": numerica, "cualitativa": cualitativa}


def _pdf_demo(titulo: str) -> bytes:
    """Un PDF mínimo de una página, válido, para que la demo tenga archivos que abrir."""
    texto = titulo.encode("latin-1", "replace").replace(b"(", b"[").replace(b")", b"]")
    contenido = b"BT /F1 18 Tf 72 720 Td (" + texto + b") Tj 0 -28 Td /F1 11 Tf (Archivo de demostracion) Tj ET"
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(contenido)).encode() + b" >>\nstream\n" + contenido + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    salida = bytearray(b"%PDF-1.4\n")
    posiciones = []
    for numero, cuerpo in enumerate(objetos, start=1):
        posiciones.append(len(salida))
        salida += f"{numero} 0 obj\n".encode() + cuerpo + b"\nendobj\n"
    inicio_xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    for posicion in posiciones:
        salida += f"{posicion:010d} 00000 n \n".encode()
    salida += f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{inicio_xref}\n%%EOF\n".encode()
    return bytes(salida)


def _a_las_18(dia: date) -> datetime:
    return datetime.combine(dia, time(18, 0), tzinfo=TZ_ESCUELA)


def _trabajo_academico(c: _Contexto, salones: list[Salon], periodos: list[PeriodoCalificacion], principal: Usuario,
                       almacen: Almacen) -> dict:
    """Tareas, entregas y notas de todos los salones, con fechas relativas a hoy para que la demo luzca al día."""
    db, rng = c.db, c.rng
    hoy = ahora().astimezone(TZ_ESCUELA).date()
    actual = next((p for p in periodos if p.inicio <= hoy <= p.fin), periodos[-1])
    anterior = next((p for p in reversed(periodos) if p.fin < actual.inicio), None)
    totales = {"tareas": 0, "entregas": 0}

    rubrica = Rubrica(materia_id=db.scalar(select(Materia.id).where(Materia.nombre == "Matemática")),
                      autor_id=principal.id, titulo="Proyecto de Matemática (básica)", compartida=True, criterios=[
                          CriterioRubrica(descripcion=d, puntos=Decimal(p), orden=i) for i, (d, p) in enumerate([
                              ("Planteamiento correcto del problema", "3"), ("Cálculos y procedimiento", "4"),
                              ("Presentación y orden", "2"), ("Trabajo en equipo", "1")])])
    db.add(rubrica)
    db.flush()

    for salon in salones:
        basica_media = salon.grado.orden >= 5
        cualitativa = salon.grado.nivel.nombre == "Preparatoria"
        clases = db.scalars(select(SalonMateriaDocente).where(SalonMateriaDocente.salon_id == salon.id)).all()
        alumnos = db.scalars(select(Alumno).where(Alumno.salon_id == salon.id).order_by(Alumno.id)).all()
        for clase in clases:
            plantillas = TAREAS_DEMO.get(clase.materia.nombre)
            if plantillas and not basica_media:
                # En los grados inferiores, menos trabajo: una tarea del trimestre anterior y una próxima
                plantillas = plantillas[:1] + plantillas[2:3]
            if not plantillas:
                continue
            db.add(Publicacion(clase_id=clase.id, autor_id=clase.docente_id, titulo="Bienvenidos al trimestre",
                               cuerpo="Aquí publicaré avisos y materiales. Revisen las tareas cada semana.\n"
                                      "Si necesitan más tiempo para una entrega, soliciten prórroga desde la tarea."))
            # Fechas: una tarea del trimestre anterior (ya cerrado) y el resto alrededor de hoy
            desplazamientos = [-21, -9, 3, 10] if basica_media else [-21, 3]
            for indice, (titulo, tipo, peso, instrucciones) in enumerate(plantillas):
                if indice == 0 and anterior is not None:
                    vence, periodo = _a_las_18(anterior.fin - timedelta(days=10)), anterior
                else:
                    dia = max(actual.inicio, min(actual.fin, hoy + timedelta(days=desplazamientos[indice])))
                    vence, periodo = _a_las_18(dia), actual
                tarea = Tarea(clase_id=clase.id, periodo_id=periodo.id, titulo=titulo, instrucciones=instrucciones,
                              tipo=tipo, fecha_entrega=vence, peso=peso,
                              tipos_permitidos="documento,imagen" if tipo != "proyecto" else "documento,imagen,video",
                              admite_enlace=True, admite_prorroga=True, estado="publicada",
                              rubrica_id=rubrica.id if tipo == "proyecto" and clase.materia.nombre == "Matemática" else None,
                              creada_por=clase.docente_id, publicada_en=vence - timedelta(days=7))
                db.add(tarea)
                db.flush()
                totales["tareas"] += 1
                pasada = vence.date() < hoy
                for posicion, alumno in enumerate(alumnos):
                    estudiante_demo = posicion == 0
                    probabilidad = 0.93 if pasada else (0.35 if vence.date() - hoy <= timedelta(days=4) else 0.1)
                    if estudiante_demo and not pasada:
                        continue  # el usuario «estudiante» tiene trabajo pendiente para probar la entrega
                    if rng.random() > probabilidad:
                        continue
                    tarde = pasada and rng.random() < 0.12
                    entregada_en = vence + timedelta(hours=rng.randint(2, 40)) if tarde else vence - timedelta(hours=rng.randint(2, 96))
                    entrega = Entrega(tarea_id=tarea.id, alumno_id=alumno.id, estado="con_retraso" if tarde else "entregada",
                                      entregada_en=entregada_en,
                                      enlace=None if estudiante_demo else "https://drive.example.test/demo",
                                      comentario_alumno="Adjunto mi trabajo." if estudiante_demo else None)
                    # Las tareas vencidas hace más de una semana ya están calificadas
                    if pasada and (hoy - vence.date()).days > 7 and rng.random() < 0.95:
                        nota = Decimal(rng.choice([5, 6, 7, 7, 8, 8, 8, 9, 9, 10])) + Decimal(rng.choice([0, 25, 50, 75])) / 100
                        entrega.estado = "calificada"
                        if cualitativa:
                            entrega.nivel = rng.choice(["A", "A", "EP", "I"])
                        else:
                            entrega.nota = min(nota, Decimal("10.00"))
                        entrega.comentario_docente = rng.choice(COMENTARIOS)
                        entrega.calificada_por, entrega.calificada_en = clase.docente_id, vence + timedelta(days=3)
                    db.add(entrega)
                    db.flush()
                    totales["entregas"] += 1
                    if estudiante_demo:
                        clave = almacen.nueva_clave()
                        pdf = _pdf_demo(titulo)
                        almacen.guardar(clave, pdf)
                        db.add(ArchivoEntrega(entrega_id=entrega.id, nombre=f"{titulo[:60]}.pdf", clave=clave,
                                              tipo="documento", mime="application/pdf", tamano=len(pdf),
                                              sha256=hashlib.sha256(pdf).hexdigest()))
                # Una prórroga pendiente en la próxima tarea de Matemática del docente principal
                if clase.docente_id == principal.id and not pasada and indice == 2 and len(alumnos) > 3:
                    solicitante = db.scalar(select(Usuario.id).where(Usuario.persona_id == alumnos[3].persona_id))
                    db.add(Prorroga(tarea_id=tarea.id, alumno_id=alumnos[3].id, solicitada_por=solicitante or principal.id,
                                    motivo="Estuve fuera de la ciudad por un viaje familiar y no alcancé a terminar.",
                                    nueva_fecha=vence + timedelta(days=3)))
    return totales


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
    periodos = []
    for nombre, inicio, fin in [("Primer trimestre", date(2026, 5, 4), date(2026, 8, 14)),
                                ("Segundo trimestre", date(2026, 8, 17), date(2026, 11, 13)),
                                ("Tercer trimestre", date(2026, 11, 16), date(2027, 2, 5))]:
        periodo = PeriodoCalificacion(anio_lectivo_id=anio.id, nombre=nombre, inicio=inicio, fin=fin,
                                      cerrado=fin < momento.astimezone(TZ_ESCUELA).date())
        db.add(periodo)
        periodos.append(periodo)
    escalas = _escalas(db)

    grados: dict[str, Grado] = {}
    for orden_nivel, (nombre_nivel, lista) in enumerate(NIVELES, start=1):
        nivel = Nivel(nombre=nombre_nivel, orden=orden_nivel,
                      escala_id=escalas["cualitativa" if nombre_nivel == "Preparatoria" else "numerica"].id)
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

    # ---------- Clases: tareas, entregas y calificaciones ----------
    db.flush()
    academico = _trabajo_academico(c, salones, periodos, principal, Almacen(settings.archivos_dir))

    registrar(db, usuario_id=None, accion="poblar_demo", entidad="sistema",
              cambio={"alumnos": len(alumnos), "familias": len(familias), "respuestas": respuestas, **academico})
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
        **academico,
    }
