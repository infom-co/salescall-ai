"""相手役の声の試験材料を作る（G-11・G-14・G-16 で使う）。

やること：相手役の短い発話を、会話に使う声（Sadachbia・Leda）とは別の声で gemini-3.8-live に読ませ、
電話の音質（8kHz）に落としてから 16kHz の WAV にして results/callee/ に保存する。
作った声は、その場で別の耳（listen.hear）に聞かせて確かめる（sample_ok）。通らなければ、--tries 回まで作り直す：
- 読みが台本と合うこと（listen.differences が空）
- 問いかけの文（「？」で終わる文）は、別の耳の文字も「？」で終わること
  ⛔ 2026-10-13 G-22：「はい？」（聞き返し）の声が、別の耳には「はい。」と聞こえていた。読みだけを比べていたので見逃し、
     AIが「はい」（答え）と受け取ったのを、AIの誤りと数えるところだった ⇒ 問いかけは「？」まで確かめる。読み方の指示も渡せるようにした
⚠️ 限界：作った声であって、人の声ではない。人の声での確かめは、別に録った音声で行う（G-14）。
   「？」は、別の耳が語尾の上がりを聞き取った印として使う（声の高さそのものは測っていない）。
"""
from __future__ import annotations

import asyncio
import json
import time

from audio import duration_s, phone_quality
from common import (HERE, base_parser, describe_error, make_client, pick_key, read_wav_pcm16_mono, require_python, save_result,
                    say, write_wav)
from listen import differences, hear
from live_util import build_config, collect_turn, send_text

CALLEE_VOICES = ("Puck", "Kore")  # 会話に使う声とは別の声（公式の音声一覧にある名前）
UTTERANCES = {
    "hello": "はい、もしもし。",
    "yes_reserved": "はい、予約しています。",
    "no_not_yet": "いいえ、まだしていません。",
    "datetime_pm": "15日の午後2時でお願いします。",
    "datetime_ambiguous": "15日の2時です。",
    "affirm": "はい、そうです。",
    "unclear": "えっと、じゅう、の……",
    # 2026-10-13：録音の名乗りへの聞き返し・途中で話し始める場面（DESIGN.md 6-13 の案を確かめる）
    "whois": "はい、どちら様ですか？",
    "what_for": "はい、何のご用件でしょうか？",
    "again": "すみません、もう一度お願いできますか？",
    "hai_q": ("はい？", "相手の言葉が聞き取れなかったので、語尾をはっきり上げて、聞き返すように"),
    "busy": "すみません、今ちょっと忙しいんですけど。",
    "transfer": "担当の者に代わりますので、少々お待ちください。",
    "taken_over": "お電話代わりました。担当の山田です。",
    "barge_who": "あ、はい、どちら様？",
    "barge_what": "はい、何でしょう？",
    "barge_yes": "あ、予約の件ですね。はい、してます。",  # ⛔ 別の耳には「はい、知ってます」と聞こえた（意味が変わる）⇒ 下の言い方に替えた
    "barge_yes2": "あ、予約の件ですね。はい、予約しています。",
    "backchannel": "はい。",
    # G-30（会話の間）：言葉の途中で間をあける相手。「えーとですね、」＋無音＋「3人です。」をつないで使う（AIが間に割り込むかを数える）
    "filler_eto": "えーとですね、",
    "three_people": "3人です。",
}
INSTRUCTION = "あなたは音声の材料を作る係です。渡された言葉だけを、そのまま、自然な日本語で読み上げます。言葉を足しません。"
OUT = HERE / "results" / "callee"


def text_and_how(v) -> tuple[str, str | None]:
    return (v, None) if isinstance(v, str) else v


def verified(wav: str) -> bool:
    """その声が、確かめ（sample_ok）に通っているか。試験は、通っていない声を使わない。"""
    index = OUT / "index.json"
    made = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
    return any(m.get("wav") == wav and m.get("ok") is True for m in made)


# 日本語の言葉の種類として範囲が決まっている一覧（業務や場面ごとの一覧ではない）
ANSWER_WORDS = {"はい", "ええ", "うん", "いいえ", "いえ", "いや"}  # 応答の言葉：答えの向きを決めるので、必ず比べる
QUESTION_WORDS = {"何", "なに", "なん", "誰", "だれ", "どなた", "どちら", "どっち", "どこ", "どれ", "どの", "どう", "いつ", "いくら",
                  "なぜ", "どうして", "いかが"}  # 問いかけの言葉


def _without_fillers(text: str) -> str:
    """応答の言葉以外の感動詞（「あ」「えっと」「あの」など）を除く。意味を変えないので、別の耳が落としても数えない。
    ⛔ 2026-10-13：「あ、はい、どちら様？」の頭の「あ」を別の耳が落として、確かめに落ちていた。"""
    from jtext import words

    return "".join(w.surface for w in words(text) if not (w.feature.pos1 == "感動詞" and w.surface not in ANSWER_WORDS))


def question_by_words(text: str) -> bool:
    """言葉だけで問いかけと分かるか（問いかけの言葉を含む・文が終助詞「か」で終わる）。「はい？」は分からない。"""
    from jtext import words

    ws = [w for w in words(text) if w.feature.pos1 != "補助記号"]
    return any(w.surface in QUESTION_WORDS for w in ws) or (bool(ws) and ws[-1].surface == "か" and ws[-1].feature.pos2 == "終助詞")


def sample_ok(text: str, heard: str) -> dict:
    """作った声の確かめ：(1) 読みが合う（応答の言葉以外の感動詞は数えない） (2) 言葉だけでは問いかけと分からない文（「はい？」）は、
    別の耳の文字も「？」で終わる（語尾の上がりが聞こえた印）。"""
    diffs = differences(_without_fillers(text), _without_fillers(heard))
    q_need = text.rstrip().endswith(("？", "?")) and not question_by_words(text)
    q_ok = (not q_need) or heard.rstrip().endswith(("？", "?"))
    return {"heard": heard, "diffs": diffs, "question_needed": q_need, "question_ok": q_ok, "ok": not diffs and q_ok}


async def run(args) -> dict:
    if args.recheck:  # 確かめの決まりを直したとき：保存した別の耳の文字に、いまの決まりを当て直す（キーを使わない）
        index = OUT / "index.json"
        made = json.loads(index.read_text(encoding="utf-8"))
        for m in made:
            if "heard" in m:
                m.update(sample_ok(m["text"], m["heard"]))
                say(f"{m['voice']} {m['id']}：聞こえた「{m['heard']}」 合格={m['ok']}")
        index.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"rechecked": made}
    if args.dry_run:
        for v in CALLEE_VOICES:
            build_config(voice=v, system_instruction=INSTRUCTION)
        return {"dry_run": True}
    client = make_client(pick_key(args))
    index = OUT / "index.json"
    made = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
    if args.verify:  # 作り直さずに、作ってある声を確かめだけする（確かめの決まりを足したとき）
        for m in made:
            if "error" in m or "ok" in m or not (OUT / m["wav"]).exists():
                continue
            pcm, rate = read_wav_pcm16_mono(OUT / m["wav"])
            m.update(sample_ok(m["text"], await hear(client, pcm, rate, "transcribe35")))
            say(f"{m['voice']} {m['id']}：聞こえた「{m['heard']}」 合格={m['ok']}")
        index.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"model": args.model, "verified": made}
    have = {(m.get("voice"), m.get("id")) for m in made if "error" not in m}
    for voice in CALLEE_VOICES:
        for uid, value in UTTERANCES.items():
            text, how = text_and_how(value)
            if uid not in (args.redo or []) and (voice, uid) in have and (OUT / f"{voice}_{uid}.wav").exists():
                continue  # 作ってある声は作り直さない（ほかの確かめの材料が変わらないように）。作り直すのは --redo で名指ししたものだけ
            made = [m for m in made if (m.get("voice"), m.get("id")) != (voice, uid)]
            tries = []
            for n in range(args.tries):
                try:
                    async with client.aio.live.connect(model=args.model, config=build_config(voice=voice, system_instruction=INSTRUCTION)) as s:
                        t = time.monotonic()
                        await send_text(s, f"読み上げる言葉：{text}" + (f"\n読み方：{how}" if how else ""))
                        turn = await collect_turn(s, started_at=t, timeout_s=60)
                    pcm = phone_quality(bytes(turn.audio), turn.audio_rate or 24000, 16000)
                    check = sample_ok(text, await hear(client, pcm, 16000, "transcribe35"))
                    tries.append(check)
                    say(f"{voice} {uid} {n + 1}回目：聞こえた「{check['heard']}」 合格={check['ok']}")
                    if check["ok"] or n == args.tries - 1:
                        path = OUT / f"{voice}_{uid}.wav"
                        write_wav(path, pcm, 16000)
                        made.append({"voice": voice, "id": uid, "text": text, "how": how, "said": turn.summary()["output_transcript"],
                                     **check, "tries": len(tries), "seconds": round(duration_s(pcm, 16000), 2), "wav": path.name})
                        break
                except Exception as exc:
                    tries.append({"error": describe_error(exc)})
                    if n == args.tries - 1:
                        made.append({"voice": voice, "id": uid, "text": text, "error": describe_error(exc)})
    index.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"model": args.model, "samples": made}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--tries", type=int, default=3, help="別の耳の確かめに通らないとき、作り直す回数の上限")
    p.add_argument("--redo", action="append", choices=sorted(UTTERANCES), help="作ってある声でも作り直す（名指ししたものだけ）")
    p.add_argument("--verify", action="store_true", help="作り直さずに、まだ確かめていない声を確かめだけする")
    p.add_argument("--recheck", action="store_true", help="保存した別の耳の文字に、いまの確かめの決まりを当て直す（キーを使わない）")
    args = p.parse_args()
    save_result("CALLEE-SAMPLES", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
