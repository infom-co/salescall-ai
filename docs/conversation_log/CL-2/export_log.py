"""会話の記録（このセッションの jsonl）から、管理者のご発言と CL-2 の返事だけを、日本時間の日ごとの Markdown に書き出す。
作業の記録（命令とその結果）・仕組みの知らせ・要約は入れない。何度流しても同じものができる（上書き）。"""
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])
JST = timezone(timedelta(hours=9))
SECRET = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY")


def when(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST)


def human_text(c) -> str:
    if isinstance(c, str):
        parts = [c]
    else:
        parts = [x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text"]
    keep = [p for p in parts if p.strip() and not p.lstrip().startswith("<")
            and not p.startswith("This session is being continued") and "Stop hook feedback" not in p]
    return "\n\n".join(keep).strip()


days = defaultdict(list)
seen = set()
for line in SRC.open(encoding="utf-8"):
    d = json.loads(line)
    if d.get("isSidechain") or d.get("uuid") in seen:
        continue
    seen.add(d.get("uuid"))
    ts = d.get("timestamp")
    if not ts:
        continue
    if d.get("type") == "user":
        t = human_text(d.get("message", {}).get("content"))
        if t:
            who = "ほかの担当のセッションから" if t.startswith("Another Claude session sent a message") else "管理者"
            days[when(ts).date()].append((when(ts), who, t))
    elif d.get("type") == "attachment":
        a = d.get("attachment") or {}
        if a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind") == "human" and a.get("prompt"):
            days[when(ts).date()].append((when(ts), "管理者（CL-2 の作業中に）", a["prompt"].strip()))
    elif d.get("type") == "assistant":
        t = "\n\n".join(x.get("text", "") for x in d.get("message", {}).get("content") or [] if x.get("type") == "text").strip()
        if t:
            days[when(ts).date()].append((when(ts), "CL-2", t))

OUT.mkdir(parents=True, exist_ok=True)
WD = "月火水木金土日"
weeks = defaultdict(list)
for day in sorted(days):
    weeks[day - timedelta(days=day.weekday())].append(day)   # 月曜から日曜までで1つのファイル
found = []
for monday, ds in sorted(weeks.items()):
    sunday = monday + timedelta(days=6)
    head = [f"# CL-2 SalesCall AI と管理者のやり取り（{monday:%Y-%m-%d}〜{sunday:%m-%d}・日本時間）\n\n",
            "ご発言と CL-2 の返事だけ（作業の記録・仕組みの知らせ・コンパクトの要約は入れていない）。書き出しは `export_log.py`。\n\n",
            "## 目次（日ごと。ご発言の時刻と書き出し）\n"]
    body = []
    for day in ds:
        items = sorted(days[day], key=lambda x: x[0])
        did = f"d{day:%Y%m%d}"
        head.append(f"\n- [{day:%m-%d}（{WD[day.weekday()]}）](#{did})　ご発言 {sum(1 for _, w, _ in items if w.startswith('管理者'))}件\n")
        body.append(f'\n<a id="{did}"></a>\n\n# {day:%Y-%m-%d}（{WD[day.weekday()]}）\n')
        for n, (t, who, text) in enumerate(items):
            eid = f"{did}-{n}"
            if who.startswith("管理者"):
                first = " ".join(text.split())[:40]
                head.append(f"  - [{t:%H:%M} {first}](#{eid})\n")
            body.append(f'\n---\n\n<a id="{eid}"></a>**【{t:%H:%M:%S} {who}】**\n\n{text}\n')
    s2 = "".join(head + ["\n"] + body)
    found += [(str(monday), m.group(0)[:6]) for m in SECRET.finditer(s2)]
    (OUT / f"{monday:%Y-%m-%d}_week.md").write_text(s2, encoding="utf-8")
print("週", len(weeks), "日数", len(days), "件数", sum(len(v) for v in days.values()), "鍵らしい文字", found)
