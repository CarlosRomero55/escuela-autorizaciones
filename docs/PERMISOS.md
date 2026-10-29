# Permisos

Todas las reglas se verifican en el backend. La interfaz solo oculta lo que el backend ya prohíbe.

## Dos niveles

1. **Funciones del rol** (`PERMISOS` en `app/permisos.py`): qué pantallas y acciones tiene cada rol. Si falla, la respuesta es **403**, porque la existencia de una pantalla de gestión no es secreta.
2. **Alcance sobre los datos** (`alumnos_visibles` y `salones_visibles`): subconsultas que se aplican en cada consulta según la relación real con los datos. Fuera de alcance la respuesta es **404**, idéntica a la de un registro inexistente, para no revelar que existe.

Los permisos se evalúan con el **rol activo** de la sesión. Quien tiene varios roles (por ejemplo, una docente que también es madre de un alumno) elige con cuál actúa. Así cada acción queda auditada con un solo rol y se aplica el mínimo privilegio.

## Matriz

| Recurso | Estudiante | Representante | Docente | Directivo | Consejería | Coordinación | Admin | Auditor | Portería |
|---|---|---|---|---|---|---|---|---|---|
| Ficha del alumno | Propia | Representados validados | Alumnos de sus salones | Salones con materias de su área (o todos si no tiene área) | — (Fase 7) | Todos | Todos, **con motivo obligatorio** | — | — |
| Salón | El suyo, compañeros solo por nombre | Materias y docentes del salón de su hijo | Los que dicta o tutela | Según su área | — | Todos | Todos | — | — |
| Búsqueda | Dentro de su alcance | Dentro de su alcance | Dentro de su alcance | Dentro de su alcance | — | Todo | Todo | — | — |
| Clase: muro y tareas publicadas | Las de su salón | Las del salón de su representado | Solo las que dicta (gestiona) | Las de su área, en lectura | — | — | Todas, en lectura | — | — |
| Entregas y notas | Las propias; entrega y pide prórroga | Las de su representado, en lectura; pide prórroga | Las de sus clases; califica, devuelve, resuelve prórrogas | Las de su área, en lectura | — | — | **Con motivo obligatorio** | — | — |
| Cumplimiento y CSV | — | — | Sus clases | Su área | — | — | Todas | — | — |
| Rúbricas | — | — | Propias y compartidas de sus materias | — | — | — | — | — | — |
| Usuarios, roles, vínculos | — | — | — | — | — | — | Gestiona | — | — |
| Estructura académica y matrículas | — | — | — | — | — | — | Gestiona | — | — |
| Autorizaciones y familias | — | — | — | — | — | Gestiona | Gestiona | — | — |
| Recogida (API) | — | Genera código desde su enlace | Consulta | — | — | Opera | Opera | — | Opera |
| Bitácora | — | — | — | — | — | — | Completa | Completa, y sus consultas quedan registradas | — |
| Su propia cuenta | Sesiones, dispositivos, contraseña, TOTP | Igual | Igual | Igual | Igual | Igual | Igual | Igual | Igual |

Reglas adicionales:

- El representante no puede vincularse solo a un alumno: el vínculo lo crea y valida la administración. Un vínculo sin validar no da acceso.
- Los estudiantes tienen cuenta propia desde el grado configurado en `GRADO_MINIMO_CUENTA_ESTUDIANTE` (por defecto 5.º). En grados menores el acceso es a través del representante.
- El segundo factor es obligatorio para administración, coordinación, directivos, docentes, consejería y auditoría. No puede desactivarse mientras el rol lo exija.
- Un rol con `vigente_hasta` vencido deja de contar de inmediato.
- Nadie puede cerrar sesiones ajenas desde su perfil; el intento responde 404.
- Una tarea en borrador solo la ve su docente; para los demás responde 404.
- Ser tutor de un salón no da acceso a las clases de otros docentes de ese salón.
- Las pruebas de la Fase 3 están en `tests/test_clases.py`.

## Pruebas

Cada regla tiene su prueba en `tests/test_permisos.py`, que incluye estos intentos de acceso cruzado:

- representante → alumno ajeno (y se compara la respuesta con la de un id inexistente)
- representante con vínculo sin validar
- estudiante → compañero, otro salón
- docente → alumno de otro salón, pantallas de administración
- directivo de área → salón sin materias de su área
- consejería, auditoría y portería → fichas
- búsqueda fuera de alcance
- cambio a un rol que no se tiene
- administrador sin motivo, con motivo corto y con motivo válido
