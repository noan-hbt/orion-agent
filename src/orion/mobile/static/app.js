// Orion's app (D125): pairing, signing in, the conversation, approvals, notifications.
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
};

try {
  state.draft = localStorage.getItem("orion.draft") || "";
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
  const height = window.visualViewport ? window.visualViewport.height : window.innerHeight;
  document.documentElement.style.setProperty("--app-h", `${height}px`);
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
    const me = await api("api/me");
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
        h("p", { class: "hint" }, "Ta clé reste dans ton iPhone, protégée par Face ID. Orion ne garde que sa partie publique."),
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
    h(
      "div",
      { class: "actions" },
      h(
        "button",
        { class: "round glass with-badge", "aria-label": "Pour toi", onclick: forYou },
        icon("bell", 20),
        view.badge,
      ),
      h("button", { class: "round glass", "aria-label": "Contrôle", onclick: menu }, icon("menu", 20)),
    ),
  );

  app.replaceChildren(
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
      h("p", {}, "Tu peux tout lui demander ici, comme sur Telegram, et lui répondre d'un geste quand il a besoin de toi."),
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
      state.approvals.clear();
      for (const a of data.approvals) state.approvals.set(a.id, a);
      paintApprovals();
      countQuestions();
    } else if (data.type === "message") {
      add(data.message);
      if (data.message.who === "orion") setTyping(false, true);
      countQuestions();
    } else if (data.type === "typing") {
      setTyping(data.on);
      if (!data.on) setWork(null);
    } else if (data.type === "work") {
      setWork(data.work);
    } else if (data.type === "approval") {
      state.approvals.set(data.approval.id, data.approval);
      paintApprovals();
      bottom(true);
    } else if (data.type === "approval_closed") {
      state.approvals.delete(data.id);
      paintApprovals();
    }
  };
  source.onerror = async () => {
    setLive(false);
    source.close();
    try {
      await api("api/me");
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
  const step = (text, current) =>
    h("li", { class: current ? "now" : "" }, h("span", { class: "mark" }, current ? h("i") : icon("check", 13)), text);
  view.work.replaceChildren(
    h(
      "header",
      {},
      h("span", { class: "ico" }, icon("orion", 18, "none")),
      h("span", {}, h("b", {}, "Il travaille"), h("small", {}, clock, ` · ${plural(work.steps, "étape", "étapes")}`, work.cost ? ` · ${money(work.cost)}` : "")),
      stop,
    ),
    h("ul", {}, (work.done || []).slice(-4).map((t) => step(t, false)), work.doing.map((t) => step(t, true))),
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

function paintApprovals() {
  paintBadge();
  view.asks.replaceChildren(...[...state.approvals.values()].map(askCard));
}

function paintBadge() {
  // What waits on him: approvals, and the questions Orion waits on him for.
  const count = state.approvals.size + (state.questions || 0);
  view.badge.hidden = !count;
  view.badge.textContent = String(count);
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
      h("span", {}, h("b", {}, "Il te demande ton accord"), left),
    ),
    ...details(asked),
    h("div", { class: "row" }, no, slider("Glisse pour accepter", () => accept())),
    status,
  );

  async function refuse() {
    no.disabled = true;
    try {
      await api(`api/approvals/${asked.id}/refuse`, {});
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
      const opened = await api(`api/approvals/${asked.id}/options`, { answer: "yes" });
      const credential = await sign(opened.options);
      await api(`api/approvals/${asked.id}`, { answer: "yes", ceremony: opened.ceremony, credential });
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

// -- Pour toi, the menu -----------------------------------------------------------------------

function forYou() {
  panel("Pour toi", async (refresh, close) => {
    let found = { approvals: [...state.approvals.values()], questions: [], waiting_on: [], tasks: [] };
    try {
      found = await api("api/for-you");
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
    const sections = [];
    if (found.approvals.length) {
      sections.push(
        h("p", { class: "label" }, "Ton accord"),
        h(
          "div",
          { class: "list items" },
          found.approvals.map((a) =>
            h(
              "button",
              {
                type: "button",
                class: "item",
                onclick: () => {
                  close();
                  setTimeout(() => view.asks.querySelector(".ask")?.scrollIntoView({ behavior: "smooth", block: "center" }), 200);
                },
              },
              h("span", { class: "ico" }, icon("shield", 16)),
              h("span", {}, h("span", { class: "text" }, askTitle(a)), h("small", {}, "glisse puis Face ID dans la conversation")),
            ),
          ),
        ),
      );
    }
    if (found.questions.length) {
      sections.push(
        h("p", { class: "label" }, "Il attend ta réponse"),
        h(
          "div",
          { class: "list items" },
          found.questions.map((q) =>
            h(
              "div",
              { class: "item" },
              h("span", { class: "ico" }, icon("where", 16)),
              h("span", {}, h("span", { class: "text" }, q.about), h("small", {}, q.auto === "approval" ? "un accord resté sans réponse" : `il y revient ${SHORT.format(new Date(q.back_at))}`)),
              h("span", { class: "acts" }, h("button", { class: "pill small", type: "button", onclick: () => askOrion(`À propos de « ${q.about} » : `) }, "Répondre")),
            ),
          ),
        ),
      );
    }
    if (found.tasks.length) {
      sections.push(
        h("p", { class: "label" }, "En tâche de fond"),
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
                h("small", {}, [t.waiting ? "attend Orion" : t.passes ? plural(t.passes, "passage", "passages") : "démarre", `${money(t.spent)} sur ${money(t.budget)}`].join(" · ")),
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
        h("p", { class: "label" }, "Il attend des autres"),
        h(
          "div",
          { class: "list items" },
          found.waiting_on.map((w) =>
            h(
              "div",
              { class: "item" },
              h("span", { class: "ico" }, icon("clock", 16)),
              h("span", {}, h("span", { class: "text" }, `${w.who} · ${w.about}`), h("small", {}, `${w.round > 1 ? `relance ${w.round - 1} · ` : ""}il y revient ${SHORT.format(new Date(w.back_at))}`)),
              h("span", { class: "acts" }, h("button", { class: "pill small ghost", type: "button", onclick: act(`api/waits/${w.id}/drop`) }, "Laisser")),
            ),
          ),
        ),
      );
    }
    return sections.length ? sections : h("p", { class: "empty" }, "Rien ne t'attend. Quand Orion aura besoin de ton accord ou d'une réponse, ce sera ici.");
  });
}

// -- the controls: a bento of what Orion is doing, and the way into everything else ------------

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

function menu() {
  panel("Contrôle", async (refresh) => {
    const o = await api("api/control");
    const act = (path, payload) => async () => {
      try {
        await api(path, payload);
        refresh();
      } catch (error) {
        toast(said(error));
      }
    };
    const paused = !!o.paused_until;
    const status = h(
      "div",
      { class: `tile hero ${paused ? "paused" : ""}` },
      h("span", { class: "dot" }),
      h(
        "div",
        {},
        h("b", {}, paused ? "En pause" : o.quiet_now ? "Actif · heures calmes" : "Actif"),
        h(
          "small",
          {},
          paused
            ? `jusqu'à ${UNTIL.format(new Date(o.paused_until))} · tes rappels passent quand même`
            : "il prend des initiatives, dans les limites ci-dessous",
        ),
      ),
      h(
        "div",
        { class: "row" },
        paused
          ? h("button", { class: "pill small dark", type: "button", onclick: act("api/control/pause", { minutes: null }) }, "Reprendre")
          : [
              h("button", { class: "pill small", type: "button", onclick: act("api/control/pause", { minutes: 60 }) }, "1 h"),
              h("button", { class: "pill small", type: "button", onclick: act("api/control/pause", { minutes: minutesUntilTomorrow(8) }) }, "Jusqu'à demain"),
            ],
      ),
    );
    const cost = o.cost
      ? [
          h("div", { class: "tile" }, h("small", {}, "Aujourd'hui"), h("b", { class: "big" }, money(o.cost.today)), h("small", {}, `dont ${money(o.cost.alone_today)} de lui-même`)),
          h(
            "div",
            { class: "tile" },
            h("small", {}, "Ce mois-ci"),
            h("b", { class: "big" }, money(o.cost.month)),
            o.cost.monthly_cap ? meter(o.cost.month / o.cost.monthly_cap, `sur ${money(o.cost.monthly_cap)}`) : h("small", {}, "sans plafond"),
          ),
        ]
      : null;
    const budget = h(
      "div",
      { class: "tile" },
      h("small", {}, "Messages de lui-même / jour"),
      h(
        "div",
        { class: "stepper" },
        h("button", { type: "button", "aria-label": "Moins", disabled: o.daily_budget <= 0, onclick: act("api/control/settings", { daily_budget: o.daily_budget - 1 }) }, "−"),
        h("b", { class: "big" }, String(o.daily_budget)),
        h("button", { type: "button", "aria-label": "Plus", disabled: o.daily_budget >= 50, onclick: act("api/control/settings", { daily_budget: o.daily_budget + 1 }) }, "+"),
      ),
    );
    const quiet = h(
      "button",
      { class: "tile", type: "button", onclick: () => quietHours(o, refresh) },
      h("small", {}, "Heures calmes"),
      h("b", { class: "big" }, `${o.quiet_start}–${o.quiet_end}`),
      h("small", {}, "il se tait, sauf urgence"),
    );
    const go = (name, title, count, open) =>
      h("button", { class: "tile link", type: "button", onclick: open }, h("span", { class: "ico" }, icon(name, 18)), h("b", {}, title), count !== null ? h("small", {}, count) : null);
    const next = o.next.length
      ? h(
          "div",
          { class: "tile wide" },
          h("small", {}, "Bientôt, de lui-même"),
          o.next.map((p) => h("p", { class: "next" }, h("span", {}, SHORT.format(new Date(p.at))), p.what)),
        )
      : null;
    return [
      h("div", { class: "grid" }, status, cost, budget, quiet, next),
      h("p", { class: "label" }, "Ce qu'il sait et fait"),
      h(
        "div",
        { class: "grid" },
        go("spark", "Mémoire", plural(o.counts.facts, "souvenir", "souvenirs"), memoryPanel),
        go("check", "Engagements", plural(o.counts.commitments, "ouvert", "ouverts"), commitmentsPanel),
        go("shield", "Autonomie", o.alone.length ? `${o.alone.length} seul` : "il demande tout", autonomyPanel),
        go("clock", "Activité", "et ses silences", activityPanel),
        go("file", "Son manuel", plural(o.counts.rules, "règle", "règles"), rulesPanel),
        go("calendar", "À venir", "ce qu'il fera seul", plannedPanel),
      ),
      h("p", { class: "label" }, "Cet appareil"),
      h(
        "div",
        { class: "list" },
        "Notification" in window && state.pushKey
          ? h(
              "button",
              { type: "button", onclick: enableNotifications },
              icon("bell", 20),
              "Notifications",
              h("small", {}, Notification.permission === "granted" ? "activées" : "désactivées"),
            )
          : null,
        h(
          "button",
          {
            type: "button",
            class: "danger",
            onclick: async () => {
              await api("api/logout", {}).catch(() => {});
              source?.close();
              layer.replaceChildren();
              open.length = 0;
              welcome();
            },
          },
          icon("door", 20),
          "Se déconnecter",
          h("small", {}, state.device || ""),
        ),
      ),
    ];
  });
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
    h("p", { class: "quote" }, "Entre ces heures, il ne t'écrit pas de lui-même, sauf un rappel que tu as demandé. Si tu lui écris, il te répond."),
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
        : [h("p", { class: "empty" }, query ? "Rien de tel." : "Il ne sait encore rien de toi.")]),
    );
  }
  panel("Ce qu'il sait de toi", async () => {
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
      h("p", { class: "hint" }, "Ce qu'il a appris de ta façon de travailler. Retire ce qui n'est plus vrai."),
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
  panel("Ce qu'il fait seul", async (refresh) => {
    const { actions } = await api("api/autonomy");
    return [
      h("p", { class: "hint" }, "Tu peux lui reprendre une autonomie d'un geste. Pour lui en donner, dis-le-lui : il te le demandera avec Face ID, limites comprises."),
      h(
        "div",
        { class: "list items" },
        actions.map((a) =>
          h(
            "div",
            { class: "item" },
            h("span", {}, h("span", { class: "text" }, a.label), h("small", {}, a.alone ? "seul, dans ses limites" : a.streak ? `il demande · oui ${a.streak} fois de suite` : "il demande")),
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
  panel("Ce qu'il a fait", async (refresh) => {
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
      h("p", { class: "hint" }, "Ce qu'il fera de lui-même ces deux prochaines semaines. Pour en annuler un, touche-le."),
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
      h("span", {}, h("b", {}, "Être prévenu quand l'app est fermée"), h("small", {}, "ses messages et ses demandes d'accord")),
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
  await api("api/push/subscribe", { endpoint: json.endpoint, keys: json.keys });
}

// -- photo viewer -----------------------------------------------------------------------------

function viewer(url, name) {
  const el = h("div", { class: "viewer", role: "dialog", "aria-label": name, onclick: () => el.remove() }, h("img", { src: url, alt: name }));
  layer.append(el);
}

start();
