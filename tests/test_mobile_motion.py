"""How the app moves: the typing dots and the keyboard, run in a real browser."""

import asyncio
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

APP_DIR = Path(__file__).parents[1] / "src/orion/mobile/static"
APP = (APP_DIR / "app.js").read_text(encoding="utf-8")


def _piece(start: str, end: str) -> str:
    begin = APP.index(start)
    return APP[begin : APP.index(end, begin) + len(end)]


FIT = APP[APP.index("const view = {};") : APP.index("\n}\n", APP.index("function fit() {")) + 3]


async def _run(source: str, script: str) -> Any:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content("<textarea></textarea>")
        await page.add_script_tag(content=source)
        result = await page.evaluate(script)
        await browser.close()
    return result


async def test_the_composer_gives_back_the_dock_room_only_once_the_keyboard_is_up() -> None:
    """Seen for real: on focus the composer dropped, then jumped up when the keyboard came."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content("<textarea></textarea>")
        await page.add_script_tag(
            content=f"function nearBottom() {{ return false; }}\n{FIT}\nwindow.fit = fit;"
        )
        keyboard = "() => (fit(), document.body.classList.contains('keyboard'))"
        await page.evaluate(keyboard)  # the app fits itself once on load
        await page.focus("textarea")
        on_focus = await page.evaluate(keyboard)
        await page.set_viewport_size({"width": 390, "height": 500})
        with_keyboard = await page.evaluate(keyboard)
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.evaluate("document.querySelector('textarea').blur()")
        after = await page.evaluate(keyboard)
        await browser.close()
    assert (on_focus, with_keyboard, after) == (False, True, False)


PIN = _piece("function pinToBottom(", "\n}\n")


async def test_a_thread_read_at_its_end_stays_there_as_it_grows() -> None:
    """Seen for real: an image or the dots appeared below and the last message was left hidden."""
    result = await _run(
        PIN + "\nwindow.pinToBottom = pinToBottom;",
        """async () => {
          const thread = document.createElement("div");
          thread.style.cssText = "height:300px; overflow-y:auto";
          document.body.append(thread);
          const grow = async (px) => {
            const el = document.createElement("div");
            el.style.height = `${px}px`;
            thread.append(el);
            await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
          };
          const gap = () => thread.scrollHeight - thread.scrollTop - thread.clientHeight;
          pinToBottom(thread, () => true);
          await grow(1000);
          const atEnd = gap();
          thread.scrollTop = 0;
          await new Promise((r) => setTimeout(r, 50));
          await grow(200);
          return { atEnd, readingAbove: thread.scrollTop };
        }""",
    )
    assert result == {"atEnd": 0, "readingAbove": 0}


RETAIL = _piece("function retail() {", "\n}\n")


async def test_a_message_sent_after_a_pause_shows_its_time_once() -> None:
    """Seen for real: after a pause, the time above a sent message appeared twice."""
    result = await _run(
        RETAIL + "\nwindow.retail = retail;",
        """() => {
          const list = document.createElement("div");
          document.body.append(list);
          window.view = { list, older: document.createElement("button") };
          window.state = { more: false };
          window.timestamp = () => Object.assign(document.createElement("p"), { className: "day" });
          const msg = (who, at) => {
            const el = document.createElement("div");
            el.className = `msg ${who}`;
            el.dataset.who = who;
            el.dataset.at = at;
            return el;
          };
          const later = Date.now();
          list.append(msg("orion", later - 3600000));
          retail();
          const pending = msg("owner", later);
          list.append(pending);
          retail();
          pending.remove();  // the server's copy replaces the pending bubble
          list.append(timestamp(), msg("owner", later));
          retail();
          return list.querySelectorAll(".day").length;
        }""",
    )
    assert result == 2


TASK = {
    "id": "t1",
    "goal": "Trier",
    "passes": 0,
    "spent": 0,
    "budget": 1,
    "minutes": 1,
    "started_at": "2026-11-04T14:00:00Z",
    "waiting": False,
    "last": "",
}


async def _in_the_app(act: str, *, fast_send: bool = False) -> tuple[list[str], Any]:
    """What enters the screen while the real app, on a fake server, does `act`, and its result."""
    message = {"who": "owner", "files": [], "text": "Salut", "at": "2026-11-04T14:00:00Z"}
    answers: dict[str, Any] = {
        "me": {"device": "d", "push_key": ""},
        "api/messages": {"more": False, "messages": [{**message, "ref": "a", "seq": 1}]},
        "api/for-you": {"questions": [], "waiting_on": [], "tasks": [TASK]},
        "approvals": {"approvals": []},
        "api/agenda": {"events": []},
        "api/nights": {"notes": [], "nights": []},
        "api/browser": {"handovers": []},
        "api/logins/asked": {"asked": []},
        "api/missions": {"missions": []},
        "api/send": {"message": {**message, "ref": "b", "seq": 2, "text": "Test"}},
    }

    async def serve(route: Any) -> None:
        path = route.request.url.split("/app/")[-1].split("?")[0]
        if (APP_DIR / (path or "index.html")).is_file():
            await route.fulfill(path=str(APP_DIR / (path or "index.html")))
        elif path == "finish-the-task":  # the task ends and the server says so on the stream
            answers["api/for-you"] = {"questions": [], "waiting_on": [], "tasks": []}
            answers["stream"] = 'data: {"type": "changed", "what": ["tasks"]}\n\n'
            await route.fulfill(json={})
        elif path == "api/stream":
            if "stream" in answers:
                await route.fulfill(body=answers["stream"], content_type="text/event-stream")
            else:
                await route.abort()
        elif path == "api/send":
            await asyncio.sleep(0.01 if fast_send else 0.2)
            await route.fulfill(json=answers[path])
        else:
            await route.fulfill(json=answers.get(path, {}))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.route("**/app/**", serve)
        await page.goto("http://orion.test/app/")
        await page.wait_for_selector(".msg")
        await page.evaluate(
            """() => {
              window.seen = [];
              addEventListener("animationstart", (e) => seen.push(e.animationName), true);
            }"""
        )
        result = await page.evaluate(act)
        await page.wait_for_timeout(800)
        seen = await page.evaluate("seen")
        await browser.close()
    return seen, result


async def test_a_message_sent_keeps_rising_when_the_server_answers() -> None:
    """Seen for real: the bubble rose, then rose again from the bottom on the server's answer."""
    _, progress = await _in_the_app(
        """async () => {
          document.querySelector(".composer textarea:not(.twin)").value = "Test";
          document.querySelector(".composer").requestSubmit();
          let bubble = null;
          while (!(bubble = document.querySelector('.msg[data-ref="b"] > .bubble'))) {
            await new Promise((r) => setTimeout(r, 10));
          }
          return bubble.getAnimations()[0].effect.getComputedTiming().progress;
        }"""
    )
    assert progress > 0.3


async def test_a_page_seen_again_does_not_enter_again() -> None:
    """Seen for real: back on a page, its content blanked and rebuilt once the data arrived."""
    seen, _ = await _in_the_app(  # row-in is the entrance cascade of a page
        """async () => {
          const pause = () => new Promise((r) => setTimeout(r, 300));
          document.querySelector(".top-back").click();
          while (!document.querySelector(".home-planet")) await pause();
          await pause();
          document.querySelector(".home-planet").click();
          await pause();
          seen.length = 0;
          document.querySelector(".top-back").click();
        }"""
    )
    assert "row-in" not in seen


async def test_the_launch_ends_with_one_planet_in_its_place() -> None:
    """Seen for real: the planet jumped on launch; it must land, and the app must show."""
    _, end = await _in_the_app(
        """async () => {
          while (document.querySelector(".planet-ghost")) {
            await new Promise((r) => setTimeout(r, 20));
          }
          return [
            document.documentElement.classList.contains("booting"),
            getComputedStyle(document.querySelector(".avatar .planet")).visibility,
          ];
        }"""
    )
    assert end == [False, "visible"]


async def test_the_dock_field_leads_to_the_chat_with_the_keyboard_up() -> None:
    """Seen for real: the bottom field threw the owner onto the chat with no transition."""
    seen, end = await _in_the_app(
        """async () => {
          const pause = () => new Promise((r) => setTimeout(r, 300));
          document.querySelector(".top-back").click();
          while (!document.querySelector(".ask-open")) await pause();
          await pause();
          seen.length = 0;
          document.querySelector(".ask-open").click();
          const field = document.querySelector(".composer .field");
          return [field.getAnimations().length > 0, document.activeElement.tagName];
        }"""
    )
    assert end == [True, "TEXTAREA"]
    assert "fade-in" in seen and "tab-right" not in seen and "tab-left" not in seen


async def test_the_comet_flies_when_a_conversation_resumes_not_on_every_message() -> None:
    """A long effect on every message wears out in a day: the owner asked for it not to."""
    _, comets = await _in_the_app(
        """async () => {
          const send = async () => {
            document.querySelector(".composer textarea:not(.twin)").value = "Test";
            document.querySelector(".composer").requestSubmit();
            await new Promise((r) => setTimeout(r, 300));
            const flying = document.querySelectorAll(".spark").length;
            await new Promise((r) => setTimeout(r, 1200));
            return flying;
          };
          return [await send(), await send()];
        }""",
        fast_send=True,  # seen when filming: an answer quicker than the comet's start lost it
    )
    assert comets[0] > 0
    assert comets[1] == 0


async def test_what_orion_finished_carrying_comes_back_to_the_planet() -> None:
    """The moment plays once, when a task the owner saw in orbit ends, and leaves nothing behind."""
    _, end = await _in_the_app(
        """async () => {
          const pause = (ms) => new Promise((r) => setTimeout(r, ms));
          await pause(300);
          const before = document.querySelectorAll(".spark").length;
          await fetch("/app/finish-the-task");
          let flew = false;
          for (let i = 0; i < 100 && !flew; i++) {
            flew = document.querySelectorAll(".spark").length > 0;
            await pause(100);
          }
          while (document.querySelector(".spark, .planet-wave, .planet-flash")) await pause(100);
          return [before, flew];
        }"""
    )
    assert end == [0, True]


async def test_no_white_band_behind_the_ios_bar_above_the_keyboard() -> None:
    """Seen for real: a white strip around iOS's form bar, under the lavender conversation."""
    css = (APP_DIR / "app.css").read_text(encoding="utf-8")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(f"<style>{css}</style><body><div class='tab talk-tab'></div></body>")
        behind = "() => getComputedStyle(document.documentElement).backgroundColor"
        tab = "() => getComputedStyle(document.querySelector('.talk-tab')).backgroundColor"
        await page.evaluate("document.body.classList.add('keyboard')")
        band, wash = await page.evaluate(behind), await page.evaluate(tab)
        await browser.close()
    assert band == wash


async def test_orion_words_unfold_then_the_bubble_is_plain_text_again() -> None:
    """The reveal must never lose a word or leave its spans behind in the thread."""
    helpers = """
      function still() { return false; }
      function h(tag, attrs, text) {
        const el = document.createElement(tag);
        el.className = attrs.class;
        el.textContent = text;
        return el;
      }
    """
    result = await _run(
        helpers + _piece("function unfold(", "\n}\n") + "\nwindow.unfold = unfold;",
        """async () => {
          const bubble = document.createElement("div");
          bubble.innerHTML = "Deux rendez-vous, <b>rien d'urgent</b> ici.";
          document.body.append(bubble);
          const before = bubble.textContent;
          unfold(bubble);
          const during = [bubble.querySelectorAll(".word").length > 0, bubble.textContent];
          await new Promise((r) => setTimeout(r, 1600));
          return [during, bubble.querySelectorAll(".word").length, bubble.textContent === before];
        }""",
    )
    assert result[0][0] is True
    assert result[0][1] == "Deux rendez-vous, rien d'urgent ici."
    assert result[1:] == [0, True]
