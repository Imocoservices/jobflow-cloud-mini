self.addEventListener("install", e=>{
  e.waitUntil(
    caches.open("jobflow-v1").then(c=>c.addAll([
      "/mobile",
      "/static/mobile.css",
      "/static/mobile.js",
      "/manifest.webmanifest"
    ]))
  );
});
self.addEventListener("fetch", e=>{
  e.respondWith(
    caches.match(e.request).then(res=> res || fetch(e.request))
  );
});
