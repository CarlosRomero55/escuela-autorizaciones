# Publicar la demo (Render + Neon, planes gratuitos)

La aplicación corre en Render y la base de datos PostgreSQL en Neon, ambos en su plan gratuito.

## Qué esperar del plan gratuito

| Servicio | Límite | Efecto en la demo |
|---|---|---|
| Render | Se duerme tras 15 minutos sin visitas y tarda cerca de un minuto en despertar | La primera visita del día espera un minuto |
| Render | 750 horas de instancia al mes | Alcanza para un servicio encendido todo el mes |
| Render | Disco temporal: se borra al reiniciar o redesplegar | Los archivos subidos a las tareas se pierden; la página lo indica sin error. En producción: almacenamiento S3/R2 |
| Render | Sin consola | Migraciones y datos de la demo se aplican solos al arrancar (`scripts/seed.py --si-hace-falta 7`) |
| Neon | 1 GB por proyecto; se suspende tras 5 minutos sin uso | La demo usa unos pocos MB; la primera consulta tras la pausa tarda un poco más |

Los límites son los publicados por cada proveedor en noviembre de 2026; conviene revisarlos antes de publicar.

## 1. Base de datos en Neon

1. Crear una cuenta en <https://neon.com> (se puede entrar con GitHub).
2. **New project** → nombre `escuela-demo`, PostgreSQL 16 o superior, región **AWS US East (N. Virginia)**.
3. En **Connect**, copiar la cadena de conexión. Tiene esta forma:
   `postgresql://usuario:clave@ep-xxxx.us-east-1.aws.neon.tech/neondb?sslmode=require`
   La aplicación la acepta tal cual (agrega el controlador `psycopg` por su cuenta).

## 2. Servicio web en Render

1. Crear una cuenta en <https://render.com> con GitHub y autorizar el acceso al repositorio.
2. **New → Blueprint** → elegir este repositorio. Render lee `render.yaml`.
3. Render pide dos valores:
   - `DATABASE_URL`: la cadena de Neon del paso anterior.
   - `DEMO_PASSWORD`: la contraseña de los usuarios de demo (mínimo 10 caracteres). Se muestra en la pantalla de acceso, así que no debe ser una contraseña personal.
4. **Apply**. El primer despliegue instala dependencias, crea las tablas y carga los datos ficticios (unos 2 a 4 minutos).
5. Abrir `https://escuela-demo.onrender.com` (o el nombre que asigne Render). Si el nombre ya está tomado, Render agrega un sufijo.

`TOKEN_HMAC_KEY` y `ENCRYPTION_KEY` las genera Render. No deben cambiarse después: sin la misma `ENCRYPTION_KEY` no se pueden leer los secretos TOTP guardados. Si se cambian, basta con vaciar la base para que la demo se vuelva a cargar.

## 3. Comprobar

- `/healthz` responde `{"ok": true}`.
- En la pantalla de acceso aparecen los usuarios de demo, el botón «Ver la página de una familia» y, para el personal, el código de verificación en pantalla.
- La aplicación de portería (`porteria`) se puede instalar en Android desde el menú de Chrome: **Agregar a la pantalla principal**.

## Mantenimiento

- Cada `git push` a `main` vuelve a desplegar (`autoDeploy`).
- Los datos de la demo se renuevan solos cuando tienen más de 7 días, para que las fechas de tareas y entregas sigan al día. Lo que hagan los visitantes se borra en esa renovación.
- Para empezar de cero antes: en Neon, **Branches → Reset** (o borrar y crear la base), y reiniciar el servicio en Render.

## Seguridad en la demo

- `APP_ENV=production`: cookies `Secure`, HSTS y sin `/docs`.
- `DEMO_MODE=true` muestra usuarios, contraseña y códigos TOTP en pantalla. **Nunca** activarlo con datos reales.
- Detrás del proxy de Render, la IP del visitante se toma de `X-Forwarded-For`. Esa IP solo se usa para el límite de intentos por conexión y para la bitácora; el bloqueo de cuentas no depende de ella.
