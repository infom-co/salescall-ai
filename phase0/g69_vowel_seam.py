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
声ごとに8本（results/name_splice/round8/{声の型}/ の 01〜08。どれがどれかは index.json。聞く前に開かない）：
   (W は) 文まるごと「岡田様はいらっしゃいますでしょうか？」（名前の部分を切り出した録音そのもの）
   (V は)「岡田さ（あ）」｜「（あ）はいらっしゃいますでしょうか？」（中島様の文から）
   (C は) 同じ2本を、G-65 の (P0) と同じ「様｜は」の一番静かな所で切ってつなぐ（切る所だけを変えた比べ）
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
from g65_seams import char_index, pause_after, wa_evidence
from g66_role_seam import FEAT_S, delta, onset_s, pick_role, seam_features, take_with_times
from phrases import PROMISE_AGAIN, TARGET_CALL, TARGET_CONFIRM
from voice_features import features, loudness_db, pitch_at, semitones

ROLE = "営業部部長の"
NAME, OTHER, ROLE_NAME = "岡田", "中島", "伊藤"
FORMS = {"は": TARGET_CALL["法人"], "で": TARGET_CONFIRM, "と": PROMISE_AGAIN, "の": TARGET_CALL["個人"]}
ALSO = {"の": ("お間違えない", "お間違いない")}   # この形だけ、聞こえた読みが違っても通す（上の ⚠️）
HEAD_TAKES, TAIL_TAKES, ROLE_TAKES = 4, 3, 5
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


def _upward_zero(pcm: bytes, rate: int, t: float) -> int:
    """t 秒に一番近い、音の波が下から上へ0をまたぐ所（標本の番号。±5ms の中。無ければ t のまま）。"""
    x = np.frombuffer(pcm, dtype="<i2")
    i = int(t * rate)
    lo, hi = max(1, i - int(0.005 * rate)), min(len(x) - 1, i + int(0.005 * rate))
    up = [j for j in range(lo, hi) if x[j - 1] < 0 <= x[j]]
    return min(up, key=lambda j: abs(j - i)) if up else i


def ma_vowel(pcm: bytes, rate: int, times, head: str) -> dict | None:
    """「様」の「ま」の母音（あ）の、音の落ち着いている所（切る所）。head＝「岡田様」など（言葉ごとの時刻で「様」と次の文字の始まりを探す。
    その時刻は 0.06秒きざみで粗いので、音で決める）：「さ」の s（声帯の震えの無い区間）⇒ 最初の母音 ⇒ m の谷（帯の強さが前後の母音より DIP_DB
    弱い、最初の谷）⇒ 「ま」の母音（谷のあとの一番強い所から DROP_DB 下がるまで、または声帯の震えが止まるまで）。母音の真ん中の6割の中で、前後の
    区間の声質の変わり方が一番小さい所を、音の波が下から上へ0をまたぐ所に合わせる。見つからなければ None。
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
    cut = _upward_zero(pcm, rate, float(ts[i_cut]))
    return {"cut": cut, "t": round(cut / rate, 4), "s_end": round(float(ts[v0]), 3), "m": round(float(ts[m]), 3),
            "vowel": (round(float(ts[a_s]), 3), round(float(ts[a_e]), 3)), "times": (t0, t1)}


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


async def build(args, out, rows: list[dict]) -> dict:
    """保存した材料から、切る所を決め、組を選んで組み、聞き比べの8本を作る。"""
    client = make_client(pick_key(args))
    notes: dict = {"切れなかった材料": []}
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
            m.update({"mv": mv, "before": side(pcm, rate, mv["cut"], True), "after": side(pcm, rate, mv["cut"], False),
                      "natural": natural(pcm, rate, mv["cut"]), "whole": features(pcm, rate)})
        mats.setdefault(r["kind"] if r["kind"] != "tail" else f"tail_{r['form']}", []).append(m)
    heads = mats.get("head", [])
    tails = {form: mats.get(f"tail_{form}", []) for form in FORMS}
    lacking = [k for k, v in {"名前の部分": heads, **{f"続く言葉（{f}）": t for f, t in tails.items()}}.items() if not v]
    if lacking:
        raise RuntimeError(f"切れる材料がありません：{lacking}（{notes['切れなかった材料']}）")

    # 組を選ぶ：名前の部分ごとに、形ごとに一番合う続く言葉を選び、4つの形のうち一番合わない形のずれが一番小さい名前の部分
    def plan(h):
        best = {}
        for form, ts in tails.items():
            gs = [(score(gap(h["before"], t["after"])), k, gap(h["before"], t["after"])) for k, t in enumerate(ts)]
            best[form] = min(gs, key=lambda g: g[0])
        return max(s for s, _, _ in best.values()), best
    plans = [(plan(h), i) for i, h in enumerate(heads)]
    (worst, best), hi = min(plans, key=lambda p: p[0][0])
    h = heads[hi]
    notes["名前の部分の候補"] = [{"材料": x["wav"], "切る所": {k: v for k, v in x["mv"].items() if k != "cut"}, "自然な変わり方": x["natural"],
                             "一番合わない形のずれ": round(p[0], 2)} for (p, _), x in zip(plans, heads)]
    notes["続く言葉の候補"] = {f: [{"材料": t["wav"], "切る所": {k: v for k, v in t["mv"].items() if k != "cut"}, "自然な変わり方": t["natural"],
                                "聞こえた読み": t["row"]["row"].get("heard_as")} for t in ts] for f, ts in tails.items()}
    notes["選んだ名前の部分"] = h["wav"]
    notes["選んだ続く言葉"] = {form: {"材料": tails[form][k]["wav"], "つなぎ目の差": g, "ずれ": round(s, 2),
                                  "全体の高さの差（半音）": round(abs(semitones(tails[form][k]["whole"]["pitch_hz"], h["whole"]["pitch_hz"])), 2)
                                  if h["whole"] and tails[form][k]["whole"] else None}
                           for form, (s, k, g) in best.items()}
    head_pcm = h["pcm"][:h["mv"]["cut"] * 2]
    joined = {form: head_pcm + tails[form][k]["pcm"][tails[form][k]["mv"]["cut"] * 2:] for form, (_, k, _) in best.items()}

    # (C は)：同じ2本を「様｜は」の一番静かな所で（G-65 の (P0) と同じ切り方）
    t_wa = tails["は"][best["は"][1]]
    wa_h, wa_t = wa_evidence(h["pcm"], rate, h["times"], f"{NAME}様"), wa_evidence(t_wa["pcm"], rate, t_wa["times"], f"{OTHER}様")
    control = h["pcm"][:wa_h["cut"] * 2] + t_wa["pcm"][wa_t["cut"] * 2:]
    notes["C は"] = {"名前の文": {k: v for k, v in wa_h.items() if k != "cut"}, "続く言葉の文": {k: v for k, v in wa_t.items() if k != "cut"},
                    "つなぎ目の差": gap(side(h["pcm"], rate, wa_h["cut"], True), side(t_wa["pcm"], rate, wa_t["cut"], False))}

    # (V 役職)：役職の材料のうち、文まるごとの変わり方に一番そろうもの（G-66 と同じ）
    wr = mats["whole_role"][0]
    pw = pause_after(wr["pcm"], rate, wr["times"], ROLE)
    target = delta(seam_features(wr["pcm"], rate, pw[0] - FEAT_S, pw[0]), seam_features(wr["pcm"], rate, pw[1], pw[1] + FEAT_S))
    roles = []
    for ro in mats.get("role", []):
        px = pause_after(ro["pcm"], rate, ro["times"], ROLE)
        fe = px and seam_features(ro["pcm"], rate, px[0] - FEAT_S, px[0])
        if fe:
            roles.append({"pcm": ro["pcm"], "pause": px, "end": fe, "wav": ro["wav"]})
    role_pcm, notes["V 役職"] = pick_role(roles, target, h["pcm"], rate, 0.0, onset_s(h["pcm"], rate))

    call = TARGET_CALL["法人"]
    items = [{"kind": "(W は) まるごと（名前の部分を切り出した録音そのもの）", "text": call.format(who=f"{NAME}様"), "pcm": h["pcm"]},
             {"kind": "(V は) 名前｜「ま」の母音の真ん中から「は」の続く言葉", "text": call.format(who=f"{NAME}様"), "pcm": joined["は"]},
             {"kind": "(C は) 同じ2本を「様｜は」の一番静かな所で（G-65 の P0 の切り方）", "text": call.format(who=f"{NAME}様"), "pcm": control},
             {"kind": "(W で) まるごと", "text": TARGET_CONFIRM.format(who=f"{NAME}様"), "pcm": mats["whole_de"][0]["pcm"]},
             {"kind": "(V で) 同じ名前の部分｜「で」の続く言葉", "text": TARGET_CONFIRM.format(who=f"{NAME}様"), "pcm": joined["で"]},
             {"kind": "(V と) 同じ名前の部分｜「と」の続く言葉（約束の一言）", "text": PROMISE_AGAIN.format(who=f"{NAME}様"), "pcm": joined["と"]},
             {"kind": "(V の) 同じ名前の部分｜「の」の続く言葉（個人の呼ぶ一言）", "text": TARGET_CALL["個人"].format(who=f"{NAME}様"), "pcm": joined["の"]},
             {"kind": "(V 役職) 役職｜同じ名前の部分｜(V は) の続く言葉", "text": call.format(who=f"{ROLE}{NAME}様"), "pcm": role_pcm + joined["は"]}]
    from make_ack_clips import hear_feeds, heard_ok
    for it in items:
        if it["kind"].startswith(("(V", "(C")):
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
    return {"model": args.model, "type": args.type, "round": 8, "items": index, "measures": notes}


async def run(args) -> dict:
    out = OUT / "round8" / args.type
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
    args = p.parse_args()
    save_result("G-69", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
