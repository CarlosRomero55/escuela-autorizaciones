"""Fase 3: clases, tareas, entregas, prórrogas y calificaciones, con énfasis en el alcance de cada rol."""

import io
from datetime import timedelta
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import select

from app import academico, archivos
from app.models import (
    ArchivoEntrega,
    Entrega,
    EscalaCalificacion,
    Nivel,
    PeriodoCalificacion,
    Persona,
    Prorroga,
    RangoEscala,
    SalonMateriaDocente,
    Tarea,
)
from app.services import Autor, ErrorNegocio
from app.tiempo import TZ_ESCUELA, ahora
from tests.conftest import (
    asignar,
    crear_alumno,
    crear_salon,
    crear_usuario,
    entrar_web,
    estructura,
    post_web,
    vincular_representante,
)

PDF = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n"


def _formulario(dt) -> str:
    return dt.astimezone(TZ_ESCUELA).strftime("%Y-%m-%dT%H:%M")


@pytest.fixture()
def escuela(session_factory):
    """5.º A con Matemática (docente A) y Lengua (docente B); 5.º B con Matemática (docente B)."""
    sf = session_factory
    datos = estructura(sf)
    hoy = ahora().astimezone(TZ_ESCUELA).date()
    with sf() as db:
        escala = EscalaCalificacion(nombre="Numérica", tipo="numerica", minimo_aprobacion=Decimal("7.00"), rangos=[
            RangoEscala(codigo="A", descripcion="Domina", minimo=Decimal("9"), maximo=Decimal("10"), orden=0),
            RangoEscala(codigo="B", descripcion="Alcanza", minimo=Decimal("7"), maximo=Decimal("8.99"), orden=1),
            RangoEscala(codigo="C", descripcion="Próximo", minimo=Decimal("1"), maximo=Decimal("6.99"), orden=2),
        ])
        db.add(escala)
        db.flush()
        for nivel in db.scalars(select(Nivel)):
            nivel.escala_id = escala.id
        cerrado = PeriodoCalificacion(anio_lectivo_id=datos["anio"], nombre="Primer trimestre",
                                      inicio=hoy - timedelta(days=120), fin=hoy - timedelta(days=31), cerrado=True)
        actual = PeriodoCalificacion(anio_lectivo_id=datos["anio"], nombre="Segundo trimestre",
                                     inicio=hoy - timedelta(days=30), fin=hoy + timedelta(days=60))
        db.add_all([cerrado, actual])
        db.commit()
        periodos = {"cerrado": cerrado.id, "actual": actual.id}

    docente_a = crear_usuario(sf, rol="docente")
    docente_b = crear_usuario(sf, rol="docente")
    salon_a = crear_salon(sf, "5.º A")
    salon_b = crear_salon(sf, "5.º B")
    asignar(sf, salon_a, "Matemática", docente_a.id)
    asignar(sf, salon_a, "Lengua y Literatura", docente_b.id)
    asignar(sf, salon_b, "Matemática", docente_b.id)
    alumno_1 = crear_alumno(sf, salon_a, "Ana", "Uno")
    alumno_2 = crear_alumno(sf, salon_a, "Beto", "Dos")
    alumno_3 = crear_alumno(sf, salon_b, "Caro", "Tres")
    with sf() as db:
        clases = {(c.salon_id, c.materia.nombre): c.id for c in db.scalars(select(SalonMateriaDocente))}
        persona_1 = db.scalar(select(Persona.id).where(Persona.nombres == "Ana"))
    estudiante = crear_usuario(sf, rol="estudiante", persona_id=persona_1)
    representante = crear_usuario(sf, rol="representante")
    vincular_representante(sf, representante.id, alumno_1)
    otro_representante = crear_usuario(sf, rol="representante")
    vincular_representante(sf, otro_representante.id, alumno_3)
    return {
        "periodos": periodos, "docente_a": docente_a, "docente_b": docente_b,
        "clase": clases[(salon_a, "Matemática")], "clase_lengua": clases[(salon_a, "Lengua y Literatura")],
        "clase_b": clases[(salon_b, "Matemática")], "alumnos": (alumno_1, alumno_2, alumno_3),
        "estudiante": estudiante, "representante": representante, "otro_representante": otro_representante,
    }


def _tarea(sf, escuela, *, titulo="Fracciones", vence=None, peso=1, publicar=True, periodo="actual",
           clase="clase", tipos=("documento", "imagen")) -> int:
    with sf() as db:
        clase_obj = db.get(SalonMateriaDocente, escuela[clase])
        autor = Autor(usuario_id=clase_obj.docente_id, rol="docente", ip=None)
        tarea = academico.crear_tarea(
            db, autor, clase_obj, titulo=titulo, instrucciones="Resolver la guía.", tipo="tarea",
            fecha_entrega=vence or ahora() + timedelta(days=3), peso=peso, tipos_permitidos=list(tipos),
            admite_enlace=True, admite_prorroga=True, periodo_id=escuela["periodos"][periodo], rubrica_id=None,
            publicar=publicar)
        return tarea.id


def _entrega_directa(sf, tarea_id: int, alumno_id: int, nota: str | None = None) -> int:
    with sf() as db:
        entrega = Entrega(tarea_id=tarea_id, alumno_id=alumno_id, estado="entregada", entregada_en=ahora(),
                          enlace="https://ejemplo.test/trabajo")
        if nota is not None:
            entrega.estado, entrega.nota = "calificada", Decimal(nota)
        db.add(entrega)
        db.commit()
        return entrega.id


# ---------- Tareas y alcance ----------

def test_docente_crea_y_publica_tarea_desde_el_portal(app, session_factory, escuela):
    c = entrar_web(app, escuela["docente_a"])
    r = post_web(c, f"/portal/clases/{escuela['clase']}/tareas", {
        "titulo": "Problemas con decimales", "instrucciones": "Cinco problemas.", "tipo": "tarea",
        "fecha_entrega": _formulario(ahora() + timedelta(days=5)), "peso": "2", "tipos_permitidos": ["documento"],
        "admite_enlace": "on", "periodo_id": str(escuela["periodos"]["actual"]), "accion": "publicar"})
    assert r.status_code == 303 and r.headers["location"].startswith("/portal/tareas/")
    with session_factory() as db:
        tarea = db.scalar(select(Tarea))
        assert (tarea.estado, tarea.peso, tarea.tipos_permitidos) == ("publicada", 2, "documento")
    assert c.get(f"/portal/clases/{escuela['clase']}").status_code == 200
    assert "Problemas con decimales" in entrar_web(app, escuela["estudiante"]).get(f"/portal/clases/{escuela['clase']}").text


def test_no_se_crean_tareas_en_un_periodo_cerrado(session_factory, escuela):
    with pytest.raises(ErrorNegocio, match="cerrado"):
        _tarea(session_factory, escuela, periodo="cerrado")


def test_borrador_invisible_para_el_estudiante(app, session_factory, escuela):
    tarea = _tarea(session_factory, escuela, titulo="Borrador secreto", publicar=False)
    estudiante = entrar_web(app, escuela["estudiante"])
    assert estudiante.get(f"/portal/tareas/{tarea}").status_code == 404
    assert "Borrador secreto" not in estudiante.get(f"/portal/clases/{escuela['clase']}").text
    assert entrar_web(app, escuela["docente_a"]).get(f"/portal/tareas/{tarea}").status_code == 200


def test_docente_no_ve_ni_gestiona_clases_ajenas(app, session_factory, escuela):
    tarea = _tarea(session_factory, escuela)
    entrega = _entrega_directa(session_factory, tarea, escuela["alumnos"][0])
    # La docente B dicta Lengua en el mismo salón, pero la clase de Matemática no es suya
    c = entrar_web(app, escuela["docente_b"])
    assert c.get(f"/portal/clases/{escuela['clase']}").status_code == 404
    assert c.get(f"/portal/tareas/{tarea}").status_code == 404
    assert c.get(f"/portal/entregas/{entrega}").status_code == 404
    assert post_web(c, f"/portal/entregas/{entrega}/calificar", {"nota": "10"}).status_code == 404
    assert post_web(c, f"/portal/clases/{escuela['clase']}/tareas", {"titulo": "x"}).status_code == 404
    with session_factory() as db:
        assert db.get(Entrega, entrega).nota is None


def test_estudiante_ve_solo_su_trabajo(app, session_factory, escuela):
    tarea = _tarea(session_factory, escuela)
    _, alumno_2, _ = escuela["alumnos"]
    ajena = _entrega_directa(session_factory, tarea, alumno_2, nota="9")
    c = entrar_web(app, escuela["estudiante"])
    assert c.get(f"/portal/entregas/{ajena}").status_code == 404
    assert c.get(f"/portal/clases/{escuela['clase_b']}").status_code == 404
    assert c.get(f"/portal/clases/{escuela['clase']}/matriz.csv").status_code == 404
    assert "Beto" not in c.get(f"/portal/clases/{escuela['clase']}?vista=calificaciones").text


def test_representante_consulta_pero_no_entrega(app, session_factory, escuela):
    tarea = _tarea(session_factory, escuela)
    rep = entrar_web(app, escuela["representante"])
    assert rep.get(f"/portal/tareas/{tarea}").status_code == 200
    r = post_web(rep, f"/portal/tareas/{tarea}/entregar", {"enlace": "https://ejemplo.test"})
    assert r.status_code == 404
    otro = entrar_web(app, escuela["otro_representante"])
    assert otro.get(f"/portal/tareas/{tarea}").status_code == 404
    with session_factory() as db:
        assert db.scalar(select(Entrega)) is None


# ---------- Entregas y archivos ----------

def test_entrega_con_archivo_y_descarga_segura(app, session_factory, escuela):
    tarea = _tarea(session_factory, escuela)
    c = entrar_web(app, escuela["estudiante"])
    r = post_web(c, f"/portal/tareas/{tarea}/entregar", {"comentario": "Listo"},
                 files=[("adjuntos", ("../../informe.pdf", PDF, "application/pdf"))])
    assert r.status_code == 303
    with session_factory() as db:
        entrega = db.scalar(select(Entrega))
        archivo = db.scalar(select(ArchivoEntrega))
        assert entrega.estado == "entregada" and archivo.nombre == "informe.pdf" and archivo.escaneo == "sin_escanear"
        assert archivo.clave != "informe.pdf" and len(archivo.clave) == 32
    descarga = c.get(f"/portal/archivos/{archivo.id}")
    assert descarga.status_code == 200 and descarga.content == PDF
    assert "sandbox" in descarga.headers["content-security-policy"]
    # Otro representante y la docente de otra clase no pueden descargarlo
    assert entrar_web(app, escuela["otro_representante"]).get(f"/portal/archivos/{archivo.id}").status_code == 404
    assert entrar_web(app, escuela["docente_b"]).get(f"/portal/archivos/{archivo.id}").status_code == 404
    assert entrar_web(app, escuela["docente_a"]).get(f"/portal/archivos/{archivo.id}").status_code == 200


def test_archivo_disfrazado_se_rechaza(app, session_factory, escuela):
    tarea = _tarea(session_factory, escuela)
    c = entrar_web(app, escuela["estudiante"])
    post_web(c, f"/portal/tareas/{tarea}/entregar",
             files=[("adjuntos", ("tarea.pdf", b"MZ\x90\x00 ejecutable", "application/pdf"))])
    with session_factory() as db:
        assert db.scalar(select(Entrega)) is None


def test_tipo_no_permitido_por_la_tarea(session_factory, escuela, tmp_path):
    tarea_id = _tarea(session_factory, escuela, tipos=("imagen",))
    with session_factory() as db:
        tarea = db.get(Tarea, tarea_id)
        with pytest.raises(ErrorNegocio, match="no admite"):
            academico.entregar(db, Autor(escuela["estudiante"].id, "estudiante", None), archivos.Almacen(tmp_path),
                               tarea, escuela["alumnos"][0], archivos=[("a.pdf", PDF)], enlace="", comentario="")


def test_entrega_tardia_y_efecto_de_la_prorroga(app, session_factory, escuela):
    alumno_1, alumno_2, _ = escuela["alumnos"]
    # Una tarea vencida ayer: se crea vigente y luego se mueve la fecha
    tarea = _tarea(session_factory, escuela)
    with session_factory() as db:
        db.get(Tarea, tarea).fecha_entrega = ahora() - timedelta(days=1)
        db.commit()

    estudiante = entrar_web(app, escuela["estudiante"])
    r = post_web(estudiante, f"/portal/tareas/{tarea}/prorrogas", {
        "alumno_id": str(alumno_1), "motivo": "Estuve fuera de la ciudad.",
        "nueva_fecha": _formulario(ahora() + timedelta(days=2))})
    assert r.status_code == 303
    with session_factory() as db:
        prorroga = db.scalar(select(Prorroga))
        assert prorroga.estado == "pendiente"

    # Solo la docente de la clase la resuelve
    assert post_web(entrar_web(app, escuela["docente_b"]), f"/portal/prorrogas/{prorroga.id}",
                    {"accion": "aprobar"}).status_code == 404
    assert post_web(entrar_web(app, escuela["docente_a"]), f"/portal/prorrogas/{prorroga.id}",
                    {"accion": "aprobar"}).status_code == 303

    post_web(estudiante, f"/portal/tareas/{tarea}/entregar", {"enlace": "https://ejemplo.test/mi-trabajo"})
    with session_factory() as db:
        assert db.scalar(select(Entrega).where(Entrega.alumno_id == alumno_1)).estado == "entregada"
        # Sin prórroga, la misma entrega llega con retraso
        t = db.get(Tarea, tarea)
        entrega = academico.entregar(db, Autor(1, "estudiante", None), archivos.Almacen("/nonexistent"), t, alumno_2,
                                     archivos=[], enlace="https://ejemplo.test/otro", comentario="")
        assert entrega.estado == "con_retraso"
        assert academico.estado_para(None, academico.fecha_efectiva(db, t, alumno_2)) == "atrasada"


def test_no_se_vuelve_a_entregar_lo_calificado(session_factory, escuela, tmp_path):
    tarea_id = _tarea(session_factory, escuela)
    _entrega_directa(session_factory, tarea_id, escuela["alumnos"][0], nota="8")
    with session_factory() as db, pytest.raises(ErrorNegocio, match="calificada"):
        academico.entregar(db, Autor(1, "estudiante", None), archivos.Almacen(tmp_path), db.get(Tarea, tarea_id),
                           escuela["alumnos"][0], archivos=[], enlace="https://ejemplo.test", comentario="")


# ---------- Calificaciones ----------

def test_calificar_guarda_nota_truncada_y_equivalencia(app, session_factory, escuela):
    tarea = _tarea(session_factory, escuela)
    entrega = _entrega_directa(session_factory, tarea, escuela["alumnos"][0])
    c = entrar_web(app, escuela["docente_a"])
    assert post_web(c, f"/portal/entregas/{entrega}/calificar", {"nota": "8,759", "comentario": "Bien"}).status_code == 303
    with session_factory() as db:
        e = db.get(Entrega, entrega)
        assert (e.estado, e.nota, e.nivel) == ("calificada", Decimal("8.75"), "B")


def test_devolver_exige_comentario(session_factory, escuela):
    tarea = _tarea(session_factory, escuela)
    entrega_id = _entrega_directa(session_factory, tarea, escuela["alumnos"][0])
    with session_factory() as db:
        with pytest.raises(ErrorNegocio, match="corregir"):
            academico.calificar(db, Autor(escuela["docente_a"].id, "docente", None), db.get(Entrega, entrega_id),
                                nota="", nivel="", comentario="", devolver=True)


def test_periodo_cerrado_bloquea_calificar(session_factory, escuela):
    tarea = _tarea(session_factory, escuela)
    entrega_id = _entrega_directa(session_factory, tarea, escuela["alumnos"][0])
    with session_factory() as db:
        db.get(PeriodoCalificacion, escuela["periodos"]["actual"]).cerrado = True
        db.commit()
        with pytest.raises(ErrorNegocio, match="cerrado"):
            academico.calificar(db, Autor(escuela["docente_a"].id, "docente", None), db.get(Entrega, entrega_id),
                                nota="9", nivel="", comentario="", devolver=False)


@pytest.mark.parametrize("texto", ["0", "10,01", "abc", "-3", ""])
def test_notas_fuera_de_rango(texto):
    with pytest.raises(ErrorNegocio):
        academico.leer_nota(texto)


def test_promedio_ponderado_y_truncado(session_factory, escuela):
    alumno = escuela["alumnos"][0]
    t1 = _tarea(session_factory, escuela, titulo="T1", peso=1)
    t2 = _tarea(session_factory, escuela, titulo="T2", peso=2)
    _entrega_directa(session_factory, t1, alumno, nota="8.75")
    _entrega_directa(session_factory, t2, alumno, nota="9.33")
    with session_factory() as db:
        clase = db.get(SalonMateriaDocente, escuela["clase"])
        # (8,75 × 1 + 9,33 × 2) / 3 = 9,1366… → 9,13 (se trunca, no se redondea)
        assert academico.promedio(db, clase, alumno, escuela["periodos"]["actual"]) == Decimal("9.13")
        assert academico.promedio(db, clase, escuela["alumnos"][1], escuela["periodos"]["actual"]) is None


def test_matriz_csv_neutraliza_formulas(app, session_factory, escuela):
    _tarea(session_factory, escuela, titulo="=HYPERLINK(\"http://malo\")")
    r = entrar_web(app, escuela["docente_a"]).get(f"/portal/clases/{escuela['clase']}/matriz.csv")
    assert r.status_code == 200
    assert "'=HYPERLINK" in r.text and ";=HYPERLINK" not in r.text


def test_administrador_justifica_antes_de_ver_una_entrega(app, session_factory, escuela):
    tarea = _tarea(session_factory, escuela)
    entrega = _entrega_directa(session_factory, tarea, escuela["alumnos"][0])
    admin = entrar_web(app, crear_usuario(session_factory, rol="administrador"))
    r = admin.get(f"/portal/entregas/{entrega}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/portal/alumnos/{escuela['alumnos'][0]}"
    assert post_web(admin, f"/portal/entregas/{entrega}/calificar", {"nota": "10"}).status_code == 404


def test_coordinacion_no_ve_el_trabajo_academico(app, session_factory, escuela):
    tarea = _tarea(session_factory, escuela)
    c = entrar_web(app, crear_usuario(session_factory, rol="coordinacion"))
    assert c.get(f"/portal/clases/{escuela['clase']}").status_code == 404
    assert c.get(f"/portal/tareas/{tarea}").status_code == 404


def test_inicio_por_rol_muestra_datos_reales(app, session_factory, escuela):
    tarea = _tarea(session_factory, escuela, titulo="Geometría del aula")
    _entrega_directa(session_factory, tarea, escuela["alumnos"][1])
    docente = entrar_web(app, escuela["docente_a"]).get("/portal").text
    assert "1 por calificar" in docente
    estudiante = entrar_web(app, escuela["estudiante"]).get("/portal").text
    assert "Geometría del aula" in estudiante
    assert "1 pendiente" in entrar_web(app, escuela["representante"]).get("/portal").text


# ---------- Archivos ----------

def test_imagen_pierde_metadatos_y_gps():
    imagen = Image.new("RGB", (40, 20), "red")
    exif = Image.Exif()
    exif[0x0112] = 6  # orientación: girada 90°
    exif[0x8825] = {1: "S", 2: (2.0, 10.0, 0.0)}  # GPS
    salida = io.BytesIO()
    imagen.save(salida, format="JPEG", exif=exif)
    limpio = archivos.validar("foto.jpg", salida.getvalue(), ["imagen"])
    with Image.open(io.BytesIO(limpio.contenido)) as resultado:
        assert not resultado.getexif()
        assert resultado.size == (20, 40)  # se aplicó la orientación antes de quitar el EXIF


@pytest.mark.parametrize(("nombre", "contenido"), [
    ("virus.pdf", b"MZ\x90\x00"),
    ("foto.png", b"\xff\xd8\xff\xe0 jpeg con extension png"),
    ("script.py", b"\x00\x01binario"),
    ("vacio.txt", b""),
])
def test_validacion_por_contenido(nombre, contenido):
    with pytest.raises(archivos.ArchivoRechazado):
        archivos.validar(nombre, contenido, list(archivos.TIPOS))


def test_nombre_seguro_quita_rutas():
    assert archivos.nombre_seguro("../../etc/passwd") == "passwd"
    assert archivos.nombre_seguro("C:\\Users\\x\\tarea<1>.pdf") == "tarea_1_.pdf"


def test_almacen_rechaza_claves_manipuladas(tmp_path):
    with pytest.raises(ValueError):
        archivos.Almacen(tmp_path).leer("../../secreto")
