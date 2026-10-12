"""G-3：声（Sadachbia・Leda）が選べて、日本語で自然に話すか。

やること：同じ日本語の台本を、声ごとに読ませて WAV に保存する。
⇒ 保存した WAV を社内の3人以上で聞き、業務に使えるかを判断して PHASE0_VERIFICATION.md に記録する。
※ 保存した WAV は、G-6 などで「相手の声」の代わりにも使える。
"""
from __future__ import annotations

import asyncio
import time

from common import HERE, VOICES, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from live_util import build_config, collect_turn, send_text

INSTRUCTION = (
    "あなたは電話で話す担当者です。これから渡す台本を、そのまま、丁寧で自然な日本語の話し方で読み上げてください。"
    "台本にない言葉を足さないでください。"
)
SCRIPT = (
    "お忙しいところ恐れ入ります。株式会社サンプルの担当でございます。"
    "本日は、ご予約の日時の確認でお電話いたしました。"
    "15日の午後2時でお間違いないでしょうか。"
    "聞き取りにくかったので、もう一度、おっしゃっていただいてよろしいでしょうか。"
    "念のため確認させてください。ありがとうございました。失礼いたします。"
)


async def one(client, args, voice: str) -> dict:
    cfg = build_config(voice=voice, system_instruction=INSTRUCTION)
    t0 = time.monotonic()
    async with client.aio.live.connect(model=args.model, config=cfg) as session:
        t1 = time.monotonic()
        await send_text(session, f"台本：{SCRIPT}")
        turn = await collect_turn(session, started_at=t1, timeout_s=120)
    wav = HERE / "results" / f"G-03_{voice}.wav"
    if turn.audio:
        write_wav(wav, bytes(turn.audio), turn.audio_rate or 24000)
    return {"voice": voice, "connected_after_s": round(t1 - t0, 3), "wav": str(wav) if turn.audio else None,
            "matches_script": turn.summary()["output_transcript"].replace(" ", "") == SCRIPT, **turn.summary()}


async def run(args) -> dict:
    if args.dry_run:
        for v in VOICES:
            build_config(voice=v, system_instruction=INSTRUCTION)
        return {"dry_run": True, "config_ok": True, "voices": list(VOICES)}
    client = make_client(pick_key(args))
    results = []
    for v in VOICES:
        try:
            results.append({"ok": True, **await one(client, args, v)})
        except Exception as exc:
            results.append({"ok": False, "voice": v, "error": describe_error(exc)})
        say(f"{v}：", "成功" if results[-1]["ok"] else "失敗")
    return {"model": args.model, "script": SCRIPT, "results": results}


def main() -> None:
    require_python()
    args = base_parser(__doc__).parse_args()
    save_result("G-03", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
