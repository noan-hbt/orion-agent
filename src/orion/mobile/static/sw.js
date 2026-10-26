// Orion's service worker (D125): notifications when the app is closed, and opening it on a tap.
// The push payload arrives decrypted by the browser; nothing is cached here: the conversation
// lives on the server, and the app always loads the current version of itself.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let message = { title: "Orion", body: "" };
  try {
    message = { ...message, ...event.data.json() };
  } catch (error) {
    message.body = event.data ? event.data.text() : "";
  }
  event.waitUntil(
    self.registration.showNotification(message.title, {
      body: message.body,
      tag: message.tag,
      data: { url: "/app/" + (message.tag ? "#" + message.tag : "") },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/app/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      for (const window of windows) {
        if (window.url.includes("/app/")) {
          window.navigate(url);
          return window.focus();
        }
      }
      return self.clients.openWindow(url);
    }),
  );
});
