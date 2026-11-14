// Bump on every rebrand/icon change: the activate handler deletes all caches
// whose name differs, which is what evicts stale icons from installed PWAs.
const CACHE_NAME = 'paperlessbrain-v6';
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
            // Status-bar icon: alpha only, so a transparent mark (make_icons.py).
            badge: '/static/badge-v3-96.png',
            tag: d.tag || undefined,
            // Same tag = same finding: without renotify a second message about
            // it (the research result, a snoozed one waking up) replaced the
            // first one silently — no sound, no banner.
            renotify: !!d.tag,
            data: { url: d.url || '/' },
        })
    );
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const url = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin);
    // Only ever open our own origin, whatever the payload says. A link built
    // for another address of this app (PROACTIVE_APP_BASE_URL vs. the LAN
    // address the app was installed from) keeps its path on ours — it used
    // to become the dashboard.
    const target = self.location.origin + url.pathname + url.search;
    // An open app window is focused and sent to the finding. navigate()
    // rejects for a window this worker does not control (includeUncontrolled
    // returns those too) — the window was focused and stayed on the
    // dashboard. So the page is also told by message (layout.PUSH_OPEN_JS),
    // and with no window left a new one opens.
    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (wins) => {
            const ours = wins.filter((w) => new URL(w.url).origin === self.location.origin);
            for (const w of ours) {
                try { await w.focus(); } catch (e) { /* not focusable: still try to send it */ }
                try {
                    if ('navigate' in w && await w.navigate(target)) return;
                } catch (e) { /* uncontrolled: fall through to the message */ }
                try { w.postMessage({ type: 'pb-open', url: target }); return; } catch (e) { /* next */ }
            }
            return self.clients.openWindow(target);
        })
    );
});
