// Bump on every rebrand/icon change: the activate handler deletes all caches
// whose name differs, which is what evicts stale icons from installed PWAs.
const CACHE_NAME = 'paperlessbrain-v5';
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

// Web Push (proactive/notify/webpush.py): title, short body, a link. No
// buttons — iOS shows none; tapping opens the link, where the reactions are.
self.addEventListener('push', (event) => {
    let d = {};
    try { d = event.data ? event.data.json() : {}; } catch (e) { d = { title: event.data && event.data.text() }; }
    event.waitUntil(
        self.registration.showNotification(d.title || 'PaperlessBrain', {
            body: d.body || '',
            icon: '/static/icon-v3-192.png',
            badge: '/static/icon-v3-192.png',
            tag: d.tag || undefined,
            data: { url: d.url || '/' },
        })
    );
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const url = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin);
    // Only ever open our own origin, whatever the payload says.
    const target = url.origin === self.location.origin ? url.href : self.location.origin + '/';
    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
            for (const w of wins) {
                if (new URL(w.url).origin === self.location.origin && 'navigate' in w) {
                    return w.focus().then(() => w.navigate(target));
                }
            }
            return self.clients.openWindow(target);
        })
    );
});
