// Portería: consulta y registro de recogidas, con modo sin conexión.
//
// - El padrón (alumnos y personas autorizadas vigentes) se descarga a IndexedDB y vence a las N horas.
// - Toda recogida o incidente entra primero a una cola local con un identificador único (sync_id) y la
//   hora real del teléfono; luego se envía. Si no hay red, queda en la cola y se reintenta con espera
//   creciente y al volver la conexión. El servidor ignora un sync_id repetido, así que no hay duplicados.
// - Validar un código de recogida necesita conexión: el código solo se puede comprobar en el servidor.
// - Al salir se borran el padrón, la cola (si está vacía) y las cachés.
'use strict';

(() => {
  const BD = 'porteria';
  const VERSION_BD = 1;
  const API = { 'X-Requested-With': 'fetch', 'Content-Type': 'application/json' };
  const REFRESCO_MS = 15 * 60 * 1000;
  const HORA = new Intl.DateTimeFormat('es-EC', { timeZone: 'America/Guayaquil', hour: '2-digit', minute: '2-digit' });
  const usuario = Number(document.body.dataset.usuario);

  const $ = (id) => document.getElementById(id);
  const estado = {
    padron: null, salon: null, alumno: null, cola: [], enviando: false, espera: 5000,
    reintento: null, sesionVencida: false, enLinea: navigator.onLine,
  };

  // ---------- IndexedDB ----------
  let conexionBd;
  function abrirBd() {
    if (conexionBd) return conexionBd;
    conexionBd = new Promise((resolver, rechazar) => {
      const pedido = indexedDB.open(BD, VERSION_BD);
      pedido.onupgradeneeded = () => {
        const bd = pedido.result;
        bd.createObjectStore('datos');                         // padrón y salidas, por clave
        bd.createObjectStore('cola', { keyPath: 'sync_id' });  // registros por enviar
      };
      pedido.onsuccess = () => resolver(pedido.result);
      pedido.onerror = () => rechazar(pedido.error);
    });
    return conexionBd;
  }
  async function bd(almacen, modo, operacion) {
    const base = await abrirBd();
    return new Promise((resolver, rechazar) => {
      const tx = base.transaction(almacen, modo);
      const pedido = operacion(tx.objectStore(almacen));
      tx.oncomplete = () => resolver(pedido && pedido.result);
      tx.onerror = () => rechazar(tx.error);
    });
  }
  const leer = (clave) => bd('datos', 'readonly', (s) => s.get(clave));
  const guardar = (clave, valor) => bd('datos', 'readwrite', (s) => s.put(valor, clave));
  const leerCola = () => bd('cola', 'readonly', (s) => s.getAll());
  const ponerEnCola = (item) => bd('cola', 'readwrite', (s) => s.put(item));
  const quitarDeCola = (id) => bd('cola', 'readwrite', (s) => s.delete(id));

  // ---------- Utilidades de interfaz ----------
  function el(etiqueta, clase, texto) {
    const nodo = document.createElement(etiqueta);
    if (clase) nodo.className = clase;
    if (texto !== undefined) nodo.textContent = texto;
    return nodo;
  }
  function icono(nombre) { const i = el('i', `bi ${nombre}`); i.setAttribute('aria-hidden', 'true'); return i; }
  function iniciales(nombre) { return nombre.split(' ').slice(0, 2).map((p) => p[0] || '').join('').toUpperCase(); }
  function hora(iso) { return HORA.format(new Date(iso)); }
  function hace(iso) {
    const min = Math.round((Date.now() - new Date(iso)) / 60000);
    if (min < 1) return 'hace un momento';
    if (min < 60) return `hace ${min} min`;
    return `hace ${Math.floor(min / 60)} h ${min % 60} min`;
  }
  function normalizar(texto) { return texto.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase(); }

  function avisar(texto, tipo = 'ok') {
    const caja = el('div', `aviso ${tipo}`);
    caja.append(icono(tipo === 'error' ? 'bi-x-circle-fill' : tipo === 'ambar' ? 'bi-cloud-slash-fill' : 'bi-check-circle-fill'), el('span', '', texto));
    $('avisos').append(caja);
    setTimeout(() => caja.remove(), tipo === 'ok' ? 3500 : 7000);
  }

  function ir(vista) {
    for (const s of document.querySelectorAll('.vista')) s.hidden = s.id !== `vista-${vista}`;
    const pestana = ['alumno', 'protocolo'].includes(vista) ? 'buscar' : vista;
    for (const b of document.querySelectorAll('.pestanas button')) {
      if (b.dataset.ir === pestana) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    }
    if (vista === 'salidas') mostrarSalidas();
    if (vista === 'pendientes') mostrarPendientes();
    if (vista === 'buscar') $('busqueda').focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }

  // ---------- Conexión y padrón ----------
  function pintarRed() {
    const caja = $('estado-red');
    caja.classList.toggle('en-linea', estado.enLinea);
    caja.classList.toggle('sin-red', !estado.enLinea);
    caja.querySelector('span').textContent = estado.enLinea ? 'En línea' : 'Sin conexión';
    $('aviso-sesion').hidden = !estado.sesionVencida;
    $('form-codigo').querySelector('button').disabled = !estado.enLinea;
    $('nota-codigo').textContent = estado.enLinea
      ? 'La familia genera el código desde su enlace; vence en pocos minutos y sirve una sola vez.'
      : 'Sin conexión no se pueden validar códigos. Use la lista de personas autorizadas y verifique el documento.';
  }

  function padronVigente() {
    return estado.padron && new Date(estado.padron.vence_en) > new Date() && estado.padron.usuario === usuario;
  }

  function pintarPadron() {
    const p = $('estado-padron');
    if (!estado.padron) {
      p.textContent = estado.enLinea ? 'Descargando el padrón…' : 'Sin padrón en este teléfono. Conéctese para descargarlo.';
      p.classList.toggle('vencido', !estado.enLinea);
      return;
    }
    if (!padronVigente()) {
      p.textContent = 'El padrón venció. Conéctese para actualizarlo; mientras tanto no se puede consultar.';
      p.classList.add('vencido');
      return;
    }
    p.classList.remove('vencido');
    p.textContent = `Padrón actualizado ${hace(estado.padron.generado_en)} · ${estado.padron.alumnos.length} alumnos · vence ${hora(estado.padron.vence_en)}`;
  }

  async function pedir(url, opciones = {}) {
    let respuesta;
    try {
      respuesta = await fetch(url, { credentials: 'same-origin', ...opciones, headers: { ...API, ...(opciones.headers || {}) } });
    } catch (e) {
      marcarRed(false);
      throw Object.assign(new Error('Sin conexión'), { red: true });
    }
    marcarRed(true);
    if (respuesta.status === 401) {
      estado.sesionVencida = true;
      pintarRed();
      throw Object.assign(new Error('Sesión vencida'), { sesion: true });
    }
    const cuerpo = await respuesta.json().catch(() => ({}));
    if (!respuesta.ok) throw Object.assign(new Error(cuerpo.detail || 'No se pudo completar.'), { estado: respuesta.status, cuerpo });
    return cuerpo;
  }

  function marcarRed(enLinea) {
    if (estado.enLinea === enLinea) return;
    estado.enLinea = enLinea;
    pintarRed();
    if (enLinea) { sincronizar(); actualizarPadron(); }
  }

  async function actualizarPadron(forzar = false) {
    if (!forzar && padronVigente() && Date.now() - new Date(estado.padron.generado_en) < REFRESCO_MS) return;
    try {
      const datos = await pedir('/api/porteria/padron');
      datos.usuario = usuario;
      estado.padron = datos;
      await guardar('padron', datos);
      // Las recogidas que aún están en la cola siguen contando como salidas de hoy
      await aplicarColaAlPadron();
      if (forzar) avisar('Padrón actualizado.');
    } catch (e) {
      if (forzar && !e.sesion) avisar(e.red ? 'Sin conexión: se mantiene el padrón guardado.' : e.message, e.red ? 'ambar' : 'error');
    }
    pintarPadron();
    pintarSalones();
    buscar();
    if (estado.alumno) mostrarAlumno(estado.alumno.id);
  }

  async function aplicarColaAlPadron() {
    if (!estado.padron) return;
    for (const item of estado.cola) {
      if (item.tipo !== 'recogida') continue;
      const alumno = estado.padron.alumnos.find((a) => a.id === item.cuerpo.alumno_id);
      if (alumno && !alumno.salida) alumno.salida = item.cuerpo.ocurrida_en;
    }
  }

  // ---------- Búsqueda ----------
  function pintarSalones() {
    const caja = $('salones');
    caja.replaceChildren();
    if (!padronVigente()) return;
    const todos = el('button', '', 'Todos');
    todos.type = 'button';
    todos.setAttribute('aria-pressed', String(estado.salon === null));
    todos.addEventListener('click', () => { estado.salon = null; pintarSalones(); buscar(); });
    caja.append(todos);
    for (const s of estado.padron.salones) {
      const b = el('button', '', s.nombre);
      b.type = 'button';
      b.setAttribute('aria-pressed', String(estado.salon === s.id));
      b.addEventListener('click', () => { estado.salon = s.id; pintarSalones(); buscar(); });
      caja.append(b);
    }
  }

  function buscar() {
    const lista = $('resultados');
    lista.replaceChildren();
    $('sin-resultados').hidden = true;
    if (!padronVigente()) return;
    const texto = normalizar($('busqueda').value.trim());
    if (!texto && estado.salon === null) {
      lista.append(el('li', 'nota', 'Escriba el nombre del alumno o elija un salón.'));
      return;
    }
    const encontrados = estado.padron.alumnos.filter((a) =>
      (estado.salon === null || a.salon_id === estado.salon) &&
      (!texto || normalizar(a.nombre).includes(texto) || normalizar(a.codigo) === texto)).slice(0, 60);
    $('sin-resultados').hidden = encontrados.length > 0;
    for (const a of encontrados) {
      const item = el('li');
      const fila = el('button', 'fila');
      fila.type = 'button';
      const cuerpo = el('span', 'cuerpo');
      cuerpo.append(el('span', 'titulo', a.nombre), el('span', 'sub', `${a.salon} · ${a.adultos.length} autorizada${a.adultos.length === 1 ? '' : 's'}`));
      fila.append(el('span', 'avatar', iniciales(a.nombre)), cuerpo);
      if (a.salida) fila.append(el('span', 'etiqueta ambar', `Salió ${hora(a.salida)}`));
      fila.addEventListener('click', () => mostrarAlumno(a.id));
      item.append(fila);
      lista.append(item);
    }
  }

  // ---------- Alumno ----------
  function adultosVigentes(alumno) {
    const ahora = new Date();
    return alumno.adultos.filter((x) => new Date(x.desde) <= ahora && (!x.hasta || new Date(x.hasta) > ahora));
  }

  function mostrarAlumno(id) {
    const alumno = padronVigente() && estado.padron.alumnos.find((a) => a.id === id);
    if (!alumno) { ir('buscar'); return; }
    estado.alumno = alumno;
    $('alumno-iniciales').textContent = iniciales(alumno.nombre);
    $('alumno-nombre').textContent = alumno.nombre;
    $('alumno-salon').textContent = `${alumno.salon} · ${alumno.codigo}`;
    const salida = $('alumno-salida');
    salida.hidden = !alumno.salida;
    if (alumno.salida) {
      salida.replaceChildren(icono('bi-exclamation-triangle-fill'),
        el('span', '', `Ya se registró su salida hoy a las ${hora(alumno.salida)}. Confirme con coordinación antes de entregarlo otra vez.`));
    }
    const lista = $('adultos');
    lista.replaceChildren();
    const vigentes = adultosVigentes(alumno);
    if (!vigentes.length) lista.append(el('li', 'vacio', 'No hay personas autorizadas vigentes. No entregue al alumno y avise a coordinación.'));
    for (const adulto of vigentes) {
      const item = el('li', 'adulto');
      const cuerpo = el('div', 'cuerpo');
      cuerpo.append(el('span', 'titulo', adulto.nombre), el('span', 'sub', adulto.parentesco));
      if (adulto.temporal) {
        const etiqueta = el('span', 'etiqueta ambar', `Temporal${adulto.hasta ? ` hasta ${hora(adulto.hasta)}` : ''}`);
        cuerpo.append(etiqueta);
      }
      const boton = el('button', 'boton boton-primario', 'Entregar');
      boton.type = 'button';
      boton.addEventListener('click', () => confirmarEntrega(alumno, adulto));
      item.append(el('span', 'avatar', iniciales(adulto.nombre)), cuerpo, boton);
      lista.append(item);
    }
    $('codigo').value = '';
    pintarRed();
    ir('alumno');
  }

  function confirmarEntrega(alumno, adulto) {
    const dialogo = $('confirmar');
    $('confirmar-texto').textContent = `${alumno.nombre} (${alumno.salon}) sale con ${adulto.nombre}, ${adulto.parentesco}.`;
    dialogo.returnValue = '';
    dialogo.onclose = () => {
      if (dialogo.returnValue === 'si') registrar('recogida', '/api/porteria/recogidas', { alumno_id: alumno.id, adulto_id: adulto.id }, alumno, adulto.nombre);
    };
    dialogo.showModal();
  }

  // ---------- Cola de envío ----------
  function nuevoId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  }

  async function registrar(tipo, url, datos, alumno, detalle) {
    const item = {
      sync_id: nuevoId(), tipo, url, estado: 'pendiente', intentos: 0, error: null,
      alumno: alumno.nombre, salon: alumno.salon, detalle,
      cuerpo: { ...datos, ocurrida_en: new Date().toISOString() },
    };
    item.cuerpo.sync_id = item.sync_id;
    await ponerEnCola(item);
    estado.cola.push(item);
    if (tipo === 'recogida') {
      alumno.salida = item.cuerpo.ocurrida_en;
      await guardar('padron', estado.padron);
    }
    pintarContador();
    const enviado = await sincronizar();
    const rechazado = estado.cola.find((x) => x.sync_id === item.sync_id && x.estado === 'rechazado');
    if (enviado.has(item.sync_id)) {
      avisar(tipo === 'recogida' ? `Salida registrada: ${alumno.nombre} con ${detalle}.` : 'Incidente registrado. Avise a coordinación.');
    } else if (rechazado) {
      // Con red, el servidor decide en el momento (p. ej. autorización revocada): no se entrega
      await quitarDeCola(item.sync_id);
      estado.cola = estado.cola.filter((x) => x.sync_id !== item.sync_id);
      if (tipo === 'recogida') { alumno.salida = null; await guardar('padron', estado.padron); }
      pintarContador();
      avisar(`${rechazado.error} No entregue al alumno.`, 'error');
      if (tipo === 'recogida') { mostrarAlumno(alumno.id); return; }
    } else {
      // A partir de ahora el servidor sabrá que llegó tarde por falta de red
      item.cuerpo.diferido = true;
      await ponerEnCola(item);
      avisar('Guardado en el teléfono. Se enviará al volver la conexión.', 'ambar');
    }
    ir('buscar');
    $('busqueda').value = '';
    buscar();
  }

  // Envía la cola en orden. Devuelve los sync_id que el servidor aceptó en esta pasada.
  async function sincronizar() {
    const aceptados = new Set();
    if (estado.enviando) return aceptados;
    estado.enviando = true;
    clearTimeout(estado.reintento);
    try {
      for (const item of estado.cola.filter((x) => x.estado === 'pendiente')) {
        try {
          const respuesta = await pedir(item.url, { method: 'POST', body: JSON.stringify(item.cuerpo) });
          await quitarDeCola(item.sync_id);
          estado.cola = estado.cola.filter((x) => x.sync_id !== item.sync_id);
          aceptados.add(item.sync_id);
          estado.sesionVencida = false;
          if (respuesta.alerta) avisar(respuesta.alerta, 'error');
        } catch (e) {
          if (e.red || e.sesion || e.estado >= 500 || e.estado === 429) break;  // se reintenta después
          // El servidor lo rechazó (p. ej. registro demasiado antiguo): queda a la vista para revisarlo
          item.estado = 'rechazado';
          item.error = e.message;
          await ponerEnCola(item);
        }
      }
    } finally {
      estado.enviando = false;
    }
    pintarContador();
    if (estado.cola.some((x) => x.estado === 'pendiente') && !estado.sesionVencida) {
      estado.reintento = setTimeout(sincronizar, estado.espera);
      estado.espera = Math.min(estado.espera * 2, 5 * 60 * 1000);
    } else {
      estado.espera = 5000;
    }
    if (!$('vista-pendientes').hidden) mostrarPendientes();
    return aceptados;
  }

  function pintarContador() {
    const n = estado.cola.length;
    $('contador').hidden = n === 0;
    $('contador').textContent = String(n);
  }

  function mostrarPendientes() {
    const lista = $('pendientes');
    lista.replaceChildren();
    $('sin-pendientes').hidden = estado.cola.length > 0;
    $('boton-sincronizar').disabled = !estado.cola.some((x) => x.estado === 'pendiente');
    for (const item of [...estado.cola].sort((a, b) => a.cuerpo.ocurrida_en.localeCompare(b.cuerpo.ocurrida_en))) {
      const li = el('li', 'fila');
      const cuerpo = el('span', 'cuerpo');
      const que = item.tipo === 'recogida' ? `Salió con ${item.detalle}` : 'Persona no autorizada';
      cuerpo.append(el('span', 'titulo', item.alumno), el('span', 'sub', `${hora(item.cuerpo.ocurrida_en)} · ${que}`));
      if (item.error) cuerpo.append(el('span', 'sub', item.error));
      li.append(icono(item.tipo === 'recogida' ? 'bi-person-check' : 'bi-shield-exclamation'), cuerpo);
      if (item.estado === 'rechazado') {
        const quitar = el('button', 'boton', 'Quitar');
        quitar.type = 'button';
        quitar.addEventListener('click', async () => {
          if (!window.confirm('¿Ya lo reportó a coordinación? El registro se quitará de este teléfono.')) return;
          await quitarDeCola(item.sync_id);
          estado.cola = estado.cola.filter((x) => x.sync_id !== item.sync_id);
          pintarContador();
          mostrarPendientes();
        });
        li.append(el('span', 'etiqueta rojo', 'Rechazado'), quitar);
      } else {
        li.append(el('span', 'etiqueta ambar', 'Por enviar'));
      }
      lista.append(li);
    }
  }

  // ---------- Salidas de hoy ----------
  async function mostrarSalidas() {
    const lista = $('salidas');
    let salidas = (await leer('salidas')) || [];
    try {
      salidas = await pedir('/api/porteria/salidas-hoy');
      await guardar('salidas', salidas);
      $('salidas-nota').textContent = 'Registradas en el servidor, de la más reciente a la más antigua.';
    } catch (e) {
      $('salidas-nota').textContent = 'Sin conexión: se muestra la última lista descargada. Lo pendiente está en «Por enviar».';
    }
    lista.replaceChildren();
    if (!salidas.length) lista.append(el('li', 'vacio', 'Todavía no hay salidas registradas hoy.'));
    for (const s of salidas) {
      const li = el('li', 'fila');
      const cuerpo = el('span', 'cuerpo');
      cuerpo.append(el('span', 'titulo', s.alumno),
        el('span', 'sub', s.no_autorizado ? `${s.salon} · Intento de persona no autorizada` : `${s.salon} · con ${s.adulto} (${s.parentesco})`));
      li.append(el('span', 'etiqueta neutro', hora(s.hora)), cuerpo);
      if (s.no_autorizado) li.append(el('span', 'etiqueta rojo', 'Incidente'));
      else if (s.sin_conexion) li.append(el('span', 'etiqueta ambar', 'Sin conexión'));
      lista.append(li);
    }
  }

  // ---------- Código de la familia (solo en línea) ----------
  async function validarCodigo(evento) {
    evento.preventDefault();
    const alumno = estado.alumno;
    const codigo = $('codigo').value.trim();
    if (!/^\d{6}$/.test(codigo)) { avisar('El código tiene 6 dígitos.', 'error'); return; }
    const boton = $('form-codigo').querySelector('button');
    boton.disabled = true;
    try {
      const r = await pedir('/api/porteria/recogidas/validar-codigo', {
        method: 'POST', body: JSON.stringify({ alumno_id: alumno.id, codigo, sync_id: nuevoId() }),
      });
      const adulto = alumno.adultos.find((x) => x.id === r.adulto_id);
      alumno.salida = r.ocurrida_en;
      await guardar('padron', estado.padron);
      avisar(`Código correcto. ${alumno.nombre} sale con ${adulto ? adulto.nombre : 'la persona autorizada'}.`);
      ir('buscar');
      buscar();
    } catch (e) {
      avisar(e.red ? 'Sin conexión: no se puede validar el código. Use la lista de autorizados.' : e.message, e.red ? 'ambar' : 'error');
    } finally {
      boton.disabled = !estado.enLinea;
      $('codigo').value = '';
    }
  }

  // ---------- Persona no autorizada ----------
  function abrirProtocolo() {
    $('protocolo-alumno').textContent = `${estado.alumno.nombre} · ${estado.alumno.salon}`;
    $('form-incidente').reset();
    ir('protocolo');
  }

  function registrarIncidente(evento) {
    evento.preventDefault();
    const descripcion = $('descripcion').value.trim();
    if (descripcion.length < 10) { avisar('Describa lo ocurrido con al menos 10 caracteres.', 'error'); return; }
    registrar('incidente', '/api/porteria/recogidas/no-autorizada', {
      alumno_id: estado.alumno.id, descripcion,
      personas_involucradas: $('personas').value.trim() || null,
      acciones_tomadas: $('acciones').value.trim() || null,
    }, estado.alumno, 'no autorizado');
  }

  // ---------- Escáner QR (si el navegador lo permite) ----------
  async function escanear() {
    const dialogo = $('escaner');
    const video = $('video');
    let flujo;
    let activo = true;
    dialogo.onclose = () => { activo = false; if (flujo) flujo.getTracks().forEach((t) => t.stop()); };
    dialogo.showModal();
    try {
      flujo = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
      video.srcObject = flujo;
      await video.play();
      const detector = new window.BarcodeDetector({ formats: ['qr_code'] });
      while (activo) {
        const codigos = await detector.detect(video).catch(() => []);
        if (codigos.length) {
          const valor = codigos[0].rawValue.replace(/^ALUMNO:/i, '').trim();
          const alumno = padronVigente() && estado.padron.alumnos.find((a) => a.codigo.toLowerCase() === valor.toLowerCase());
          dialogo.close();
          if (alumno) mostrarAlumno(alumno.id); else avisar('El código escaneado no corresponde a un alumno activo.', 'error');
          return;
        }
        await new Promise((r) => setTimeout(r, 300));
      }
    } catch (e) {
      $('escaner-nota').textContent = 'No se pudo usar la cámara. Revise el permiso del navegador o busque por nombre.';
    }
  }

  // ---------- Salir ----------
  async function salir() {
    if (estado.cola.length) {
      avisar(`Hay ${estado.cola.length} registro(s) sin enviar. Conéctese y envíelos antes de salir.`, 'error');
      ir('pendientes');
      return;
    }
    try { await pedir('/api/auth/logout', { method: 'POST', body: '{}' }); } catch (e) { /* se borra igual */ }
    try {
      (await abrirBd()).close();
      conexionBd = null;
      indexedDB.deleteDatabase(BD);
      for (const clave of await caches.keys()) await caches.delete(clave);
    } catch (e) { /* nada que borrar */ }
    window.location.assign('/ingresar');
  }

  // ---------- Arranque ----------
  async function iniciar() {
    for (const b of document.querySelectorAll('[data-ir]')) b.addEventListener('click', () => ir(b.dataset.ir === 'alumno' && estado.alumno ? 'alumno' : b.dataset.ir));
    $('busqueda').addEventListener('input', buscar);
    $('form-codigo').addEventListener('submit', validarCodigo);
    $('boton-no-autorizado').addEventListener('click', abrirProtocolo);
    $('form-incidente').addEventListener('submit', registrarIncidente);
    $('boton-sincronizar').addEventListener('click', () => { estado.espera = 5000; sincronizar(); });
    $('boton-actualizar').addEventListener('click', () => { document.querySelector('.menu-usuario').open = false; actualizarPadron(true); });
    $('boton-salir').addEventListener('click', salir);
    window.addEventListener('online', () => marcarRed(true));
    window.addEventListener('offline', () => marcarRed(false));
    if ('BarcodeDetector' in window && navigator.mediaDevices) {
      $('boton-escanear').hidden = false;
      $('boton-escanear').addEventListener('click', escanear);
    }

    estado.padron = (await leer('padron')) || null;
    if (estado.padron && estado.padron.usuario !== usuario) estado.padron = null;  // padrón de otra cuenta
    estado.cola = (await leerCola()) || [];
    await aplicarColaAlPadron();
    pintarRed();
    pintarPadron();
    pintarSalones();
    pintarContador();
    buscar();
    await actualizarPadron();
    sincronizar();
    setInterval(() => { pintarPadron(); if (estado.enLinea) actualizarPadron(); }, 60 * 1000);

    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/porteria/sw.js', { scope: '/porteria/' }).catch(() => {});
  }

  iniciar();
})();
