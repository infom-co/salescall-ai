"""G-66：役職の部分と名前の部分を分けて組み合わせても自然か（DESIGN.md 4-3 の名前の録音の台帳。CL-2 の連絡帳 4-156 の「役職の部分」）。

G-65 で分かったこと（✅ 2026-10-25 ユーザーの聞き比べ。PHASE0 G-65）：名前の部分は助詞を含めて息の音まで、形ごとに持つ（「様｜は」・「〇〇様」
1つの使い回しは使えない）。残る問いは、役職の部分を分けられるか（分けられなければ、役職と名前の組ごとに録音が要る）。G-65 の役職と名前の
間は「声質が違う」「途切れる」だった。直すこと：
   (1) 声の頭を削らない（⛔ G-65 の試しの道具は g58_name_splice.trim で、声の 0.03秒前までしか残さず、弱く始まる「お」の立ち上がりを削っていた）
       ⇒ 声の前後は、とても小さい音（LEAD_DB）まで残す（LEAD_S）。文の途中では、間の一番静かな所で切る
   (2) 役職と名前の間の長さは、役職の文のもとの間のまま（役職の側を、名前の側の頭に残った無音の分だけ短く切る）
   (3) 役職の録音を、名前の部分と声の合うものを選ぶ：つなぎ目の前（役職の終わり）とあと（名前の始まり）の、高さ・大きさ・明るさの差が、
       文まるごとの録音の同じ所の差に一番近い組（文まるごとでも、「部長の」のあとで声は変わる＝その変わり方にそろえる）
声ごとに3本（results/name_splice/round7/{声の型}/ の 01〜03。どれがどれかは index.json。聞く前に開かない）：
   (W)  文まるごと「営業部部長の岡田様はいらっしゃいますでしょうか？」
   (R1) 役職（伊藤様の文から。--role-name）｜「岡田様はいらっ」（役職の無い文から）｜「しゃいますでしょうか？」（中島様の文から）
   (R2) 役職｜「岡田様はいらっ」（「課長の岡田様は…」の文の、のあとの間から）｜「しゃいますでしょうか？」
録り方と確かめは make_ack_clips.take と同じ（g58_name_splice.good_take）。回帰と並べて流すときは --key-index を別にする（CL-1.md 0-2）。
"""
from __future__ import annotations

import asyncio
import json
import random

import numpy as np

from common import base_parser, make_client, pick_key, require_python, save_result, say, write_wav
from g58_name_splice import OUT, good_take
from g65_seams import char_index, pause_after, quiet_point
from phrases import TARGET_CALL

ROLE = "営業部部長の"
NAME, OTHER, CARRIER_ROLE = "岡田", "中島", "課長の"
# 役職の録音に入れる名前（--role-name）。⛔ 2026-10-25 の1回目：「山田」で録ったら、L4 は8本とも「部長の」から「やまだ」へ声が切れずに続き
#    （や＝声の続く音）、声帯の震えの無い区間が無くて切れなかった（文まるごとの「岡田」は母音で始まり、その前で声が止まる）⇒ 母音で始まる「伊藤」に
ROLE_NAME_DEFAULT = "伊藤"
LEAD_DB, LEAD_S = -60.0, 0.15   # 声の前後に残す：一番大きい所からこの dB より大きい音は全部・その外は LEAD_S まで
FEAT_S = 0.35                  # つなぎ目の前後の声を比べる長さ
ROLE_TAKES = 5                 # 役職の録音を、この本数まで録って選ぶ


def gentle_trim(pcm: bytes, rate: int) -> bytes:
    """声の前後の無音だけを切る（とても小さい音＝LEAD_DB より大きい所は残し、その外は LEAD_S まで残す）。弱く始まる声の立ち上がりを削らない。"""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    w = int(0.005 * rate)
    if len(x) < w * 4:
        return pcm
    rms = np.sqrt((x[: len(x) // w * w].reshape(-1, w) ** 2).mean(axis=1)) + 1e-9
    db = 20 * np.log10(rms / rms.max())
    loud = np.nonzero(db > LEAD_DB)[0]
    if not len(loud):
        return pcm
    a = max(0, int(loud[0] * w - LEAD_S * rate))
    b = min(len(x), int((loud[-1] + 1) * w + LEAD_S * rate))
    return pcm[a * 2:b * 2]


def onset_s(pcm: bytes, rate: int) -> float:
    """声の始まり（はっきりした声＝一番大きい所から −40 dB を超える最初の所）の秒。"""
    from barge import voice_runs, voiced
    runs = voice_runs(voiced(pcm, rate), 0.05)
    return runs[0][0] if runs else 0.0


def seam_features(pcm: bytes, rate: int, a: float, b: float) -> dict | None:
    from voice_features import features, loudness_db
    seg = pcm[int(max(0.0, a) * rate) * 2:int(b * rate) * 2]
    f = features(seg, rate)
    return {"hz": f["pitch_hz"], "db": loudness_db(seg), "bright": f["brightness_hz"]} if f else None


def delta(before: dict, after: dict) -> dict:
    from voice_features import semitones
    return {"st": semitones(after["hz"], before["hz"]), "db": after["db"] - before["db"], "bright": after["bright"] - before["bright"]}


def mismatch(d: dict, target: dict) -> float:
    """つなぎ目の前後の変わり方が、文まるごとの変わり方からどれだけ離れているか（半音1つ＝大きさ3dB＝明るさ300Hz を同じ重さに）。"""
    return abs(d["st"] - target["st"]) + abs(d["db"] - target["db"]) / 3 + abs(d["bright"] - target["bright"]) / 300


# 役職の側の切る所（残す間の終わり）の直前 STEP_S の大きさが、名前の部分の頭（声の始まりまで）より、これだけ大きい録音は、ほかに無いときだけ選ぶ。
# ⛔ 2026-10-25 G-69 の2回目（S2 の (V 役職) ○△「『岡田様』の前に、雑音か前の言葉が入っている。途切れにも聞こえる」）：選んだ役職の録音の
#    「の」のあとの間（声帯の震えの無い区間＝pause_after）は 0.06秒で、静かにならないまま（一番静かな所でも録音の一番大きい所から −48.5 dB。
#    ほかの録音は −53〜−81 dB）次の「伊藤」の「い」へつながっていた。その音を 0.04秒残して名前の部分の頭の無音を足したので、切る所で
#    +9.3 dB の段差になった（ご指摘の無かった組は −9.4〜−23.9 dB）
ROLE_STEP_DB = 3.0
STEP_S = 0.01
# 上限を超えた組に足すずれ：100＋超えた量÷unit×10（ほかに組が無いときだけ選ばれ、そのときは超えた量の小さい組から。⛔ 2026-10-25 の3回目：どれも
# 同じ +100 にしたので、L4 の「で」で、声質の差 6.39 dB の組より 12.84 dB の組を選んだ）。役職の切る所の段差・録音全体の高さの差と、
# g69_vowel_seam の声質の差・話す速さのずれは、みなこの足し方（G-66・G-69 の唯一の場所）
OVER = 100.0


def over_by(value: float, limit: float, unit: float) -> float:
    """上限 limit を超えた組に足すずれ：OVER＋（超えた量÷unit）×10。超えていなければ 0（上の OVER の説明）。"""
    return OVER + (value - limit) / unit * 10.0 if value > limit else 0.0


def pitch_over(a: dict | None, b: dict | None) -> tuple[float | None, float]:
    """2つの録音全体（voice_features.features）の声の高さの差（半音）と、上限（name_clips.MAX_ST＝半音 2.4。G-58 の4回目は半音3つ・G-69 の5回目までのご評価で締めた）を
    超えた分のずれ。どちらかの高さが読めなければ (None, OVER)。役職｜名前の部分と、名前の部分｜続く言葉の両方の唯一の決まり。"""
    from name_clips import MAX_ST
    from voice_features import semitones
    if not (a and b and a.get("pitch_hz") and b.get("pitch_hz")):
        return None, OVER
    st = round(abs(semitones(b["pitch_hz"], a["pitch_hz"])), 2)
    return st, over_by(st, MAX_ST, 1.0)


def _db(pcm: bytes, rate: int, a: float, b: float) -> float:
    x = np.frombuffer(pcm[max(0, int(a * rate)) * 2:int(b * rate) * 2], dtype="<i2").astype(np.float64)
    return float(20 * np.log10(np.sqrt((x ** 2).mean()) + 1e-9)) if len(x) else -999.0


def pick_role(roles: list[dict], target: dict, y: bytes, rate: int, start_q: float, lead: float) -> tuple[bytes, dict]:
    """名前の部分 y（start_q 秒から。声の始まりまで lead 秒）に、つなぎ目の前後の声の変わり方が文まるごとの変わり方（target）に一番そろう
    役職の録音（roles の1つ＝{pcm, pause, end}）を選び、間をもとの長さにして、役職の側の音を返す（G-66・G-69 の唯一の場所）。
    役職の側の切る所が、名前の部分の頭より ROLE_STEP_DB 以上大きい録音（間が静かにならない録音）と、録音全体の声の高さが名前の部分の録音と
    MAX_ST より離れた録音は、ほかに無いときだけ選ぶ（pitch_over）。⛔ 2026-10-28 G-69 の4回目（S2 の (V 役職) ○「『営業部部長の』と『岡田様』
    以降の声質が違う」）：選んだ役職の録音は全体で 180 Hz・名前の部分の録音は 148 Hz（3.4半音）で、この決まりを役職には当てていなかった"""
    from voice_features import features
    o = start_q + lead
    fs = seam_features(y, rate, o, o + FEAT_S)
    floor = _db(y, rate, start_q, start_q + max(lead, STEP_S))   # 名前の部分の頭（声の始まりまで）の大きさ
    fy = features(y, rate)
    whole = [pitch_over(fy, features(ro["pcm"], rate)) for ro in roles]

    def cut_at(ro) -> tuple[float, float]:
        a, b = ro["pause"]
        keep = max(0.02, (b - a) - lead)               # 役職の側に残す間（名前の側の頭の無音と合わせて、もとの間になる）
        return keep, min(b, a + keep)

    def step(ro) -> float:
        return _db(ro["pcm"], rate, cut_at(ro)[1] - STEP_S, cut_at(ro)[1]) - floor
    scored = (sorted(((mismatch(delta(ro["end"], fs), target) + over_by(step(ro), ROLE_STEP_DB, 1.0) + whole[k][1], k)
                      for k, ro in enumerate(roles)), key=lambda t: t[0]) if fs else [(None, 0)])
    sc, k = scored[0]
    ro = roles[k]
    a, b = ro["pause"]
    keep, end = cut_at(ro)
    cut = int(end * rate)
    return ro["pcm"][:cut * 2], {"役職の録音": k + 1, "変わり方のずれ": sc and round(sc, 2), "役職のもとの間（秒）": round(b - a, 3),
                                 "名前の頭の無音（秒）": round(lead, 3), "役職の側に残した間（秒）": round(keep, 3),
                                 "切る所の段差（dB）": round(step(ro), 1), "全体の高さの差（半音）": whole[k][0],
                                 "役職の候補の全体の高さの差（半音）": [w for w, _ in whole],
                                 "役職の候補のずれ": [round(s, 2) for s, _ in scored if s is not None]}


async def take_with_times(client, args, key: str, text: str, check, also: tuple[str, ...] = ()):
    """確かめに通り、check（音・速さ・時刻 ⇒ 値。無ければ None）が値を返す録音（声の頭を削らない）。(音, 速さ, 時刻, 値, 記録)。
    also：この読みに聞こえても通す文（g58_name_splice.good_take）。"""
    from ears import soniox_word_times
    for _ in range(args.tries):
        pcm, rate, row = await good_take(client, args, args.type, "whole", text, also)
        pcm = gentle_trim(pcm, rate)
        times = await soniox_word_times(key, pcm, rate)
        row["words"] = [(t, round(a, 3), round(b, 3)) for t, a, b in times]
        got = check(pcm, rate, times)
        if got is not None:
            return pcm, rate, times, got, row
        say("切れ目が見つからなかった：", text, [t for t, _, _ in times])
    raise RuntimeError(f"切れ目の見つかる録音が作れなかった：{text}")


async def run(args) -> dict:
    from common import load_secret
    from name_clips import head_ok, measure_head, seam_cut, tail_from
    client = make_client(pick_key(args))
    key = load_secret("SONIOX_API_KEY", args.env)
    out = OUT / "round7" / args.type
    out.mkdir(parents=True, exist_ok=True)
    call = TARGET_CALL["法人"]
    notes: dict = {}

    # 文まるごと：つなぎ目（「の」のあとの間）の前後の声の変わり方を、組み合わせのそろえる先にする
    w, rate, _, pause_w, row_w = await take_with_times(client, args, key, call.format(who=f"{ROLE}{NAME}様"), lambda p, r, t: pause_after(p, r, t, ROLE))
    fw_end, fw_start = seam_features(w, rate, pause_w[0] - FEAT_S, pause_w[0]), seam_features(w, rate, pause_w[1], pause_w[1] + FEAT_S)
    target = delta(fw_end, fw_start)
    notes["文まるごとの変わり方"] = {k: round(v, 2) for k, v in target.items()}
    notes["文まるごとの間（秒）"] = round(pause_w[1] - pause_w[0], 3)

    # 続く言葉：中島様の文の、息の音の継ぎ目より後ろ（G-58 の4回目＝◎）
    z, rz, times_z, tail_b, row_z = await take_with_times(client, args, key, call.format(who=f"{OTHER}様"), lambda p, r, t: tail_from(p, r, t))
    assert rz == rate

    # 役職の録音を ROLE_TAKES 本：「の」のあとの間（声帯の震えの無い区間）と、その前の声
    roles = []
    for _ in range(ROLE_TAKES):
        x, rx, _, pause_x, row_x = await take_with_times(client, args, key, call.format(who=f"{ROLE}{args.role_name}様"), lambda p, r, t: pause_after(p, r, t, ROLE))
        fe = seam_features(x, rx, pause_x[0] - FEAT_S, pause_x[0])
        if fe:
            roles.append({"pcm": x, "pause": pause_x, "end": fe, "row": row_x})

    def name_take(text, start_of):
        """名前の部分の録音を tries 本まで録り、続く言葉と声の合う（head_ok）録音、無ければずれの一番小さい録音。start_of＝名前の始まりの所。"""
        async def go():
            best, tries = None, []
            for n in range(args.tries):
                y, ry, times, start, row = await take_with_times(client, args, key, text, start_of)
                m = measure_head(y, ry, times, tail_b, CARRIER_ROLE if CARRIER_ROLE in text else "")
                ok = head_ok(m)
                miss = ((m["継ぎ目の前の高さの差（半音）"] or 99) + (m["高さの差（半音）"] or 99) + (500 if m["不自然な間"] else 0)
                        if m["cut"] is not None else 999)
                tries.append({"try": n + 1, "合う": ok, **{k: v for k, v in m.items() if k != "cut"}})
                say("名前の部分", tries[-1])
                if best is None or (ok, -miss) > (best[0], -best[1]):
                    best = (ok, miss, y, times, start, m["cut"], row)
                if ok:
                    break
            return best, tries
        return go()

    def pair(y, start_q: float, lead: float):
        return pick_role(roles, target, y, rate, start_q, lead)

    # (R1) 役職の無い文の名前の部分（文の頭から＝頭を削らない）
    (ok1, _, y1, t1, _, cut1, row1), tries1 = await name_take(call.format(who=f"{NAME}様"), lambda p, r, t: 0.0 if seam_cut(p, r, t) else None)
    role1, info1 = pair(y1, 0.0, onset_s(y1, rate))
    r1 = role1 + y1[:cut1 * 2] + tail_b["pcm"]

    # (R2) 「課長の岡田様は…」の文の、「の」のあとの間の一番静かな所から
    def r2_start(p, r, t):
        span = pause_after(p, r, t, CARRIER_ROLE)
        if span is None or seam_cut(p, r, t) is None:
            return None
        q, _ = quiet_point(p, r, span[0], span[1])
        return q / r
    (ok2, _, y2, t2, q2, cut2, row2), tries2 = await name_take(call.format(who=f"{CARRIER_ROLE}{NAME}様"), r2_start)
    span2 = pause_after(y2, rate, t2, CARRIER_ROLE)
    role2, info2 = pair(y2, q2, span2[1] - q2)
    r2 = role2 + y2[int(q2 * rate) * 2:cut2 * 2] + tail_b["pcm"]

    notes.update({"R1": {**info1, "名前の部分が続く言葉と合う": ok1, "名前の部分の録り": tries1},
                  "R2": {**info2, "名前の部分が続く言葉と合う": ok2, "名前の部分の録り": tries2}})
    items = [{"kind": "(W) まるごと", "text": call.format(who=f"{ROLE}{NAME}様"), "pcm": w},
             {"kind": "(R1) 役職｜役職の無い文の名前と助詞｜息の音からの続く言葉", "text": f"{ROLE}｜{NAME}様はいらっ｜しゃいますでしょうか？", "pcm": r1},
             {"kind": "(R2) 役職｜「課長の」の文の名前と助詞｜息の音からの続く言葉", "text": f"{ROLE}｜{NAME}様はいらっ｜しゃいますでしょうか？", "pcm": r2}]
    random.Random(args.seed).shuffle(items)
    index = []
    for k, it in enumerate(items, 1):
        wav = f"{k:02d}.wav"
        write_wav(out / wav, it["pcm"], rate)
        index.append({"wav": wav, "kind": it["kind"], "text": it["text"]})
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    say("作った：", ", ".join(f"{args.type}/{x['wav']}" for x in index))
    return {"model": args.model, "type": args.type, "round": 7, "items": index, "measures": notes,
            "takes": {"whole": row_w, "tail": row_z, "roles": [ro["row"] for ro in roles], "name_r1": row1, "name_r2": row2}}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--type", default="S2")
    p.add_argument("--tries", type=int, default=8)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=66)
    p.add_argument("--role-name", default=ROLE_NAME_DEFAULT, help="役職の録音に入れる名前（声の止まる所が「部長の」のあとにできる名前）")
    args = p.parse_args()
    save_result("G-66", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
