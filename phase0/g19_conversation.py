"""G-19：実際の電話に近い会話の形（相手の声に答える）で、選ばれた声の型の間違いの起きる割合を測る。

やること：
- 予約の確認の短い会話を、相手役の声（results/callee/ の電話の音質の声）で進める。相手の発話は4回、AIの発話も4回。
- AIの最初の発話の名乗りは、決まった文（OPENING）を言わせる（言うべき言葉が分かっている）。
- AIの1ターンごとに、見分け方（turn_check）を当てる（比べる言葉は、AI自身の文字起こし。最初の名乗りは決まった文も）。
- 通話1本ぶんのAIの声をつないで、別の耳（listen）で聞き直し、AI自身の文字起こし（言うつもりの言葉）と比べる
  ⇒ 食い違い＝声が言うつもりの言葉どおりに出なかった所（途中で止まった・抜けた・崩れた）。
- AI自身の文字起こしが届いた割合も数える（会話の中で見分けられるかが、これで決まる）。
⚠️ 限界：相手役は作った声。わずかな言い間違い（「おも忙しい」）は別の耳では見つけられない。
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time

from common import HERE, base_parser, describe_error, make_client, pick_key, read_wav_pcm16_mono, require_python, save_result, say, write_wav
from instruction import VOICE_TYPES, voice_style
from jtext import reading
from listen import differences, hear
from live_util import build_config, callee_turn, collect_turn, send_text
from turn_check import check_ai_turn

OPENING = "お忙しいところ恐れ入ります。株式会社「アオバ物流」の「佐藤」と申します。"
INSTRUCTION = (
    "あなたは株式会社「アオバ物流」の「佐藤」として、ご予約の確認の電話をかけています。"
    f"相手が電話に出たら、まず「{OPENING}」と名乗ってください。続けて、ご予約をされているかを聞いてください。"
    "予約しているとわかったら、ご予約の日時を聞いてください。"
    "日時を聞いたら、その日時を言い直して、合っているかを聞いてください。"
    "合っていれば、お礼を言って電話を終えてください。"
    "1回の返事は短くしてください。"
)
# 2026-10-13 G-19：通話の最初の発話の書き出し（「お忙しいところ」）が、36本中6本で崩れた（セッションを開いて最初の声）。
# ⇒ 仮説：セッションの最初の声は崩れやすい。電話がつながる前に、相手に聞かせない「慣らし」の一言を言わせ、
#    名乗りをセッションの2回目の声にすれば防げるか、を比べる（--warmup on/off）。
WARMUP_RULE = "電話がつながる前に「準備」という合図が来ます。そのときは「はい」とだけ言ってください（相手には聞こえません）。"
WARMUP_CUE = "準備"
CALLEE = ["hello", "yes_reserved", "datetime_pm", "affirm"]
# 2026-10-13：慣らしの一言は効かなかった（崩れ 24本中2本 ⇔ 慣らしなし 24本中2本）。
# ⇒ 名乗り（と最初の質問）を、前もって作って人が聞いて確かめた録音で流す形（--opening recorded）を試す。
#    AIは名乗りを自分で言わず、相手の答えから会話を続ける。AIの最初の声は、相手の答えへの返事になる。
RECORDED_OPENING = OPENING + "ご予約の確認でお電話いたしました。ご予約はされていますでしょうか。"
RECORDED_RULE = (f"相手が電話に出たときの名乗りと最初の質問（「{RECORDED_OPENING}」）は、録音で相手に流してあります。"
                 "あなたは、相手の答えから会話を続けます。もう一度名乗ったり、同じ質問をしたりしてはいけません。")
CALLEE_VOICE = "Puck"
TYPES = VOICE_TYPES  # 声の型は instruction.py の1か所から
AUDIO_DIR = HERE / "results" / "g19_audio"
GAP = b"\x00\x00" * int(24000 * 0.6)


async def one_call(client, args, cid: str, type_id: str, thinking: int | None, warmup: bool, recorded: bool = False) -> dict:
    voice, style_text = voice_style(type_id)
    cfg = build_config(voice=voice, system_instruction=INSTRUCTION + (WARMUP_RULE if warmup else "")
                       + (RECORDED_RULE if recorded else "") + style_text, thinking_budget=thinking)
    callee_ids = CALLEE[1:] if recorded else CALLEE  # 録音の名乗りのときは、相手の最初の発話は名乗りへの答え
    first_audio = b""
    turns, audio = [], bytearray()
    warm = None
    async with client.aio.live.connect(model=args.model, config=cfg) as s:
        if warmup:  # 相手に聞かせない慣らしの一言（声は捨てる）
            t0 = time.monotonic()
            await send_text(s, WARMUP_CUE)
            w = await collect_turn(s, started_at=t0, timeout_s=30)
            warm = {"intended": w.summary()["output_transcript"], "audio_s": w.summary()["audio_seconds"],
                    "first_audio_after_s": w.first_audio_after_s}
        for i, callee_id in enumerate(callee_ids):
            pcm, rate = read_wav_pcm16_mono(HERE / "results" / "callee" / f"{CALLEE_VOICE}_{callee_id}.wav")
            turn = await callee_turn(s, pcm, rate, timeout_s=45)
            r = turn.summary()
            check = check_ai_turn(audio=bytes(turn.audio), rate=turn.audio_rate or 24000, voice=voice,
                                  timed_out=turn.timed_out, intended_text=r["output_transcript"] or None)
            turns.append({"callee": callee_id, "heard": r["input_transcript"], "intended": r["output_transcript"],
                          "first_audio_after_s": r["first_audio_after_s"], "check": check, "timed_out": turn.timed_out})
            if i == 0:
                first_audio = bytes(turn.audio)
            if audio and turn.audio:
                audio += GAP
            audio += turn.audio
    row = {"id": cid, "type": type_id, "voice": voice, "thinking": "auto" if thinking is None else thinking,
           "warmup": warmup, "warmup_turn": warm, "opening_mode": "recorded" if recorded else "ai", "turns": turns}
    if audio:
        wav = AUDIO_DIR / f"{cid}.wav"
        write_wav(wav, bytes(audio), 24000)
        row["wav"] = wav.name
        row["ear"] = await hear(client, bytes(audio), 24000, "transcribe35")
        intended = "".join(t["intended"] for t in turns)
        row["intended_complete"] = all(t["intended"] for t in turns)
        # 言うつもりの言葉と、実際に出た声（別の耳）の食い違い。言うつもりの言葉が欠けている回は比べない
        row["ear_vs_intended"] = differences(intended, row["ear"]) if row["intended_complete"] else None
        # 名乗り（決まった文）が、声で正しく出たか：別の耳の書き出しの最初の部分と比べる
        head = reading(row["ear"])[:len(reading(OPENING)) + 4]
        # 名乗りの後ろに続く言葉（次の文の書き出し）は、名乗りの誤りとして数えない
        row["opening_diffs"] = [d for d in differences(OPENING, head) if not (d["kind"] == "insert" and d["script"].endswith("[]"))]
        # 書き出し（社名より前）の崩れだけ。社名の「ア」がオ寄りに聞こえるもの（ユーザー評価：少し噛んだくらい）は別に数える
        # AIの最初の声の書き出しが崩れたか（名乗りを録音で流すときは、相手の答えへの最初の返事）：最初の声だけを別の耳で聞き、言うつもりの言葉と比べる
        if first_audio and turns[0]["intended"]:
            row["first_turn_ear"] = await hear(client, first_audio, 24000, "transcribe35")
            fd = differences(turns[0]["intended"], row["first_turn_ear"])
            row["first_turn_diffs"] = fd
            # 書き出し＝最初の10音以内の食い違い（社名の「ア」がオ寄りなだけのものは数えない）
            row["first_head_broken"] = any(d["at"] < 10 and "あおば" not in d["script"] and d["heard"] != "お" for d in fd)
        row["reintroduced"] = recorded and "申します" in (turns[0]["intended"] or "")
        row["opening_head_broken"] = (not recorded) and any(d["script"].startswith(("お[", "おい[", "[]お", "おいそ[")) or "おそれい" in d["script"][:12]
                                         for d in row["opening_diffs"])
    return row


def summarize(rows: list[dict]) -> dict:
    out = {}
    def k(r):
        return (r.get("type"), str(r.get("thinking")), str(r.get("warmup")), str(r.get("opening_mode")))
    for key in sorted({k(r) for r in rows}):
        xs = [r for r in rows if k(r) == key and "error" not in r]
        turns = [u for r in xs for u in r["turns"]]
        gaps = [u["first_audio_after_s"] for u in turns if u["first_audio_after_s"] is not None]
        kinds = {}
        for u in turns:
            kinds[u["check"]["kind"]] = kinds.get(u["check"]["kind"], 0) + 1
        out[f"{key[0]}/考える量={key[1]}/慣らし={key[2]}/名乗り={key[3]}"] = {"calls": len(xs), "turns": len(turns), "kinds": kinds,
                  "intended_present": sum(1 for u in turns if u["intended"]),
                  "calls_with_ear_vs_intended_diffs": sum(1 for r in xs if r.get("ear_vs_intended")),
                  "calls_with_opening_diffs": sum(1 for r in xs if r.get("opening_diffs")),
                  "calls_with_opening_head_broken": sum(1 for r in xs if r.get("opening_head_broken")),
                  "calls_with_first_head_broken": sum(1 for r in xs if r.get("first_head_broken")),
                  "calls_reintroduced": sum(1 for r in xs if r.get("reintroduced")),
                  "gap_median_s": round(statistics.median(gaps), 2) if gaps else None,
                  "gap_max_s": max(gaps) if gaps else None}
    out["errors"] = sum(1 for r in rows if "error" in r)
    return out


async def run(args) -> dict:
    if args.dry_run:
        for type_id in TYPES:
            voice, style_text = voice_style(type_id)
            build_config(voice=voice, system_instruction=INSTRUCTION + style_text)
        return {"dry_run": True}
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    client = make_client(pick_key(args))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log = AUDIO_DIR / f"partial_{stamp}.jsonl"
    rows, n = [], 0
    thinkings = [None if x == "auto" else int(x) for x in (args.thinking or ["auto"])]
    warmups = [x == "on" for x in (args.warmup or ["off"])]
    openings = [x == "recorded" for x in (args.opening or ["ai"])]
    for rep in range(args.repeat):
        for type_id in args.type or TYPES:
            for thinking, warmup, recorded in [(th, w, o) for th in thinkings for w in warmups for o in openings]:
                n += 1
                cid = f"{stamp}_{n:03d}_{type_id}_t{'auto' if thinking is None else thinking}_w{int(warmup)}_o{'rec' if recorded else 'ai'}"
                try:
                    row = await one_call(client, args, cid, type_id, thinking, warmup, recorded)
                except Exception as exc:
                    row = {"id": cid, "type": type_id, "thinking": "auto" if thinking is None else thinking, "error": describe_error(exc)}
                rows.append(row)
                with log.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                kinds = [u["check"]["kind"] for u in row.get("turns", [])]
                say(f"[{n}] {type_id} 考える量={row.get('thinking')} 慣らし={row.get('warmup')} 名乗り={row.get('opening_mode')} 最初の声の崩れ={row.get('first_head_broken')} 名乗り直し={row.get('reintroduced')} 判定={kinds} 文字起こし={[bool(u['intended']) for u in row.get('turns', [])]} "
                    f"名乗り={json.dumps(row.get('opening_diffs'), ensure_ascii=False)[:120]} "
                    f"声と言うつもりの食い違い={json.dumps(row.get('ear_vs_intended'), ensure_ascii=False)[:160]} {row.get('error', '')}")
                await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "opening": OPENING, "callee_voice": CALLEE_VOICE, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=10)
    p.add_argument("--pause", type=float, default=4.0)
    p.add_argument("--type", choices=sorted(TYPES), action="append")
    p.add_argument("--thinking", choices=["auto", "0"], action="append", help="考える量：auto＝既定（自動）、0＝考えない")
    p.add_argument("--warmup", choices=["off", "on"], action="append", help="電話がつながる前の、相手に聞かせない慣らしの一言")
    p.add_argument("--opening", choices=["ai", "recorded"], action="append", help="名乗りを AI が言う／前もって作った録音で流す")
    args = p.parse_args()
    save_result("G-19", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
