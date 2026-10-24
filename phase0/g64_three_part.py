"""G-64：お名前の入る一言を、役職の部分・名前の部分・続く言葉の3つに分けて組み合わせても、自然に聞こえるか（DESIGN.md 4-3 の名前の録音の台帳）。

✅ 2026-10-24 ユーザー決定（CL-2 の会話で）：「『営業部部長の〇〇様は』も『営業部部長』『〇〇様』『は』に分けて録音しておいた方が汎用性があるんじゃない
   ですか？…永遠に録音が必要ですよ。」⇒ CL-2 の計画書の書き方：役職の部分（「営業部部長の」）・名前の部分（「〇〇様」）・続く言葉（助詞から）。
   「つなぎ目の自然さは CL-1 が試験で確かめる」。
⚠️ G-58 で分かっていること：(1) 文まるごとの録音でも、声は「営業部部長の｜おかだ様は」の所で 0.12〜0.26秒の間を置く（12本中11本）＝役職と名前の間には
   もともと間がある (2) 母音から母音へ切れ目なく続く所（「は｜いら」「で｜いら」）で別の録音とつなぐと、音が乱れた（3回目）(3) 助詞のあとの最初の
   息の音（「いらっ｜しゃいます」の「っし」）で切る形は「◎ 一番いい」（4回目）＝ name_clips.seam_cut。
⇒ 3つを、どれか伏せて並べ、人が聞いて比べる（声ごと）：
   (A) 決まった分け方：役職（別の名前の文から、役職のあとの間で切る）＋名前「岡田様」（役職の無い文から「様」のあとで切る）＋続く言葉「はいらっしゃいますでしょうか？」
       （別の名前の文から「様」のあとで切る）
   (B) 名前と続く言葉は息の音で切る：役職（同じ）＋「岡田様はいらっ」（役職の無い文から、name_clips の継ぎ目で）＋「しゃいますでしょうか？」（別の名前の文から）
   (W) 文まるごと「営業部部長の岡田様はいらっしゃいますでしょうか？」
役職と名前の間には、役職を切り出した文のもとの間を置く（音には手を加えない）。作るもの：results/name_splice/round5/{声の型}/ の 01〜03（どれがどれかは index.json。
聞く前に開かない）。録り方と確かめは make_ack_clips.take と同じ（g58_name_splice.good_take）。
"""
from __future__ import annotations

import asyncio
import json
import random

from common import base_parser, make_client, pick_key, require_python, save_result, say, write_wav
from g58_name_splice import OUT, cut_by_words, good_take, trim

ROLE = "営業部部長の"
NAME, OTHER, ROLE_NAME = "岡田", "中島", "山田"   # 名前の部分を作る名前・続く言葉を切り出す文の名前・役職を切り出す文の名前
TAIL = "はいらっしゃいますでしょうか？"


async def timed_take(client, args, key: str, text: str, head: str):
    """確かめに通り、言葉ごとの時刻で頭（head）の終わりが見つかる録音。(音, 標本の速さ, 言葉ごとの時刻, 頭で切った (前, 後ろ, 間), 記録)。"""
    from ears import soniox_word_times
    for _ in range(args.tries):
        pcm, rate, row = await good_take(client, args, args.type, "whole", text)
        pcm = trim(pcm, rate)
        times = await soniox_word_times(key, pcm, rate)
        cut = cut_by_words(pcm, rate, head, times)
        row["words"] = [(t, round(a, 3), round(b, 3)) for t, a, b in times]
        if cut is not None:
            return pcm, rate, times, cut, row
        say("頭の所が見つからなかった：", text, [t for t, _, _ in times])
    raise RuntimeError(f"切れ目の見つかる録音が作れなかった：{text}")


async def run(args) -> dict:
    from common import load_secret
    from name_clips import head_ok, join, measure_head, tail_from
    client = make_client(pick_key(args))
    key = load_secret("SONIOX_API_KEY", args.env)
    out = OUT / "round5" / args.type
    out.mkdir(parents=True, exist_ok=True)
    # 役職の部分：別の名前の文から、役職のあとで切る（役職のあとの間は、その文のもとの間）
    x, rate, _, (role_pcm, _, role_gap), row_x = await timed_take(client, args, key, f"{ROLE}{ROLE_NAME}様{TAIL}", ROLE)
    # 続く言葉（別の名前の、役職の無い文から）：(A) 「様」のあとから・(B) 息の音の継ぎ目から（name_clips）
    z, rz, times_z, (_, tail_a, gap_a), row_z = await timed_take(client, args, key, f"{OTHER}様{TAIL}", f"{OTHER}様")
    assert rz == rate, (rz, rate)
    tail_b = tail_from(z, rate, times_z)
    if tail_b is None:
        raise RuntimeError("続く言葉の文に、息の音の継ぎ目が見つからない")
    # 名前の部分（役職の無い岡田様の文から）：(A) 「様」まで・(B) 続く言葉の声と合う録音を選んで、息の音の継ぎ目まで
    best = None
    notes = []
    for n in range(args.tries):
        y, ry, times_y, cut_a, row_y = await timed_take(client, args, key, f"{NAME}様{TAIL}", f"{NAME}様")
        m = measure_head(y, ry, times_y, tail_b, "")
        notes.append({"try": n + 1, **{k: v for k, v in m.items() if k != "cut"}})
        say("名前の部分", notes[-1])
        if head_ok(m):
            best = (0.0, y, cut_a, m["cut"], row_y)
            break
        # 合う録音が無ければ、続く言葉の声とのずれが一番小さい録音で（⛔ 2026-10-24：初めの録音で組むと、(B) だけ声のずれで不利になった）
        miss = (m["継ぎ目の前の高さの差（半音）"] or 99) + (m["高さの差（半音）"] or 99) if m["cut"] is not None else 999
        if best is None or miss < best[0]:
            best = (miss, y, cut_a, m["cut"], row_y)
    _, y, (name_a, _, _), cut_b, row_y = best
    w, rw, row_w = await good_take(client, args, args.type, "whole", f"{ROLE}{NAME}様{TAIL}")
    pause = bytes(int(role_gap * rate) * 2)
    items = [{"kind": "まるごと", "text": f"{ROLE}{NAME}様{TAIL}", "pcm": trim(w, rw)},
             {"kind": f"(A) 役職｜名前｜助詞からの続く言葉（役職のあとの間 {role_gap}秒・名前のあとの間 {gap_a}秒）",
              "text": f"{ROLE}｜{NAME}様｜{TAIL}", "pcm": role_pcm + pause + name_a + bytes(int(gap_a * rate) * 2) + tail_a}]
    if cut_b is not None:
        items.append({"kind": f"(B) 役職｜名前と助詞｜息の音からの続く言葉（役職のあとの間 {role_gap}秒）",
                      "text": f"{ROLE}｜{NAME}様はいらっ｜しゃいますでしょうか？", "pcm": role_pcm + pause + join(y, cut_b, tail_b)})
    else:
        say("名前の部分に、息の音の継ぎ目が見つからなかった（(B) は作れない）")
    random.Random(args.seed).shuffle(items)
    index = []
    for k, it in enumerate(items, 1):
        wav = f"{k:02d}.wav"
        write_wav(out / wav, it["pcm"], rate)
        index.append({"wav": wav, "kind": it["kind"], "text": it["text"]})
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    say("作った：", ", ".join(f"{args.type}/{x['wav']}" for x in index))
    return {"model": args.model, "type": args.type, "round": 5, "items": index, "head_tries": notes,
            "takes": {"role": row_x, "tail": row_z, "name": row_y, "whole": row_w}}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--type", default="S2")
    p.add_argument("--tries", type=int, default=8)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=64)
    args = p.parse_args()
    save_result("G-64", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
