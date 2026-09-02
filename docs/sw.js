/* Service worker: makes the aisle app work with no signal, which is the
   normal condition in the middle of a grocery store.

   CACHE is stamped with a content hash at build time, so the daily rebuild
   invalidates yesterday's recalls sitting in someone's phone cache. */
const CACHE = "recall-radar-867822b714fe";

const ASSETS = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-180.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  // Navigations go to the network first. The page carries the recall data, so
  // serving a stale cached copy when a fresh one is available would answer with
  // yesterday's recalls for no reason. The cache is the offline fallback only.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put("./index.html", copy));
          return res;
        })
        .catch(() => caches.match("./index.html").then((r) => r || caches.match("./")))
    );
    return;
  }

  // Icons and the manifest never change within a build: cache first.
  event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
});
