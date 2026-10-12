"""相手役の声の試験材料を作る（G-11・G-14・G-16 で使う）。

やること：相手役の短い発話を、会話に使う声（Sadachbia・Leda）とは別の声で gemini-3.8-live に読ませ、
電話の音質（8kHz）に落としてから 16kHz の WAV にして results/callee/ に保存する。
⚠️ 限界：作った声であって、人の声ではない。人の声での確かめは、別に録った音声で行う（G-14）。
"""
from __future__ import annotations

import asyncio
import json
import time

from audio import duration_s, phone_quality
from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from live_util import build_config, collect_turn, send_text

CALLEE_VOICES = ("Puck", "Kore")  # 会話に使う声とは別の声（公式の音声一覧にある名前）
UTTERANCES = {
    "hello": "はい、もしもし。",
    "yes_reserved": "はい、予約しています。",
    "no_not_yet": "いいえ、まだしていません。",
    "datetime_pm": "15日の午後2時でお願いします。",
    "datetime_ambiguous": "15日の2時です。",
    "affirm": "はい、そうです。",
    "unclear": "えっと、じゅう、の……",
}
INSTRUCTION = "あなたは音声の材料を作る係です。渡された言葉だけを、そのまま、自然な日本語で読み上げます。言葉を足しません。"
OUT = HERE / "results" / "callee"


async def run(args) -> dict:
    if args.dry_run:
        for v in CALLEE_VOICES:
            build_config(voice=v, system_instruction=INSTRUCTION)
        return {"dry_run": True}
    client = make_client(pick_key(args))
    made = []
    for voice in CALLEE_VOICES:
        for uid, text in UTTERANCES.items():
            try:
                async with client.aio.live.connect(model=args.model, config=build_config(voice=voice, system_instruction=INSTRUCTION)) as s:
                    t = time.monotonic()
                    await send_text(s, f"読み上げる言葉：{text}")
                    turn = await collect_turn(s, started_at=t, timeout_s=60)
                pcm = phone_quality(bytes(turn.audio), turn.audio_rate or 24000, 16000)
                path = OUT / f"{voice}_{uid}.wav"
                write_wav(path, pcm, 16000)
                said = turn.summary()["output_transcript"]
                made.append({"voice": voice, "id": uid, "text": text, "said": said, "exact": said.replace(" ", "") == text,
                             "seconds": round(duration_s(pcm, 16000), 2), "wav": path.name})
                say(f"{voice} {uid}: {said}")
            except Exception as exc:
                made.append({"voice": voice, "id": uid, "text": text, "error": describe_error(exc)})
    (OUT / "index.json").write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"model": args.model, "samples": made}


def main() -> None:
    require_python()
    args = base_parser(__doc__).parse_args()
    save_result("CALLEE-SAMPLES", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
