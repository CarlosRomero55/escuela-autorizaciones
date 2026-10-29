# escuela-autorizaciones

Proyecto de portafolio: aplicación web para que una escuela recolecte autorizaciones digitales (salidas, vacunas, eventos) y controle quién recoge a cada alumno en portería.

> **Aviso:** es un proyecto demostrativo con datos completamente ficticios. No debe usarse con datos reales de menores sin una revisión legal de protección de datos personales.

## Qué resuelve

- **Coordinación:** crea campañas de autorización, ve el avance por salón, valida a las personas autorizadas a recoger, genera enlaces para las familias e importa alumnos desde Excel (CSV).
- **Familias:** responden desde un enlace personal en el navegador del teléfono, sin crear cuenta ni instalar nada, y registran hasta dos personas autorizadas.
- **Portería:** verifica en segundos si quien llega está autorizado, con un código de un solo uso, y registra el protocolo cuando llega alguien no autorizado.

## Estado

En desarrollo. Implementado y con 93 pruebas automatizadas (SQLite y PostgreSQL):

- Panel de administración en HTML, sin JavaScript, con token CSRF en cada formulario
- Inicio de sesión del personal por roles, con argon2 y segundo factor TOTP opcional
- Campañas con texto legal fijado por hash, avance por salón y exportación CSV protegida contra inyección de fórmulas
- Importación de alumnos desde CSV con vista previa, detección de duplicados y reporte de errores
- Enlaces firmados para familias (128 bits, guardados como HMAC, con vencimiento)
- Número de documento cifrado con AES-256-GCM
- Portería: códigos de recogida de un solo uso con intentos limitados, recogidas, persona no autorizada y autorizaciones temporales
- Sincronización sin duplicados (identificador único por evento) lista en la API
- Bitácora de auditoría inmutable (trigger en PostgreSQL)

Pendiente: PWA de portería con modo sin conexión, mensajería por WhatsApp, comprobante en PDF y despliegue público.

## Probarlo localmente

Requisitos: Python 3.12 o superior y Docker (para PostgreSQL).

```bash
cp .env.example .env          # completar las claves (ver comentarios en el archivo)
docker compose up -d db
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/seed.py        # crea las tablas y carga los datos ficticios
uvicorn app.main:create_app --factory --reload
```

Abra http://localhost:8000 e inicie sesión como `sofia@demo.test` con la contraseña definida en `DEMO_PASSWORD`. El script imprime además tres enlaces de familia para ver la experiencia desde el teléfono.

### Datos de demostración

| Elemento | Cantidad |
|---|---|
| Salones | 3 (1A, 2A, 3A) |
| Alumnos | 60, con hermanos que comparten familia |
| Personas autorizadas | 2 por alumno; algunas pendientes de validar |
| Campañas | 2, con respuestas parciales |
| Usuarios | `sofia@demo.test` (coordinación), `admin@demo.test`, `porteria@demo.test`, `maestra@demo.test` |

## Pruebas

```bash
pytest                                     # SQLite en memoria
TEST_POSTGRES_URL=postgresql+psycopg://escuela:escuela_dev@localhost:5432/escuela_test pytest
```

La segunda forma también verifica que la bitácora rechace `UPDATE` y `DELETE` en PostgreSQL.

## Estructura

```
app/
  main.py        aplicación FastAPI, cabeceras de seguridad y CSRF de la API
  config.py      configuración desde variables de entorno
  models.py      modelo de datos (SQLAlchemy 2)
  services.py    reglas de negocio compartidas por la API y el panel
  security.py    contraseñas, tokens, códigos de recogida y TOTP
  crypto.py      cifrado AES-256-GCM de datos sensibles
  auth.py        inicio de sesión y TOTP
  seed.py        datos ficticios de la demo
  routers/       panel HTML, API de administración, familias y portería
migrations/      migraciones de Alembic
scripts/         carga de datos de demostración
templates/       páginas del panel y de las familias (Jinja2)
static/          estilos
tests/           pruebas con pytest
```

## Decisiones de diseño

- **Formularios HTML clásicos en el panel:** funcionan en cualquier navegador, no necesitan compilación y respetan una CSP estricta (`script-src 'self'`).
- **Reglas de negocio en `services.py`:** la API y el panel aplican exactamente las mismas validaciones y escriben la misma bitácora.
- **La bitácora se escribe en la misma transacción que la acción:** si la acción falla, no queda registrada como hecha.
- **Los secretos nunca se guardan en claro:** contraseñas con argon2; tokens y códigos como HMAC; documentos cifrados con una clave fuera de la base de datos.

## Datos que se almacenan

| Dato | Finalidad | Retención |
|---|---|---|
| Nombre del alumno y salón | Organizar autorizaciones y recogidas | Hasta la anonimización de fin de año escolar |
| Nombre, parentesco y teléfono de adultos autorizados | Validar quién puede recoger | Hasta fin del ciclo escolar |
| Número de documento, cifrado, y su HMAC para búsquedas | Verificar identidad en portería | Igual que el adulto |
| Respuestas de autorización: firmante, decisión, fecha, IP, navegador y hash del texto | Evidencia de consentimiento | Según la política de la escuela |
| Bitácora de auditoría (sin documentos ni teléfonos) | Trazabilidad de acciones | Igual que los datos asociados |

La tarea de anonimización se implementará junto con los procesos en segundo plano, con fecha configurable.
