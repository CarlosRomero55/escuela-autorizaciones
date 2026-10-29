# escuela-autorizaciones

Proyecto de portafolio: aplicación web para recolectar autorizaciones digitales de salidas, vacunas y eventos escolares, y para controlar la recogida de alumnos en portería.

> **Aviso:** es un proyecto demostrativo con datos completamente ficticios. No debe usarse con datos reales de menores sin una revisión legal de protección de datos personales.

## Estado

En desarrollo. Implementado y con pruebas (68, en SQLite y PostgreSQL):

- Autenticación del personal por roles, con argon2 y TOTP opcional
- API de administración: salones, alumnos, familias, adultos autorizados y campañas
- Enlaces firmados para familias, autorización con evidencia y registro de adultos con documento cifrado (AES-256-GCM)
- Portería: búsqueda, códigos de recogida de un solo uso, recogidas, persona no autorizada y autorizaciones temporales
- Bitácora de auditoría inmutable (trigger en PostgreSQL)

Pendiente: panel web de administración, PWA de portería sin conexión, mensajería, PDF y despliegue.

## Requisitos

- Python 3.12
- Docker y Docker Compose (para PostgreSQL local)

## Puesta en marcha local

```bash
cp .env.example .env          # editar los valores de SECRET_KEY y TOKEN_HMAC_KEY
docker compose up -d db
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
uvicorn app.main:create_app --factory --reload
```

## Estructura

```
app/
  config.py      configuración desde variables de entorno
  security.py    contraseñas, tokens, códigos de recogida y TOTP
  crypto.py      cifrado AES-256-GCM de datos sensibles
  models.py      modelo de datos (SQLAlchemy 2)
  main.py        aplicación FastAPI, cabeceras de seguridad y CSRF
  auth.py        inicio de sesión y TOTP
  routers/       administración, familias y portería
migrations/      migraciones de Alembic
templates/       páginas de familias (Jinja2)
static/          estilos
tests/           pruebas con pytest
uvicorn app.main:create_app --factory --reload
```

## Datos que se almacenarán (demo)

| Dato | Finalidad | Retención |
|---|---|---|
| Nombre de alumno (ficticio), salón | Organizar autorizaciones y recogidas | Hasta anonimización o borrado |
| Nombre, parentesco y teléfono de adultos (ficticios) | Validar quién puede recoger | Hasta fin del ciclo escolar |
| Hash del número de documento, cifrado | Verificar identidad en portería | Igual que el adulto |
| Respuestas de autorización: texto aceptado (hash), firmante, fecha, IP | Evidencia de consentimiento | Según política de la escuela |
| Bitácora de auditoría | Trazabilidad de acciones | Igual que los datos asociados |

La tarea de anonimización se implementará en la entrega de workers y su fecha será configurable.
