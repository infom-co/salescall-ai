"""名前の録音の台帳（DESIGN.md 4-3）の、音の側の唯一の場所：名前の部分と続く言葉を分けて持ち、組み合わせて流す。

✅ 2026-10-19 ユーザー：「名前+続く言葉を別に保存して、それぞれをシステムにできるだけ置いておく録音にしたらどうですか？同じ録音だと
   永遠に録音し続けなくてはならないので、実運用的ではありません。というか無駄」⇒ 4回目の聞き比べ（PHASE0 G-58）で、組み合わせた呼ぶ一言が
   「◎ 一番いい」、確かめる一言も「○ 合格レベル」で、材料にした文まるごと（「○△ ちょっと冷たい言い方」）より良かった。
- 続く言葉（tail）：人が聞いて承認した文まるごとの録音から、継ぎ目より後ろを切り出す（形と声ごとに1つ。どの名前にも使い回す）。
- 名前の部分（head）：名前ごとに、同じ形の文まるごとを作り、継ぎ目より前を使う。続く言葉と声の合う録音だけを選ぶ（音は加工しない＝合う録音を
  選ぶ。⛔ 声の高さも速さも大きさも変えない）。選んだあとに、人が組み合わせを聞いて承認する（DESIGN.md 4-3・6-13）。
- 継ぎ目（seam_cut）：「様」のあとの言葉（は・で・の）より後ろで、最初の息の音（声帯は震えず、音はある所＝「し」「ち」の息）の真ん中。
  音の波が0をまたぐ所。⛔ 無音も数えると、「様で」「様は」のあとの言葉の間で切った（8本中4本）
  ⛔ 3回目：母音から母音へ続く所（「は｜いら」「で｜いら」）で切ると、継ぎ目の音が乱れた（⚠️ 2026-10-24：3回目は言葉ごとの時刻の「終わり」＝始まり＋0.06秒で
  切って無音を足していた＝乱れの一部は声の途中の断ち切り。声の続く所でつなげないことは g65_seams で確かめ直す）。⛔ 4回目の1回目：無音を探すと見つからなかった
  （「っし」は息の音が続く）。呼ぶ一言・確かめる一言では「いらっ｜しゃいます」の「っし」、個人の呼ぶ一言では「お間｜ちがえ」の「ち」になる。
"""
from __future__ import annotations

import numpy as np

from voice_features import features, loudness_db, semitones, unvoiced_spans

# 名前の部分を選ぶ条件（G-58 の4回目で測った）
# 全体の声の高さは、続く言葉のもとの録音と半音 2.4 以内（役職の録音も同じ＝g66_role_seam.pitch_over）。
# ⛔ 2026-10-29 G-69 の5回目まで：半音3つ（G-58 の4回目に、材料が足りる所で私が決めた値。2つ以内では、14本ずつ録って4つ中3つ見つからなかった）では、
#    差が 2.48〜2.94半音の4組がどれも「少し気になる」などのご指摘（S2 の「で」✕・「は」○○「イントネーションが少し気になる」・「で」○○「『岡田様』の
#    イントネーションが少し違和感」・L4 の「で」◎「『で』のあたりが少し気になる」）。2.29半音以下の8組には、高さのご指摘が無い。
#    10-28 の材料（S2 は名前の部分10本・続く言葉は形ごとに 3〜15本）なら、2.4 でも名前の部分10本中6本が4つの形すべてに組める
MAX_ST = 2.4
MIN_CLARITY = 0.84  # 澄み具合（「威圧的・声が低い」の録音は 0.78。◎の録音は 0.87）
# 継ぎ目の直前の声を、続く言葉のもとの録音の同じ所にそろえる。⛔ 4回目の1回目：全体の高さだけで選んだら、継ぎ目で声が2〜7半音跳んだ。
# ⛔ 2026-10-19：高さは合ったのに 6.2dB 小さい録音は、継ぎ目で声の大きさが跳んだ（4回目の 01・02 は 2.4〜2.5dB で ○・◎）
SEAM_S, MAX_SEAM_ST, MAX_SEAM_DB = 0.35, 1.0, 3.0
ODD_GAP_S = 0.08    # 名前の部分の中で、これより長い間は不自然（「様でっ、」＝3回目の 08 は 0.16秒）。部署・役職の「の｜」のあとは、もとの声の間なので除く
AFTER_SLACK_S = 0.05  # 言葉ごとの時刻のずれの見込み
MIN_SPAN_S = 0.03     # 継ぎ目にする、声帯の震えの無い区間の短さの下限


def _end_of_char(times: list[tuple[str, float, float]], k: int) -> float | None:
    """言葉ごとの時刻（文字の並び）で、k 文字目を含む言葉の終わりの秒。"""
    n = 0
    for t, _, b in times:
        n += len(t)
        if n > k:
            return b
    return None


def seam_cut(pcm: bytes, rate: int, times: list[tuple[str, float, float]]) -> int | None:
    """継ぎ目の位置（標本の番号）。「様」の次の言葉（は・で・の）の終わりより後ろで、最初の声帯の震えの無い区間（voice_features.unvoiced_spans）
    の真ん中を、音の波が0をまたぐ所に合わせる。見つからなければ None。"""
    text = "".join(t for t, _, _ in times)
    k = text.find("様")
    after = _end_of_char(times, k + 1) if k >= 0 else None
    if after is None:
        return None
    spans = [(a, b) for a, b in unvoiced_spans(pcm, rate, min_s=MIN_SPAN_S, noise_only=True) if b > after and a >= after - AFTER_SLACK_S]
    if not spans:
        return None
    i = int((spans[0][0] + spans[0][1]) / 2 * rate)
    x = np.frombuffer(pcm, dtype="<i2")
    lo, hi = max(1, i - int(0.003 * rate)), min(len(x) - 1, i + int(0.003 * rate))
    cross = [j for j in range(lo, hi) if (x[j - 1] <= 0 < x[j]) or (x[j - 1] >= 0 > x[j])]
    return min(cross, key=lambda j: abs(j - i)) if cross else i


def odd_gaps(pcm: bytes, rate: int, end: int, times, before: str) -> list[float]:
    """名前の部分（頭から end まで）の中の、不自然な間（ODD_GAP_S より長い）。部署・役職（before）のあとの「の」のあとの間は除く。"""
    from barge import voice_runs, voiced
    runs = voice_runs(voiced(pcm[:end * 2], rate), 0.01)
    no_end = next((b for t, a, b in times if t == "の"), None) if before else None
    out = []
    for a, b in zip(runs, runs[1:]):
        g = b[0] - a[1]
        if g > ODD_GAP_S and not (no_end is not None and abs(a[1] - no_end) <= 0.15):
            out.append(round(g, 3))
    return out


def tail_from(pcm: bytes, rate: int, times) -> dict | None:
    """承認した文まるごとの録音から、続く言葉と、名前の部分を合わせる基準（全体・継ぎ目の直前の高さと大きさ）を取り出す。"""
    cut = seam_cut(pcm, rate, times)
    if cut is None:
        return None
    before = pcm[max(0, cut - int(SEAM_S * rate)) * 2:cut * 2]
    whole, seam = features(pcm, rate), features(before, rate)
    if not whole or not seam:
        return None
    return {"pcm": pcm[cut * 2:], "rate": rate, "cut": cut, "pitch_hz": whole["pitch_hz"], "seam_hz": seam["pitch_hz"], "seam_db": loudness_db(before)}


def measure_head(pcm: bytes, rate: int, times, tail: dict, before: str = "") -> dict:
    """名前の部分の材料（文まるごと1本）を、続く言葉に合うかで測る。cut が None なら継ぎ目が無い。"""
    cut = seam_cut(pcm, rate, times)
    f = features(pcm, rate)
    seam_part = pcm[max(0, cut - int(SEAM_S * rate)) * 2:cut * 2] if cut is not None else b""
    fs = features(seam_part, rate) if cut is not None else None
    return {"cut": cut,
            "高さの差（半音）": round(abs(semitones(f["pitch_hz"], tail["pitch_hz"])), 2) if f else None,
            "継ぎ目の前の高さの差（半音）": round(abs(semitones(fs["pitch_hz"], tail["seam_hz"])), 2) if fs else None,
            "継ぎ目の前の大きさの差（dB）": round(abs(loudness_db(seam_part) - tail["seam_db"]), 1) if cut is not None else None,
            "澄み具合": f and round(f["clarity"], 3),
            "不自然な間": odd_gaps(pcm, rate, cut, times, before) if cut is not None else None}


def head_ok(m: dict) -> bool:
    """名前の部分の材料が、続く言葉に合うか（上の条件のすべて）。"""
    return (m["cut"] is not None and not m["不自然な間"] and m["高さの差（半音）"] is not None and m["高さの差（半音）"] <= MAX_ST
            and m["継ぎ目の前の高さの差（半音）"] is not None and m["継ぎ目の前の高さの差（半音）"] <= MAX_SEAM_ST
            and m["継ぎ目の前の大きさの差（dB）"] <= MAX_SEAM_DB and (m["澄み具合"] or 0) >= MIN_CLARITY)


def join(head_pcm: bytes, cut: int, tail: dict) -> bytes:
    """名前の部分（継ぎ目まで）と続く言葉を、そのまま並べる（音には手を加えない）。"""
    return head_pcm[:cut * 2] + tail["pcm"]
