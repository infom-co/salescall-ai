"""G-65：お名前の入る一言のつなぎ目を、声帯の震えの止まっている所（無音・息の止まる所・息の音）の中だけに置くと、自然に聞こえるか
（DESIGN.md 4-3 の名前の録音の台帳。CL-2 の連絡帳 4-156 の「役職・名前・続く言葉の3つ」の切れ目を決めるため）。

⛔ G-64 の切り方の誤り（2026-10-24 に、作った音で測った）：言葉ごとの時刻（Soniox）の「終わり」は、どの文字も始まり＋0.06秒で、声の本当の
   終わりではない。G-64 はそこで切って無音を足したので、足した無音の直前で声が鳴ったまま断ち切れていた（一番大きい所から −13〜−18 dB）
   ＝ユーザーの「『部長の』のあとで途切れる」（(B)）・「『部長の』と『岡田様』のあとで途切れる」（(A)）。⇒ G-64 は、3つに分ける形の
   良し悪しを示していない。
⇒ 切る所の決まり（1つ）：言葉の切れ目の場所は言葉ごとの時刻で決め、切るのはその近くの、声帯の震えの無い区間（voice_features.unvoiced_spans
   ＝無音と息の音）の中だけ。その区間が無い切れ目では切らない（「様｜は」は、声が切れずに続く録音がある）。
   ⚠️ G-58 の3回目（「は｜いら」「で｜いら」でつないで乱れた）も、同じ時刻の「終わり」で切って無音を足していた（✕「中島様でっ、いらっしゃいます」は
   その断ち切りの音）＝声の続く所でつなげないことは、まだ確かめていない ⇒ (P0) で、ユーザーの案（『〇〇様』と『は』を分ける）をこの決まりの外で試す。
   - 役職と名前の間（RN_*）：役職の文のもとの間をそのまま残す。名前の部分は、声の始まる KEEP_S 前から（g58_name_splice.trim と同じ）
   - 「様」と助詞（と・で）の間（PT_*）：助詞の頭の息の止まる所の、一番静かな所で、名前の部分も続く言葉も切る
   - 助詞のあと：息の音の真ん中（name_clips.seam_cut。G-58 の4回目で ◎）
声ごとに、どれか伏せて並べ、人が聞いて比べる（results/name_splice/round6/{声の型}/ の 01〜06。どれがどれかは index.json。聞く前に開かない）：
   (W)  文まるごと「営業部部長の岡田様はいらっしゃいますでしょうか？」
   (R1) 役職（山田様の文から）｜「岡田様はいらっ」（役職の無い文から）｜「しゃいますでしょうか？」（中島様の文から）＝G-64 の (B) の切り方だけを直した形
   (R2) 役職｜「岡田様はいらっ」（「課長の岡田様は…」の文の、のあとの間から＝文の途中の声）｜「しゃいますでしょうか？」
   (P0) 役職｜「岡田様」（R1 の名前の文から）｜「はいらっしゃいますでしょうか？」（中島様の文から）：「様｜は」の近くの一番静かな所で両方を切り、
        無音を足さずにつなぐ（声が続いていても切る＝決まりの外。間があった録音かどうかは記録に残す）
   (P1) 「岡田様」（「岡田様とお電話の約束を…」の文から）｜「とお電話の約束を…」（中島様の文から）＝約束の一言
   (P2) 同じ「岡田様」｜「でいらっしゃいますでしょうか？」（中島様の文から）＝確かめる一言（名前の部分1つを、別の形に使い回せるか）
録り方と確かめは make_ack_clips.take と同じ（g58_name_splice.good_take）。
"""
from __future__ import annotations

import asyncio
import json
import random

import numpy as np

from common import base_parser, make_client, pick_key, require_python, save_result, say, write_wav
from g58_name_splice import OUT, good_take, trim
from phrases import PROMISE_AGAIN, TARGET_CALL, TARGET_CONFIRM

ROLE = "営業部部長の"
NAME, OTHER, ROLE_NAME, CARRIER_ROLE = "岡田", "中島", "山田", "課長の"   # 名前の部分の名前・続く言葉の文の名前・役職の文の名前・名前の部分の文の役職
KEEP_S = 0.03            # 名前の部分の、声の始まる前に残す長さ（trim と同じ）
RN_WINDOW_S = 0.06       # 役職と名前の間を探す幅：「の」の始まりから、次の文字の始まり＋この長さまで
PT_BEFORE_S, PT_AFTER_S = 0.15, 0.10   # 「様」と助詞の間を探す幅：助詞の文字の始まりの前後
QUIET_DB = -20.0         # 助詞の頭で切る所は、一番大きい所よりこれだけ静か（息の止まる所）。それより大きければ切らない
STEP_S = 0.005           # 一番静かな所を探す細かさ


def char_index(times, head: str) -> int | None:
    """言葉ごとの時刻のかけらを頭からつないで、head と同じになるかけらの番号（g58_name_splice.cut_by_words と同じ比べ方）。"""
    got = ""
    for k, (text, _, _) in enumerate(times):
        got += text.strip()
        if got == head and k + 1 < len(times):
            return k
        if not head.startswith(got):
            return None
    return None


def voiceless_in(pcm: bytes, rate: int, a: float, b: float) -> tuple[float, float] | None:
    """a〜b の秒と一番重なる、声帯の震えの無い区間（無音・息の音）。"""
    from voice_features import unvoiced_spans
    best = None
    for s, e in unvoiced_spans(pcm, rate, min_s=0.03):
        ov = min(e, b) - max(s, a)
        if ov > 0 and (best is None or ov > best[0]):
            best = (ov, s, e)
    return (best[1], best[2]) if best else None


def _zero_cross(x: np.ndarray, i: int, rate: int) -> int:
    lo, hi = max(1, i - int(0.003 * rate)), min(len(x) - 1, i + int(0.003 * rate))
    cross = [j for j in range(lo, hi) if (x[j - 1] <= 0 < x[j]) or (x[j - 1] >= 0 > x[j])]
    return min(cross, key=lambda j: abs(j - i)) if cross else i


def quiet_point(pcm: bytes, rate: int, a: float, b: float) -> tuple[int, float]:
    """a〜b の秒の中で一番静かな所（標本の番号。音の波が0をまたぐ所）と、その大きさ（録音の一番大きい所からの dB）。"""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    w = int(0.03 * rate)
    peak = max(np.sqrt((x[i:i + w] ** 2).mean()) for i in range(0, max(1, len(x) - w), int(0.01 * rate)))
    s = int(STEP_S * rate)
    cands = [(np.sqrt((x[i:i + s] ** 2).mean()), i + s // 2) for i in range(int(a * rate), max(int(a * rate) + 1, int(b * rate) - s), s)]
    rms, i = min(cands)
    return _zero_cross(x, i, rate), round(float(20 * np.log10(rms / peak + 1e-9)), 1)


def pause_after(pcm: bytes, rate: int, times, head: str) -> tuple[float, float] | None:
    """役職と名前の間（head＝「営業部部長の」「課長の」のあとの間）の、声帯の震えの無い区間 (始まり, 終わり)。"""
    k = char_index(times, head)
    if k is None:
        return None
    return voiceless_in(pcm, rate, times[k][1], times[k + 1][1] + RN_WINDOW_S)


def particle_cut(pcm: bytes, rate: int, times, head: str) -> dict | None:
    """「様」と助詞の間（head＝「岡田様」のあと）の切る所。助詞の頭の息の止まる所の、一番静かな所。無ければ None。"""
    k = char_index(times, head)
    if k is None:
        return None
    t = times[k + 1][1]
    span = voiceless_in(pcm, rate, t - PT_BEFORE_S, t + PT_AFTER_S)
    if span is None:
        return {"cut": None, "span": None, "db": None}
    cut, db = quiet_point(pcm, rate, max(span[0], t - PT_BEFORE_S), min(span[1], t + PT_AFTER_S))
    return {"cut": cut if db <= QUIET_DB else None, "span": span, "db": db}


async def timed(client, args, key: str, text: str, check):
    """確かめに通り、check（音・速さ・言葉ごとの時刻 ⇒ 値。切れ目が無ければ None）が値を返す録音。(音, 速さ, 時刻, 値, 記録)。"""
    from ears import soniox_word_times
    for _ in range(args.tries):
        pcm, rate, row = await good_take(client, args, args.type, "whole", text)
        pcm = trim(pcm, rate)
        times = await soniox_word_times(key, pcm, rate)
        row["words"] = [(t, round(a, 3), round(b, 3)) for t, a, b in times]
        got = check(pcm, rate, times)
        if got is not None:
            return pcm, rate, times, got, row
        say("切れ目が見つからなかった：", text, [t for t, _, _ in times])
    raise RuntimeError(f"切れ目の見つかる録音が作れなかった：{text}")


def wa_evidence(pcm: bytes, rate: int, times, head: str) -> dict | None:
    """「様｜は」の近くの一番静かな所（(P0) で切る所）と、そこに声帯の震えの無い区間があるか（区間・大きさ）。"""
    k = char_index(times, head)
    if k is None:
        return None
    t = times[k + 1][1]
    span = voiceless_in(pcm, rate, t - PT_BEFORE_S, t + PT_AFTER_S)
    cut, db = quiet_point(pcm, rate, t - PT_BEFORE_S, t + PT_AFTER_S)
    return {"cut": cut, "span": span, "db": db}


async def best_head(client, args, key: str, text: str, check, score):
    """名前の部分の材料を tries 本まで録り、score（値 ⇒ (合うか, ずれ)）が合う録音、無ければずれの一番小さい録音。"""
    best, notes = None, []
    for n in range(args.tries):
        got = await timed(client, args, key, text, check)
        ok, miss, note = score(*got[:4])
        notes.append({"try": n + 1, **note})
        say("名前の部分", notes[-1])
        if best is None or (ok, -miss) > (best[0], -best[1]):
            best = (ok, miss, got)
        if ok:
            break
    return best[2], notes


async def run(args) -> dict:
    from common import load_secret
    from name_clips import MAX_SEAM_DB, MAX_SEAM_ST, MAX_ST, MIN_CLARITY, SEAM_S, head_ok, measure_head, seam_cut, tail_from
    from voice_features import features, loudness_db, semitones
    client = make_client(pick_key(args))
    key = load_secret("SONIOX_API_KEY", args.env)
    out = OUT / "round6" / args.type
    out.mkdir(parents=True, exist_ok=True)
    call = TARGET_CALL["法人"]
    notes = {}

    # 役職の部分：山田様の文から、「の」のあとの間を残して（名前の声の始まる KEEP_S 前まで）
    x, rate, _, pause_x, row_x = await timed(client, args, key, call.format(who=f"{ROLE}{ROLE_NAME}様"),
                                             lambda p, r, t: pause_after(p, r, t, ROLE))
    role_pcm = x[:int(max(pause_x[0], pause_x[1] - KEEP_S) * rate) * 2]
    notes["役職のあとの間（秒）"] = round(pause_x[1] - pause_x[0], 3)

    # 呼ぶ一言の続く言葉：中島様の文から、息の音の継ぎ目より後ろ（name_clips.tail_from）
    z, rz, times_z, tail_b, row_z = await timed(client, args, key, call.format(who=f"{OTHER}様"), lambda p, r, t: tail_from(p, r, t))
    assert rz == rate, (rz, rate)
    wa_z = wa_evidence(z, rz, times_z, f"{OTHER}様")
    notes["様とはの間（中島様の文）"] = {k: v for k, v in wa_z.items() if k != "cut"}

    def head_score(before: str):
        def score(pcm, r, times, cut):
            m = measure_head(pcm, r, times, tail_b, before)
            ok = head_ok(m)
            # 不自然な間のある録音は、ずれが小さくても選ばない（⛔ 2026-10-24 S2 の R2：合う録音が無く、ずれの一番小さい録音に 0.16秒の間があった）
            miss = ((m["継ぎ目の前の高さの差（半音）"] or 99) + (m["高さの差（半音）"] or 99) + (500 if m["不自然な間"] else 0)
                    if m["cut"] is not None else 999)
            wa = wa_evidence(pcm, r, times, f"{NAME}様") or {}
            return ok, miss, {**{k: v for k, v in m.items() if k != "cut"}, "様とはの間": {k: v for k, v in wa.items() if k != "cut"}}
        return score

    # (R1) 名前の部分：役職の無い文から（G-64 の (B) と同じ材料）
    (y1, _, times1, _, row1), notes["R1 名前の部分"] = await best_head(
        client, args, key, call.format(who=f"{NAME}様"), lambda p, r, t: seam_cut(p, r, t), head_score(""))
    r1 = role_pcm + y1[:seam_cut(y1, rate, times1) * 2] + tail_b["pcm"]
    # (P0) 同じ名前の文と続く言葉の文を、「様｜は」の近くの一番静かな所で切って、そのままつなぐ
    wa_y = wa_evidence(y1, rate, times1, f"{NAME}様")
    p0 = role_pcm + y1[:wa_y["cut"] * 2] + z[wa_z["cut"] * 2:]
    notes["P0 切った所"] = {"名前の文": {k: v for k, v in wa_y.items() if k != "cut"}, "続く言葉の文": notes["様とはの間（中島様の文）"]}

    # (R2) 名前の部分：「課長の岡田様は…」の文の、「の」のあとの間から
    def r2_check(p, r, t):
        span, cut = pause_after(p, r, t, CARRIER_ROLE), seam_cut(p, r, t)
        return (span, cut) if span is not None and cut is not None else None
    (y2, _, times2, (span2, cut2), row2), notes["R2 名前の部分"] = await best_head(
        client, args, key, call.format(who=f"{CARRIER_ROLE}{NAME}様"), r2_check, head_score(CARRIER_ROLE))
    r2 = role_pcm + y2[int(max(span2[0], span2[1] - KEEP_S) * rate) * 2:cut2 * 2] + tail_b["pcm"]

    # (P1)(P2) 続く言葉：中島様の文から、助詞（と・で）の頭の一番静かな所より後ろ
    def pt_check(head):
        return lambda p, r, t: (lambda c: c if c and c["cut"] is not None else None)(particle_cut(p, r, t, head))
    tails = {}
    for form, tmpl in (("と", PROMISE_AGAIN), ("で", TARGET_CONFIRM)):
        p, r, _, c, row = await timed(client, args, key, tmpl.format(who=f"{OTHER}様"), pt_check(f"{OTHER}様"))
        before = p[max(0, c["cut"] - int(SEAM_S * r)) * 2:c["cut"] * 2]
        tails[form] = {"pcm": p[c["cut"] * 2:], "whole_hz": features(p, r)["pitch_hz"], "seam_hz": (features(before, r) or {}).get("pitch_hz"),
                       "seam_db": loudness_db(before), "span": c["span"], "db": c["db"], "row": row}

    # 「岡田様」：「岡田様とお電話の約束を…」の文から、との頭の一番静かな所まで。両方の続く言葉と声の合う録音を選ぶ
    def name_score(p, r, times, c):
        seg = p[:c["cut"] * 2]
        before = seg[-int(SEAM_S * r) * 2:]
        f, fs = features(p, r), features(before, r)
        if not f or not fs:
            return False, 999, {"声が測れない": True}
        d = {form: {"継ぎ目の前の高さの差（半音）": round(abs(semitones(fs["pitch_hz"], t["seam_hz"])), 2) if t["seam_hz"] else None,
                    "継ぎ目の前の大きさの差（dB）": round(abs(loudness_db(before) - t["seam_db"]), 1),
                    "高さの差（半音）": round(abs(semitones(f["pitch_hz"], t["whole_hz"])), 2)} for form, t in tails.items()}
        ok = f["clarity"] >= MIN_CLARITY and all(v["継ぎ目の前の高さの差（半音）"] is not None and v["継ぎ目の前の高さの差（半音）"] <= MAX_SEAM_ST
                                                 and v["継ぎ目の前の大きさの差（dB）"] <= MAX_SEAM_DB and v["高さの差（半音）"] <= MAX_ST for v in d.values())
        miss = sum((v["継ぎ目の前の高さの差（半音）"] or 99) + v["高さの差（半音）"] for v in d.values())
        return ok, miss, {"澄み具合": round(f["clarity"], 3), "切る所の大きさ（dB）": c["db"], **d}
    (yp, _, _, cp, rowp), notes["P 名前の部分"] = await best_head(
        client, args, key, PROMISE_AGAIN.format(who=f"{NAME}様"), pt_check(f"{NAME}様"), name_score)
    name_p = yp[:cp["cut"] * 2]

    w, rw, row_w = await good_take(client, args, args.type, "whole", call.format(who=f"{ROLE}{NAME}様"))
    items = [{"kind": "(W) まるごと", "text": call.format(who=f"{ROLE}{NAME}様"), "pcm": trim(w, rw)},
             {"kind": "(R1) 役職｜役職の無い文の名前と助詞｜息の音からの続く言葉", "text": f"{ROLE}｜{NAME}様はいらっ｜しゃいますでしょうか？", "pcm": r1},
             {"kind": "(P0) 役職｜名前｜「は」からの続く言葉（「様｜は」の一番静かな所で、無音を足さずに）", "text": f"{ROLE}｜{NAME}様｜はいらっしゃいますでしょうか？", "pcm": p0},
             {"kind": "(R2) 役職｜「課長の」の文の名前と助詞｜息の音からの続く言葉", "text": f"{ROLE}｜{NAME}様はいらっ｜しゃいますでしょうか？", "pcm": r2},
             {"kind": "(P1) 名前｜「と」の頭からの続く言葉（約束の一言）", "text": PROMISE_AGAIN.format(who=f"{NAME}様｜"), "pcm": name_p + tails["と"]["pcm"]},
             {"kind": "(P2) 同じ名前｜「で」の頭からの続く言葉（確かめる一言）", "text": TARGET_CONFIRM.format(who=f"{NAME}様｜"), "pcm": name_p + tails["で"]["pcm"]}]
    random.Random(args.seed).shuffle(items)
    index = []
    for k, it in enumerate(items, 1):
        wav = f"{k:02d}.wav"
        write_wav(out / wav, it["pcm"], rate)
        index.append({"wav": wav, "kind": it["kind"], "text": it["text"]})
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    say("作った：", ", ".join(f"{args.type}/{x['wav']}" for x in index))
    return {"model": args.model, "type": args.type, "round": 6, "items": index, "measures": notes,
            "tails": {f: {k: v for k, v in t.items() if k != "pcm"} for f, t in tails.items()},
            "takes": {"role": row_x, "tail_call": row_z, "name_r1": row1, "name_r2": row2, "name_p": rowp, "whole": row_w}}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--type", default="S2")
    p.add_argument("--tries", type=int, default=8)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=65)
    args = p.parse_args()
    save_result("G-65", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
