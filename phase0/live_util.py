"""フェーズ0で Gemini Live を試すときの共通部分（設定の組み立てと、1ターンの受け取り）。

- 設定を組み立てる場所は `build_config` の1つだけ（DESIGN.md 6-6 の start_session と同じ考え方）。
  どの確認スクリプトも、ここを通してセッションを開く。
- 受け取りは `collect_turn` の1つだけ。音声・文字起こし・利用量・関数の呼び出し・切れる前の知らせを、
  同じ形で記録する。時間切れでも、そこまでの記録を返す（`timed_out`）。
- 音声を送るのは `stream_audio` の1つだけ。無音を送り続けるのは `silence_while` の1つだけ。
  相手の発話1回ぶん（送る→話し終わりに気づかせる→AIの返事を受け取る）は `callee_turn` の1つだけ。
"""
from __future__ import annotations

import asyncio
import inspect
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from common import INPUT_RATE, parse_rate, redact, silence


def build_config(
    *,
    voice: str | None,
    system_instruction: str,
    tools: list | None = None,
    transcription: bool = True,
    resumption_handle: str | None = None,
    enable_resumption: bool = False,
    language_code: str | None = "ja-JP",
    thinking_budget: int | None = None,
    speech_end: dict | None = None,
):
    """Live のセッション設定を作る唯一の場所。

    thinking_budget：None＝既定（自動）／0＝考えない／正の数＝考える量の上限。
    speech_end：相手の話し終わりの判定（None＝既定）。{"sensitivity": "HIGH" か "LOW", "silence_ms": 無音を待つ長さ}。
      会話の間を縮めるための試験（G-30。DESIGN.md 6-15）。⚠️ 速くすると、相手が言葉の途中で間をあけたときにAIが割り込む。
    ⛔「考えるレベル」（thinking_level）は gemini-3.8-live では受け付けられない（2026-10-12 G-11：1007 で拒否）。
    """
    from google.genai import types

    if not system_instruction.strip():
        # DESIGN.md I-15：指示文を渡さずにセッションを開かない
        raise ValueError("system_instruction が空です（指示文なしではセッションを開かない）")

    speech = None
    if voice or language_code:
        speech = types.SpeechConfig(
            voice_config=(
                types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)) if voice else None
            ),
            language_code=language_code,
        )
    cfg = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=speech,
        system_instruction=types.Content(parts=[types.Part(text=system_instruction)]),
        tools=tools,
        input_audio_transcription=types.AudioTranscriptionConfig() if transcription else None,
        output_audio_transcription=types.AudioTranscriptionConfig() if transcription else None,
        session_resumption=(
            types.SessionResumptionConfig(handle=resumption_handle) if (enable_resumption or resumption_handle) else None
        ),
        thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget) if thinking_budget is not None else None,
        realtime_input_config=_realtime_input(speech_end),
    )
    return cfg


def _realtime_input(speech_end: dict | None):
    from google.genai import types

    if not speech_end:
        return None
    sens = {"HIGH": types.EndSensitivity.END_SENSITIVITY_HIGH, "LOW": types.EndSensitivity.END_SENSITIVITY_LOW}
    return types.RealtimeInputConfig(automatic_activity_detection=types.AutomaticActivityDetection(
        end_of_speech_sensitivity=sens[speech_end.get("sensitivity", "HIGH")], silence_duration_ms=speech_end.get("silence_ms")))


@dataclass
class TurnResult:
    audio: bytearray = field(default_factory=bytearray)
    audio_mime: str | None = None
    audio_rate: int | None = None
    input_transcript: list[str] = field(default_factory=list)
    output_transcript: list[str] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    go_away_time_left: list[str] = field(default_factory=list)
    resumption_handles: list[str] = field(default_factory=list)
    interrupted: bool = False
    timed_out: bool = False  # 決めた秒数のうちに、AIがターンを終えなかった（黙った・止まった）
    nudged: bool = False     # 声が出ないので、返事を促す合図を送った（DESIGN.md 6-7）
    silent: bool = False     # 決めた秒数のうちに、声も操作も出なかった（AIが黙った。silent_after_s。DESIGN.md 6-7 の張り替えへ）
    expects_speech: bool = False  # 話させる形の返事を送った（このあとAIが話すはず）
    off_script: bool = False      # AIの声が、渡した言い方から外れたので止めた（off_script の見張り。DESIGN.md 6-7）
    first_audio_after_s: float | None = None
    finished_after_s: float | None = None
    message_kinds: dict[str, int] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)  # 起きた順の記録（話した順番を確かめるため）

    def summary(self) -> dict:
        return {
            "audio_bytes": len(self.audio),
            "audio_mime": self.audio_mime,
            "audio_rate": self.audio_rate,
            "audio_seconds": round(len(self.audio) / 2 / self.audio_rate, 2) if self.audio_rate else None,
            "input_transcript": "".join(self.input_transcript),
            "output_transcript": "".join(self.output_transcript),
            "usage": self.usage,
            "tool_calls": self.tool_calls,
            "go_away_time_left": self.go_away_time_left,
            "resumption_handle_received": bool(self.resumption_handles),
            "interrupted": self.interrupted,
            "timed_out": self.timed_out,
            "nudged": self.nudged,
            "first_audio_after_s": self.first_audio_after_s,
            "finished_after_s": self.finished_after_s,
            "message_kinds": self.message_kinds,
            "events": self.events,
        }


async def collect_turn(session, *, started_at: float, on_tool_call=None, timeout_s: float = 60.0,
                       nudge_after_s: float | None = None, nudge_text: str | None = None, on_heard=None,
                       silent_after_s: float | None = None, off_script=None) -> TurnResult:
    """1ターン分を受け取る。関数の呼び出しが来たら `on_tool_call(function_call) -> dict` で返事を作って返す。

    nudge_after_s：この秒数のあいだ声が出なければ、nudge_text を1回だけ送って返事を促す（AIが黙ったとき。DESIGN.md 6-7）。
    silent_after_s：この秒数のあいだ声も操作も出なければ、受け取りをやめて silent にして返す（AIが黙った＝6-7 の張り替えは呼ぶ側が決める）。
    見張りはここだけに置く（相手の声で始まるターンも、文字で始まるターンも、同じ見張りを通る）。
    ⛔ 2026-10-14 G-33：答えの報告を「返事を待たない」宣言にする形（受けの言葉が先）は、AIが受けの言葉を先に言わなかったので採らず、
       受け取りの処理も外した（記録は DESIGN.md 6-15・PHASE0_VERIFICATION.md の G-33）。
    on_heard：相手の言葉の文字（会話AIの聞き取り）が届くたびに `on_heard(text)` を呼ぶ（値を直されたときの直され方を、プログラムが
      相手の言葉から決める。flowctl.Flow.on_heard）。
    off_script：AI自身の文字が届くたびに `off_script(返事を送ったあとの文字)` を呼び、True なら受け取りをやめて off_script にして返す
      （AIの声が、渡した言い方から外れた。本番では、電話接続側がその場で相手へ流すのをやめる。外れたあとは呼ぶ側が決める＝6-7）。
    on_tool_call が (返事, True) を返したら、その返事は「話させない」形（SILENT）で返し、AIの声を待たない（電話接続側が録音を流すとき。
      G-37。⛔ 2026-10-14 に外した形 B の受け取りと同じ仕組み）。
    """
    r = TurnResult()
    task = asyncio.create_task(_collect_into(r, session, started_at=started_at, on_tool_call=on_tool_call, timeout_s=timeout_s,
                                             on_heard=on_heard, off_script=off_script))
    if nudge_after_s is not None and nudge_text:
        done, _ = await asyncio.wait({task}, timeout=nudge_after_s)
        if not done and r.first_audio_after_s is None:
            r.nudged = True
            r.events.append({"at_s": round(time.monotonic() - started_at, 3), "kind": "nudged"})
            await send_text(session, nudge_text)
    if silent_after_s is not None:
        done, _ = await asyncio.wait({task}, timeout=silent_after_s)
        # 声が出ていない：操作も無い、または話させる形の返事を送ったのに声が来ない（⛔ 2026-10-19 G-57 の3回目：報告に「言い直して」と返した
        # あと、AIが声を出さずに英語の文字だけを長く返し、操作があったので「黙った」と見なさず、90秒待った。次のターンで、AIは用件と関係の
        # ない話「実際のプレゼン資料の構成や…」を12秒話した＝壊れたつなぎを張り替えずに使い続けた）
        if not done and r.first_audio_after_s is None and (not r.tool_calls or r.expects_speech):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            r.silent = r.timed_out = True
            r.events.append({"at_s": round(time.monotonic() - started_at, 3), "kind": "silent"})
            return r
    await task
    return r


async def _collect_into(r: "TurnResult", session, *, started_at: float, on_tool_call, timeout_s: float, on_heard=None,
                        off_script=None) -> None:
    from google.genai import types

    audio_after_nudge = False
    since_reply: list[str] = []  # 返事を送ったあとの AI自身の文字（off_script の見張りに渡す）

    def at() -> float:
        return round(time.monotonic() - started_at, 3)

    async def _loop():
        nonlocal audio_after_nudge
        # 操作（関数）への返事を送ったら、AIがそれを受けて話し終えるまで受け取り続ける。
        # ⛔ 2026-10-12 の不具合：操作の入ったターンの区切りで受け取りをやめていたため、
        #    返事を受けたAIの声（聞き返し・言い直し）が、次の相手の発話のあとにずれていた。
        awaiting_reply_speech = False
        chunk = 0                  # いま受け取っている声のまとまりの番号（まとまりの終わりごとに増える）
        audio_in_chunk = False

        async def handle(msg):
            nonlocal awaiting_reply_speech, audio_after_nudge, audio_in_chunk
            _count(r, msg)
            if msg.usage_metadata is not None:
                r.usage.append(msg.usage_metadata.model_dump(exclude_none=True))
            if msg.go_away is not None:
                r.go_away_time_left.append(str(msg.go_away.time_left))
                r.events.append({"at_s": at(), "kind": "go_away", "time_left": str(msg.go_away.time_left)})
            if msg.session_resumption_update is not None and msg.session_resumption_update.new_handle:
                r.resumption_handles.append(msg.session_resumption_update.new_handle)
            responses = []  # (返事, 話させない形か)
            if msg.tool_call is not None:
                for fc in msg.tool_call.function_calls or []:
                    r.tool_calls.append({"name": fc.name, "args": fc.args, "at_s": at()})
                    out = on_tool_call(fc) if on_tool_call else {"result": "ok"}
                    if inspect.isawaitable(out):  # 返事を作る処理が待つとき（別の耳の文字を待つ。convo.run_call の ear_check。G-15）
                        out = await out
                    # on_tool_call は (返事, 話させない形か) も返せる（電話接続側が録音を流すときは、AIに話させない。G-37）
                    reply, silent = out if isinstance(out, tuple) else (out, False)
                    r.events.append({"at_s": at(), "kind": "tool_call", "name": fc.name, "args": fc.args, "reply": reply})
                    extra = {"scheduling": types.FunctionResponseScheduling.SILENT} if silent else {}
                    responses.append((types.FunctionResponse(id=fc.id, name=fc.name, response=reply, **extra), silent))
            sc = msg.server_content
            if sc is not None:
                if sc.interrupted:
                    r.interrupted = True
                    r.events.append({"at_s": at(), "kind": "interrupted"})
                if sc.input_transcription is not None and sc.input_transcription.text:
                    r.input_transcript.append(sc.input_transcription.text)
                    # 相手の言葉の文字が、AIの報告より先に届くか（報告の時点で、プログラムが相手の言葉を見て決められるか。2026-10-14 G-36：
                    # AIは、こちらの聞き違いと相手の言い間違いを見分けられなかった＝12本中12本「相手が言い直した」）
                    r.events.append({"at_s": at(), "kind": "heard", "text": sc.input_transcription.text,
                                     "finished": sc.input_transcription.finished})
                    if on_heard:
                        on_heard(sc.input_transcription.text)
                if sc.output_transcription is not None and sc.output_transcription.text:
                    r.output_transcript.append(sc.output_transcription.text)
                    since_reply.append(sc.output_transcription.text)
                    # audio_s：この文字が届いた時点で、受け取り済みの声の長さ（秒）。声は話す速さで届くので、相手に流れた長さに近い。
                    # 文字が声より先に届くかを測り、英語などをプログラムが止められるかを判断する（2026-10-13 G-16c）
                    r.events.append({"at_s": at(), "kind": "ai_said", "text": sc.output_transcription.text,
                                     "audio_s": round(len(r.audio) / 2 / (r.audio_rate or 24000), 3)})
                    if off_script is not None and not r.off_script and off_script("".join(since_reply)):
                        r.off_script = True
                        r.events.append({"at_s": at(), "kind": "off_script", "text": "".join(since_reply),
                                         "audio_s": round(len(r.audio) / 2 / (r.audio_rate or 24000), 3)})
                if sc.model_turn is not None:
                    for part in sc.model_turn.parts or []:
                        blob = part.inline_data
                        if blob is not None and blob.data:
                            if r.first_audio_after_s is None:
                                r.first_audio_after_s = at()
                            if not audio_in_chunk:  # 声のまとまりの始まり（報告の返事の前と後の声を分けて、間を測る）
                                r.events.append({"at_s": at(), "kind": "audio_start", "chunk": chunk})
                            audio_in_chunk = True
                            r.audio_mime = r.audio_mime or blob.mime_type
                            r.audio_rate = r.audio_rate or parse_rate(blob.mime_type, 24000)
                            r.audio.extend(blob.data)
                            awaiting_reply_speech = False
                            if r.nudged:
                                audio_after_nudge = True
            return responses

        while True:
            async for msg in session.receive():
                responses = await handle(msg)
                if r.off_script:
                    r.finished_after_s = at()
                    return  # 外れた：受け取りをやめる（このつなぎは、呼ぶ側が張り替える）
                if responses:
                    since_reply.clear()
                    await session.send_tool_response(function_responses=[x for x, _ in responses])
                    r.events.append({"at_s": at(), "kind": "tool_response_sent", "chunk": chunk})
                    if not all(silent for _, silent in responses):
                        awaiting_reply_speech = True  # 話させない返事だけなら、AIの声を待たない
                        r.expects_speech = True
            r.events.append({"at_s": at(), "kind": "turn_complete"})
            audio_in_chunk = False
            chunk += 1
            # 返事を促したのに、まだ声が来ていなければ、促しへの返事（次のターン）まで受け取る
            # （受け取らずに終えると、その返事が次の相手の発話のあとにずれて記録される）
            if not awaiting_reply_speech and not (r.nudged and not audio_after_nudge):
                break
        r.finished_after_s = at()

    try:
        await asyncio.wait_for(_loop(), timeout=timeout_s)
    except asyncio.TimeoutError:
        # ⛔ 2026-10-12 G-17：時間切れで例外を投げていたため、どのターンで、どこまで声が出ていたかが記録に残らなかった
        r.timed_out = True
        r.events.append({"at_s": at(), "kind": "timed_out"})


def _count(r: TurnResult, msg) -> None:
    for name in ("setup_complete", "server_content", "tool_call", "tool_call_cancellation", "usage_metadata",
                 "go_away", "session_resumption_update", "voice_activity", "voice_activity_detection_signal"):
        if getattr(msg, name, None) is not None:
            r.message_kinds[name] = r.message_kinds.get(name, 0) + 1


async def send_note(session, text: str) -> None:
    """AIへのお知らせを、返事をさせずに会話の中へ入れる（このあと届く相手の声に、まとめて返事をさせるため）。"""
    from google.genai import types

    await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=False)


async def send_text(session, text: str) -> None:
    """相手の発話の代わりに、文字で1ターン送る（音声を用意しなくても流れを確かめられる）。"""
    from google.genai import types

    await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)


async def stream_audio(session, pcm16: bytes, rate: int, *, chunk_ms: int = 100, realtime: bool = True) -> None:
    """音声を、実際の通話と同じように少しずつ送る（音声を送る唯一の場所）。"""
    from google.genai import types

    mime = f"audio/pcm;rate={rate}"
    step = rate * 2 * chunk_ms // 1000
    for i in range(0, len(pcm16), step):
        await session.send_realtime_input(audio=types.Blob(data=pcm16[i : i + step], mime_type=mime))
        if realtime:
            await asyncio.sleep(chunk_ms / 1000)


@asynccontextmanager
async def open_session(client, model: str, config, closing: list):
    """通話の中で会話AIのつなぎを開く唯一の場所（つなぎ直し＝DESIGN.md 6-7 も、ここを通る）。閉じるのは待たない：閉じる処理は後ろで走らせ、
    closing に足す（通話の終わりにまとめて待つ）。⛔ 2026-10-19 G-57 の言い間違えの直し：古いつなぎを閉じるのを待ってから新しいつなぎを
    開いていたので、つなぎ直しのたびに約10秒、相手に何も聞こえない時間ができた（閉じる 10.0秒・開く 0.37秒・知らせてから声 0.49秒）。
    黙ったときの張り替えも同じだった。"""
    cm = client.aio.live.connect(model=model, config=config)
    session = await cm.__aenter__()
    try:
        yield session
    finally:
        closing.append(asyncio.create_task(cm.__aexit__(None, None, None)))


@asynccontextmanager
async def silence_while(session, rate: int = INPUT_RATE, chunk_ms: int = 100):
    """この中にいる間、無音を送り続ける（話し終わりに気づかせる。電話がつながっている間と同じ状態にする）。"""
    from google.genai import types

    chunk = silence(chunk_ms, rate)

    async def _loop():
        while True:
            await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={rate}"))
            await asyncio.sleep(chunk_ms / 1000)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()


async def callee_turn(session, pcm16: bytes, rate: int, *, on_tool_call=None, timeout_s: float = 60.0,
                      nudge_after_s: float | None = None, nudge_text: str | None = None, on_heard=None,
                      silent_after_s: float | None = None, off_script=None) -> TurnResult:
    """相手の発話1回ぶん：声を送り、話し終わりのあとは無音を送りながら、AIの返事（1ターン）を受け取る。
    待ち時間（first_audio_after_s）は、相手が話し終えた時刻から数える。"""
    await stream_audio(session, pcm16, rate)
    ended = time.monotonic()
    async with silence_while(session, rate):
        return await collect_turn(session, started_at=ended, on_tool_call=on_tool_call, timeout_s=timeout_s,
                                  nudge_after_s=nudge_after_s, nudge_text=nudge_text, on_heard=on_heard, silent_after_s=silent_after_s,
                                  off_script=off_script)


def safe(text: object) -> str:
    return redact(text)
