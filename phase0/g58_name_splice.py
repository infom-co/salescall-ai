"""G-58：目的の人を呼ぶ一言を、部署・役職の録音と名前の文の録音の継ぎ合わせにしても、自然に聞こえるか（DESIGN.md 4-3 の名前の録音の台帳）。

✅ 2026-10-19 ユーザー：「代表的な名前を事前にシステム単位で用意しておき、それにない物だけ、録音すればいいんじゃないですか？
   人は聞く形にしないと今の判定では些細な違いは間違うでしょ？」⇒ 名前の文（「「おかだ」様はいらっしゃいますでしょうか？」）は台帳で使い回せるが、
   部署・役職の付く呼び方（「営業部部長の「おかだ」様は…」）は依頼主ごとに文が変わる。継ぎ合わせ（部署・役職の録音＋名前の文の録音）で
   自然に聞こえるなら、部署・役職の付く呼び方も台帳で使い回せる。⇒ 文まるごとの録音と、継ぎ合わせを、番号を伏せて並べ、人が聞いて比べる。
作るもの：results/name_splice/ の 01〜（どれが継ぎ合わせかは index.json。聞く前に開かない）。録り方と確かめは make_ack_clips.take と同じ。
"""
from __future__ import annotations

import asyncio
import json
import random

from barge import voice_runs, voiced
from common import HERE, base_parser, make_client, pick_key, require_python, save_result, say, write_wav
from make_ack_clips import take

OUT = HERE / "results" / "name_splice"
ROLE = "営業部部長の"
NAMES = ("「おかだ」", "「なかじま」")
# 継ぎ目に足す無音（声の前後の無音は切ってから継ぐ）。2026-10-19 に測った：文まるごとの録音でも、声は「営業部部長の｜おかだ様は」の所で
# 0.12〜0.26秒の間を置く（12本中11本。かぎかっこの有無によらない＝results/G-58-quote.json）⇒ 継ぎ目にも同じくらいの間を置く
JOIN_S = 0.15


def trim(pcm: bytes, rate: int, keep_s: float = 0.03) -> bytes:
    """声の前後の無音を切る（継ぎ目に余分な間を入れないため）。"""
    runs = voice_runs(voiced(pcm, rate), 0.05)
    if not runs:
        return pcm
    a = max(0, int((runs[0][0] - keep_s) * rate)) * 2
    b = min(len(pcm), int((runs[-1][1] + keep_s) * rate) * 2)
    return pcm[a:b]


async def good_take(client, args, vt: str, aid: str, text: str) -> tuple[bytes, int, dict]:
    """読みが合う録音（make_ack_clips の確かめのうち、文の途中の間の確かめだけは外す：上の JOIN_S のとおり、文まるごとでも間が入るので、
    間の自然さは人が聞いて比べる）。"""
    from make_ack_clips import ends_in_silence, heard_ok
    for _ in range(args.tries):
        row, pcm, rate = await take(client, args, vt, aid, text, "question")
        if pcm and heard_ok(text, row.get("heard") or {}) and ends_in_silence(pcm, rate):
            return pcm, rate, row
        await asyncio.sleep(args.pause)
    raise RuntimeError(f"確かめに通る録音が作れなかった：{text}")


async def run(args) -> dict:
    client = make_client(pick_key(args))
    OUT.mkdir(parents=True, exist_ok=True)
    items = []
    for name in NAMES:
        whole = f"{ROLE}{name}様はいらっしゃいますでしょうか？"
        pcm, rate, row = await good_take(client, args, args.type, "whole", whole)
        items.append({"kind": "まるごと", "text": whole, "pcm": trim(pcm, rate), "rate": rate, "rows": [row]})
        rp, rr, row_r = await good_take(client, args, args.type, "role", ROLE)
        np_, nr, row_n = await good_take(client, args, args.type, "name", f"{name}様はいらっしゃいますでしょうか？")
        assert rr == nr, (rr, nr)
        joined = trim(rp, rr) + bytes(int(JOIN_S * rr) * 2) + trim(np_, nr)
        items.append({"kind": "継ぎ合わせ", "text": f"{ROLE}｜{name}様はいらっしゃいますでしょうか？", "pcm": joined, "rate": rr,
                      "rows": [row_r, row_n]})
    random.Random(args.seed).shuffle(items)
    index = []
    for k, it in enumerate(items, 1):
        name = f"{k:02d}.wav"
        write_wav(OUT / name, it["pcm"], it["rate"])
        index.append({"wav": name, "kind": it["kind"], "text": it["text"], "takes": it["rows"]})
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    say("作った：", ", ".join(f"{x['wav']}" for x in index), "（どれが継ぎ合わせかは index.json）")
    return {"model": args.model, "type": args.type, "items": [{k: v for k, v in x.items() if k != "takes"} for x in index], "takes": index}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--type", default="S2")
    p.add_argument("--tries", type=int, default=4)
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=19)
    args = p.parse_args()
    save_result("G-58", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
