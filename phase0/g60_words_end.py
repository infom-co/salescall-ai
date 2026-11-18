"""G-60：相手の言葉の文字は、いつ出揃うか（DESIGN.md 6-10 の判定に渡す言葉）。

⛔ 2026-10-19 G-57 の本番の形：受付の方の「岡田ですね。」「少々お待ちください。」（間0.6秒）の間で、AIが報告し、判定は「岡田ですね。」
   だけを見た。声の終わりから決めた秒数だけ続きの文字を待つ形では、文字の届き方（相手の話し方・会話AIの混み具合）で穴が残る
   （G-34：考えている相手の「ええとですね。」「3人です。」が、声の終わりのあとに0.7秒あいて届いた）。
⇒ 秒数ではなく、会話AIが知らせる区切り（話し始め・話し終わりの知らせ＝explicit_vad_signal・相手の言葉の文字・AIの返事）で決められるかを
   測る：相手役の声を、電話と同じ速さで送り（送っている間も受け取る）、届いた知らせを、相手の声の終わりからの秒で並べる。
使い方：python g60_words_end.py --repeat 5 [--wav Sadachbia_hold_target.wav …]
"""
from __future__ import annotations

import asyncio
import time

from common import base_parser, make_client, pick_key, require_python, save_result, say
from live_util import build_config, silence_while, stream_audio
from stitch_gap import callee_speech

WAVS = ("Sadachbia_hold_target.wav", "Sadachbia_taken_over_okada.wav", "Sadachbia_taken_over.wav", "Puck_pause1500_three.wav",
        "Puck_pause700_datetime.wav", "Sadachbia_voicemail.wav", "Sadachbia_ivr.wav", "Leda_affirm.wav", "Leda_datetime_pm.wav")
SYSTEM = ("あなたは電話をかけた会社の担当者です。相手の言葉を最後まで聞いてから、日本語で短く返事をしてください。"
          "相手が話している途中で、話し始めないでください。")
AFTER_S = 6.0  # 相手の声のあと、受け取り続ける秒


def kinds(msg) -> list[tuple[str, object]]:
    out = []
    if getattr(msg, "voice_activity_detection_signal", None) is not None:
        out.append(("vad", str(msg.voice_activity_detection_signal.vad_signal_type).split(".")[-1]))
    if getattr(msg, "voice_activity", None) is not None:
        va = msg.voice_activity
        out.append(("activity", f"{str(va.voice_activity_type).split('.')[-1]}@{va.audio_offset}"))
    if msg.tool_call_cancellation is not None:
        out.append(("cancelled", list(msg.tool_call_cancellation.ids or [])))
    sc = msg.server_content
    if sc is not None:
        if sc.input_transcription is not None and sc.input_transcription.text:
            out.append(("heard", sc.input_transcription.text))
        if sc.output_transcription is not None and sc.output_transcription.text:
            out.append(("ai_said", sc.output_transcription.text))
        if sc.model_turn is not None and any(p.inline_data is not None and p.inline_data.data for p in sc.model_turn.parts or []):
            out.append(("audio", None))
        if sc.interrupted:
            out.append(("interrupted", None))
        if sc.turn_complete:
            out.append(("turn_complete", None))
    return out


async def one(client, args, wav: str) -> dict:
    pcm, rate, tail = callee_speech(wav)
    cfg = build_config(voice="Charon", system_instruction=SYSTEM, vad_signal=not args.no_vad)
    events = []
    async with client.aio.live.connect(model=args.model, config=cfg) as s:
        # 声の終わり＝送り終えた時刻から、声のあとの無音を引く（送るのは、声の長さより少し遅い＝送り始めからは数えない）。
        # 知らせは届いた時刻のまま記録し、最後に声の終わりからの秒に直す

        async def read():
            last_audio = False
            while True:
                async for m in s.receive():
                    now = time.monotonic()
                    for k, v in kinds(m):
                        if k == "audio":
                            if last_audio:
                                continue
                            last_audio = True
                        elif k == "turn_complete":
                            last_audio = False
                        events.append((now, k, v))
        reader = asyncio.create_task(read())
        await stream_audio(s, pcm, rate)
        voice_end = time.monotonic() - tail
        async with silence_while(s, rate):
            await asyncio.sleep(AFTER_S)
        reader.cancel()
    return {"wav": wav, "tail_s": round(tail, 3), "events": [(round(t - voice_end, 3), k, v) for t, k, v in events]}


async def run(args) -> dict:
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for wav in args.wav or WAVS:
            try:
                row = await one(client, args, wav)
            except Exception as exc:  # 1本落ちても続ける（落ちた理由を残す）
                row = {"wav": wav, "error": repr(exc)[:200]}
            row["rep"] = rep
            rows.append(row)
            say(wav, rep, " ".join(f"{t:+.2f}:{k}{'' if v is None else '=' + str(v)[:16]}" for t, k, v in row.get("events", [])) or row.get("error"))
            await asyncio.sleep(args.pause)
    return {"model": args.model, "vad_signal": not args.no_vad, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--wav", action="append")
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--no-vad", action="store_true", help="話し始め・話し終わりの知らせを送らせない（比べるため）")
    args = p.parse_args()
    save_result("G-60", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
