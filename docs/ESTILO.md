# Guía de estilo

El portal usa Bootstrap 5.3 ajustado con los tokens de `static/css/app.css`. Todas las pantallas comparten el mismo layout (`templates/layout.html`) y las mismas macros (`templates/_macros.html`).

## Tokens

| Token | Valor | Uso |
|---|---|---|
| `--ui-sidebar` | #1e2a3a | Barra lateral |
| `--ui-fondo` | #f4f6fb | Fondo del área de contenido |
| `--ui-superficie` | #ffffff | Tarjetas, cabecera |
| `--ui-borde` | #e3e7ef | Separadores |
| `--ui-texto` | #1d2433 | Texto principal |
| `--ui-texto-2` | #566074 | Texto secundario (contraste AA sobre blanco y sobre el fondo) |
| `--ui-primario` | #4f5bd5 | Botones y acentos (5,6:1 sobre blanco) |
| `--ui-primario-osc` | #3c47b5 | Enlaces y estados activos |
| `--ui-acento` | #7b5cd6 | Morado secundario |
| `--ui-ok` / `--ui-ok-suave` | #17663a / #e2f3e9 | Aprobado, entregado |
| `--ui-alerta` / `--ui-alerta-suave` | #7a4f00 / #fdf0d8 | Pendiente, por vencer |
| `--ui-error` / `--ui-error-suave` | #a1281d / #fbe6e3 | Atrasado, rechazado |
| `--ui-radio` | 1rem | Bordes de tarjetas |

Regla de color: los estados se muestran con **texto oscuro sobre fondo pastel**. El ámbar puro no alcanza 4,5:1 sobre blanco y no se usa para texto.

## Tipografía

Inter en 400, 600 y 700, alojada en `/static/vendor/inter`. Los títulos usan 600. No se cargan fuentes externas.

## Layout

- **Escritorio (≥ 992 px):** barra lateral fija de 264 px, cabecera pegajosa de 64 px con buscador, selector de rol, alertas y menú de cuenta.
- **Móvil:** la barra lateral pasa a un menú desplegable superior (`offcanvas-top`); los indicadores van en dos columnas y las tablas con clase `tabla-tarjetas` se muestran como tarjetas.

## Componentes (macros)

| Macro | Uso |
|---|---|
| `encabezado(titulo, subtitulo)` | Título de página; con `{% call %}` agrega botones a la derecha |
| `indicador(icono, color, cifra, etiqueta, url)` | Tarjeta de estadística con icono en círculo pastel (`azul`, `morado`, `verde`, `ambar`, `rojo`) |
| `estado(texto, tipo, icono)` | Insignia de estado: `ok`, `alerta`, `error`, `info`, `neutro` |
| `vacio(icono, texto)` | Estado vacío con mensaje útil |
| `paginacion(request, pagina, paginas)` | Navegación entre páginas que conserva los filtros |
| `estado_respuesta(respuesta)` | Estado de una autorización |

Otros patrones:

- **Tarjeta de salón o materia:** `.tarjeta-salon` con `.franja franja-N` (N de 0 a 5) para el color de cabecera.
- **Avisos:** se guardan en la sesión y se muestran como toasts; los de error no se ocultan solos.
- **Enlaces de un solo uso:** campo de solo lectura con botón `data-copiar`.

## Reglas

- **Sin estilos ni scripts en línea:** la CSP los bloquea. Todo va en `app.css` o `app.js`.
- **Objetivos táctiles de al menos 44 px:** botones, enlaces del menú y campos.
- **Cada campo tiene una etiqueta visible o `visually-hidden`;** los iconos decorativos llevan `aria-hidden`.
- **Formularios que cambian datos:** POST con token CSRF y redirección después de guardar.
- **Números de fase:** cuando una función todavía no existe, la pantalla lo dice ("llega en la Fase 3") en lugar de mostrar datos inventados.
