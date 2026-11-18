"""会話の記録（jsonl）から、管理者のご発言と CL-2 の返事だけを、日本時間の週ごとの Markdown に書き出す。
作業の記録（命令とその結果）・仕組みの知らせ・要約は入れない。
選んで答えていただく伺い（AskUserQuestion）は、伺いの文と選択肢、選んだお返事（添えたお言葉も）を入れる（2026-11-13。CL-1 の連絡帳 4-145：
⛔ 前は、選んだお返事が道具の結果として届くため書き出しに入らず、ほかの担当がもとの伺いとお答えを確かめられなかった）。

前に書き出した分は消さない：このフォルダの控え `rows.jsonl`（1行に1つ：時刻・誰・発言）に残し、
いまの会話の記録と、同じフォルダのほかの会話の記録の分と合わせてから書き出す（同じものは1つ。何度流しても、前の分は消えない）。
⛔ 2026-11-03 までは、いまの会話の記録だけから週ごとのファイルを上書きしていた＝/clear で会話の記録が新しくなると、
前の会話の分が消える形だった（CL-1 が同じ誤りを直した記録＝CL-1 の conversation_log/README.md を読んで見つけた）。
控えが無いとき（初めの1回）は、いまある週ごとのファイルから控えを作る（それまでに書き出した分を残すため）。

使い方：python3 export_log.py <会話の記録の jsonl> <書き出すフォルダ>
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])
STORE = OUT / "rows.jsonl"
JST = timezone(timedelta(hours=9))
SECRET = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY")
WD = "月火水木金土日"


def when(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST).replace(microsecond=0)


def human_text(c) -> str:
    if isinstance(c, str):
        parts = [c]
    else:
        parts = [x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text"]
    keep = [p for p in parts if p.strip() and not p.lstrip().startswith("<")
            and not p.startswith("This session is being continued") and "Stop hook feedback" not in p]
    return "\n\n".join(keep).strip()


def ask_text(qs: list) -> str:
    """選んで答えていただく伺い（AskUserQuestion）の文と選択肢を、そのままの言葉で書く（CL-1 の連絡帳 4-145：選んだお返事も書き出しに入れる）。"""
    out = []
    for q in qs:
        out.append(f"伺い：{q.get('question', '')}")
        out += [f"- {o.get('label', '')}：{o.get('description', '')}" for o in q.get("options") or []]
    return "\n".join(out).strip()


def answer_text(qs: list, result, block: dict) -> str:
    """選んだお返事を、伺いの文と組で書く。答えの形が読めなければ、道具の返した文をそのまま書く（黙って捨てない）。"""
    answers = (result or {}).get("answers") if isinstance(result, dict) else None
    notes = (result or {}).get("annotations") if isinstance(result, dict) else None
    if isinstance(answers, dict) and answers:
        out = []
        for q, a in answers.items():
            out.append(f"伺い：{q}\nお返事：{a}")
            note = (notes or {}).get(q, {}).get("notes") if isinstance(notes, dict) else None
            if note:
                out.append(f"添えたお言葉：{note}")
        return "\n\n".join(out)
    c = block.get("content")
    return (c if isinstance(c, str) else "\n".join(y.get("text", "") for y in c or [] if isinstance(y, dict))).strip()


def from_session(path: Path):
    """1つの会話の記録から (時刻, 誰, 発言) を順に返す（どれをご発言とするかは、この1か所）。"""
    seen = set()
    asked: dict[str, list] = {}   # 選んで答えていただく伺い（AskUserQuestion）の番号 → 伺いの中身
    for n, line in enumerate(path.open(encoding="utf-8", errors="replace"), 1):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            # 書いている途中でディスクがいっぱいになると、記録の1行が途中で切れて次の行とつながる（2026-11-07 夜に起きた）。
            # 読めない行は飛ばし、飛ばしたことを必ず知らせる（黙って捨てない）
            print(f"⚠️ {path.name} の {n} 行目は壊れていて読めないので飛ばしました", file=sys.stderr)
            continue
        if d.get("isSidechain") or d.get("uuid") in seen:
            continue
        seen.add(d.get("uuid"))
        ts = d.get("timestamp")
        if not ts:
            continue
        # isMeta の付いた行（仕組みの知らせ・手引きの本文・分身の報告）は、ご発言ではない（CL-1 の連絡帳 4-68 の知らせ。⛔ 前は拾い、
        # 「作業の流れが立ち上げ直された」という仕組みの知らせを管理者のご発言として書いていた）
        if d.get("type") == "user" and not d.get("isMeta"):
            for x in d.get("message", {}).get("content") or []:
                if isinstance(x, dict) and x.get("type") == "tool_result" and x.get("tool_use_id") in asked:
                    yield when(ts), "管理者（選んだお返事）", answer_text(asked.pop(x["tool_use_id"]), d.get("toolUseResult"), x)
            t = human_text(d.get("message", {}).get("content"))
            if t:
                who = "ほかの担当のセッションから" if t.startswith("Another Claude session sent a message") else "管理者"
                yield when(ts), who, t
        elif d.get("type") == "attachment":
            a = d.get("attachment") or {}
            if a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind") == "human" and a.get("prompt"):
                yield when(ts), "管理者（CL-2 の作業中に）", a["prompt"].strip()
        elif d.get("type") == "assistant":
            content = d.get("message", {}).get("content") or []
            t = "\n\n".join(x.get("text", "") for x in content if x.get("type") == "text").strip()
            if t:
                yield when(ts), "CL-2", t
            for x in content:
                if x.get("type") == "tool_use" and x.get("name") == "AskUserQuestion":
                    qs = (x.get("input") or {}).get("questions") or []
                    asked[x.get("id")] = qs
                    yield when(ts), "CL-2（選んで答えていただく伺い）", ask_text(qs)


# 発言の終わりは、次の発言の頭か、次の日の見出しか、ファイルの終わり（発言の中の区切りの線「---」では切らない）
ENTRY = re.compile(r'<a id="d(\d{8})-\d+"></a>\*\*【(\d\d:\d\d:\d\d) (.+?)】\*\*\n\n(.*?)\n'
                   r'(?=\n---\n\n<a id="d\d{8}-\d+"></a>\*\*【|\n<a id="d\d{8}"></a>\n\n# |\Z)', re.S)


def from_weeks(folder: Path):
    """控えが無いときだけ：いまある週ごとのファイルから (時刻, 誰, 発言) を読む（このファイルが書いた形の逆）。"""
    for f in sorted(folder.glob("*_week.md")):
        for day, hms, who, text in ENTRY.findall(f.read_text(encoding="utf-8")):
            yield datetime.strptime(day + hms, "%Y%m%d%H:%M:%S").replace(tzinfo=JST), who, text.strip()


rows: dict[tuple, int] = {}   # (時刻, 誰, 発言) → 入った順（同じものは1つ）


def add(items):
    for t, who, text in items:
        rows.setdefault((t.isoformat(), who, text), len(rows))


if STORE.exists():
    add((datetime.fromisoformat(r["at"]), r["who"], r["text"]) for r in map(json.loads, STORE.open(encoding="utf-8")))
else:
    add(from_weeks(OUT))
before = len(rows)
for path in sorted({SRC.resolve(), *SRC.resolve().parent.glob("*.jsonl")}):   # いまの会話の記録と、同じフォルダのほかの会話の記録
    add(from_session(path))

ordered = sorted(rows, key=lambda k: (k[0], rows[k]))   # 時刻の順（同じ秒は入った順）
text_all = "".join(json.dumps({"at": a, "who": w, "text": x}, ensure_ascii=False) + "\n" for a, w, x in ordered)
found = [m.group(0)[:6] for m in SECRET.finditer(text_all)]
if found:   # キーらしい文字があれば、何も書かずに止める
    sys.exit(f"鍵らしい文字があるので書き出しませんでした：{found}")

days: dict = {}
for a, who, text in ordered:
    t = datetime.fromisoformat(a)
    days.setdefault(t.date(), []).append((t, who, text))
weeks: dict = {}
for day in sorted(days):
    weeks.setdefault(day - timedelta(days=day.weekday()), []).append(day)   # 月曜から日曜までで1つのファイル

OUT.mkdir(parents=True, exist_ok=True)
for monday, ds in sorted(weeks.items()):
    sunday = monday + timedelta(days=6)
    head = [f"# CL-2 SalesCall AI と管理者のやり取り（{monday:%Y-%m-%d}〜{sunday:%m-%d}・日本時間）\n\n",
            "ご発言と CL-2 の返事・選んで答えていただく伺いとそのお返事だけ（作業の記録・仕組みの知らせ・コンパクトの要約は入れていない）。書き出しは `export_log.py`。\n\n",
            "## 目次（日ごと。ご発言の時刻と書き出し）\n"]
    body = []
    for day in ds:
        items = days[day]
        did = f"d{day:%Y%m%d}"
        head.append(f"\n- [{day:%m-%d}（{WD[day.weekday()]}）](#{did})　ご発言 {sum(1 for _, w, _ in items if w.startswith('管理者'))}件\n")
        body.append(f'\n<a id="{did}"></a>\n\n# {day:%Y-%m-%d}（{WD[day.weekday()]}）\n')
        for n, (t, who, text) in enumerate(items):
            eid = f"{did}-{n}"
            if who.startswith("管理者"):
                first = " ".join(text.split())[:40]
                head.append(f"  - [{t:%H:%M} {first}](#{eid})\n")
            body.append(f'\n---\n\n<a id="{eid}"></a>**【{t:%H:%M:%S} {who}】**\n\n{text}\n')
    (OUT / f"{monday:%Y-%m-%d}_week.md").write_text("".join(head + ["\n"] + body), encoding="utf-8")
STORE.write_text(text_all, encoding="utf-8")
print("週", len(weeks), "日数", len(days), "件数", len(rows), "このたび足した件数", len(rows) - before)
