"""How the app moves: the typing dots and the keyboard, run in a real browser."""

from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

APP = (Path(__file__).parents[1] / "src/orion/mobile/static/app.js").read_text(encoding="utf-8")


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
