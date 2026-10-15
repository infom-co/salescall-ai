"""AIと同じ声で、プログラムが流す録音を作る（DESIGN.md 6-13・6-15）。
- 契約者の単位（声の型ごとに1回。どの依頼主にも同じもの）：相づち・つなぎの言葉・締め・待ってもらう一言（phrases.RECORDED）⇒ results/ack/
- 依頼主の単位（--script。トークスクリプトだけ）：質問の文（業務設定の質問。つなぎの言葉は含めない）⇒ results/script/
  ✅ 2026-10-15 ユーザー：「頻繁に使うワードは、契約者単位でシステム的に録音しておいたらいい。依頼者ごとに録音するのは、トークスクリプトのみ」

✅ 2026-10-14 ユーザー：聞き比べで「明らかにこちらの方が自然な間。話の流れもいい感じ」。基本は「はい、」。違和感のある場面や
   「はい」が続くときは、別の相づち。⇒ 言葉は決まった言い方の表（phrases.ACK）の1か所から読み、どの場面でどれを流すかは
   流れの制御（flowctl.Flow._ack）が決める。
作った録音は、別の耳の2通りの聞き方の**両方**で、読みが合うことを確かめる（毎回の通話で同じ録音を流すので、ほかの試験より厳しくする）。
長すぎる録音も落とす（間を埋めるための相づちが、AIの声を待たせないように）。長さは、声の前後の無音を除いた、声だけの長さで比べる
（⛔ 2026-10-14 の1回目：無音まで含めた長さで比べ、読みの合った S4 の「承知しました、」を長すぎとして落とした）。
作ってある録音は作り直さない（--redo で名指ししたものだけ作り直す）。
⚠️ 本番で使うなら、決まった発話の録音（6-13）と同じく、人（運営者）が聞いて承認する。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

from barge import voice_runs, voiced
from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from instruction import VOICE_TYPES, voice_style
from jtext import compare_reading
from listen import hear_feeds
from live_util import build_config, collect_turn, send_text
from phrases import RECORDED

OUT = HERE / "results" / "ack"
SCRIPT_OUT = HERE / "results" / "script"
def speech_seconds(pcm: bytes, rate: int) -> float:
    """声の前後の無音を除いた、声だけの長さ（秒）。"""
    runs = voice_runs(voiced(pcm, rate), 0.15)
    return runs[-1][1] - runs[0][0] if runs else 0.0


# 読み方（相づちとお詫びで、声の調子を変える）と、長さの上限（声だけの長さ・秒）
HOW = {"moushiwake": "電話で、相手に聞き取れなかったことを申し訳なく思いながら、丁寧に",
       "shitsurei": "電話で、こちらの聞き違いを丁寧にお詫びするように",
       # ✅ 2026-10-14 ユーザー：「Sadachbia の『ありがとうございます。』の1つが、明るいのは良いが、ハイテンション過ぎて、ちょっとお客が
       #    引きそう。『前向き』なくらいでいい」「Leda の『ありがとうございます。』は逆に少し、感謝が足りない暗い感じが多い」
       "arigatou": "電話で、相手に感謝を伝えるように、前向きで明るく、心を込めて。ただし、はしゃがず、声を張り上げず、落ち着いた調子で、短く",
       "lead_soredeha": "電話で、次のお話に移るつなぎの言葉として、自然に、柔らかく、短く",
       "lead_tsuzukimashite": "電話で、次のお話に移るつなぎの言葉として、自然に、柔らかく、短く",
       "lead_deha": "電話で、次のお話に移るつなぎの言葉として、自然に、柔らかく、短く",
       "closing": "電話の最後に、お客様に感謝を伝えるように、前向きで明るく、心を込めて、落ち着いた調子で",
       "wait": "電話で、お客様に少し待っていただくことを、申し訳なく思いながら、柔らかく丁寧にお願いするように",
       "question": "電話で、お客様に丁寧に、柔らかくお聞きするように"}
DEFAULT_HOW = "電話で、相手の話に相づちを打つように、短く"
MAX_S = {"hai": 1.0, "shouchi": 1.4, "arigatou": 1.6, "shitsurei": 1.6, "moushiwake": 1.6, "lead_soredeha": 1.0,
         "lead_tsuzukimashite": 1.2, "lead_deha": 0.8, "closing": 8.0, "wait": 3.5, "question": 5.0}


def heard_ok(text: str, heard: dict) -> bool:
    """2通りの聞き方の両方で、読みが合うか（句読点・言い終わりの違いは比べない）。"""
    want = compare_reading(text)
    return bool(heard) and all(v is not None and compare_reading(v) == want for v in heard.values())


def words(script: bool) -> tuple[dict, Path]:
    """作る録音の表 {中の名前: (言葉, 読み方と長さの上限の鍵)} と、置き場所。script＝依頼主の単位（試験の業務設定の質問の文）。"""
    if script:
        from g22_opening import BUSINESS
        from phrases import LEAD_RECORDED
        table = {q.id: (q.text, "question") for q in BUSINESS.questions}
        # 聞き比べ用：つなぎの言葉と質問を1本にした録音（ふつうの流れで付くつなぎの言葉＝2問目「それでは」・3問目「続きまして」）。
        # ⚠️ 2026-10-15 ユーザー：「『承知しました。それでは、ご希望の日時を、お聞かせいただけますでしょうか？』が特に酷い」⇒ 別々の録音を
        #    つなぐ形と、1本の録音の形を聞き比べていただく
        for q, lead in zip(BUSINESS.questions[1:], ("それでは", "続きまして")):
            table[f"{q.id}_{LEAD_RECORDED[lead]}"] = (lead + q.text, "question")
        return table, SCRIPT_OUT
    return {aid: (text, aid) for aid, text in RECORDED.items()}, OUT


async def run(args) -> dict:
    table, out = words(args.script)
    ids = args.id or list(table)
    assert set(ids) <= set(table), set(ids) - set(table)
    index_path = out / "index.json"
    made = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    # 作り済みでも、文が変わった録音は作り直す（⛔ 2026-10-15：読点を外した質問の文が、作り済みとして飛ばされていた）
    have = {(m["type"], m["id"]) for m in made if m.get("ok") and m.get("text") == table.get(m["id"], (None,))[0]}
    todo = [(vt, aid) for vt in (args.type or VOICE_TYPES) for aid in ids if (vt, aid) not in have or aid in (args.redo or [])]
    if args.dry_run:
        return {"dry_run": True, "todo": todo, "words": {a: table[a][0] for a in ids}}
    client = make_client(pick_key(args))
    out.mkdir(parents=True, exist_ok=True)
    for vt, aid in todo:
        voice, style = voice_style(vt)
        text, how = table[aid]
        row = None
        for n in range(args.tries):
            try:
                inst = (f"{style}\nあなたは音声の材料を作る係です。渡された言葉だけを、{HOW.get(how, DEFAULT_HOW)}、そのまま読み上げます。"
                        "言葉を足しません。")
                async with client.aio.live.connect(model=args.model, config=build_config(voice=voice, system_instruction=inst)) as s:
                    t = time.monotonic()
                    await send_text(s, f"読み上げる言葉：{text}")
                    turn = await collect_turn(s, started_at=t, timeout_s=60)
                pcm, rate = bytes(turn.audio), turn.audio_rate or 24000
                seconds = speech_seconds(pcm, rate) if pcm else 0.0
                heard = await hear_feeds(client, pcm, rate) if pcm else {}
                ok = bool(pcm) and heard_ok(text, heard) and seconds <= MAX_S[how]
                say(f"{vt} {text} {n + 1}回目：聞こえた {heard} 長さ {seconds:.2f}秒 合格={ok}")
                row = {"type": vt, "voice": voice, "id": aid, "text": text, "heard": heard, "ok": ok, "seconds": round(seconds, 2),
                       "tries": n + 1}
                if ok:
                    path = out / f"{vt}_{aid}.wav"
                    write_wav(path, pcm, rate)
                    row["wav"] = path.name
                    break
            except Exception as exc:
                row = {"type": vt, "id": aid, "text": text, "ok": False, "error": describe_error(exc), "tries": n + 1}
        made = [m for m in made if (m["type"], m["id"]) != (vt, aid)] + [row]
        index_path.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")  # 1本ごとに残す（途中で止まっても失わない）
        await asyncio.sleep(args.pause)
    return {"model": args.model, "made": made, "missing": [(m["type"], m["id"]) for m in made if not m.get("ok")]}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--tries", type=int, default=3)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--script", action="store_true", help="依頼主の単位の録音（試験の業務設定の質問の文）を作る")
    p.add_argument("--id", action="append", help="作る録音（中の名前。質問なら Q2 など）。無ければ全部")
    p.add_argument("--redo", action="append", help="作ってある録音でも作り直す（名指ししたものだけ）")
    args = p.parse_args()
    if args.dry_run:  # 下見は保存しない（G-36 と同じ）
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
        return
    save_result("ACK-CLIPS", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
