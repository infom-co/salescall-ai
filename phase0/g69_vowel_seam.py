"""G-69：名前の部分「〇〇様」と、助詞から始まる続く言葉を、「様」の「ま」の母音（あ）の真ん中で切ってつなぐと、自然に聞こえるか
（DESIGN.md 4-3 の名前の録音の台帳。CL-1.md 4-55 の (2)）。

✅ 2026-10-25 日本時間の夕方 ユーザー（「岡田様は」「岡田様で」…のように助詞まで含めて録る形の案に）：「意味がない。助詞に続く語と声質が
   違うことになる。つながりの声質を揃える。また、助詞ごとに名前を録音すると、録音が増えすぎる。」「『営業部部長』『〇〇様』『続く文章』に
   録音が分けられなかったら、この録音で名前を対応するという仕組みは破綻します。」
考え方：
- つなぎ目の前は、どの名前でも「様」の「ま」＝つなぎ目の音の並びは名前によらず同じ。違うのは録音ごとの声の高さ・大きさ・声質だけ。
- G-65 の (P0)(P2) は、「ま」から助詞へ音が移っていく途中（一番静かな所）で切った＝録音ごとの違いが一番出る所（✕・△「声質が違う」）。
  ⇒ 音の落ち着いている「あ」の真ん中で切る：名前の部分＝「〇〇さ」＋「あ」の前半／続く言葉＝「あ」の後半＋助詞から。名前の部分は後ろの助詞の
  影響を受けない＝1つの名前の録音を「は」「で」「と」「の」の4つの形に使う。助詞への音の移り方は、続く言葉の録音にもともと入っている。
- 声の合う組を選ぶ（⛔ 音は加工しない＝高さも速さも大きさも変えない。合う録音を選ぶだけ）：つなぎ目の前後 SIDE_S の高さ（半音）・大きさ（dB）・
  声質（帯ごとの強さの形の差 dB）。同じ録音の同じ所（切らずに続く声）の差が、自然な声の変わり方（記録に並べる）。
- 名前の部分は、名前が声の頭になる文（役職の無い文）から（G-66：「課長の」の文の途中から切ると、「お」で始まる名前の頭が欠けた）。
  役職の部分は G-66 と同じ選び方（g66_role_seam.pick_role）。
段は2つ：(1) 録る（材料を takes/ に保存する。--reuse を付けなければ録る）(2) 保存した材料から、切る所を決め、組を選んで組む（録り直さずに、
切り方を直して組み直せる。⛔ 2026-10-25 の1回目：材料を保存していなかったので、切り方の誤りが分かっても組み直せなかった）。
1回目（round8・✅ 2026-10-25 夜のユーザーの聞き比べ＝PHASE0 G-69）：S2 は (V) 5本中4本 ◎・前の切り方 (C) は ✕「『は』の助詞が聞こえない」。
   残ったのは、助詞のあたりの雑音（L4 に多い）・「岡田様と」のあとの間・早口・L4 の役職のつなぎ目の音量。⇒ 2回目（round9）で直したこと：
   (1) 切る所を、声の1周期の同じ位置にそろえる（周期ごとの一番大きい山の直前で、波が0をまたぐ所。名前の部分と続く言葉で同じ向きの山。
       ⛔ 1回目は、どちらも「下から上へ0をまたぐ所」というだけで、周期の中の位置は録音ごとにばらばらだった）。前後の1周期の形の近さも選ぶ条件に足す
   (2) 材料を増やす（名前6・続く言葉は形ごとに5・役職10）。続く言葉は、助詞のあとに不自然な間がある録音は使わず、話す速さが同じ形の材料の
       中央から離れた録音は選ばれにくくする
   (3) 雑音の出どころ（つなぎ目か、材料か）を決めるため、使った続く言葉の材料そのもの（M）も聞いていただく。(C) は結論が出たので外した
声ごとに9本（results/name_splice/round{回}/{声の型}/ の 01〜09。どれがどれかは index.json。聞く前に開かない）：
   (W は) 文まるごと「岡田様はいらっしゃいますでしょうか？」（名前の部分を切り出した録音そのもの）
   (V は)「岡田さ（あ）」｜「（あ）はいらっしゃいますでしょうか？」（中島様の文から）
   (M は)・(M と) (V は)・(V と) に使った続く言葉の材料そのもの（「中島様は…」「中島様とお電話の約束を…」）
   (W で) 文まるごと「岡田様でいらっしゃいますでしょうか？」
   (V で)(V と)(V の) 同じ名前の部分｜「（あ）でいらっしゃいますでしょうか？」「（あ）とお電話の約束を…」「（あ）のお電話でお間違えないでしょうか？」
   (V 役職) 役職（伊藤様の文から）｜同じ名前の部分｜(V は) と同じ続く言葉＝3つに分けた形そのもの
録り方と確かめは make_ack_clips.take と同じ（g58_name_splice.good_take）。組んだ音も、同じ2通りの聞き方で読みが合うかを確かめる（記録に残す）。
⚠️「お間違えない」（✅ ユーザーの言い方）は、どの耳も「お間違いない」と書く（Gemini の2通り・Soniox とも、S2・L4 の4本とも。2026-10-25）＝
   声が「い」と言っているのか、耳が直して書くのかは機械では決められない ⇒ 「の」の形は「お間違いない」に聞こえた録音も通し、人に聞いていただく。
回帰と並べて流すときは --key-index を別にする（CL-1.md 0-2）。
"""
from __future__ import annotations

import asyncio
import json
import random

import numpy as np

from common import base_parser, make_client, pick_key, read_wav_pcm16_mono, require_python, save_result, say, write_wav
from g58_name_splice import OUT
from g65_seams import char_index, pause_after
from g66_role_seam import FEAT_S, delta, onset_s, pick_role, seam_features, take_with_times
from phrases import PROMISE_AGAIN, TARGET_CALL, TARGET_CONFIRM
from voice_features import features, loudness_db, pitch_at, semitones

ROLE = "営業部部長の"
NAME, OTHER, ROLE_NAME = "岡田", "中島", "伊藤"
FORMS = {"は": TARGET_CALL["法人"], "で": TARGET_CONFIRM, "と": PROMISE_AGAIN, "の": TARGET_CALL["個人"]}
ALSO = {"の": ("お間違えない", "お間違いない")}   # この形だけ、聞こえた読みが違っても通す（上の ⚠️）
HEAD_TAKES, TAIL_TAKES, ROLE_TAKES = 6, 5, 10   # 1回目は 4・3・5（L4 の役職は5本とも、文まるごとの変わり方から高さで 2.9半音以上ずれた）
GAP_WINDOW_S = 0.8             # 続く言葉の、助詞のあとの間を探す長さ（つなぎ目から）
# 助詞とその次の文字の間の上限（秒）。ユーザーが聞いた3つの点の間：0.12秒＝G-69 1回目の S2 (V は) の「は｜い」で ◎／0.16秒＝G-58 3回目の
# 「様で｜」で ○△「『岡田様でっ』のように聞こえ、後に間がある」／0.26秒＝G-69 1回目の S2 (V と) の「と｜お」で「間が長すぎる」
PARTICLE_GAP_S = 0.14
RATE_WEIGHT = 10.0             # 話す長さが同じ形の材料の中央から 10% 離れると、半音1つと同じ重さ（「少し早口」）
SHAPE_WEIGHT = 3.0             # 前後の1周期の形の近さ（相関）が 1 から 0.33 下がると、半音1つと同じ重さ
FRAME_S, HOP_S = 0.02, 0.005   # 「ま」を探す区間の長さと細かさ
SIDE_S = 0.025                 # つなぎ目の前後で比べる長さ
BAND = (1000.0, 4000.0)        # 鼻の音（「ま」の m）で弱まる帯
DIP_DB = 6.0                   # m の谷は、前後の母音より、帯の強さがこれだけ弱い
DROP_DB = 6.0                  # 母音の終わり：帯の強さが、母音の一番強い所からこれだけ下がった所（助詞の頭）
MIN_VOWEL_S = 0.025            # 切る所を選べる母音の短さの下限
TIME_SLACK_S = 0.08            # 言葉ごとの時刻（0.06秒きざみ）のずれの見込み：m は助詞の文字の始まり＋これより前
EDGES = np.geomspace(200.0, 6000.0, 21)   # 声質を比べる帯（20）
NFFT = 2048


def _spectrum(seg: np.ndarray, rate: int) -> tuple[np.ndarray, float]:
    p = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n=NFFT)) ** 2
    f = np.fft.rfftfreq(NFFT, 1 / rate)
    return (np.array([10 * np.log10(p[(f >= lo) & (f < hi)].sum() + 1e-9) for lo, hi in zip(EDGES, EDGES[1:])]),
            float(10 * np.log10(p[(f >= BAND[0]) & (f < BAND[1])].sum() + 1e-9)))


def contour(pcm: bytes, rate: int, a: float, b: float) -> dict:
    """a〜b 秒の HOP_S ごとの区間（FRAME_S）の：帯（BAND）の強さ dB・帯ごとの強さ・声帯の震え（声の高さが取れるか）。t は区間の真ん中。"""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    w = int(FRAME_S * rate)
    ts = np.arange(max(0.0, a), min(len(x) / rate - FRAME_S, b), HOP_S)
    shape, band, voiced = [], [], []
    for t in ts:
        s, bd = _spectrum(x[int(t * rate):int(t * rate) + w], rate)
        shape.append(s)
        band.append(bd)
        voiced.append(pitch_at(pcm, rate, t + FRAME_S / 2 - 0.022) is not None)
    return {"t": ts + FRAME_S / 2, "shape": np.array(shape), "band": np.array(band), "voiced": np.array(voiced, dtype=bool)}


def pulse_cut(pcm: bytes, rate: int, t: float, sign: int) -> tuple[int, int] | None:
    """t 秒に一番近い、声の1周期の決まった所：t を真ん中にした1周期の中の、向き sign（+1 か −1）の一番大きい山の、直前で波が0をまたぐ所
    （その向きへ上がっていく所）。返り値＝(標本の番号, 周期の標本数)。周期が取れなければ None。名前の部分と続く言葉を同じ向きの山でそろえて
    切る＝周期の同じ位置でつなぐ（⛔ 1回目は、どちらも「下から上へ0をまたぐ所」というだけで、周期の中の位置は録音ごとにばらばらだった）。"""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    hz = pitch_at(pcm, rate, t - 0.022)
    if not hz:
        return None
    period = int(rate / hz)
    a = max(1, int(t * rate) - period // 2)
    w = x[a:a + period] * sign
    if not len(w):
        return None
    k = a + int(np.argmax(w))
    j = next((j for j in range(k, max(1, k - period), -1) if x[j - 1] * sign < 0 <= x[j] * sign), None)
    return (j, period) if j is not None else None


def dominant_sign(pcm: bytes, rate: int, t: float) -> int:
    """t 秒のまわりの声で、大きい山が上向き（+1）か下向き（−1）か（名前の部分の向きに、続く言葉もそろえる）。"""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    seg = x[max(0, int((t - 0.01) * rate)):int((t + 0.01) * rate)]
    return 1 if seg.max() >= -seg.min() else -1


def ma_vowel(pcm: bytes, rate: int, times, head: str) -> dict | None:
    """「様」の「ま」の母音（あ）の、音の落ち着いている所（切る所）。head＝「岡田様」など（言葉ごとの時刻で「様」と次の文字の始まりを探す。
    その時刻は 0.06秒きざみで粗いので、音で決める）：「さ」の s（声帯の震えの無い区間）⇒ 最初の母音 ⇒ m の谷（帯の強さが前後の母音より DIP_DB
    弱い、最初の谷）⇒ 「ま」の母音（谷のあとの一番強い所から DROP_DB 下がるまで、または声帯の震えが止まるまで）。母音の真ん中の6割の中で、前後の
    区間の声質の変わり方が一番小さい所（t_steady）の、声の1周期の決まった所（pulse_cut。上向きの山・下向きの山の両方＝cuts）。cut は、その録音の
    大きい山の向きのほう。見つからなければ None。
    ⛔ 2026-10-25 の1回目：谷のうち一番深い所を m にしていた＝「で」の d・「の」の n・「は」の w（どれも帯の強さが下がる）を選び、助詞の母音で
       切った録音があった（「ま」の母音が助詞の文字の始まりより後ろに出た）⇒ 「さ」の母音のあとの最初の谷にし、助詞の文字の始まりより
       TIME_SLACK_S 以上後ろの谷は数えない。"""
    k = char_index(times, head)
    if k is None or k + 1 >= len(times):
        return None
    t0, t1 = times[k][1], times[k + 1][1]
    c = contour(pcm, rate, t0 - 0.12, t1 + 0.18)
    ts, band, voiced, shape = c["t"], c["band"], c["voiced"], c["shape"]
    # 「さ」の s：t0 の近くで、声帯の震えの無い区間（2つ以上続く）のあと、声が始まる所
    v0 = next((i for i in range(2, len(ts)) if t0 - 0.06 <= ts[i] <= t0 + 0.16 and voiced[i] and not voiced[i - 1] and not voiced[i - 2]), None)
    if v0 is None:
        return None
    m = None
    for i in range(v0 + 1, len(ts) - 1):
        if ts[i] > min(ts[v0] + 0.20, t1 + TIME_SLACK_S):
            break
        if ts[i] < ts[v0] + 0.03 or not voiced[i] or band[i] > band[i - 1] or band[i] > band[i + 1]:
            continue
        before, after = band[v0:i], band[i + 1:i + 1 + int(0.12 / HOP_S)]
        if len(before) and len(after) and before.max() - band[i] >= DIP_DB and after.max() - band[i] >= DIP_DB:
            m = i
            break
    if m is None:
        return None
    after = range(m + 1, min(len(ts), m + 1 + int(0.12 / HOP_S)))
    p = max(after, key=lambda i: band[i])
    a_s = next(i for i in range(m, p + 1) if band[i] >= band[p] - 3)
    a_e = next((i for i in range(p, len(ts)) if band[i] < band[p] - DROP_DB or not voiced[i]), len(ts) - 1)
    if ts[a_e] - ts[a_s] < MIN_VOWEL_S:
        return None
    lo, hi = a_s + int((a_e - a_s) * 0.2), a_s + int((a_e - a_s) * 0.8)
    mid = range(max(lo, 1), min(hi, len(ts) - 2) + 1)
    if not len(mid):
        return None
    i_cut = min(mid, key=lambda i: float(np.sqrt(((shape[i + 1] - shape[i - 1]) ** 2).mean())))
    steady = float(ts[i_cut])
    cuts = {s: pulse_cut(pcm, rate, steady, s) for s in (1, -1)}
    sign = dominant_sign(pcm, rate, steady)
    if cuts[sign] is None:
        return None
    cut, period = cuts[sign]
    return {"cut": cut, "sign": sign, "cuts": cuts, "period": period, "t": round(cut / rate, 4), "t_steady": round(steady, 4),
            "s_end": round(float(ts[v0]), 3), "m": round(float(ts[m]), 3), "vowel": (round(float(ts[a_s]), 3), round(float(ts[a_e]), 3)),
            "times": (t0, t1)}


def side(pcm: bytes, rate: int, cut: int, before: bool) -> dict:
    """つなぎ目の前（before）か後ろの SIDE_S の、声の高さ（Hz。つなぎ目に接する1区間）・大きさ（dB）・帯ごとの強さの形（平均を引いた dB）。"""
    t = cut / rate
    a, b = (t - SIDE_S, t) if before else (t, t + SIDE_S)
    seg = pcm[int(a * rate) * 2:int(b * rate) * 2]
    s, _ = _spectrum(np.frombuffer(seg, dtype="<i2").astype(np.float64), rate)
    win = 0.03 + 1 / 70.0   # pitch_at の区間の長さ（FRAME_S と、周期を探す長さ）
    return {"hz": pitch_at(pcm, rate, t - win if before else t), "db": loudness_db(seg), "shape": s - s.mean()}


def gap(a: dict, b: dict) -> dict:
    """2つの側の差：高さ（半音。どちらかが取れなければ None）・大きさ（dB）・声質（帯ごとの形の差の2乗平均 dB）。"""
    return {"高さ（半音）": round(abs(semitones(b["hz"], a["hz"])), 2) if a["hz"] and b["hz"] else None,
            "大きさ（dB）": round(abs(b["db"] - a["db"]), 2), "声質（dB）": round(float(np.sqrt(((b["shape"] - a["shape"]) ** 2).mean())), 2)}


def score(g: dict) -> float:
    """差を1つの数に（半音1つ＝大きさ3dB＝声質3dB を同じ重さに。高さが取れなければ半音3つとみなす＝選ばない側）。"""
    return (g["高さ（半音）"] if g["高さ（半音）"] is not None else 3.0) + g["大きさ（dB）"] / 3 + g["声質（dB）"] / 3


def natural(pcm: bytes, rate: int, cut: int) -> dict:
    """同じ録音の同じ所（切らずに続く声）の、前後の差＝自然な声の変わり方。"""
    return gap(side(pcm, rate, cut, True), side(pcm, rate, cut, False))


def _plan() -> list[tuple[str, str, int, str, tuple]]:
    """録る材料の一覧：(種類, 形, 本数, 文, 通す読み)。"""
    call = TARGET_CALL["法人"]
    out = [("head", "は", HEAD_TAKES, call.format(who=f"{NAME}様"), ())]
    out += [("tail", form, TAIL_TAKES, tmpl.format(who=f"{OTHER}様"), (tmpl.format(who=f"{OTHER}様").replace(*ALSO[form]),) if form in ALSO else ())
            for form, tmpl in FORMS.items()]
    out += [("role", "は", ROLE_TAKES, call.format(who=f"{ROLE}{ROLE_NAME}様"), ()), ("whole_role", "は", 1, call.format(who=f"{ROLE}{NAME}様"), ()),
            ("whole_de", "で", 1, TARGET_CONFIRM.format(who=f"{NAME}様"), ())]
    return out


async def record(args, out) -> list[dict]:
    """材料を録って、takes/ に保存する（音と、言葉ごとの時刻と、録ったときの記録）。"""
    from common import load_secret
    client = make_client(pick_key(args))
    key = load_secret("SONIOX_API_KEY", args.env)
    (out / "takes").mkdir(parents=True, exist_ok=True)
    rows = []
    for kind, form, n, text, also in _plan():
        name = NAME if kind in ("head", "whole_role", "whole_de") else ROLE_NAME if kind == "role" else OTHER
        head = f"{ROLE}{name}様" if kind in ("role", "whole_role") else f"{name}様"
        for j in range(n):
            pcm, rate, times, _, row = await take_with_times(
                client, args, key, text, lambda p, r, t: True if char_index(t, head) is not None else None, also)
            wav = f"{kind}_{form}_{j + 1}.wav"
            write_wav(out / "takes" / wav, pcm, rate)
            rows.append({"kind": kind, "form": form, "text": text, "head": head, "wav": wav, "rate": rate, "words": row["words"],
                         "row": {k: v for k, v in row.items() if k != "words"}})
            say("録った", wav, row.get("heard_as"))
    (out / "takes" / "takes.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def _period_corr(a_pcm: bytes, a_cut: int, b_pcm: bytes, b_cut: int, period: int) -> float:
    """つなぎ目の前の1周期（a の a_cut まで）と後ろの1周期（b の b_cut から）の形の近さ（相関。−1〜1）。"""
    a = np.frombuffer(a_pcm, dtype="<i2")[a_cut - period:a_cut].astype(np.float64)
    b = np.frombuffer(b_pcm, dtype="<i2")[b_cut:b_cut + period].astype(np.float64)
    if a_cut < period or len(a) < period or len(b) < period or not a.std() or not b.std():
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def odd_gaps_after(pcm: bytes, rate: int, cut: int, times, head: str) -> list[float]:
    """続く言葉の、助詞とその次の文字の間の、PARTICLE_GAP_S より長い間（秒）。（✅ ユーザー：「『岡田様と』の後の間が長すぎる」）
    数えるのは、つなぎ目から次の文字の始まり（言葉ごとの時刻＋その粗さ）までに始まる間だけ。⛔ つなぎ目から GAP_WINDOW_S の間を全部数えると、
    「いらっしゃい」の「っ」の自然な間（0.12秒）で、ユーザーが ◎ と聞いた材料（G-69 1回目の S2 の (V は)）まで落とした。"""
    from barge import voice_runs, voiced
    k = char_index(times, head)
    if k is None or k + 2 >= len(times):
        return []
    until = times[k + 2][1] + TIME_SLACK_S - cut / rate   # つなぎ目からの秒
    runs = voice_runs(voiced(pcm[cut * 2:(cut + int(GAP_WINDOW_S * rate)) * 2], rate), 0.01)
    return [round(b[0] - a[1], 3) for a, b in zip(runs, runs[1:]) if b[0] - a[1] > PARTICLE_GAP_S and a[1] <= until]


def speech_after(pcm: bytes, rate: int, cut: int) -> float:
    """つなぎ目から声の終わりまでの長さ（秒）＝同じ形の材料どうしで話す速さを比べる（✅ ユーザー：「少し早口」）。"""
    from barge import voice_runs, voiced
    runs = voice_runs(voiced(pcm[cut * 2:], rate), 0.05)
    return runs[-1][1] if runs else 0.0


async def build(args, out, rows: list[dict]) -> dict:
    """保存した材料から、切る所を決め、組を選んで組み、聞き比べの9本を作る。"""
    from name_clips import odd_gaps
    client = make_client(pick_key(args))
    notes: dict = {"切れなかった材料": [], "使わなかった材料": {}}
    mats: dict[str, list] = {}
    rate = None
    for r in rows:
        pcm, rate = read_wav_pcm16_mono(out / "takes" / r["wav"])
        times = [tuple(w) for w in r["words"]]
        m = {"pcm": pcm, "times": times, "row": r, "wav": r["wav"]}
        if r["kind"] in ("head", "tail"):
            mv = ma_vowel(pcm, rate, times, r["head"].replace(ROLE, ""))
            if mv is None:
                notes["切れなかった材料"].append(r["wav"])
                continue
            m.update({"mv": mv, "natural": natural(pcm, rate, mv["cut"]), "whole": features(pcm, rate),
                      "natural_shape": round(_period_corr(pcm, mv["cut"], pcm, mv["cut"], mv["period"]), 2)})
            if r["kind"] == "head":
                gaps = odd_gaps(pcm, rate, mv["cut"], times, "")
                m["before"] = side(pcm, rate, mv["cut"], True)
            else:
                gaps = odd_gaps_after(pcm, rate, mv["cut"], times, r["head"])
                m["by_sign"] = {s: c and {"cut": c[0], "after": side(pcm, rate, c[0], False)} for s, c in mv["cuts"].items()}
                m["speech_s"] = speech_after(pcm, rate, mv["cut"])
            if gaps:
                notes["使わなかった材料"][r["wav"]] = {"不自然な間（秒）": gaps}
                continue
        mats.setdefault(r["kind"] if r["kind"] != "tail" else f"tail_{r['form']}", []).append(m)
    heads = mats.get("head", [])
    tails = {form: mats.get(f"tail_{form}", []) for form in FORMS}
    lacking = [k for k, v in {"名前の部分": heads, **{f"続く言葉（{f}）": t for f, t in tails.items()}}.items() if not v]
    if lacking:
        raise RuntimeError(f"使える材料がありません：{lacking}（{notes}）")
    median_s = {form: float(np.median([t["speech_s"] for t in ts])) for form, ts in tails.items()}

    def pair(h, t, form):
        """名前の部分 h と続く言葉 t（h と同じ向きの山で切る）の、つなぎ目の差と、組のずれ（小さいほど合う）。組めなければ None。"""
        ts = t["by_sign"].get(h["mv"]["sign"])
        if not ts:
            return None
        g = gap(h["before"], ts["after"])
        shape = round(_period_corr(h["pcm"], h["mv"]["cut"], t["pcm"], ts["cut"], h["mv"]["period"]), 2)
        speed = round(abs(t["speech_s"] - median_s[form]) / median_s[form], 3) if median_s[form] else 0.0
        return score(g) + SHAPE_WEIGHT * (1 - shape) + RATE_WEIGHT * speed, {**g, "前後の1周期の形": shape, "話す長さの中央からの離れ": speed}, ts["cut"]

    # 組を選ぶ：名前の部分ごとに、形ごとに一番合う続く言葉を選び、4つの形のうち一番合わない形のずれが一番小さい名前の部分
    def plan(h):
        best = {}
        for form, ts in tails.items():
            gs = [(p[0], k, p[1], p[2]) for k, t in enumerate(ts) if (p := pair(h, t, form))]
            if not gs:
                return 999.0, {}
            best[form] = min(gs, key=lambda g: g[0])
        return max(s for s, _, _, _ in best.values()), best
    plans = [(plan(h), i) for i, h in enumerate(heads)]
    (worst, best), hi = min(plans, key=lambda p: p[0][0])
    if not best:
        raise RuntimeError("同じ向きの山で切れる組がありません")
    h = heads[hi]
    notes["名前の部分の候補"] = [{"材料": x["wav"], "切る所": {k: v for k, v in x["mv"].items() if k not in ("cut", "cuts")},
                             "自然な変わり方": x["natural"], "自然な1周期の形": x["natural_shape"], "一番合わない形のずれ": round(p[0], 2)}
                            for (p, _), x in zip(plans, heads)]
    notes["続く言葉の候補"] = {f: [{"材料": t["wav"], "切る所": {k: v for k, v in t["mv"].items() if k not in ("cut", "cuts")},
                                "自然な変わり方": t["natural"], "自然な1周期の形": t["natural_shape"], "話す長さ（秒）": round(t["speech_s"], 3),
                                "聞こえた読み": t["row"]["row"].get("heard_as")} for t in ts] for f, ts in tails.items()}
    notes["選んだ名前の部分"] = h["wav"]
    notes["選んだ続く言葉"] = {form: {"材料": tails[form][k]["wav"], "つなぎ目の差": g, "ずれ": round(s, 2),
                                  "全体の高さの差（半音）": round(abs(semitones(tails[form][k]["whole"]["pitch_hz"], h["whole"]["pitch_hz"])), 2)
                                  if h["whole"] and tails[form][k]["whole"] else None}
                           for form, (s, k, g, _) in best.items()}
    head_pcm = h["pcm"][:h["mv"]["cut"] * 2]
    joined = {form: head_pcm + tails[form][k]["pcm"][cut * 2:] for form, (_, k, _, cut) in best.items()}

    # (V 役職)：役職の材料のうち、文まるごとの変わり方に一番そろうもの（G-66 と同じ）
    wr = mats["whole_role"][0]
    pw = pause_after(wr["pcm"], rate, wr["times"], ROLE)
    if pw is None:
        raise RuntimeError("文まるごと（役職つき）に、役職のあとの間が見つかりません")
    target = delta(seam_features(wr["pcm"], rate, pw[0] - FEAT_S, pw[0]), seam_features(wr["pcm"], rate, pw[1], pw[1] + FEAT_S))
    roles = []
    for ro in mats.get("role", []):
        px = pause_after(ro["pcm"], rate, ro["times"], ROLE)
        fe = px and seam_features(ro["pcm"], rate, px[0] - FEAT_S, px[0])
        if fe:
            roles.append({"pcm": ro["pcm"], "pause": px, "end": fe, "wav": ro["wav"]})
    role_pcm, notes["V 役職"] = pick_role(roles, target, h["pcm"], rate, 0.0, onset_s(h["pcm"], rate))
    notes["V 役職"]["文まるごとの変わり方"] = {k: round(v, 2) for k, v in target.items()}

    call = TARGET_CALL["法人"]
    raw = {form: tails[form][best[form][1]] for form in ("は", "と")}
    items = [{"kind": "(W は) まるごと（名前の部分を切り出した録音そのもの）", "text": call.format(who=f"{NAME}様"), "pcm": h["pcm"]},
             {"kind": "(V は) 名前｜「ま」の母音の真ん中から「は」の続く言葉", "text": call.format(who=f"{NAME}様"), "pcm": joined["は"]},
             {"kind": f"(M は) (V は) の続く言葉の材料そのもの（{raw['は']['wav']}）", "text": raw["は"]["row"]["text"], "pcm": raw["は"]["pcm"]},
             {"kind": "(W で) まるごと", "text": TARGET_CONFIRM.format(who=f"{NAME}様"), "pcm": mats["whole_de"][0]["pcm"]},
             {"kind": "(V で) 同じ名前の部分｜「で」の続く言葉", "text": TARGET_CONFIRM.format(who=f"{NAME}様"), "pcm": joined["で"]},
             {"kind": "(V と) 同じ名前の部分｜「と」の続く言葉（約束の一言）", "text": PROMISE_AGAIN.format(who=f"{NAME}様"), "pcm": joined["と"]},
             {"kind": f"(M と) (V と) の続く言葉の材料そのもの（{raw['と']['wav']}）", "text": raw["と"]["row"]["text"], "pcm": raw["と"]["pcm"]},
             {"kind": "(V の) 同じ名前の部分｜「の」の続く言葉（個人の呼ぶ一言）", "text": TARGET_CALL["個人"].format(who=f"{NAME}様"), "pcm": joined["の"]},
             {"kind": "(V 役職) 役職｜同じ名前の部分｜(V は) の続く言葉", "text": call.format(who=f"{ROLE}{NAME}様"), "pcm": role_pcm + joined["は"]}]
    from make_ack_clips import hear_feeds, heard_ok
    for it in items:
        if it["kind"].startswith("(V"):
            heard = await hear_feeds(client, it["pcm"], rate)
            alts = (it["text"],) + ((it["text"].replace(*ALSO["の"]),) if "(V の)" in it["kind"] else ())
            it["耳"] = {"heard": heard, "読みが合う": next((a for a in alts if heard_ok(a, heard)), None)}
            say(it["kind"], it["耳"])
    random.Random(args.seed).shuffle(items)
    index = []
    for k, it in enumerate(items, 1):
        wav = f"{k:02d}.wav"
        write_wav(out / wav, it["pcm"], rate)
        index.append({"wav": wav, "kind": it["kind"], "text": it["text"], **({"耳": it["耳"]} if "耳" in it else {})})
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    say("作った：", ", ".join(f"{args.type}/{x['wav']}" for x in index))
    return {"model": args.model, "type": args.type, "round": args.round, "items": index, "measures": notes}


async def run(args) -> dict:
    out = OUT / f"round{args.round}" / args.type
    out.mkdir(parents=True, exist_ok=True)
    if args.reuse:
        rows = json.loads((out / "takes" / "takes.json").read_text(encoding="utf-8"))
    else:
        rows = await record(args, out)
    return await build(args, out, rows)


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--type", default="S2")
    p.add_argument("--tries", type=int, default=8)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=69)
    p.add_argument("--reuse", action="store_true", help="録らずに、保存した材料（takes/）から組み直す")
    p.add_argument("--round", type=int, default=9, help="置き場所の回（results/name_splice/round{回}）。1回目は 8")
    args = p.parse_args()
    save_result("G-69", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
