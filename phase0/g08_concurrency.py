"""G-8：1つのキー（プロジェクト）で、同時に何本のセッションを開けるか。

やること：同時に N 本（1, 2, 3, 5, 8）のセッションを開き、それぞれ短く1往復させる。
成功した本数と、失敗したときのエラーの番号・文面を記録する（DESIGN.md 9章の「プロジェクトの枠」の見分け方の材料）。
⚠️ 上限に当たると、しばらく使えなくなるかもしれないので、N は小さい順に、途中で失敗が出たら止める。
"""
from __future__ import annotations

import asyncio
import time

from common import base_parser, describe_error, make_client, pick_key, require_python, save_result, say
from live_util import build_config, collect_turn, send_text

INSTRUCTION = "日本語で、ひとことだけ短く返事をしてください。"


async def one(client, model: str, i: int) -> dict:
    try:
        async with client.aio.live.connect(model=model, config=build_config(voice=None, system_instruction=INSTRUCTION)) as s:
            t = time.monotonic()
            await send_text(s, f"試験です。番号は{i}です。")
            r = (await collect_turn(s, started_at=t, timeout_s=40)).summary()
            await asyncio.sleep(3)  # 同時に開いている時間を重ねる
        return {"i": i, "ok": True, "first_audio_after_s": r["first_audio_after_s"]}
    except Exception as exc:
        return {"i": i, "ok": False, "error": describe_error(exc)}


async def run(args) -> dict:
    if args.dry_run:
        build_config(voice=None, system_instruction=INSTRUCTION)
        return {"dry_run": True}
    client = make_client(pick_key(args))
    rounds = []
    for n in (1, 2, 3, 5, 8):
        res = await asyncio.gather(*(one(client, args.model, i) for i in range(n)))
        ok = sum(r["ok"] for r in res)
        rounds.append({"n": n, "ok": ok, "results": res})
        say(f"同時 {n} 本：成功 {ok}")
        if ok < n:
            break
        await asyncio.sleep(5)
    return {"model": args.model, "rounds": rounds}


def main() -> None:
    require_python()
    save_result("G-08", asyncio.run(run(base_parser(__doc__).parse_args())))


if __name__ == "__main__":
    main()
