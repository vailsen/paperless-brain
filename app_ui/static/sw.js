// Bump on every rebrand/icon change: the activate handler deletes all caches
// whose name differs, which is what evicts stale icons from installed PWAs.
const CACHE_NAME = 'paperlessbrain-v3';
const WS_PREFIXES = ['wss://', 'ws://'];
const SKIP_PATHS = ['/_nicegui/', '/socket.io', '/manifest.json', '/sw.js'];

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE_NAME));
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((names) =>
            Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', (event) => {
    const url = event.request.url;
    if (
        WS_PREFIXES.some((p) => url.startsWith(p)) ||
        SKIP_PATHS.some((p) => url.includes(p))
    ) {
        return;
    }
    event.respondWith(
        caches.match(event.request)
            .then((cached) => cached || fetch(event.request))
            .catch(() => new Response('', { status: 503, statusText: 'Offline' }))
    );
});

self.addEventListener('message', (event) => {
    if (event.data === 'keepalive') event.ports[0].postMessage('alive');
});
