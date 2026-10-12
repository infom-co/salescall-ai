"""別の耳：録ったAIの声を、会話AIとは別の音声認識で聞き直して文字にする（DESIGN.md 6-10 の (5)・6-12。フェーズ0では G-15・G-17 で使う）。

耳は2つを比べる：
- "transcribe35"：gemini-3.5-transcribe-live（文字起こし専用）。設定は「入力の文字起こし」だけ。
- "live38"：gemini-3.8-live に聞かせ、入力の文字起こしを使う（会話に使うのと同じモデル＝独立性は低い。比べるためだけ）。
⚠️ どちらも Google の仕組み。言い間違いを「正しい言葉」に直して書き出すことがありうるので、知っている言い間違いで先に確かめる（G-17）。
"""
from __future__ import annotations

import asyncio
import difflib
import re

from audio import resample
from common import silence

EARS = ("transcribe35", "live38")
_MODEL = {"transcribe35": "gemini-3.5-transcribe-live", "live38": "gemini-3.8-live"}
_PUNCT = re.compile(r"[\s、。，．,.!?！？「」…・\-‐−－]")
_KAKASI = None


def norm(text: str) -> str:
    return _PUNCT.sub("", text)


def reading(text: str) -> str:
    """読み（ひらがな）にそろえる。「ない」と「無い」のような書き方の違いを、食い違いとして数えないため。"""
    global _KAKASI
    if _KAKASI is None:
        import pykakasi

        _KAKASI = pykakasi.kakasi()
    return "".join(x["hira"] for x in _KAKASI.convert(norm(text)))


def _config(ear: str):
    from google.genai import types

    if ear == "transcribe35":
        return types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig())
    from live_util import build_config

    return build_config(voice=None, system_instruction="相手の話を聞いてください。返事は「はい」だけにしてください。")


async def hear(client, pcm16: bytes, rate: int, ear: str, idle_s: float = 3.0) -> str:
    """声を 16kHz にして 0.1秒ずつ送り、確定した文字起こしをつなげて返す。"""
    from google.genai import types

    pcm = resample(pcm16, rate, 16000)
    finals: list[str] = []
    async with client.aio.live.connect(model=_MODEL[ear], config=_config(ear)) as s:
        async def receiver():
            while True:
                async for m in s.receive():
                    sc = m.server_content
                    if sc and sc.input_transcription and sc.input_transcription.text and getattr(sc.input_transcription, "finished", True) is not False:
                        finals.append(sc.input_transcription.text)

        rx = asyncio.create_task(receiver())
        step = 16000 * 2 // 10
        for i in range(0, len(pcm), step):
            await s.send_realtime_input(audio=types.Blob(data=pcm[i:i + step], mime_type="audio/pcm;rate=16000"))
            await asyncio.sleep(0.1)
        for _ in range(15):  # 話し終わりに気づかせる無音 1.5秒
            await s.send_realtime_input(audio=types.Blob(data=silence(100), mime_type="audio/pcm;rate=16000"))
            await asyncio.sleep(0.1)
        await s.send_realtime_input(audio_stream_end=True)
        last = -1
        while len(finals) != last:  # 新しい文字起こしが idle_s 秒届かなくなるまで待つ
            last = len(finals)
            await asyncio.sleep(idle_s)
        rx.cancel()
    return "".join(finals)


def differences(expected: str, heard: str) -> list[dict]:
    """台本と、聞こえた言葉の食い違いを、読み（ひらがな）で比べて返す（句読点・空白・書き方の違いは無視）。"""
    a, b = reading(expected), reading(heard)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            out.append({"kind": op, "script": a[max(0, i1 - 4):i1] + "[" + a[i1:i2] + "]" + a[i2:i2 + 4], "heard": b[j1:j2]})
    return out
