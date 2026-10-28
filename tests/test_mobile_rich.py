"""How the app draws Orion's text: links, bold and code, run in a real browser."""

from pathlib import Path

from playwright.async_api import async_playwright

APP = (Path(__file__).parents[1] / "src/orion/mobile/static/app.js").read_text(encoding="utf-8")


def _piece(start: str, end: str) -> str:
    begin = APP.index(start)
    return APP[begin : APP.index(end, begin) + len(end)]


SOURCE = _piece("function h(", "\n}\n") + _piece("// An address may hold", "\n  return out;\n}\n")

DRAW = """(texts) => texts.map((text) => {
  const el = document.createElement("div");
  el.append(...rich(text));
  return {
    html: el.innerHTML.replace(/ target="_blank" rel="noopener noreferrer"/g, ""),
    text: el.textContent,
  };
})"""


async def _draw(texts: list[str]) -> list[dict[str, str]]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.add_script_tag(content=SOURCE + "\nwindow.rich = rich;")
        drawn: list[dict[str, str]] = await page.evaluate(DRAW, texts)
        await browser.close()
    return drawn


async def test_links_are_drawn_whole_and_nothing_is_parsed_as_html() -> None:
    """Seen for real: `[le site](https://…)` showed its brackets and lost its label."""
    drawn = await _draw(
        [
            "Regarde [le site](https://example.com/a?b=1) ce soir.",
            "C'est ici : https://example.com/page.",
            "(voir https://www.example.com/)",
            "https://fr.wikipedia.org/wiki/Paris_(France), la page.",
            "[Paris](https://fr.wikipedia.org/wiki/Paris_(France))",
            "Autolien <https://example.com/x>",
            "**Important** : **[billet](https://example.com/t)**",
            "<b>pas du html</b> [x](javascript:alert(1))",
            "https://example.com/" + "a" * 80,
        ]
    )
    html = [d["html"] for d in drawn]
    assert html[0] == 'Regarde <a href="https://example.com/a?b=1">le site</a> ce soir.'
    assert html[1] == 'C\'est ici : <a href="https://example.com/page">example.com/page</a>.'
    assert html[2] == '(voir <a href="https://www.example.com/">example.com</a>)'
    assert html[3] == (
        '<a href="https://fr.wikipedia.org/wiki/Paris_(France)">'
        "fr.wikipedia.org/wiki/Paris_(France)</a>, la page."
    )
    assert html[4] == '<a href="https://fr.wikipedia.org/wiki/Paris_(France)">Paris</a>'
    assert html[5] == 'Autolien <a href="https://example.com/x">example.com/x</a>'
    assert html[6] == '<b>Important</b> : <b><a href="https://example.com/t">billet</a></b>'
    assert drawn[7]["text"] == "<b>pas du html</b> [x](javascript:alert(1))"
    assert "<a" not in html[7]
    assert drawn[8]["text"].endswith("…") and len(drawn[8]["text"]) == 48


async def test_backquoted_text_is_drawn_as_code_not_shown_with_its_quotes() -> None:
    """Seen for real: `orion doctor` came through with its backquotes."""
    drawn = await _draw(["Lance `orion doctor` puis `https://example.com/x` ici."])
    assert drawn[0]["html"] == (
        "Lance <code>orion doctor</code> puis <code>https://example.com/x</code> ici."
    )
