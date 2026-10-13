"""G-11（＋G-2 の入力側）：相手の声を送ったとき、話し終わってから AI が話し始めるまでの時間。

やること：
- make_callee_samples.py で作った相手役の声（電話の音質に落とした 16kHz）を、実際の通話と同じく 0.1秒ずつ送る。
- 送り終わった時刻から、AI の声が届き始めるまでを測る（＝相手が待たされる時間の、AI の側のぶん）。
- 「考える量」の設定（既定／MINIMAL）ごとに比べる。相手の声の文字起こしも記録する（G-2 入力側・G-14 の材料）。
⚠️ 限界：作業環境から Google までの通信の時間を含む。電話の回線の遅れは含まない（R・H で測る）。
"""
from __future__ import annotations

import asyncio
import json
import statistics

from common import HERE, base_parser, describe_error, make_client, pick_key, read_wav_pcm16_mono, require_python, save_result, say
from live_util import build_config, callee_turn

def label(level: int | None) -> str:
    return "default" if level is None else f"budget{level}"


INSTRUCTION = "あなたは電話で予約の確認をする担当者です。相手の言ったことに、日本語で、ひとことだけ短く返事をしてください。"
SAMPLES = ["yes_reserved", "datetime_pm", "affirm", "no_not_yet"]
VOICES = ["Puck", "Kore"]


async def one(client, model: str, level: int | None, wav_name: str) -> dict:
    pcm, rate = read_wav_pcm16_mono(HERE / "results" / "callee" / wav_name)
    cfg = build_config(voice="Leda", system_instruction=INSTRUCTION, thinking_budget=level)
    async with client.aio.live.connect(model=model, config=cfg) as s:
        turn = await callee_turn(s, pcm, rate, timeout_s=30)
    r = turn.summary()
    return {"sample": wav_name, "thinking_level": label(level), "latency_s": r["first_audio_after_s"],
            "heard": r["input_transcript"], "ai": r["output_transcript"], "usage": r["usage"]}


async def run(args) -> dict:
    if args.dry_run:
        build_config(voice="Leda", system_instruction=INSTRUCTION, thinking_budget=0)
        return {"dry_run": True}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for level in (None, 0):
            for v in VOICES:
                for sid in SAMPLES:
                    name = f"{v}_{sid}.wav"
                    try:
                        row = await one(client, args.model, level, name)
                    except Exception as exc:
                        row = {"sample": name, "thinking_level": label(level), "error": describe_error(exc)}
                    rows.append(row)
                    say(f"[{rep}] {row['thinking_level']:<8} {name:<24} 待ち={row.get('latency_s')} 聞こえた={row.get('heard', '')!r} AI={row.get('ai', '')!r}")
    summary = {}
    for level in ("default", "budget0"):
        xs = sorted(r["latency_s"] for r in rows if r["thinking_level"] == level and r.get("latency_s") is not None)
        if xs:
            summary[level] = {"n": len(xs), "median_s": round(statistics.median(xs), 3),
                              "p90_s": round(xs[min(len(xs) - 1, int(len(xs) * 0.9))], 3), "max_s": xs[-1],
                              "no_reply": sum(1 for r in rows if r["thinking_level"] == level and r.get("latency_s") is None)}
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=3)
    args = p.parse_args()
    save_result("G-11", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
