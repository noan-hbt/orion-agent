// Orion's app (D125): pairing, signing in, four tabs (now, the conversation, his notebook,
// the settings that replace the server), approvals, notifications.
// No framework and no build: one module, the DOM built by hand, nothing Orion or a page
// wrote ever parsed as HTML (text goes in as text; links are made from it).

const BASE = "/app/";
const app = document.getElementById("app");
const layer = document.getElementById("layer");

// -- small tools ------------------------------------------------------------------------------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el && key !== "list") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const ICONS = {
  orion: '<circle cx="12" cy="12" r="6" stroke="#fff" stroke-width="1.7"/><circle cx="18" cy="6" r="1.6" fill="#fff" stroke="none"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2.5" fill="currentColor"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  photo: '<rect x="3" y="5" width="18" height="14" rx="3"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-8 8"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  file: '<path d="M6 3h8l4 4v14H6zM14 3v4h4"/>',
  bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0"/>',
  menu: '<circle cx="5" cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="19" cy="12" r="1.4" fill="currentColor"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  face: '<path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2M9 9v1M15 9v1M12 9v4h-1M9 16c1.5 1 4.5 1 6 0"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="3"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  where: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5V14M12 17v.5"/>',
  wrong: '<path d="M4 20l4-1 11-11-3-3L5 16zM14 6l3 3"/>',
  clock: '<circle cx="12" cy="13" r="7"/><path d="M12 10v3l2 2M9 3h6"/>',
  pin: '<path d="M9 4h6l-1 6 3 3H7l3-3zM12 13v7"/>',
forget: '<path d="M5 7h14M10 7V5h4v2M7 7l1 13h8l1-13"/>',
  reply: '<path d="M10 8L4 13l6 5M4 13h10a6 6 0 0 1 6 6"/>',
  moon: '<path d="M20 14A8 8 0 0 1 10 4a8 8 0 1 0 10 10z"/>',
  door: '<path d="M14 4h5v16h-5M10 8l-4 4 4 4M6 12h9"/>',
  spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M3 8l9 6 9-6"/>',
  back: '<path d="M15 5l-7 7 7 7"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  calendar: '<rect x="4" y="5" width="16" height="15" rx="3"/><path d="M4 10h16M9 3v4M15 3v4"/>',
  now: '<circle cx="12" cy="12" r="6"/><circle cx="18" cy="6" r="1.6" fill="currentColor" stroke="none"/>',
  chat: '<path d="M4 11.5C4 7.4 7.6 4.5 12 4.5s8 2.9 8 7-3.6 7-8 7c-1 0-2-.1-2.9-.4L5 19.5l1.2-3.3C4.8 15 4 13.3 4 11.5z"/>',
  book: '<path d="M6 4h11a2 2 0 0 1 2 2v14H8a2 2 0 0 1-2-2z"/><path d="M6 16a2 2 0 0 1 2-2h11"/>',
  sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  server: '<rect x="4" y="4" width="16" height="7" rx="2"/><rect x="4" y="13" width="16" height="7" rx="2"/><path d="M8 7.5h.01M8 16.5h.01"/>',
  blocks: '<rect x="4" y="4" width="7" height="7" rx="2"/><rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/><path d="M16.5 13v7M13 16.5h7"/>',
  phone: '<rect x="7" y="3" width="10" height="18" rx="3"/><path d="M11 18h2"/>',
  toml: '<path d="M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h4"/>',
  chevron: '<path d="M9 5l7 7-7 7"/>',
  people: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0M16 5.2a3 3 0 0 1 0 5.6M17.5 14.2A5.5 5.5 0 0 1 20.5 19"/>',
  rules: '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
  wave: '<path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3M16 7l3 3M14 9l2 2"/>',
};

function icon(name, size = 20, stroke = "currentColor") {
  const wrap = document.createElement("span");
  wrap.setAttribute("aria-hidden", "true");
  wrap.style.display = "inline-flex";
  wrap.innerHTML =
    `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="${stroke}" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name]}</svg>`;
  return wrap;
}

function toast(text, ms = 2400) {
  const el = h("div", { class: "toast", role: "status" }, text);
  layer.append(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  }, ms);
}

function spinner() {
  return h("span", { class: "spinner", "aria-hidden": "true" }, Array.from({ length: 12 }, (_, i) => {
    const spoke = h("i");
    spoke.style.setProperty("--i", i);
    return spoke;
  }));
}

function dialogFocus(el) {
  const before = document.activeElement;
  el.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const targets = [...el.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), textarea, select, [tabindex="0"]')].filter((target) => target.getClientRects().length);
    if (!targets.length) return event.preventDefault();
    const first = targets[0];
    const last = targets[targets.length - 1];
    if (event.shiftKey && document.activeElement === first) (event.preventDefault(), last.focus());
    else if (!event.shiftKey && document.activeElement === last) (event.preventDefault(), first.focus());
  });
  el.querySelector("button, input, textarea, select, a[href]")?.focus({ preventScroll: true });
  return () => before?.isConnected && before.focus({ preventScroll: true });
}

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(path, body, method = body === undefined ? "GET" : "POST") {
  const response = await fetch(BASE + path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data.error || response.statusText);
  return data;
}

const FRENCH_ERRORS = {
  "sign in": "Ta session a expiré : reconnecte-toi.",
  "nothing to send": "Rien à envoyer.",
  "malformed request": "Requête refusée.",
};

function said(error) {
  if (error instanceof ApiError) return FRENCH_ERRORS[error.message] || error.message;
  if (error && error.name === "NotAllowedError") return "Annulé.";
  return "Pas de connexion avec Orion.";
}

// -- passkeys ---------------------------------------------------------------------------------

function fromB64url(text) {
  const plain = text.replace(/-/g, "+").replace(/_/g, "/");
  const padded = plain + "=".repeat((4 - (plain.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0)).buffer;
}

function toB64url(buffer) {
  let text = "";
  for (const byte of new Uint8Array(buffer)) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function creation(options) {
  return {
    ...options,
    challenge: fromB64url(options.challenge),
    user: { ...options.user, id: fromB64url(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((c) => ({ ...c, id: fromB64url(c.id) })),
  };
}

function request(options) {
  return {
    ...options,
    challenge: fromB64url(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((c) => ({ ...c, id: fromB64url(c.id) })),
  };
}

function credentialJSON(credential) {
  const response = credential.response;
  const json = {
    id: credential.id,
    rawId: toB64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment || undefined,
    clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {},
    response: { clientDataJSON: toB64url(response.clientDataJSON) },
  };
  if (response.attestationObject) {
    json.response.attestationObject = toB64url(response.attestationObject);
    json.response.transports = response.getTransports ? response.getTransports() : [];
  } else {
    json.response.authenticatorData = toB64url(response.authenticatorData);
    json.response.signature = toB64url(response.signature);
    json.response.userHandle = response.userHandle ? toB64url(response.userHandle) : null;
  }
  return json;
}

async function sign(options) {
  const credential = await navigator.credentials.get({ publicKey: request(options) });
  return credentialJSON(credential);
}

// -- the state of the app ---------------------------------------------------------------------

const state = {
  device: null,
  pushKey: null,
  messages: [], // as the server shows them, by seq
  refs: new Set(),
  more: false,
  pending: [], // sent from here, not yet confirmed
  approvals: new Map(),
  typing: false,
  live: false,
  attached: [], // files picked for the next message
  draft: "",
  tab: "now",
};

try {
  state.draft = localStorage.getItem("orion.draft") || "";
  state.tab = localStorage.getItem("orion.tab") || "now";
} catch {
  // private mode: no draft kept
}

function keepDraft(text) {
  state.draft = text;
  try {
    localStorage.setItem("orion.draft", text);
  } catch {
    // nothing to do: the draft only lives in this page then
  }
}

// -- viewport: the keyboard shrinks the visual viewport, not the layout ----------------------

const view = {};
let roomy = 0; // the tallest the app has been without a keyboard, for this orientation

function fit() {
  // With the keyboard up, iOS shrinks the visual viewport and may slide it: the app follows it,
  // so the composer sits right on the keyboard and nothing is pushed off the top.
  const vv = window.visualViewport;
  const height = vv ? vv.height : window.innerHeight;
  const editing = !!document.activeElement?.matches?.("input, textarea, select, [contenteditable]");
  if (!editing) roomy = height;
  // The last message stays in view while the keyboard takes its room.
  const stick = !!view.thread?.isConnected && !view.talk?.hidden && nearBottom();
  document.documentElement.style.setProperty("--app-h", `${height}px`);
  document.documentElement.style.setProperty("--app-top", `${vv ? Math.max(0, vv.offsetTop) : 0}px`);
  // The composer gives back the dock's room in the same frame the app shrinks, not on focus:
  // otherwise it drops, then jumps back up once the keyboard is there.
  document.body.classList.toggle("keyboard", editing && (window.innerHeight - height > 120 || roomy - height > 120));
  window.scrollTo(0, 0);
  if (stick) view.thread.scrollTop = view.thread.scrollHeight;
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", fit);
  window.visualViewport.addEventListener("scroll", fit);
}
window.addEventListener("resize", fit);
window.addEventListener("orientationchange", () => (roomy = 0));
addEventListener("focusout", () => setTimeout(fit, 0));
fit();

// -- start ------------------------------------------------------------------------------------

async function start() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register(`${BASE}sw.js`, { scope: BASE }).catch((error) => {
      console.warn("service worker", error);
    });
  }
  try {
    const me = await api("me");
    state.device = me.device;
    state.pushKey = me.push_key;
    await conversation();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      welcome();
    } else {
      offlineStart();
    }
  }
}

function offlineStart() {
  app.replaceChildren(
    h(
      "section",
      { class: "welcome" },
      h("img", { class: "mark", src: `${BASE}icon-180.png`, alt: "" }),
      h("h1", {}, "Orion ne répond pas"),
      h("p", { class: "lead" }, "Le serveur est injoignable pour l'instant. Je réessaie tout seul."),
      h("button", { class: "pill dark wide", onclick: start }, "Réessayer"),
    ),
  );
  setTimeout(start, 5000);
}

// -- welcome: sign in, or pair this phone ----------------------------------------------------

function welcome(error = "") {
  clearLayers();
  const pairFirst = new URLSearchParams(location.search).has("code");
  const status = h("p", { class: "error", role: "alert" }, error);
  const signIn = h(
    "button",
    {
      class: "pill primary wide",
      onclick: async () => {
        status.textContent = "";
        signIn.disabled = true;
        try {
          const opened = await api("login/options", {});
          const credential = await sign(opened.options);
          await api("login", { ceremony: opened.ceremony, credential });
          await start();
        } catch (failure) {
          if (failure instanceof ApiError && failure.status === 409) return pairing();
          status.textContent = said(failure);
        } finally {
          signIn.disabled = false;
        }
      },
    },
    icon("face", 20),
    "Entrer avec Face ID",
  );
  app.replaceChildren(
    h(
      "section",
      { class: "welcome" },
      h("img", { class: "mark", src: `${BASE}icon-180.png`, alt: "" }),
      h("h1", {}, "Orion"),
      h("p", { class: "lead" }, "Ta ligne directe avec lui. Rien ne passe sans ta clé, gardée par ton iPhone."),
      h(
        "div",
        { class: "card" },
        signIn,
        h("button", { class: "pill ghost wide", onclick: () => pairing() }, "J'ai un code d'appairage"),
        status,
      ),
    ),
  );
  if (pairFirst) pairing();
}

function pairing() {
  const given = (new URLSearchParams(location.search).get("code") || "").toUpperCase();
  const boxes = Array.from({ length: 8 }, () => h("span"));
  const status = h("p", { class: "error", role: "alert" });
  const input = h("input", {
    type: "text",
    inputMode: "text",
    autocomplete: "one-time-code",
    autocapitalize: "characters",
    spellcheck: false,
    maxLength: 8,
    value: given,
    "aria-label": "Code d'appairage, 8 caractères",
    oninput: () => paint(),
  });
  const go = h(
    "button",
    { class: "pill primary wide", disabled: true, onclick: () => submit() },
    icon("face", 20),
    "Créer ma clé",
  );

  function paint() {
    input.value = input.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
    boxes.forEach((box, i) => {
      box.textContent = input.value[i] || "";
      box.className = (input.value[i] ? "filled" : "") + (i === input.value.length ? " at" : "");
    });
    go.disabled = input.value.length !== 8;
  }

  async function submit() {
    status.textContent = "";
    go.disabled = true;
    try {
      const opened = await api("pair/options", { code: input.value });
      const credential = await navigator.credentials.create({ publicKey: creation(opened.options) });
      await api("pair", { ceremony: opened.ceremony, credential: credentialJSON(credential) });
      history.replaceState(null, "", BASE);
      await start();
    } catch (failure) {
      status.textContent = said(failure);
      go.disabled = false;
    }
  }

  app.replaceChildren(
    h(
      "section",
      { class: "welcome" },
      h("img", { class: "mark", src: `${BASE}icon-180.png`, alt: "" }),
      h("h1", {}, "Appairer cet iPhone"),
      h("p", { class: "lead" }, "Tape le code affiché à la fin de l'installation, ou créé depuis un téléphone déjà appairé (Réglages > Téléphones). Il vaut 10 minutes."),
      h(
        "div",
        { class: "card" },
        h("label", { class: "code" }, input, boxes),
        go,
        status,
        h("p", { class: "hint" }, "Ta clé reste dans ton iPhone, protégée par Face ID. Je n'en garde que la partie publique."),
      ),
      h("button", { class: "pill ghost wide", onclick: () => welcome() }, "Retour"),
    ),
  );
  paint();
  input.focus();
}

// -- the conversation -------------------------------------------------------------------------


async function conversation() {
  view.status = h("span", { class: "status" }, "connexion…");
  view.badge = h("span", { class: "badge", hidden: true });
  view.thread = h("div", { class: "thread", role: "log", "aria-label": "Conversation" });
  view.list = h("div", { class: "messages" });
  view.list.style.display = "contents";
  view.asks = h("div");
  view.asks.style.display = "contents";
  view.typing = h("div", { class: "typing", hidden: true, "aria-label": "Orion écrit" }, h("i"), h("i"), h("i"));
  view.offline = h("p", { class: "offline", hidden: true }, "Hors ligne : je me reconnecte…");
  view.older = h("button", { class: "pill small older", hidden: true, onclick: older }, "Messages plus anciens");
  view.thread.append(view.older, view.list, view.offline, view.typing, view.asks);
  view.scrollBottom = h("button", {
    class: "scroll-bottom glass", type: "button", hidden: true, "aria-label": "Derniers messages",
    onpointerdown: (event) => event.preventDefault(),
    onclick: () => bottom(true, true),
  }, icon("up", 20));

  view.attached = h("div", { class: "attached", hidden: true });
  view.text = h("textarea", {
    rows: 1,
    placeholder: "Message",
    enterKeyHint: "send",
    "aria-label": "Message",
    value: state.draft,
    oninput: () => {
      keepDraft(view.text.value);
      grow();
      sendMode();
    },
    onkeydown: (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && matchMedia("(hover: hover)").matches) {
        event.preventDefault();
        submit();
      }
    },
  });
  view.send = h("button", {
    class: "send", type: "button", onclick: sendOrRecord,
    // Keep the keyboard and composer still until the send click lands (D139).
    onpointerdown: (event) => event.preventDefault(),
  });
  view.field = h("div", { class: "field glass" }, view.text, view.send);
  view.files = h("input", { type: "file", multiple: true, hidden: true, onchange: () => pick(view.files) });
  view.camera = h("input", { type: "file", accept: "image/*", capture: "environment", hidden: true, onchange: () => pick(view.camera) });
  view.photos = h("input", { type: "file", accept: "image/*,video/*", multiple: true, hidden: true, onchange: () => pick(view.photos) });

  const top = h(
    "header",
    { class: "top" },
    h("button", { class: "round glass top-back", type: "button", "aria-label": "Maintenant", onclick: () => show("now") }, icon("back", 20)),
    h(
      "div",
      { class: "who" },
      (view.avatar = h("span", { class: "avatar" }, h("img", { src: `${BASE}orion-avatar.svg`, alt: "" }))),
      h("span", { class: "who-name glass" }, h("h1", {}, "Orion"), h("span", { class: "who-chevron" }, icon("chevron", 10))),
      view.status,
    ),
    h("button", { class: "round glass", type: "button", "aria-label": "Mode voix", onclick: voiceMode }, icon("wave", 20)),
  );

  view.talk = h(
    "section",
    { class: "tab talk-tab", "aria-label": "Conversation" },
    top,
    view.thread,
    view.scrollBottom,
    view.attached,
    h(
      "form",
      { class: "composer", onsubmit: (event) => (event.preventDefault(), submit()) },
      h("button", { class: "round attach", type: "button", "aria-label": "Joindre", onpointerdown: (event) => event.preventDefault(), onclick: actions }, icon("plus", 20)),
      view.field,
    ),
    view.files,
    view.camera,
    view.photos,
  );
  shell();
  grow();
  sendMode();

  let scrollFrame = 0;
  view.thread.addEventListener("scroll", () => {
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = 0;
      if (view.thread.scrollTop < 60 && state.more && !view.loadingOlder) older();
      scrollButton();
    });
  }, { passive: true });
  swipeTimes();
  view.follow = pinToBottom(view.thread, () => !view.talk.hidden);

  state.messages = [];
  state.refs.clear();
  try {
    const page = await api("api/messages?limit=60");
    state.more = page.more;
    for (const message of page.messages) add(message, false);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return welcome();
    toast(said(error));
  }
  if (!state.messages.length) first();
  bottom(true);
  listen();
  offerNotifications();
  show(state.tab);
  grow();
  if (location.hash.length > 1) goTo(location.hash.slice(1));
}

// A tap on a notification opens what it was about (D140): `message:<ref>`, `approval:<id>`, or
// `reply` (the composer). The service worker says it by postMessage when the app is already open.
function goTo(where) {
  history.replaceState(null, "", location.pathname);
  const [kind, ...rest] = decodeURIComponent(where).split(":");
  const id = rest.join(":");
  clearLayers();
  if (kind === "reply") {
    show("talk");
    view.text?.focus();
  } else if (kind === "approval") {
    show("now");
    flash(() => document.querySelector(`.ask[data-id="${CSS.escape(id)}"]`));
  } else {
    show("talk");
    const ref = kind === "message" ? id : where;
    flash(() => view.list?.querySelector(`.msg[data-ref="${CSS.escape(ref)}"]`));
  }
}

function flash(find, tries = 12) {
  const el = find();
  if (!el) {
    if (tries > 0) setTimeout(() => flash(find, tries - 1), 150);
    return;
  }
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

navigator.serviceWorker?.addEventListener("message", (event) => {
  const open = event.data?.open;
  if (!open || !view.thread) return;
  if (open.reply) goTo("reply");
  else if (open.tag) goTo(`${open.kind}:${open.tag}`);
});
addEventListener("hashchange", () => {
  if (view.thread && location.hash.length > 1) goTo(location.hash.slice(1));
});

// The field is measured on a hidden twin: collapsing the real one to measure it lost its caret,
// and iOS slid the whole app up to find it again, one line at a time.
function grow() {
  const text = view.text;
  if (!text.offsetWidth) return;
  if (!view.twin) {
    view.twin = text.cloneNode();
    view.twin.removeAttribute("aria-label");
    view.twin.className = "twin";
    view.twin.tabIndex = -1;
    view.twin.readOnly = true;
    view.twin.setAttribute("aria-hidden", "true");
    view.twin.style.height = "0px";
    text.after(view.twin);
  }
  view.twin.style.width = `${text.offsetWidth}px`;
  view.twin.value = text.value;
  const height = `${Math.min(view.twin.scrollHeight, 118)}px`;
  if (text.style.height === height) return;
  // The thread keeps the same distance to its end, so a taller field never scrolls it.
  const thread = view.thread;
  const fromEnd = thread.scrollHeight - thread.scrollTop - thread.clientHeight;
  text.style.height = height;
  thread.scrollTop = thread.scrollHeight - thread.clientHeight - fromEnd;
}

function sendMode() {
  const empty = !view.text.value.trim() && !state.attached.length;
  if (recorder.active) return;
  const stop = empty && !!state.work;
  view.send.className = `send${stop ? " stopping" : empty ? " mic" : ""}`;
  if (!view.send.querySelector(".send-icon")) {
    view.send.replaceChildren(...[["mic", "mic"], ["send", "up"], ["stop", "stop"]].map(([mode, name]) => h("span", { class: `send-icon mode-${mode}` }, icon(name, 18))));
  }
  view.send.disabled = stop && !!state.work.stopping;
  view.send.setAttribute("aria-label", stop ? "Arrêter Orion" : empty ? "Message vocal" : "Envoyer");
}

// A thread read at its end stays at its end: an image that loads, the dots, a question, a
// taller composer or the keyboard never leave the last message hidden. Only the owner's own
// scrolling unpins it: a scroll the app makes (smooth or clamped) never does, and every bubble is
// watched as it grows, so a message still rising into place is followed to its end.
function pinToBottom(thread, shown) {
  let pinned = true;
  let touching = false;
  let byHand = 0;
  const hand = () => (byHand = performance.now());
  thread.addEventListener("touchstart", () => ((touching = true), hand()), { passive: true });
  thread.addEventListener("touchend", () => ((touching = false), hand()), { passive: true });
  thread.addEventListener("touchcancel", () => ((touching = false), hand()), { passive: true });
  // A mouse dragging the scrollbar is a hand too.
  thread.addEventListener("pointerdown", () => ((touching = true), hand()), { passive: true });
  addEventListener("pointerup", () => (touching = false), { passive: true });
  thread.addEventListener("wheel", hand, { passive: true });
  thread.addEventListener("keydown", hand);
  thread.addEventListener("scroll", () => {
    // Momentum keeps scrolling after the finger lifts: each of its frames carries the hand on.
    if (!touching && performance.now() - byHand > 250) return;
    hand();
    pinned = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 140;
  }, { passive: true });
  const pin = () => {
    if (pinned && shown()) thread.scrollTop = thread.scrollHeight;
  };
  const sizes = new ResizeObserver(pin);
  sizes.observe(thread);
  const watch = (node) => {
    if (!(node instanceof Element)) return;
    if (node.matches(".msg, .typing, .ask, .day")) sizes.observe(node);
    else for (const el of node.querySelectorAll(".msg, .typing, .ask")) sizes.observe(el);
  };
  watch(thread);
  new MutationObserver((changes) => {
    for (const change of changes) change.addedNodes.forEach(watch);
    pin();
  }).observe(thread, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["hidden"] });
  thread.addEventListener("load", pin, true);
  return () => {
    pinned = true;
  };
}

function nearBottom() {
  const t = view.thread;
  return t.scrollHeight - t.scrollTop - t.clientHeight < 140;
}

function bottom(force = false, smooth = false) {
  if (force || nearBottom()) {
    view.follow?.();
    requestAnimationFrame(() => {
      view.thread.scrollTo({ top: view.thread.scrollHeight, behavior: smooth && !matchMedia("(prefers-reduced-motion: reduce)").matches ? "smooth" : "auto" });
      view.unread = false;
      scrollButton();
    });
  }
}

function scrollButton() {
  const near = nearBottom();
  view.scrollBottom.hidden = near;
  if (near) view.unread = false;
  view.scrollBottom.classList.toggle("unread", !!view.unread);
}

function swipeTimes() {
  let start = null;
  let axis = "";
  let x = 0;
  let frame = 0;
  const paint = () => {
    frame = 0;
    view.thread.style.setProperty("--swipe-x", `${x}px`);
    view.thread.style.setProperty("--swipe-progress", String(-x / 64));
  };
  const reset = () => {
    start = null;
    axis = "";
    x = 0;
    view.thread.classList.remove("dragging");
    if (frame) cancelAnimationFrame(frame);
    paint();
  };
  view.thread.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || event.target.closest("button, input, audio, video")) return;
    start = { x: event.clientX, y: event.clientY };
    axis = "";
  });
  view.thread.addEventListener("pointermove", (event) => {
    if (!start) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (!axis && Math.max(Math.abs(dx), Math.abs(dy)) >= 8) {
      axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
      if (axis === "x") {
        view.thread.setPointerCapture(event.pointerId);
        view.thread.classList.add("dragging");
      }
    }
    if (axis !== "x") return;
    event.preventDefault();
    // Resistance keeps a wide drag inside the 64px time gutter (D139).
    x = -64 * (1 - Math.exp(-Math.max(0, -dx) / 64));
    if (!frame) frame = requestAnimationFrame(paint);
  });
  view.thread.addEventListener("pointerup", reset);
  view.thread.addEventListener("pointercancel", reset);
  view.thread.addEventListener("lostpointercapture", reset);
}

// -- messages ---------------------------------------------------------------------------------

const DAY = new Intl.DateTimeFormat("fr-FR", { weekday: "long", day: "numeric", month: "long" });
const TIME = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" });

function dayLabel(date) {
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (date.toDateString() === today.toDateString()) return "Aujourd'hui";
  if (date.toDateString() === yesterday.toDateString()) return "Hier";
  if (date.toDateString() === new Date(Date.now() + 86400000).toDateString()) return "Demain";
  return DAY.format(date);
}

function add(message, live = true) {
  if (state.refs.has(message.ref)) return;
  state.refs.add(message.ref);
  const mine = state.pending.findIndex((p) => p.ref === message.ref);
  if (mine >= 0) {
    state.pending[mine].el.remove();
    state.pending.splice(mine, 1);
  }
  state.messages.push(message);
  const wasNear = nearBottom();
  view.list.querySelector(".first")?.remove();
  const els = render(message, state.messages[state.messages.length - 2]);
  // A message that arrives now rises into place; history does not.
  if (live) for (const el of els) el.classList?.add("new");
  view.list.append(...els);
  retail();
  if (live && (wasNear || message.who === "owner")) bottom(true, true);
  else if (live) {
    view.unread = true;
    scrollButton();
  }
}

function prepend(messages) {
  if (!messages.length) return;
  const before = view.thread.scrollHeight;
  const nodes = [];
  messages.forEach((message, i) => {
    if (state.refs.has(message.ref)) return;
    state.refs.add(message.ref);
    nodes.push(...render(message, messages[i - 1]));
  });
  state.messages = [...messages, ...state.messages];
  view.list.prepend(...nodes);
  retail();
  view.thread.scrollTop += view.thread.scrollHeight - before;
}

async function older() {
  if (!state.messages.length) return;
  view.loadingOlder = true;
  try {
    const page = await api(`api/messages?before=${state.messages[0].seq}&limit=60`);
    state.more = page.more;
    prepend(page.messages);
  } catch (error) {
    toast(said(error));
  } finally {
    view.loadingOlder = false;
    view.older.hidden = !state.more;
  }
}

function retail() {
  // Two-minute groups and fifteen-minute separators follow Messages (D139).
  // A separator left behind by a removed pending bubble no longer heads a message.
  for (const day of view.list.querySelectorAll(".day")) {
    if (!day.nextElementSibling?.classList.contains("msg")) day.remove();
  }
  const items = [...view.list.querySelectorAll(".msg")];
  const latestOwner = items.findLast((el) => el.dataset.who === "owner");
  items.forEach((el, i) => {
    const next = items[i + 1];
    const prev = items[i - 1];
    const gap = prev ? Number(el.dataset.at) - Number(prev.dataset.at) : Infinity;
    const grouped = prev && prev.dataset.who === el.dataset.who && gap <= 120000 && gap >= 0;
    const last = !next || next.dataset.who !== el.dataset.who || Number(next.dataset.at) - Number(el.dataset.at) > 120000;
    el.classList.toggle("tail", last);
    el.classList.toggle("group-start", !grouped);
    el.classList.toggle("other-side", !!prev && prev.dataset.who !== el.dataset.who);
    const bubbles = [...el.children].filter((child) => child.classList.contains("bubble"));
    bubbles.forEach((bubble, index) => bubble.classList.toggle("bubble-tail", index === bubbles.length - 1));
    const date = new Date(Number(el.dataset.at));
    const separator = !prev || gap > 900000 || new Date(Number(prev.dataset.at)).toDateString() !== date.toDateString();
    const day = el.previousElementSibling?.classList.contains("day") ? el.previousElementSibling : null;
    if (separator && !day) el.before(timestamp(date));
    else if (!separator && day) day.remove();
    const receipt = el.querySelector(".delivery");
    if (receipt) {
      receipt.hidden = el !== latestOwner;
      receipt.textContent = items.slice(i + 1).some((later) => later.dataset.who === "orion") ? "Lu" : "Distribué";
    }
    if (el.dataset.who === "orion" && items.slice(i + 1).some((later) => later.dataset.who === "owner")) {
      for (const group of el.querySelectorAll(".quick:not(.answered), .checklist:not(.answered)")) answered(group, []);
    }
  });
  view.older.hidden = !state.more;
}

function timestamp(date) {
  return h("p", { class: "day" }, h("b", {}, dayLabel(date)), " ", TIME.format(date));
}

const GRAPHEMES = new Intl.Segmenter("fr", { granularity: "grapheme" });
function emojiOnly(text) {
  const parts = [...GRAPHEMES.segment(text.replace(/\s/g, ""))].map((part) => part.segment);
  return parts.length >= 1 && parts.length <= 3 && parts.every((part) => /^(?:\p{Extended_Pictographic}|\p{Regional_Indicator}|[0-9#*]\uFE0F?\u20E3)/u.test(part));
}

function render(message, previous) {
  const nodes = [];
  const date = new Date(message.at);
  if (!previous || date - new Date(previous.at) > 900000 || new Date(previous.at).toDateString() !== date.toDateString()) {
    nodes.push(timestamp(date));
  }
  const el = h("div", { class: `msg ${message.who}`, "data-who": message.who, "data-ref": message.ref, "data-at": date.getTime() });
  for (const [index, file] of message.files.entries()) el.append(attachment(message, file, index));
  if (message.blocks && message.blocks.length) el.append(...message.blocks.map(block));
  else if (message.text) el.append(h("div", { class: "bubble" }, ...rich(message.text)));
  el.classList.toggle("emoji-only", !message.files.length && !message.blocks?.some((piece) => piece.type !== "text") && emojiOnly(message.text || ""));
  el.classList.toggle("rich", !!message.blocks?.some((piece) => piece.type !== "text") || /\n/.test(message.text || ""));
  el.append(h("time", { class: "msg-time", dateTime: date.toISOString() }, TIME.format(date)));
  if (message.who === "owner") el.append(h("span", { class: "delivery", hidden: true }, "Distribué"));
  if (message.reactions && message.reactions.length) showReactions(el, message.reactions);
  press(el, message);
  nodes.push(el);
  return nodes;
}

// -- structured pieces (D126): choices, cards, things done --------------------------------------

function block(piece) {
  if (piece.type === "choices") return piece.multiple ? checklist(piece) : quick(piece);
  if (piece.type === "card") return card(piece);
  if (piece.type === "done") return done(piece);
  if (piece.type === "event") return eventPiece(piece);
  if (piece.type === "mail") return mailPiece(piece);
  if (piece.type === "open") return openPiece(piece);
  if (piece.type === "progress") return progressPiece(piece);
  return h("div", { class: "bubble" }, ...rich(piece.text));
}

function answered(group, chosen) {
  if (group.classList.contains("answered")) return;
  group.classList.add("answered");
  for (const b of group.querySelectorAll("button, input")) b.disabled = true;
  for (const b of chosen) b.classList.add("chosen");
  if (group.classList.contains("quick")) setTimeout(() => {
    for (const b of group.querySelectorAll("button:not(.chosen)")) b.hidden = true;
    group.classList.add("collapsed");
  }, 220);
}

function quick(piece) {
  const group = h("div", { class: "quick", role: "group", "aria-label": "Réponses rapides" });
  group.append(
    ...piece.options.map((option) =>
      h(
        "button",
        {
          type: "button",
          onclick: (event) => {
            answered(group, [event.currentTarget]);
            submit(option);
          },
        },
        option,
      ),
    ),
  );
  return group;
}

function checklist(piece) {
  const boxes = piece.options.map((option) => h("input", { type: "checkbox", value: option }));
  const go = h("button", { type: "button", class: "pill primary wide", disabled: true }, piece.send);
  const count = () => {
    const n = boxes.filter((b) => b.checked).length;
    go.disabled = !n;
    go.textContent = n ? `${piece.send} · ${n}` : piece.send;
  };
  const group = h(
    "div",
    { class: "checklist card", role: "group" },
    piece.options.map((option, i) => {
      boxes[i].addEventListener("change", () => {
        boxes[i].closest("label").classList.toggle("on", boxes[i].checked);
        count();
      });
      return h("label", {}, h("span", {}, option), boxes[i], h("span", { class: "checkmark", "aria-hidden": "true" }, icon("check", 16)));
    }),
    go,
  );
  go.addEventListener("click", () => {
    const picked = boxes.filter((b) => b.checked).map((b) => b.value);
    answered(group, [go]);
    submit(`${piece.send} : ${picked.join(", ")}`);
  });
  return group;
}

function card(piece) {
  const body = [
    h("b", { class: "title" }, piece.title),
    piece.subtitle ? h("span", { class: "subtitle" }, piece.subtitle) : null,
    piece.lines.length ? h("ul", {}, piece.lines.map((line) => h("li", {}, ...rich(line)))) : null,
  ];
  if (!piece.link) return h("div", { class: "piece card" }, body);
  return h(
    "a",
    { class: "piece card", href: piece.link, target: "_blank", rel: "noopener noreferrer" },
    body,
    h("span", { class: "go" }, new URL(piece.link).hostname.replace(/^www\./, ""), icon("arrow", 14)),
  );
}

function done(piece) {
  const undo = piece.undo
    ? h(
        "button",
        {
          type: "button",
          class: "pill small",
          onclick: (event) => {
            event.currentTarget.disabled = true;
            event.currentTarget.textContent = "Annulé";
            submit(piece.undo);
          },
        },
        "Annuler",
      )
    : null;
  return h("div", { class: "done card" }, h("span", { class: "ico" }, icon("check", 16, "#fff")), h("span", { class: "what" }, piece.text), undo);
}

// -- records drawn as iOS does (D140): an event, a mail, a screen to open, a progress ------------

const MONTH = new Intl.DateTimeFormat("fr-FR", { month: "short" });
const WEEKDAY = new Intl.DateTimeFormat("fr-FR", { weekday: "long" });
const HOUR = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" });

function eventPiece(piece) {
  const when = new Date(piece.when);
  const end = piece.end ? new Date(piece.end) : null;
  const time = piece.all_day ? "toute la journée" : end ? `${HOUR.format(when)} – ${HOUR.format(end)}` : HOUR.format(when);
  const add = h(
    "button",
    {
      type: "button",
      class: "pill small tinted",
      onclick: (event) => {
        event.currentTarget.disabled = true;
        event.currentTarget.textContent = "Demandé";
        submit(`Ajoute à mon agenda : ${piece.title} — ${piece.all_day ? dayLabel(when) : `${dayLabel(when)} ${HOUR.format(when)}`}${piece.where ? ` — ${piece.where}` : ""}`);
      },
    },
    "Ajouter à l'agenda",
  );
  return h(
    "div",
    { class: "piece event" },
    h("span", { class: "date", "aria-hidden": "true" }, h("small", {}, MONTH.format(when).replace(".", "")), h("b", {}, String(when.getDate()))),
    h(
      "span",
      { class: "about" },
      h("b", { class: "title" }, piece.title),
      h("span", { class: "subtitle" }, `${WEEKDAY.format(when)} · ${time}`),
      piece.where ? h("span", { class: "where" }, icon("where", 13), piece.where) : null,
      add,
    ),
  );
}

function mailPiece(piece) {
  const initial = (piece.from || piece.subject || "?").trim().charAt(0).toUpperCase();
  return h(
    "div",
    { class: "piece mail" },
    h("span", { class: "initial", "aria-hidden": "true" }, initial),
    h(
      "span",
      { class: "about" },
      h("b", { class: "title" }, piece.from || "Mail"),
      piece.subject ? h("span", { class: "subject" }, piece.subject) : null,
      piece.snippet ? h("span", { class: "snippet" }, piece.snippet) : null,
    ),
  );
}

const SCREENS = {
  missions: () => missionsPanel(),
  planned: () => plannedPanel(),
  activity: () => activityPanel(),
  autonomy: () => autonomyPanel(),
  memory: () => memoryPanel(),
  people: () => peoplePanel(),
  papers: () => papersPanel(),
  weeks: () => chroniclePanel(),
  commitments: () => commitmentsPanel(),
  rules: () => rulesPanel(),
  agenda: () => agendaPanel(),
  mail: () => mailPanel(),
  night: () => nightsPanel(),
  settings: () => show("settings"),
};

function openPiece(piece) {
  const go = SCREENS[piece.screen];
  if (!go) return null;
  return h("button", { type: "button", class: "piece open", onclick: go }, h("span", {}, piece.label), icon("chevron", 14));
}

function progressPiece(piece) {
  const fill = h("i", { class: "fill" });
  const el = h(
    "div",
    { class: "piece progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(piece.value * 100)), "aria-label": piece.label },
    h("span", { class: "head" }, h("b", {}, piece.label), h("small", {}, `${Math.round(piece.value * 100)} %`)),
    h("span", { class: "track" }, fill),
  );
  // The bar fills once it is on screen, as iOS does.
  requestAnimationFrame(() => requestAnimationFrame(() => fill.style.setProperty("--value", String(piece.value))));
  return el;
}

function showReactions(el, emoji) {
  el.querySelector(".reactions")?.remove();
  el.classList.toggle("has-reactions", !!emoji.length);
  if (!emoji.length) return;
  (el.querySelector(".bubble") || el).append(h("span", { class: "reactions", "aria-label": `Réactions : ${emoji.join(" ")}` }, [...new Set(emoji)].join("")));
}

function fileUrl(file) {
  return `${BASE}api/files/${file.ref}`;
}

function attachment(message, file, index) {
  const url = fileUrl(file);
  if (file.type.startsWith("image/")) {
    const img = h("img", { class: "photo", src: url, alt: file.name, loading: "lazy" });
    img.addEventListener("click", () => viewer(url, file.name));
    img.addEventListener("load", () => bottom());
    return img;
  }
  if (file.type.startsWith("audio/")) return h("audio", { controls: true, preload: "metadata", src: url });
  if (file.type.startsWith("video/")) return h("video", { class: "photo", controls: true, playsInline: true, preload: "metadata", src: url });
  return h(
    "a",
    { class: "file", href: url, download: file.name, target: "_blank", rel: "noopener" },
    h("span", { class: "tile" }, icon("file", 18)),
    h("span", {}, h("b", {}, file.name), h("small", {}, size(file.size))),
  );
}

function size(bytes) {
  if (bytes < 1024) return `${bytes} o`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} Ko`;
  return `${(bytes / 1024 / 1024).toFixed(1).replace(".", ",")} Mo`;
}

// An address may hold one level of parentheses (Wikipedia's `Paris_(France)`), never a space.
const URL_BODY = String.raw`https?:\/\/(?:[^\s()<>"]|\([^\s()<>"]*\))+`;
// [label](address), <address>, a bare address, **bold** or __bold__, *italic* or _italic_,
// ~~struck~~, `code` (\x60 is the backquote): the only markup the app reads.
const RICH = new RegExp(
  String.raw`\[([^\]\n]+)\]\((${URL_BODY})\)|<(${URL_BODY})>|(${URL_BODY})` +
    String.raw`|\*\*(?=\S)((?:[^*\n]|\*(?!\*))+?)\*\*|__(?=\S)([^_\n]+?)__|~~(?=\S)([^~\n]+?)~~|\x60([^\x60\n]+)\x60` +
    String.raw`|(?<![\w*])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?![\w*])|(?<![\w_])_(?=[^\s_])([^_\n]*?[^\s_])_(?![\w_])`,
  "g",
);
// What closes a sentence is not part of the address it ends on.
const URL_TAIL = /[.,;:!?'’"»\]*_]+$/;

function anchor(href, label) {
  return h("a", { href, target: "_blank", rel: "noopener noreferrer" }, label);
}

function shortUrl(url) {
  const bare = url.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "");
  return bare.length > 48 ? `${bare.slice(0, 47)}…` : bare;
}

function rich(text) {
  // Links, emphasis and `code`, nothing else: the text is never parsed as HTML. A heading reads
  // as a bold line, a starred item as a bullet.
  text = text.replace(/^#{1,6}[ \t]+(.+?)[ \t#]*$/gm, "**$1**").replace(/^([ \t]*)[*+][ \t]+/gm, "$1• ");
  const out = [];
  let at = 0;
  const plain = (piece) => piece && out.push(piece);
  for (const match of text.matchAll(RICH)) {
    const [whole, label, labelled, angled, bare, bold, under, struck, code, em, emUnder] = match;
    plain(text.slice(at, match.index));
    at = match.index + whole.length;
    if (labelled) out.push(anchor(labelled, label.replace(/\*\*|__/g, "")));
    else if (angled) out.push(anchor(angled, shortUrl(angled)));
    else if (bare) {
      const url = bare.replace(URL_TAIL, "");
      at -= bare.length - url.length;
      out.push(anchor(url, shortUrl(url)));
    } else if (code) out.push(h("code", {}, code));
    else if (struck) out.push(h("s", {}, ...rich(struck)));
    else if (em || emUnder) out.push(h("i", {}, ...rich(em || emUnder)));
    else out.push(h("b", {}, ...rich(bold || under)));
  }
  plain(text.slice(at));
  return out;
}

function first() {
  const ask = (text) => () => {
    view.text.value = text;
    keepDraft(text);
    grow();
    sendMode();
    view.text.focus();
  };
  const tile = (name, text, wide = false) =>
    h("button", { class: wide ? "span" : "", onclick: ask(text) }, h("span", { class: "ico" }, icon(name, 18)), text);
  view.list.append(
    h(
      "div",
      { class: "first" },
      h("h2", {}, "Bonjour."),
      h("p", {}, "Tu peux tout me demander ici, comme sur Telegram, et me répondre d'un geste quand j'ai besoin de toi."),
      h(
        "div",
        { class: "bento" },
        tile("calendar", "Qu'est-ce que j'ai cette semaine ?"),
        tile("mail", "Des mails importants ?"),
        tile("clock", "Rappelle-moi demain à 9 h d'appeler la banque", true),
        tile("target", "Trouve-moi un T2 à moins de 700 €"),
        tile("spark", "Qu'est-ce que tu sais de moi ?"),
      ),
    ),
  );
}

// -- sending ----------------------------------------------------------------------------------

function sendOrRecord() {
  if (recorder.active) return recorder.stop();
  if (!view.text.value.trim() && !state.attached.length && state.work) {
    view.send.disabled = true;
    return api("api/stop", {}).catch((error) => (view.send.disabled = false, toast(said(error))));
  }
  if (!view.text.value.trim() && !state.attached.length) return recorder.start();
  submit();
}

async function submit(textOverride) {
  const text = (textOverride ?? view.text.value).trim();
  const files = textOverride === undefined ? state.attached.splice(0) : [];
  if (!text && !files.length) return;
  if (textOverride === undefined) {
    view.text.value = "";
    keepDraft("");
    grow();
    paintAttached();
    sendMode();
  }
  await send(text, files);
}

async function send(text, files) {
  const el = h("div", { class: "msg owner pending tail sending", "data-who": "owner", "data-at": Date.now() });
  for (const f of files) {
    el.append(
      f.preview
        ? h("img", { class: "photo", src: f.preview, alt: f.name })
        : h("span", { class: "file" }, h("span", { class: "tile" }, icon(f.type.startsWith("audio/") ? "mic" : "file", 18)), h("span", {}, h("b", {}, f.name), h("small", {}, size(f.size)))),
    );
  }
  if (text) el.append(h("div", { class: "bubble bubble-tail" }, ...rich(text)));
  el.classList.toggle("emoji-only", !files.length && emojiOnly(text));
  const meta = h("span", { class: "meta" }, "envoi…");
  el.append(meta);
  view.list.querySelector(".first")?.remove();
  view.list.append(el);
  retail();
  view.field.classList.remove("pulse");
  requestAnimationFrame(() => view.field.classList.add("pulse"));
  const entry = { ref: `local-${Date.now()}`, el };
  state.pending.push(entry);
  bottom(true, true);
  try {
    const payload = { text, files: await Promise.all(files.map(encode)) };
    const { message } = await api("api/send", payload);
    el.remove();
    state.pending.splice(state.pending.indexOf(entry), 1);
    add(message); // unless the stream brought it first
  } catch (error) {
    el.classList.remove("pending");
    el.classList.add("failed");
    meta.replaceChildren(
      `${said(error)} · `,
      h(
        "button",
        {
          type: "button",
          onclick: () => {
            el.remove();
            state.pending.splice(state.pending.indexOf(entry), 1);
            send(text, files);
          },
        },
        "Réessayer",
      ),
    );
  }
}

function encode(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, type: file.type, data: String(reader.result).split(",", 2)[1] || "" });
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file.blob);
  });
}

const MAX_FILE = 25 * 1024 * 1024;

function pick(input) {
  for (const blob of input.files) {
    if (blob.size > MAX_FILE) {
      toast(`${blob.name} : trop lourd (25 Mo au plus).`);
      continue;
    }
    state.attached.push({
      blob,
      name: blob.name || "photo.jpg",
      type: blob.type,
      size: blob.size,
      preview: blob.type.startsWith("image/") ? URL.createObjectURL(blob) : null,
    });
  }
  input.value = "";
  paintAttached();
  sendMode();
  view.text.focus();
}

function paintAttached() {
  view.attached.hidden = !state.attached.length;
  view.attached.replaceChildren(
    ...state.attached.map((file, i) =>
      h(
        "div",
        {},
        file.preview ? h("img", { src: file.preview, alt: file.name }) : h("span", { class: "doc" }, file.name),
        h(
          "button",
          {
            type: "button",
            "aria-label": `Retirer ${file.name}`,
            onclick: () => {
              state.attached.splice(i, 1);
              paintAttached();
              sendMode();
            },
          },
          "×",
        ),
      ),
    ),
  );
}

// -- voice ------------------------------------------------------------------------------------

const recorder = {
  active: false,
  async start() {
    if (!navigator.mediaDevices || !window.MediaRecorder) return toast("Le micro n'est pas disponible ici.");
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      return toast("Micro refusé : autorise-le dans Réglages › Safari.");
    }
    const type = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
    const media = new MediaRecorder(stream, type ? { mimeType: type } : {});
    const chunks = [];
    media.ondataavailable = (event) => event.data.size && chunks.push(event.data);
    media.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      clearInterval(this.timer);
      this.active = false;
      view.field.replaceChildren(view.text, view.send);
      sendMode();
      if (this.cancelled) return;
      const mime = (media.mimeType || "audio/mp4").split(";")[0];
      const blob = new Blob(chunks, { type: mime });
      const ext = mime.includes("webm") ? "webm" : "m4a";
      send("", [{ blob, name: `vocal.${ext}`, type: mime, size: blob.size, preview: null }]);
    };
    this.media = media;
    this.cancelled = false;
    this.active = true;
    const started = Date.now();
    const clock = h("span", {}, "0:00");
    this.timer = setInterval(() => {
      const s = Math.floor((Date.now() - started) / 1000);
      clock.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      if (s >= 300) this.stop();
    }, 250);
    view.field.replaceChildren(
      h(
        "div",
        { class: "recorder" },
        h("i"),
        clock,
        h(
          "button",
          { type: "button", class: "pill small ghost", onclick: () => ((this.cancelled = true), media.stop()) },
          "Annuler",
        ),
      ),
      view.send,
    );
    view.send.className = "send recording";
    view.send.replaceChildren(icon("stop", 18));
    view.send.setAttribute("aria-label", "Arrêter et envoyer");
    media.start();
  },
  stop() {
    if (this.media && this.media.state !== "inactive") this.media.stop();
  },
};

// -- live: the stream -------------------------------------------------------------------------

let source = null;
let retry = 1000;

function listen() {
  source?.close();
  source = new EventSource(`${BASE}api/stream`);
  source.onopen = () => {
    retry = 1000;
    setLive(true);
    catchUp();
  };
  source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "ready") {
      setTyping(data.typing);
      setWork(data.work);
      // A question that waited while the app was closed shows at the end of the thread, in view.
      const near = nearBottom();
      loadApprovals().then(() => near && bottom(true));
      countQuestions();
      nowChanged();
    } else if (data.type === "message") {
      // The dots step aside before the message lands where they were.
      if (data.message.who === "orion") setTyping(false, true);
      add(data.message);
      speak(data.message);
      countQuestions();
      nowChanged();
    } else if (data.type === "typing") {
      setTyping(data.on);
      if (!data.on) setWork(null);
    } else if (data.type === "work") {
      setWork(data.work);
    } else if (data.type === "approval") {
      loadApprovals().then(() => bottom(true));
      nowChanged();
    } else if (data.type === "approval_closed") {
      state.approvals.delete(data.id);
      paintApprovals();
      nowChanged();
    } else if (data.type === "changed") {
      changed(data.what || []);
    }
  };
  source.onerror = async () => {
    setLive(false);
    source.close();
    try {
      await api("me");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return welcome("Ta session a expiré.");
    }
    setTimeout(listen, retry);
    retry = Math.min(retry * 2, 30000);
  };
}

// What each tab shows, by the parts the server names when they change (D140).
const TAB_PARTS = {
  now: ["tasks", "missions", "waits", "planned", "night", "agenda"],
  notebook: ["missions", "notebook", "mail", "agenda", "night", "planned"],
  settings: ["cost", "settings", "tasks"],
};
const livePanels = new Set();
const changedParts = new Set();
let changeTimer = null;

function changed(parts) {
  for (const part of parts) changedParts.add(part);
  clearTimeout(changeTimer);
  changeTimer = setTimeout(repaintChanged, 250);
}

function repaintChanged() {
  const parts = [...changedParts];
  changedParts.clear();
  const hit = (watched) => watched.some((part) => parts.includes(part));
  // A quiet cross-fade, not the entrance cascade: the page is already there.
  document.body.classList.add("live-refresh");
  setTimeout(() => document.body.classList.remove("live-refresh"), 700);
  if (TAB_PARTS[state.tab] && hit(TAB_PARTS[state.tab])) view.tabs?.[state.tab]?.refresh();
  for (const live of livePanels) if (hit(live.parts)) live.refresh();
}

async function catchUp() {
  const last = state.messages[state.messages.length - 1];
  if (!last) return;
  try {
    const page = await api(`api/messages?after=${last.seq}&limit=200`);
    for (const message of page.messages) add(message);
  } catch (error) {
    console.warn("catch up", error);
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && view.thread && (!source || source.readyState === EventSource.CLOSED)) listen();
});

function setLive(on) {
  state.live = on;
  view.offline.hidden = on;
  paintStatus();
}

function setTyping(on, keep = false) {
  // Orion may keep working after a message he sends mid-work: the server says when it stops.
  if (keep && state.typing) return typingBubble(false, true);
  const was = state.typing;
  state.typing = on;
  typingBubble(on);
  paintStatus();
  if (on && !was) bottom(false, true);
}

// The dots give their place to the message that ends them in the same frame, so the thread never
// jumps; if Orion keeps working, they come back under it a moment later. Entry and exit are one
// animation at a time, each started from where the last one left the bubble.
function typingBubble(on, comeBack = false) {
  clearTimeout(view.typingExit);
  const el = view.typing;
  const leaving = el.classList.contains("leaving");
  if (on) {
    if (!el.hidden && !leaving) return;
    view.typingMove?.cancel();
    el.classList.remove("leaving");
    el.hidden = false;
    view.typingMove = el.animate(
      [{ opacity: 0, transform: "scale(.2)" }, { opacity: 1, offset: 0.6 }, { opacity: 1, transform: "scale(1)" }],
      { duration: still() ? 0 : 500, easing: "cubic-bezier(.34,1.56,.64,1)" },
    );
  } else if (comeBack) {
    view.typingMove?.cancel();
    el.hidden = true;
    el.classList.remove("leaving");
    view.typingExit = setTimeout(() => {
      if (!state.typing) return;
      typingBubble(true);
      bottom(false, true);
    }, 900);
  } else if (!el.hidden && !leaving) {
    view.typingMove?.cancel();
    el.classList.add("leaving");
    const move = (view.typingMove = el.animate(
      [{}, { opacity: 0, transform: "scale(.3)", height: "0px", marginTop: "0px", marginBottom: "0px" }],
      { duration: still() ? 0 : 260, easing: "cubic-bezier(.32,.72,0,1)", fill: "forwards" },
    ));
    move.onfinish = () => {
      if (view.typingMove !== move) return;
      el.hidden = true;
      el.classList.remove("leaving");
      move.cancel();
    };
  }
}

function still() {
  return matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// -- Orion at work (D133, D169) ----------------------------------------------------------------
// Only the three dots while he works, as anyone typing: what he finds comes as messages. The
// work is kept to stop it from the composer's button.

function elapsed(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")}`;
}

function setWork(work) {
  state.work = work;
  view.avatar.classList.toggle("busy", state.typing || !!work);
  sendMode();
  paintNowWork();
}

function paintStatus() {
  const [text, kind] = !state.live ? ["hors ligne", ""] : state.typing ? ["écrit…", "busy"] : ["en ligne", "live"];
  if (view.status.textContent !== text) {
    view.status.textContent = text;
    view.status.classList.remove("swap");
    void view.status.offsetWidth;
  }
  view.status.className = `status ${kind} swap`;
  view.avatar.classList.toggle("busy", state.typing || !!state.work);
}

// -- approvals: yes takes Face ID, no does not ------------------------------------------------

const ASK_TIME = new Intl.RelativeTimeFormat("fr-FR", { numeric: "auto" });

async function loadApprovals() {
  // The questions are read where they are kept (the guardian, run split), never from the stream.
  try {
    const { approvals } = await api("approvals");
    const ids = new Set(approvals.map((a) => a.id));
    const same = ids.size === state.approvals.size && [...ids].every((id) => state.approvals.has(id));
    if (same) return;
    state.approvals.clear();
    for (const a of approvals) state.approvals.set(a.id, a);
    paintApprovals();
  } catch (error) {
    console.warn("approvals", error);
  }
}

function paintApprovals() {
  paintBadge();
  view.asks.replaceChildren(...[...state.approvals.values()].map(askCard));
}

function paintBadge() {
  // What waits on him: approvals, and the questions Orion waits on him for.
  const count = state.approvals.size + (state.questions || 0);
  if (view.badge.textContent !== String(count)) {
    // A new count pops, so a change is seen from the corner of the eye.
    view.badge.classList.remove("pop");
    void view.badge.offsetWidth;
    view.badge.classList.add("pop");
  }
  view.badge.hidden = !count;
  view.badge.textContent = String(count);
  paintNowWork();
}

async function countQuestions() {
  try {
    state.questions = (await api("api/for-you")).questions.length;
    paintBadge();
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 404)) console.warn("for you", error);
  }
}

function askCard(asked) {
  const left = h("small");
  const tick = () => {
    const minutes = Math.round((new Date(asked.expires_at) - Date.now()) / 60000);
    left.textContent = minutes > 0 ? `sans réponse, ce sera non ${ASK_TIME.format(minutes, "minute")}` : "bientôt non";
  };
  tick();
  const timer = setInterval(() => (card.isConnected ? tick() : clearInterval(timer)), 30000);
  const status = h("p", { class: "error", role: "alert", hidden: true });
  const no = h("button", { class: "pill", type: "button", onclick: () => refuse() }, "Non");
  const card = h(
    "article",
    { class: "ask card", "aria-label": "Orion demande ton accord", "data-id": asked.id },
    h(
      "header",
      {},
      h("span", { class: "ico" }, icon("shield", 18, "#fff")),
      h("span", {}, h("b", {}, "J'ai besoin de ton accord"), left),
    ),
    ...details(asked),
    h("div", { class: "row" }, no, slider("Glisse pour accepter", () => accept())),
    status,
  );

  async function refuse() {
    no.disabled = true;
    try {
      await api(`approvals/${asked.id}/refuse`, {});
      card.classList.add("done");
    } catch (error) {
      status.hidden = false;
      status.textContent = said(error);
      no.disabled = false;
    }
  }

  async function accept() {
    status.hidden = true;
    try {
      const opened = await api(`approvals/${asked.id}/options`, { answer: "yes" });
      const credential = await sign(opened.options);
      await api(`approvals/${asked.id}`, { answer: "yes", ceremony: opened.ceremony, credential });
      card.classList.add("done");
      toast("Accordé.");
      return true;
    } catch (error) {
      status.hidden = false;
      status.textContent = said(error);
      return false;
    }
  }
  return card;
}

const KEYS = {
  to: "À", cc: "Copie", bcc: "Copie cachée", subject: "Objet", body: "Message", text: "Texte",
  title: "Titre", when: "Quand", start: "Début", end: "Fin", url: "Lien", amount: "Montant",
  name: "Nom", path: "Fichier", query: "Recherche", address: "Adresse", phone: "Téléphone",
};

// What an action does, in his words; an unknown tool shows its name.
const TOOL_NAMES = {
  mail_send: "Envoyer un mail", mail_draft: "Préparer un mail", calendar_add: "Ajouter à ton agenda", calendar_change: "Changer ton agenda",
  contact_message: "Écrire à un proche", peer_message: "Écrire à un autre Orion", call_someone: "Appeler quelqu'un",
  meeting_propose: "Proposer un rendez-vous", meeting_accept: "Accepter un rendez-vous", configure_change: "Changer un réglage",
  autonomy_grant: "Me laisser faire seul", module_install: "Installer un module", module_remove: "Retirer un module",
  shell: "Lancer une commande sur le serveur", delegate: "Confier à un autre agent", booking: "Réserver",
  hand_open: "Ouvrir sur ton ordinateur", hand_read_file: "Lire un fichier de ton ordinateur", secret_ask: "Te demander un secret",
};

function toolName(tool) {
  if (TOOL_NAMES[tool]) return TOOL_NAMES[tool];
  const words = tool.replace(/_/g, " ");
  return `${words[0].toUpperCase()}${words.slice(1)}`;
}

function askTitle(asked) {
  // One line for a list: the action, and what it is about.
  const args = asked.arguments || {};
  const about = [args.subject, args.to, args.title, args.text, args.name].find((v) => typeof v === "string" && v);
  if (!asked.tool) return excerpt(asked.question.replace(/^Avant que je le fasse : /, ""), 90);
  return excerpt(about ? `${toolName(asked.tool)} · ${about}` : toolName(asked.tool), 90);
}

function details(asked) {
  // The action and its arguments, readable; the raw question when there is nothing to show.
  const entries = Object.entries(asked.arguments || {}).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length));
  if (!asked.tool) return [h("p", {}, asked.question.replace(/^Avant que je le fasse : /, ""))];
  const value = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 1));
  return [
    h("p", { class: "tool" }, toolName(asked.tool)),
    entries.length && h(
      "dl",
      { class: "args" },
      entries.map(([key, v]) => {
        const text = value(v);
        const dd = h("dd", { class: text.length > 120 ? "long" : "" }, text);
        if (text.length > 120) dd.addEventListener("click", () => dd.classList.toggle("open"));
        return [h("dt", {}, KEYS[key] || key.replace(/_/g, " ")), dd];
      }),
    ),
    asked.reason ? h("p", { class: "why" }, `Je demande parce que ${asked.reason}.`) : null,
  ].filter(Boolean);
}

function slider(label, done) {
  const knob = h("span", { class: "knob" }, icon("arrow", 20));
  const el = h(
    "button",
    {
      class: "slide",
      type: "button",
      "aria-label": `${label} (Face ID)`,
      onclick: (event) => {
        // A tap (or the keyboard) works too: the slide is a gesture, Face ID is the proof.
        if (event.detail === 0 || !moved) finish();
      },
    },
    h("span", { class: "label", "data-label": label }, label),
    knob,
  );
  let startX = 0;
  let x = 0;
  let moved = false;
  let busy = false;
  const max = () => el.clientWidth - knob.clientWidth - 8;
  el.addEventListener("pointerdown", (event) => {
    if (busy) return;
    startX = event.clientX;
    moved = false;
    el.classList.remove("back");
    el.setPointerCapture(event.pointerId);
  });
  el.addEventListener("pointermove", (event) => {
    if (!el.hasPointerCapture(event.pointerId)) return;
    x = Math.max(0, Math.min(max(), event.clientX - startX));
    if (x > 6) moved = true;
    knob.style.transform = `translateX(${x}px)`;
    el.style.setProperty("--p", String(x / max()));
  });
  el.addEventListener("pointerup", () => {
    if (!moved) return;
    if (x >= max() * 0.85) finish();
    else reset();
  });
  el.addEventListener("pointercancel", reset);
  function reset() {
    el.classList.add("back");
    el.classList.remove("ok");
    knob.style.transform = "translateX(0)";
    el.style.setProperty("--p", "0");
    x = 0;
  }
  async function finish() {
    if (busy) return;
    busy = true;
    knob.style.transform = `translateX(${max()}px)`;
    el.style.setProperty("--p", "1");
    el.classList.add("ok");
    knob.replaceChildren(icon("check", 20));
    const ok = await done();
    busy = false;
    if (!ok) {
      knob.replaceChildren(icon("arrow", 20));
      reset();
    }
    setTimeout(() => (moved = false), 0);
  }
  return el;
}

// -- long press on a message ------------------------------------------------------------------

function press(el, message) {
  let timer = null;
  let x = 0;
  let y = 0;
  const cancel = () => {
    clearTimeout(timer);
    timer = null;
    el.classList.remove("pressed");
  };
  el.addEventListener("pointerdown", (event) => {
    if (event.target.closest("a, audio, video, button")) return;
    x = event.clientX;
    y = event.clientY;
    el.classList.add("pressed");
    timer = setTimeout(() => {
      cancel();
      messageMenu(el, message);
    }, 420);
  });
  el.addEventListener("pointermove", (event) => {
    if (timer && Math.hypot(event.clientX - x, event.clientY - y) > 10) cancel();
  });
  el.addEventListener("pointerup", cancel);
  el.addEventListener("pointercancel", cancel);
  el.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    cancel();
    messageMenu(el, message);
  });
  el.tabIndex = 0;
  el.addEventListener("keydown", (event) => {
    if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
      event.preventDefault();
      messageMenu(el, message);
    }
  });
}

// Sheets and panels pile up: Escape closes the one on top.
const open = [];
function stack(close) {
  open.push(close);
}
function unstack(close) {
  const at = open.indexOf(close);
  if (at >= 0) open.splice(at, 1);
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && open.length) {
    event.preventDefault();
    open[open.length - 1]();
  }
});

function overlayState() {
  const panels = [...layer.querySelectorAll(".panel:not(.leaving)")];
  const sheets = [...layer.querySelectorAll(".scrim:not(.leaving)")];
  document.body.classList.toggle("panel-open", !!panels.length);
  document.body.classList.toggle("sheet-open", !!sheets.length);
  app.inert = !!(panels.length || sheets.length || layer.querySelector(".tapback-overlay:not(.leaving), .plus-scrim:not(.leaving)"));
  for (const scrim of layer.querySelectorAll(".scrim")) scrim.inert = scrim !== sheets[sheets.length - 1];
  for (const el of [...Object.values(view.tabs || {}), ...layer.querySelectorAll(".panel")]) {
    el.classList.toggle("panel-behind", panels.length > 0 && el !== panels[panels.length - 1]);
    el.classList.toggle("sheet-behind", sheets.length > 0 && (panels.length ? el === panels[panels.length - 1] : el === view.tabs?.[state.tab]));
    if (el.classList.contains("panel")) el.inert = el !== panels[panels.length - 1] || sheets.length > 0;
  }
}

function clearLayers() {
  layer.replaceChildren();
  view.list?.querySelectorAll(".menu-source").forEach((el) => el.classList.remove("menu-source"));
  open.length = 0;
  overlayState();
}

function sheet(...children) {
  const scrim = h("div", { class: "scrim", onclick: (event) => event.target === scrim && close() });
  const body = h("div", { class: "sheet", role: "dialog", "aria-modal": "true" }, h("span", { class: "grab" }), ...children);
  body.setAttribute("aria-label", body.querySelector("h2")?.textContent || "Actions");
  scrim.append(body);
  let closed = false;
  let restoreFocus = () => {};
  function close() {
    if (closed) return;
    closed = true;
    scrim.classList.add("leaving");
    body.classList.remove("dragging");
    body.style.transform = "";
    setTimeout(() => scrim.remove(), 450);
    unstack(close);
    overlayState();
    restoreFocus();
  }
  stack(close);
  // Swipe down to close.
  let startY = null;
  let startX = 0;
  let dragY = 0;
  let dragFrame = 0;
  const follow = () => {
    dragFrame = 0;
    body.style.setProperty("--sheet-y", `${dragY}px`);
    body.style.transform = `translateY(${dragY}px)`;
  };
  body.addEventListener("touchstart", (event) => {
    startY = null;
    dragY = 0;
    if (body.scrollTop <= 0 && !event.target.closest("input, textarea, select, button, a, audio, video")) {
      startY = event.touches[0].clientY;
      startX = event.touches[0].clientX;
    }
  }, { passive: true });
  body.addEventListener("touchmove", (event) => {
    if (startY === null) return;
    const dy = event.touches[0].clientY - startY;
    if (Math.abs(event.touches[0].clientX - startX) > Math.abs(dy)) return;
    if (dy > 0) {
      event.preventDefault();
      body.classList.add("settled", "dragging");
      dragY = dy;
      if (!dragFrame) dragFrame = requestAnimationFrame(follow);
    }
  }, { passive: false });
  const release = (event) => {
    if (startY === null) return;
    const dy = (event.changedTouches[0]?.clientY ?? startY) - startY;
    startY = null;
    if (dragFrame) cancelAnimationFrame(dragFrame);
    dragFrame = 0;
    body.classList.remove("dragging");
    if (event.type !== "touchcancel" && dy > 90 && dragY) close();
    else {
      body.style.transform = "";
      body.style.setProperty("--sheet-y", "0px");
    }
    dragY = 0;
  };
  body.addEventListener("touchend", release);
  body.addEventListener("touchcancel", release);
  layer.append(scrim);
  overlayState();
  restoreFocus = dialogFocus(body);
  return close;
}

function excerpt(text, max = 160) {
  const plain = text.replace(/\s+/g, " ").trim();
  return plain.length > max ? `${plain.slice(0, max - 1)}…` : plain;
}

function messageMenu(el, message) {
  if (layer.querySelector(".tapback-overlay:not(.leaving)")) return;
  const orion = message.who === "orion";
  const quoted = message.text ? `« ${excerpt(message.text)} »\n` : "";
  const ask = (text) => () => {
    close();
    submit(`${quoted}${text}`);
  };
  const item = (name, text, onclick, danger = false) =>
    h("button", { type: "button", class: danger ? "danger" : "", onclick }, icon(name, 20), text);
  const reactions = orion
    ? h(
        "div",
        { class: "emoji tapback-picker", role: "group", "aria-label": "Réagir" },
        ["❤️", "👍", "👎", "😂", "‼️", "❓"].map((emoji, i) => {
          const button = h(
            "button",
            {
              type: "button",
              "aria-label": emoji,
              "aria-pressed": String((message.reactions || []).includes(emoji)),
              class: (message.reactions || []).includes(emoji) ? "selected" : "",
              onclick: async () => {
                navigator.vibrate?.(8);
                close();
                try {
                  await api("api/react", { ref: message.ref, emoji });
                  message.reactions = [...(message.reactions || []), emoji];
                  showReactions(el, message.reactions);
                } catch (error) {
                  toast(said(error));
                }
              },
            },
            emoji,
          );
          button.style.setProperty("--i", i);
          return button;
        }),
      )
    : null;
  const menu = h(
      "div",
      { class: "list tapback-menu" },
      message.text
        ? item("copy", "Copier", async () => {
            close();
            try {
              await navigator.clipboard.writeText(message.text);
              toast("Copié.");
            } catch {
              toast("Copie impossible ici.");
            }
          })
        : null,
      item("reply", "Répondre à ce message", () => {
        close();
        view.text.value = `${quoted}${view.text.value}`;
        keepDraft(view.text.value);
        grow();
        sendMode();
        view.text.focus();
      }),
      orion ? item("where", "D'où tu sais ça ?", ask("D'où tu sais ça ? Donne-moi tes sources.")) : null,
      orion ? item("wrong", "C'est faux, corrige", ask("C'est faux. Corrige et retiens-le.")) : null,
      item("clock", "En faire un rappel", ask("Fais-en un rappel : propose-moi un moment.")),
      item("forget", "Oublie ça", ask("Oublie ça : ne le garde pas en mémoire."), true),
    );
  const clone = el.cloneNode(true);
  clone.classList.remove("new", "sending", "pressed");
  clone.removeAttribute("tabindex");
  clone.removeAttribute("data-ref");
  clone.inert = true;
  clone.querySelectorAll(".delivery, .meta, .msg-time").forEach((node) => node.remove());
  const lift = h("div", { class: "tapback-lift", "aria-hidden": "true" }, clone);
  const overlay = h("div", {
    class: "tapback-overlay", role: "dialog", "aria-modal": "true", "aria-label": "Actions du message",
    onclick: (event) => event.target === overlay && close(),
  }, lift, reactions, menu);
  const bounds = el.getBoundingClientRect();
  const surface = app.getBoundingClientRect();
  const viewport = window.visualViewport;
  const offsetTop = viewport?.offsetTop || 0;
  const height = viewport?.height || window.innerHeight;
  const width = Math.min(330, surface.width - 32);
  const menuHeight = menu.children.length * 44;
  const liftHeight = Math.min(bounds.height, Math.max(24, height - menuHeight - (orion ? 120 : 40)));
  const top = Math.max(orion ? 86 : 16, Math.min(bounds.top - offsetTop, height - liftHeight - menuHeight - 26));
  const left = orion ? Math.max(16, bounds.left - surface.left) : Math.max(16, surface.width - 266);
  overlay.style.top = `${offsetTop}px`;
  overlay.style.height = `${height}px`;
  overlay.style.bottom = "auto";
  lift.style.left = `${bounds.left - surface.left}px`;
  lift.style.top = `${top}px`;
  lift.style.width = `${bounds.width}px`;
  lift.style.maxHeight = `${liftHeight}px`;
  if (reactions) {
    reactions.style.left = `${Math.min(left, surface.width - width - 16)}px`;
    reactions.style.top = `${top - 72}px`;
    reactions.style.width = `${width}px`;
  }
  menu.style.left = `${Math.min(left, surface.width - 266)}px`;
  menu.style.top = `${top + liftHeight + 12}px`;
  let closed = false;
  let restoreFocus = () => {};
  function close() {
    if (closed) return;
    closed = true;
    overlay.classList.add("leaving");
    el.classList.remove("menu-source");
    setTimeout(() => overlay.remove(), 250);
    unstack(close);
    overlayState();
    restoreFocus();
  }
  el.classList.add("menu-source");
  stack(close);
  layer.append(overlay);
  overlayState();
  restoreFocus = dialogFocus(overlay);
  navigator.vibrate?.(8);
}

// -- the + menu ---------------------------------------------------------------------------------
// As in Messages: the thread blurs and a column of round, colored apps rises from the + button.

function actions(event) {
  if (layer.querySelector(".plus-scrim:not(.leaving)")) return;
  const plus = event?.currentTarget || view.talk.querySelector(".composer .attach");
  const prefill = (text) => () => {
    view.text.value = text;
    keepDraft(text);
    grow();
    sendMode();
    view.text.focus();
    view.text.setSelectionRange(text.length, text.length);
  };
  const items = [
    ["camera", "Caméra", "c-gray", () => view.camera.click()],
    ["photo", "Photos", "c-photos", () => view.photos.click()],
    ["file", "Fichier", "c-blue", () => view.files.click()],
    ["mic", "Vocal", "c-orange", () => recorder.start()],
    ["clock", "Rappel", "c-red", prefill("Rappelle-moi ")],
    ["target", "Mission", "c-green", prefill("Nouvelle mission : ")],
    ["spark", "Recherche", "c-indigo", prefill("Cherche en tâche de fond : ")],
    ["moon", "Penses-y", "c-purple", prefill("Penses-y à tête reposée : ")],
  ];
  const list = h(
    "div",
    { class: "plus-menu", role: "menu", "aria-label": "Joindre" },
    items.map(([name, text, color, run], i) =>
      h(
        "button",
        { type: "button", role: "menuitem", style: `--i:${items.length - 1 - i}`, onclick: () => (close(), run()) },
        h("span", { class: `ico plus-ico ${color}` }, icon(name, 20)),
        text,
      ),
    ),
  );
  const scrim = h("div", { class: "plus-scrim", onclick: (e) => e.target === scrim && close() }, list);
  const at = plus.getBoundingClientRect();
  const top = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--app-top")) || 0;
  list.style.left = `${Math.max(8, at.left)}px`;
  let closed = false;
  let restoreFocus = () => {};
  function close() {
    if (closed) return;
    closed = true;
    scrim.classList.add("leaving");
    setTimeout(() => scrim.remove(), 300);
    unstack(close);
    overlayState();
    restoreFocus();
  }
  stack(close);
  layer.append(scrim);
  list.style.bottom = `${Math.max(8, top + scrim.offsetHeight - at.top + 10)}px`;
  overlayState();
  restoreFocus = dialogFocus(list);
}

// -- the tabs: now, the conversation, his notebook, the settings ------------------------------

const TABS = [
  ["now", "Maintenant", "now"],
  ["talk", "Conversation", "chat"],
  ["notebook", "Carnet", "book"],
  ["settings", "Réglages", "sliders"],
];
const DAY_TITLE = new Intl.DateTimeFormat("fr-FR", { weekday: "long", day: "numeric", month: "long" });

function shell() {
  view.nowWork = h("section", { class: "hero card", "aria-live": "polite" });
  view.tabs = {
    now: page("Maintenant", nowFill, () => DAY_TITLE.format(new Date())),
    talk: view.talk,
    notebook: page("Carnet", notebookFill, () => "Tout ce que je garde à l'œil pour toi"),
    settings: page("Réglages", settingsFill),
  };
  view.tabButtons = {};
  // The chosen tab sits on a lens that slides from one tab to the next.
  view.lens = h("span", { class: "lens", "aria-hidden": "true" });
  const bar = h(
    "nav",
    { class: "tabbar glass", "aria-label": "Onglets" },
    view.lens,
    TABS.map(([key, title, name]) =>
      (view.tabButtons[key] = h(
        "button",
        { type: "button", onclick: () => show(key) },
        icon(name, 24),
        h("span", {}, title),
        key === "now" ? view.badge : null,
      )),
    ),
  );
  const find = h("button", { class: "find glass", type: "button", "aria-label": "Chercher dans ce que je sais", onclick: memoryPanel }, icon("search", 22));
  app.replaceChildren(...Object.values(view.tabs), h("div", { class: "dock" }, bar, find));
}

function show(key) {
  if (!view.tabs[key]) key = "now";
  const from = TABS.findIndex(([k]) => k === state.tab);
  const to = TABS.findIndex(([k]) => k === key);
  const leaving = from >= 0 && from !== to ? view.tabs[state.tab] : null;
  if (leaving) {
    // The new tab comes in from the side of its button while the old one slips away under it.
    const el = view.tabs[key];
    el.hidden = false;
    for (const tab of [el, leaving]) {
      clearTimeout(tab.leaveTimer);
      tab.classList.remove("enter-left", "enter-right", "leave-left", "leave-right");
    }
    void el.offsetWidth;
    el.classList.add(to > from ? "enter-right" : "enter-left");
    leaving.classList.add(to > from ? "leave-left" : "leave-right");
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    leaving.leaveTimer = setTimeout(() => {
      leaving.classList.remove("leave-left", "leave-right");
      if (view.tabs[state.tab] !== leaving) leaving.hidden = true;
    }, still ? 0 : 320);
  }
  state.tab = key;
  try {
    localStorage.setItem("orion.tab", key);
  } catch {
    // private mode: the app opens on Maintenant next time
  }
  for (const [k, el] of Object.entries(view.tabs)) if (el !== leaving) el.hidden = k !== key;
  for (const [k, b] of Object.entries(view.tabButtons)) b.setAttribute("aria-current", k === key ? "page" : "false");
  const selected = view.tabButtons[key];
  selected.classList.remove("selected-pop");
  requestAnimationFrame(() => selected.classList.add("selected-pop"));
  placeLens(!!leaving);
  if (key === "talk") bottom(true);
  else view.tabs[key].refresh();
}

function placeLens(moving = false) {
  const b = view.tabButtons?.[state.tab];
  if (!b || !view.lens) return;
  view.lens.style.width = `${b.offsetWidth}px`;
  view.lens.style.transform = `translateX(${b.offsetLeft}px)`;
  // On its way, the lens stretches like a drop of liquid, then settles.
  if (moving && view.lens.animate && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    view.lens.animate([{ scale: "1 1" }, { scale: "1.18 .86", offset: 0.35 }, { scale: ".97 1.03", offset: 0.7 }, { scale: "1 1" }], { duration: 520, easing: "ease-out" });
  }
}
addEventListener("resize", () => placeLens());

// A tab of its own: a large title that scrolls away, as on iOS, and what fills it.
function page(title, fill, subtitle) {
  const line = h("p", { hidden: !subtitle });
  const body = h("div", { class: "page-body" });
  const pull = title === "Maintenant" ? h("div", { class: "pull-refresh", "aria-hidden": "true" }, spinner()) : null;
  const el = h("section", { class: "tab page", "aria-label": title }, h("div", { class: "page-inline-title", "aria-hidden": "true" }, title), pull, h("header", { class: "page-head" }, line, h("h1", {}, title)), body);
  let scrollFrame = 0;
  el.addEventListener("scroll", () => {
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = 0;
      el.classList.toggle("scrolled", el.scrollTop > 52);
    });
  }, { passive: true });
  let busy = false;
  el.refresh = async () => {
    if (busy) return;
    busy = true;
    line.textContent = subtitle ? subtitle() : "";
    try {
      body.replaceChildren(...[await fill(el.refresh)].flat(Infinity).filter(Boolean));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return welcome("Ta session a expiré.");
      body.replaceChildren(h("p", { class: "empty" }, said(error)));
    } finally {
      busy = false;
    }
  };
  if (pull) {
    let start = null;
    let distance = 0;
    let frame = 0;
    const paint = () => {
      frame = 0;
      pull.style.setProperty("--pull-y", `${distance}px`);
      pull.classList.toggle("pulling", distance > 0);
    };
    el.addEventListener("touchstart", (event) => {
      start = null;
      distance = 0;
      if (el.scrollTop <= 0 && !busy && !event.target.closest("button, input, textarea, a, .slide")) start = { x: event.touches[0].clientX, y: event.touches[0].clientY };
    }, { passive: true });
    el.addEventListener("touchmove", (event) => {
      if (!start) return;
      const dx = event.touches[0].clientX - start.x;
      const dy = event.touches[0].clientY - start.y;
      if (Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) {
        start = null;
        distance = 0;
        paint();
        return;
      }
      if (dy <= 0) {
        distance = 0;
        paint();
        return;
      }
      event.preventDefault();
      distance = Math.min(90, dy * .45);
      if (!frame) frame = requestAnimationFrame(paint);
    }, { passive: false });
    const release = async (event) => {
      if (!start) return;
      start = null;
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      if (distance >= 58 && event.type !== "touchcancel") {
        distance = 52;
        paint();
        pull.classList.add("refreshing");
        await el.refresh();
        pull.classList.remove("refreshing");
      }
      distance = 0;
      paint();
    };
    el.addEventListener("touchend", release);
    el.addEventListener("touchcancel", release);
  }
  return el;
}

// Things changed while he looks at Maintenant: paint it again, a little later, once.
let nowTimer = null;
function nowChanged() {
  clearTimeout(nowTimer);
  nowTimer = setTimeout(() => state.tab === "now" && view.tabs?.now.refresh(), 500);
}

function talkAbout(text) {
  // A change of the server is asked in the conversation: Orion checks it whole, and he signs it.
  clearLayers();
  show("talk");
  submit(text);
}

// -- Maintenant: what I do, what waits on him, what comes ---------------------------------------

function paintNowWork() {
  if (!view.nowWork) return;
  const o = state.overview;
  const work = state.work;
  const waiting = state.approvals.size + (state.questions || 0);
  const orb = h("button", { class: `orb ${work ? "busy" : o?.paused_until ? "still" : ""}`, type: "button", "aria-label": "Conversation", onclick: () => show("talk") }, h("img", { class: "orb-avatar", src: `${BASE}orion-avatar.svg`, alt: "" }));
  let title;
  let line;
  let acts = [];
  if (work) {
    title = h("span", { class: "shimmer" }, "Je m'en occupe…");
    line = `depuis ${elapsed((Date.now() - new Date(work.since)) / 1000)}`;
    acts = [
      h("button", { class: "pill small", type: "button", onclick: () => show("talk") }, "Suivre"),
      h("button", { class: "pill small danger", type: "button", disabled: work.stopping, onclick: () => api("api/stop", {}).catch((e) => toast(said(e))) }, work.stopping ? "J'arrête…" : "Arrêter"),
    ];
  } else if (o?.paused_until) {
    title = "Je suis en pause.";
    line = `jusqu'à ${UNTIL.format(new Date(o.paused_until))} · tes rappels passent quand même`;
    acts = [h("button", { class: "pill small primary", type: "button", onclick: () => api("api/control/pause", { minutes: null }).then(nowChanged, (e) => toast(said(e))) }, "Reprendre")];
  } else if (waiting) {
    title = waiting > 1 ? `J'ai besoin de toi pour ${waiting} choses.` : "J'ai besoin de toi pour une chose.";
    line = o?.quiet_now ? "heures calmes : je ne t'écris pas d'ici là" : o?.next?.length ? `ensuite : ${o.next[0].what}, ${SHORT.format(new Date(o.next[0].at))}` : "";
  } else {
    title = o?.quiet_now ? "Heures calmes. Je veille sans t'écrire." : "Rien en cours. Je garde un œil sur tout.";
    line = o?.next?.length ? `ensuite : ${o.next[0].what}, ${SHORT.format(new Date(o.next[0].at))}` : "écris-moi quand tu veux";
  }
  view.nowWork.classList.toggle("busy", !!work);
  view.nowWork.classList.toggle("still", !work && !!o?.paused_until);
  view.nowWork.replaceChildren(
    h("div", { class: "state-head" }, orb, h("span", {}, h("b", {}, title), line ? h("small", {}, line) : null)),
    ...(acts.length ? [h("div", { class: "row2" }, acts)] : []),
    ...(state.glance?.length ? [h("div", { class: "glance" }, state.glance)] : []),
  );
}

// The day at a glance, under the state: each count leads to its section.
function glanceChip(count, label, tone, target) {
  return h(
    "button",
    { class: `chip ${tone}`, type: "button", onclick: () => target.scrollIntoView({ behavior: "smooth", block: "start" }) },
    h("b", {}, String(count)),
    label,
  );
}

async function nowFill(refresh) {
  await loadApprovals();
  let found = { questions: [], waiting_on: [], tasks: [] };
  try {
    [found, state.overview] = await Promise.all([api("api/for-you"), api("api/control")]);
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 404)) throw error; // controls off: approvals only
  }
  state.questions = found.questions.length;
  paintBadge();
  const act = (path) => async () => {
    try {
      await api(path, {});
      refresh();
    } catch (error) {
      toast(said(error));
    }
  };
  const extras = await nowExtras();
  const sections = [view.nowWork, extras.night];
  const glance = [];
  const { handovers } = await api("api/browser").catch(() => ({ handovers: [] }));
  for (const handover of handovers) {
    sections.push(
      h(
        "article",
        { class: "ask card" },
        h("header", {}, h("span", { class: "ico" }, icon("face", 18, "#fff")), h("span", {}, h("b", {}, "J'ai besoin de tes mains"), h("small", {}, "une page attend : te connecter, un code…"))),
        h("p", {}, handover.why),
        h("button", { class: "pill primary wide", type: "button", onclick: () => browserPanel(handover) }, "Prendre la main"),
      ),
    );
  }
  const { asked } = await api("api/logins/asked").catch(() => ({ asked: [] }));
  for (const offer of asked) sections.push(loginAskCard(offer));
  const approvals = [...state.approvals.values()];
  if (approvals.length || found.questions.length) {
    const head = h("h2", { class: "section" }, "À toi de jouer", h("small", {}, String(approvals.length + found.questions.length)));
    glance.push(glanceChip(approvals.length + found.questions.length, "pour toi", "orange", head));
    sections.push(
      head,
      approvals.map(askCard),
      found.questions.length
        ? h(
            "div",
            { class: "list items" },
            found.questions.map((q) =>
              h(
                "div",
                { class: "item" },
                h("span", { class: "ico wait" }, icon("where", 16)),
                h("span", {}, h("span", { class: "text" }, q.about), h("small", {}, q.auto === "approval" ? "un accord resté sans réponse" : `j'y reviens ${SHORT.format(new Date(q.back_at))}`)),
                h("span", { class: "acts" }, h("button", { class: "pill small", type: "button", onclick: () => askOrion(`À propos de « ${q.about} » : `) }, "Répondre")),
              ),
            ),
          )
        : null,
    );
  }
  if (extras.today.length) {
    glance.push(glanceChip(extras.events, "aujourd'hui", "blue", extras.today[0]));
    sections.push(...extras.today);
  }
  // What I carry on my side, in one list: tasks running, people I wait on, missions.
  const mine = [
    ...found.tasks.map((t) =>
      h(
        "div",
        { class: "item" },
        h("span", { class: "ico on" }, icon("spark", 16)),
        h(
          "span",
          {},
          h("span", { class: "text" }, t.goal),
          h("small", {}, [t.waiting ? "attend mon tour" : t.passes ? plural(t.passes, "passage", "passages") : "démarre", `${money(t.spent)} sur ${money(t.budget)}`].join(" · ")),
          t.budget ? meter(t.spent / t.budget, "") : null,
          t.last ? h("small", { class: "last" }, excerpt(t.last, 140)) : null,
        ),
        h("span", { class: "acts" }, h("button", { class: "pill small ghost", type: "button", onclick: act(`api/tasks/${t.id}/stop`) }, "Arrêter")),
      ),
    ),
    ...found.waiting_on.map((w) =>
      h(
        "div",
        { class: "item" },
        h("span", { class: "ico" }, icon("clock", 16)),
        h("span", {}, h("span", { class: "text" }, `${w.who} · ${w.about}`), h("small", {}, `j'attends sa réponse · ${w.round > 1 ? `relance ${w.round - 1} · ` : ""}j'y reviens ${SHORT.format(new Date(w.back_at))}`)),
        h("span", { class: "acts" }, h("button", { class: "pill small ghost", type: "button", onclick: act(`api/waits/${w.id}/drop`) }, "Laisser")),
      ),
    ),
  ];
  const missions = await api("api/missions")
    .then((found) => found.missions.filter((m) => !m.closed))
    .catch((error) => {
      if (error instanceof ApiError && error.status === 404) return [];
      throw error;
    });
  mine.push(...missions.map((m) => missionRow(m, refresh)));
  if (mine.length) {
    const head = h("h2", { class: "section" }, "De mon côté", h("small", {}, String(mine.length)));
    glance.push(glanceChip(mine.length, "en cours", "gray", head));
    sections.push(head, h("div", { class: "list items" }, mine));
  }
  const next = state.overview?.next || [];
  if (next.length) {
    sections.push(
      h("h2", { class: "section" }, "Bientôt, de moi-même"),
      h(
        "div",
        { class: "list items soon" },
        next.map((p) => h("div", { class: "item" }, h("span", { class: "when" }, SHORT.format(new Date(p.at))), h("span", {}, h("span", { class: "text" }, p.what)))),
      ),
    );
  }
  state.glance = glance.length > 1 ? glance : [];
  paintNowWork();
  return sections;
}

// Today's agenda and, in the morning, the note of the night (D140): each only when its module
// is on, quietly absent otherwise.
async function nowExtras() {
  const off = (error) => {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  };
  const [agenda, nights] = await Promise.all([api("api/agenda").catch(off), api("api/nights").catch(off)]);
  const out = { night: null, today: [], events: 0 };
  const note = nights?.notes?.[0];
  const now = new Date();
  if (note && now.getHours() < 12 && new Date(note.at).toDateString() === now.toDateString()) {
    out.night = (
      h(
        "article",
        { class: "night card" },
        h("header", {}, h("span", { class: "ico" }, icon("moon", 18, "#fff")), h("span", {}, h("b", {}, "Cette nuit"), h("small", {}, "ma note pour ce matin"))),
        h("p", {}, ...rich(excerpt(note.note, 360))),
        h("button", { class: "pill small tinted", type: "button", onclick: nightsPanel }, "Tout voir"),
      )
    );
  }
  const today = (agenda?.events || []).filter((e) => new Date(e.start).toDateString() === now.toDateString() || (e.all_day && new Date(e.start) <= now && new Date(e.end) > now));
  if (today.length) {
    out.events = today.length;
    out.today.push(
      h("h2", { class: "section" }, "Aujourd'hui", h("small", {}, String(today.length))),
      h("div", { class: "list items agenda" }, today.slice(0, 3).map(eventRow), today.length > 3 ? row("calendar", "Tout l'agenda", `+${today.length - 3}`, agendaPanel) : null),
    );
  }
  return out;
}

function eventRow(e) {
  const start = new Date(e.start);
  const end = new Date(e.end);
  return h(
    "div",
    { class: "item event-row" },
    h("span", { class: "when" }, e.all_day ? h("b", {}, "journée") : [h("b", {}, HOUR.format(start)), h("small", {}, HOUR.format(end))]),
    h("span", { class: "bar", "aria-hidden": "true" }),
    h("span", {}, h("span", { class: "text" }, e.title), e.location ? h("small", {}, e.location) : null),
  );
}

function agendaPanel() {
  panel(
    "Ton agenda",
    async () => {
      const { events } = await api("api/agenda");
      if (!events.length) return h("p", { class: "empty" }, "Rien dans les deux semaines qui viennent.");
      const days = new Map();
      for (const e of events) {
        const key = new Date(e.start).toDateString();
        if (!days.has(key)) days.set(key, []);
        days.get(key).push(e);
      }
      return [...days].map(([key, list]) => [
        h("p", { class: "label day-head" }, dayLabel(new Date(key))),
        h("div", { class: "list items agenda" }, list.map(eventRow)),
      ]);
    },
    ["agenda"],
  );
}

const MAIL_WHEN = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "short" });

function mailPanel() {
  panel(
    "Tes mails",
    async () => {
      const { mails } = await api("api/mails");
      if (!mails.length) return h("p", { class: "empty" }, "Rien qui compte ces deux dernières semaines.");
      return h(
        "div",
        { class: "list items mails" },
        mails.map((m) =>
          h(
            "div",
            { class: `item mail-row${m.important ? " important" : ""}` },
            h("span", { class: "initial", "aria-hidden": "true" }, (m.sender || "?").trim().charAt(0).toUpperCase()),
            h(
              "span",
              {},
              h("span", { class: "line" }, h("span", { class: "text" }, m.sender), h("small", { class: "value" }, MAIL_WHEN.format(new Date(m.at)))),
              h("span", { class: "subject" }, m.subject),
              m.summary ? h("small", {}, m.summary) : null,
              m.needs_reply || m.due || m.amount !== null
                ? h(
                    "span",
                    { class: "tags" },
                    m.needs_reply ? h("i", { class: "tag blue" }, "réponse attendue") : null,
                    m.due ? h("i", { class: "tag orange" }, `échéance ${MAIL_WHEN.format(new Date(m.due))}`) : null,
                    m.amount !== null ? h("i", { class: "tag" }, `${String(m.amount).replace(".", ",")} ${m.currency}`) : null,
                  )
                : null,
            ),
          ),
        ),
      );
    },
    ["mail"],
  );
}

function nightsPanel() {
  panel(
    "Mes nuits",
    async () => {
      const { notes, nights, budget_usd: budget } = await api("api/nights");
      if (!notes.length && !nights.length) return h("p", { class: "empty" }, "Pas encore de nuit. Je travaille quand tu dors, une fois par nuit.");
      return [
        nights.length
          ? h(
              "div",
              { class: "list items" },
              nights.slice(0, 7).map((n) =>
                h(
                  "div",
                  { class: "item" },
                  h("span", { class: `ico ${n.failed ? "c-red" : "c-indigo"}` }, icon("moon", 16)),
                  h("span", {}, h("span", { class: "text" }, dayLabel(new Date(n.day))), h("small", {}, n.failed ? "interrompue" : `${plural(n.read || 0, "message relu", "messages relus")}${n.noted ? " · une note" : ""}`)),
                  n.cost_usd !== undefined ? h("small", { class: "value" }, `${money(n.cost_usd)} / ${money(budget)}`) : null,
                ),
              ),
            )
          : null,
        notes.map((n) => h("article", { class: "card night-note" }, h("small", {}, `${dayLabel(new Date(n.at))} · ${HOUR.format(new Date(n.at))}`), h("p", {}, ...rich(n.note)))),
      ];
    },
    ["night"],
  );
}

// -- Carnet: what I know and do, a bento ---------------------------------------------------------

async function notebookFill() {
  const o = await api("api/control").catch((error) => {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  });
  if (!o) return h("p", { class: "empty" }, "Mon carnet n'est pas ouvert ici.");
  const missions = await api("api/missions")
    .then((found) => found.missions.filter((m) => !m.closed))
    .catch((error) => {
      if (error instanceof ApiError && error.status === 404) return null; // missions off
      throw error;
    });
  const kept = missions ? missions.reduce((n, m) => n + m.seen.filter((s) => !s.rejected).length, 0) : 0;
  return [
    h("button", { class: "search glass", type: "button", onclick: memoryPanel }, icon("search", 18), h("span", {}, "Chercher dans ce que je sais")),
    h("p", { class: "label" }, "Ce que je fais"),
    h(
      "div",
      { class: "list items" },
      missions ? row("target", "Missions", missions.length ? `${missions.length} en cours` : "aucune", missionsPanel, "", missions.length ? plural(kept, "trouvaille retenue", "trouvailles retenues") : "") : null,
      row("calendar", "À venir", null, plannedPanel, "", "ce que je ferai seul"),
      row("shield", "Ce que je fais seul", o.alone.length ? `${o.alone.length} permis` : "je demande tout", autonomyPanel),
      row("clock", "Activité", null, activityPanel, "", "ce que j'ai fait, et mes silences"),
    ),
    h("p", { class: "label" }, "Ce que je sais"),
    h(
      "div",
      { class: "list items" },
      row("spark", "Mes souvenirs", String(o.counts.facts), memoryPanel),
      row("people", "Mes proches", null, peoplePanel, "", "anniversaires, nouvelles"),
      row("file", "Mes papiers", null, papersPanel, "", "pièces, fiches, contrats"),
      row("book", "Mes semaines", null, chroniclePanel, "", "la page du dimanche"),
    ),
    h("p", { class: "label" }, "Ce que je suis pour toi"),
    h(
      "div",
      { class: "list items" },
      row("calendar", "Ton agenda", null, agendaPanel, "", "les deux semaines qui viennent"),
      row("mail", "Tes mails", null, mailPanel, "", "ceux qui comptent, résumés"),
      row("moon", "Mes nuits", null, nightsPanel, "", "ce que j'ai fait pendant que tu dormais"),
    ),
    h("p", { class: "label" }, "Ce que tu m'as confié"),
    h(
      "div",
      { class: "list items" },
      row("check", "Engagements", plural(o.counts.commitments, "ouvert", "ouverts"), commitmentsPanel),
      row("rules", "Mon manuel", plural(o.counts.rules, "règle", "règles"), rulesPanel),
    ),
    h("p", { class: "hint" }, "Pour un rappel, dis-le-moi dans la conversation, ou touche + à côté du message."),
  ];
}

// -- Réglages: me, and what replaces the server ---------------------------------------------------

// Each kind of row keeps its colour, as in iOS Settings.
const TINTS = {
  toml: "gray", sliders: "gray", blocks: "purple", server: "blue", phone: "green", bell: "red",
  door: "red", shield: "indigo", check: "green", clock: "orange", calendar: "red", spark: "yellow",
  target: "pink", file: "blue", where: "teal", book: "orange", photo: "pink", face: "indigo",
  people: "teal", rules: "purple", moon: "indigo", wave: "purple", key: "gray",
};

function tint(name) {
  return `c-${TINTS[name] || "gray"}`;
}

function row(name, title, value, onclick, tone = "", sub = "") {
  return h(
    onclick ? "button" : "div",
    { class: "item", type: onclick ? "button" : undefined, onclick },
    h("span", { class: `ico ${tone || tint(name)}` }, icon(name, 17)),
    h("span", {}, h("span", { class: "text" }, title), sub ? h("small", {}, sub) : null),
    value !== null ? h("small", { class: "value" }, value) : null,
    onclick ? h("span", { class: "chev" }, icon("chevron", 14)) : null,
  );
}

async function settingsFill(refresh) {
  const o = await api("api/control").catch((error) => {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  });
  const server = await api("api/server").catch((error) => {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  });
  const vault = await api("logins")
    .then((found) => found.logins)
    .catch((error) => {
      if (error instanceof ApiError && error.status === 404) return null; // no vault here
      throw error;
    });
  const kept = await api("keep").catch((error) => {
    if (error instanceof ApiError && error.status === 404) return null; // one process: no guardian
    throw error;
  });
  const machines = await api("api/machines")
    .then((found) => found.machines)
    .catch((error) => {
      if (error instanceof ApiError && error.status === 404) return null; // hands off
      throw error;
    });
  const act = (path, payload) => async () => {
    try {
      await api(path, payload);
      refresh();
    } catch (error) {
      toast(said(error));
    }
  };
  const parts = [];
  if (o) {
    const paused = !!o.paused_until;
    parts.push(
      h(
        "div",
        { class: `card state ${paused ? "paused" : ""}` },
        h(
          "div",
          { class: "state-head" },
          h("span", { class: "dot" }),
          h(
            "span",
            {},
            h("b", {}, paused ? "Je suis en pause" : o.quiet_now ? "Je suis actif · heures calmes" : "Je suis actif"),
            h("small", {}, paused ? `jusqu'à ${UNTIL.format(new Date(o.paused_until))} · tes rappels passent quand même` : "je prends des initiatives, dans tes limites"),
          ),
        ),
        h(
          "div",
          { class: "row2" },
          paused
            ? h("button", { class: "pill small primary", type: "button", onclick: act("api/control/pause", { minutes: null }) }, "Reprendre")
            : [
                h("button", { class: "pill small", type: "button", onclick: act("api/control/pause", { minutes: 60 }) }, "Pause 1 h"),
                h("button", { class: "pill small", type: "button", onclick: act("api/control/pause", { minutes: minutesUntilTomorrow(8) }) }, "Jusqu'à demain"),
              ],
        ),
      ),
      o.cost
        ? h(
            "div",
            { class: "grid" },
            h("div", { class: "tile" }, h("small", {}, "Aujourd'hui"), h("b", { class: "big" }, money(o.cost.today)), o.cost.daily_cap ? meter(o.cost.today / o.cost.daily_cap, `sur ${money(o.cost.daily_cap)} · ${money(o.cost.alone_today)} de moi-même`) : h("small", {}, `dont ${money(o.cost.alone_today)} de moi-même`)),
            h("div", { class: "tile" }, h("small", {}, "Ce mois-ci"), h("b", { class: "big" }, money(o.cost.month)), o.cost.monthly_cap ? meter(o.cost.month / o.cost.monthly_cap, `sur ${money(o.cost.monthly_cap)}`) : h("small", {}, "sans plafond")),
          )
        : null,
      h("p", { class: "label" }, "Quand je t'écris"),
      h(
        "div",
        { class: "list items" },
        h(
          "div",
          { class: "item" },
          h("span", { class: `ico ${tint("bell")}` }, icon("bell", 17)),
          h("span", {}, h("span", { class: "text" }, "Messages par jour"), h("small", {}, "de moi-même, au plus")),
          h(
            "span",
            { class: "stepper" },
            h("button", { type: "button", "aria-label": "Moins", disabled: o.daily_budget <= 0, onclick: act("api/control/settings", { daily_budget: o.daily_budget - 1 }) }, "−"),
            h("b", {}, String(o.daily_budget)),
            h("button", { type: "button", "aria-label": "Plus", disabled: o.daily_budget >= 50, onclick: act("api/control/settings", { daily_budget: o.daily_budget + 1 }) }, "+"),
          ),
        ),
        row("moon", "Heures calmes", `${o.quiet_start} – ${o.quiet_end}`, () => quietHours(o, refresh), "", "je me tais, sauf urgence"),
        row("calendar", "Points du jour", o.digests?.length ? o.digests.join(" · ") : "aucun", () => digestsSheet(o, refresh), "", "ce qui compte, en un message"),
      ),
    );
  }
  if (server) {
    const on = server.modules.filter((m) => m.on).length;
    const owner = server.settings.filter((s) => s.tier === "owner").length;
    parts.push(
      h("p", { class: "label" }, "Le serveur"),
      h(
        "div",
        { class: "list items" },
        row("toml", "orion.toml", plural(server.settings.length, "réglage", "réglages"), () => configPanel(server)),
        row("blocks", "Modules", `${on} allumés · ${server.catalog.filter((c) => !c.installed).length} à installer`, () => modulesPanel(server)),
        row("server", "Version", server.release?.set_aside ? "une mise à jour écartée" : `orion ${server.version}${server.release ? ` · ${server.release.commit}` : ""}`, () => versionPanel(server), server.release?.set_aside ? "wait" : ""),
        kept?.vault ? row("key", "Secrets", kept.asked.length ? plural(kept.asked.length, "demandé", "demandés") : plural(kept.secrets.length, "clé", "clés"), () => secretsPanel(refresh), kept.asked.length ? "wait" : "", "tes clés d'accès, jamais montrées") : null,
        row("spark", "ChatGPT", "", chatgptPanel, "", "ton forfait pour mes modèles"),
        row("where", "Raccourcis du téléphone", "", phoneTokenPanel, "", "lieux, sommeil, concentration"),
        kept ? row("people", "Autres Orions", kept.federation ? plural(kept.federation.peers.length, "relié", "reliés") : "éteint", () => peersPanel(refresh), "", "caler des rendez-vous entre nous") : null,
        row("wrong", "Mes ratés", "", missesPanel, "", "ce que j'ai relevé le soir"),
        row("book", "Bibliothèque", "", importNotes, "", "ajouter tes notes (.md, .txt)"),
      ),
      h("p", { class: "hint" }, `Tu changes un réglage ici en le signant avec Face ID, sans passer par la conversation. ${plural(owner, "réglage te protège", "réglages te protègent")} : eux ne changent jamais sans ta signature.`),
    );
  }
  if (vault) {
    parts.push(
      h("p", { class: "label" }, "Mes accès"),
      h("div", { class: "list items" }, row("key", "Mots de passe", vault.length ? plural(vault.length, "gardé", "gardés") : "aucun", vaultPanel, "", "je me connecte pour toi, sans les voir")),
    );
  }
  parts.push(
    h("p", { class: "label" }, "Machines et téléphones"),
    h(
      "div",
      { class: "list items" },
      machines ? row("server", "Machines", machines.length ? `${machines.filter((m) => m.online).length} en ligne sur ${machines.length}` : "aucune", machinesPanel) : null,
      row("phone", "Téléphones", state.device || "", phonesPanel),
    ),
    h("p", { class: "label" }, "Cet appareil"),
    h(
      "div",
      { class: "list items" },
      "Notification" in window && state.pushKey ? row("bell", "Notifications", Notification.permission === "granted" ? "activées" : "désactivées", enableNotifications) : null,
      h(
        "button",
        {
          type: "button",
          class: "item danger",
          onclick: async () => {
            await api("logout", {}).catch(() => {});
            source?.close();
            clearLayers();
            welcome();
          },
        },
        h("span", { class: `ico ${tint("door")}` }, icon("door", 17)),
        h("span", {}, h("span", { class: "text" }, "Se déconnecter")),
      ),
    ),
  );
  return parts;
}

function shown(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "oui" : "non";
  if (typeof value === "string") return value || "« »";
  return excerpt(JSON.stringify(value), 60);
}

function configPanel(server) {
  let query = "";
  const list = h("div");
  const search = h("input", { type: "search", placeholder: "Chercher un réglage", "aria-label": "Chercher un réglage", oninput: () => ((query = search.value.trim().toLowerCase()), paint()) });
  function paint() {
    const groups = new Map();
    for (const s of server.settings) {
      if (query && !s.path.toLowerCase().includes(query) && !shown(s.value).toLowerCase().includes(query)) continue;
      const parts = s.path.split(".");
      const group = parts[0] === "modules" ? parts.slice(0, 2).join(".") : parts[0];
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(s);
    }
    list.replaceChildren(
      ...(groups.size
        ? [...groups].map(([group, items]) => [
            h("p", { class: "label" }, group),
            h(
              "div",
              { class: "list items" },
              items.map((s) => row(s.tier === "owner" ? "shield" : "sliders", s.path.slice(group.length + 1) || s.path, shown(s.value), () => changeSheet(s), s.tier === "owner" ? "owner" : "")),
            ),
          ]).flat()
        : [h("p", { class: "empty" }, "Aucun réglage de ce nom.")]),
    );
  }
  panel("orion.toml", () => {
    paint();
    return [
      h("label", { class: "search glass" }, icon("search", 18), search),
      h("p", { class: "hint" }, "Touche un réglage pour le changer : tu signes avec Face ID, mon gardien l'écrit. Le bouclier marque ce qui te protège. Les secrets se changent dans Réglages > Secrets."),
      list,
    ];
  });
}

async function writeSettings(changes) {
  // Signed with Face ID for these exact changes, then written by my guardian: the change does
  // not go through the conversation, so nothing read there can stand in the way (D135).
  const opened = await api("settings/options", { changes });
  const credential = await sign(opened.options);
  const done = await api("settings", { changes, ceremony: opened.ceremony, credential });
  toast(done.changed ? "C'est écrit. Je redémarre quelques secondes pour l'appliquer." : "C'était déjà réglé ainsi.", 3600);
  return done;
}

function changeSheet(setting) {
  const kind = setting.value === null ? "json" : typeof setting.value === "object" ? "json" : typeof setting.value;
  const input =
    kind === "boolean"
      ? h("select", { "aria-label": "Valeur" }, h("option", { value: "true", selected: setting.value === true }, "oui"), h("option", { value: "false", selected: setting.value === false }, "non"))
      : kind === "json"
        ? h("textarea", { rows: 5, "aria-label": "Valeur", value: JSON.stringify(setting.value, null, 1), spellcheck: false })
        : h("input", { type: kind === "number" ? "number" : "text", step: "any", "aria-label": "Valeur", value: String(setting.value), spellcheck: false });
  const status = h("p", { class: "error", role: "alert" });
  function read() {
    if (kind === "boolean") return input.value === "true";
    if (kind === "number") {
      const n = Number(input.value);
      if (input.value.trim() === "" || Number.isNaN(n)) throw new Error("Un nombre, s'il te plaît.");
      return n;
    }
    if (kind === "string") return input.value;
    try {
      return JSON.parse(input.value);
    } catch {
      throw new Error("Ce n'est pas du JSON valide.");
    }
  }
  const close = sheet(
    h("h2", {}, setting.path),
    h("p", { class: "quote" }, `Aujourd'hui : ${shown(setting.value)}`),
    h("label", { class: "field-edit" }, input),
    h("p", { class: "hint" }, setting.tier === "owner" ? "Ce réglage te protège : tu signes ce changement exact avec Face ID, et mon gardien l'écrit après avoir vérifié le fichier entier." : "Tu signes avec Face ID, je vérifie le fichier entier, puis je redémarre pour l'appliquer."),
    status,
    h(
      "button",
      {
        class: "pill primary wide",
        type: "button",
        onclick: async (event) => {
          let value;
          try {
            value = read();
          } catch (error) {
            status.textContent = error.message;
            return;
          }
          if (JSON.stringify(value) === JSON.stringify(setting.value)) return close();
          event.currentTarget.disabled = true;
          try {
            await writeSettings([{ path: setting.path, value }]);
            setting.value = value;
            close();
          } catch (error) {
            status.textContent = said(error);
            event.currentTarget.disabled = false;
          }
        },
      },
      icon("face", 20),
      "Changer avec Face ID",
    ),
  );
}

function modulesPanel(server) {
  const switchPath = (name) => (name === "voice" || name === "delegation" ? `${name}.enabled` : `modules.${name}.enabled`);
  const confirm = (title, text, button, message) => {
    const close = sheet(
      h("h2", {}, title),
      h("p", { class: "quote" }, text),
      h("button", { class: "pill primary wide", type: "button", onclick: () => (close(), talkAbout(message)) }, button),
    );
  };
  const toggle = (m) => {
    const close = sheet(
      h("h2", {}, m.name),
      h("p", { class: "quote" }, m.on ? "Je l'éteins et je redémarre. Tu pourras le rallumer d'ici." : "Je l'allume et je redémarre. S'il lui manque un réglage, je te le dis avant que tu signes."),
      h(
        "button",
        {
          class: "pill primary wide",
          type: "button",
          onclick: async () => {
            try {
              await writeSettings([{ path: switchPath(m.name), value: !m.on }]);
              m.on = !m.on;
              close();
            } catch (error) {
              toast(said(error), 4000);
            }
          },
        },
        icon("face", 20),
        m.on ? "Éteindre avec Face ID" : "Allumer avec Face ID",
      ),
    );
  };
  panel("Modules", () => [
    h("p", { class: "label" }, "Les miens"),
    h(
      "div",
      { class: "list items" },
      server.modules.map((m) =>
        h(
          "button",
          {
            type: "button",
            class: "item",
            onclick: () => toggle(m),
          },
          h("span", { class: `ico ${m.on ? "on" : ""}` }, icon("blocks", 16)),
          h("span", {}, h("span", { class: "text" }, m.name), m.added ? h("small", {}, "installé depuis le catalogue") : null),
          h("small", { class: `value ${m.on ? "on" : ""}` }, m.on ? "allumé" : "éteint"),
          h("span", { class: `switch ${m.on ? "on" : ""}`, "aria-hidden": "true" }),
        ),
      ),
    ),
    server.catalog.length ? h("p", { class: "label" }, "Le catalogue") : null,
    server.catalog.length
      ? h(
          "div",
          { class: "list items" },
          server.catalog.map((c) =>
            h(
              "button",
              {
                type: "button",
                class: "item",
                onclick: () =>
                  c.installed
                    ? confirm(c.name, "Je l'éteins, j'efface son dossier, puis je redémarre.", "Retirer", `Retire le module « ${c.name} ».`)
                    : confirm(c.name, [c.description, c.accesses && `Ce qu'il atteint : ${c.accesses}`, c.settings && `À régler : ${c.settings}`, "Son code tournera avec mes accès, ma mémoire comprise : je te demande ton oui, avec Face ID."].filter(Boolean).join("\n\n"), "Installer", `Installe le module « ${c.name} » du catalogue.`),
              },
              h("span", { class: "ico" }, icon("blocks", 16)),
              h("span", {}, h("span", { class: "text" }, c.name), h("small", {}, c.description)),
              h("small", { class: "value" }, c.installed ? "installé" : "installer"),
            ),
          ),
        )
      : null,
  ]);
}

// -- missions: what each looks for, what it kept, what he turned down ---------------------------

const PACES = [6, 12, 24, 48];

function pace(hours) {
  return hours % 24 === 0 ? (hours === 24 ? "chaque jour" : `tous les ${hours / 24} jours`) : `toutes les ${hours} h`;
}

function missionRow(m, refresh) {
  return h(
    "button",
    { type: "button", class: `item ${m.closed ? "quiet" : ""}`, onclick: () => missionPanel(m.id, refresh) },
    h("span", { class: `ico ${m.closed ? "" : "on"}` }, icon("target", 16)),
    h(
      "span",
      {},
      h("span", { class: "text" }, m.goal),
      h("small", {}, m.closed ? `close · ${m.closed}` : [plural(m.runs, "passage", "passages"), plural(m.seen.length, "vu", "vus"), m.next_run ? `prochain ${SHORT.format(new Date(m.next_run))}` : ""].filter(Boolean).join(" · ")),
    ),
    h("span", { class: "chev" }, icon("chevron", 14)),
  );
}

function missionsPanel() {
  panel("Missions", async (refresh) => {
    const { missions } = await api("api/missions");
    const running = missions.filter((m) => !m.closed);
    const closed = missions.filter((m) => m.closed);
    return [
      running.length ? h("div", { class: "list items" }, running.map((m) => missionRow(m, refresh))) : h("p", { class: "empty" }, "Aucune mission en cours."),
      closed.length ? [h("p", { class: "label" }, "Terminées"), h("div", { class: "list items" }, closed.map((m) => missionRow(m, refresh)))] : null,
      h("button", { class: "pill primary wide", type: "button", onclick: () => askOrion("Nouvelle mission : ") }, icon("plus", 18), "Nouvelle mission"),
      h("p", { class: "hint" }, "Une mission, c'est un but sur plusieurs jours : je cherche seul, à mon rythme, et je ne t'écris que s'il y a du neuf."),
    ];
  });
}

function link(key, label) {
  return /^https?:\/\//.test(key)
    ? h("a", { href: key, target: "_blank", rel: "noopener noreferrer" }, label || key)
    : h("span", {}, label || key);
}

function missionPanel(id, refreshList) {
  panel("Une mission", async (refresh) => {
    const { missions } = await api("api/missions");
    const m = missions.find((each) => each.id === id);
    if (!m) return h("p", { class: "empty" }, "Cette mission n'existe plus.");
    const act = (path, payload = {}, done = "") => async () => {
      try {
        await api(path, payload);
        if (done) toast(done);
        refresh();
        refreshList?.();
      } catch (error) {
        toast(said(error));
      }
    };
    const kept = m.seen.filter((s) => !s.rejected);
    const head = h(
      "div",
      { class: "tile wide mission-head" },
      h("b", { class: "goal" }, m.goal),
      m.criteria ? h("small", {}, m.criteria) : null,
      h(
        "div",
        { class: "stats" },
        h("span", {}, h("b", {}, String(m.runs)), "passages"),
        h("span", {}, h("b", {}, String(kept.length)), "retenus"),
        h("span", {}, h("b", {}, String(m.rejected.length)), "écartés"),
      ),
      m.closed
        ? h("small", {}, `Close : ${m.closed}`)
        : h("small", {}, `Je passe ${pace(m.every_hours)}${m.next_run ? ` · prochain passage ${SHORT.format(new Date(m.next_run))}` : ""}`),
    );
    if (m.closed) return [head, reports(m), seenList(m, kept, null)];
    return [
      head,
      h(
        "div",
        { class: "row2" },
        h("button", { class: "pill primary", type: "button", disabled: !m.rushes_left, onclick: act(`api/missions/${id}/now`, {}, "J'y vais maintenant.") }, icon("spark", 18), "Chercher maintenant"),
      ),
      h("p", { class: "label" }, "Mon rythme"),
      h(
        "div",
        { class: "chips" },
        PACES.map((hours) => h("button", { type: "button", class: "chip", "aria-pressed": String(m.every_hours === hours), onclick: act(`api/missions/${id}/pace`, { every_hours: hours }) }, pace(hours))),
      ),
      reports(m),
      seenList(m, kept, (s) => rejectSheet(m, s, refresh)),
      m.rejected.length
        ? [
            h("p", { class: "label" }, "Écartés, avec ton motif"),
            h("div", { class: "list items" }, m.rejected.map((r) => h("div", { class: "item past" }, h("span", { class: "ico" }, icon("close", 16)), h("span", {}, h("span", { class: "text" }, r.key), h("small", {}, r.why))))),
          ]
        : null,
      m.notes.length ? [h("p", { class: "label" }, "Mes notes"), h("div", { class: "list items" }, m.notes.map((n) => h("div", { class: "item" }, h("span", {}, h("span", { class: "text" }, n)))))] : null,
      h("div", { class: "list items" }, h("button", { type: "button", class: "item danger", onclick: () => endMission(m, refresh, refreshList) }, h("span", { class: "ico" }, icon("stop", 16)), h("span", {}, h("span", { class: "text" }, "Arrêter la mission")))),
    ];
  });
}

function reports(m) {
  if (!m.reports.length) return null;
  const last = m.reports[m.reports.length - 1];
  return [
    h("p", { class: "label" }, `Mon dernier passage · ${SHORT.format(new Date(last.at))}`),
    h("div", { class: "card report" }, h("p", {}, ...rich(last.said)), last.trusted ? null : h("small", {}, "d'après les pages que j'ai lues : vérifie avant d'agir")),
  ];
}

function seenList(m, kept, reject) {
  if (!kept.length) return null;
  return [
    h("p", { class: "label" }, "Ce que j'ai retenu"),
    h(
      "div",
      { class: "list items" },
      kept.slice(-20).reverse().map((s) =>
        h(
          "div",
          { class: "item" },
          h("span", { class: "ico" }, icon("check", 16)),
          h("span", {}, h("span", { class: "text" }, link(s.key, s.label))),
          reject ? h("span", { class: "acts" }, h("button", { class: "pill small ghost", type: "button", onclick: () => reject(s) }, "Écarter")) : null,
        ),
      ),
    ),
  ];
}

function rejectSheet(m, item, refresh) {
  const why = h("input", { type: "text", placeholder: "trop cher, trop loin, déjà vu…", "aria-label": "Pourquoi" });
  const close = sheet(
    h("h2", {}, "Écarter"),
    h("p", { class: "quote" }, item.label || item.key),
    h("label", { class: "field-edit" }, why),
    h("p", { class: "hint" }, "Je garde ton motif : les prochains passages en tiennent compte."),
    h(
      "button",
      {
        class: "pill primary wide",
        type: "button",
        onclick: async () => {
          try {
            await api(`api/missions/${m.id}/reject`, { key: item.key, why: why.value });
            close();
            refresh();
          } catch (error) {
            toast(said(error));
          }
        },
      },
      "Écarter",
    ),
  );
  why.focus();
}

function endMission(m, refresh, refreshList) {
  const close = sheet(
    h("h2", {}, "Arrêter la mission ?"),
    h("p", { class: "quote" }, m.goal),
    h(
      "button",
      {
        class: "pill danger wide",
        type: "button",
        onclick: async () => {
          try {
            await api(`api/missions/${m.id}/close`, {});
            close();
            refresh();
            refreshList?.();
          } catch (error) {
            toast(said(error));
          }
        },
      },
      "Arrêter",
    ),
  );
}

// -- machines and version -----------------------------------------------------------------------

const ACTION_NAMES = { notify: "notifier", open_url: "ouvrir un lien", find_files: "chercher des fichiers", read_file: "lire un fichier", get_file: "m'envoyer un fichier" };

function machinesPanel() {
  panel("Machines", async (refresh) => {
    const { machines } = await api("api/machines");
    const name = h("input", { type: "text", placeholder: "mac, pc-bureau…", "aria-label": "Nom de la machine", autocapitalize: "none", spellcheck: false });
    const pair = async () => {
      const wanted = name.value.trim().toLowerCase();
      if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(wanted)) return toast("Un nom en minuscules, sans espace.");
      try {
        const { code, command } = await signed(`api/machines/${encodeURIComponent(wanted)}/pair`);
        sheet(
          h("h2", {}, `Relier ${wanted}`),
          h("p", { class: "quote big-code" }, code),
          h("p", { class: "hint" }, "Sur la machine, dans un terminal (Orion installé dessus), lance cette commande, puis « orion hand run ». Le code vaut 10 minutes. Ajoute --allow-open pour que j'ouvre des pages, --exit pour que je navigue par sa connexion."),
          copyable(command),
        );
        refresh();
      } catch (error) {
        toast(said(error));
      }
    };
    return [
      machines.length
        ? h(
            "div",
            { class: "list items" },
            machines.map((m) =>
              h(
                "div",
                { class: "item" },
                h("span", { class: `ico ${m.online ? "on" : ""}` }, icon("server", 16)),
                h(
                  "span",
                  {},
                  h("span", { class: "text" }, m.name),
                  h("small", {}, m.online ? `en ligne · ${m.actions.map((a) => ACTION_NAMES[a] || a).join(", ") || "rien de permis"}` : "hors ligne"),
                  m.roots.length ? h("small", {}, `dossiers : ${m.roots.join(", ")}`) : null,
                ),
                h(
                  "span",
                  { class: "acts" },
                  h(
                    "button",
                    {
                      class: "pill small danger",
                      type: "button",
                      onclick: async () => {
                        try {
                          await api(`api/machines/${encodeURIComponent(m.name)}/revoke`, {});
                          toast(`${m.name} est coupée.`);
                          refresh();
                        } catch (error) {
                          toast(said(error));
                        }
                      },
                    },
                    "Couper",
                  ),
                ),
              ),
            ),
          )
        : h("p", { class: "empty" }, "Aucune machine appairée."),
      h("p", { class: "label" }, "Ajouter une machine"),
      h("div", { class: "card add-phone" }, h("label", { class: "field-edit" }, name), h("button", { class: "pill primary wide", type: "button", onclick: pair }, icon("face", 20), "Créer un code")),
      h("p", { class: "hint" }, "Une machine, c'est mon accès à ton ordinateur : ce que tu y permets, dans les dossiers que tu choisis. Ce qu'elle me dit compte comme venant de toi : tu signes son code avec Face ID, une session volée ne peut pas en ajouter."),
    ];
  });
}

function versionPanel(server) {
  const r = server.release;
  panel("Version", () => [
    h(
      "div",
      { class: "list items" },
      row("server", "Orion", server.version, null),
      r ? row("check", "Ce qui tourne", r.commit, null) : null,
      r ? row("clock", "Depuis le", r.at ? SHORT.format(new Date(r.at)) : "—", null) : null,
      row("shield", "Gardien", server.split ? "à part, il protège tes clés" : "dans le même processus", null),
    ),
    r && r.subject ? h("p", { class: "quote report" }, r.subject) : null,
    r && r.set_aside
      ? h("div", { class: "card report warn" }, h("p", {}, `J'ai écarté une mise à jour (${r.set_aside}) : elle ne démarrait pas, je suis revenu à la version d'avant. Je retenterai au prochain changement.`))
      : null,
    h("p", { class: "hint" }, "Je me mets à jour seul : toutes les deux minutes, je regarde si le code a avancé. J'installe, je vérifie que je démarre, et je reviens en arrière si ça casse. Je te préviens dans les deux cas."),
  ]);
}

// -- papers, people, the pages of his weeks ------------------------------------------------------

const LONG_DAY = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "long", year: "numeric" });

function papersPanel() {
  let timer = null;
  const list = h("div", { class: "list items" });
  const search = h("input", {
    type: "search",
    placeholder: "Chercher : passeport, fiche de paie…",
    "aria-label": "Chercher un papier",
    oninput: () => {
      clearTimeout(timer);
      timer = setTimeout(paint, 250);
    },
  });
  async function paint() {
    const { papers } = await api(`api/papers?q=${encodeURIComponent(search.value)}`);
    list.replaceChildren(
      ...(papers.length
        ? papers.map((p) =>
            h(
              "a",
              { class: "item", href: `${BASE}api/papers/${encodeURIComponent(p.id)}/file`, target: "_blank", rel: "noopener" },
              h("span", { class: `ico ${p.expired ? "owner" : p.expiring ? "wait" : ""}` }, icon(p.type.startsWith("image/") ? "photo" : "file", 16)),
              h(
                "span",
                {},
                h("span", { class: "text" }, p.label),
                h("small", { class: p.expired || p.expiring ? "warn" : "" }, [p.holder, p.issued && `du ${LONG_DAY.format(new Date(p.issued))}`, p.valid_until && `${p.expired ? "périmé depuis le" : "valable jusqu'au"} ${LONG_DAY.format(new Date(p.valid_until))}`].filter(Boolean).join(" · ") || p.about),
              ),
              h("span", { class: "chev" }, icon("chevron", 14)),
            ),
          )
        : [h("p", { class: "empty" }, search.value ? "Aucun papier de ce genre." : "Aucun papier rangé. Envoie-moi une photo ou un PDF et dis-moi de le ranger.")]),
    );
  }
  panel("Mes papiers", async () => {
    await paint();
    return [
      h("label", { class: "search glass" }, icon("search", 18), search),
      list,
      h("p", { class: "hint" }, "Je te préviens trois mois puis un mois avant qu'un papier expire. Pour un dossier (location, banque), demande-le-moi : je l'assemble en un PDF."),
    ];
  });
}

function peoplePanel() {
  panel("Mes proches", async (refresh) => {
    const { people } = await api("api/people");
    if (!people.length) return h("p", { class: "empty" }, "Je ne connais encore personne autour de toi.");
    return h(
      "div",
      { class: "people" },
      people.map((p) => {
        const meta = [
          p.birthday && (p.birthday.in_days === 0 ? "anniversaire aujourd'hui" : `anniversaire dans ${plural(p.birthday.in_days, "jour", "jours")}${p.birthday.age ? ` (${p.birthday.age} ans)` : ""}`),
          p.every_days && (p.overdue ? `pas vu depuis plus de ${p.every_days} jours` : p.last_seen ? `vu le ${LONG_DAY.format(new Date(p.last_seen))}` : `tous les ${p.every_days} jours`),
        ].filter(Boolean);
        return h(
          "article",
          { class: `card person ${p.overdue ? "overdue" : ""}` },
          h("header", {}, h("span", { class: "initial" }, p.name.slice(0, 1).toUpperCase()), h("span", {}, h("b", {}, p.name), meta.length ? h("small", {}, meta.join(" · ")) : null)),
          p.facts.length ? h("ul", {}, p.facts.slice(-4).map((f) => h("li", {}, f))) : null,
          p.commitments.length ? h("p", { class: "owe" }, `À faire : ${p.commitments.join(" ; ")}`) : null,
          h(
            "div",
            { class: "acts" },
            h("button", { class: "pill small", type: "button", onclick: () => askOrion(`À propos de ${p.name} : `) }, "En parler"),
            h(
              "button",
              {
                class: "pill small primary",
                type: "button",
                onclick: async () => {
                  try {
                    await api("api/people/saw", { name: p.name });
                    toast(`Noté : tu as vu ${p.name} aujourd'hui.`);
                    refresh();
                  } catch (error) {
                    toast(said(error));
                  }
                },
              },
              "Vu aujourd'hui",
            ),
          ),
        );
      }),
    );
  });
}

function chroniclePanel() {
  panel("Mes semaines", async () => {
    const { pages } = await api("api/chronicle");
    if (!pages.length) return h("p", { class: "empty" }, "Pas encore de page. J'écris celle de ta semaine le dimanche soir.");
    return pages.map((p, i) =>
      h(
        "details",
        { class: "card page-week", open: i === 0 },
        h("summary", {}, p.title),
        h("p", {}, ...rich(p.text)),
      ),
    );
  });
}

// -- the times of his summaries ------------------------------------------------------------------

function digestsSheet(o, refresh) {
  const times = [...(o.digests || [])];
  const rows = h("div", { class: "times-list" });
  function paint() {
    rows.replaceChildren(
      ...times.map((t, i) =>
        h(
          "div",
          { class: "time-row" },
          h("input", { type: "time", value: t, "aria-label": `Point ${i + 1}`, onchange: (e) => (times[i] = e.target.value) }),
          h("button", { class: "round", type: "button", "aria-label": "Retirer", onclick: () => (times.splice(i, 1), paint()) }, icon("close", 18)),
        ),
      ),
      times.length < 6 ? h("button", { class: "pill wide", type: "button", onclick: () => (times.push(times.length ? "18:00" : "09:00"), paint()) }, icon("plus", 18), "Ajouter un point") : null,
    );
  }
  paint();
  const close = sheet(
    h("h2", {}, "Mes points du jour"),
    h("p", { class: "quote" }, "À ces heures, je te dis en un message ce qui compte : ce que j'ai fait, ce qui t'attend, ce qui arrive."),
    rows,
    h(
      "button",
      {
        class: "pill primary wide",
        type: "button",
        onclick: async () => {
          try {
            await api("api/control/settings", { digests: times.filter(Boolean) });
            close();
            refresh();
          } catch (error) {
            toast(said(error));
          }
        },
      },
      "Enregistrer",
    ),
  );
}

// -- Orion's browser, in his hands (D116) ---------------------------------------------------------

function browserPanel(handover) {
  // His hands on Orion's tab, like a browser of his own (D116, D143): the page fills the screen;
  // a tap clicks, a drag scrolls; the bar at the bottom types into the field he touched (a
  // password field gets his phone's passwords); « Terminé » gives the tab back.
  let size = { width: 412, height: 860 };
  let over = false;
  let n = 0;
  let busy = 0;
  let form = null;
  const host = h("span", { class: "bv-host" }, "…");
  const bar = h("div", { class: "bv-progress", hidden: true });
  const picture = h("img", { class: "bv-page", alt: "La page de mon navigateur", draggable: false });
  const stage = h("div", { class: "bv-stage" }, picture);
  const shot = () => {
    if (!over && picture.isConnected) picture.src = `${BASE}api/browser/${handover.token}/shot?n=${n++}`;
  };
  picture.addEventListener("load", () => setTimeout(shot, 900));
  picture.addEventListener("error", () => setTimeout(shot, 2000));

  async function act(body) {
    if (over) return null;
    busy += 1;
    bar.hidden = false;
    try {
      const data = await api(`api/browser/${handover.token}/act`, body);
      if (data.done) {
        over = true;
        toast(data.done);
        close();
        nowChanged();
        if (data.keep) keepSheet(data.keep);
        return data;
      }
      if (data.url) host.textContent = new URL(data.url).host.replace(/^www\./, "");
      if (data.width) size = { width: data.width, height: data.height };
      form = data.form || null;
      signChip.hidden = !form;
      if ("field" in data) fieldFor(data.field);
      return data;
    } catch (error) {
      toast(said(error));
      return null;
    } finally {
      busy -= 1;
      bar.hidden = busy === 0;
      setTimeout(shot, 250);
    }
  }

  // A tap clicks where he touched; a drag scrolls the page by as much.
  let start = null;
  stage.addEventListener("pointerdown", (event) => {
    start = { x: event.clientX, y: event.clientY, dy: 0, dragging: false };
    stage.setPointerCapture(event.pointerId);
  });
  stage.addEventListener("pointermove", (event) => {
    if (!start) return;
    const dy = event.clientY - start.y;
    if (!start.dragging && Math.abs(dy) > 8) start.dragging = true;
    if (start.dragging) {
      start.dy = dy;
      picture.style.transform = `translateY(${Math.max(-120, Math.min(120, dy / 3))}px)`;
    }
  });
  stage.addEventListener("pointerup", (event) => {
    if (!start) return;
    const was = start;
    start = null;
    picture.style.transform = "";
    const box = picture.getBoundingClientRect();
    const scale = size.width / box.width;
    if (was.dragging) {
      act({ do: "scroll", dy: Math.round(-was.dy * scale * 1.6) });
      return;
    }
    const x = (event.clientX - box.left) * scale;
    const y = (event.clientY - box.top) * scale;
    const dot = h("span", { class: "bv-tap" });
    dot.style.left = `${event.clientX - stage.getBoundingClientRect().left}px`;
    dot.style.top = `${event.clientY - stage.getBoundingClientRect().top}px`;
    stage.append(dot);
    setTimeout(() => dot.remove(), 600);
    act({ do: "tap", x, y });
  });
  stage.addEventListener("pointercancel", () => {
    start = null;
    picture.style.transform = "";
  });

  // The bar: what he types goes into the field he touched, as that field asks.
  const field = h("input", { type: "text", class: "bv-input", enterkeyhint: "go", autocapitalize: "none", spellcheck: false, autocomplete: "off", placeholder: "Écrire dans la page", "aria-label": "Texte à taper dans la page" });
  function fieldFor(kind) {
    const password = kind === "password";
    field.type = password ? "password" : kind === "email" ? "email" : "text";
    field.autocomplete = password ? "current-password" : kind === "email" ? "username" : "off";
    field.placeholder = password ? "Mot de passe" : kind ? "Écrire dans ce champ" : "Écrire dans la page";
    dock.classList.toggle("ready", !!kind);
  }
  async function typeIt(enter) {
    const text = field.value;
    if (!text && !enter) return;
    field.value = "";
    if (text) await act({ do: "type", text });
    if (enter) await act({ do: "key", key: "Enter" });
  }
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      typeIt(true);
    } else if (event.key === "Backspace" && !field.value) {
      act({ do: "key", key: "Backspace" });
    }
  });
  const tool = (name, label, onclick) => h("button", { class: "bv-tool", type: "button", "aria-label": label, onclick }, icon(name, 20));
  const dock = h(
    "div",
    { class: "bv-dock glass" },
    tool("back", "Page précédente", () => act({ do: "back" })),
    h("label", { class: "bv-field" }, field, h("button", { class: "bv-send", type: "button", "aria-label": "Taper", onpointerdown: (event) => event.preventDefault(), onclick: () => typeIt(false) }, icon("up", 16))),
    tool("arrow", "Champ suivant", () => act({ do: "key", key: "Tab" })),
  );

  // A sign-in form on the page: his phone's passwords, in one go (D142).
  const signChip = h("button", { class: "bv-chip glass", type: "button", hidden: true, onclick: () => signInSheet() }, icon("key", 16), "Se connecter avec mes identifiants");
  function signInSheet() {
    const user = h("input", { type: "text", name: "username", autocomplete: "username", autocapitalize: "none", spellcheck: false, placeholder: "Identifiant ou e-mail", "aria-label": "Identifiant" });
    const pass = h("input", { type: "password", name: "password", autocomplete: "current-password", placeholder: "Mot de passe", "aria-label": "Mot de passe" });
    const done = sheet(
      h("h2", {}, `Se connecter à ${host.textContent}`),
      h(
        "form",
        {
          class: "login-form",
          onsubmit: (event) => {
            event.preventDefault();
            done();
            act({ do: "signin", username: form?.username ? user.value : "", password: form?.password ? pass.value : "" });
          },
        },
        form?.username ? h("label", { class: "field-edit" }, user) : null,
        form?.password ? h("label", { class: "field-edit" }, pass) : null,
        h("button", { class: "pill primary wide", type: "submit" }, icon("key", 18), "Me connecter"),
      ),
      h("p", { class: "hint" }, "Touche un champ : ton iPhone propose tes mots de passe enregistrés. Je les tape dans la page, puis je l'envoie ; après, je te propose de les garder."),
    );
  }

  const el = h(
    "section",
    { class: "panel browser-view", role: "dialog", "aria-modal": "true", "aria-label": "Prendre la main" },
    h(
      "header",
      { class: "bv-top glass" },
      h("button", { class: "bv-cancel", type: "button", onclick: () => act({ do: "cancel" }) }, "Annuler"),
      h("span", { class: "bv-where" }, icon("shield", 13), host),
      h("button", { class: "bv-done", type: "button", onclick: () => act({ do: "done" }) }, "Terminé"),
      bar,
    ),
    h("p", { class: "bv-why" }, handover.why),
    signChip,
    stage,
    dock,
  );
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    over = true;
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 500);
    unstack(close);
    overlayState();
  }
  stack(close);
  layer.append(el);
  overlayState();
  setTimeout(() => act({ do: "start" }), 50);
}

// -- his vault: the logins Orion signs in with, never shown again (D142) --------------------------

function vaultPanel() {
  let query = "";
  panel("Mots de passe", async (refresh) => {
    const { logins } = await api("logins");
    const list = h("div");
    const search = h("input", { type: "search", value: query, placeholder: "Chercher un site", "aria-label": "Chercher un site", autocapitalize: "none" });
    function paint() {
      const q = query.toLowerCase();
      const found = logins.filter((l) => !q || [l.name, l.username, ...l.sites].join(" ").toLowerCase().includes(q));
      list.replaceChildren(
        found.length
          ? h("div", { class: "list items" }, found.map((l) => row("key", l.sites[0] || l.name, l.totp ? "2FA" : null, () => loginDetails(l, refresh), "", l.username || "sans identifiant")))
          : h("p", { class: "empty" }, logins.length ? "Aucun accès pour ce site." : "Ton coffre est vide. Ajoute un accès : je me connecterai pour toi, sans jamais voir le mot de passe."),
      );
    }
    search.addEventListener("input", () => ((query = search.value.trim()), paint()));
    paint();
    return [
      logins.length > 5 ? h("label", { class: "search glass" }, icon("search", 18), search) : null,
      h("button", { class: "pill primary wide", type: "button", onclick: () => loginSheet({ onSaved: refresh }) }, icon("plus", 20), "Ajouter un accès"),
      h("button", { class: "pill wide", type: "button", onclick: () => importLogins(refresh) }, icon("file", 20), "Importer un export (CSV)"),
      list,
      h("p", { class: "hint" }, "Ton coffre est sur ton serveur. Je tape un mot de passe pour toi, seulement sur son site et dans un champ mot de passe : ni moi ni notre conversation ne le voyons, et cette app ne le réaffiche jamais."),
    ];
  });
}

function importLogins(refresh) {
  // KeePass, Bitwarden, Chrome, Firefox, 1Password: their CSV export, read here and sent to my
  // guardian's vault; the conversation never sees it.
  const input = h("input", { type: "file", accept: ".csv,text/csv", hidden: true });
  input.addEventListener("change", async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      const done = await api("logins/import", { text: await file.text() });
      sheet(
        h("h2", {}, plural(done.kept.length, "accès gardé", "accès gardés")),
        done.kept.length ? h("div", { class: "list items" }, done.kept.map((l) => row("key", l.sites[0] || l.name, null, null, "", l.name))) : null,
        done.skipped.length ? h("p", { class: "label" }, "Laissés de côté") : null,
        ...done.skipped.slice(0, 30).map((why) => h("p", { class: "hint" }, why)),
        h("p", { class: "hint" }, "Efface maintenant le fichier exporté : il contient tes mots de passe en clair."),
      );
      refresh();
    } catch (error) {
      toast(said(error));
    }
  });
  document.body.append(input);
  input.click();
  setTimeout(() => input.remove(), 60_000);
}

function copyable(text) {
  return h(
    "button",
    {
      class: "pill wide",
      type: "button",
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(text);
          toast("Copié.");
        } catch {
          toast("Copie impossible : sélectionne le texte.");
        }
      },
    },
    icon("copy", 18),
    h("code", {}, text),
  );
}

// -- what used to need the server's shell (D154) ----------------------------------------------

function secretsPanel(refreshSettings) {
  const WHERE = { app: "réglé depuis l'app", server: "réglé sur le serveur", missing: "manquant" };
  panel("Secrets", async (refresh) => {
    const kept = await api("keep");
    const asked = new Map(kept.asked.map((a) => [a.name, a.why]));
    const done = () => (refresh(), refreshSettings());
    return [
      kept.asked.length ? h("p", { class: "label" }, "Je te les demande") : null,
      kept.asked.length ? h("div", { class: "list items" }, kept.asked.map((a) => row("key", a.name, "à saisir", () => secretSheet(a.name, a.why, done), "wait", a.why))) : null,
      h("p", { class: "label" }, "Tes clés"),
      h("div", { class: "list items" }, kept.secrets.filter((x) => !asked.has(x.name)).map((x) => row("key", x.name, WHERE[x.where] || x.where, () => secretSheet(x.name, "", done), x.where === "missing" ? "wait" : ""))),
      h("button", { class: "pill wide", type: "button", onclick: () => secretSheet("", "", done) }, icon("plus", 20), "Une autre clé"),
      h("p", { class: "hint" }, "Une clé va droit à mon gardien, signée avec Face ID pour cette valeur exacte : ni la conversation ni le modèle ne la voient, et l'app ne la réaffiche jamais. Elle remplace celle du serveur ; je redémarre pour la prendre."),
    ];
  });
}

function secretSheet(name, why, done) {
  const named = h("input", { type: "text", value: name, placeholder: "MAIL_PERSO_PASSWORD", "aria-label": "Nom de la variable", autocapitalize: "characters", spellcheck: false });
  const value = h("input", { type: "password", placeholder: "La valeur", "aria-label": "Valeur", autocomplete: "off", spellcheck: false });
  const status = h("p", { class: "error", role: "alert" });
  const close = sheet(
    h("h2", {}, name || "Une clé"),
    why ? h("p", { class: "quote" }, why) : null,
    name ? null : h("label", { class: "field-edit" }, named),
    h("label", { class: "field-edit" }, value),
    status,
    h(
      "button",
      {
        class: "pill primary wide",
        type: "button",
        onclick: async (event) => {
          const args = { name: named.value.trim().toUpperCase(), value: value.value };
          if (!/^[A-Z][A-Z0-9_]{2,63}$/.test(args.name)) return (status.textContent = "Un nom en capitales, comme MAIL_PERSO_PASSWORD.");
          if (!args.value.trim()) return (status.textContent = "La valeur est vide.");
          event.currentTarget.disabled = true;
          try {
            const done_ = await signed("keep/secret", { args }, { args });
            value.value = "";
            close();
            toast(done_.where === "core" ? "C'est gardé. Je redémarre pour la prendre." : "C'est gardé par mon gardien. Il redémarre quelques secondes.", 3600);
            done();
          } catch (error) {
            status.textContent = said(error);
            event.currentTarget.disabled = false;
          }
        },
      },
      icon("face", 20),
      "Garder avec Face ID",
    ),
  );
}

function chatgptPanel() {
  panel("ChatGPT", async (refresh) => {
    const c = await api("api/chatgpt");
    const address = h("input", { type: "url", placeholder: "http://127.0.0.1:…/auth/callback?code=…", "aria-label": "Adresse de retour", autocapitalize: "none", spellcheck: false });
    const start = async () => {
      try {
        const { url } = await api("api/chatgpt/start", {});
        window.open(url, "_blank", "noopener");
      } catch (error) {
        toast(said(error));
      }
    };
    const finish = async () => {
      try {
        await api("api/chatgpt/finish", { address: address.value.trim() });
        toast("Connecté.");
        refresh();
      } catch (error) {
        toast(said(error));
      }
    };
    return [
      h(
        "div",
        { class: "list items" },
        row("spark", "Compte", c.signed_in ? c.who : "pas connecté", null, c.signed_in ? "" : "wait"),
        c.signed_in ? row("check", "Usage du forfait", c.plan ? "accordé" : "pas accordé", null, c.plan ? "" : "wait") : null,
        row("sliders", "Modèles chatgpt:…", c.enabled ? "allumés" : "éteints (providers.chatgpt.enabled)", null),
      ),
      c.error ? h("p", { class: "error" }, c.error) : null,
      h("p", { class: "label" }, c.signed_in ? "Se reconnecter" : "Se connecter"),
      h("button", { class: "pill primary wide", type: "button", onclick: start }, "1. Ouvrir la connexion ChatGPT"),
      h("p", { class: "hint" }, "Connecte-toi et accepte. Tu arrives sur une page 127.0.0.1 qui ne s'affiche pas : c'est normal. Copie son adresse (barre d'adresse) et colle-la ici."),
      h("label", { class: "field-edit" }, address),
      h("button", { class: "pill wide", type: "button", onclick: finish }, "2. Terminer"),
      c.signed_in
        ? h(
            "button",
            {
              class: "pill wide danger",
              type: "button",
              onclick: async () => {
                await api("api/chatgpt/logout", {}).catch((error) => toast(said(error)));
                refresh();
              },
            },
            "Se déconnecter",
          )
        : null,
    ];
  });
}

function phoneTokenPanel() {
  panel("Raccourcis du téléphone", () => [
    h("p", { class: "hint" }, "Tes automatisations Raccourcis me disent où tu arrives, comment tu as dormi, quand tu te concentres. Elles s'authentifient avec un jeton : en créer un nouveau coupe l'ancien."),
    h(
      "button",
      {
        class: "pill primary wide",
        type: "button",
        onclick: async () => {
          try {
            const t = await api("api/phone/token", {});
            sheet(
              h("h2", {}, "Nouveau jeton"),
              copyable(t.token),
              h("p", { class: "label" }, "Arrivées et départs"),
              h("p", { class: "hint" }, "Automatisation > Arrivée (ou Départ) > le lieu > « Obtenir le contenu de l'URL », méthode POST, en-tête Authorization : Bearer <jeton>, corps JSON {\"place\": \"boulangerie\", \"event\": \"arrive\"}."),
              copyable(t.event_url),
              h("p", { class: "label" }, "Ce que sait le téléphone"),
              h("p", { class: "hint" }, "Même chose, corps {\"kind\": \"sleep\", \"value\": \"6 h 10, réveillé 2 fois\"}. kind : sleep (au réveil), focus (quand une concentration s'active), battery, workout, health, music, other."),
              copyable(t.sense_url),
            );
          } catch (error) {
            toast(said(error));
          }
        },
      },
      "Créer un jeton",
    ),
  ]);
}

function peersPanel(refreshSettings) {
  panel("Autres Orions", async (refresh) => {
    const kept = await api("keep");
    const f = kept.federation;
    const done = () => (refresh(), refreshSettings());
    if (!f) {
      return [
        h("p", { class: "hint" }, "Relié à l'Orion d'un proche, je cale des rendez-vous avec lui : chacun garde son agenda, nous n'échangeons que des créneaux. Pour commencer, je crée la clé de cet Orion et j'allume la fonction ; je redémarre ensuite."),
        h(
          "button",
          {
            class: "pill primary wide",
            type: "button",
            onclick: async () => {
              try {
                const { fingerprint } = await signed("keep/federation", { args: {} }, { args: {} });
                toast(`C'est allumé. Mon empreinte : ${fingerprint}`, 5000);
                done();
              } catch (error) {
                toast(said(error));
              }
            },
          },
          icon("face", 20),
          "Allumer avec Face ID",
        ),
      ];
    }
    const label = h("input", { type: "text", placeholder: "Paul", "aria-label": "Son prénom" });
    const code = h("textarea", { rows: 3, placeholder: "Le code qu'il t'a donné", "aria-label": "Code d'invitation", spellcheck: false });
    const act = (what, args, says) => async () => {
      try {
        const got = await signed(`keep/${what}`, { args }, { args });
        says(got);
        done();
      } catch (error) {
        toast(said(error));
      }
    };
    return [
      h("div", { class: "list items" }, row("shield", "Mon empreinte", f.fingerprint, null)),
      f.peers.length
        ? h(
            "div",
            { class: "list items" },
            f.peers.map((p) =>
              h(
                "div",
                { class: "item" },
                h("span", { class: "ico" }, icon("people", 16)),
                h("span", {}, h("span", { class: "text" }, p.label), h("small", {}, p.instance)),
                h("span", { class: "acts" }, h("button", { class: "pill small danger", type: "button", onclick: act("unpair", { peer: p.instance }, () => toast(`${p.label} n'est plus relié.`)) }, "Délier")),
              ),
            ),
          )
        : h("p", { class: "empty" }, "Aucun Orion relié."),
      h("p", { class: "label" }, "Inviter ou accepter"),
      h("label", { class: "field-edit" }, label),
      h(
        "button",
        {
          class: "pill primary wide",
          type: "button",
          onclick: () =>
            act("invite", { label: label.value.trim() }, (got) =>
              sheet(h("h2", {}, `Invitation pour ${label.value.trim()}`), copyable(got.code), h("p", { class: "hint" }, `Donne-lui ce code ; il l'accepte dans son app. Valable jusqu'au ${got.expires_at.slice(0, 10)}.`)),
            )(),
        },
        icon("face", 20),
        "Créer une invitation",
      ),
      h("label", { class: "field-edit" }, code),
      h(
        "button",
        {
          class: "pill wide",
          type: "button",
          onclick: () => act("accept", { label: label.value.trim(), code: code.value.trim() }, (got) => toast(`Relié. Son empreinte : ${got.fingerprint} — vérifie-la avec lui.`, 6000))(),
        },
        icon("face", 20),
        "Accepter son code",
      ),
    ];
  });
}

function importNotes() {
  // Notes picked here go whole into my library: I search them when they can help.
  const input = h("input", { type: "file", accept: ".md,.txt,.markdown,text/plain,text/markdown", multiple: true, hidden: true });
  input.addEventListener("change", async () => {
    const files = [...(input.files || [])].slice(0, 200);
    if (!files.length) return;
    try {
      const notes = await Promise.all(files.map(async (f) => ({ name: f.name.replace(/\.(md|txt|markdown)$/i, ""), text: await f.text() })));
      const done = await api("api/library/import", { notes });
      sheet(
        h("h2", {}, plural(done.added.length, "note ajoutée", "notes ajoutées")),
        ...done.skipped.slice(0, 30).map((why) => h("p", { class: "hint" }, why)),
        h("p", { class: "hint" }, "Je les cherche quand elles peuvent t'aider. Ce sont des contenus, jamais des ordres."),
      );
    } catch (error) {
      toast(said(error));
    }
  });
  document.body.append(input);
  input.click();
  setTimeout(() => input.remove(), 60_000);
}

function missesPanel() {
  panel("Mes ratés", async () => {
    const { misses } = await api("api/misses");
    return [
      h("p", { class: "hint" }, "Chaque soir, je relis la journée et tes 👎, et je note ici ce que j'ai raté, avec ce que j'avais répondu. Une règle que j'en tire va dans mon manuel."),
      misses.length
        ? h(
            "div",
            { class: "list items" },
            misses.map((m) =>
              h(
                "div",
                { class: "item" },
                h("span", { class: "ico" }, icon("wrong", 16)),
                h(
                  "span",
                  {},
                  h("small", {}, `${MAIL_WHEN.format(new Date(m.at))} · ${m.why}`),
                  m.asked ? h("span", { class: "text" }, `Toi : ${excerpt(m.asked, 160)}`) : null,
                  h("small", {}, `Moi : ${excerpt(m.answered, 220)}`),
                  m.said ? h("small", {}, `Puis toi : ${excerpt(m.said, 160)}`) : null,
                ),
              ),
            ),
          )
        : h("p", { class: "empty" }, "Aucun raté relevé."),
    ];
  });
}

function loginDetails(login, refresh) {
  let sure = false;
  const remove = h(
    "button",
    {
      class: "pill wide danger",
      type: "button",
      onclick: async () => {
        if (!sure) {
          sure = true;
          remove.textContent = "Retirer vraiment ?";
          return;
        }
        try {
          await api(`logins/${encodeURIComponent(login.name)}/remove`, {});
          toast("Retiré de mon coffre.");
          close();
          refresh();
        } catch (error) {
          toast(said(error));
        }
      },
    },
    "Retirer du coffre",
  );
  const close = sheet(
    h("h2", {}, login.sites[0] || login.name),
    h("p", { class: "quote" }, [`Identifiant : ${login.username || "aucun"}`, `Sites : ${login.sites.join(", ")}`, `Double authentification : ${login.totp ? "oui" : "non"}`, `Nom pour moi : ${login.name}`].join("\n")),
    h(
      "button",
      {
        class: "pill primary wide",
        type: "button",
        onclick: () => {
          close();
          loginSheet({ site: login.sites[0], username: login.username, url: login.url, onSaved: refresh });
        },
      },
      "Changer le mot de passe",
    ),
    remove,
  );
}

function loginSheet({ site = "", username = "", url = "", why = "", token = "", onSaved } = {}) {
  // A real form, with the fields iOS knows: the phone fills them from its own passwords.
  const where = h("input", { type: "text", name: "url", inputmode: "url", value: site, placeholder: "doctolib.fr, ou l'adresse de la page", "aria-label": "Site", autocapitalize: "none", autocomplete: "off", spellcheck: false, readOnly: !!token });
  const user = h("input", { type: "text", name: "username", value: username, placeholder: "Identifiant ou e-mail", "aria-label": "Identifiant", autocomplete: "username", autocapitalize: "none", spellcheck: false });
  const pass = h("input", { type: "password", name: "password", placeholder: "Mot de passe", "aria-label": "Mot de passe", autocomplete: "current-password" });
  const otp = h("input", { type: "text", placeholder: "Clé, ou lien otpauth://", "aria-label": "Clé de double authentification", autocomplete: "off", autocapitalize: "none", spellcheck: false });
  const status = h("p", { class: "error", role: "alert" });
  const send = h("button", { class: "pill primary wide", type: "submit" }, icon("key", 20), "Garder dans mon coffre");
  const form = h(
    "form",
    {
      class: "login-form",
      onsubmit: async (event) => {
        event.preventDefault();
        if (!where.value.trim()) return (status.textContent = "Le site, s'il te plaît.");
        if (!pass.value) return (status.textContent = "Le mot de passe, s'il te plaît.");
        send.disabled = true;
        try {
          const kept = await api("logins", { site: where.value.trim(), username: user.value.trim(), password: pass.value, totp: otp.value.trim(), url });
          pass.value = "";
          otp.value = "";
          if (token) await api(`api/logins/asked/${encodeURIComponent(token)}`, { answer: "saved", name: kept.name });
          toast(`Gardé dans mon coffre, sous « ${kept.name} ».`);
          close();
          onSaved?.();
          nowChanged();
        } catch (error) {
          status.textContent = said(error);
          send.disabled = false;
        }
      },
    },
    h("label", { class: "field-edit" }, where),
    h("label", { class: "field-edit" }, user),
    h("label", { class: "field-edit" }, pass),
    h("details", { class: "more" }, h("summary", {}, "Double authentification (facultatif)"), h("label", { class: "field-edit" }, otp), h("p", { class: "hint" }, "La clé que le site montre quand tu actives la double authentification. Je taperai alors le code moi-même : les deux facteurs seront sur ton serveur.")),
    status,
    send,
  );
  const close = sheet(
    h("h2", {}, token ? `Ton accès ${site}` : site ? `Ton accès ${site}` : "Ajouter un accès"),
    why ? h("p", { class: "quote" }, why) : null,
    form,
    h("p", { class: "hint" }, "Touche Identifiant : ton iPhone te propose tes mots de passe enregistrés, avec Face ID. Ils vont droit dans mon coffre, jamais dans notre conversation."),
  );
}

function answerLogin(offer, answer, done) {
  return async () => {
    try {
      const reply = await api(`api/logins/asked/${encodeURIComponent(offer.token)}`, { answer });
      toast(reply.done);
      done?.();
      nowChanged();
    } catch (error) {
      toast(said(error));
    }
  };
}

function keepSheet(offer) {
  const close = sheet(
    h("h2", {}, "Garder cet accès ?"),
    h("p", { class: "quote" }, `${offer.site}${offer.username ? ` · ${offer.username}` : ""}`),
    h("p", { class: "hint" }, "Le mot de passe que tu viens de taper va dans mon coffre : la prochaine fois, je me connecte seul, sans le voir."),
    h("div", { class: "row2" }, h("button", { class: "pill", type: "button", onclick: answerLogin(offer, "no", () => close()) }, "Non merci"), h("button", { class: "pill primary", type: "button", onclick: answerLogin(offer, "keep", () => close()) }, "Garder")),
  );
}

function loginAskCard(offer) {
  const keep = offer.kind === "keep";
  return h(
    "article",
    { class: "ask card" },
    h("header", {}, h("span", { class: "ico" }, icon("key", 18, "#fff")), h("span", {}, h("b", {}, keep ? "Garder cet accès ?" : `Ton accès ${offer.site}`), h("small", {}, keep ? `${offer.site}${offer.username ? ` · ${offer.username}` : ""}` : "pour que je me connecte à ta place"))),
    offer.why ? h("p", {}, offer.why) : null,
    keep
      ? h("div", { class: "row2" }, h("button", { class: "pill", type: "button", onclick: answerLogin(offer, "no") }, "Non merci"), h("button", { class: "pill primary", type: "button", onclick: answerLogin(offer, "keep") }, "Garder"))
      : h("div", { class: "row2" }, h("button", { class: "pill", type: "button", onclick: answerLogin(offer, "no") }, "Plus tard"), h("button", { class: "pill primary", type: "button", onclick: () => loginSheet({ site: offer.site, url: offer.url, why: offer.why, token: offer.token }) }, "Saisir")),
  );
}

// -- talking: a voice mode over the conversation ----------------------------------------------------

const voice = { on: false, el: null, said: null };

function voiceMode() {
  if (!navigator.mediaDevices || !window.MediaRecorder) return toast("Le micro n'est pas disponible ici.");
  voice.on = true;
  const line = h("p", { class: "voice-line" }, "Touche le rond et parle. Touche encore pour m'envoyer.");
  const orb = h("button", { class: "orb voice-orb", type: "button", "aria-label": "Parler", onclick: () => {
        if (recorder.active) {
          recorder.stop();
          line.textContent = "J'ai entendu, je réfléchis…";
        } else {
          window.speechSynthesis?.cancel();
          recorder.start();
          line.textContent = "Je t'écoute… touche encore pour m'envoyer.";
          orb.classList.add("busy");
        }
      } }, icon("mic", 36, "#fff"));
  voice.line = line;
  voice.orb = orb;
  voice.el = h(
    "section",
    { class: "voice", role: "dialog", "aria-modal": "true", "aria-label": "Me parler" },
    h("button", { class: "round glass voice-close", type: "button", "aria-label": "Fermer", onclick: () => stopVoice() }, icon("close", 20)),
    orb,
    line,
    h("p", { class: "hint" }, "Je te réponds à voix haute. Ce qu'on se dit reste dans la conversation."),
  );
  stack(stopVoice);
  layer.append(voice.el);
}

function stopVoice() {
  voice.on = false;
  if (recorder.active) {
    recorder.cancelled = true;
    recorder.media?.stop();
  }
  window.speechSynthesis?.cancel();
  voice.el?.remove();
  unstack(stopVoice);
}

function speak(message) {
  // In voice mode, Orion's answers are read aloud (his own audio when he sent one).
  if (!voice.on || message.who !== "orion") return;
  voice.orb?.classList.remove("busy");
  const audio = (message.files || []).find((f) => (f.type || "").startsWith("audio/"));
  if (audio) {
    new Audio(fileUrl(audio)).play().catch((error) => console.warn("voice", error));
  } else if (message.text && window.speechSynthesis) {
    const utterance = new SpeechSynthesisUtterance(message.text.replace(/\*+|__|~~|\x60/g, "").replace(/https?:\/\/\S+/g, "le lien"));
    utterance.lang = "fr-FR";
    window.speechSynthesis.speak(utterance);
  }
  if (voice.line) voice.line.textContent = excerpt(message.text || "…", 220);
}

async function signed(path, payload = {}, asked = {}) {
  // A change answers one Face ID signature, drawn for it alone (`asked`: what it is about).
  const opened = await api(`${path}/options`, asked);
  const credential = await sign(opened.options);
  return api(path, { ...payload, ceremony: opened.ceremony, credential });
}

function phonesPanel() {
  panel("Téléphones", async (refresh) => {
    const { devices, this: me } = await api("devices");
    const name = h("input", { type: "text", placeholder: "ipad, telephone-pro…", "aria-label": "Nom du nouveau téléphone", autocapitalize: "none", spellcheck: false });
    return [
      h(
        "div",
        { class: "list items" },
        devices.map((d) =>
          h(
            "div",
            { class: "item" },
            h("span", { class: "ico" }, icon("phone", 16)),
            h("span", {}, h("span", { class: "text" }, d), d === me ? h("small", {}, "celui-ci") : null),
            d === me
              ? null
              : h(
                  "span",
                  { class: "acts" },
                  h(
                    "button",
                    {
                      class: "pill small danger",
                      type: "button",
                      onclick: async () => {
                        try {
                          await signed(`devices/revoke/${encodeURIComponent(d)}`);
                          toast(`${d} ne peut plus entrer.`);
                          refresh();
                        } catch (error) {
                          toast(said(error));
                        }
                      },
                    },
                    "Retirer",
                  ),
                ),
          ),
        ),
      ),
      h("p", { class: "label" }, "Ajouter un téléphone"),
      h(
        "div",
        { class: "card add-phone" },
        h("label", { class: "field-edit" }, name),
        h(
          "button",
          {
            class: "pill primary wide",
            type: "button",
            onclick: async () => {
              const wanted = name.value.trim().toLowerCase();
              if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(wanted)) return toast("Un nom en minuscules, sans espace.");
              try {
                const { code } = await signed(`devices/pair/${encodeURIComponent(wanted)}`);
                const link = `${location.origin}${BASE}?code=${code}`;
                sheet(
                  h("h2", {}, `Appairer ${wanted}`),
                  h("p", { class: "quote big-code" }, code),
                  h("p", { class: "hint" }, "Sur l'autre téléphone, ouvre ce lien dans Safari, puis crée sa clé avec Face ID. Le code vaut 10 minutes."),
                  h("a", { class: "pill wide", href: link }, link.replace(/^https?:\/\//, "")),
                );
                refresh();
              } catch (error) {
                toast(said(error));
              }
            },
          },
          icon("face", 20),
          "Créer un code",
        ),
      ),
      h("p", { class: "hint" }, "Ajouter ou retirer un téléphone se signe avec Face ID. Pour ce téléphone-ci, déconnecte-toi plutôt."),
    ];
  });
}

// -- panels: full screen, over the tabs ----------------------------------------------------------

function panel(title, fill, parts = []) {
  const content = h("div", { class: "panel-content" });
  const body = h("div", { class: "panel-body" }, h("h1", { class: "panel-title" }, title), content);
  const el = h(
    "section",
    { class: "panel", role: "dialog", "aria-modal": "true", "aria-label": title },
    h(
      "header",
      {},
      h("button", { class: "round glass", type: "button", "aria-label": "Retour", onclick: () => close() }, icon("back", 20)),
      h("h1", {}, title),
    ),
    body,
  );
  let closed = false;
  let restoreFocus = () => {};
  function close() {
    if (closed) return;
    closed = true;
    livePanels.delete(live);
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 500);
    unstack(close);
    overlayState();
    restoreFocus();
  }
  stack(close);
  layer.append(el);
  overlayState();
  restoreFocus = dialogFocus(el);
  let scrollFrame = 0;
  el.addEventListener("scroll", () => {
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = 0;
      el.classList.toggle("scrolled", el.scrollTop > 52);
    });
  }, { passive: true });
  const refresh = async () => {
    try {
      const children = await fill(refresh, close);
      if (!closed) content.replaceChildren(...[children].flat(Infinity).filter(Boolean));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return welcome("Ta session a expiré.");
      if (!closed) content.replaceChildren(h("p", { class: "empty" }, said(error)));
    }
  };
  content.replaceChildren(h("div", { class: "skeleton", role: "status", "aria-label": "Chargement" }, Array.from({ length: 3 }, () => h("div", { class: "skeleton-row" }))));
  const live = { parts, refresh };
  if (parts.length) livePanels.add(live);
  refresh();
  return close;
}

const SHORT = new Intl.DateTimeFormat("fr-FR", { weekday: "short", hour: "2-digit", minute: "2-digit" });
const UNTIL = new Intl.DateTimeFormat("fr-FR", { weekday: "long", hour: "2-digit", minute: "2-digit" });

function money(dollars) {
  return `${dollars.toFixed(dollars < 1 ? 2 : 2).replace(".", ",")} $`;
}

function plural(n, one, many) {
  return `${n} ${n > 1 ? many : one}`;
}

function minutesUntilTomorrow(hour) {
  const now = new Date();
  const then = new Date(now);
  then.setDate(now.getDate() + (now.getHours() >= hour ? 1 : 0));
  then.setHours(hour, 0, 0, 0);
  return Math.round((then - now) / 60000);
}

function meter(ratio, caption) {
  const bar = h("span", { class: "fill" });
  bar.style.width = `${Math.max(2, Math.min(100, Math.round(ratio * 100)))}%`;
  if (ratio >= 0.9) bar.classList.add("warn");
  return h("span", { class: "meter" }, h("span", { class: "track" }, bar), h("small", {}, caption));
}

function quietHours(o, refresh) {
  const start = h("input", { type: "time", value: o.quiet_start, "aria-label": "Début" });
  const end = h("input", { type: "time", value: o.quiet_end, "aria-label": "Fin" });
  const close = sheet(
    h("h2", {}, "Heures calmes"),
    h("p", { class: "quote" }, "Entre ces heures, je ne t'écris pas de moi-même, sauf un rappel que tu as demandé. Si tu m'écris, je te réponds."),
    h("div", { class: "times" }, h("label", {}, "De", start), h("label", {}, "à", end)),
    h(
      "button",
      {
        class: "pill dark wide",
        type: "button",
        onclick: async () => {
          try {
            await api("api/control/settings", { quiet_start: start.value, quiet_end: end.value });
            close();
            refresh();
          } catch (error) {
            toast(said(error));
          }
        },
      },
      "Enregistrer",
    ),
  );
}

function askOrion(text) {
  clearLayers();
  show("talk");
  view.text.value = text;
  keepDraft(text);
  grow();
  sendMode();
  view.text.focus();
}

function memoryPanel() {
  let query = "";
  let past = false;
  let timer = null;
  let kind = "all";
  const search = h("input", {
    type: "search",
    placeholder: "Chercher : loyer, ce que Paul aime…",
    "aria-label": "Chercher dans la mémoire",
    oninput: () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        query = search.value;
        refreshList();
      }, 250);
    },
  });
  const body = h("div");
  const KINDS = { all: "Tout", profile: "Toi", preference: "Tes goûts", person: "Proches", project: "Projets", other: "Divers" };
  const TITLES = { profile: "Toi", preference: "Tes goûts", person: "Tes proches", project: "Tes projets", other: "Divers" };
  const kinds = Object.entries(KINDS).map(([key, text]) =>
    h("button", { type: "button", class: "chip", "aria-pressed": String(key === kind), onclick: () => {
      kind = key;
      kinds.forEach((b, i) => b.setAttribute("aria-pressed", String(Object.keys(KINDS)[i] === key)));
      refreshList();
    } }, text),
  );
  const toggle = h("button", { type: "button", class: "chip", "aria-pressed": "false", onclick: () => ((past = !past), toggle.setAttribute("aria-pressed", String(past)), refreshList()) }, "Avec l'historique");
  const day = (iso) => new Date(iso).toLocaleDateString("fr-FR", { day: "numeric", month: "long" });
  const section = (title, facts) => (facts.length ? [h("h2", { class: "section" }, title, h("small", {}, String(facts.length))), h("div", { class: "list items" }, facts.map(row))] : []);
  function row(f) {
    const line = f.current
      ? [f.pinned ? "épinglé" : null, f.stale ? "à vérifier" : null, f.how, f.known ? day(f.known) : null].filter(Boolean).join(" · ")
      : f.why_not;
    return h(
      "button",
      { type: "button", class: `item ${f.current ? "" : "past"}`, onclick: () => factSheet(f, refreshList) },
      h("span", {}, h("span", { class: "text" }, f.statement), h("small", { class: f.trusted && !f.stale ? "" : "warn" }, line)),
    );
  }
  function portraitCard(found) {
    if (!found.portrait) return [];
    const when = found.portrait.day === new Date().toLocaleDateString("sv") ? "réécrit cette nuit" : `écrit le ${day(found.portrait.day)}`;
    return [
      h(
        "article",
        { class: "card night-note portrait" },
        h("small", {}, `Comment je te vois · ${when}`),
        ...found.portrait.text.split(/\n\s*\n/).map((part) => h("p", {}, part)),
        h("button", { type: "button", class: "link", onclick: () => askOrion("Dans ton portrait de moi, ") }, "Ce n'est pas tout à fait ça"),
      ),
    ];
  }
  function situationsCard(found) {
    if (!found.situations.length) return [];
    return [
      h(
        "article",
        { class: "card night-note situations" },
        h("small", {}, "En ce moment"),
        ...found.situations.map((s) => h("p", {}, h("strong", {}, s.title), s.until ? ` · jusqu'au ${day(s.until)}` : "", h("br"), s.news || s.summary)),
        h("button", { type: "button", class: "link", onclick: () => askOrion("Dans ce que tu suis en ce moment, ") }, "Ce n'est plus d'actualité"),
      ),
    ];
  }
  function checkCard(stale) {
    if (!stale.length) return [];
    const answer = (f, still) => async () => {
      try {
        if (still) await api(`api/memory/${f.id}/confirm`, {});
        else return factSheet(f, refreshList, true);
        toast("Noté, c'est toujours vrai.");
        refreshList();
      } catch (error) {
        toast(said(error));
      }
    };
    return [
      h(
        "article",
        { class: "card check" },
        h("small", {}, "À vérifier · ça change souvent, et ça date"),
        ...stale.slice(0, 3).map((f) =>
          h(
            "div",
            { class: "check-item" },
            h("p", {}, h("strong", {}, f.stale), ` tu m'as dit : « ${f.statement} »`),
            h(
              "div",
              { class: "check-actions" },
              h("button", { type: "button", class: "pill tinted", onclick: answer(f, true) }, "Toujours vrai"),
              h("button", { type: "button", class: "pill", onclick: answer(f, false) }, "Ça a changé"),
            ),
          ),
        ),
        stale.length > 3 ? h("small", {}, `et ${plural(stale.length - 3, "autre", "autres")}`) : null,
      ),
    ];
  }
  function byPerson(facts) {
    const people = new Map();
    for (const f of facts) for (const name of f.people.length ? f.people : ["Sans nom"]) people.set(name, [...(people.get(name) || []), f]);
    return [...people.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0])).flatMap(([name, own]) => section(name, own));
  }
  async function refreshList() {
    const found = await api(`api/memory?q=${encodeURIComponent(query)}${past ? "&past=1" : ""}`);
    const facts = found.facts.filter((f) => kind === "all" || f.category === kind);
    let parts;
    if (!facts.length) {
      parts = [h("p", { class: "empty" }, query ? "Rien de tel." : kind === "all" ? "Je ne sais encore rien de toi." : "Rien ici pour l'instant.")];
    } else if (query) {
      parts = [h("div", { class: "list items" }, facts.map(row))];
    } else if (kind === "person") {
      parts = byPerson(facts);
    } else if (kind !== "all") {
      parts = [...(kind === "profile" ? portraitCard(found) : []), h("div", { class: "list items" }, facts.map(row))];
    } else {
      const current = facts.filter((f) => f.current);
      const fresh = current.filter((f) => f.new);
      const pinned = current.filter((f) => f.pinned);
      const rest = current.filter((f) => !f.new && !f.pinned);
      parts = [
        ...portraitCard(found),
        ...situationsCard(found),
        ...checkCard(current.filter((f) => f.stale)),
        ...section("Épinglés", pinned),
        ...section("Nouveau cette semaine", fresh.filter((f) => !f.pinned)),
        ...Object.entries(TITLES).flatMap(([category, title]) => section(title, rest.filter((f) => f.category === category))),
        ...section("Plus vrai", facts.filter((f) => !f.current)),
      ];
    }
    body.replaceChildren(...parts);
  }
  panel("Ce que je sais de toi", async () => {
    await refreshList();
    return [
      h("label", { class: "search glass" }, icon("search", 18), search),
      h("div", { class: "chips" }, kinds, toggle),
      body,
      h("p", { class: "hint" }, "Touche un souvenir pour le modifier, l'épingler ou l'oublier. Rien n'est effacé en silence : l'ancienne version reste dans l'historique."),
    ];
  });
}

function undoToast(text, undo) {
  const el = h(
    "div",
    { class: "toast with-action", role: "status" },
    text,
    h("button", { type: "button", onclick: async () => {
      el.remove();
      await undo();
    } }, "Annuler"),
  );
  layer.append(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  }, 6000);
}

function factSheet(fact, refresh, editing = false) {
  const act = (path, data, done) => async () => {
    try {
      const result = await api(`api/memory/${fact.id}/${path}`, data);
      close();
      await refresh();
      done?.(result);
    } catch (error) {
      toast(said(error));
    }
  };
  const edit = h("textarea", { rows: 3, maxlength: 1000, "aria-label": "Ce qui est vrai maintenant" });
  edit.value = fact.statement;
  const editor = h(
    "div",
    { class: "field-edit memory-edit", hidden: !editing },
    edit,
    h("button", { type: "button", class: "pill dark wide", onclick: () => act("correct", { statement: edit.value }, () => toast("Corrigé. L'ancienne version reste dans l'historique."))() }, "Enregistrer"),
  );
  const versions = h("div");
  const when = fact.known ? `, le ${new Date(fact.known).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" })}` : "";
  const close = sheet(
    h("p", { class: "quote" }, fact.statement),
    h("p", { class: "label" }, `${fact.how.charAt(0).toUpperCase()}${fact.how.slice(1)}${when}.`),
    fact.stale ? h("p", { class: "label warn" }, `Ça change souvent, et c'était ${fact.stale} : c'est peut-être plus vrai.`) : null,
    fact.current ? null : h("p", { class: "label" }, `Plus vrai : ${fact.why_not}.`),
    editor,
    fact.current
      ? h(
          "div",
          { class: "list" },
          h("button", { type: "button", onclick: () => ((editor.hidden = false), edit.focus()) }, icon("wrong", 20), "Modifier"),
          fact.stale ? h("button", { type: "button", onclick: act("confirm", {}, () => toast("Noté, c'est toujours vrai.")) }, icon("check", 20), "C'est toujours vrai") : null,
          h("button", { type: "button", onclick: act("pin", { pinned: !fact.pinned }, () => toast(fact.pinned ? "Désépinglé." : "Épinglé : je l'aurai toujours en tête.")) }, icon("pin", 20), fact.pinned ? "Désépingler" : "Épingler"),
          h("button", { type: "button", onclick: () => askOrion(`D'où tu sais que « ${fact.statement} » ?`) }, icon("where", 20), "D'où ça vient"),
          h(
            "button",
            {
              type: "button",
              class: "danger",
              onclick: act("forget", {}, () =>
                undoToast("Oublié.", async () => {
                  try {
                    await api(`api/memory/${fact.id}/restore`, {});
                    toast("Remis.");
                    refresh();
                  } catch (error) {
                    toast(said(error));
                  }
                }),
              ),
            },
            icon("forget", 20),
            "Oublier",
          ),
        )
      : null,
    versions,
  );
  api(`api/memory/${fact.id}`)
    .then((detail) => {
      const parts = [];
      if (detail.versions.length > 1) {
        parts.push(
          h("h2", { class: "section" }, "Ses versions"),
          h(
            "div",
            { class: "list items" },
            detail.versions.map((v) =>
              h("div", { class: `item ${v.current ? "" : "past"}` }, h("span", {}, h("span", { class: "text" }, v.statement), h("small", {}, [v.how, v.known ? new Date(v.known).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" }) : null].filter(Boolean).join(" · ")))),
            ),
          ),
        );
      }
      if (detail.restorable) parts.push(h("div", { class: "list" }, h("button", { type: "button", onclick: act("restore", {}, () => toast("Remis.")) }, icon("reply", 20), "Le remettre")));
      versions.replaceChildren(...parts);
    })
    .catch((error) => console.warn("memory detail", error));
}

function commitmentsPanel() {
  panel("Engagements", async (refresh) => {
    const { commitments } = await api("api/commitments");
    const close = (id, how) => async () => {
      try {
        await api(`api/commitments/${id}/${how}`, {});
        refresh();
      } catch (error) {
        toast(said(error));
      }
    };
    const who = (c) => (c.debtor === "owner" ? `toi → ${c.creditor}` : `${c.debtor} → ${c.creditor === "owner" ? "toi" : c.creditor}`);
    return commitments.length
      ? h(
          "div",
          { class: "list items" },
          commitments.map((c) =>
            h(
              "div",
              { class: "item" },
              h("span", {}, h("span", { class: "text" }, c.what), h("small", {}, [who(c), c.due].filter(Boolean).join(" · "))),
              h("span", { class: "acts" }, h("button", { class: "pill small", type: "button", onclick: close(c.id, "done") }, "Fait"), h("button", { class: "pill small ghost", type: "button", onclick: close(c.id, "cancel") }, "Annuler")),
            ),
          ),
        )
      : h("p", { class: "empty" }, "Aucun engagement ouvert.");
  });
}

function rulesPanel() {
  panel("Son manuel", async (refresh) => {
    const { rules } = await api("api/rules");
    return [
      h("p", { class: "hint" }, "Ce que j'ai appris de ta façon de travailler. Retire ce qui n'est plus vrai."),
      rules.length
        ? h(
            "div",
            { class: "list items" },
            rules.map((r) =>
              h(
                "div",
                { class: "item" },
                h("span", {}, h("span", { class: "text" }, r.rule), h("small", {}, r.from_owner ? "tu le lui as dit" : "appris en te regardant faire")),
                h(
                  "span",
                  { class: "acts" },
                  h(
                    "button",
                    {
                      class: "pill small ghost",
                      type: "button",
                      onclick: async () => {
                        await api(`api/rules/${r.id}/retire`, {}).catch((e) => toast(said(e)));
                        refresh();
                      },
                    },
                    "Retirer",
                  ),
                ),
              ),
            ),
          )
        : h("p", { class: "empty" }, "Rien encore."),
    ];
  });
}

function autonomyPanel() {
  panel("Ce que je fais seul", async (refresh) => {
    const { actions } = await api("api/autonomy");
    return [
      h("p", { class: "hint" }, "Tu peux me reprendre une autonomie d'un geste. Pour m'en donner, dis-le-moi : je te le demanderai avec Face ID, limites comprises."),
      h(
        "div",
        { class: "list items" },
        actions.map((a) =>
          h(
            "div",
            { class: "item" },
            h("span", {}, h("span", { class: "text" }, a.label), h("small", {}, a.alone ? "seul, dans mes limites" : a.streak ? `je demande · oui ${a.streak} fois de suite` : "je demande")),
            h(
              "span",
              { class: "acts" },
              a.alone
                ? h(
                    "button",
                    {
                      class: "pill small danger",
                      type: "button",
                      onclick: async () => {
                        await api(`api/autonomy/${a.action}/revoke`, {}).catch((e) => toast(said(e)));
                        refresh();
                      },
                    },
                    "Reprendre",
                  )
                : h("button", { class: "pill small ghost", type: "button", onclick: () => askOrion(`Je veux que tu puisses ${a.label} sans me demander. Propose-moi des limites.`) }, "Lui confier"),
            ),
          ),
        ),
      ),
    ];
  });
}

function activityPanel() {
  let filter = "all";
  panel("Ce que j'ai fait", async (refresh) => {
    const { activity } = await api("api/activity");
    const shown = activity.filter((a) => filter === "all" || (filter === "silences" ? a.silence : !a.silence));
    const tab = (key, text) => h("button", { type: "button", role: "tab", "aria-selected": String(filter === key), onclick: () => ((filter = key), refresh()) }, text);
    const groups = [];
    let day = "";
    for (const a of shown) {
      const label = dayLabel(new Date(a.at));
      if (label !== day) {
        day = label;
        groups.push(h("p", { class: "label" }, label), h("div", { class: "list items" }));
      }
      groups[groups.length - 1].append(
        h(
          "div",
          { class: `item ${a.silence ? "quiet" : ""}` },
          h("span", { class: "ico" }, icon(a.silence ? "moon" : a.kind.startsWith("autonomy") ? "shield" : a.kind.startsWith("mail") ? "mail" : a.kind.startsWith("calendar") ? "calendar" : a.kind.startsWith("web") ? "search" : "spark", 16)),
          h("span", {}, h("span", { class: "text" }, a.what), h("small", {}, [TIME.format(new Date(a.at)), a.detail].filter(Boolean).join(" · "))),
        ),
      );
    }
    return [h("div", { class: "tabs", role: "tablist" }, tab("all", "Tout"), tab("actions", "Actions"), tab("silences", "Silences")), groups.length ? groups : h("p", { class: "empty" }, "Rien pour l'instant.")];
  });
}

function plannedPanel() {
  panel("À venir", async () => {
    const { planned } = await api("api/planned");
    return [
      h("p", { class: "hint" }, "Ce que je ferai de moi-même ces deux prochaines semaines. Pour en annuler un, touche-le."),
      planned.length
        ? h(
            "div",
            { class: "list items" },
            planned.map((p) =>
              h(
                "button",
                { type: "button", class: "item", onclick: () => p.undoable && askOrion(`Annule ça : « ${p.what} » (${SHORT.format(new Date(p.at))}).`) },
                h("span", {}, h("span", { class: "text" }, p.what), h("small", {}, SHORT.format(new Date(p.at)))),
              ),
            ),
          )
        : h("p", { class: "empty" }, "Rien de prévu."),
    ];
  });
}

// -- notifications ----------------------------------------------------------------------------

function offerNotifications() {
  if (!state.pushKey || !("Notification" in window) || !("PushManager" in window)) return;
  if (Notification.permission === "granted") return subscribePush().catch((e) => console.warn("push", e));
  if (Notification.permission === "denied") return;
  let dismissed = false;
  try {
    dismissed = localStorage.getItem("orion.notify.later") === "1";
  } catch {
    // ignored
  }
  if (dismissed) return;
  const card = h(
    "article",
    { class: "ask card" },
    h(
      "header",
      {},
      h("span", { class: "ico" }, icon("bell", 18, "#fff")),
      h("span", {}, h("b", {}, "Être prévenu quand l'app est fermée"), h("small", {}, "mes messages et mes demandes d'accord")),
    ),
    h(
      "div",
      { class: "row" },
      h(
        "button",
        {
          class: "pill",
          type: "button",
          onclick: () => {
            card.remove();
            try {
              localStorage.setItem("orion.notify.later", "1");
            } catch {
              // ignored
            }
          },
        },
        "Plus tard",
      ),
      h("button", { class: "pill dark", type: "button", onclick: () => (card.remove(), enableNotifications()) }, "Activer"),
    ),
  );
  view.asks.before(card);
}

async function enableNotifications() {
  if (!("PushManager" in window)) {
    return toast("Ajoute d'abord Orion à l'écran d'accueil : Partager › Sur l'écran d'accueil.", 4000);
  }
  try {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") return toast("Notifications refusées : Réglages › Notifications › Orion.");
    await subscribePush();
    toast("Notifications activées.");
  } catch (error) {
    toast(said(error));
  }
}

async function subscribePush() {
  const registration = await navigator.serviceWorker.ready;
  const subscription =
    (await registration.pushManager.getSubscription()) ||
    (await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: fromB64url(state.pushKey) }));
  const json = subscription.toJSON();
  await api("push/subscribe", { endpoint: json.endpoint, keys: json.keys });
}

// -- photo viewer -----------------------------------------------------------------------------

function viewer(url, name) {
  const el = h("div", { class: "viewer", role: "dialog", "aria-label": name, onclick: () => el.remove() }, h("img", { src: url, alt: name }));
  layer.append(el);
}

start();
