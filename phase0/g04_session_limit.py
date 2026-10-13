"""G-4・G-5：1回の接続・セッションは何分で切れるか／切れる前の知らせ／セッションの再開。

やること：
1. 指示の中に「合言葉」を入れてセッションを開き、無音を流し続ける。
2. 切れる前の知らせ（go_away）と、実際に切れた時刻、再開用の手がかり（handle）が届いたかを記録する。
3. --resume を付けると、切れたあと handle で再開し、「合言葉は何でしたか」と聞いて、前の内容を覚えているかを見る。
   さらに、handle を使わず「指示文といまの位置を渡し直す」形（DESIGN.md 6-6）でも同じことを聞く。
※ 最長 --max-minutes 分で止める（料金がかかるので、まずは短めで試す）。
"""
from __future__ import annotations

import asyncio
import time

from common import base_parser, describe_error, make_client, pick_key, require_python, save_result, say
from live_util import build_config, collect_turn, send_text, silence_while

WORD = "さくら"
INSTRUCTION = f"あなたは電話の応対の試験をしています。合言葉は「{WORD}」です。聞かれたら合言葉だけを答えてください。それ以外は黙っていてください。"
REINSTRUCTION = INSTRUCTION + "（これは前のセッションの続きです。いまの位置：合言葉を聞かれるのを待っている）"


async def stream_until_closed(session, max_s: float, log: dict) -> None:
    start = time.monotonic()

    async def sender():
        async with silence_while(session):
            await asyncio.sleep(max_s)

    async def receiver():
        while True:
            async for msg in session.receive():
                now = round(time.monotonic() - start, 1)
                if msg.go_away is not None:
                    log["go_away"].append({"at_s": now, "time_left": str(msg.go_away.time_left)})
                    say(f"切れる前の知らせ：{now} 秒 残り {msg.go_away.time_left}")
                u = msg.session_resumption_update
                if u is not None and u.new_handle:
                    log["handle"] = u.new_handle
                    log["handle_updates"] += 1

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        exc = t.exception()
        if exc is not None:
            log["closed_by"] = describe_error(exc)
    log["ended_after_s"] = round(time.monotonic() - start, 1)
    log["reached_max"] = log["ended_after_s"] >= max_s - 1


async def ask_word(client, model: str, cfg) -> dict:
    async with client.aio.live.connect(model=model, config=cfg) as session:
        t = time.monotonic()
        await send_text(session, "合言葉は何でしたか。")
        turn = await collect_turn(session, started_at=t)
    text = turn.summary()["output_transcript"]
    return {"answer": text, "remembered": WORD in text}


async def run(args) -> dict:
    cfg = build_config(voice=None, system_instruction=INSTRUCTION, transcription=True, enable_resumption=True)
    if args.dry_run:
        build_config(voice=None, system_instruction=REINSTRUCTION, resumption_handle="dummy")
        return {"dry_run": True, "config_ok": True}
    client = make_client(pick_key(args))
    log = {"go_away": [], "handle": None, "handle_updates": 0}
    try:
        async with client.aio.live.connect(model=args.model, config=cfg) as session:
            await stream_until_closed(session, args.max_minutes * 60, log)
    except Exception as exc:
        log.setdefault("closed_by", describe_error(exc))
    out = {"model": args.model, "max_minutes": args.max_minutes, **{k: v for k, v in log.items() if k != "handle"},
           "handle_received": bool(log["handle"])}
    if args.resume:
        if log["handle"]:
            try:
                out["resume_with_handle"] = await ask_word(
                    client, args.model, build_config(voice=None, system_instruction=INSTRUCTION, resumption_handle=log["handle"]))
            except Exception as exc:
                out["resume_with_handle"] = {"error": describe_error(exc)}
        try:
            out["reinstruct_without_handle"] = await ask_word(
                client, args.model, build_config(voice=None, system_instruction=REINSTRUCTION))
        except Exception as exc:
            out["reinstruct_without_handle"] = {"error": describe_error(exc)}
    return out


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--max-minutes", type=float, default=20.0)
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()
    save_result("G-04", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
