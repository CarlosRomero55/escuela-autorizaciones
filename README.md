# Sistema escolar

Proyecto de portafolio: sistema web para una escuela primaria de Ecuador, con portales por rol (administración, coordinación, directivos, docentes, consejería, auditoría, portería, representantes y estudiantes), gestión académica y un módulo de autorizaciones y recogida de alumnos.

> **Aviso:** es un proyecto demostrativo con datos completamente ficticios. No debe usarse con datos reales de menores sin una revisión legal conforme a la Ley Orgánica de Protección de Datos Personales de Ecuador.

## Estado

| Fase | Contenido | Estado |
|---|---|---|
| 1 | Layout y sistema de diseño, roles con ámbito, motor de permisos, sesiones en servidor, dispositivos y alertas, TOTP por rol, activación y recuperación de cuentas, estructura académica, salones, asignaciones y matrículas | **Hecha** |
| 6 | Autorizaciones digitales, enlaces firmados para familias, códigos de recogida de un solo uso, persona no autorizada | Backend y página de familias hechos; falta la PWA de portería |
| 3 | Portal docente y estudiante: tareas, entregas, rúbricas, calificaciones | Siguiente |
| 2, 4, 5, 7–11 | Sitio público, portal del representante, boletines, bienestar, directivos, evaluaciones, operación | Diseño en [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md) |

Calidad: **156 pruebas** (en SQLite y PostgreSQL), que incluyen los intentos de acceso cruzado entre roles, y una comprobación de que la migración coincide con el modelo. Lint con ruff e integración continua en GitHub Actions.

## Lo más destacado

- **Permisos por relación real con los datos:** un representante solo ve a sus representados validados y un docente solo a los alumnos de sus salones. Fuera de alcance, la respuesta es igual a la de un recurso inexistente. Ver [docs/PERMISOS.md](docs/PERMISOS.md).
- **El administrador justifica cada consulta** a datos de un alumno; el motivo queda en una bitácora que PostgreSQL no permite modificar ni borrar.
- **Sesiones en el servidor:** lista de dispositivos, cierre remoto, expiración por inactividad, rotación del token al ingresar y alerta cuando se usa un dispositivo nuevo, con botón "No fui yo".
- **Segundo factor obligatorio por rol**, con enrolamiento guiado por código QR generado en el servidor.
- **Sin secretos en claro:** argon2 para contraseñas; HMAC para tokens y códigos; AES-256-GCM para documentos y secretos TOTP.
- **CSP estricta:** Bootstrap, HTMX, iconos y la fuente Inter se sirven desde el propio servidor. No hay CDN ni scripts en línea.
- **Diseño responsive** probado a 390 px (teléfono), 1366 px (Chromebook) y 1440 px (escritorio). Ver [docs/ESTILO.md](docs/ESTILO.md).

## Probarlo localmente

Requisitos: Python 3.12 o superior y Docker (para PostgreSQL).

```bash
cp .env.example .env          # completar TOKEN_HMAC_KEY, ENCRYPTION_KEY y DEMO_PASSWORD
docker compose up -d db
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/seed.py        # crea las tablas y carga los datos ficticios
uvicorn app.main:create_app --factory --reload
```

Abra http://localhost:8000. Con `DEMO_MODE=true`, la pantalla de acceso muestra los usuarios de prueba y, para el personal, el código de verificación vigente, de modo que la demo se recorre sin una aplicación de autenticación.

| Usuario | Rol |
|---|---|
| `admin` | Administración |
| `sofia` | Coordinación (autorizaciones y recogida) |
| `rector` | Directivo de toda la escuela |
| `dir.matematica` | Directivo del área de Matemática |
| `docente` | Docente tutora de 5.º A; además es representante (prueba el cambio de rol) |
| `representante` | Representante de dos hermanos |
| `estudiante` | Estudiante de 5.º A |
| `consejeria`, `auditor`, `porteria` | Roles con alcance restringido |

Datos cargados: año lectivo 2026-2027 (régimen Costa), 3 niveles, 6 salones, 120 alumnos, 110 familias, 8 docentes con asignaciones completas y 2 campañas de autorización con respuestas parciales.

## Pruebas

```bash
pytest                                     # SQLite en memoria
TEST_POSTGRES_URL=postgresql+psycopg://escuela:escuela_dev@localhost:5432/escuela_test pytest
ruff check .
```

## Estructura

```
app/
  main.py         aplicación, cabeceras de seguridad, manejo de errores
  config.py       configuración por variables de entorno
  models.py       modelo de datos (SQLAlchemy 2)
  permisos.py     roles, funciones y alcance sobre los datos
  sesiones.py     sesiones en servidor, dispositivos y alertas
  auth.py         credenciales, bloqueo, TOTP, enlaces de cuenta
  services.py     reglas de negocio compartidas por la API y las páginas
  web.py          capa web: sesión, CSRF, menú por rol, renderizado
  security.py     contraseñas, tokens, códigos, política de contraseñas
  crypto.py       cifrado AES-256-GCM
  seed.py         datos ficticios
  routers/        acceso, portal, administración, familias, portería, API
migrations/       migraciones de Alembic
templates/        layout, macros y páginas (Jinja2)
static/           app.css, app.js y librerías incluidas (vendor/)
docs/             arquitectura, permisos y guía de estilo
tests/            pruebas con pytest
```

## Datos que se almacenan

| Dato | Finalidad | Protección |
|---|---|---|
| Nombres de personas, alumnos y salones | Organización académica | Acceso por rol y relación |
| Usuario, correo y contraseña del personal y las familias | Acceso al portal | argon2; correo enmascarado en administración |
| Sesiones y dispositivos | Seguridad de la cuenta | Token como HMAC; huella del dispositivo como HMAC |
| Documento de adultos autorizados | Verificar identidad en portería | AES-256-GCM + HMAC |
| Respuestas de autorización: firmante, fecha, IP, navegador y hash del texto | Evidencia de consentimiento | Solo coordinación y administración |
| Bitácora (sin documentos, teléfonos ni correos) | Trazabilidad | Inmutable en PostgreSQL |

La retención por tipo de dato y la anonimización al cierre del año lectivo se implementan con los procesos en segundo plano.
