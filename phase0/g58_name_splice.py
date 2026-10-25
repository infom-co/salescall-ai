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
QUOTA_WAIT_S = 30.0  # 録る問い合わせが上限の答えだったときに待つ秒（1分の上限）


def trim(pcm: bytes, rate: int, keep_s: float = 0.03) -> bytes:
    """声の前後の無音を切る（継ぎ目に余分な間を入れないため）。"""
    runs = voice_runs(voiced(pcm, rate), 0.05)
    if not runs:
        return pcm
    a = max(0, int((runs[0][0] - keep_s) * rate)) * 2
    b = min(len(pcm), int((runs[-1][1] + keep_s) * rate) * 2)
    return pcm[a:b]


async def good_take(client, args, vt: str, aid: str, text: str, also: tuple[str, ...] = ()) -> tuple[bytes, int, dict]:
    """読みが合う録音（make_ack_clips の確かめのうち、文の途中の間の確かめだけは外す：上の JOIN_S のとおり、文まるごとでも間が入るので、
    間の自然さは人が聞いて比べる）。also：この読みに聞こえても通す文（どれに聞こえたかを記録の heard_as に残す。⛔ 2026-10-25：
    「お間違えない」（✅ ユーザーの言い方）は、どの耳も「お間違いない」と書き、8回とも通らなかった＝声か耳かは人が聞いて決める）。"""
    from common import is_quota
    from make_ack_clips import ends_in_silence, heard_ok
    for _ in range(args.tries):
        row, pcm, rate = await take(client, args, vt, aid, text, "question")
        as_ = next((t for t in (text,) + tuple(also) if heard_ok(t, row.get("heard") or {})), None)
        if pcm and as_ is not None and ends_in_silence(pcm, rate):
            return pcm, rate, row | {"heard_as": as_}
        # 上限の答え（1分の上限）：すぐ録り直すと、残りの回数を同じ1分の中で使い切る（⛔ 2026-10-25 G-69 の1回目）⇒ 1分の上限が戻るまで待つ
        quota = row.get("error") and is_quota(Exception(row["error"].get("message", "")))
        await asyncio.sleep(QUOTA_WAIT_S if quota else args.pause)
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


# 3回目（✅ 2026-10-19 ユーザー：「名前+続く言葉を別に保存して、それぞれをシステムにできるだけ置いておく録音にしたらどうですか？同じ録音だと
#   永遠に録音し続けなくてはならないので、実運用的ではありません。というか無駄」）：名前の部分（「岡田様で」「岡田様は」）と、続く言葉
#   （「いらっしゃいますでしょうか？」＝確かめる一言・呼ぶ一言で同じ）を別に持ち、組み合わせる。部署・役職も別の部分にする。
#   ⚠️ 2回目の継ぎ合わせ（05・07 ✕）は、部署と役職の間に空白を置いた形（それ自体が「間が空きすぎ」で ✕）で作っていた＝継ぎ合わせの
#      良し悪しは公平に確かめていない ⇒ 3回目は、決まった渡し方（漢字・かぎかっこなし・空白なし）の文から切り出す。
#   継ぎ目には無音を足さない（文の途中の 0.12秒より長い無音は「、」に聞こえる＝10-15）：切り出した所の、もとの文の声の間をそのまま使う。
ROUND3_NAMES = ("岡田", "中島")
ROUND3_FORMS = {  # 形 → (前, 名前のあとの言葉の頭, 続く言葉)。名前の部分＝前＋名前＋頭
    "確かめる一言": ("", "様で", "いらっしゃいますでしょうか？"),
    "呼ぶ一言": ("営業部部長の", "様は", "いらっしゃいますでしょうか？"),
}


def cut_by_words(pcm: bytes, rate: int, head: str, times: list[tuple[str, float, float]]) -> tuple[bytes, bytes, float] | None:
    """文まるごとの録音を、頭（head＝名前の部分「中島様で」）の最後の言葉が終わった所で切る（言葉ごとの時刻＝ears.soniox_word_times）。
    (前, 後ろ, もとの間の秒)。頭の文字と、聞き取った言葉の並びの頭が合わなければ None（その録音は継ぎ合わせの材料にしない）。
    ⛔ 2026-10-19 の1回目：声の中の短い間を目安に切ったら、言葉の切れ目でない所（「なか｜じま」の中・「営業部部長の｜」）で切り、組み合わせた
       4本とも言葉どおりに聞こえなかった（「中」で切れた・名前が入れ替わった・2回くり返した）"""
    # 文字そのもの（漢字のまま）で比べる（⛔ かけらごとに読みに直すと、「様」が1字だけで「よう」と読まれて合わなかった）
    want, got = head.replace(" ", ""), ""
    for k, (text, _, end) in enumerate(times):
        got += text.strip()
        if got == want and k + 1 < len(times):
            nxt = times[k + 1][1]
            return pcm[:int(end * rate) * 2], pcm[int(nxt * rate) * 2:], round(max(0.0, nxt - end), 3)
        if not want.startswith(got):
            return None
    return None


async def aligned_take(client, args, text: str, head: str, key: str):
    """確かめに通り、言葉ごとの時刻で頭の所が見つかる録音（見つからなければ録り直す）。"""
    from ears import soniox_word_times
    for _ in range(args.tries):
        pcm, rate, row = await good_take(client, args, args.type, "whole", text)
        pcm = trim(pcm, rate)
        times = await soniox_word_times(key, pcm, rate)
        cut = cut_by_words(pcm, rate, head, times)
        row["words"] = [(t, round(a, 3), round(b, 3)) for t, a, b in times]
        if cut is not None:
            return pcm, rate, row, cut
        say("頭の所が見つからなかった：", text, [t for t, _, _ in times])
    raise RuntimeError(f"継ぎ目の所が見つかる録音が作れなかった：{text}")


async def run3(args) -> dict:
    client = make_client(pick_key(args))
    out = OUT / "round3"
    out.mkdir(parents=True, exist_ok=True)
    items, parts = [], {}
    from common import load_secret
    key = load_secret("SONIOX_API_KEY", args.env)
    for form, (before, mid, tail) in ROUND3_FORMS.items():
        for name in ROUND3_NAMES:
            text = f"{before}{name}{mid}{tail}"
            pcm, rate, row, cut = await aligned_take(client, args, text, f"{before}{name}{mid}", key)
            items.append({"kind": f"{form}・まるごと", "text": text, "pcm": pcm, "rate": rate, "rows": [row]})
            parts[(form, name)] = (cut, rate, row)
    # 継ぎ合わせ：名前の部分は、その名前の文から・続く言葉は、もう1つの名前の文から（別の録音どうしを組み合わせる）
    for form in ROUND3_FORMS:
        for name, other in (("岡田", "中島"), ("中島", "岡田")):
            (mine, rate, _), (theirs, _, _) = parts[(form, name)], parts[(form, other)]
            if mine is None or theirs is None:
                say("切れ目が見つからなかった：", form, name)
                continue
            gap = theirs[2]  # 続く言葉の前の間は、続く言葉を切り出した文のもとの間
            joined = mine[0] + bytes(int(gap * rate) * 2) + theirs[1]
            before, mid, tail = ROUND3_FORMS[form]
            items.append({"kind": f"{form}・継ぎ合わせ（続く言葉は「{other}」の文から・継ぎ目の間 {gap}秒）",
                          "text": f"{before}{name}{mid}｜{tail}", "pcm": joined, "rate": rate, "rows": [],
                          "gaps": {"名前の文": mine[2], "続く言葉の文": gap}})
    random.Random(args.seed).shuffle(items)
    index = []
    for k, it in enumerate(items, 1):
        wav = f"{k:02d}.wav"
        write_wav(out / wav, it["pcm"], it["rate"])
        index.append({"wav": wav, "kind": it["kind"], "text": it["text"], "takes": it["rows"], **({"gaps": it["gaps"]} if "gaps" in it else {})})
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    say("作った：", ", ".join(x["wav"] for x in index))
    return {"model": args.model, "type": args.type, "round": 3, "items": [{k: v for k, v in x.items() if k != "takes"} for x in index],
            "takes": index}


# 4回目（✅ 2026-10-19 ユーザーの聞き比べ＝3回目）：01 ✕「中島様でっ、いらっしゃいますでしょうか」・02 ◎・03 △「営業部長」のような言い方・威圧的・
#   声が低い・04 ○△ 威圧的・声が低い・05 ○△「営業部長」のような言い方・06 ○△ 名前とその後が別の声・07 ○「いら」の辺りで乱れ（無ければ◎）・
#   08 ○△「岡田様でっ」・後に間。⇒ (1) 組み合わせる形は使える（07）(2) 欠点の多くは材料の録音から引き継いだ（05←03・01←08・06←04）
#   (3) 継ぎ目「は｜いら」「で｜いら」は母音から母音へ切れ目なく続く所＝別の録音とつなぐと音がつながらない（07 の乱れ・06 の別の声）。
#   ⚠️ 2026-10-24：3回目は cut_by_words（言葉ごとの時刻の「終わり」＝始まり＋0.06秒）で切って無音を足していた＝01「でっ、」・08「でっ」は声の途中の
#   断ち切り。(3) の「つながらない」もその分が混ざる（g65_seams で確かめ直す）。
#   ⇒ 4回目：(a) 切るのは「いらっ｜しゃいます」の「っ」で声が止まる所（無音どうしをつなぐ。音には手を加えない）(b) 続く言葉「しゃいますでしょうか？」は、
#   ◎をいただいた 3回目の 02 から切り出し、全部の組み合わせで同じものを使う（承認した録音の使い回し）(c) 名前の部分は、02 と声が近い録音だけ
#   （高さの差が半音2つ以内・澄み具合 0.84 以上＝voice_features。3回目で「威圧的・声が低い」とされた 04 は 0.78）・不自然な間が無いもの。
ROUND4_TAIL = "round3/02.wav"   # ◎（2026-10-19 ユーザー）。文＝営業部部長の岡田様はいらっしゃいますでしょうか？
ROUND4_FORMS = {"確かめる一言": ("", "様で"), "呼ぶ一言": ("営業部部長の", "様は")}
ROUND4_TAIL_TEXT = "いらっしゃいますでしょうか？"
# 名前の部分を選ぶ条件・継ぎ目・組み合わせ方は、名前の録音の台帳の1か所（name_clips）。⛔ 2026-10-19：ここに「っ」の近くで切る形を持っていたが、
#   「っ」の無い言い方（個人の呼ぶ一言）で使えず、3本で息の音ではない所（かすれた声）で切っていた ⇒ name_clips.seam_cut に一本化


async def run4(args) -> dict:
    from common import load_secret
    from ears import soniox_word_times
    from name_clips import head_ok, join, measure_head, tail_from
    from voice_features import read_wav
    client = make_client(pick_key(args))
    key = load_secret("SONIOX_API_KEY", args.env)
    out = OUT / "round4"
    out.mkdir(parents=True, exist_ok=True)
    tail_pcm, tail_rate = read_wav(OUT / ROUND4_TAIL)
    tail = tail_from(tail_pcm, tail_rate, await soniox_word_times(key, tail_pcm, tail_rate))
    assert tail is not None, "02 の継ぎ目が見つからない"
    items, notes = [], []
    for form, (before, mid) in ROUND4_FORMS.items():
        for name in ROUND3_NAMES:
            text = f"{before}{name}{mid}{ROUND4_TAIL_TEXT}"
            best = None
            for n in range(args.tries):
                pcm, rate, row = await good_take(client, args, args.type, "whole", text)
                pcm = trim(pcm, rate)
                assert rate == tail_rate, (rate, tail_rate)
                m = measure_head(pcm, rate, await soniox_word_times(key, pcm, rate), tail, before)
                note = {"text": text, "try": n + 1, **{k: v for k, v in m.items() if k != "cut"}, "切れ目": m["cut"] is not None}
                notes.append(note)
                say(form, name, note)
                if not head_ok(m):
                    continue
                seam = m["継ぎ目の前の高さの差（半音）"]
                if best is None or seam < best[0]:
                    best = (seam, pcm, rate, m["cut"], row, note)
                if seam <= 0.5:
                    break
            if best is None:
                say("02 と声の近い録音が作れなかった：", text)
                continue
            _, pcm, rate, cut, row, note = best
            items.append({"kind": f"{form}・まるごと", "text": text, "pcm": pcm, "rate": rate, "rows": [row], "note": note})
            items.append({"kind": f"{form}・組み合わせ（続く言葉は 3回目の 02 から）", "text": f"{before}{name}{mid}いらっ｜しゃいますでしょうか？",
                          "pcm": join(pcm, cut, tail), "rate": rate, "rows": [], "note": note})
    random.Random(args.seed).shuffle(items)
    index = []
    for k, it in enumerate(items, 1):
        wav = f"{k:02d}.wav"
        write_wav(out / wav, it["pcm"], it["rate"])
        index.append({"wav": wav, "kind": it["kind"], "text": it["text"], "takes": it["rows"], "note": it["note"]})
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    say("作った：", ", ".join(x["wav"] for x in index))
    return {"model": args.model, "type": args.type, "round": 4, "items": [{k: v for k, v in x.items() if k != "takes"} for x in index],
            "takes": index, "tries": notes}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--type", default="S2")
    p.add_argument("--tries", type=int, default=4)
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=19)
    p.add_argument("--round", type=int, default=1)
    args = p.parse_args()
    runs = {1: run, 2: run2, 3: run3, 4: run4}
    save_result({1: "G-58", 2: "G-58-2", 3: "G-58-3", 4: "G-58-4"}[args.round], asyncio.run(runs[args.round](args)))


if __name__ == "__main__":
    main()
