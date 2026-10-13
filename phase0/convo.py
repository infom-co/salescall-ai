"""台本どおりの通話を1本動かす唯一の場所（G-6・G-16・G-13・G-5 で使う）。

- 指示文は instruction.build、操作の宣言は flowctl.tool_declarations、操作への返事は flowctl.Flow が作る（どれも1か所）。
- 相手の発話は文字で送る（流れの決まりを、毎回同じ入力で確かめるため）。
- AIの発話は、ターンごとに (1) AIの1ターンの見分け方（turn_check）と (2) 別の耳（listen）で確かめる。
  ⛔ AI自身の文字起こしは、口から出た声の確かめに使わない（G-7・G-17：届かない・言い間違いが写らない）。
- swap_before：その番号の相手の発話の前で、セッションを閉じて開き直す（張り替え。DESIGN.md 6-6・G-5）。
  開き直すときは、instruction.build に流れの制御のいまの位置を渡す。
- stall_at：AIが止まったときの張り替え（DESIGN.md 6-7 の案）を試す。その番号の相手の発話を古いセッションへ送り、
  返事を待たずに閉じ、新しいセッションへ同じ発話を送り直す（相手は1回しか話していないので、相手の発話は1回と数える）。
"""
from __future__ import annotations

import asyncio
import time

from flowctl import Flow, tool_declarations
from instruction import Business, build
from listen import hear
from live_util import build_config, collect_turn, send_text
from turn_check import check_ai_turn


async def run_call(client, model: str, business: Business, voice_type: str, callee_lines: list[str], *,
                   swap_before: int | None = None, stall_at: int | None = None, stall_wait_s: float = 1.0,
                   thinking_budget: int | None = None, use_ear: bool = True) -> dict:
    flow = Flow(business.questions)
    turns: list[dict] = []
    sessions = 0
    replay: int | None = None  # 送り直す相手の発話の番号（止まったときの張り替え）

    def config():
        voice, text = build(business, voice_type, resume=flow if sessions > 1 else None)
        return voice, build_config(voice=voice, system_instruction=text, tools=tool_declarations(business.questions),
                                   thinking_budget=thinking_budget)

    i = 0
    while i < len(callee_lines) and flow.phase != "closing":
        sessions += 1
        voice, cfg = config()
        async with client.aio.live.connect(model=model, config=cfg) as s:
            while i < len(callee_lines) and flow.phase != "closing":
                if swap_before is not None and i == swap_before and sessions == 1:
                    break  # 張り替え：このセッションを閉じて、いまの位置を渡して開き直す
                line = callee_lines[i]
                if stall_at is not None and i == stall_at and sessions == 1:
                    # AIが止まった：古いセッションへ相手の発話を送り、返事を受け取らずに閉じる
                    flow.on_callee()
                    await send_text(s, line)
                    await asyncio.sleep(stall_wait_s)
                    replay = i
                    break
                t = time.monotonic()
                if replay != i:  # 送り直しは、相手がもう一度話したのではないので数えない
                    flow.on_callee()
                await send_text(s, line)
                turn = await collect_turn(s, started_at=t, on_tool_call=flow.handle, timeout_s=90)
                r = turn.summary()
                audio, rate = bytes(turn.audio), turn.audio_rate or 24000
                row = {"index": i, "session": sessions, "callee": line, "intended": r["output_transcript"],
                       "tool_events": [e for e in r["events"] if e["kind"] == "tool_call"],
                       "first_audio_after_s": r["first_audio_after_s"], "timed_out": turn.timed_out,
                       "check": check_ai_turn(audio=audio, rate=rate, voice=voice, timed_out=turn.timed_out,
                                              intended_text=r["output_transcript"] or None),
                       "phase_after": flow.phase, "current_after": flow.current}
                if use_ear and audio:
                    row["ear"] = await hear(client, audio, rate, "transcribe35")
                turns.append(row)
                i += 1
    return {"voice_type": voice_type, "sessions": sessions, "turns": turns, "flow_log": flow.log,
            "items": {k: {"state": v.state, "value": v.value, "condition": v.condition, "ask_backs": v.ask_backs,
                          "rechecks": v.rechecks, "corrections": v.corrections} for k, v in flow.items.items()},
            "export": flow.export(), "final_phase": flow.phase, "hurried": flow.hurried}
