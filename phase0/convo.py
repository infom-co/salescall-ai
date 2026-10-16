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
- 相手の発話の代わりに {"silence_s": 秒} を置くと、相手が黙っている間（考えている途中など）を作る：無音だけをその秒数送り、
  その間にAIが話し出すかを記録する（話さなければ、記録の行は timed_out＝黙って待った）。相手の発話としては数えない。G-34
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
# お客役の声（✅ 2026-10-14 ユーザー：Leda と Sadachbia に入れ替わりでお客役をさせる）：会話のAIの声と違う声にする
CALLEE_FOR = {"Sadachbia": "Leda", "Leda": "Sadachbia"}


def callee_voice_for(voice_type: str) -> str:
    """会話のAIの声の型に対する、お客役の声（同じ声どうしで話させない）。"""
    from instruction import voice_style
    return CALLEE_FOR[voice_style(voice_type)[0]]


def callee_wav(name: str, callee_voice: str | None) -> str:
    """台詞の声のファイル名。台詞は「Puck_（台詞の名前）.wav」で書き、お客役の声を指定したら、その声のファイルに読み替える。"""
    if callee_voice and name.startswith("Puck_"):
        return f"{callee_voice}_{name[len('Puck_'):]}"
    return name


MAX_AGAIN = 5  # 試験の歯止め（聞き返しの回数は、流れの制御が決める。これはその決まりが壊れたときに、通話を終わらせるためだけ）


def asked_back(entries: list[dict]) -> bool:
    """そのターンで受け付けた最後の報告への返事が、聞き返しか（流れの制御の記録から読む）。"""
    accepted = [e for e in entries if (e.get("reply") or {}).get("result") != "rejected"]
    return bool(accepted) and accepted[-1]["reply"].get("result") == "ask_back"


def followup_reason(turn) -> str | None:
    """相手の続きの言葉への返事を、次の台詞を送る前に、もう1回ぶん待つ理由（待たなくてよいなら None）。
    - "cut_in"：相手の言葉の途中で、AIが話し出していた（G-31）
    - "waited"：AIが声も報告も出さずにターンを終えた（相手の「えーとですね、」のあと、黙って待った形）
      ⛔ 2026-10-14 G-34 の1回目：この空のターンを「AIの返事」と受け取って、すぐ次の台詞を送ったため、相手の続きへのAIの返事
         （言い直し）に次の台詞「はい、そうです。」が重なり、会話が1つずれた（試験の作りの誤り。本物の電話では、相手は続きを言い終えて、
         AIの返事を待つ）"""
    if turn.timed_out:
        return None
    if turn.first_audio_after_s is not None and turn.first_audio_after_s < CUT_IN_S:
        return "cut_in"
    if turn.first_audio_after_s is None and not turn.tool_calls:
        return "waited"  # 声も報告も無い
    return None


async def run_call(client, model: str, business: Business, voice_type: str, callee_lines: list, *,
                   swap_before: int | None = None, stall_at: int | None = None, stall_wait_s: float = 1.0,
                   opening_mode: str = "ai", barge_in: tuple[list[str], list[str]] | None = None,
                   thinking_budget: int | None = None, speech_end: dict | None = None, use_ear: bool = True, save_audio: str = "bad",
                   audio_label: str = "", confirm=None, gap_s: float = 0.0, lead_ins: bool = True,
                   followup_wait_s: float | None = None, callee_voice: str | None = None, play_recordings: bool = False) -> dict:
    """followup_wait_s：相手の言葉の途中でAIが話し出していたとき・AIが黙って待ったとき（followup_reason）は、相手の続きの言葉への
    返事を、次の台詞を送る前に、この秒数まで、もう1回ぶん待つ（来なければ、相手が黙って待たされたことになる。
    記録の行は callee={"followup_of": 台詞, "reason": 理由}）。G-31・G-34
    callee_voice：お客役の声（"swap"＝会話のAIの声と入れ替え。callee_voice_for）。台詞の声を、その声のファイルに読み替える（G-36〜）"""
    if callee_voice == "swap":
        callee_voice = callee_voice_for(voice_type)
    callee_lines = [{**x, "wav": callee_wav(x["wav"], callee_voice), **({"again": callee_wav(x["again"], callee_voice)} if "again" in x else {})}
                    if isinstance(x, dict) and "wav" in x else
                    {**x, "yes": callee_wav(x["yes"], callee_voice), "no": callee_wav(x["no"], callee_voice)}
                    if isinstance(x, dict) and "if_value" in x else x for x in callee_lines]
    flow = Flow(business.questions, busy_ask=business.busy_ask, lead_ins=lead_ins, play_recordings=play_recordings,
                **({"confirm": confirm} if confirm else {}))  # confirm：言い直しの言い方（G-25 の比べだけ）。lead_ins：G-27
    turns: list[dict] = []
    sessions = 0
    replay: int | None = None  # 送り直す相手の発話の番号（止まったときの張り替え）
    again: dict | None = None  # 聞き返されたので、相手が次に言い直す台詞（台詞の "again"。i は進めない）
    again_n = 0

    def config():
        voice, text = build(business, voice_type, resume=flow if sessions > 1 else None, opening_mode=opening_mode,
                            play_recordings=play_recordings)
        return voice, build_config(voice=voice, system_instruction=text, tools=tool_declarations(business.questions),
                                   thinking_budget=thinking_budget, speech_end=speech_end)

    async def add_turn(turn, line, log_start: int, i: int, voice: str) -> None:
        """AIの1ターンぶんを記録する唯一の場所。"""
        r = turn.summary()
        audio, rate = bytes(turn.audio), turn.audio_rate or 24000
        row = {"index": i, "session": sessions, "callee": line, "heard": r["input_transcript"], "intended": r["output_transcript"],
               # 操作の記録は、流れの制御の記録（中の名前）から取る。時刻はAIからの届いた順に合わせる。
               # ack：その報告が届いた瞬間に、電話接続側が流す相づち（flowctl.Flow._ack。試験では流さず、時刻と中身だけを記録する）
               "tool_events": [{"at_s": live.get("at_s"), "name": log["op"], "args": log["args"], "reply": log["reply"].get("result"),
                                "ack": log["reply"].get("ack"), "say": log["reply"].get("say"), "play": log["reply"].get("play")}
                               for live, log in zip([e for e in r["events"] if e["kind"] == "tool_call"], flow.log[log_start:])],
               "first_audio_after_s": r["first_audio_after_s"], "finished_after_s": r["finished_after_s"],
               "audio_seconds": r["audio_seconds"], "timed_out": turn.timed_out,
               # AI自身の文字が届いた時刻と、その時点で受け取り済みの声の長さ（文字が声より先に届くかを測る）
               "said_events": [{k: e[k] for k in ("at_s", "audio_s", "text") if k in e} for e in r["events"] if e["kind"] == "ai_said"],
               # 相手の言葉の文字が届いた時刻（報告より先に届くか＝報告の時点でプログラムが相手の言葉を見て決められるか。G-36 の3回目）
               "heard_events": [{k: e[k] for k in ("at_s", "text")} for e in r["events"] if e["kind"] == "heard"],
               # 声のまとまりの始まり・返事を送った時刻（報告の返事の前と後の声を分けて、間を測る）
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
                line = again or callee_lines[i]
                if isinstance(line, dict) and "if_value" in line:
                    # こちらの言った値で答えが変わる相手（G-46：名前の読みを違う読みで聞かれたら、もう一度直す）。
                    # その項目がもう確かめ済みなら、この台詞は言わない（次の台詞へ）
                    qid, want = line["if_value"]
                    if flow.items[qid].state == "confirmed":
                        i += 1
                        continue
                    line = {"wav": line["yes"] if flow.items[qid].value == want else line["no"]}
                if isinstance(line, dict) and "silence_s" in line:
                    log_start = len(flow.log)
                    async with silence_while(s):
                        turn = await collect_turn(s, started_at=time.monotonic(), on_tool_call=flow.handle_call, timeout_s=line["silence_s"],
                                                 on_heard=flow.on_heard)
                    await add_turn(turn, line, log_start, i, voice)
                    i += 1
                    continue
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
                    turn = await callee_turn(s, pcm, prate, on_tool_call=flow.handle_call, timeout_s=90, on_heard=flow.on_heard)
                else:
                    t = time.monotonic()
                    flow.on_heard(line)  # 文字で送る相手の言葉は、そのまま相手の言葉の文字
                    await send_text(s, line)
                    turn = await collect_turn(s, started_at=t, on_tool_call=flow.handle_call, timeout_s=90, on_heard=flow.on_heard)
                await add_turn(turn, line, log_start, i, voice)
                turn_log_start = log_start
                reason = followup_reason(turn) if followup_wait_s else None
                if reason:
                    log_start = len(flow.log)
                    # 待つ間も、電話と同じく無音を送り続ける（会話AIは、無音が続くことで相手の話し終わりを知る）
                    # ⛔ 2026-10-14 G-31 の1回目：無音を送らずに待っていたため、相手の続きの言葉の話し終わりが会話AIに伝わらず、
                    #    返事が来なかった（試験の作りの誤り。本物の電話では無音が流れ続ける）
                    async with silence_while(s, prate if isinstance(line, dict) else 16000):
                        follow = await collect_turn(s, started_at=time.monotonic(), on_tool_call=flow.handle_call, timeout_s=followup_wait_s,
                                                    on_heard=flow.on_heard)
                    await add_turn(follow, {"followup_of": line, "reason": reason}, log_start, i, voice)
                if isinstance(line, dict) and "again" in line and asked_back(flow.log[turn_log_start:]) and again_n < MAX_AGAIN:
                    # 聞き返された：本物の相手は、次の台詞へは進まず、聞かれたことに答え直す（⛔ 2026-10-14 G-36：台本の相手が聞き返しの
                    # あとも次の台詞「はい、そうです」「3人です」へ進み、それが日時の答えとして報告された＝試験の作りの誤り）
                    again, again_n = {"wav": line["again"], "again": line["again"]}, again_n + 1
                    continue
                again, again_n = None, 0
                i += 1
    return {"voice_type": voice_type, "sessions": sessions, "turns": turns, "flow_log": flow.log,
            "items": {k: {"state": v.state, "value": v.value, "condition": v.condition, "ask_backs": v.ask_backs,
                          "rechecks": v.rechecks, "corrections": v.corrections} for k, v in flow.items.items()},
            "export": flow.export(), "final_phase": flow.phase, "hurried": flow.hurried,
            "opening_mode": opening_mode, "barge_in": barge_in}
