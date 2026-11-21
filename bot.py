"""MMA/UFC news bot: RSS -> Gemini (rewrite in Russian) -> Telegram channel."""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).parent
SEEN_FILE = HERE / "seen.json"
FEEDS = [
    "https://www.championat.com/rss/news/boxing/",  # бокс+ММА, бокс отсеивает PROMPT
    "https://fightnews.info/mma",                    # нет RSS, читаем страницу
    "https://www.mmafighting.com/rss/index.xml",
    "https://www.mmamania.com/rss/index.xml",
    "https://www.sherdog.com/rss/news.xml",
    "https://www.espn.com/espn/rss/mma/news",
    "https://www.ufc.com/rss/news",
]
MAX_POSTS_PER_RUN = 3      # don't flood the channel
RUN_BUDGET_MIN = 8         # stop starting new posts after this, the rest waits for the next run
INTERVAL_MIN = 15          # how often to check feeds
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"
# free tier; the stronger model is often overloaded, then the lite one answers in seconds
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]

PROMPT = """Ты редактор русскоязычного Telegram-канала про MMA и UFC.
Перепиши новость ниже в пост для канала:
- на русском языке, своими словами (не дословный перевод);
- первая строка — короткий цепляющий заголовок с 1 эмодзи;
- затем 2–4 коротких предложения с сутью: кто, что, когда;
- имена бойцов пиши в принятой русской транслитерации;
- ничего не выдумывай сверх текста новости;
- без хештегов и без ссылок.
Если это не новость про MMA (например, бокс, реклама, ставки, мерч), ответь ровно словом SKIP.

Заголовок: {title}
Текст: {summary}"""


def load_env():
    # shortcut: minimal .env reader (KEY=VALUE lines), use python-dotenv if you need quoting
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


QUESTIONS = {
    "TELEGRAM_BOT_TOKEN": "Вставь токен бота от @BotFather: ",
    "TELEGRAM_CHANNEL": "Имя канала (например @my_mma_news): ",
    "GEMINI_API_KEY": "Ключ Gemini с aistudio.google.com/apikey: ",
}


def ask(key, problem=""):
    if not sys.stdin.isatty():
        sys.exit(problem or f"Не заполнен {key} в файле .env")
    if problem:
        print(problem)
    os.environ[key] = input(QUESTIONS[key]).strip()


def save_env():
    (HERE / ".env").write_text("".join(f"{k}={os.environ.get(k, '')}\n" for k in QUESTIONS))
    (HERE / ".env").chmod(0o600)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def parse_feed(xml_bytes):
    """Return [{'id','title','link','summary'}] from RSS 2.0 or Atom."""
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        items.append({
            "id": (it.findtext("guid") or link).strip(),
            "title": strip_html(it.findtext("title")),
            "link": link,
            "summary": strip_html(it.findtext("description") or it.findtext(CONTENT_ENCODED))[:3000],
        })
    for e in root.iter(f"{ATOM}entry"):
        link_el = e.find(f"{ATOM}link[@rel='alternate']")
        if link_el is None:
            link_el = e.find(f"{ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        items.append({
            "id": (e.findtext(f"{ATOM}id") or link).strip(),
            "title": strip_html(e.findtext(f"{ATOM}title")),
            "link": link,
            "summary": strip_html(e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content"))[:3000],
        })
    return items


def parse_fightnews(page_bytes):
    """fightnews.info has no RSS: pull title/link/teaser from the list page."""
    # shortcut: depends on the site's HTML layout, if it yields 0 items the markup changed
    page = page_bytes.decode("utf-8", "replace")
    rows = re.findall(r'<a class="page-title" href="([^"]+)">(.*?)</a>\s*</h5>(.*?)<a title="Читать', page, re.S)
    return [{"id": link, "title": strip_html(t), "link": link, "summary": strip_html(d)[:3000]}
            for link, t, d in rows]


def rewrite(api_key, item):
    """Rewrite one news item with Gemini (free tier). Returns post text or None to skip."""
    text = gemini(api_key, PROMPT.format(**item))
    return None if not text or text == "SKIP" else text


def gemini(api_key, prompt):
    """Ask Gemini, falling back to the lite model when the strong one is busy. Returns text."""
    for i, model in enumerate(GEMINI_MODELS):
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            data=json.dumps({"model": model, "input": prompt}).encode(),
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json",
                     "Api-Revision": "2026-05-20"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.load(r)
            break
        except (urllib.error.HTTPError, TimeoutError) as e:
            busy = not isinstance(e, urllib.error.HTTPError) or e.code in (429, 500, 503)
            if not busy or i == len(GEMINI_MODELS) - 1:
                raise
            print(f"{model} busy ({e}), trying {GEMINI_MODELS[i + 1]}", file=sys.stderr)
    return "".join(
        c.get("text", "")
        for step in resp.get("steps", []) if step.get("type") == "model_output"
        for c in step.get("content", []) if c.get("type") == "text"
    ).strip()


def og_image(link):
    """Main picture of the article page (og:image), or None."""
    try:
        page = fetch(link).decode("utf-8", "replace")
    except Exception:
        return None
    for tag in re.findall(r"<meta[^>]+>", page):
        if re.search(r'property=["\']og:image["\']', tag):
            m = re.search(r'content=["\']([^"\']+)', tag)
            if m and m.group(1).startswith("http"):
                return html.unescape(m.group(1))
    return None


def tg(token, method, params):
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        if not json.load(r).get("ok"):
            raise RuntimeError(f"Telegram rejected {method}")


def send_telegram(token, channel, text, link, photo=None):
    body = f"{html.escape(text)}\n\n<a href=\"{html.escape(link)}\">Источник</a>"
    if photo and len(body) <= 1024:  # Telegram caption limit
        try:
            return tg(token, "sendPhoto", {"chat_id": channel, "photo": photo,
                                           "caption": body, "parse_mode": "HTML"})
        except Exception as e:
            print(f"photo failed, posting text only: {e}", file=sys.stderr)
    tg(token, "sendMessage", {"chat_id": channel, "text": body, "parse_mode": "HTML",
                              "disable_web_page_preview": "true"})


def pick_new(items, seen):
    """New items (not in seen), deduplicated, in feed order."""
    out, ids = [], set()
    for it in items:
        if it["id"] and it["id"] not in seen and it["id"] not in ids and it["title"]:
            ids.add(it["id"])
            out.append(it)
    return out


def run_once(api_key, token, channel):
    seen_list = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else []
    seen = set(seen_list)
    first_run = not seen
    items = []
    for url in FEEDS:
        try:
            parse = parse_fightnews if "fightnews.info" in url else parse_feed
            found = parse(fetch(url))
            if not found:
                print(f"feed empty {url}", file=sys.stderr)
            items += found[:10]  # feeds list newest first
        except Exception as e:
            print(f"feed failed {url}: {e}", file=sys.stderr)
    new = pick_new(items, seen)
    # On the very first run post just one, the rest of the backlog is just marked seen.
    to_post = new[:1] if first_run else new[:MAX_POSTS_PER_RUN]
    started = time.time()
    for it in to_post:
        if time.time() - started > RUN_BUDGET_MIN * 60:
            print("time budget used up, the rest waits for the next run", file=sys.stderr)
            break
        try:
            text = rewrite(api_key, it)
            if text:
                send_telegram(token, channel, text, it["link"], og_image(it["link"]))
                print("posted:", it["title"])
        except Exception as e:
            print(f"failed on {it['link']}: {e}", file=sys.stderr)
            continue  # stays unseen -> retried next run
        seen_list.append(it["id"])
        SEEN_FILE.write_text(json.dumps(seen_list[-5000:]))  # save now: a killed run must not repost
    for it in new[len(to_post):]:
        seen_list.append(it["id"])  # skip backlog beyond the per-run limit
    SEEN_FILE.write_text(json.dumps(seen_list[-5000:]))  # keep newest ids only


def main():
    load_env()
    for key in QUESTIONS:
        while not os.environ.get(key):
            ask(key)
    while True:
        try:
            tg(os.environ["TELEGRAM_BOT_TOKEN"], "getMe", {})
            break
        except Exception:
            ask("TELEGRAM_BOT_TOKEN", "Telegram не принял токен — скопируй его из @BotFather заново.")
    while True:
        try:
            tg(os.environ["TELEGRAM_BOT_TOKEN"], "getChat", {"chat_id": os.environ["TELEGRAM_CHANNEL"]})
            break
        except Exception:
            ask("TELEGRAM_CHANNEL", "Бот не видит канал — проверь @имя и что бот добавлен в канал администратором.")
    save_env()
    token, channel = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHANNEL"]
    api_key = os.environ["GEMINI_API_KEY"]
    print("Бот запущен, проверяю новости каждые", INTERVAL_MIN, "мин. Остановить: Ctrl+C")
    while True:
        run_once(api_key, token, channel)
        if "--once" in sys.argv:
            break
        time.sleep(INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
