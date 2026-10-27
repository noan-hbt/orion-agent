// Orion's service worker (D125): notifications when the app is closed, and opening it on a tap.
// The push payload arrives decrypted by the browser; nothing is cached here: the conversation
// lives on the server, and the app always loads the current version of itself.
// A tap opens the app on what the notification was about (D140): `#message:<ref>` or
// `#approval:<id>`. Where the platform shows actions (not iOS), a request can be refused from
// the notification itself; saying yes always goes through the app and Face ID.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

const KINDS = new Set(["message", "approval"]);

function target(kind, tag) {
  if (!tag) return "/app/";
  return "/app/#" + (KINDS.has(kind) ? kind + ":" : "") + encodeURIComponent(tag);
}

self.addEventListener("push", (event) => {
  let message = { title: "Orion", body: "" };
  try {
    message = { ...message, ...event.data.json() };
  } catch (error) {
    message.body = event.data ? event.data.text() : "";
  }
  const kind = KINDS.has(message.kind) ? message.kind : "message";
  const actions =
    kind === "approval"
      ? [
          { action: "open", title: "Voir" },
          { action: "refuse", title: "Refuser" },
        ]
      : [{ action: "reply", title: "Répondre" }];
  event.waitUntil(
    self.registration.showNotification(message.title, {
      body: message.body,
      tag: message.tag,
      actions,
      data: { url: target(kind, message.tag), kind, tag: message.tag },
    }),
  );
});

async function refuse(id) {
  // The session cookie goes along (same origin); refusing needs no Face ID (D125).
  const response = await fetch(`/app/approvals/${encodeURIComponent(id)}/refuse`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: "{}",
  });
  return self.registration.showNotification("Orion", {
    body: response.ok ? "C'est non : je ne le fais pas." : "Je n'ai pas pu refuser d'ici : ouvre l'app.",
    tag: `refused:${id}`,
  });
}

async function open(url, data) {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const window of windows) {
    if (window.url.includes("/app/")) {
      // Already open: tell it where to go rather than reloading it.
      window.postMessage({ open: data });
      return window.focus();
    }
  }
  return self.clients.openWindow(url);
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const url = data.url || "/app/";
  if (event.action === "refuse" && data.tag) {
    event.waitUntil(refuse(data.tag));
    return;
  }
  const where = { kind: data.kind || "message", tag: data.tag || "", reply: event.action === "reply" };
  event.waitUntil(open(event.action === "reply" ? url.replace(/^\/app\/#.*$/, "/app/#reply") : url, where));
});
