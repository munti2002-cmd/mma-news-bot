"""Run: python3 test_live.py  (offline, nothing is posted)"""
import copy
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import live

live.STATE_FILE = Path(tempfile.mkdtemp()) / "live.json"
posted = []
live.post = lambda tok, ch, text: posted.append(text)
live.ru_names = lambda key, names: {"A One": "Эй Уан", "B Two": "Би Ту", "C Three": "Си Три", "D Four": "Ди Фор"}


def fight(cid, date, a, b):
    return {"id": cid, "date": date, "type": {"abbreviation": "Lightweight"}, "details": [],
            "status": {"period": 0, "displayClock": "-", "type": {"state": "pre", "name": "STATUS_SCHEDULED"}},
            "competitors": [{"order": 1, "winner": False, "athlete": {"displayName": a}},
                            {"order": 2, "winner": False, "athlete": {"displayName": b}}]}


ev = {"id": "e1", "name": "UFC Test", "competitions": [fight("2", "2026-11-21T18:00Z", "C Three", "D Four"),
                                                      fight("1", "2026-11-21T15:00Z", "A One", "B Two")]}
assert live.is_on(ev, datetime(2026, 11, 21, 14, 40, tzinfo=timezone.utc))
assert not live.is_on(ev, datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc))


def poll():
    return live.step(copy.deepcopy(ev), live.load_state("e1"), "k", "t", "c")


assert not poll() and posted == []                       # nothing started yet
f1 = ev["competitions"][1]
f1["status"]["type"]["state"] = "in"
poll(); poll()                                           # start posted once
assert len(posted) == 2 and "Онлайн" in posted[0] and posted[1] == "⏰ Начинается бой: Эй Уан vs Би Ту\nЛёгкий вес", posted
f1["status"].update(period=2, displayClock="3:14", type={"state": "post", "name": "STATUS_FINAL"})
f1["competitors"][0]["winner"] = True
f1["details"] = [{"type": {"text": "Unofficial Winner Kotko"}}]
poll()
assert posted[-1] == "🏆 Побеждает Эй Уан!\nБи Ту проигрывает: нокаут, 2-й раунд, 3:14", posted[-1]
f2 = ev["competitions"][0]                              # main event jumps straight to final
f2["status"].update(period=3, displayClock="5:00", type={"state": "post", "name": "STATUS_FINAL"})
f2["competitors"][1]["winner"] = True
f2["details"] = [{"type": {"text": "Unofficial Winner Decision"}}]
assert poll()
assert posted[-2] == "🏆 Побеждает Ди Фор!\nСи Три проигрывает: решением судей", posted[-2]
assert posted[-1] == ("📋 Итоги турнира UFC Test:\n• Ди Фор победил(а) Си Три — решением судей\n"
                      "• Эй Уан победил(а) Би Ту — нокаут, 2-й раунд, 3:14"), posted[-1]
n = len(posted)
poll()
assert len(posted) == n                                  # nothing reposted
assert json.loads(live.STATE_FILE.read_text())["summary"]

# live data from ESPN still has the fields we use
ev = json.loads(live.bot.fetch(live.SCOREBOARD))["events"][0]
names = {x["athlete"]["displayName"]: x["athlete"]["displayName"] for c in ev["competitions"] for x in c["competitors"]}
for c in ev["competitions"]:
    live.fight_info(c, names)
print("ok", ev["name"], len(ev["competitions"]), "fights")
print("all checks passed")
