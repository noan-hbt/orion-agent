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
  orion: '<circle cx="12" cy="12" r="6" stroke="#fff" stroke-width="1.7"/><circle cx="18" cy="6" r="1.6" fill="#9DA6FF" stroke="none"/>',
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
  setTimeout(() => el.remove(), ms);
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

function fit() {
  // With the keyboard up, iOS shrinks the visual viewport and may slide it: the app follows it,
  // so the composer sits right on the keyboard and nothing is pushed off the top.
  const vv = window.visualViewport;
  document.documentElement.style.setProperty("--app-h", `${vv ? vv.height : window.innerHeight}px`);
  document.documentElement.style.setProperty("--app-top", `${vv ? Math.max(0, vv.offsetTop) : 0}px`);
  window.scrollTo(0, 0);
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", fit);
  window.visualViewport.addEventListener("scroll", fit);
}
window.addEventListener("resize", fit);
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
      h("p", { class: "lead" }, "Sur le serveur : orion app pair iphone. Tape le code qu'il affiche, il vaut 10 minutes."),
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

const view = {};

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
  view.work = h("article", { class: "work card", hidden: true, "aria-live": "polite", "aria-label": "Orion au travail" });
  view.thread.append(view.older, view.list, view.offline, view.typing, view.work, view.asks);

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
    onfocus: () => document.body.classList.add("typing"),
    onblur: () => document.body.classList.remove("typing"),
    onkeydown: (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && matchMedia("(hover: hover)").matches) {
        event.preventDefault();
        submit();
      }
    },
  });
  view.send = h("button", { class: "send", type: "button", onclick: sendOrRecord });
  view.field = h("div", { class: "field glass" }, view.text, view.send);
  view.files = h("input", { type: "file", multiple: true, hidden: true, onchange: () => pick(view.files) });
  view.camera = h("input", { type: "file", accept: "image/*", capture: "environment", hidden: true, onchange: () => pick(view.camera) });
  view.photos = h("input", { type: "file", accept: "image/*,video/*", multiple: true, hidden: true, onchange: () => pick(view.photos) });

  const top = h(
    "header",
    { class: "top" },
    h(
      "div",
      { class: "who glass" },
      h("span", { class: "avatar" }, icon("orion", 22, "none")),
      h("span", {}, h("h1", {}, "Orion"), view.status),
    ),
    h("button", { class: "round glass", type: "button", "aria-label": "Me parler", onclick: voiceMode }, icon("mic", 20)),
  );

  view.talk = h(
    "section",
    { class: "tab", "aria-label": "Conversation" },
    top,
    view.thread,
    view.attached,
    h(
      "form",
      { class: "composer", onsubmit: (event) => (event.preventDefault(), submit()) },
      h("button", { class: "round glass", type: "button", "aria-label": "Joindre", onclick: actions }, icon("plus", 20)),
      view.field,
    ),
    view.files,
    view.camera,
    view.photos,
  );
  shell();
  grow();
  sendMode();

  view.thread.addEventListener("scroll", () => {
    if (view.thread.scrollTop < 60 && state.more && !view.loadingOlder) older();
  });

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
}

function grow() {
  view.text.style.height = "auto";
  view.text.style.height = `${Math.min(view.text.scrollHeight, 132)}px`;
}

function sendMode() {
  const empty = !view.text.value.trim() && !state.attached.length;
  if (recorder.active) return;
  view.send.className = empty ? "send mic" : "send";
  view.send.replaceChildren(icon(empty ? "mic" : "up", 18));
  view.send.setAttribute("aria-label", empty ? "Message vocal" : "Envoyer");
}

function nearBottom() {
  const t = view.thread;
  return t.scrollHeight - t.scrollTop - t.clientHeight < 140;
}

function bottom(force = false) {
  if (force || nearBottom()) {
    requestAnimationFrame(() => {
      view.thread.scrollTop = view.thread.scrollHeight;
    });
  }
}

// -- messages ---------------------------------------------------------------------------------

const DAY = new Intl.DateTimeFormat("fr-FR", { weekday: "long", day: "numeric", month: "long" });
const TIME = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" });

function dayLabel(date) {
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (date.toDateString() === today.toDateString()) return "Aujourd'hui";
  if (date.toDateString() === yesterday.toDateString()) return "Hier";
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
  view.list.append(...render(message, state.messages[state.messages.length - 2]));
  retail();
  if (live && (wasNear || message.who === "owner")) bottom(true);
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
  // The first message already shown may now share its day with the ones above it.
  const firstDay = view.list.querySelector(".day");
  if (firstDay && messages.length && dayLabel(new Date(messages[messages.length - 1].at)) === firstDay.textContent) {
    firstDay.remove();
  }
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
  // The last message of a run from the same side gets the tail and the time.
  const items = [...view.list.querySelectorAll(".msg")];
  items.forEach((el, i) => {
    const next = items[i + 1];
    const last = !next || next.dataset.who !== el.dataset.who || next.previousElementSibling?.classList.contains("day");
    el.classList.toggle("tail", last);
    const meta = el.querySelector(".meta.time");
    if (meta) meta.hidden = !last;
    const prev = items[i - 1];
    el.classList.toggle("other-side", !!prev && prev.dataset.who !== el.dataset.who);
    if (el.dataset.who === "orion" && items.slice(i + 1).some((later) => later.dataset.who === "owner")) {
      for (const group of el.querySelectorAll(".quick:not(.answered), .checklist:not(.answered)")) answered(group, []);
    }
  });
  view.older.hidden = !state.more;
}

function render(message, previous) {
  const nodes = [];
  const date = new Date(message.at);
  if (!previous || new Date(previous.at).toDateString() !== date.toDateString()) {
    nodes.push(h("p", { class: "day" }, dayLabel(date)));
  }
  const el = h("div", { class: `msg ${message.who}`, "data-who": message.who, "data-ref": message.ref });
  for (const [index, file] of message.files.entries()) el.append(attachment(message, file, index));
  if (message.blocks && message.blocks.length) el.append(...message.blocks.map(block));
  else if (message.text) el.append(h("div", { class: "bubble" }, ...rich(message.text)));
  el.append(h("span", { class: "meta time" }, TIME.format(date)));
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
  return h("div", { class: "bubble" }, ...rich(piece.text));
}

function answered(group, chosen) {
  group.classList.add("answered");
  for (const b of group.querySelectorAll("button, input")) b.disabled = true;
  for (const b of chosen) b.classList.add("chosen");
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
      return h("label", {}, h("span", {}, option), boxes[i]);
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

function showReactions(el, emoji) {
  el.querySelector(".reactions")?.remove();
  if (!emoji.length) return;
  el.classList.add("has-reactions");
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

const LINK = /(https?:\/\/[^\s<>"']+[^\s<>"'.,;:!?)\]])/g;
const BOLD = /\*\*([^*\n]+)\*\*/g;

function rich(text) {
  // Links and **bold**, nothing else: the text is never parsed as HTML.
  const out = [];
  for (const [i, part] of text.split(LINK).entries()) {
    if (i % 2 === 1) {
      out.push(h("a", { href: part, target: "_blank", rel: "noopener noreferrer" }, part.replace(/^https?:\/\//, "")));
      continue;
    }
    for (const [j, piece] of part.split(BOLD).entries()) {
      if (piece) out.push(j % 2 === 1 ? h("b", {}, piece) : piece);
    }
  }
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
  const el = h("div", { class: "msg owner pending tail", "data-who": "owner" });
  for (const f of files) {
    el.append(
      f.preview
        ? h("img", { class: "photo", src: f.preview, alt: f.name })
        : h("span", { class: "file" }, h("span", { class: "tile" }, icon(f.type.startsWith("audio/") ? "mic" : "file", 18)), h("span", {}, h("b", {}, f.name), h("small", {}, size(f.size)))),
    );
  }
  if (text) el.append(h("div", { class: "bubble" }, ...rich(text)));
  const meta = h("span", { class: "meta" }, "envoi…");
  el.append(meta);
  view.list.querySelector(".first")?.remove();
  view.list.append(el);
  const entry = { ref: `local-${Date.now()}`, el };
  state.pending.push(entry);
  bottom(true);
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
      loadApprovals();
      countQuestions();
      nowChanged();
    } else if (data.type === "message") {
      add(data.message);
      speak(data.message);
      if (data.message.who === "orion") setTyping(false, true);
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
  // Orion may keep working after a message (a progress line): the server says when it stops.
  if (keep && state.typing) return;
  state.typing = on;
  view.typing.hidden = !on || !view.work.hidden;
  paintStatus();
  if (on) bottom();
}

// -- Orion at work: what it does as it goes, and a way to stop it (D133) ------------------------

let workClock = null;

function setWork(work) {
  state.work = work;
  clearInterval(workClock);
  const shown = work && (work.steps > 0 || work.doing.length);
  view.work.hidden = !shown;
  view.typing.hidden = !state.typing || !!shown;
  paintNowWork();
  if (!shown) return;
  const since = new Date(work.since);
  const clock = h("span", { class: "clock" });
  const tick = () => {
    const s = Math.max(0, Math.floor((Date.now() - since) / 1000));
    clock.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  tick();
  workClock = setInterval(tick, 1000);
  const stop = h(
    "button",
    {
      type: "button",
      class: "pill small",
      disabled: work.stopping,
      onclick: async () => {
        stop.disabled = true;
        try {
          await api("api/stop", {});
        } catch (error) {
          stop.disabled = false;
          toast(said(error));
        }
      },
    },
    work.stopping ? "J'arrête…" : "Arrêter",
  );
  // A message that arrives during a turn joins it between two steps: Orion reads this one,
  // hands the rest to a background task with everything it knows, and says so.
  const later = h(
    "button",
    {
      type: "button",
      class: "pill small ghost",
      disabled: work.stopping || state.backgrounded === work.since,
      onclick: () => {
        state.backgrounded = work.since;
        later.disabled = true;
        submit("Passe ça en tâche de fond et préviens-moi quand c'est prêt.");
      },
    },
    "En fond",
  );
  const step = (text, current) =>
    h("li", { class: current ? "now" : "" }, h("span", { class: "mark" }, current ? h("i") : icon("check", 13)), text);
  view.work.replaceChildren(
    h(
      "header",
      {},
      h("span", { class: "ico" }, icon("orion", 18, "none")),
      h("span", {}, h("b", {}, "Je travaille"), h("small", {}, clock, ` · ${plural(work.steps, "étape", "étapes")}`, work.cost ? ` · ${money(work.cost)}` : "")),
    ),
    h("ul", {}, (work.done || []).slice(-4).map((t) => step(t, false)), work.doing.map((t) => step(t, true))),
    h("div", { class: "acts" }, later, stop),
  );
  bottom();
}

function paintStatus() {
  const [text, kind] = !state.live ? ["hors ligne", ""] : state.typing ? ["écrit…", "busy"] : ["en ligne", "live"];
  view.status.textContent = text;
  view.status.className = `status ${kind}`;
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
    { class: "ask card", "aria-label": "Orion demande ton accord" },
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

function askTitle(asked) {
  // One line for a list: the action, and what it is about.
  const args = asked.arguments || {};
  const about = [args.subject, args.to, args.title, args.text, args.name].find((v) => typeof v === "string" && v);
  if (!asked.tool) return excerpt(asked.question.replace(/^Avant que je le fasse : /, ""), 90);
  const tool = asked.tool.replace(/_/g, " ");
  return excerpt(about ? `${tool[0].toUpperCase()}${tool.slice(1)} · ${about}` : `${tool[0].toUpperCase()}${tool.slice(1)}`, 90);
}

function details(asked) {
  // The action and its arguments, readable; the raw question when there is nothing to show.
  const entries = Object.entries(asked.arguments || {}).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length));
  if (!asked.tool) return [h("p", {}, asked.question.replace(/^Avant que je le fasse : /, ""))];
  const value = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 1));
  return [
    h("p", { class: "tool" }, asked.tool.replace(/_/g, " ")),
    entries.length && h(
      "dl",
      { class: "args" },
      entries.map(([key, v]) => {
        const text = value(v);
        const dd = h("dd", { class: text.length > 160 ? "long" : "" }, text);
        if (text.length > 160) dd.addEventListener("click", () => dd.classList.toggle("open"));
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
    h("span", { class: "label" }, label),
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
  });
  el.addEventListener("pointerup", () => {
    if (!moved) return;
    if (x >= max() * 0.85) finish();
    else reset();
  });
  el.addEventListener("pointercancel", reset);
  function reset() {
    el.classList.add("back");
    knob.style.transform = "translateX(0)";
    x = 0;
  }
  async function finish() {
    if (busy) return;
    busy = true;
    knob.style.transform = `translateX(${max()}px)`;
    const ok = await done();
    busy = false;
    if (!ok) reset();
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
  if (event.key === "Escape" && open.length) open[open.length - 1]();
});

function sheet(...children) {
  const scrim = h("div", { class: "scrim", onclick: (event) => event.target === scrim && close() });
  const body = h("div", { class: "sheet", role: "dialog", "aria-modal": "true" }, h("span", { class: "grab" }), ...children);
  scrim.append(body);
  function close() {
    scrim.remove();
    unstack(close);
  }
  stack(close);
  // Swipe down to close.
  let startY = null;
  body.addEventListener("touchstart", (event) => {
    if (body.scrollTop <= 0) startY = event.touches[0].clientY;
  }, { passive: true });
  body.addEventListener("touchmove", (event) => {
    if (startY === null) return;
    const dy = event.touches[0].clientY - startY;
    if (dy > 0) body.style.transform = `translateY(${dy}px)`;
  }, { passive: true });
  body.addEventListener("touchend", (event) => {
    if (startY === null) return;
    const dy = event.changedTouches[0].clientY - startY;
    startY = null;
    if (dy > 90) close();
    else body.style.transform = "";
  });
  layer.append(scrim);
  body.querySelector("button")?.focus({ preventScroll: true });
  return close;
}

function excerpt(text, max = 160) {
  const plain = text.replace(/\s+/g, " ").trim();
  return plain.length > max ? `${plain.slice(0, max - 1)}…` : plain;
}

function messageMenu(el, message) {
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
        { class: "emoji", role: "group", "aria-label": "Réagir" },
        ["👍", "❤️", "😂", "👎", "❓"].map((emoji) =>
          h(
            "button",
            {
              type: "button",
              "aria-label": emoji,
              "aria-pressed": String((message.reactions || []).includes(emoji)),
              onclick: async () => {
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
          ),
        ),
      )
    : null;
  const close = sheet(
    message.text ? h("p", { class: "quote" }, ...rich(excerpt(message.text, 400))) : null,
    reactions,
    h(
      "div",
      { class: "list" },
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
    ),
  );
}

// -- the + sheet ------------------------------------------------------------------------------

function actions() {
  const tile = (name, text, onclick, accent = false) =>
    h("button", { type: "button", class: accent ? "accent" : "", onclick: () => (close(), onclick()) }, icon(name, 22), text);
  const prefill = (text) => () => {
    view.text.value = text;
    keepDraft(text);
    grow();
    sendMode();
    view.text.focus();
    view.text.setSelectionRange(text.length, text.length);
  };
  const close = sheet(
    h(
      "div",
      { class: "tiles" },
      tile("photo", "Photos", () => view.photos.click(), true),
      tile("camera", "Caméra", () => view.camera.click()),
      tile("file", "Fichier", () => view.files.click()),
      tile("mic", "Vocal", () => recorder.start()),
      tile("clock", "Rappel", prefill("Rappelle-moi ")),
      tile("target", "Mission", prefill("Nouvelle mission : ")),
      tile("spark", "Recherche", prefill("Cherche en tâche de fond : ")),
      tile("moon", "Penses-y", prefill("Penses-y à tête reposée : ")),
    ),
  );
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
  view.nowWork = h("section", { class: "hero", "aria-live": "polite" });
  view.tabs = {
    now: page("Maintenant", nowFill, () => DAY_TITLE.format(new Date())),
    talk: view.talk,
    notebook: page("Carnet", notebookFill, () => "Tout ce que je garde à l'œil pour toi"),
    settings: page("Réglages", settingsFill),
  };
  view.tabButtons = {};
  const bar = h(
    "nav",
    { class: "tabbar glass", "aria-label": "Onglets" },
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
  state.tab = key;
  try {
    localStorage.setItem("orion.tab", key);
  } catch {
    // private mode: the app opens on Maintenant next time
  }
  for (const [k, el] of Object.entries(view.tabs)) el.hidden = k !== key;
  for (const [k, b] of Object.entries(view.tabButtons)) b.setAttribute("aria-current", k === key ? "page" : "false");
  if (key === "talk") bottom(true);
  else view.tabs[key].refresh();
}

// A tab of its own: a large title that scrolls away, as on iOS, and what fills it.
function page(title, fill, subtitle) {
  const line = h("p");
  const body = h("div", { class: "page-body" });
  const el = h("section", { class: "tab page", "aria-label": title }, h("header", { class: "page-head" }, line, h("h1", {}, title)), body);
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
  layer.replaceChildren();
  open.length = 0;
  show("talk");
  submit(text);
}

// -- Maintenant: what I do, what waits on him, what comes ---------------------------------------

function paintNowWork() {
  if (!view.nowWork) return;
  const o = state.overview;
  const work = state.work && (state.work.steps > 0 || state.work.doing.length) ? state.work : null;
  const waiting = state.approvals.size + (state.questions || 0);
  const orb = h("button", { class: `orb ${work ? "busy" : o?.paused_until ? "still" : ""}`, type: "button", "aria-label": "Conversation", onclick: () => show("talk") }, icon("orion", 36, "none"));
  let title;
  let line;
  let acts = [];
  if (work) {
    title = work.doing[0] || "Je travaille.";
    line = `${plural(work.steps, "étape", "étapes")}${work.cost ? ` · ${money(work.cost)}` : ""}`;
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
    line = o?.quiet_now ? "heures calmes : je ne t'écris pas d'ici là" : "elles sont juste en dessous";
  } else {
    title = o?.quiet_now ? "Heures calmes. Je veille sans t'écrire." : "Rien en cours. Je garde un œil sur tout.";
    line = o?.next?.length ? `ensuite : ${o.next[0].what}, ${SHORT.format(new Date(o.next[0].at))}` : "écris-moi quand tu veux";
  }
  view.nowWork.replaceChildren(orb, h("h2", {}, title), h("div", { class: "line" }, h("span", {}, line), acts));
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
  paintNowWork();
  const act = (path) => async () => {
    try {
      await api(path, {});
      refresh();
    } catch (error) {
      toast(said(error));
    }
  };
  const sections = [view.nowWork];
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
  const approvals = [...state.approvals.values()];
  if (approvals.length || found.questions.length) {
    sections.push(
      h("h2", { class: "section" }, "À toi de jouer", h("small", {}, String(approvals.length + found.questions.length))),
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
  if (found.tasks.length) {
    sections.push(
      h("h2", { class: "section" }, "En tâche de fond"),
      h(
        "div",
        { class: "list items" },
        found.tasks.map((t) =>
          h(
            "div",
            { class: "item" },
            h("span", { class: "ico" }, icon("spark", 16)),
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
      ),
    );
  }
  if (found.waiting_on.length) {
    sections.push(
      h("h2", { class: "section" }, "J'attends des autres"),
      h(
        "div",
        { class: "list items" },
        found.waiting_on.map((w) =>
          h(
            "div",
            { class: "item" },
            h("span", { class: "ico" }, icon("clock", 16)),
            h("span", {}, h("span", { class: "text" }, `${w.who} · ${w.about}`), h("small", {}, `${w.round > 1 ? `relance ${w.round - 1} · ` : ""}j'y reviens ${SHORT.format(new Date(w.back_at))}`)),
            h("span", { class: "acts" }, h("button", { class: "pill small ghost", type: "button", onclick: act(`api/waits/${w.id}/drop`) }, "Laisser")),
          ),
        ),
      ),
    );
  }
  const missions = await api("api/missions")
    .then((found) => found.missions.filter((m) => !m.closed))
    .catch((error) => {
      if (error instanceof ApiError && error.status === 404) return [];
      throw error;
    });
  if (missions.length) {
    sections.push(
      h("h2", { class: "section" }, "Mes missions", h("small", {}, String(missions.length))),
      h("div", { class: "list items" }, missions.map((m) => missionRow(m, refresh))),
    );
  }
  const next = state.overview?.next || [];
  if (next.length) {
    sections.push(
      h("h2", { class: "section" }, "Bientôt, de moi-même"),
      h(
        "div",
        { class: "list items" },
        next.map((p) => h("div", { class: "item" }, h("span", { class: "ico" }, icon("calendar", 16)), h("span", {}, h("span", { class: "text" }, p.what), h("small", {}, SHORT.format(new Date(p.at)))))),
      ),
    );
  }
  return sections;
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
  const go = (name, title, count, open, wide = false) =>
    h("button", { class: `tile link ${wide ? "wide" : ""}`, type: "button", onclick: open }, h("span", { class: `ico ${tint(name)}` }, icon(name, 18)), h("b", {}, title), count !== null ? h("small", {}, count) : null);
  return [
    h(
      "div",
      { class: "grid" },
      missions ? go("target", "Missions", missions.length ? `${missions.length} en cours · ${plural(missions.reduce((n, m) => n + m.seen.filter((s) => !s.rejected).length, 0), "retenu", "retenus")}` : "aucune en cours", missionsPanel, true) : null,
      go("spark", "Ce que je sais", plural(o.counts.facts, "souvenir", "souvenirs"), memoryPanel, true),
      go("file", "Mes papiers", "pièces, fiches, contrats", papersPanel),
      go("where", "Mes proches", "anniversaires, nouvelles", peoplePanel),
      go("book", "Mes semaines", "la page du dimanche", chroniclePanel, true),
      go("check", "Engagements", plural(o.counts.commitments, "ouvert", "ouverts"), commitmentsPanel),
      go("file", "Mon manuel", plural(o.counts.rules, "règle", "règles"), rulesPanel),
      go("calendar", "À venir", "ce que je ferai seul", plannedPanel),
      go("clock", "Activité", "et mes silences", activityPanel),
      go("shield", "Ce que je fais seul", o.alone.length ? `${o.alone.length} permis` : "je demande tout", autonomyPanel, true),
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
};

function tint(name) {
  return `c-${TINTS[name] || "gray"}`;
}

function row(name, title, value, onclick, tone = "") {
  return h(
    onclick ? "button" : "div",
    { class: "item", type: onclick ? "button" : undefined, onclick },
    h("span", { class: `ico ${tone || tint(name)}` }, icon(name, 17)),
    h("span", {}, h("span", { class: "text" }, title)),
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
        { class: "grid" },
        h(
          "div",
          { class: `tile hero ${paused ? "paused" : ""}` },
          h("span", { class: "dot" }),
          h(
            "div",
            {},
            h("b", {}, paused ? "Je suis en pause" : o.quiet_now ? "Je suis actif · heures calmes" : "Je suis actif"),
            h("small", {}, paused ? `jusqu'à ${UNTIL.format(new Date(o.paused_until))} · tes rappels passent quand même` : "je prends des initiatives, dans les limites ci-dessous"),
          ),
          h(
            "div",
            { class: "row" },
            paused
              ? h("button", { class: "pill small primary", type: "button", onclick: act("api/control/pause", { minutes: null }) }, "Reprendre")
              : [
                  h("button", { class: "pill small", type: "button", onclick: act("api/control/pause", { minutes: 60 }) }, "Pause 1 h"),
                  h("button", { class: "pill small", type: "button", onclick: act("api/control/pause", { minutes: minutesUntilTomorrow(8) }) }, "Jusqu'à demain"),
                ],
          ),
        ),
        o.cost
          ? [
              h("div", { class: "tile" }, h("small", {}, "Aujourd'hui"), h("b", { class: "big" }, money(o.cost.today)), o.cost.daily_cap ? meter(o.cost.today / o.cost.daily_cap, `sur ${money(o.cost.daily_cap)} · ${money(o.cost.alone_today)} de moi-même`) : h("small", {}, `dont ${money(o.cost.alone_today)} de moi-même`)),
              h("div", { class: "tile" }, h("small", {}, "Ce mois-ci"), h("b", { class: "big" }, money(o.cost.month)), o.cost.monthly_cap ? meter(o.cost.month / o.cost.monthly_cap, `sur ${money(o.cost.monthly_cap)}`) : h("small", {}, "sans plafond")),
            ]
          : null,
        h(
          "div",
          { class: "tile" },
          h("small", {}, "Messages de moi-même / jour"),
          h(
            "div",
            { class: "stepper" },
            h("button", { type: "button", "aria-label": "Moins", disabled: o.daily_budget <= 0, onclick: act("api/control/settings", { daily_budget: o.daily_budget - 1 }) }, "−"),
            h("b", { class: "big" }, String(o.daily_budget)),
            h("button", { type: "button", "aria-label": "Plus", disabled: o.daily_budget >= 50, onclick: act("api/control/settings", { daily_budget: o.daily_budget + 1 }) }, "+"),
          ),
        ),
        h("button", { class: "tile", type: "button", onclick: () => quietHours(o, refresh) }, h("small", {}, "Heures calmes"), h("b", { class: "big" }, `${o.quiet_start}–${o.quiet_end}`), h("small", {}, "je me tais, sauf urgence")),
        h("button", { class: "tile wide", type: "button", onclick: () => digestsSheet(o, refresh) }, h("small", {}, "Mes points du jour"), h("b", { class: "big" }, o.digests?.length ? o.digests.join(" · ") : "aucun"), h("small", {}, "ce qui compte, en un message")),
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
      ),
      h("p", { class: "hint" }, `Tu changes un réglage ici en le signant avec Face ID, sans passer par la conversation. ${plural(owner, "réglage te protège", "réglages te protègent")} : eux ne changent jamais sans ta signature.`),
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
            layer.replaceChildren();
            open.length = 0;
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
      h("p", { class: "hint" }, "Touche un réglage pour le changer : tu signes avec Face ID, mon gardien l'écrit. Le bouclier marque ce qui te protège. Les secrets et les chemins ne se changent que sur le serveur."),
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
      h("p", { class: "hint" }, "Une machine, c'est mon accès à ton ordinateur : ce que tu y permets, dans les dossiers que tu choisis. En ajouter une se fait sur le serveur (orion hands pair <nom>), puis orion hand pair sur la machine : ce qu'elle me dit compte comme venant de toi, alors une session volée ne doit pas pouvoir en ajouter."),
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
  let size = { width: 412, height: 860 };
  let over = false;
  let n = 0;
  const status = h("p", { class: "hint" }, "Ouverture…");
  const picture = h("img", { class: "live", alt: "L'onglet de mon navigateur" });
  const shot = () => {
    if (!over && picture.isConnected) picture.src = `${BASE}api/browser/${handover.token}/shot?n=${n++}`;
  };
  let busy = false;
  async function act(body) {
    if (busy || over) return;
    busy = true;
    try {
      const data = await api(`api/browser/${handover.token}/act`, body);
      if (data.done) {
        over = true;
        toast(data.done);
        close();
        nowChanged();
      } else {
        status.textContent = data.url || "";
        if (data.width) size = { width: data.width, height: data.height };
      }
    } catch (error) {
      status.textContent = said(error);
    } finally {
      busy = false;
      setTimeout(shot, 250);
    }
  }
  picture.addEventListener("click", (event) => {
    const box = picture.getBoundingClientRect();
    act({ do: "tap", x: ((event.clientX - box.left) / box.width) * size.width, y: ((event.clientY - box.top) / box.height) * size.height });
  });
  picture.addEventListener("load", () => setTimeout(shot, 900));
  picture.addEventListener("error", () => setTimeout(shot, 2000));
  const field = h("input", { type: "password", autocomplete: "off", placeholder: "Texte à taper", "aria-label": "Texte à taper" });
  const typeIt = () => {
    if (!field.value) return;
    act({ do: "type", text: field.value });
    field.value = "";
  };
  field.addEventListener("keydown", (event) => event.key === "Enter" && (event.preventDefault(), typeIt()));
  const key = (label, aria, body) => h("button", { class: "pill small", type: "button", "aria-label": aria, onclick: () => act(body) }, label);
  const close = panel("Prendre la main", () => [
    h("p", { class: "quote report" }, handover.why),
    picture,
    status,
    h("div", { class: "type-row" }, h("label", { class: "field-edit" }, field), h("button", { class: "round", type: "button", "aria-label": field.type === "password" ? "Voir" : "Cacher", onclick: () => (field.type = field.type === "password" ? "text" : "password") }, icon("where", 18)), h("button", { class: "pill small primary", type: "button", onclick: typeIt }, "Taper")),
    h("div", { class: "keys" }, key("⏎", "Entrée", { do: "key", key: "Enter" }), key("⌫", "Effacer", { do: "key", key: "Backspace" }), key("▲", "Monter", { do: "scroll", dy: -500 }), key("▼", "Descendre", { do: "scroll", dy: 500 }), key("←", "Page précédente", { do: "back" })),
    h("div", { class: "row2" }, h("button", { class: "pill", type: "button", onclick: () => act({ do: "cancel" }) }, "Annuler"), h("button", { class: "pill primary", type: "button", onclick: () => act({ do: "done" }) }, "J'ai fini")),
    h("p", { class: "hint" }, "Touche l'image pour cliquer, puis tape dessous. C'est mon navigateur, sur ton serveur : ce que tu tapes va dans la page, jamais dans notre conversation."),
  ]);
  setTimeout(() => act({ do: "start" }), 50);
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
    const utterance = new SpeechSynthesisUtterance(message.text.replace(/\*\*/g, "").replace(/https?:\/\/\S+/g, "le lien"));
    utterance.lang = "fr-FR";
    window.speechSynthesis.speak(utterance);
  }
  if (voice.line) voice.line.textContent = excerpt(message.text || "…", 220);
}

async function signed(path, payload = {}) {
  // A change of the phones answers one Face ID signature, drawn for it alone.
  const opened = await api(`${path}/options`, {});
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

function panel(title, fill) {
  const body = h("div", { class: "panel-body" });
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
  function close() {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 180);
    unstack(close);
  }
  stack(close);
  layer.append(el);
  const refresh = async () => {
    try {
      body.replaceChildren(...[await fill(refresh, close)].flat(Infinity).filter(Boolean));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return welcome("Ta session a expiré.");
      body.replaceChildren(h("p", { class: "empty" }, said(error)));
    }
  };
  body.replaceChildren(h("p", { class: "empty" }, "…"));
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
  layer.replaceChildren();
  open.length = 0;
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
  const search = h("input", {
    type: "search",
    placeholder: "Chercher : loyer, maman, voiture…",
    "aria-label": "Chercher dans la mémoire",
    oninput: () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        query = search.value;
        refreshList();
      }, 250);
    },
  });
  const list = h("div", { class: "list items" });
  let kind = "all";
  const KINDS = { all: "Tout", you: "Toi", people: "Proches", projects: "Projets" };
  const IN = { you: ["profile", "preference"], people: ["person"], projects: ["project"] };
  const CATEGORY = { profile: "toi", preference: "tes goûts", person: "un proche", project: "un projet", other: "divers" };
  const kinds = Object.entries(KINDS).map(([key, text]) =>
    h("button", { type: "button", class: "chip", "aria-pressed": String(key === kind), onclick: () => {
      kind = key;
      for (const b of kinds) b.setAttribute("aria-pressed", String(b === kinds[Object.keys(KINDS).indexOf(key)]));
      refreshList();
    } }, text),
  );
  const toggle = h("button", { type: "button", class: "chip", "aria-pressed": "false", onclick: () => ((past = !past), toggle.setAttribute("aria-pressed", String(past)), refreshList()) }, "Avec l'historique");
  async function refreshList() {
    const found = await api(`api/memory?q=${encodeURIComponent(query)}${past ? "&past=1" : ""}`);
    const facts = found.facts.filter((f) => kind === "all" || IN[kind].includes(f.category));
    list.replaceChildren(
      ...(facts.length
        ? facts.map((f) =>
            h(
              "button",
              { type: "button", class: `item ${f.current ? "" : "past"}`, onclick: () => factSheet(f, refreshList) },
              h("span", {}, h("span", { class: "text" }, f.statement), h("small", { class: f.trusted ? "" : "warn" }, f.current ? `${CATEGORY[f.category] || f.category} · ${f.from_owner ? "tu le lui as dit" : f.source}` : f.why_not || "remplacé")),
            ),
          )
        : [h("p", { class: "empty" }, query ? "Rien de tel." : "Je ne sais encore rien de toi.")]),
    );
  }
  panel("Ce que je sais de toi", async () => {
    await refreshList();
    return [
      h("label", { class: "search glass" }, icon("search", 18), search),
      h("div", { class: "chips" }, kinds, toggle),
      list,
      h("p", { class: "hint" }, "Touche un souvenir pour le corriger ou l'oublier. Rien n'est effacé en silence : l'ancienne version reste dans l'historique."),
    ];
  });
}

function factSheet(fact, refresh) {
  const close = sheet(
    h("p", { class: "quote" }, fact.statement),
    h("p", { class: "label" }, fact.from_owner ? "Tu le lui as dit" : `Vient de : ${fact.source}`),
    fact.current
      ? h(
          "div",
          { class: "list" },
          h("button", { type: "button", onclick: () => askOrion(`Corrige ce souvenir : « ${fact.statement} ». En fait, `) }, icon("wrong", 20), "Corriger"),
          h("button", { type: "button", onclick: () => askOrion(`D'où tu sais que « ${fact.statement} » ?`) }, icon("where", 20), "D'où ça vient"),
          h(
            "button",
            {
              type: "button",
              class: "danger",
              onclick: async () => {
                try {
                  await api(`api/memory/${fact.id}/forget`, {});
                  close();
                  toast("Oublié.");
                  refresh();
                } catch (error) {
                  toast(said(error));
                }
              },
            },
            icon("forget", 20),
            "Oublier",
          ),
        )
      : null,
  );
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
