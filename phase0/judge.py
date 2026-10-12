"""発音の審査：台本と、それを読み上げた録音を AI に渡し、台本どおりに発音されていない所を挙げさせる（G-17）。

別の耳（listen.py）は、録音を文字にしてから台本と比べるので、噛んだ言葉を正しい言葉に直して書き出してしまう
（「おも忙しい」を「お忙しい」と書いた）。ここでは、台本を先に渡したうえで、音そのものを聞いて比べさせる。
⚠️ 審査する AI が、無い問題をあると言う（作り話）おそれがある。ユーザーが聞いて確かめた録音で、
   見つけられるか・問題の無い録音で「無い」と言えるかを、先に確かめてから使う。
"""
from __future__ import annotations

import asyncio
import time

from audio import resample
from common import silence

MODEL = "gemini-3.8-live"
INSTRUCTION = (
    "あなたは、電話の読み上げの発音を審査する係です。"
    "最初に台本を渡し、そのあと、台本を読み上げた録音を流します。"
    "録音を一音ずつ注意深く聞き、台本どおりに発音されていない所をすべて見つけてください。"
    "見つけるもの：言い間違い、噛んだ所、音が抜けた所、前後の音がつながって別の言葉に聞こえる所、途中で切れて言い終わっていない所、同じ所を繰り返した所。"
    "句読点、間の取り方、声の調子は問題にしません。"
    "録音を聞き終えたら、必ず report を1回だけ呼んでください。問題が無ければ、空の一覧で呼んでください。"
    "聞こえなかったことを、あったことにしないでください。"
)


def _tools():
    from google.genai import types

    item = types.Schema(type=types.Type.OBJECT, properties={
        "script_part": types.Schema(type=types.Type.STRING, description="台本の、その部分の言葉"),
        "heard_as": types.Schema(type=types.Type.STRING, description="実際にどう聞こえたか（ひらがな）"),
        "kind": types.Schema(type=types.Type.STRING, description="言い間違い／噛み／抜け／つながり／途中で切れた／繰り返し"),
    }, required=["script_part", "heard_as", "kind"])
    fn = types.FunctionDeclaration(
        name="report", description="審査の結果を報告する",
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "problems": types.Schema(type=types.Type.ARRAY, items=item, description="見つかった問題。無ければ空"),
        }, required=["problems"]))
    return [types.Tool(function_declarations=[fn])]


async def judge(client, pcm16: bytes, rate: int, script: str, timeout_s: float = 60.0) -> dict:
    """録音を審査して {"problems": [...]} を返す。report が呼ばれなければ {"problems": None, "no_report": True}。"""
    from google.genai import types

    from live_util import build_config

    cfg = build_config(voice=None, system_instruction=INSTRUCTION, tools=_tools(), thinking_budget=None)
    pcm = resample(pcm16, rate, 16000)
    result: dict = {"problems": None, "no_report": True}
    async with client.aio.live.connect(model=MODEL, config=cfg) as s:
        await s.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=f"台本：{script}\nこのあと録音を流します。")]),
                                    turn_complete=False)
        got = asyncio.Event()

        async def receiver():
            while True:
                async for m in s.receive():
                    if m.tool_call:
                        for fc in m.tool_call.function_calls:
                            if fc.name == "report":
                                result.update({"problems": list((fc.args or {}).get("problems", [])), "no_report": False})
                                got.set()
                        await s.send_tool_response(function_responses=[
                            types.FunctionResponse(id=fc.id, name=fc.name, response={"ok": True}) for fc in m.tool_call.function_calls])

        rx = asyncio.create_task(receiver())
        step = 16000 * 2 // 10
        for i in range(0, len(pcm), step):
            await s.send_realtime_input(audio=types.Blob(data=pcm[i:i + step], mime_type="audio/pcm;rate=16000"))
            await asyncio.sleep(0.1)
        t0 = time.monotonic()
        while not got.is_set() and time.monotonic() - t0 < timeout_s:  # 話し終わりに気づかせる無音を送りながら、報告を待つ
            await s.send_realtime_input(audio=types.Blob(data=silence(100), mime_type="audio/pcm;rate=16000"))
            await asyncio.sleep(0.1)
        rx.cancel()
    return result
