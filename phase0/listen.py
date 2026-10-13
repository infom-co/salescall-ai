"""別の耳：録ったAIの声を、会話AIとは別の音声認識で聞き直して文字にする（DESIGN.md 6-10 の (5)・6-12。フェーズ0では G-15・G-17 で使う）。

耳は2つを比べる：
- "transcribe35"：gemini-3.5-transcribe-live（文字起こし専用）。設定は「入力の文字起こし」だけ。
- "live38"：gemini-3.8-live に聞かせ、入力の文字起こしを使う（会話に使うのと同じモデル＝独立性は低い。比べるためだけ）。
⚠️ どちらも Google の仕組み。言い間違いを「正しい言葉」に直して書き出すことがありうるので、知っている言い間違いで先に確かめる（G-17）。
"""
from __future__ import annotations

import asyncio
import difflib

from audio import resample
from jtext import compare_reading
from live_util import silence_while, stream_audio

EARS = ("transcribe35", "live38")
# 声の前に置く無音（秒）。2026-10-13 に「無音を置かないと頭を聞き落とす」と疑って 1.0 にしたが、測ると（ear_head_check.py）
#   聞き違いの疑いは 無音なし 70ターン中3・無音あり 70ターン中6 ほどで、無音ありのほうが多かった（操作のあとのターンで、無音ありでだけ
#   崩れて聞こえた回が4本）⇒ 0（もとの形）に戻した。⚠️ 別の耳は、1回聞いただけだと、1ターンの頭を約5%で聞き違える。
#   頭の崩れを数えるときは、2通りの聞かせ方で同じに崩れたものだけを数える（EAR-HEAD-convo-mismatch_20261013_150506.json）
LEAD_S = 0.0
# 2通りの聞かせ方（声の前の無音の秒数）。1ターンの崩れを数えるときは、両方で同じに崩れたものだけを数える（G-24）
FEEDS = (0.0, 1.0)
_MODEL = {"transcribe35": "gemini-3.5-transcribe-live", "live38": "gemini-3.8-live"}


def _config(ear: str):
    from google.genai import types

    if ear == "transcribe35":
        return types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig())
    from live_util import build_config

    return build_config(voice=None, system_instruction="相手の話を聞いてください。返事は「はい」だけにしてください。")


def _cut_off(exc: BaseException) -> bool:
    """接続が向こうの都合で切れた（1006 abnormal closure）か。"""
    return getattr(exc, "code", None) == 1006


async def hear(client, pcm16: bytes, rate: int, ear: str, idle_s: float = 3.0, lead_s: float = LEAD_S,
               retry_wait_s: float = 10.0) -> str:
    """声を 16kHz にして、前に lead_s 秒の無音を置いてから 0.1秒ずつ送り、確定した文字起こしをつなげて返す。
    接続が途中で切れたら、間を空けて1回だけ聞き直す。それでも切れたら誤りを上げる（聞けた途中までを、聞けた結果として返さない）。
    ⛔ 2026-10-13 G-22：別の耳の接続が途中で切れ（1006）、受け取りの処理だけが黙って止まり、途中までの文字を全部聞いた結果として返していた。"""
    try:
        return await _hear_once(client, pcm16, rate, ear, idle_s, lead_s)
    except Exception as exc:
        if not _cut_off(exc):
            raise
    await asyncio.sleep(retry_wait_s)
    return await _hear_once(client, pcm16, rate, ear, idle_s, lead_s)


async def _hear_once(client, pcm16: bytes, rate: int, ear: str, idle_s: float, lead_s: float) -> str:
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
        if lead_s > 0:
            async with silence_while(s, 16000):  # 声の前の無音（話し始めの音を聞き落とさないため）
                await asyncio.sleep(lead_s)
        await stream_audio(s, pcm, 16000)
        async with silence_while(s, 16000):  # 話し終わりに気づかせる無音 1.5秒
            await asyncio.sleep(1.5)
        await s.send_realtime_input(audio_stream_end=True)
        last = -1
        while len(finals) != last and not rx.done():  # 新しい文字起こしが idle_s 秒届かなくなるまで待つ
            last = len(finals)
            await asyncio.sleep(idle_s)
        if rx.done() and not rx.cancelled() and rx.exception() is not None:
            raise rx.exception()  # 受け取りが途中で止まった（接続が切れたなど）
        rx.cancel()
    return "".join(finals)


def differences(expected: str, heard: str) -> list[dict]:
    """台本と、聞こえた言葉の食い違いを、読み（ひらがな）で比べて返す（句読点・空白・書き方の違いは無視）。"""
    a, b = compare_reading(expected), compare_reading(heard)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            out.append({"kind": op, "script": a[max(0, i1 - 4):i1] + "[" + a[i1:i2] + "]" + a[i2:i2 + 4], "heard": b[j1:j2],
                        "at": i1})  # 台本の読み（ひらがな）の何文字目か
    return out


async def hear_feeds(client, pcm16: bytes, rate: int, ear: str = "transcribe35") -> dict[str, str | None]:
    """同じ声を、2通りの聞かせ方（FEEDS）で聞く。聞けなかったほうは None。"""
    out: dict[str, str | None] = {}
    for lead in FEEDS:
        try:
            out[str(lead)] = await hear(client, pcm16, rate, ear, lead_s=lead)
        except Exception:
            out[str(lead)] = None
    return out


def in_all(heard: dict[str, str | None], test) -> bool:
    """2通りの聞かせ方の、どちらでも test が成り立つか（聞けなかったほうがあれば、成り立たないとする）。"""
    return all(v is not None and test(v) for v in heard.values())


def in_any(heard: dict[str, str | None], test) -> bool:
    """2通りの聞かせ方の、どちらか一方でも test が成り立つか。⇒ 「言えなかった」と数えるのは、両方で聞こえなかったときだけ。"""
    return any(v is not None and test(v) for v in heard.values())
