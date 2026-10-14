"""AIと同じ声で、短い相づちの録音を作る（DESIGN.md 6-15：AIの報告が届いた瞬間に、プログラムが流す）。

✅ 2026-10-14 ユーザー：聞き比べで「明らかにこちらの方が自然な間。話の流れもいい感じ」。基本は「はい、」。違和感のある場面や
   「はい」が続くときは、別の相づち。⇒ 言葉は決まった言い方の表（phrases.ACK）の1か所から読み、どの場面でどれを流すかは
   流れの制御（flowctl.Flow._ack）が決める。
作った録音は、別の耳の2通りの聞き方の**両方**で、読みが合うことを確かめる（毎回の通話で同じ録音を流すので、ほかの試験より厳しくする）。
長すぎる録音も落とす（間を埋めるための相づちが、AIの声を待たせないように）。長さは、声の前後の無音を除いた、声だけの長さで比べる
（⛔ 2026-10-14 の1回目：無音まで含めた長さで比べ、読みの合った S4 の「承知しました、」を長すぎとして落とした）。
作ってある録音は作り直さない（--redo で名指ししたものだけ作り直す）。
⚠️ 本番で使うなら、決まった発話の録音（6-13）と同じく、人（運営者）が聞いて承認する。
"""
from __future__ import annotations

import asyncio
import json
import time

from barge import voice_runs, voiced
from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from instruction import VOICE_TYPES, voice_style
from jtext import compare_reading
from listen import hear_feeds
from live_util import build_config, collect_turn, send_text
from phrases import ACK

OUT = HERE / "results" / "ack"
def speech_seconds(pcm: bytes, rate: int) -> float:
    """声の前後の無音を除いた、声だけの長さ（秒）。"""
    runs = voice_runs(voiced(pcm, rate), 0.15)
    return runs[-1][1] - runs[0][0] if runs else 0.0


# 読み方（相づちとお詫びで、声の調子を変える）と、長さの上限（声だけの長さ・秒）
HOW = {"moushiwake": "電話で、相手に聞き取れなかったことを申し訳なく思いながら、丁寧に",
       "shitsurei": "電話で、こちらの聞き違いを丁寧にお詫びするように",
       # ⚠️ 2026-10-14 ユーザー：S2・S4 の考えている相手への「はい」が、物欲しそう・期待しているように聞こえた ⇒ 相づち風に
       "aizuchi": "電話で、相手が考えているのを待ちながら打つ相づちとして、軽く、落ち着いた声で、語尾を上げずに、短く"}
DEFAULT_HOW = "電話で、相手の話に相づちを打つように、短く"
MAX_S = {"hai": 1.0, "shouchi": 1.4, "arigatou": 1.6, "shitsurei": 1.6, "moushiwake": 1.6, "aizuchi": 1.0}


def heard_ok(text: str, heard: dict) -> bool:
    """2通りの聞き方の両方で、読みが合うか（句読点・言い終わりの違いは比べない）。"""
    want = compare_reading(text)
    return bool(heard) and all(v is not None and compare_reading(v) == want for v in heard.values())


async def run(args) -> dict:
    ids = args.id or list(ACK)
    index_path = OUT / "index.json"
    made = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    have = {(m["type"], m["id"]) for m in made if m.get("ok")}
    todo = [(vt, aid) for vt in (args.type or VOICE_TYPES) for aid in ids if (vt, aid) not in have or aid in (args.redo or [])]
    if args.dry_run:
        return {"dry_run": True, "todo": todo, "words": {a: ACK[a] for a in ids}}
    client = make_client(pick_key(args))
    OUT.mkdir(parents=True, exist_ok=True)
    for vt, aid in todo:
        voice, style = voice_style(vt)
        text = ACK[aid]
        row = None
        for n in range(args.tries):
            try:
                inst = (f"{style}\nあなたは音声の材料を作る係です。渡された言葉だけを、{HOW.get(aid, DEFAULT_HOW)}、そのまま読み上げます。"
                        "言葉を足しません。")
                async with client.aio.live.connect(model=args.model, config=build_config(voice=voice, system_instruction=inst)) as s:
                    t = time.monotonic()
                    await send_text(s, f"読み上げる言葉：{text}")
                    turn = await collect_turn(s, started_at=t, timeout_s=60)
                pcm, rate = bytes(turn.audio), turn.audio_rate or 24000
                seconds = speech_seconds(pcm, rate) if pcm else 0.0
                heard = await hear_feeds(client, pcm, rate) if pcm else {}
                ok = bool(pcm) and heard_ok(text, heard) and seconds <= MAX_S[aid]
                say(f"{vt} {text} {n + 1}回目：聞こえた {heard} 長さ {seconds:.2f}秒 合格={ok}")
                row = {"type": vt, "voice": voice, "id": aid, "text": text, "heard": heard, "ok": ok, "seconds": round(seconds, 2),
                       "tries": n + 1}
                if ok:
                    path = OUT / f"{vt}_{aid}.wav"
                    write_wav(path, pcm, rate)
                    row["wav"] = path.name
                    break
            except Exception as exc:
                row = {"type": vt, "id": aid, "text": text, "ok": False, "error": describe_error(exc), "tries": n + 1}
        made = [m for m in made if (m["type"], m["id"]) != (vt, aid)] + [row]
        index_path.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")  # 1本ごとに残す（途中で止まっても失わない）
        await asyncio.sleep(args.pause)
    return {"model": args.model, "made": made, "missing": [(m["type"], m["id"]) for m in made if not m.get("ok")]}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--tries", type=int, default=3)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--id", choices=sorted(ACK), action="append", help="作る相づち（中の名前）。無ければ全部")
    p.add_argument("--redo", choices=sorted(ACK), action="append", help="作ってある録音でも作り直す（名指ししたものだけ）")
    args = p.parse_args()
    save_result("ACK-CLIPS", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
