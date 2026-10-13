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
):
    """Live のセッション設定を作る唯一の場所。

    thinking_budget：None＝既定（自動）／0＝考えない／正の数＝考える量の上限。
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
    )
    return cfg


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
                       nudge_after_s: float | None = None, nudge_text: str | None = None) -> TurnResult:
    """1ターン分を受け取る。関数の呼び出しが来たら `on_tool_call(function_call) -> dict` で返事を作って返す。

    nudge_after_s：この秒数のあいだ声が出なければ、nudge_text を1回だけ送って返事を促す（AIが黙ったとき。DESIGN.md 6-7）。
    見張りはここだけに置く（相手の声で始まるターンも、文字で始まるターンも、同じ見張りを通る）。
    """
    r = TurnResult()
    task = asyncio.create_task(_collect_into(r, session, started_at=started_at, on_tool_call=on_tool_call, timeout_s=timeout_s))
    if nudge_after_s is not None and nudge_text:
        done, _ = await asyncio.wait({task}, timeout=nudge_after_s)
        if not done and r.first_audio_after_s is None:
            r.nudged = True
            r.events.append({"at_s": round(time.monotonic() - started_at, 3), "kind": "nudged"})
            await send_text(session, nudge_text)
    await task
    return r


async def _collect_into(r: "TurnResult", session, *, started_at: float, on_tool_call, timeout_s: float) -> None:
    from google.genai import types

    audio_after_nudge = False

    def at() -> float:
        return round(time.monotonic() - started_at, 3)

    async def _loop():
        nonlocal audio_after_nudge
        # 操作（関数）への返事を送ったら、AIがそれを受けて話し終えるまで受け取り続ける。
        # ⛔ 2026-10-12 の不具合：操作の入ったターンの区切りで受け取りをやめていたため、
        #    返事を受けたAIの声（聞き返し・言い直し）が、次の相手の発話のあとにずれていた。
        awaiting_reply_speech = False
        while True:
            async for msg in session.receive():
                _count(r, msg)
                if msg.usage_metadata is not None:
                    r.usage.append(msg.usage_metadata.model_dump(exclude_none=True))
                if msg.go_away is not None:
                    r.go_away_time_left.append(str(msg.go_away.time_left))
                    r.events.append({"at_s": at(), "kind": "go_away", "time_left": str(msg.go_away.time_left)})
                if msg.session_resumption_update is not None and msg.session_resumption_update.new_handle:
                    r.resumption_handles.append(msg.session_resumption_update.new_handle)
                if msg.tool_call is not None:
                    responses = []
                    for fc in msg.tool_call.function_calls or []:
                        r.tool_calls.append({"name": fc.name, "args": fc.args, "at_s": at()})
                        reply = on_tool_call(fc) if on_tool_call else {"result": "ok"}
                        r.events.append({"at_s": at(), "kind": "tool_call", "name": fc.name, "args": fc.args,
                                         "reply": reply.get("result")})
                        responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=reply))
                    if responses:
                        await session.send_tool_response(function_responses=responses)
                        awaiting_reply_speech = True
                sc = msg.server_content
                if sc is None:
                    continue
                if sc.interrupted:
                    r.interrupted = True
                    r.events.append({"at_s": at(), "kind": "interrupted"})
                if sc.input_transcription is not None and sc.input_transcription.text:
                    r.input_transcript.append(sc.input_transcription.text)
                if sc.output_transcription is not None and sc.output_transcription.text:
                    r.output_transcript.append(sc.output_transcription.text)
                    r.events.append({"at_s": at(), "kind": "ai_said", "text": sc.output_transcription.text})
                if sc.model_turn is not None:
                    for part in sc.model_turn.parts or []:
                        blob = part.inline_data
                        if blob is not None and blob.data:
                            if r.first_audio_after_s is None:
                                r.first_audio_after_s = at()
                            r.audio_mime = r.audio_mime or blob.mime_type
                            r.audio_rate = r.audio_rate or parse_rate(blob.mime_type, 24000)
                            r.audio.extend(blob.data)
                            awaiting_reply_speech = False
                            if r.nudged:
                                audio_after_nudge = True
            r.events.append({"at_s": at(), "kind": "turn_complete"})
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
                      nudge_after_s: float | None = None, nudge_text: str | None = None) -> TurnResult:
    """相手の発話1回ぶん：声を送り、話し終わりのあとは無音を送りながら、AIの返事（1ターン）を受け取る。
    待ち時間（first_audio_after_s）は、相手が話し終えた時刻から数える。"""
    await stream_audio(session, pcm16, rate)
    ended = time.monotonic()
    async with silence_while(session, rate):
        return await collect_turn(session, started_at=ended, on_tool_call=on_tool_call, timeout_s=timeout_s,
                                  nudge_after_s=nudge_after_s, nudge_text=nudge_text)


def safe(text: object) -> str:
    return redact(text)
