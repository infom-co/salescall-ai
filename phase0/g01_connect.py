"""G-1・G-2・G-9：モデルIDで接続できるか／返ってくる音声の形式／利用量が返るか。

やること：手元のキー1つで接続し、文字で短く話しかけ、返ってきた音声を WAV に保存する。
記録するもの：接続にかかった時間、最初の音声までの時間、音声の形式（mime）、利用量の情報、届いた知らせの種類。
"""
from __future__ import annotations

import asyncio
import time

from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from live_util import build_config, collect_turn, send_text

INSTRUCTION = "あなたは電話の応対の試験をしています。日本語で、ひとことだけ短く返事をしてください。"
PROMPT = "こんにちは。接続の試験です。ひとことだけ返事をしてください。"


async def run(args) -> dict:
    cfg = build_config(voice=args.voice, system_instruction=INSTRUCTION)
    if args.dry_run:
        return {"dry_run": True, "config_ok": True, "config": cfg.model_dump(exclude_none=True)}
    key = pick_key(args)
    client = make_client(key)
    t0 = time.monotonic()
    async with client.aio.live.connect(model=args.model, config=cfg) as session:
        connected_s = round(time.monotonic() - t0, 3)
        t1 = time.monotonic()
        await send_text(session, PROMPT)
        turn = await collect_turn(session, started_at=t1)
    wav = HERE / "results" / f"G-01_{args.voice or 'default'}.wav"
    if turn.audio:
        write_wav(wav, bytes(turn.audio), turn.audio_rate or 24000)
    return {"key": key.label, "model": args.model, "voice": args.voice, "connected_after_s": connected_s,
            "wav": str(wav) if turn.audio else None, **turn.summary()}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--voice", default="Leda")
    args = p.parse_args()
    try:
        result = asyncio.run(run(args))
        result["ok"] = True
    except Exception as exc:  # 失敗の中身も記録する（K-3 の材料）
        result = {"ok": False, "model": args.model, "error": describe_error(exc)}
    say("結果：", "成功" if result["ok"] else f"失敗 {result['error']['type']}")
    save_result("G-01", result)


if __name__ == "__main__":
    main()
