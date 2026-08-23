// Service worker mínimo. Su sola presencia es uno de los requisitos que
// Safari/iOS revisa para permitir "Añadir a pantalla de inicio" como app
// instalable (junto con el manifest y los íconos). No necesitamos que
// haga cache offline real, porque Zora siempre necesita hablar con el
// backend en vivo de todas formas.
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  // Deja pasar todas las peticiones normalmente (sin cache propio).
  event.respondWith(fetch(event.request));
});
