"""Text live coverage of a UFC event: fight start, result, summary -> Telegram channel.

Run: python3 live.py  (exits at once when no event is on; started every 15 min by the workflow)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bot

STATE_FILE = Path(__file__).parent / "live.json"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
POLL_SEC = 60
RUN_LIMIT_MIN = 330        # stay under the 6 h GitHub job limit, the next run takes over
START_EARLY_MIN = 30       # start watching this long before the first fight
METHODS = {"decision": "решение судей", "submission": "сдача", "kotko": "нокаут", "dq": "дисквалификация"}
WEIGHTS = {
    "Strawweight": "минимальный вес", "Flyweight": "наилегчайший вес", "Bantamweight": "легчайший вес",
    "Featherweight": "полулёгкий вес", "Lightweight": "лёгкий вес", "Welterweight": "полусредний вес",
    "Middleweight": "средний вес", "Light Heavyweight": "полутяжёлый вес", "Heavyweight": "тяжёлый вес",
}


def load_state(event_id):
    s = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if s.get("event") != event_id:  # new event -> fresh state
        s = {"event": event_id, "started": [], "done": [], "results": [], "summary": False, "names": {}}
    return s


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1))


def ru_names(api_key, names):
    """English -> Russian transliteration via Gemini, English names if it fails."""
    try:
        text = bot.gemini(api_key, "Напиши имена бойцов MMA в принятой русской транслитерации. "
                                   "Ответь только JSON-объектом {\"английское имя\": \"русское имя\"}.\n"
                          + json.dumps(names, ensure_ascii=False))
        m = json.loads(re.search(r"\{.*\}", text, re.S).group())
        return {n: m.get(n) or n for n in names}
    except Exception as e:
        print(f"name translation failed, using English: {e}", file=sys.stderr)
        return {n: n for n in names}


def fight_info(c, names):
    fighters = sorted(c["competitors"], key=lambda x: x.get("order", 0))
    ru = [names.get(x["athlete"]["displayName"], x["athlete"]["displayName"]) for x in fighters]
    winner = next((names.get(x["athlete"]["displayName"], x["athlete"]["displayName"])
                   for x in fighters if x.get("winner")), None)
    method = next((METHODS.get(d["type"]["text"].split()[-1].lower())
                   for d in c.get("details", []) if d["type"]["text"].startswith("Unofficial Winner")), None)
    st = c["status"]
    return {"a": ru[0], "b": ru[1], "weight": WEIGHTS.get(c["type"].get("abbreviation"), ""),
            "winner": winner, "loser": next((n for n in ru if n != winner), None) if winner else None,
            "method": method, "round": st.get("period"), "clock": st.get("displayClock")}


def start_text(f):
    return f"⏰ Начинается бой: {f['a']} vs {f['b']}" + (f"\n{f['weight'].capitalize()}" if f["weight"] else "")


def how(f):
    if f["method"] == "решение судей" or (not f["method"] and f["clock"] == "5:00"):
        return "решением судей"
    return ", ".join(x for x in (f["method"], f"{f['round']}-й раунд" if f["round"] else "", f["clock"]) if x)


def result_text(f):
    if not f["winner"]:
        return f"🤝 Бой {f['a']} vs {f['b']} завершился без победителя (ничья или no contest)"
    return f"🏆 Побеждает {f['winner']}!\n{f['loser']} проигрывает: {how(f)}"


def summary_text(event_name, results):
    lines = [f"📋 Итоги турнира {event_name}:"]
    for f in reversed(results):  # main event first
        lines.append(f"• {f['winner']} победил(а) {f['loser']} — {how(f)}" if f["winner"]
                     else f"• {f['a']} vs {f['b']} — без победителя")
    return "\n".join(lines)


def post(token, channel, text):
    bot.tg(token, "sendMessage", {"chat_id": channel, "text": text})
    print("posted:", text.splitlines()[0])


def step(event, s, api_key, token, channel):
    """Post whatever changed since last poll. Returns True when the event is over and summarised."""
    comps = sorted(event["competitions"], key=lambda c: c["date"])  # prelims first, main event last
    if not s["names"]:
        s["names"] = ru_names(api_key, sorted({x["athlete"]["displayName"]
                                               for c in comps for x in c["competitors"]}))
        save_state(s)
    for c in comps:
        state, cid = c["status"]["type"]["state"], c["id"]
        f = fight_info(c, s["names"])
        if state == "in" and cid not in s["started"]:
            if not s["started"]:
                post(token, channel, f"🔴 Онлайн-трансляция: {event['name']}\nСледите за результатами здесь!")
            post(token, channel, start_text(f))
            s["started"].append(cid)
            save_state(s)
        if state == "post" and cid not in s["done"]:
            if c["status"]["type"]["name"] not in ("STATUS_CANCELED", "STATUS_POSTPONED"):
                post(token, channel, result_text(f))
                s["results"].append(f)
            s["started"].append(cid)
            s["done"].append(cid)
            save_state(s)
    if all(c["status"]["type"]["state"] == "post" for c in comps) and not s["summary"]:
        if s["results"]:
            post(token, channel, summary_text(event["name"], s["results"]))
        s["summary"] = True
        save_state(s)
    return s["summary"]


def is_on(event, now):
    first = min(datetime.fromisoformat(c["date"].replace("Z", "+00:00")) for c in event["competitions"])
    return first - timedelta(minutes=START_EARLY_MIN) <= now <= first + timedelta(hours=12)


def main():
    bot.load_env()
    api_key, token, channel = (os.environ[k] for k in ("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL"))
    started = time.time()
    while time.time() - started < RUN_LIMIT_MIN * 60:
        try:
            events = json.loads(bot.fetch(SCOREBOARD)).get("events", [])
        except Exception as e:
            print(f"scoreboard failed: {e}", file=sys.stderr)
            if time.time() - started < POLL_SEC:
                return  # can't tell if an event is on, the next run tries again
            events = None
        if events is not None:
            event = next((e for e in events if is_on(e, datetime.now(timezone.utc))), None)
            if not event:
                print("no event on now, next:", events[0]["name"] if events else "-")
                return
            s = load_state(event["id"])
            if s["summary"]:
                print("event already covered")
                return
            try:
                if step(event, s, api_key, token, channel):
                    return
            except Exception as e:  # one bad poll must not end the coverage
                print(f"poll failed: {e}", file=sys.stderr)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
