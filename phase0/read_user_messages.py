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
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SKIP = ("<system-reminder>", "<task-notification>", "This session is being continued", "[SYSTEM NOTIFICATION", "Caveat:",
        "<command-", "<local-command")


def user_messages(path: Path, since: str = "", until: str = "", with_assistant: bool = False) -> list[tuple[str, str, str]]:
    """[(時刻, 誰, 発言)]。古い順。誰＝「ユーザー」か「CL-1」。"""
    out, last_seen = [], {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = d.get("timestamp", "")
        if ts < since or (until and ts > until):
            continue
        texts, who = [], "ユーザー"
        if d.get("type") == "user":
            c = d.get("message", {}).get("content")
            texts = [c] if isinstance(c, str) else [x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text"]
        elif d.get("type") == "queue-operation" and isinstance(d.get("content"), str):  # 作業中に届いた発言
            texts = [d["content"]]
        elif with_assistant and d.get("type") == "assistant":
            c = d.get("message", {}).get("content") or []
            texts, who = [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"], "CL-1"
        for t in texts:
            s = t.strip()
            if not s or (who == "ユーザー" and s.startswith(SKIP)):
                continue
            key = (who, s[:200])
            if key in last_seen and _minutes(last_seen[key], ts) < 10:  # 同じ発言が続けて2回記録されたもの
                continue
            last_seen[key] = ts
            out.append((ts[:16], who, s))
    return sorted(out)


_EN_WORDS = re.compile(r"\b(?:the|a|an|to|of|and|for|is|are|now|next|with|so|it|that|this|on|in|then|i'll|i|we|from|by)\b", re.I)


def english(text: str) -> bool:
    """英語で書いた一言か：英字が、かな・漢字の半分より多く、英語の文のつなぎの言葉（the・to・now など）が2つ以上ある
    （コードの名前を並べただけの日本語の文は数えない。` ` の中も数えない）。"""
    t = re.sub(r"`[^`]*`", "", text)
    jp = len(re.findall(r"[ぁ-んァ-ヶ一-龥]", t))
    en = len(re.findall(r"[A-Za-z]", t))
    return en > 15 and en > jp * 0.5 and len(_EN_WORDS.findall(t)) >= 2


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
    args = p.parse_args()
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
