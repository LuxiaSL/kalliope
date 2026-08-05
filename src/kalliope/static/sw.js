// kalliope service worker — deliberately does almost nothing.
// Its presence satisfies PWA installability; it must NEVER cache the
// live stream or /now (a radio that plays yesterday is a bug we already
// fixed once, in the client). No fetch handler = everything hits the
// network exactly as before.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(clients.claim()));
