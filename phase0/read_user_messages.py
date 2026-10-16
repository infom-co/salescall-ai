"""コンパクト（会話の要約）のあとに、会話の記録からユーザーの発言だけを取り出して読み直すための道具（docs/team/CL-1.md 0-1）。

✅ 2026-10-15 ユーザー：「コンパクト後、これまでのやり取りをしっかりと確認しましたか？同じことを繰り返していますよ」
⛔ 要約だけを頼りにして、前にいただいた注意（締めの言葉の「、」のような区切り・「お忙しいところ」の崩れ・「明るく」「はきはき」を使わない・
   音の変換は間が空く）を繰り返した。⇒ コンパクトのあとは、この道具でユーザーの発言をすべて読み直してから作業に戻る。

使い方：python read_user_messages.py <会話の記録 .jsonl> [--since 2026-10-12T14:00] [--out ファイル]
- 途中で届いた発言（作業中に送られたもの）も拾う。同じ発言が2回記録されていれば1回にする。
- キーなどの秘密は会話の記録に書かない決まりなので、読み直しても外へは出ない（出力は作業場所に置き、リポジトリには入れない）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SKIP = ("<system-reminder>", "<task-notification>", "This session is being continued", "[SYSTEM NOTIFICATION", "Caveat:",
        "<command-", "<local-command")


def user_messages(path: Path, since: str = "") -> list[tuple[str, str]]:
    """[(時刻, 発言)]。古い順。"""
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = d.get("timestamp", "")
        if ts < since:
            continue
        texts = []
        if d.get("type") == "user":
            c = d.get("message", {}).get("content")
            texts = [c] if isinstance(c, str) else [x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text"]
        elif d.get("type") == "queue-operation" and isinstance(d.get("content"), str):  # 作業中に届いた発言
            texts = [d["content"]]
        for t in texts:
            s = t.strip()
            if not s or s.startswith(SKIP) or s[:200] in seen:
                continue
            seen.add(s[:200])
            out.append((ts[:16], s))
    return sorted(out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("transcript", type=Path)
    p.add_argument("--since", default="")
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    text = "\n\n------\n".join(f"[{ts}]\n{s}" for ts, s in user_messages(args.transcript, args.since))
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"{args.out} に書きました（{len(text)} 字）")
    else:
        print(text)


if __name__ == "__main__":
    main()
