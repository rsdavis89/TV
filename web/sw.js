/* Offline support for the app shell.

   The shell is fetched from the network first and only falls back to the cache
   when the network fails. That way a new deploy shows up the next time you open
   the app, rather than a version behind — cache-first would serve yesterday's
   JavaScript and only update in the background.

   Everything else (icons) is cache-first, and API calls are never cached
   because stale data is worse than no data here. */

const CACHE = 'tv-tracker-v2';
const SHELL = [
  '/',
  '/index.html',
  '/styles.css',
  '/app.js',
  '/manifest.webmanifest',
  '/icons/icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

function save(request, response) {
  if (response.ok) {
    const copy = response.clone();
    caches.open(CACHE).then((cache) => cache.put(request, copy));
  }
  return response;
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api')) return;

  const isShell = event.request.mode === 'navigate'
    || url.pathname === '/'
    || /\.(?:html|js|css|webmanifest)$/.test(url.pathname);

  if (isShell) {
    event.respondWith(
      fetch(event.request)
        .then((response) => save(event.request, response))
        .catch(() => caches.match(event.request).then((hit) => hit || caches.match('/index.html')))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((hit) => (
      hit || fetch(event.request).then((response) => save(event.request, response))
    ))
  );
});
