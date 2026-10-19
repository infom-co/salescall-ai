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
    vad_signal: bool = False,
):
    """Live のセッション設定を作る唯一の場所。

    thinking_budget：None＝既定（自動）／0＝考えない／正の数＝考える量の上限。
    speech_end：相手の話し終わりの判定（None＝既定）。{"sensitivity": "HIGH" か "LOW", "silence_ms": 無音を待つ長さ}。
      会話の間を縮めるための試験（G-30。DESIGN.md 6-15）。⚠️ 速くすると、相手が言葉の途中で間をあけたときにAIが割り込む。
    ⛔「考えるレベル」（thinking_level）は gemini-3.8-live では受け付けられない（2026-10-12 G-11：1007 で拒否）。
    vad_signal：会話AIの話し始め・話し終わりの知らせ（voice_activity_detection_signal）を送らせる（G-60：相手の言葉の文字が出揃った合図に
      使えるかを測る）。
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
        **({"explicit_vad_signal": True} if vad_signal else {}),
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
    off_script: bool = False      # AIの声が、渡した言い方から外れたので止めた（offscript.SpeechGate。DESIGN.md 6-7）
    heard: dict | None = None     # 止めたときに相手に聞こえていたもの（SpeechGate.heard）
    first_audio_after_s: float | None = None
    finished_after_s: float | None = None
    # 報告の返事を待たせている間に届いたAIの声（相手に流さない＝電話接続側。その声は、プログラムの返事を知らずに作られている）
    held_audio_s: float = 0.0
    held_text: list[str] = field(default_factory=list)
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
            "held_audio_s": round(self.held_audio_s, 2),
            "held_text": "".join(self.held_text),
        }


async def collect_turn(session, *, started_at: float, on_tool_call=None, timeout_s: float = 60.0,
                       nudge_after_s: float | None = None, nudge_text: str | None = None, on_heard=None,
                       silent_after_s: float | None = None, gate=None) -> TurnResult:
    """1ターン分を受け取る。関数の呼び出しが来たら `on_tool_call(function_call) -> dict` で返事を作って返す。

    nudge_after_s：この秒数のあいだ声が出なければ、nudge_text を1回だけ送って返事を促す（AIが黙ったとき。DESIGN.md 6-7）。
    silent_after_s：この秒数のあいだ声も操作も出なければ、受け取りをやめて silent にして返す（AIが黙った＝6-7 の張り替えは呼ぶ側が決める）。
    見張りはここだけに置く（相手の声で始まるターンも、文字で始まるターンも、同じ見張りを通る）。
    ⛔ 2026-10-14 G-33：答えの報告を「返事を待たない」宣言にする形（受けの言葉が先）は、AIが受けの言葉を先に言わなかったので採らず、
       受け取りの処理も外した（記録は DESIGN.md 6-15・PHASE0_VERIFICATION.md の G-33）。
    on_heard：相手の言葉の文字（会話AIの聞き取り）が届くたびに `on_heard(text)` を呼ぶ（値を直されたときの直され方を、プログラムが
      相手の言葉から決める。flowctl.Flow.on_heard）。
    gate：渡した言い方の見張り（offscript.SpeechGate）。外れたと決まったら、受け取りをやめて off_script にして返す（本番では、電話接続側が
      決まるまで最初の文の切れ目で待ち、外れたら流さない）。止めたときに相手に聞こえていたものは heard（外れたあとは呼ぶ側が決める＝6-7）。
    on_tool_call が (返事, True) を返したら、その返事は「話させない」形（SILENT）で返し、AIの声を待たない（電話接続側が録音を流すとき。
      G-37。⛔ 2026-10-14 に外した形 B の受け取りと同じ仕組み）。
    """
    r = TurnResult()
    if on_heard is not None:  # 受け取りを始める前に（Inbox は、届いた瞬間に渡す）
        inbox_of(session).on_heard = on_heard
    task = asyncio.create_task(_collect_into(r, session, started_at=started_at, on_tool_call=on_tool_call, timeout_s=timeout_s,
                                             on_heard=on_heard, gate=gate))
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


class Inbox:
    """会話AIからの知らせを、届いた瞬間に受け取る唯一の場所（1つのつなぎに1つ＝inbox_of）。電話接続側と同じく、相手の声を送っている間も、
    報告の返事を待たせている間も受け取り続け、届いた時刻を付けて並べる（受け取りは _collect_into が、この順に読む）。
    相手の言葉の文字は、届いた瞬間に on_heard へ、相手の話し始め・話し終わりの知らせ（voice_activity）は on_activity(話しているか, 位置) へ渡す
    （読む側が読むのを待たない＝電話接続側と同じ）。
    ⛔ 2026-10-19 G-57 の本番の形：受け取りを、相手の声を送り終えてから始め、報告の返事を作る間（確かめを待つ間）は止めていた ⇒
       受付の方の「岡田ですね。」「少々お待ちください。」の間でAIが報告したとき、「少々お待ちください。」の文字を、返事を決めるまで読めず、
       「岡田ですね。」だけで判定した（電話接続側には無い遅れ＝試験の作りの誤り）。届いた時刻も、送り終えた時刻に寄っていた。"""

    END = object()  # 会話AIの1ターンの知らせが終わった（session.receive が終わった）

    def __init__(self, session):
        self.q: asyncio.Queue = asyncio.Queue()
        self.on_heard = self.on_activity = None
        self.task = asyncio.create_task(self._run(session))

    async def _run(self, session) -> None:
        try:
            while True:
                async for msg in session.receive():
                    sc = getattr(msg, "server_content", None)
                    it = getattr(sc, "input_transcription", None) if sc is not None else None
                    if it is not None and it.text and self.on_heard:
                        self.on_heard(it.text)
                    act = activity(msg)
                    if act is not None and self.on_activity:
                        self.on_activity(*act)
                    self.q.put_nowait((time.monotonic(), msg))
                self.q.put_nowait((time.monotonic(), Inbox.END))
        except Exception as exc:  # つなぎが切れた：読む側へ渡す（読む側で、これまでと同じ例外になる）
            self.q.put_nowait((time.monotonic(), exc))

    def close(self) -> None:
        """このつなぎの受け取りをやめる（張り替えたとき。open_session）：知らせを渡す先を外し、読むのをやめる。"""
        self.on_heard = self.on_activity = None
        self.task.cancel()


def activity(msg) -> tuple[bool, float | None] | None:
    """会話AIの、相手の話し始め（True）・話し終わり（False）の知らせ（voice_activity）と、その位置（送った音の上の秒。audio_offset＝
    "3.520s"。読めなければ None）。無ければ None。⛔ 話し始め・話し終わりの合図（explicit_vad_signal）は、Gemini Developer API では
    受け付けられない（G-60：ValueError）。voice_activity は、設定なしで届く。"""
    va = getattr(msg, "voice_activity", None)
    kind = str(getattr(va, "voice_activity_type", "") or "")
    speaking = True if kind.endswith("ACTIVITY_START") else False if kind.endswith("ACTIVITY_END") else None
    if speaking is None:
        return None
    try:
        at = float(str(getattr(va, "audio_offset", "") or "").removesuffix("s"))
    except ValueError:
        at = None
    return speaking, at


def inbox_of(session) -> Inbox:
    box = getattr(session, "_salescall_inbox", None)
    if box is None:
        box = Inbox(session)
        session._salescall_inbox = box
    return box


async def _collect_into(r: "TurnResult", session, *, started_at: float, on_tool_call, timeout_s: float, on_heard=None,
                        gate=None) -> None:
    from google.genai import types

    audio_after_nudge = False
    inbox = inbox_of(session)

    def at(when: float | None = None) -> float:
        return round((time.monotonic() if when is None else when) - started_at, 3)

    async def _loop():
        nonlocal audio_after_nudge
        # 操作（関数）への返事を送ったら、AIがそれを受けて話し終えるまで受け取り続ける。
        # ⛔ 2026-10-12 の不具合：操作の入ったターンの区切りで受け取りをやめていたため、
        #    返事を受けたAIの声（聞き返し・言い直し）が、次の相手の発話のあとにずれていた。
        awaiting_reply_speech = False
        chunk = 0                  # いま受け取っている声のまとまりの番号（まとまりの終わりごとに増える）
        audio_in_chunk = False
        answering: list[asyncio.Task] = []  # 返事を作っている報告（1つの知らせごと。届いた順に作る）。待つ間も、ほかの知らせは読み続ける
        cancelled: set = set()              # 会話AIが取り消した報告（tool_call_cancellation。相手が話し続けたときなど）
        turn_ended = False

        async def answer(calls: list, before: asyncio.Task | None) -> list:
            """1つの知らせの中の報告に、順に返事を作る（前の知らせの報告の返事が先＝流れの制御が受ける順は、届いた順）。"""
            if before is not None:
                await asyncio.wait({before})
            responses = []  # (返事, 話させない形か)
            for fc, arrived in calls:
                out = on_tool_call(fc) if on_tool_call else {"result": "ok"}
                if inspect.isawaitable(out):  # 返事を作る処理が待つとき（確かめを待つ。convo.check_handler。G-15）
                    out = await out
                # on_tool_call は (返事, 話させない形か) も返せる（電話接続側が録音を流すときは、AIに話させない。G-37）
                reply, silent = out if isinstance(out, tuple) else (out, False)
                r.events.append({"at_s": arrived, "kind": "tool_call", "name": fc.name, "args": fc.args, "reply": reply,
                                 "answered_s": at()})
                extra = {"scheduling": types.FunctionResponseScheduling.SILENT} if silent else {}
                responses.append((types.FunctionResponse(id=fc.id, name=fc.name, response=reply, **extra), silent))
            return responses

        async def answered(job: asyncio.Task) -> None:
            nonlocal awaiting_reply_speech
            answering.remove(job)
            responses = [(x, s) for x, s in job.result() if x.id not in cancelled]
            if r.off_script or not responses:
                return
            if gate is not None:
                gate.reset(len(r.audio))  # 返事を送った：このあとの声と文字を、新しい言い方で見張る
            await session.send_tool_response(function_responses=[x for x, _ in responses])
            r.events.append({"at_s": at(), "kind": "tool_response_sent", "chunk": chunk})
            if not all(silent for _, silent in responses):
                awaiting_reply_speech = True  # 話させない返事だけなら、AIの声を待たない
                r.expects_speech = True

        def handle(msg, arrived: float) -> None:
            nonlocal awaiting_reply_speech, audio_after_nudge, audio_in_chunk
            _count(r, msg)
            if msg.usage_metadata is not None:
                r.usage.append(msg.usage_metadata.model_dump(exclude_none=True))
            if msg.go_away is not None:
                r.go_away_time_left.append(str(msg.go_away.time_left))
                r.events.append({"at_s": arrived, "kind": "go_away", "time_left": str(msg.go_away.time_left)})
            if msg.session_resumption_update is not None and msg.session_resumption_update.new_handle:
                r.resumption_handles.append(msg.session_resumption_update.new_handle)
            act = activity(msg)
            if act is not None:
                r.events.append({"at_s": arrived, "kind": "activity", "speaking": act[0], "offset_s": act[1]})
            gone = getattr(msg, "tool_call_cancellation", None)
            if gone is not None and gone.ids:
                cancelled.update(gone.ids)
                r.events.append({"at_s": arrived, "kind": "tool_call_cancelled", "ids": list(gone.ids)})
                for job in [j for j in answering if j.ids <= cancelled]:
                    job.cancel()  # 取り消された報告：まだ返事を作っている（流れの制御へ渡す前）なら、渡さない
                    answering.remove(job)
            if msg.tool_call is not None and msg.tool_call.function_calls:
                calls = [(fc, arrived) for fc in msg.tool_call.function_calls]
                for fc, _ in calls:
                    r.tool_calls.append({"name": fc.name, "args": fc.args, "at_s": arrived})
                job = asyncio.create_task(answer(calls, answering[-1] if answering else None))
                job.ids = {fc.id for fc, _ in calls}
                answering.append(job)
            sc = msg.server_content
            if sc is not None:
                if sc.interrupted:
                    r.interrupted = True
                    r.events.append({"at_s": arrived, "kind": "interrupted"})
                if sc.input_transcription is not None and sc.input_transcription.text:
                    r.input_transcript.append(sc.input_transcription.text)
                    # 相手の言葉の文字が、AIの報告より先に届くか（報告の時点で、プログラムが相手の言葉を見て決められるか。2026-10-14 G-36：
                    # AIは、こちらの聞き違いと相手の言い間違いを見分けられなかった＝12本中12本「相手が言い直した」）。流れの制御へは、届いた瞬間に
                    # Inbox が渡している
                    r.events.append({"at_s": arrived, "kind": "heard", "text": sc.input_transcription.text,
                                     "finished": sc.input_transcription.finished})
                if sc.output_transcription is not None and sc.output_transcription.text and answering:
                    r.held_text.append(sc.output_transcription.text)  # 報告を待たせている間のAIの文字（流さない声の分）
                elif sc.output_transcription is not None and sc.output_transcription.text:
                    r.output_transcript.append(sc.output_transcription.text)
                    # audio_s：この文字が届いた時点で、受け取り済みの声の長さ（秒）。声は話す速さで届くので、相手に流れた長さに近い。
                    # 文字が声より先に届くかを測り、英語などをプログラムが止められるかを判断する（2026-10-13 G-16c）
                    r.events.append({"at_s": arrived, "kind": "ai_said", "text": sc.output_transcription.text,
                                     "audio_s": round(len(r.audio) / 2 / (r.audio_rate or 24000), 3)})
                    if gate is not None and not r.off_script and gate.on_text(sc.output_transcription.text, arrived) == "off":
                        r.off_script = True
                        r.heard = gate.heard(arrived, bytes(r.audio), r.audio_rate or 24000)
                        r.events.append({"at_s": arrived, "kind": "off_script", "text": gate.text, "heard": r.heard,
                                         "audio_s": round(len(r.audio) / 2 / (r.audio_rate or 24000), 3)})
                if sc.model_turn is not None:
                    for part in sc.model_turn.parts or []:
                        blob = part.inline_data
                        if blob is not None and blob.data and answering:
                            # 報告の返事を待たせている間に、AIが話し始めた（相手がまた話し、会話AIが新しい返事を作った）：相手に流さない
                            # （⛔ 2026-10-19 G-57 の間を長くした場面：受付の方が取り次いでいる間に、AIが「申し訳ございません。
                            # かしこまりました。」と言った＝取り次ぎを待つと決める前の声）。間は、電話接続側の「はい、」の相づちで埋まっている
                            if not r.held_audio_s:
                                r.events.append({"at_s": arrived, "kind": "audio_held"})
                            r.held_audio_s += len(blob.data) / 2 / parse_rate(blob.mime_type, 24000)
                        elif blob is not None and blob.data:
                            if r.first_audio_after_s is None:
                                r.first_audio_after_s = arrived
                            if not audio_in_chunk:  # 声のまとまりの始まり（報告の返事の前と後の声を分けて、間を測る）
                                r.events.append({"at_s": arrived, "kind": "audio_start", "chunk": chunk})
                            audio_in_chunk = True
                            r.audio_mime = r.audio_mime or blob.mime_type
                            r.audio_rate = r.audio_rate or parse_rate(blob.mime_type, 24000)
                            r.audio.extend(blob.data)
                            if gate is not None:
                                gate.on_audio(arrived)
                            awaiting_reply_speech = False
                            if r.nudged:
                                audio_after_nudge = True

        try:
            while True:
                if turn_ended and not answering:
                    # 会話AIのターンが終わり、そのターンの報告にすべて返事をした
                    r.events.append({"at_s": ended_at, "kind": "turn_complete"})
                    turn_ended, audio_in_chunk = False, False
                    chunk += 1
                    # 返事を促したのに、まだ声が来ていなければ、促しへの返事（次のターン）まで受け取る
                    # （受け取らずに終えると、その返事が次の相手の発話のあとにずれて記録される）
                    if not awaiting_reply_speech and not (r.nudged and not audio_after_nudge):
                        break
                get = asyncio.ensure_future(inbox.q.get())
                done, _ = await asyncio.wait({get, *answering}, return_when=asyncio.FIRST_COMPLETED)
                for job in [j for j in answering if j in done]:
                    await answered(job)
                if get not in done:
                    get.cancel()
                    continue
                when, msg = get.result()
                if isinstance(msg, Exception):
                    raise msg
                if msg is Inbox.END:
                    turn_ended, ended_at = True, at(when)
                    continue
                handle(msg, at(when))
                if r.off_script:
                    if answering:  # 返事を作っている報告は、流れの制御へ受け渡し終えてから（返事は送らない＝このつなぎは張り替える）
                        await asyncio.wait(set(answering))
                    r.finished_after_s = at()
                    return  # 外れた：受け取りをやめる（このつなぎは、呼ぶ側が張り替える）
            r.finished_after_s = at()
        finally:
            for job in answering:
                job.cancel()

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


def audio_sent_s(session) -> float:
    """このつなぎへ送った音の長さ（秒）。会話AIの話し始め・話し終わりの知らせの位置（voice_activity の audio_offset）は、この長さで数える
    （G-60：位置は送った音の上で、毎回同じ値）。送るのは stream_audio と silence_while だけ（_sent）。"""
    return getattr(session, "_salescall_sent_s", 0.0)


def _sent(session, nbytes: int, rate: int) -> None:
    session._salescall_sent_s = audio_sent_s(session) + nbytes / 2 / rate


async def stream_audio(session, pcm16: bytes, rate: int, *, chunk_ms: int = 100, realtime: bool = True) -> None:
    """音声を、実際の通話と同じように少しずつ送る（音声を送る唯一の場所）。"""
    from google.genai import types

    mime = f"audio/pcm;rate={rate}"
    step = rate * 2 * chunk_ms // 1000
    for i in range(0, len(pcm16), step):
        await session.send_realtime_input(audio=types.Blob(data=pcm16[i : i + step], mime_type=mime))
        _sent(session, len(pcm16[i : i + step]), rate)
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
        # 張り替えた古いつなぎの知らせは、もう流れの制御へ渡さない（閉じ終わるまで届き続ける＝いまの相手の発話の言葉に混ざる）
        box = getattr(session, "_salescall_inbox", None)
        if box is not None:
            box.close()
        closing.append(asyncio.create_task(cm.__aexit__(None, None, None)))


@asynccontextmanager
async def silence_while(session, rate: int = INPUT_RATE, chunk_ms: int = 100):
    """この中にいる間、無音を送り続ける（話し終わりに気づかせる。電話がつながっている間と同じ状態にする）。"""
    from google.genai import types

    chunk = silence(chunk_ms, rate)

    async def _loop():
        while True:
            await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={rate}"))
            _sent(session, len(chunk), rate)
            await asyncio.sleep(chunk_ms / 1000)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()


async def callee_turn(session, pcm16: bytes, rate: int, *, on_tool_call=None, timeout_s: float = 60.0,
                      nudge_after_s: float | None = None, nudge_text: str | None = None, on_heard=None,
                      silent_after_s: float | None = None, gate=None) -> TurnResult:
    """相手の発話1回ぶん：声を送り、話し終わりのあとは無音を送りながら、AIの返事（1ターン）を受け取る。
    待ち時間（first_audio_after_s）は、相手が話し終えた時刻から数える。声を送っている間に届いた知らせ（相手の言葉の文字・AIの報告）も、
    届いた時刻で記録する（送り終える前なら、負の秒）。相手の言葉の文字は、送っている間も、届いた瞬間に on_heard へ渡す（Inbox）。"""
    if on_heard is not None:
        inbox_of(session).on_heard = on_heard
    await stream_audio(session, pcm16, rate)
    ended = time.monotonic()
    async with silence_while(session, rate):
        return await collect_turn(session, started_at=ended, on_tool_call=on_tool_call, timeout_s=timeout_s,
                                  nudge_after_s=nudge_after_s, nudge_text=nudge_text, on_heard=on_heard, silent_after_s=silent_after_s,
                                  gate=gate)


def safe(text: object) -> str:
    return redact(text)
