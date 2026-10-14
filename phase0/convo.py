"""台本どおりの通話を1本動かす唯一の場所（G-6・G-16・G-13・G-5 で使う）。

- 指示文は instruction.build、操作の宣言は flowctl.tool_declarations、操作への返事は flowctl.Flow が作る（どれも1か所）。
- 相手の発話は文字で送る（流れの決まりを、毎回同じ入力で確かめるため）。
- AIの発話は、ターンごとに (1) AIの1ターンの見分け方（turn_check）と (2) 別の耳（listen）で確かめる。
  ⛔ AI自身の文字起こしは、口から出た声の確かめに使わない（G-7・G-17：届かない・言い間違いが写らない）。
- swap_before：その番号の相手の発話の前で、セッションを閉じて開き直す（張り替え。DESIGN.md 6-6・G-5）。
  開き直すときは、instruction.build に流れの制御のいまの位置を渡す。
- 相手の発話は、文字（str）か、録った声（{"wav": ファイル名}。results/callee/ の電話の音質の声）。声のときは、
  実際の通話と同じく少しずつ送り、話し終わりのあとは無音を送りながら返事を待つ（live_util.callee_turn）。
- opening_mode="recorded"：名乗りと最初の質問は録音で流した形（DESIGN.md 6-13 の案）。AIは相手の最初の発話から続ける。
  barge_in＝(届いた文, まだの文)：録音の途中で相手が話し始めて止めた形。最初の相手の発話の前に、どこまで届いたかを
  AIへ知らせる（instruction.barge_in_note・live_util.send_note）。
- gap_s：相手の発話を送る前に空ける秒数（ふだんは0。別の耳を会話の途中で動かすと、その間だけ間が空く。G-26）。
- 見張り（turn_check）が引っかかったターンは、AIの声を results/convo_audio/ に残す（人が聞いて確かめるため。GitHub には上げない）。
  save_audio="all" なら、全部のターンを残す（ユーザーが聞いて決める試験。G-22）。
- stall_at：AIが止まったときの張り替え（DESIGN.md 6-7 の案）を試す。その番号の相手の発話を古いセッションへ送り、
  返事を待たずに閉じ、新しいセッションへ同じ発話を送り直す（相手は1回しか話していないので、相手の発話は1回と数える）。
"""
from __future__ import annotations

import asyncio
import time

from common import HERE, read_wav_pcm16_mono, write_wav
from flowctl import Flow, tool_declarations
from instruction import Business, barge_in_note, build
from listen import hear
from live_util import build_config, callee_turn, collect_turn, send_note, send_text, silence_while
from turn_check import BAD_KINDS, check_ai_turn

AUDIO_DIR = HERE / "results" / "convo_audio"


CUT_IN_S = 0.05  # 相手の声を送り終えた直後に、AIの声がもうできていた＝相手の言葉の途中で話し出していた（受け取りの処理は送り終えてから始まる）


async def run_call(client, model: str, business: Business, voice_type: str, callee_lines: list, *,
                   swap_before: int | None = None, stall_at: int | None = None, stall_wait_s: float = 1.0,
                   opening_mode: str = "ai", barge_in: tuple[list[str], list[str]] | None = None,
                   thinking_budget: int | None = None, speech_end: dict | None = None, use_ear: bool = True, save_audio: str = "bad",
                   audio_label: str = "", confirm=None, gap_s: float = 0.0, lead_ins: bool = True,
                   followup_wait_s: float | None = None, ack_first: bool = False) -> dict:
    """followup_wait_s：相手の言葉の途中でAIが話し出していたら（CUT_IN_S）、相手の続きの言葉への返事を、次の台詞を送る前に
    この秒数まで、もう1回ぶん待つ（来なければ、相手が黙って待たされたことになる。記録の行は callee={"followup_of": 台詞}）。G-31"""
    # ack_first：受けの言葉が先の形（AIはまず受けの言葉を言い、返事を待たずに報告する。DESIGN.md 6-15 の案・G-33）
    flow = Flow(business.questions, busy_ask=business.busy_ask, lead_ins=lead_ins, ack_first=ack_first,
                **({"confirm": confirm} if confirm else {}))  # confirm：言い直しの言い方（G-25 の比べだけ）。lead_ins：G-27
    turns: list[dict] = []
    sessions = 0
    replay: int | None = None  # 送り直す相手の発話の番号（止まったときの張り替え）

    def config():
        voice, text = build(business, voice_type, resume=flow if sessions > 1 else None, opening_mode=opening_mode, ack_first=ack_first)
        return voice, build_config(voice=voice, system_instruction=text, tools=tool_declarations(business.questions, non_blocking=ack_first),
                                   thinking_budget=thinking_budget, speech_end=speech_end)

    async def add_turn(turn, line, log_start: int, i: int, voice: str) -> None:
        """AIの1ターンぶんを記録する唯一の場所。"""
        r = turn.summary()
        audio, rate = bytes(turn.audio), turn.audio_rate or 24000
        row = {"index": i, "session": sessions, "callee": line, "heard": r["input_transcript"], "intended": r["output_transcript"],
               # 操作の記録は、流れの制御の記録（中の名前）から取る。時刻はAIからの届いた順に合わせる
               "tool_events": [{"at_s": live.get("at_s"), "name": log["op"], "args": log["args"], "reply": log["reply"].get("result")}
                               for live, log in zip([e for e in r["events"] if e["kind"] == "tool_call"], flow.log[log_start:])],
               "first_audio_after_s": r["first_audio_after_s"], "finished_after_s": r["finished_after_s"],
               "audio_seconds": r["audio_seconds"], "timed_out": turn.timed_out,
               # AI自身の文字が届いた時刻と、その時点で受け取り済みの声の長さ（文字が声より先に届くかを測る）
               "said_events": [{k: e[k] for k in ("at_s", "audio_s", "text") if k in e} for e in r["events"] if e["kind"] == "ai_said"],
               # 声のまとまりの始まり・返事を送った時刻（受けの言葉が先の形で、受けの言葉と続きの間を測る。G-33）
               "timeline": [{k: e[k] for k in ("at_s", "kind", "chunk") if k in e} for e in r["events"]
                            if e["kind"] in ("audio_start", "tool_response_sent", "turn_complete", "interrupted")],
               "phase_after": flow.phase, "current_after": flow.current}
        if use_ear and audio:
            row["ear"] = await hear(client, audio, rate, "transcribe35")
        # 見張りは別の耳の文字のあとで当てる（何語を話したかは、口から出た声で決める。turn_check）
        row["check"] = check_ai_turn(audio=audio, rate=rate, voice=voice, timed_out=turn.timed_out,
                                     intended_text=r["output_transcript"] or None, heard_text=row.get("ear"))
        if audio and (save_audio == "all" or (save_audio == "bad" and row["check"]["kind"] in BAD_KINDS)):
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            label = f"{audio_label}_" if audio_label else ""
            part = "f" if isinstance(line, dict) and "followup_of" in line else ""
            path = AUDIO_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{label}{voice_type}_{i}{part}_{row['check']['kind']}.wav"
            write_wav(path, audio, rate)
            row["audio_file"] = path.name
        turns.append(row)

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
                if i == 0 and barge_in is not None and sessions == 1:
                    await send_note(s, barge_in_note(business, *barge_in))  # 録音のどこまでが届いたか
                if replay != i:  # 送り直しは、相手がもう一度話したのではないので数えない
                    flow.on_callee()
                log_start = len(flow.log)
                if gap_s and i > 0:  # 相手が話し始めるまでの間（G-26：間の長さで、AIのふるまいが変わるかを見る）
                    await asyncio.sleep(gap_s)
                if isinstance(line, dict):  # 録った相手の声
                    pcm, prate = read_wav_pcm16_mono(HERE / "results" / "callee" / line["wav"])
                    turn = await callee_turn(s, pcm, prate, on_tool_call=flow.handle, timeout_s=90, non_blocking=ack_first)
                else:
                    t = time.monotonic()
                    await send_text(s, line)
                    turn = await collect_turn(s, started_at=t, on_tool_call=flow.handle, timeout_s=90, non_blocking=ack_first)
                await add_turn(turn, line, log_start, i, voice)
                if followup_wait_s and turn.first_audio_after_s is not None and turn.first_audio_after_s < CUT_IN_S:
                    log_start = len(flow.log)
                    # 待つ間も、電話と同じく無音を送り続ける（会話AIは、無音が続くことで相手の話し終わりを知る）
                    # ⛔ 2026-10-14 G-31 の1回目：無音を送らずに待っていたため、相手の続きの言葉の話し終わりが会話AIに伝わらず、
                    #    返事が来なかった（試験の作りの誤り。本物の電話では無音が流れ続ける）
                    async with silence_while(s, prate if isinstance(line, dict) else 16000):
                        follow = await collect_turn(s, started_at=time.monotonic(), on_tool_call=flow.handle, timeout_s=followup_wait_s,
                                                    non_blocking=ack_first)
                    await add_turn(follow, {"followup_of": line}, log_start, i, voice)
                i += 1
    return {"voice_type": voice_type, "sessions": sessions, "turns": turns, "flow_log": flow.log,
            "items": {k: {"state": v.state, "value": v.value, "condition": v.condition, "ask_backs": v.ask_backs,
                          "rechecks": v.rechecks, "corrections": v.corrections} for k, v in flow.items.items()},
            "export": flow.export(), "final_phase": flow.phase, "hurried": flow.hurried,
            "opening_mode": opening_mode, "barge_in": barge_in}
