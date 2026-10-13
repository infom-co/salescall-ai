"""G-18：AIの発話が途中で止まったとき、「失礼いたしました」と言ってから言い直させる対処が働くか（DESIGN.md 6-7 の案）。

やること：
1. 声の型（S2・S4・L4）で、会話の途中の一言（台本の1文）を言わせる。
2. 「途中で止まった」と見分けたことにして、決まった文面の合図（RESTATE_CUE）を文字で送る。
3. AIの言い直しを、AIの1ターンの見分け方（turn_check）と、別の耳（listen）で確かめる。
   合格＝「失礼いたしました」＋同じ文を、抜けや言い換え無しに、1回だけ言う（長さも「ふつう」）。
⚠️ 限界：本当に止まった直後ではなく、ふつうに言い終えたあとに合図を送っている（止まる現象はわざと起こせないため）。
   ⇒ 本当に止まったあとの言い直しは、フェーズ1の通話の記録で確かめる。
"""
from __future__ import annotations

import asyncio
import json
import time

from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from g03_voices import INSTRUCTION
from instruction import VOICE_TYPES, voice_style
from listen import differences, hear
from live_util import build_config, collect_turn, send_text
from turn_check import check_ai_turn

# AIへの合図の文面（プログラムからAIへ文字で送る。相手には聞こえない）。DESIGN.md 6-7 の候補。
RESTATE_CUE = ("直前のあなたの発言は、途中で途切れて、相手に届いていません。"
               "「失礼いたしました。」と言ってから、同じ内容を最初から、もう一度だけ言ってください。")
APOLOGY = "失礼いたしました。"
TYPES = VOICE_TYPES  # 声の型は instruction.py の1か所から
LINES = [  # 会話の途中で言う一言（名前は「」でくくる）
    "ご担当の「田中」様はいらっしゃいますでしょうか。",
    "来週の火曜日、10月20日の午後3時に、改めてお電話してもよろしいでしょうか。",
    "株式会社「アオバ物流」の「佐藤」と申します。",
]
AUDIO_DIR = HERE / "results" / "g18_audio"


async def one(client, args, tid: str, type_id: str, line: str) -> dict:
    voice, style_text = voice_style(type_id)
    cfg = build_config(voice=voice, system_instruction=INSTRUCTION + style_text)
    row = {"id": tid, "type": type_id, "voice": voice, "line": line}
    async with client.aio.live.connect(model=args.model, config=cfg) as s:
        t = time.monotonic()
        await send_text(s, f"台本：{line}")
        first = await collect_turn(s, started_at=t, timeout_s=60)
        t = time.monotonic()
        await send_text(s, RESTATE_CUE)
        again = await collect_turn(s, started_at=t, timeout_s=60)
    expected = APOLOGY + line
    for name, turn, exp in (("first", first, line), ("restated", again, expected)):
        r = turn.summary()
        row[name] = {"intended": r["output_transcript"], "first_audio_after_s": r["first_audio_after_s"],
                     "check": check_ai_turn(audio=bytes(turn.audio), rate=turn.audio_rate or 24000, voice=voice,
                                            timed_out=turn.timed_out, expected_text=exp)}
    if again.audio:
        wav = AUDIO_DIR / f"{tid}.wav"
        write_wav(wav, bytes(again.audio), again.audio_rate or 24000)
        row["wav"] = wav.name
        row["ear"] = await hear(client, bytes(again.audio), again.audio_rate or 24000, "transcribe35")
        row["ear_diffs"] = differences(expected, row["ear"])
    row["passed"] = row["restated"]["check"]["kind"] == "ok" and row.get("ear_diffs") == []
    return row


async def run(args) -> dict:
    if args.dry_run:
        for type_id in TYPES:
            voice, style_text = voice_style(type_id)
            build_config(voice=voice, system_instruction=INSTRUCTION + style_text)
        return {"dry_run": True}
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    client = make_client(pick_key(args))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    rows, n = [], 0
    for rep in range(args.repeat):
        for line in LINES:
            for type_id in args.type or TYPES:
                n += 1
                tid = f"{stamp}_{n:03d}_{type_id}"
                try:
                    row = await one(client, args, tid, type_id, line)
                except Exception as exc:
                    row = {"id": tid, "type": type_id, "line": line, "error": describe_error(exc)}
                rows.append(row)
                say(f"[{n}] {type_id} 合格={row.get('passed')} 言い直しの判定={row.get('restated', {}).get('check', {}).get('kind')} "
                    f"耳の食い違い={json.dumps(row.get('ear_diffs'), ensure_ascii=False)[:160]} {row.get('error', '')}")
                await asyncio.sleep(args.pause)
    summary = {t: {"trials": sum(1 for r in rows if r["type"] == t),
                   "passed": sum(1 for r in rows if r["type"] == t and r.get("passed"))} for t in TYPES}
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "cue": RESTATE_CUE, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--pause", type=float, default=4.0)
    p.add_argument("--type", choices=sorted(TYPES), action="append")
    args = p.parse_args()
    save_result("G-18", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
