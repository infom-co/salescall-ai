"""フェーズ0で Gemini Live を試すときの共通部分（設定の組み立てと、1ターンの受け取り）。

- 設定を組み立てる場所は `build_config` の1つだけ（DESIGN.md 6-6 の start_session と同じ考え方）。
  どの確認スクリプトも、ここを通してセッションを開く。
- 受け取りは `collect_turn` の1つだけ。音声・文字起こし・利用量・関数の呼び出し・切れる前の知らせを、
  同じ形で記録する。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from common import parse_rate, redact


def build_config(
    *,
    voice: str | None,
    system_instruction: str,
    tools: list | None = None,
    transcription: bool = True,
    resumption_handle: str | None = None,
    enable_resumption: bool = False,
    language_code: str | None = "ja-JP",
):
    """Live のセッション設定を作る唯一の場所。"""
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
    first_audio_after_s: float | None = None
    finished_after_s: float | None = None
    message_kinds: dict[str, int] = field(default_factory=dict)

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
            "first_audio_after_s": self.first_audio_after_s,
            "finished_after_s": self.finished_after_s,
            "message_kinds": self.message_kinds,
        }


async def collect_turn(session, *, started_at: float, on_tool_call=None, timeout_s: float = 60.0) -> TurnResult:
    """1ターン分を受け取る。関数の呼び出しが来たら `on_tool_call(function_call) -> dict` で返事を作って返す。"""
    from google.genai import types

    r = TurnResult()

    async def _loop():
        async for msg in session.receive():
            _count(r, msg)
            if msg.usage_metadata is not None:
                r.usage.append(msg.usage_metadata.model_dump(exclude_none=True))
            if msg.go_away is not None:
                r.go_away_time_left.append(str(msg.go_away.time_left))
            if msg.session_resumption_update is not None and msg.session_resumption_update.new_handle:
                r.resumption_handles.append(msg.session_resumption_update.new_handle)
            if msg.tool_call is not None:
                responses = []
                for fc in msg.tool_call.function_calls or []:
                    r.tool_calls.append({"name": fc.name, "args": fc.args, "at_s": round(time.monotonic() - started_at, 3)})
                    reply = on_tool_call(fc) if on_tool_call else {"result": "ok"}
                    responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=reply))
                if responses:
                    await session.send_tool_response(function_responses=responses)
            sc = msg.server_content
            if sc is None:
                continue
            if sc.interrupted:
                r.interrupted = True
            if sc.input_transcription is not None and sc.input_transcription.text:
                r.input_transcript.append(sc.input_transcription.text)
            if sc.output_transcription is not None and sc.output_transcription.text:
                r.output_transcript.append(sc.output_transcription.text)
            if sc.model_turn is not None:
                for part in sc.model_turn.parts or []:
                    blob = part.inline_data
                    if blob is not None and blob.data:
                        if r.first_audio_after_s is None:
                            r.first_audio_after_s = round(time.monotonic() - started_at, 3)
                        r.audio_mime = r.audio_mime or blob.mime_type
                        r.audio_rate = r.audio_rate or parse_rate(blob.mime_type, 24000)
                        r.audio.extend(blob.data)
        r.finished_after_s = round(time.monotonic() - started_at, 3)

    await asyncio.wait_for(_loop(), timeout=timeout_s)
    return r


def _count(r: TurnResult, msg) -> None:
    for name in ("setup_complete", "server_content", "tool_call", "tool_call_cancellation", "usage_metadata",
                 "go_away", "session_resumption_update", "voice_activity", "voice_activity_detection_signal"):
        if getattr(msg, name, None) is not None:
            r.message_kinds[name] = r.message_kinds.get(name, 0) + 1


async def send_text(session, text: str) -> None:
    """相手の発話の代わりに、文字で1ターン送る（音声を用意しなくても流れを確かめられる）。"""
    from google.genai import types

    await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)


async def send_audio(session, pcm16: bytes, rate: int, chunk_ms: int = 100, realtime: bool = True) -> None:
    """音声を、実際の通話と同じように少しずつ送る。"""
    from google.genai import types

    mime = f"audio/pcm;rate={rate}"
    step = rate * 2 * chunk_ms // 1000
    for i in range(0, len(pcm16), step):
        await session.send_realtime_input(audio=types.Blob(data=pcm16[i : i + step], mime_type=mime))
        if realtime:
            await asyncio.sleep(chunk_ms / 1000)
    await session.send_realtime_input(audio_stream_end=True)


def safe(text: object) -> str:
    return redact(text)
