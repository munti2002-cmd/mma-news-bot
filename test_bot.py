"""Run: .venv/bin/python test_bot.py  (no API keys needed, nothing is posted)"""
import json
import tempfile
from pathlib import Path

import bot

RSS = b"""<rss><channel><item><title>A &amp; B</title><link>https://x/1</link>
<guid>g1</guid><description>&lt;p&gt;Hello&lt;/p&gt;</description></item></channel></rss>"""
ATOM_XML = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>e1</id><title>T</title>
<link rel="alternate" href="https://y/1"/><summary>S</summary></entry></feed>"""

r = bot.parse_feed(RSS)
assert r == [{"id": "g1", "title": "A & B", "link": "https://x/1", "summary": "Hello"}], r
a = bot.parse_feed(ATOM_XML)
assert a[0]["link"] == "https://y/1" and a[0]["id"] == "e1", a
assert bot.pick_new(r + r + a, {"e1"}) == r  # dedup + skip seen

FN = """<h5 class="media-heading"> <a class="page-title" href="https://fightnews.info/a"> Заголовок </a> </h5> Текст &quot;x&quot;... <a title="Читать статью полностью" href="https://fightnews.info/a">"""
assert bot.parse_fightnews(FN.encode()) == [{"id": "https://fightnews.info/a", "title": "Заголовок",
                                            "link": "https://fightnews.info/a", "summary": 'Текст "x"...'}]

# Gemini response parsing (fake HTTP)
import io, urllib.error, urllib.request
fake = {"steps": [{"type": "user_input"}, {"type": "model_output",
        "content": [{"type": "text", "text": "Пост"}]}]}
orig_urlopen = urllib.request.urlopen
urllib.request.urlopen = lambda req, timeout: io.BytesIO(json.dumps(fake).encode())
assert bot.rewrite("k", {"title": "t", "summary": "s"}) == "Пост"
fake["steps"][1]["content"][0]["text"] = "SKIP"
assert bot.rewrite("k", {"title": "t", "summary": "s"}) is None
calls = []
def busy_then_ok(req, timeout):  # strong model overloaded -> lite model answers
    calls.append(json.loads(req.data)["model"])
    if len(calls) == 1:
        raise urllib.error.HTTPError("u", 503, "busy", {}, None)
    return io.BytesIO(json.dumps({"steps": [{"type": "model_output", "content": [{"type": "text", "text": "ok"}]}]}).encode())
urllib.request.urlopen = busy_then_ok
assert bot.rewrite("k", {"title": "t", "summary": "s"}) == "ok" and calls == bot.GEMINI_MODELS, calls
urllib.request.urlopen = orig_urlopen

# og:image extraction (attribute order varies between sites)
bot.fetch = lambda _: b'<meta content="https://i/a.jpg?a=1&amp;b=2" property="og:image" />'
assert bot.og_image("x") == "https://i/a.jpg?a=1&b=2"
bot.fetch = lambda _: b"<html></html>"
assert bot.og_image("x") is None

# run_once with fake feeds/Gemini/Telegram
bot.SEEN_FILE = Path(tempfile.mkdtemp()) / "seen.json"
feed = [{"id": f"id{i}", "title": f"t{i}", "link": f"l{i}", "summary": ""} for i in range(5)]
bot.FEEDS = ["fake"]
bot.parse_feed = lambda _: feed
bot.fetch = lambda _: b""
posted = []
bot.rewrite = lambda c, it: "SKIP-me" if it["id"] != "id3" else None
bot.send_telegram = lambda tok, ch, text, link, photo: posted.append(link)

bot.run_once(None, "t", "c")  # first run: post 1, mark the rest seen
assert posted == ["l0"], posted
assert len(json.loads(bot.SEEN_FILE.read_text())) == 5
bot.run_once(None, "t", "c")  # nothing new
assert posted == ["l0"]
feed[:0] = [{"id": "new", "title": "n", "link": "ln", "summary": ""}]
bot.run_once(None, "t", "c")
assert posted == ["l0", "ln"], posted
bot.RUN_BUDGET_MIN = -1  # budget already used up: post nothing, keep the item for the next run
feed[:0] = [{"id": "late", "title": "x", "link": "lx", "summary": ""}]
bot.run_once(None, "t", "c")
assert posted == ["l0", "ln"] and "late" not in json.loads(bot.SEEN_FILE.read_text())
bot.RUN_BUDGET_MIN = 8
bot.run_once(None, "t", "c")
assert posted == ["l0", "ln", "lx"], posted

# live feeds still parse
import importlib
importlib.reload(bot)
for url in bot.FEEDS:
    items = (bot.parse_fightnews if "fightnews" in url else bot.parse_feed)(bot.fetch(url))
    assert items and items[0]["title"] and items[0]["link"], url
    print(f"ok {len(items):3} items  {url}\n    e.g. {items[0]['title'][:70]}")
print("all checks passed")
