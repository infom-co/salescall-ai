"""G-3（続き）：会話の形で、AIの話す速さを測る（台本の読み上げではなく、相手の声に答える形）。

やること：
- 予約の確認の短い会話を、相手役の声（results/callee/ の電話の音質の声）で進める。AIは相手の発話ごとに1回話す。
- 話し方の指示（なし／「明るく・2倍速」）ごとに、AIの話す速さ（1秒あたりの文字数）と、返事の間（相手が話し終えてから話し出すまで）を測る。
- 速さは「前後の無音を除いた声の長さ」で割る。文字はAIの発話の文字起こし（届かない回は、その回だけ数えない）。
⚠️ 限界：相手役は作った声。1秒あたりの文字数は、漢字とかなの割合で変わる（同じ会話の流れで比べるので、比は使える）。
"""
from __future__ import annotations

import asyncio
import re
import statistics
import time

from audio import voiced_seconds
from common import HERE, VOICES, base_parser, describe_error, make_client, pick_key, read_wav_pcm16_mono, require_python, save_result, say, silence
from g03_voices import STYLES
from live_util import build_config, collect_turn

INSTRUCTION = (
    "あなたは「株式会社サンプル」の担当として、ご予約の確認の電話をかけています。"
    "相手が電話に出たら、名乗って、ご予約をされているかを聞いてください。"
    "予約しているとわかったら、ご予約の日時を聞いてください。"
    "日時を聞いたら、その日時を言い直して、合っているかを聞いてください。"
    "合っていれば、お礼を言って電話を終えてください。"
    "1回の返事は短くしてください。"
)
CALLEE = ["hello", "yes_reserved", "datetime_pm", "affirm"]
CALLEE_VOICE = "Puck"
COMPARE = ["baseline", "bright_x2"]
PUNCT = re.compile(r"[\s、。，．,.!?！？「」…ー]")


def count_chars(text: str) -> int:
    return len(PUNCT.sub("", text))


async def callee_turn(s, pcm: bytes, rate: int):
    from google.genai import types

    step = rate * 2 // 10  # 0.1秒ぶん
    for i in range(0, len(pcm), step):
        await s.send_realtime_input(audio=types.Blob(data=pcm[i:i + step], mime_type=f"audio/pcm;rate={rate}"))
        await asyncio.sleep(0.1)
    sent_end = time.monotonic()

    async def keep_silence():  # 話し終わりを AI に気づかせるため、無音を送り続けながら返事を待つ
        chunk = silence(100, rate)
        while True:
            await s.send_realtime_input(audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={rate}"))
            await asyncio.sleep(0.1)

    bg = asyncio.create_task(keep_silence())
    try:
        return await collect_turn(s, started_at=sent_end, timeout_s=30)
    finally:
        bg.cancel()


async def one_call(client, model: str, voice: str, style: str) -> list[dict]:
    cfg = build_config(voice=voice, system_instruction=INSTRUCTION + STYLES[style])
    rows = []
    async with client.aio.live.connect(model=model, config=cfg) as s:
        for cid in CALLEE:
            pcm, rate = read_wav_pcm16_mono(HERE / "results" / "callee" / f"{CALLEE_VOICE}_{cid}.wav")
            turn = await callee_turn(s, pcm, rate)
            r = turn.summary()
            text = r["output_transcript"]
            voiced = round(voiced_seconds(bytes(turn.audio), turn.audio_rate or 24000), 2) if turn.audio else 0.0
            chars = count_chars(text)
            rows.append({"voice": voice, "style": style, "callee": cid, "heard": r["input_transcript"], "ai": text,
                         "gap_s": r["first_audio_after_s"], "audio_s": r["audio_seconds"], "voiced_s": voiced,
                         "chars": chars, "chars_per_s": round(chars / voiced, 2) if chars and voiced >= 0.5 else None,
                         "usage": r["usage"]})
            say(f"{voice:<9} {style:<9} {cid:<12} 間={r['first_audio_after_s']} 声={voiced}秒 文字={chars} AI={text!r}")
    return rows


def summarize(rows: list[dict]) -> dict:
    out = {}
    for v in VOICES:
        for st in COMPARE:
            xs = [r for r in rows if r.get("voice") == v and r.get("style") == st]
            ok = [r for r in xs if r.get("chars_per_s")]
            gaps = [r["gap_s"] for r in xs if r.get("gap_s") is not None]
            out[f"{v}/{st}"] = {
                "turns": len(xs), "measured": len(ok),
                "chars_per_s": round(sum(r["chars"] for r in ok) / sum(r["voiced_s"] for r in ok), 2) if ok else None,
                "gap_median_s": round(statistics.median(gaps), 2) if gaps else None,
                "no_reply": sum(1 for r in xs if r.get("gap_s") is None),
            }
        base, fast = out[f"{v}/baseline"]["chars_per_s"], out[f"{v}/bright_x2"]["chars_per_s"]
        out[f"{v}/speed_ratio"] = round(fast / base, 2) if base and fast else None  # 1.3 なら 1.3倍の速さ
    return out


async def run(args) -> dict:
    if args.dry_run:
        for v in VOICES:
            for st in COMPARE:
                build_config(voice=v, system_instruction=INSTRUCTION + STYLES[st])
        return {"dry_run": True}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for v in VOICES:
            for st in COMPARE:
                try:
                    rows += await one_call(client, args.model, v, st)
                except Exception as exc:
                    rows.append({"voice": v, "style": st, "rep": rep, "error": describe_error(exc)})
                    say(f"{v} {st}：失敗 {rows[-1]['error']}")
    summary = summarize(rows)
    say("まとめ：", summary)
    return {"model": args.model, "callee_voice": CALLEE_VOICE, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=2)
    args = p.parse_args()
    save_result("G-03-conversation-speed", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
