"""コンパクト（会話の要約）のあとに、会話の記録からユーザーの発言だけを取り出して読み直すための道具（docs/team/CL-1.md 0-1）。

✅ 2026-10-15 ユーザー：「コンパクト後、これまでのやり取りをしっかりと確認しましたか？同じことを繰り返していますよ」
⛔ 要約だけを頼りにして、前にいただいた注意（締めの言葉の「、」のような区切り・「お忙しいところ」の崩れ・「明るく」「はきはき」を使わない・
   音の変換は間が空く）を繰り返した。⇒ コンパクトのあとは、この道具でユーザーの発言をすべて読み直してから作業に戻る。

使い方：python read_user_messages.py <会話の記録 .jsonl> [--since 2026-10-12T14:00] [--until …] [--with-assistant] [--english] [--out ファイル]
- 途中で届いた発言（作業中に送られたもの）も拾う。同じ発言が続けて2回記録されたもの（10分以内）だけを1回にする。
  ⛔ 2026-10-16 の失敗：同じ文の発言を、時刻が離れていても1回にしていた ⇒ ユーザーが同じ注意をくり返した発言
  （10-12 と 10-16 の「コンパクト前のやり取りをきっちりと確認して、記憶を取り戻して」）が、読み直しから消えていた。
- --with-assistant：こちらの返事（文字の部分だけ）も、時刻の順に混ぜて出す（コンパクト前に何を言ったか・何を約束したかを確かめる）。
- --english：こちらの返事のうち、英語で書いた一言だけを出す（日本語で書く決まり＝CL-1.md 0-2 の書き方。読み直しで数える）。
  ⛔ 2026-10-24 の失敗（7回目）：コンパクトの前後で、作業の途中の一言を56回英語で書いた（読み直したあとのターンにも2回）。
- キーなどの秘密は会話の記録に書かない決まりなので、読み直しても外へは出ない（出力は作業場所に置き、リポジトリには入れない）。
  ただし --export（下）は、管理者のご指示でリポジトリに置くもの＝書き出す前にキーらしい文字を探し、あれば何も書かずに止める。
- --export <フォルダ>：管理者のご発言と CL-1 の返事の全文を、日本時間の週（月曜〜日曜）ごとの Markdown に書き出す（頭に日ごとの目次）。
  前に書き出した分は、そのフォルダの控え（rows.jsonl）から読み、いまの会話の記録の分と合わせてから書き出す（何度流しても、前の分は消えない）。
  いまの会話の記録と同じフォルダにある、ほかの会話の記録（/clear の前の会話など）も読む。⛔ 2026-11-03：週ごとのファイルを、いまの会話の記録
  だけから上書きしていた＝/clear で会話の記録が新しくなると、前の会話の分が消える形だった（管理者が /clear を伺ったときに見つけた）。✅ 2026-11-01 管理者（CL-2 の連絡帳 4-181 のとおり）：「CL-1とCL-3用にもあなたが同様の
  フォルダを作って、そこに同様に履歴を今後、完全保存していくように私から指示があったことを彼らに伝えてください。」⇒ docs/conversation_log/CL-1/。
  読み直しと同じ読み方（`_rows` の1か所）で書き出す＝読み直しで読めるご発言と、残したご発言が食い違わない。

どれをご発言とするか（`_rows` の1か所。2026-11-02 に会話の記録の全部で数えて決めた）：
- ふつうの発言のうち、中の文の印（`isMeta`）の付いたもの（手引きの本文・画面の命令の注意・サブエージェントの報告・分類器の知らせ）は外す。
- 作業中に届いた発言は、人が送った `queued_command`（`origin.kind`＝human）から拾う。⛔ 2026-11-02 まで `queue-operation` の行から拾っていて、
  届くのが10分以上遅れたご発言（作業が終わるまで待って届いたもの）を2回出していた（11-02 の 7時22分と 7時44分）。`queue-operation` に
  しか無いのは `/context`（画面の命令）とサブエージェントの報告だけだった（10-12〜11-02 の全部で19件）。
- サブエージェント（`isSidechain`）の行は外す。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKIP = ("<system-reminder>", "<task-notification>", "This session is being continued", "[SYSTEM NOTIFICATION", "Caveat:",
        "<command-", "<local-command")
USER, USER_MIDTURN, ME = "ユーザー", "ユーザー（作業中に）", "CL-1"
JST = timezone(timedelta(hours=9))
SECRET = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY")   # キーらしい文字（書き出しの前に探す）


def _texts(c) -> list[str]:
    return [c] if isinstance(c, str) else [x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text"]


def _rows(path: Path, since: str = "", until: str = "", with_assistant: bool = False) -> list[tuple[str, str, str]]:
    """[(時刻（世界標準時・秒まで）, 誰, 発言)]。古い順。誰＝USER・USER_MIDTURN・ME。ご発言の決め方の唯一の場所（上の注）。"""
    out, last_seen = [], {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = d.get("timestamp", "")
            if not ts or ts < since or (until and ts > until) or d.get("isSidechain"):
                continue
            texts, who = [], USER
            if d.get("type") == "user" and not d.get("isMeta"):
                texts = _texts(d.get("message", {}).get("content"))
            elif d.get("type") == "attachment":
                a = d.get("attachment") or {}
                if a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind") == "human":   # 作業中に届いた発言
                    texts, who = _texts(a.get("prompt")), USER_MIDTURN
            elif with_assistant and d.get("type") == "assistant" and not d.get("isApiErrorMessage"):   # 仕組みの知らせ（週の上限など）は CL-1 の返事ではない
                texts, who = _texts(d.get("message", {}).get("content") or []), ME
            for t in texts:
                s = t.strip()
                if not s or (who != ME and s.startswith(SKIP)):
                    continue
                key = (who == ME, s[:200])
                if key in last_seen and _minutes(last_seen[key], ts) < 10:  # 同じ発言が続けて2回記録されたもの
                    continue
                last_seen[key] = ts
                out.append((ts, who, s))
    return sorted(out, key=lambda r: r[0])


def user_messages(path: Path, since: str = "", until: str = "", with_assistant: bool = False) -> list[tuple[str, str, str]]:
    """[(時刻（世界標準時・分まで）, 誰, 発言)]。古い順。誰＝「ユーザー」「ユーザー（作業中に）」か「CL-1」。"""
    return [(ts[:16], who, s) for ts, who, s in _rows(path, since, until, with_assistant)]


ARCHIVE = "rows.jsonl"   # 書き出したやり取りの控え（書き出しのフォルダの中。1行に1つ {ts, who, text}）


def _write_whole(path: Path, text: str) -> None:
    """ファイルを丸ごと書き直す唯一の書き方（export_weeks の控えと週ごとのファイル）：同じフォルダの仮のファイルに書いてから置き換える（os.replace）。
    途中で止まっても、前のファイルは前のまま（仮のファイルは残さない）。⛔ 2026-11-03 の見直しで見つけた：控え rows.jsonl を直接書き直していた＝
    書いている途中で止まると控えが途中までになり、次の書き出しで、もう会話の記録に無い行（/clear の前の会話の分）が消える形だった"""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(name)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def export_weeks(path: Path, out_dir: Path, siblings: bool = False) -> dict:
    """管理者のご発言と CL-1 の返事の全文を、日本時間の週ごとの Markdown に書き出す。前に書き出した分（控え ARCHIVE）と、いまの会話の記録 path の分
    （siblings なら、同じフォルダのほかの会話の記録の分も）を合わせ、同じもの（時刻・誰・発言が同じ）は1つにする。キーらしい文字があれば何も書かずに止める。"""
    label = {USER: "管理者", USER_MIDTURN: "管理者（CL-1 の作業中に）", ME: "CL-1"}
    arch = out_dir / ARCHIVE
    kept = [(d["ts"], d["who"], d["text"]) for d in map(json.loads, arch.read_text(encoding="utf-8").splitlines() if arch.is_file() else []) if d]
    logs = sorted({path, *(path.parent.glob("*.jsonl") if siblings else ())})
    rows = sorted(set(kept) | {r for p in logs for r in _rows(p, with_assistant=True)}, key=lambda r: (r[0], r[1] != ME))
    days = defaultdict(list)
    for ts, who, s in rows:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST)
        days[t.date()].append((t, label[who], s))
    weeks = defaultdict(list)
    for day in sorted(days):
        weeks[day - timedelta(days=day.weekday())].append(day)
    wd, files = "月火水木金土日", {}
    for monday, ds in sorted(weeks.items()):
        head = [f"# CL-1 SalesCall AI（会話の担当）と管理者のやり取り（{monday:%Y-%m-%d}〜{monday + timedelta(days=6):%m-%d}・日本時間）\n\n",
                "管理者のご発言（CL-1 の作業中に届いたものも）と CL-1 の返事の全文。作業の記録（命令とその結果）・仕組みの知らせ・要約・"
                "サブエージェントの報告は入れていない。書き出しは `phase0/read_user_messages.py --export`（README.md）。\n\n",
                "## 目次（日ごと。ご発言の時刻と書き出し）\n"]
        body = []
        for day in ds:
            items = days[day]
            did = f"d{day:%Y%m%d}"
            head.append(f"\n- [{day:%m-%d}（{wd[day.weekday()]}）](#{did})　ご発言 {sum(w != 'CL-1' for _, w, _ in items)}件\n")
            body.append(f'\n<a id="{did}"></a>\n\n# {day:%Y-%m-%d}（{wd[day.weekday()]}）\n')
            for n, (t, who, text) in enumerate(items):
                eid = f"{did}-{n}"
                if who != "CL-1":
                    head.append(f"  - [{t:%H:%M} {' '.join(text.split())[:40]}](#{eid})\n")
                body.append(f'\n---\n\n<a id="{eid}"></a>**【{t:%H:%M:%S} {who}】**\n\n{text}\n')
        files[f"{monday:%Y-%m-%d}_week.md"] = "".join(head + ["\n"] + body)
    found = [(name, m.group(0)[:6]) for name, s in files.items() for m in SECRET.finditer(s)]
    if found:
        raise SystemExit(f"キーらしい文字があるので書き出しません（ファイルと頭の6字）：{found}")
    out_dir.mkdir(parents=True, exist_ok=True)
    # 控え（正本）を先に、そのあと週ごとのファイル（控えから作り直せる）。どれも置き換える形（_write_whole）
    _write_whole(arch, "".join(json.dumps({"ts": ts, "who": who, "text": s}, ensure_ascii=False) + "\n" for ts, who, s in rows))
    for name, s in files.items():
        _write_whole(out_dir / name, s)
    return {"週": len(weeks), "日": len(days), "件": sum(map(len, days.values())),
            "ご発言": sum(w != "CL-1" for v in days.values() for _, w, _ in v)}


_EN_WORDS = re.compile(r"\b(?:the|a|an|to|of|and|for|is|are|now|next|with|so|it|that|this|on|in|then|i'll|i|we|from|by)\b", re.I)


# 英語のつなぎの言葉で書き始めた一言（「Next に…」「Now the …」）。名前で始まる一言（「CL-2 の…」「I-30 の…」「G-59 の…」）は数えない＝
# 1文字の言葉（a・i）は入れず、言葉のあとが空白・区切りのときだけ
_EN_HEAD = re.compile(r"^\s*(?:the|an|to|of|and|for|is|are|now|next|with|so|it|that|this|on|in|then|i'll|we|from|by)(?=[\s,.:;—–!?])", re.I)


def english(text: str) -> bool:
    """英語で書いた一言か：英字が、かな・漢字の半分より多く、英語の文のつなぎの言葉（the・to・now など）が2つ以上ある・かな・漢字が無く英語の言葉が並ぶ
    （コードの名前を並べただけの日本語の文は数えない。` ` の中と「」の中も数えない）。または、英語のつなぎの言葉で書き始めた（_EN_HEAD）。
    ⛔ 2026-11-02 日本時間21時59分・22時00分（CL-1.md 0-1 の48回目・49回目）：頭の言葉だけ英語の一言（「Next に進む前に、…」）を、
       英字の割合だけで見ていたので、見張りも数える道具も英語と見なかった。"""
    # ` ` の中と「」の中（引用した言葉）は数えない（⛔ 2026-11-05 日本時間6時57分：英語の一言を「」で引用して記録する日本語の一言を、見張りが英語と見て止めた）
    t = re.sub(r"`[^`]*`|「[^「」]*」", "", text)
    jp = len(re.findall(r"[ぁ-んァ-ヶ一-龥]", t))
    en = len(re.findall(r"[A-Za-z]", t))
    # かな・漢字が1つも無く、英語の言葉（3字以上）のあとに続く言葉がある一言も英語（⛔ 2026-11-06 日本時間13時55分・13時58分の143回目〜145回目：
    # 「Implement B10 and B4.」は英字15字以下・つなぎの言葉1つで、見張りも数える道具も英語と見なかった）。英字の名前だけ（「B8.」「OK」）は数えない
    return (bool(_EN_HEAD.match(t)) or (en > 15 and en > jp * 0.5 and len(_EN_WORDS.findall(t)) >= 2)
            or (jp == 0 and bool(re.search(r"[A-Za-z]{3,}\s+\S*[A-Za-z]", t))))


def _minutes(a: str, b: str) -> float:
    import datetime as dt
    f = lambda x: dt.datetime.fromisoformat(x.replace("Z", "+00:00"))  # noqa: E731
    try:
        return abs((f(b) - f(a)).total_seconds()) / 60
    except ValueError:
        return 0.0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("transcript", type=Path)
    p.add_argument("--since", default="")
    p.add_argument("--until", default="")
    p.add_argument("--with-assistant", action="store_true")
    p.add_argument("--english", action="store_true", help="こちらの返事のうち、英語で書いた一言だけを出す")
    p.add_argument("--out", type=Path)
    p.add_argument("--export", type=Path, help="管理者のご発言と CL-1 の返事の全文を、週ごとの Markdown でこのフォルダに書き出す")
    args = p.parse_args()
    if args.export:
        print(f"{args.export} に書きました：{export_weeks(args.transcript, args.export, siblings=True)}")
        return
    rows = user_messages(args.transcript, args.since, args.until, args.with_assistant or args.english)
    if args.english:
        rows = [r for r in rows if r[1] == "CL-1" and english(r[2])]
    text = "\n\n------\n".join(f"[{ts}] {who}\n{s}" for ts, who, s in rows)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"{args.out} に書きました（{len(text)} 字・{len(rows)} 件）")
    else:
        print(text)


if __name__ == "__main__":
    main()
