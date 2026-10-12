"""G-17：どういう時に、AIが言い間違える・言葉を抜かす・声が途切れる・黙るのかを調べる（2026-10-12 ユーザー指示）。

やること：同じ台本を、条件を変えて読ませ、次の4つを数える。
- 声の途切れ：そのターンの台本の長さに比べて、届いた声が短すぎる（1秒に15文字を超える速さでしか読めない長さ）
- 黙り：そのターンで声が1つも届かない
- 言葉の抜け・入れ替わり：録った声を「別の耳」（listen.py の transcribe35）で聞き直し、台本と読み（ひらがな）で比べる
- AIが言うつもりだった言葉：AI自身の文字起こしを台本と比べる
  ⇒ 別の耳では食い違うのに、言うつもりの言葉は台本どおり ⇒ 声を作るところで崩れた
  ⇒ 言うつもりの言葉から台本と違う ⇒ AIが言葉そのものを変えた
条件：声（2）× 話し方の指示（なし／調子だけ／調子＋2倍速）× 長さ（全文を一度に／1文ずつ）× 台本（2）× 回数。
⚠️ 限界：別の耳は、わずかな言い間違い（「おも忙しい」）を正しい言葉に直して書き出し、見つけられない（G-17 の耳の確かめ）。
   ここで数えられるのは、言葉の抜け・入れ替わり・声の途切れ・黙りまで。
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from audio import voiced_seconds
from common import HERE, VOICES, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from g03_voices import INSTRUCTION, SCRIPT, STYLES
from listen import differences, hear, norm
from live_util import build_config, collect_turn, send_text

SCRIPTS = {
    "A": SCRIPT,
    # 会社名・人の名前・日時が多い台本（実際の電話に近い中身。会社名と人の名前は架空）
    "B": (
        "お忙しいところ恐れ入ります。株式会社アオバ物流の佐藤と申します。"
        "本日は、先日お送りした資料の件でお電話いたしました。"
        "ご担当の田中様はいらっしゃいますでしょうか。"
        "来週の火曜日、10月20日の午後3時に、改めてお電話してもよろしいでしょうか。"
        "ありがとうございました。失礼いたします。"
    ),
}
# 2026-10-12 G-17 の1回目：「株式会社アオバ物流」が、別の耳に「株式会社大場（おおば）物流」と聞こえた（Sadachbia 12回中9回）。
# 「がいしゃ」の「ゃ（a）」と「アオバ」の「ア（a）」が続いて、1つの音につながると見られる。⇒ 直し方の候補を比べる。
SCRIPTS["B_comma"] = SCRIPTS["B"].replace("株式会社アオバ物流", "株式会社、アオバ物流")  # 案1：社名の前に区切りを入れる
_SPEED = "話す速さは2倍速にしてください。"
CONDITIONS = {
    "none": "",
    "tone": STYLES["warm_business_x2"].replace(_SPEED, ""),  # 調子だけ（2倍速の一文を抜いた同じ指示）
    "tone_x2": STYLES["warm_business_x2"],
    # 案2：会社名・人の名前を、ほかの言葉とつなげずに言わせる（どの社名・人名にも効く、1つの決まり）
    "tone_x2_names": STYLES["warm_business_x2"] + "会社名と人の名前は、前後の言葉とつなげず、1語ずつはっきり言ってください。",
}
assert _SPEED in STYLES["warm_business_x2"] and _SPEED not in CONDITIONS["tone"]
MAX_CHARS_PER_S = 15.0  # これより速くは読めない（会話の形の実測は1秒に 6.7〜8.2文字）
AUDIO_DIR = HERE / "results" / "g17_audio"
GAP = b"\x00\x00" * int(24000 * 0.6)  # 1文ずつのときに、声をつなぐ間の無音


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。？?])", text) if s.strip()]


async def speak(client, model: str, voice: str, cond: str, parts: list[str]) -> tuple[list[dict], bytes]:
    cfg = build_config(voice=voice, system_instruction=INSTRUCTION + CONDITIONS[cond])
    turns, audio = [], bytearray()
    async with client.aio.live.connect(model=model, config=cfg) as s:
        for part in parts:
            t = time.monotonic()
            await send_text(s, f"台本：{part}")
            turn = await collect_turn(s, started_at=t, timeout_s=90)
            r = turn.summary()
            rate = turn.audio_rate or 24000
            voiced = voiced_seconds(bytes(turn.audio), rate) if turn.audio else 0.0
            need = len(norm(part)) / MAX_CHARS_PER_S
            usage = r["usage"][-1] if r["usage"] else {}
            turns.append({
                "script": part, "intended": r["output_transcript"], "audio_s": r["audio_seconds"], "voiced_s": round(voiced, 2),
                "silent": not turn.audio, "cut_off": bool(turn.audio) and voiced < need,
                "first_audio_after_s": r["first_audio_after_s"],
                "audio_tokens": sum(x.get("token_count", 0) for x in usage.get("response_tokens_details", []) if x.get("modality") == "AUDIO"),
                "thoughts_tokens": usage.get("thoughts_token_count"),
            })
            if audio and turn.audio:
                audio += GAP
            audio += turn.audio
    return turns, bytes(audio)


async def one_trial(client, args, tid: str, voice: str, cond: str, length: str, sid: str) -> dict:
    script = SCRIPTS[sid]
    parts = [script] if length == "long" else sentences(script)
    turns, audio = await speak(client, args.model, voice, cond, parts)
    row = {"id": tid, "voice": voice, "condition": cond, "length": length, "script_id": sid, "turns": turns,
           "silent_turns": sum(t["silent"] for t in turns), "cut_off_turns": sum(t["cut_off"] for t in turns)}
    intended = "".join(t["intended"] for t in turns)
    row["intended_missing"] = any(not t["intended"] for t in turns)  # AI自身の文字起こしが届かなかったターンがある
    row["intended_diffs"] = differences(script, intended) if not row["intended_missing"] else None
    if audio:
        wav = AUDIO_DIR / f"{tid}.wav"
        write_wav(wav, audio, 24000)
        row["wav"] = wav.name
        try:
            row["ear"] = await hear(client, audio, 24000, "transcribe35")
            row["ear_diffs"] = differences(script, row["ear"])
        except Exception as exc:
            row["ear_error"] = describe_error(exc)
    return row


def summarize(rows: list[dict]) -> dict:
    def agg(key, val):
        xs = [r for r in rows if r.get(key) == val and "error" not in r]
        return {"trials": len(xs),
                "silent_turns": sum(r["silent_turns"] for r in xs),
                "cut_off_turns": sum(r["cut_off_turns"] for r in xs),
                "turns": sum(len(r["turns"]) for r in xs),
                "trials_with_ear_diffs": sum(1 for r in xs if r.get("ear_diffs")),
                "trials_with_intended_diffs": sum(1 for r in xs if r.get("intended_diffs"))}
    out = {}
    for key, vals in (("voice", VOICES), ("condition", CONDITIONS), ("length", ("long", "short")), ("script_id", SCRIPTS)):
        out[key] = {v: agg(key, v) for v in vals}
    out["errors"] = sum(1 for r in rows if "error" in r)
    return out


async def run(args) -> dict:
    if args.dry_run:
        for v in VOICES:
            for c in CONDITIONS:
                build_config(voice=v, system_instruction=INSTRUCTION + CONDITIONS[c])
        return {"dry_run": True, "sentences": {k: sentences(v) for k, v in SCRIPTS.items()}}
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    client = make_client(pick_key(args))
    rows = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log = AUDIO_DIR / f"partial_{stamp}.jsonl"  # 途中で止まっても、そこまでの結果が残る
    n = 0
    for rep in range(args.repeat):
        for sid in args.script or ("A", "B"):
            for length in args.length or ("long", "short"):
                for voice in args.voice or VOICES:
                    for cond in args.condition or ("none", "tone", "tone_x2"):
                        n += 1
                        tid = f"{stamp}_{n:03d}_{voice}_{cond}_{length}_{sid}"
                        try:
                            row = await one_trial(client, args, tid, voice, cond, length, sid)
                        except Exception as exc:
                            row = {"id": tid, "voice": voice, "condition": cond, "length": length, "script_id": sid, "error": describe_error(exc)}
                        rows.append(row)
                        with log.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(row, ensure_ascii=False) + "\n")
                        say(f"[{n}] {voice:<9} {cond:<7} {length:<5} {sid} 黙り={row.get('silent_turns')} 途切れ={row.get('cut_off_turns')} "
                            f"耳の食い違い={json.dumps(row.get('ear_diffs'), ensure_ascii=False)[:160]} "
                            f"言うつもりの食い違い={json.dumps(row.get('intended_diffs'), ensure_ascii=False)[:120]} {row.get('error', '')}")
                        await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "scripts": SCRIPTS, "conditions": CONDITIONS, "max_chars_per_s": MAX_CHARS_PER_S,
            "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--pause", type=float, default=3.0, help="1回ごとに空ける秒数（1分あたりの上限を避けるため）")
    p.add_argument("--script", choices=sorted(SCRIPTS), action="append", help="この台本だけ（省略すると A と B）")
    p.add_argument("--length", choices=("long", "short"), action="append")
    p.add_argument("--voice", choices=VOICES, action="append")
    p.add_argument("--condition", choices=sorted(CONDITIONS), action="append", help="省略すると none・tone・tone_x2")
    args = p.parse_args()
    save_result("G-17", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
