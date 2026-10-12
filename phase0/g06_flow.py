"""G-6・G-7・G-16（の入口）：AIが操作を呼び、プログラムの返事に従って進むか／文字起こしが取れるか。

DESIGN.md 6-1・6-2・6-11 の考え方（会話の位置はプログラムが持つ）が、Gemini Live で成り立つかを確かめる。
⚠️ これは確かめるための小さな試験台であり、製品の流れの制御そのものではない。

台本（相手の発話は文字で送る）：
  Q1「ご予約はされていますか」（はい／いいえ・確かめない型）⇒「はい、予約しています」
  Q2「ご希望の日時をお聞かせください」（日時・確かめる型）⇒ 聞き取りにくい答え ⇒ 聞き返し ⇒「15日の2時です」⇒ 復唱 ⇒「はい」
確かめること：
  (1) record_answer の質問IDが、いまの質問と一致する
  (2) Q2 は、復唱の結果（confirm_field）が届くまで、プログラムが次へ進めない（I-18）
  (3) 聞き取りにくい答えで report_unclear を呼び、決まった言い方で1回だけ聞き返す（6-11）
  (4) 復唱の言葉が、AIの発話の文字起こしに出る（G-7）
"""
from __future__ import annotations

import asyncio
import time

from common import base_parser, describe_error, make_client, pick_key, require_python, save_result, say
from live_util import build_config, collect_turn, send_text

QUESTIONS = {
    "Q1": {"text": "ご予約はされていますか。", "type": "yes_no", "conditions": ["yes", "no", "unknown"]},
    "Q2": {"text": "ご希望の日時をお聞かせください。", "type": "datetime", "conditions": ["answered", "unknown"]},
}
CONFIRM_TYPES = {"datetime"}
ASK_BACK = "聞き取りにくかったので、もう一度、おっしゃっていただいてよろしいでしょうか。"
CALLEE = ["はい、もしもし。", "はい、予約しています。", "えっと、じゅう……の……", "15日の2時です。", "はい、そうです。"]

INSTRUCTION = f"""あなたは電話で、予約の確認をする担当者です。日本語で、丁寧に短く話します。
決まり：
- 質問は、プログラムから渡されたものだけを、1つずつ聞きます。最初の質問は Q1「{QUESTIONS['Q1']['text']}」です。
- 相手が答えたら、必ず record_answer を呼びます。返事に次の質問が書いてあるので、それを聞きます。
- 日付・時刻・数・名前・電話番号・メール・住所は、聞いたその場で言い直して確かめます（例：「15日の午後2時ですね」）。
  確かめたら、相手の答えを confirm_field で報告します。
- 聞き取れない・曖昧なときは report_unclear を呼びます。返事が「聞き返す」なら「{ASK_BACK}」と聞き返します。
- 返事に「終わり」とあれば、お礼を言って終えます。
"""


def tools():
    from google.genai import types

    def schema(spec: dict):
        # Google の決まった形（types.Schema）で引数を定義する（変換後の形を確かめ済み）
        return types.Schema(type=types.Type.STRING, enum=spec.get("enum"))

    def fd(name, desc, props, required):
        return types.FunctionDeclaration(
            name=name, description=desc,
            parameters=types.Schema(type=types.Type.OBJECT, properties={k: schema(v) for k, v in props.items()},
                                    required=required))

    return [types.Tool(function_declarations=[
        fd("record_answer", "相手の答えを、いまの質問の選択肢に分類して報告する",
           {"question_id": {"type": "string", "enum": list(QUESTIONS)},
            "condition_id": {"type": "string", "enum": sorted({c for q in QUESTIONS.values() for c in q["conditions"]})},
            "value": {"type": "string"}}, ["question_id", "condition_id"]),
        fd("confirm_field", "復唱の結果を報告する",
           {"question_id": {"type": "string", "enum": list(QUESTIONS)},
            "result": {"type": "string", "enum": ["affirmed", "corrected", "no_answer"]},
            "correction": {"type": "string"}}, ["question_id", "result"]),
        fd("report_unclear", "聞き取れない・曖昧だったことを報告する",
           {"question_id": {"type": "string", "enum": list(QUESTIONS)}}, ["question_id"]),
    ])]


class MiniFlow:
    """試験用の小さな流れの制御（回数と位置はここが持つ）。"""

    def __init__(self):
        self.current = "Q1"
        self.awaiting_confirm: str | None = None
        self.ask_back_used: dict[str, int] = {}
        self.values: dict[str, dict] = {}
        self.checks = {"question_id_matched": True, "held_until_confirm": None, "ask_back_requested": False,
                       "rejected_calls": []}
        self.done = False

    def _next(self) -> str:
        order = list(QUESTIONS)
        i = order.index(self.current)
        if i + 1 < len(order):
            self.current = order[i + 1]
            return f"受け付けました。次の質問は {self.current}「{QUESTIONS[self.current]['text']}」です。"
        self.done = True
        return "受け付けました。終わりです。お礼を言って終えてください。"

    def handle(self, fc) -> dict:
        a = dict(fc.args or {})
        qid = a.get("question_id")
        if qid != self.current:
            self.checks["question_id_matched"] = False
            self.checks["rejected_calls"].append({"name": fc.name, "args": a, "why": "いまの質問ではない"})
            return {"result": "rejected", "message": f"いまの質問は {self.current} です。"}
        qtype = QUESTIONS[qid]["type"]
        if fc.name == "report_unclear":
            used = self.ask_back_used.get(qid, 0)
            self.ask_back_used[qid] = used + 1
            if used == 0:
                self.checks["ask_back_requested"] = True
                return {"result": "ask_back", "message": f"「{ASK_BACK}」と1回だけ聞き返してください。"}
            self.values[qid] = {"state": "unresolved"}
            return {"result": "give_up", "message": "聞き返しは使い切りました。「-」にして次へ進みます。 " + self._next()}
        if fc.name == "record_answer":
            self.values[qid] = {"state": "heard", "condition": a.get("condition_id"), "value": a.get("value")}
            if qtype in CONFIRM_TYPES or self.ask_back_used.get(qid):
                self.awaiting_confirm = qid
                return {"result": "confirm_required", "message": "その場で言い直して確かめてください。結果は confirm_field で報告してください。"}
            self.values[qid]["state"] = "accepted"
            return {"result": "accepted", "message": self._next()}
        if fc.name == "confirm_field":
            if self.awaiting_confirm != qid:
                self.checks["rejected_calls"].append({"name": fc.name, "args": a, "why": "復唱を求めていない"})
                return {"result": "rejected", "message": "いまは復唱を求めていません。"}
            self.checks["held_until_confirm"] = True
            self.awaiting_confirm = None
            if a.get("result") == "affirmed":
                self.values[qid]["state"] = "confirmed"
                return {"result": "confirmed", "message": self._next()}
            if a.get("result") == "corrected":
                self.values[qid].update(state="heard", value=a.get("correction"))
                self.awaiting_confirm = qid
                return {"result": "confirm_required", "message": "訂正された値を、もう1回だけ言い直して確かめてください。"}
            self.values[qid]["state"] = "unresolved"
            return {"result": "unresolved", "message": self._next()}
        return {"result": "rejected", "message": "知らない操作です。"}


async def run(args) -> dict:
    cfg = build_config(voice=args.voice, system_instruction=INSTRUCTION, tools=tools())
    if args.dry_run:
        return {"dry_run": True, "config_ok": True, "tools": [t.model_dump(exclude_none=True) for t in cfg.tools]}
    client = make_client(pick_key(args))
    flow = MiniFlow()
    turns = []
    async with client.aio.live.connect(model=args.model, config=cfg) as session:
        for line in CALLEE:
            t = time.monotonic()
            await send_text(session, line)
            turn = await collect_turn(session, started_at=t, on_tool_call=flow.handle, timeout_s=90)
            s = turn.summary()
            turns.append({"callee": line, "ai": s["output_transcript"], "tool_calls": s["tool_calls"],
                          "first_audio_after_s": s["first_audio_after_s"], "usage": s["usage"]})
            say(f"相手：{line} ／ AI：{s['output_transcript']}")
            if flow.done:
                break
    ai_text = "".join(t["ai"] for t in turns)
    checks = dict(flow.checks)
    checks["ask_back_phrase_count"] = ai_text.count("聞き取りにくかった")
    checks["ask_back_at_most_once"] = checks["ask_back_phrase_count"] <= 1
    checks["readback_spoken"] = ("15日" in ai_text) and ("2時" in ai_text or "14時" in ai_text)
    checks["transcript_available"] = bool(ai_text)
    return {"model": args.model, "voice": args.voice, "values": flow.values, "checks": checks, "turns": turns,
            "finished": flow.done}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--voice", default="Leda")
    args = p.parse_args()
    try:
        result = {"ok": True, **asyncio.run(run(args))}
    except Exception as exc:
        result = {"ok": False, "error": describe_error(exc)}
    save_result("G-06", result)


if __name__ == "__main__":
    main()
