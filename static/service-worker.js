"use strict";

const CACHE_NAME = "s50ttt-pwa-1.25.0";
const APP_SHELL = [
  "/static/app.css",
  "/static/app.js",
  "/static/pwa.js",
  "/static/offline.html",
  "/static/icons/app-icon.svg",
  "/static/icons/app-icon-180.png",
  "/static/icons/app-icon-192.png",
  "/static/icons/app-icon-512.png",
  "/app.webmanifest"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(function (cache) { return cache.addAll(APP_SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (key) {
          return key.startsWith("s50ttt-pwa-") && key !== CACHE_NAME;
        }).map(function (key) { return caches.delete(key); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match("/static/offline.html");
      })
    );
    return;
  }

  if (url.pathname.startsWith("/static/") || url.pathname === "/app.webmanifest") {
    event.respondWith(
      caches.match(request).then(function (cached) {
        if (cached) return cached;
        return fetch(request).then(function (response) {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then(function (cache) { cache.put(request, copy); });
          }
          return response;
        });
      })
    );
  }
});
