"""AIと同じ声で、短い相づち（「はい、」）の録音を作る（DESIGN.md 6-15 の案：AIの報告が届いた瞬間に、プログラムが流す）。

会話AIは、相手が話し終えてから約0.8秒で報告を呼ぶ（G-30 の判定0.4秒の形）が、声が出るのは約1.3秒（報告の返事を待つため）。その間を、
AIと同じ声の決まった短い相づちで埋める案を、ユーザーに聞いていただくための材料。声の型ごとに作り、別の耳で確かめる（読みが合うこと）。
⚠️ 本番で使うなら、決まった発話の録音（6-13）と同じく、人（運営者）が聞いて承認する。
"""
from __future__ import annotations

import asyncio
import json
import time

from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from instruction import VOICE_TYPES, voice_style
from listen import hear_feeds, in_any
from live_util import build_config, collect_turn, send_text

ACKS = {"hai": "はい、"}
OUT = HERE / "results" / "ack"


async def run(args) -> dict:
    if args.dry_run:
        return {"dry_run": True, "acks": ACKS, "types": list(VOICE_TYPES)}
    client = make_client(pick_key(args))
    made = []
    OUT.mkdir(parents=True, exist_ok=True)
    for vt in args.type or VOICE_TYPES:
        voice, style = voice_style(vt)
        for aid, text in ACKS.items():
            for n in range(args.tries):
                try:
                    inst = (f"{style}\nあなたは音声の材料を作る係です。渡された言葉だけを、電話で相手の話に相づちを打つように、そのまま読み上げます。"
                            "言葉を足しません。")
                    async with client.aio.live.connect(model=args.model, config=build_config(voice=voice, system_instruction=inst)) as s:
                        t = time.monotonic()
                        await send_text(s, f"読み上げる言葉：{text}")
                        turn = await collect_turn(s, started_at=t, timeout_s=60)
                    pcm, rate = bytes(turn.audio), turn.audio_rate or 24000
                    heard = await hear_feeds(client, pcm, rate)
                    ok = bool(pcm) and in_any(heard, lambda v: "はい" in v) and len(pcm) / 2 / rate < 1.0
                    say(f"{vt} {aid} {n + 1}回目：聞こえた {heard} 長さ {len(pcm) / 2 / rate:.2f}秒 合格={ok}")
                    if ok or n == args.tries - 1:
                        path = OUT / f"{vt}_{aid}.wav"
                        write_wav(path, pcm, rate)
                        made.append({"type": vt, "voice": voice, "id": aid, "text": text, "heard": heard, "ok": ok,
                                     "seconds": round(len(pcm) / 2 / rate, 2), "wav": path.name})
                        break
                except Exception as exc:
                    if n == args.tries - 1:
                        made.append({"type": vt, "id": aid, "error": describe_error(exc)})
    (OUT / "index.json").write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"model": args.model, "made": made}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--tries", type=int, default=3)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("ACK-CLIPS", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
