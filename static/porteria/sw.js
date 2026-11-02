// Service worker de portería: guarda la aplicación para abrirla sin conexión.
// Los datos (padrón y cola) no pasan por aquí: viven en IndexedDB y la API nunca se guarda en caché.
'use strict';

const VERSION = 'porteria-v1';
const APLICACION = '/porteria/';
const ARCHIVOS = [
  APLICACION,
  '/porteria/manifest.webmanifest',
  '/static/porteria/porteria.css',
  '/static/porteria/porteria.js',
  '/static/porteria/icono-192.png',
  '/static/vendor/bootstrap-icons/bootstrap-icons.min.css',
  '/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2',
  '/static/vendor/inter/inter-latin-400-normal.woff2',
  '/static/vendor/inter/inter-latin-600-normal.woff2',
  '/static/vendor/inter/inter-latin-700-normal.woff2',
];

self.addEventListener('install', (evento) => {
  evento.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(ARCHIVOS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (evento) => {
  evento.waitUntil(caches.keys()
    .then((claves) => Promise.all(claves.filter((c) => c !== VERSION).map((c) => caches.delete(c))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (evento) => {
  const pedido = evento.request;
  const url = new URL(pedido.url);
  if (pedido.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;

  if (pedido.mode === 'navigate' && url.pathname === APLICACION) {
    // Primero la red (sesión y versión al día); sin red, la copia guardada
    evento.respondWith(fetch(pedido).then((respuesta) => {
      if (respuesta.ok && !respuesta.redirected) {
        const copia = respuesta.clone();
        caches.open(VERSION).then((cache) => cache.put(APLICACION, copia));
      }
      return respuesta;
    }).catch(() => caches.match(APLICACION)));
    return;
  }

  if (url.pathname.startsWith('/static/') || url.pathname === '/porteria/manifest.webmanifest') {
    // Archivos estáticos: la copia guardada, y en segundo plano se refresca
    evento.respondWith(caches.match(pedido, { ignoreSearch: true }).then((guardada) => {
      const red = fetch(pedido).then((respuesta) => {
        if (respuesta.ok) caches.open(VERSION).then((cache) => cache.put(pedido, respuesta.clone()));
        return respuesta;
      }).catch(() => guardada);
      return guardada || red;
    }));
  }
});
