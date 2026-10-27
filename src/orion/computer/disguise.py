"""Orion's browser looks like a person's, and knows when a site turns it away (D145).

Headless Chromium says what it is: « HeadlessChrome » in its name and in the Client Hints it
sends, `navigator.webdriver`, no plugins, one language, notifications refused before anyone
asked, a graphics card no phone has. Anti-bot services (DataDome, Cloudflare, Akamai…) read
that before the page has even loaded, and the owner's errands stopped at « Accès temporairement
restreint ». So the browser runs as the full Chromium (new headless), with automation flags off,
and each page is told the same coherent story: a Chrome on an Android phone, of the engine's own
version, in French.

None of this gets past a site that refuses servers outright: that is the address, not the
browser (`orion.hands.exit` sends the browser out through the owner's own connection). What a
site shows when it turns the browser away is recognised (`BLOCKED`), so Orion says what it is
and what can be done, instead of reading a wall as a page.
"""

from __future__ import annotations

from typing import Any

LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--lang=fr-FR",
)
ACCEPT_LANGUAGE = "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"

INIT = """(() => {
  const phone = MOBILE;
  const define = (proto, name, value) => {
    try { Object.defineProperty(proto, name, {get: value, configurable: true}); } catch (e) {}
  };
  define(Navigator.prototype, 'languages', () => Object.freeze(['fr-FR', 'fr', 'en-US', 'en']));
  define(Navigator.prototype, 'webdriver', () => false);
  if (phone) {
    define(Navigator.prototype, 'platform', () => 'Linux armv81');
    define(Navigator.prototype, 'deviceMemory', () => 8);
    define(Navigator.prototype, 'hardwareConcurrency', () => 8);
    define(Navigator.prototype, 'maxTouchPoints', () => 5);
  }
  if (window.Notification) define(Notification, 'permission', () => 'default');
  const query = navigator.permissions && navigator.permissions.query;
  if (query) {
    navigator.permissions.query = function (parameters) {
      if (parameters && parameters.name === 'notifications')
        return Promise.resolve({state: 'prompt', onchange: null});
      return query.call(this, parameters);
    };
  }
  const [vendor, renderer] = phone ? ['Qualcomm', 'Adreno (TM) 740'] : ['Google Inc. (Intel)',
    'ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)'];
  for (const gl of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
    if (!gl) continue;
    const get = gl.prototype.getParameter;
    gl.prototype.getParameter = function (p) {
      if (p === 37445) return vendor;
      if (p === 37446) return renderer;
      return get.call(this, p);
    };
  }
})();"""
"""Run in every page and frame before the site's own scripts."""


def init_script(phone: bool) -> str:
    return INIT.replace("MOBILE", "true" if phone else "false")


def user_agent(version: str, phone: bool) -> dict[str, Any]:
    """`Emulation.setUserAgentOverride` for a Chrome of the engine's `version` (« 153.0.8010.12 »):
    the user agent as Chrome now reduces it, and the Client Hints that go with it."""
    major = version.split(".")[0]
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not_A Brand", "version": "24"},
    ]
    full = [
        {**b, "version": version if b["brand"] != "Not_A Brand" else "24.0.0.0"} for b in brands
    ]
    if phone:
        agent = (
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Mobile Safari/537.36"
        )
        metadata = {
            "platform": "Android",
            "platformVersion": "14.0.0",
            "architecture": "",
            "model": "Pixel 8",
            "mobile": True,
        }
    else:
        agent = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36"
        )
        metadata = {
            "platform": "Linux",
            "platformVersion": "6.8.0",
            "architecture": "x86",
            "model": "",
            "mobile": False,
        }
    return {
        "userAgent": agent,
        "acceptLanguage": ACCEPT_LANGUAGE,
        "platform": "Linux armv81" if phone else "Linux x86_64",
        "userAgentMetadata": {
            "brands": brands,
            "fullVersionList": full,
            "fullVersion": version,
            "bitness": "64",
            "wow64": False,
            **metadata,
        },
    }


_REFUSED = "Accès temporairement restreint|access is temporarily restricted"
_CHECKING = "just a moment|un instant…|checking your browser|vérification de votre navigateur"
_HUMAN = "vérification que vous êtes un humain|verify you are human"
BLOCKED = (
    """() => {
  const text = (document.body ? document.body.innerText : '').slice(0, 3000);
  const title = document.title || '';
  const frames = [...document.querySelectorAll('iframe')].map(f => f.src || '').join(' ');
  const has = (re) => re.test(text) || re.test(title);
  const refused = new RegExp('REFUSED', 'i');
  const checking = new RegExp('CHECKING', 'i');
  const human = new RegExp('HUMAN', 'i');
  if (/captcha-delivery\\.com/.test(frames) || has(refused))
    return {by: 'DataDome', puzzle: /captcha-delivery\\.com\\/captcha/.test(frames)};
  if (/challenges\\.cloudflare\\.com/.test(frames) || has(checking) || has(human))
    return {by: 'Cloudflare', puzzle: /challenges\\.cloudflare\\.com/.test(frames),
            passing: has(checking)};
  if (document.getElementById('px-captcha') || has(/press & hold|appuyez et maintenez/i))
    return {by: 'PerimeterX', puzzle: true};
  if (has(/^access denied$/i) && /reference #/i.test(text))
    return {by: 'Akamai', puzzle: false};
  const puzzles = /hcaptcha\\.com|google\\.com\\/recaptcha|recaptcha\\.net/;
  if (puzzles.test(frames) && text.length < 1500)
    return {by: 'captcha', puzzle: true};
  return null;
}""".replace("REFUSED", _REFUSED)
    .replace("CHECKING", _CHECKING)
    .replace("HUMAN", _HUMAN)
)
"""What a page says when an anti-bot service turns the browser away: by whom, whether a person
can get past it (a puzzle), and whether it may clear by itself (a check that runs, then lets
through)."""
