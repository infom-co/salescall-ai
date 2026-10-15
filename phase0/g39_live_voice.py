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

YOUNG = "声は、明るく、はつらつとした、若々しい声で話してください。"
CONDITIONS = {"C0": "smile", "C1": "smile_young", "C2": "warm_business"}
g03_voices.STYLES["smile_young"] = g03_voices.STYLES["smile"] + YOUNG
for _c, _style in CONDITIONS.items():
    instruction.VOICE_TYPES[f"G39{_c}"] = ("Sadachbia", _style)

SCENE = "ふつう"
REFERENCE_QUESTION = AUDIO_DIR.parent / "script" / "S4_Q3_lead_tsuzukimashite.wav"  # ユーザーが選んだ06（若々しい声）
GAP_S = 0.6


def run_rows(rows: list[dict]) -> dict:
    rs = [r for r in rows if "checks" in r]
    return {c: {"calls": sum(r["cond"] == c for r in rs), "values_ok": sum(r["checks"]["q2_ok"] + r["checks"]["q3_ok"] for r in rs if r["cond"] == c),
                "speech_rate": speech_rate([r for r in rs if r["cond"] == c]).get("_")} for c in CONDITIONS}


async def run(args) -> dict:
    p = plan()[SCENE]
    if args.dry_run:
        return {"dry_run": True, "styles": {c: instruction.voice_style(f"G39{c}") for c in CONDITIONS}}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for c in args.cond or CONDITIONS:  # 指示の違いを交互に流す（時間による揺れを、どれかに偏らせない）
            vt = f"G39{c}"
            try:
                call = await run_call(client, args.model, BUSINESS, vt, p, opening_mode="recorded", use_ear=False, save_audio="all",
                                      audio_label=f"G39_{c}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S, callee_voice="swap",
                                      play_recordings=True, **FAST)
                row = keep_call({"rep": rep, "cond": c, "type": "_", "scene": SCENE}, call, lambda x: measures(x, SCENE))
            except Exception as exc:
                row = {"rep": rep, "cond": c, "type": "_", "scene": SCENE, "error": describe_error(exc)}
            rows.append(row)
            said = [t.get("intended") for t in row.get("turns", []) if t.get("audio_file")]
            say(f"[{rep}] {c} 値={row.get('checks')} AIの言葉={said} {row.get('error', '')}")
            save_partial("G-39", {"model": args.model, "rows": rows})
            await asyncio.sleep(args.pause)
    return {"model": args.model, "variant": FAST, "conditions": CONDITIONS, "young_sentence": YOUNG, "summary": run_rows(rows), "rows": rows}


def make_listening(result: Path, out: Path) -> list[dict]:
    """録音の質問（06）→ 0.6秒 → お客様の「3人です。」→ 0.6秒 → AIの「かしこまりました。3人で…」。どの指示か分からない名前にする。"""
    d = json.loads(result.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    q, rate = read_wav_pcm16_mono(REFERENCE_QUESTION)
    gap = np.zeros(int(rate * GAP_S), dtype=np.int16)
    items = []
    for r in d["rows"]:
        for t in r.get("turns") or []:
            if t.get("audio_file") and "3人" in (t.get("intended") or "").replace(" ", "") and (t.get("check") or {}).get("kind") == "ok":
                items.append((r, t))
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
    p.add_argument("--repeat", type=int, default=4)
    p.add_argument("--cond", choices=sorted(CONDITIONS), action="append")
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--listen", type=Path, help="この結果から、聞き比べの声を作る（キーを使わない）")
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    if args.listen:
        for m in make_listening(args.listen, args.out):
            print(json.dumps(m, ensure_ascii=False))
        return
    if args.dry_run:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
        return
    save_result("G-39", asyncio.run(run(args)))
    drop_partial("G-39")


if __name__ == "__main__":
    main()
