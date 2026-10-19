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


# 2回目（✅ 2026-10-19 ユーザーの聞き比べ：「どれも少しおかしい」）：
#   ・名前が「誇張しすぎ」（4本とも）⇒ かぎかっこを外す・漢字で渡す、を比べる
#   ・「営業部部長」が「営業部長」の言い方になる（文まるごとの2本）⇒ 「営業部 部長」と間に空白を置いて渡す、を比べる（言葉は変えない）
#   ・継ぎ合わせの「営業部部長の」が「偉そうな言い方」（1本だけで読ませた録音）⇒ 文まるごとの録音から、部署・役職の所を切り出して継ぐ
ROUND2 = {  # 渡し方の名前 → (部署・役職, 名前の書き方 {読み: 書き方})
    "空白_ひらがな": ("営業部 部長の", {"おかだ": "おかだ", "なかじま": "なかじま"}),
    "空白_漢字": ("営業部 部長の", {"おかだ": "岡田", "なかじま": "中島"}),
    "空白なし_漢字": ("営業部部長の", {"おかだ": "岡田", "なかじま": "中島"}),
}
CUT_FROM = "空白_漢字"  # 継ぎ合わせの材料にする渡し方


def boundary_cut(pcm: bytes, rate: int, role: str, text: str) -> tuple[bytes, bytes] | None:
    """文まるごとの録音を、部署・役職の所と、名前からあとの所に切る：部署・役職の読みの割合に近い所の、声の中の間（0.05秒以上）で切る。"""
    from jtext import compare_reading
    runs = voice_runs(voiced(pcm, rate), 0.03)
    if len(runs) < 2:
        return None
    start, end = runs[0][0], runs[-1][1]
    want = start + (end - start) * len(compare_reading(role)) / len(compare_reading(text))
    gaps = [(abs(a[1] - want), a[1], b[0]) for a, b in zip(runs, runs[1:]) if b[0] - a[1] >= 0.05]
    if not gaps:
        return None
    _, g0, g1 = min(gaps)
    keep = 0.03
    return pcm[:int((g0 + keep) * rate) * 2], pcm[int(max(g0, g1 - keep) * rate) * 2:]


async def run2(args) -> dict:
    client = make_client(pick_key(args))
    out = OUT / "round2"
    out.mkdir(parents=True, exist_ok=True)
    items, cuts = [], {}
    for how, (role, names) in ROUND2.items():
        for reading, written in names.items():
            text = f"{role}{written}様はいらっしゃいますでしょうか？"
            pcm, rate, row = await good_take(client, args, args.type, "whole", text)
            items.append({"kind": f"まるごと・{how}", "text": text, "pcm": trim(pcm, rate), "rate": rate, "rows": [row]})
            if how == CUT_FROM:
                cuts[reading] = (boundary_cut(trim(pcm, rate), rate, role, text), rate, text)
    # 継ぎ合わせ：部署・役職の所は、もう1つの名前の文から切り出す（名前の文と、部署・役職の所が、別の録音になるように）
    for reading, other in (("おかだ", "なかじま"), ("なかじま", "おかだ")):
        (mine, rate, text), (theirs, _, _) = cuts[reading], cuts[other]
        if mine is None or theirs is None:
            say("切れ目が見つからなかった：", reading)
            continue
        joined = theirs[0] + bytes(int(JOIN_S * rate) * 2) + mine[1]
        items.append({"kind": f"継ぎ合わせ・{CUT_FROM}（部署・役職は「{other}」の文から）", "text": text, "pcm": joined, "rate": rate, "rows": []})
    random.Random(args.seed).shuffle(items)
    index = []
    for k, it in enumerate(items, 1):
        name = f"{k:02d}.wav"
        write_wav(out / name, it["pcm"], it["rate"])
        index.append({"wav": name, "kind": it["kind"], "text": it["text"], "takes": it["rows"]})
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    say("作った：", ", ".join(x["wav"] for x in index))
    return {"model": args.model, "type": args.type, "round": 2, "items": [{k: v for k, v in x.items() if k != "takes"} for x in index],
            "takes": index}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--type", default="S2")
    p.add_argument("--tries", type=int, default=4)
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=19)
    p.add_argument("--round", type=int, default=1)
    args = p.parse_args()
    save_result("G-58" if args.round == 1 else "G-58-2", asyncio.run(run(args) if args.round == 1 else run2(args)))


if __name__ == "__main__":
    main()
