"""G-50：名乗りや用件を聞き返されたとき、AIが自分で名乗り直さずに、録音の流し直し（flowctl の replay_opening）を頼めるか（2026-10-17）。

✅ 2026-10-13 ユーザー：名乗り直しも録音で流す（「間が大丈夫ならOK」。DESIGN.md 6-13）。⛔ G-22・G-29：AIが自分で名乗り直すと、社名が正しく
聞こえない（24本中、2通りの耳の両方で正しく聞こえたのは3本）。
- 場面：G-22 の場面（どちら様・ご用件・もう一度・はい？・代わる・録音の途中で話し始めた3つ）。名乗りと質問を録音で流す形（play_recordings）。
  相手の声は、会話のAIと入れ替えのお客役（Sadachbia の型には Leda、Leda の型には Sadachbia。G-36 から。電話の音質）。
- 見ること：(1) AIが、場面に合う部分で流し直しを頼んだか（どちら様＝名乗り・ご用件＝用件・もう一度／はい？＝最初から・代わった＝代わった方へ。
  名乗りや用件を求める場面で「最初から」も可） (2) ⛔ AIが自分で社名・名前を言わなかったか（AI自身の文字と別の耳の両方を見る）
  (3) そのあとの相手の答え（「はい、予約しています。」）を Q1 として受け付け、次の質問へ進んだか (4) 相手の声の終わりから、AIが流し直しを
  頼むまでの時間（頼んだ瞬間に「失礼いたしました。」の録音が流れる＝相手が待つ間）。
⚠️ 試験では録音を実際には流さない（相手役は台本どおりに次の言葉を言う）。録音そのものの声と間は、録音を作ったとき（make_ack_clips）と
本物の電話で確かめる。
"""
from __future__ import annotations

import asyncio
import json

from common import KeyRing, base_parser, describe_error, drop_partial, require_python, save_partial, save_result, say
from meaning_judge import add_judge_args, make_judges
from convo import callee_voice_for, callee_wav, run_call
from flowctl import GOT, PART_LABEL
from g22_opening import BUSINESS, NAME, PARTS, W
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from instruction import VOICE_TYPES
from make_callee_samples import verified
from stitch_gap import callee_speech

ALL = PART_LABEL["all"]
# 場面：(相手の台詞, 録音の途中で止めた形（届いた文, まだの文）, 合う部分, 流し直しを頼む台詞の番号)
SCENES = {
    "K1_どちら様": ([W("whois"), W("yes_reserved")], None, {PART_LABEL["intro"], ALL}, 0),
    "K2_ご用件": ([W("what_for"), W("yes_reserved")], None, {PART_LABEL["purpose"], ALL}, 0),
    "K3_もう一度": ([W("again"), W("yes_reserved")], None, {ALL}, 0),
    "K4_はい？": ([W("hai_q"), W("yes_reserved")], None, {ALL}, 0),
    "K6_代わる": ([W("transfer"), W("taken_over"), W("yes_reserved")], None, {PART_LABEL["handover"]}, 1),
    "B0_最初から": ([W("barge_who"), W("yes_reserved")], ([], PARTS), {PART_LABEL["intro"], ALL}, 0),
    "B1_あいさつのあと": ([W("barge_who"), W("yes_reserved")], (PARTS[:1], PARTS[1:]), {PART_LABEL["intro"], ALL}, 0),
    "B2_名乗りのあと": ([W("barge_what"), W("yes_reserved")], (PARTS[:2], PARTS[2:]), {PART_LABEL["purpose"], ALL}, 0),
}


def measures(call: dict, scene: str) -> dict:
    lines, barge, ok_parts, at = SCENES[scene]
    turns, log = call["turns"], call["flow_log"]
    # 流れの制御が受け付けた流し直しだけ（取り次ぎを待つ間の頼みは、流れの制御が止める＝held。2026-10-18）
    held = [e for e in log if e["op"] == "replay_opening" and e["reply"]["result"] == "hold"]
    replays = [e for e in log if e["op"] == "replay_opening" and e["reply"]["result"] != "hold"]
    part = PART_LABEL.get(replays[0]["args"].get("part")) if replays else None
    # AIが自分で社名・名前を言ったか：言うつもりの文字（AI自身）と、別の耳の文字の、どちらかにあれば言ったとする（G-22 と同じ見方）
    spoke_name = [t["index"] for t in turns
                  if any(w in (t.get("intended") or "") + " " + (t.get("ear") or "") for w in NAME + [BUSINESS.caller])]
    replay_turn = next((t for t in turns if any(e["name"] == "replay_opening" and e["reply"] != "hold" for e in t.get("tool_events") or [])), None)
    ask_s = None
    if replay_turn and isinstance(replay_turn.get("callee"), dict) and "wav" in replay_turn["callee"]:
        ev = next(e for e in replay_turn["tool_events"] if e["name"] == "replay_opening" and e["reply"] != "hold")
        if ev.get("at_s") is not None:  # 相手の声の終わりから数える（at_s は送り終えてから。声のあとの無音を足す。G-30 と同じ）
            ask_s = round(ev["at_s"] + callee_speech(replay_turn["callee"]["wav"])[2], 3)
    # 相手に、名乗りと用件が一度は届いたか（録音の途中で止めたときに届いた文＋流し直した文。あいさつは問わない。⛔ G-50 の1回目：
    # 用件が届く前の「どちら様」に、AIは「名乗り」だけを選び、用件が一度も届かなかった）
    delivered = set(barge[0] if barge else PARTS) | {p["text"] for e in replays for p in (e["reply"].get("play") or [])}
    it = call["items"]["Q1"]
    return {"checks": {"all_delivered": all(p in delivered for p in PARTS[1:3]),"replay_ok": bool(replays) and part in ok_parts and replays[0]["reply"]["result"] == "replayed"
                                    and replay_turn is not None and replay_turn["index"] == at,
                       "part": part, "replays": len(replays), "replay_at": replay_turn["index"] if replay_turn else None,
                       "held": len(held),  # 取り次ぎを待つ間に頼んで、流れの制御が止めた回数
                       "spoke_name": bool(spoke_name), "spoke_name_at": spoke_name,
                       # 流し直しのターンで、AIが自分の声を出したか（質問も録音で流すので、黙っているのが正しい）
                       "replay_turn_voice_s": (replay_turn or {}).get("audio_seconds"),
                       "q1_ok": it["condition"] == "はい" and it["state"] in GOT,
                       "asked_q2": any(e["reply"].get("result") == "next" and e["reply"].get("question_id") == "Q2" for e in log),
                       "ask_s": ask_s},
            "ops": [(e["op"], e["args"], e["reply"]["result"]) for e in log],
            "said": [(t.get("intended") or "")[:80] for t in turns],
            "heard": [t.get("heard") for t in turns]}


async def run(args) -> dict:
    ring = KeyRing(args)  # 上限・止められたキーは飛ばす（common.KeyRing.acall）
    judges = make_judges(args)
    rows = []
    for rep in range(args.repeat):
        for scene, (lines, barge, _, _) in SCENES.items():
            if args.only and not any(scene.startswith(o) for o in args.only):
                continue
            for vt in args.type or VOICE_TYPES:
                head = {"rep": rep, "scene": scene, "type": vt}
                voice = callee_voice_for(vt) if args.callee == "swap" else args.callee
                head["callee_voice"] = voice
                if args.swap_before is not None:
                    head["swap_before"] = args.swap_before
                callee = [{**x, "wav": callee_wav(x["wav"], voice)} for x in lines]
                unverified = [c["wav"] for c in callee if not verified(c["wav"])]
                if unverified:  # 確かめに通っていない相手役の声では動かさない（G-22 と同じ）
                    rows.append({**head, "skipped": f"相手役の声が確かめに通っていない：{unverified}"})
                    say(f"[{rep}] {scene} {vt} 動かさない：相手役の声が確かめに通っていない {unverified}")
                    continue
                try:
                    call, _ = await ring.acall(args.model, lambda client: run_call(
                        client, args.model, BUSINESS, vt, lines, opening_mode="recorded", barge_in=barge, use_ear=True,
                        save_audio="bad", audio_label=f"G50_{scene}_{vt}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S,
                        play_recordings=True, callee_voice=voice, swap_before=args.swap_before, **judges, **FAST))
                    row = keep_call(head, call, lambda c: measures(c, scene))
                except Exception as exc:
                    row = {**head, "error": describe_error(exc)}
                rows.append(row)
                c = row.get("checks", {})
                say(f"[{rep}] {scene} {vt}：流し直し={c.get('replay_ok')}（{c.get('part')}・{c.get('replays')}回・{c.get('replay_at')}番目）"
                    f"名乗りと用件が届いた={c.get('all_delivered')} 自分で名乗った={c.get('spoke_name_at')} 流し直しのターンの声={c.get('replay_turn_voice_s')}秒 Q1={c.get('q1_ok')} "
                    f"次の質問={c.get('asked_q2')} 頼むまで={c.get('ask_s')}秒 AIの言葉={row.get('said')} {row.get('error', '')}")
                save_partial(f"G-50{args.tag}", {"rows": rows})
                await asyncio.sleep(args.pause)
    return {"model": args.model, "summary": summarize(rows), "rows": rows}


def summarize(rows: list[dict]) -> dict:
    done = [r for r in rows if "checks" in r]
    summary = {s: {k: f"{sum(1 for r in done if r['scene'] == s and r['checks'].get(k))}/{sum(1 for r in done if r['scene'] == s)}"
                   for k in ("replay_ok", "all_delivered", "spoke_name", "q1_ok", "asked_q2", "held")} for s in SCENES}
    asks = sorted(r["checks"]["ask_s"] for r in done if r["checks"].get("ask_s") is not None)
    summary["頼むまでの秒（中央・遅いほうから1割・最大）"] = [asks[len(asks) // 2], asks[int(len(asks) * 0.9)], asks[-1]] if asks else None
    summary["誤り・数えられなかった"] = [f"{r['scene']} {r.get('type')}" for r in rows if "error" in r or "measure_error" in r]
    say("数え：", json.dumps(summary, ensure_ascii=False))
    return summary


def merge(paths: list[str]) -> dict:
    """別々のキーで同時に流した結果を1つにまとめて数え直す（数え方を直したときも、通話をやり直さずに当て直す）。"""
    rows = []
    for p in paths:
        d = json.loads(open(p, encoding="utf-8").read())
        for r in d["rows"]:
            if "turns" in r:
                r.update(measures(r, r["scene"]))
            rows.append(r)
    return {"merged_from": paths, "summary": summarize(rows), "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--only", action="append")
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--tag", default="")
    add_judge_args(p)
    # お客役の声：既定は会話のAIと入れ替え（G-36 から）。その声の台詞が確かめに通っていないときは、通っている声（Puck など）を指定する
    p.add_argument("--callee", default="swap", help="お客役の声（swap＝会話のAIと入れ替え／Puck・Leda・Sadachbia など）")
    # 張り替え：その番号の相手の発話の前でセッションを開き直す（張り替えのあとの指示文にも、流し直しの決まりが要る。0＝最初の発話の前）
    p.add_argument("--swap-before", type=int)
    p.add_argument("--merge", nargs="+", help="保存した結果をまとめて数え直す（問い合わせはしない）")
    args = p.parse_args()
    if args.merge:
        save_result("G-50", merge(args.merge))
        return
    save_result(f"G-50{args.tag}", asyncio.run(run(args)))
    drop_partial(f"G-50{args.tag}")


if __name__ == "__main__":
    main()
