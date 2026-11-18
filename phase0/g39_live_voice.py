"""G-39：会話の中でAIが自分で話す声が、人が選んだ若々しい声（録音の基準）と同じ人物に聞こえるか。話し方の指示で変わるか（DESIGN.md 6-13）。

✅ 2026-10-15 ユーザー（G-38 の聞き比べ）：「06.は若々しく、『承知しました』と同じ声に聞こえる。…これまでの録音は全て、おっさんっぽい側です。
   若々しいで統一するなら間違いなく06.」「ただし、録音だけでなく、AIでも、この声が普通に出るようにしないと別人になってしまう」
比べる話し方の指示（声の名前はどれも Sadachbia）：
- C0：いまの S4（にこやか）
- C1：C0 に、良い向きの言葉だけで声を言う一文を足す（G-38 の B は「しゃがれた声・年配の人のような声にしない」と避ける声も並べたが、
      若々しい声は出やすくならなかった＝避ける声の言葉が、かえってその声を呼んだかもしれない）
- C2：S2（取引先に電話をかける会社員）。✅ ユーザー：「録音は…S4とS2を分ける必要はありません」＝声の型の違いで出やすさが変わるかも見る
見ること：AIが話した「かしこまりました。3人で大丈夫でしょうか？」を、録音の質問（06）→ お客様の「3人です。」のあとにつないで、
   人が聞いて「同じ人物（若々しい）か」を決める（どの指示か分からない名前にする。対応は key.json）。⚠️ 若々しいかを数で見分ける方法は無い（G-38）。
あわせて：値・言い直しの速さ（g37_recorded.speech_rate）。

G-40（--set G-40）：✅ 2026-10-15 ユーザー（G-39 の「どれもおっさんっぽい」のあと）：「『トーンを高めで』とか、他の指示も考慮してみたら」
⇒ 声の高さ・調子を言う指示を5つ、いまの S4（C0）に足して比べる。声の高さは数で測れる（voice_features）ので、指示ごとに
   AIの声の高さが C0 より上がったかも出す（⚠️ 高さが上がっても若々しく聞こえるとは限らない＝G-38。最後は人の耳で決める）。
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

import numpy as np

import g03_voices
import instruction
from common import base_parser, describe_error, drop_partial, make_client, pick_key, read_wav_pcm16_mono, require_python, save_partial, save_result, say, write_wav
from convo import AUDIO_DIR, run_call
from g22_opening import BUSINESS
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from g37_recorded import measures, plan, speech_rate
from stitch_gap import callee_audio
from voice_features import features, read_wav, semitones

# ⛔ 2026-10-15：C1 の「明るく」・T2 の「明るく」・T3 の「はきはきと」は、ユーザーが 2026-10-12 に「営業には不向きな強引そうな
#    詐欺師のような怪しいニュアンス」と言った言葉だった（コンパクトのあと、前のやり取りを読み直さずに使った）。⇒ この試験の記録としてだけ残す。
#    本番の指示・録音の読み方には使わない（test_offline.WordingTest）。
# 指示の組（試験の名前ごと）。値＝いまの S4（にこやか）に足す一文（None＝別の型をそのまま使う）
SETS = {
    "G-39": {"C0": "", "C1": "声は、明るく、はつらつとした、若々しい声で話してください。", "C2": None},
    "G-40": {"C0": "",
             "T1": "声のトーンを高めにして話してください。",
             "T2": "声のトーンを少し高めにして、明るく、軽やかに話してください。",
             "T3": "声のトーンを高めに保ち、語尾まで下げずに、はきはきと話してください。",
             "T4": "入社2年目くらいの、元気で誠実な営業担当者として話してください。",
             "T5": "口角を上げて、笑顔で話しているような声で話してください。"},
}
OTHER_TYPE = {"C2": "warm_business"}  # G-39 の C2＝S2


def register(set_name: str) -> dict:
    """指示の組を、声の型として登録する（声の名前はどれも Sadachbia）。{条件: 声の型の名前}"""
    out = {}
    for c, sentence in SETS[set_name].items():
        style = OTHER_TYPE[c] if sentence is None else f"smile_{set_name}_{c}"
        if sentence is not None:
            g03_voices.STYLES[style] = g03_voices.STYLES["smile"] + sentence
        vt = f"{set_name.replace('-', '')}{c}"
        instruction.VOICE_TYPES[vt] = ("Sadachbia", style)
        out[c] = vt
    return out

SCENE = "ふつう"
REFERENCE_QUESTION = AUDIO_DIR.parent / "script" / "S4_Q3_lead_tsuzukimashite.wav"  # ユーザーが選んだ06（若々しい声）
GAP_S = 0.6


def pitch_of_ai(rows: list[dict]) -> dict:
    """条件ごとの、AIが話したターンの声の高さ（Hz）の中央と、C0 との差（半音）。声の確かめが通ったターンだけ。"""
    by = {}
    for r in rows:
        for t in r.get("turns") or []:
            if t.get("audio_file") and (t.get("check") or {}).get("kind") == "ok":
                f = features(*read_wav(AUDIO_DIR / t["audio_file"]))
                if f:
                    by.setdefault(r["cond"], []).append(f["pitch_hz"])
    med = {c: float(np.median(v)) for c, v in by.items()}
    base = med.get("C0")
    return {c: {"n": len(by[c]), "pitch_hz": round(m, 1), "st_from_C0": round(semitones(m, base), 2) if base else None}
            for c, m in med.items()}


def run_rows(rows: list[dict], conds) -> dict:
    rs = [r for r in rows if "checks" in r]
    pitch = pitch_of_ai(rs)
    return {c: {"calls": sum(r["cond"] == c for r in rs), "values_ok": sum(r["checks"]["q2_ok"] + r["checks"]["q3_ok"] for r in rs if r["cond"] == c),
                "speech_rate": speech_rate([r for r in rs if r["cond"] == c]).get("_"), "pitch": pitch.get(c)} for c in conds}


async def run(args) -> dict:
    p = plan()[SCENE]
    types = register(args.set)
    if args.dry_run:
        return {"dry_run": True, "styles": {c: instruction.voice_style(vt) for c, vt in types.items()}}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for c in args.cond or types:  # 指示の違いを交互に流す（時間による揺れを、どれかに偏らせない）
            vt = types[c]
            try:
                call = await run_call(client, args.model, BUSINESS, vt, p, opening_mode="recorded", use_ear=False, save_audio="all",
                                      audio_label=f"{vt}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S, callee_voice="swap",
                                      play_recordings=True, **FAST)
                row = keep_call({"rep": rep, "cond": c, "type": "_", "scene": SCENE}, call, lambda x: measures(x, SCENE))
            except Exception as exc:
                row = {"rep": rep, "cond": c, "type": "_", "scene": SCENE, "error": describe_error(exc)}
            rows.append(row)
            said = [t.get("intended") for t in row.get("turns", []) if t.get("audio_file")]
            say(f"[{rep}] {c} 値={row.get('checks')} AIの言葉={said} {row.get('error', '')}")
            save_partial(args.set, {"model": args.model, "rows": rows})
            await asyncio.sleep(args.pause)
    return {"model": args.model, "variant": FAST, "set": args.set, "sentences": SETS[args.set], "types": types,
            "summary": run_rows(rows, types), "rows": rows}


def make_listening(result: Path, out: Path, per_cond: int | None = None) -> list[dict]:
    """録音の質問（06）→ 0.6秒 → お客様の「3人です。」→ 0.6秒 → AIの「かしこまりました。3人で…」。どの指示か分からない名前にする。"""
    d = json.loads(result.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    q, rate = read_wav_pcm16_mono(REFERENCE_QUESTION)
    gap = np.zeros(int(rate * GAP_S), dtype=np.int16)
    items, seen = [], {}
    for r in d["rows"]:
        if per_cond is not None and seen.get(r["cond"], 0) >= per_cond:  # 条件ごとに、流した順で per_cond 本まで（良いものを選ばない）
            continue
        for t in r.get("turns") or []:
            if t.get("audio_file") and "3人" in (t.get("intended") or "").replace(" ", "") and (t.get("check") or {}).get("kind") == "ok":
                items.append((r, t))
                seen[r["cond"]] = seen.get(r["cond"], 0) + 1
                break
    random.Random(39).shuffle(items)
    key = []
    for n, (r, t) in enumerate(items, 1):
        callee, _ = callee_audio(t["callee"]["wav"], rate)
        ai, ai_rate = read_wav_pcm16_mono(AUDIO_DIR / t["audio_file"])
        assert ai_rate == rate, (ai_rate, rate)
        name = f"{n:02d}_質問の録音→お客様→AIの言い直し.wav"
        x = np.concatenate([np.frombuffer(q, dtype=np.int16), gap, callee.astype(np.int16), gap, np.frombuffer(ai, dtype=np.int16)])
        write_wav(out / name, x.tobytes(), rate)
        key.append({"file": name, "cond": r["cond"], "rep": r["rep"], "said": t["intended"], "audio_file": t["audio_file"]})
    (out / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    return key


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--set", choices=sorted(SETS), default="G-39")
    p.add_argument("--repeat", type=int, default=4)
    p.add_argument("--cond", action="append")
    p.add_argument("--per-cond", type=int, help="聞き比べに入れる本数（条件ごと。流した順）")
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--listen", type=Path, help="この結果から、聞き比べの声を作る（キーを使わない）")
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    if args.listen:
        for m in make_listening(args.listen, args.out, args.per_cond):
            print(json.dumps(m, ensure_ascii=False))
        return
    if args.dry_run:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
        return
    save_result(args.set, asyncio.run(run(args)))
    drop_partial(args.set)


if __name__ == "__main__":
    main()
