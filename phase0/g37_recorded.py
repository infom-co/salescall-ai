"""G-37：次の質問と締めを、電話接続側が録音で流し、AIには話させない形を確かめる（DESIGN.md 6-13）。

✅ 2026-10-15 ユーザー：
- 「『はい、』『ありがとうございます。』『ありがとうございました。』『それでは』『続きまして』『承知しました、』『かしこまりました』
  『少々お待ちいただいてもよろしいでしょうか。』などなど、頻繁に使うワードは、契約者単位でシステム的に録音しておいたらいい。依頼者ごとに
  録音するのは、トークスクリプトのみにしておいたら」
- 「両方録音の場合は、良いものを録音として残すので問題ない。AIが喋る時だけ、相槌を入れないと問題が起こる」
⇒ 流す順：相づち（契約者の単位）→ 0.6秒 → つなぎの言葉（契約者の単位）→ 質問の文（依頼主の単位）。締めの言葉も契約者の単位の録音。
   AIへの返事は「話させない」形（SILENT）で返し、AIは黙って相手の答えを待つ。値が変わる言い直しは、これまでどおりAIが話す。
見ること：
- 録音を流したターンで、AIが黙っていたか（話していたら、電話接続側はその声を流さない＝聞こえないが、AIの会話の中ではずれる）
- 次のターンで、AIが質問を言い直したり、名乗り直したりしなかったか
- 値と項目の状態・差し戻し・相づち・相手に最初の声が届くまでの間
- 聞き比べの声（録音の質問・つなぎの言葉・締めを入れてつないだもの）
"""
from __future__ import annotations

import asyncio
import json
import statistics as st
from pathlib import Path

from common import base_parser, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, save_result, say
from convo import CALLEE_FOR, callee_voice_for, run_call
from g22_opening import BUSINESS
from g30_latency import keep_call
from g34_ack import FAST, FOLLOWUP_WAIT_S
from g36_customer import WANT, judge, scenes, turn_measures, utt
from instruction import VOICE_TYPES
from stitch_gap import played_wav, stitch_call

SCENES = ("ふつう", "長い間", "先に答えた", "訂正")


def plan() -> dict[str, list]:
    p = scenes()
    w = p["訂正"]  # 「ふつう」＝訂正の場面から、直す台詞を除いたもの
    return {"ふつう": [w[0], w[1], *w[3:]], **{k: p[k] for k in SCENES if k != "ふつう"}}


def measures(call: dict, scene: str) -> dict:
    q_texts = [q.text for q in BUSINESS.questions]
    tm = turn_measures(call)
    play_turns, after_play = [], []
    for i, t in enumerate(call["turns"]):
        play = next((e for e in t.get("tool_events") or [] if e.get("play")), None)
        if play is None:
            continue
        play_turns.append({"played": [x["text"] for x in play["play"]], "ai_spoke": bool(t.get("audio_file")),
                           "ai_said": (t.get("intended") or "")[:60]})
        if i + 1 < len(call["turns"]):  # 録音のあとのAIの言葉に、質問の文が入っていないか（言い直し・名乗り直し）
            nxt = call["turns"][i + 1]
            said = nxt.get("intended") or ""
            # プログラムが渡した言い方（聞き直し「恐れ入ります。ご希望の日時を、もう一度、…」）は、繰り返しに数えない
            # （⛔ G-37 の1回目：数え方がこれを繰り返しに数えていた＝3回とも聞き直し）
            program = [e["say"] for e in nxt.get("tool_events") or [] if e.get("say")]
            after_play.append({"said": said[:60], "repeated_question": any(q[:6] in said for q in q_texts)
                               and not any(x[:12] in said for x in program)})
    return {"turns_measured": tm, "checks": judge(call, scene if scene in WANT else "考える間"),
            "acks": [e["reply"].get("ack") for e in call["flow_log"] if e["reply"].get("ack")],
            "says": [e["reply"].get("say") for e in call["flow_log"] if e["reply"].get("say")],
            "play_turns": play_turns, "after_play": after_play}


def speech_rate(rows: list[dict]) -> dict:
    """AIが自分で話した言い直しの速さ（声だけの長さ1秒あたりの拍の数）。声の型ごとの中央と最も速い値。
    ⚠️ 2026-10-15 ユーザー：「『かしこまりました。3人で大丈夫でしょうか』が早回しのように聞こえる」（G-37 の2回目の S2。1秒あたり 10.3拍）。
    「はい。」だけのターンは除く（拍が少なく、速さにならない）。声の確かめが通ったターンだけ（崩れた声の長さは比べない）。"""
    by = {}
    for r in rows:
        for t in r.get("turns") or []:
            c = t.get("check") or {}
            if t.get("audio_file") and c.get("kind") == "ok" and c.get("voiced_s") and (c.get("morae") or 0) >= 10:
                by.setdefault(r["type"], []).append(c["morae"] / c["voiced_s"])
    return {vt: {"n": len(v), "median": round(st.median(v), 2), "max": round(max(v), 2)} for vt, v in sorted(by.items())}


def summarize(rows: list[dict]) -> dict:
    rs = [r for r in rows if "checks" in r]
    pt = [p for r in rs for p in r["play_turns"]]
    ap = [a for r in rs for a in r["after_play"]]
    first = sorted(t["first_heard_s"] for r in rs for t in r["turns_measured"]
                   if t.get("first_heard_s") is not None and not t.get("cut_in"))
    return {"calls": len(rs), "errors": sum(1 for r in rows if "error" in r),
            "values_ok": sum(r["checks"]["q2_ok"] + r["checks"]["q3_ok"] for r in rs), "values": 2 * len(rs),
            "by_scene": {sc: f'{sum(r["checks"]["q2_ok"] and r["checks"]["q3_ok"] for r in rs if r["scene"] == sc)}/'
                             f'{sum(1 for r in rs if r["scene"] == sc)}' for sc in SCENES if any(r["scene"] == sc for r in rs)},
            "play_turns": len(pt), "ai_spoke_on_play": sum(p["ai_spoke"] for p in pt),
            "ai_said_on_play": sorted({p["ai_said"] for p in pt if p["ai_said"]}),
            "repeated_question_after_play": sum(a["repeated_question"] for a in ap),
            "rejected": sum(r["checks"]["rejected"] for r in rs), "speech_rate": speech_rate(rs),
            "first_heard": {"n": len(first), "median": round(st.median(first), 2),
                            "p90": round(first[min(len(first) - 1, int(len(first) * 0.9))], 2)} if first else None}


def needed(p: dict) -> list[str]:
    """試験に要る録音（お客役の声・契約者の単位の録音・依頼主の単位の録音）で、まだ無いもの。"""
    from make_callee_samples import verified
    from flowctl import spoken_question
    miss = sorted({f"{v}_{utt(x[k])}.wav" for v in CALLEE_FOR.values() for xs in p.values() for x in xs for k in ("wav", "again")
                   if k in x and "pause" not in x[k] and not verified(f"{v}_{utt(x[k])}.wav")})
    for vt in VOICE_TYPES:
        items = [{"kind": "closing", "text": ""}] + [{"kind": "question", "id": q.id, "text": t, "lead": lead}
                                                      for q in BUSINESS.questions[1:] for t, lead in [spoken_question(BUSINESS.questions, q.id)]]
        miss += [str(played_wav(vt, i).name) for i in items if not played_wav(vt, i).exists()]
    return miss


def make_listening(result: Path, out: Path) -> list[dict]:
    """場面ごと・声の型ごとに1本、録音を入れてつないだ聞き比べの声（値が両方取れた通話を先に選ぶ）。"""
    d = json.loads(result.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    rows = sorted((r for r in d["rows"] if "turns" in r), key=lambda r: not (r["checks"]["q2_ok"] and r["checks"]["q3_ok"]))
    made, n = [], 0
    for scene in SCENES:
        for vt in VOICE_TYPES:
            for r in rows:
                if (r["type"], r["scene"]) != (vt, scene):
                    continue
                n += 1
                path = out / f"{n:02d}_{scene}_{vt}_お客{callee_voice_for(vt)}_録音の質問.wav"
                p = stitch_call(r, vt, path)
                if p is None:
                    n -= 1
                    path.unlink(missing_ok=True)
                    continue
                made.append({"file": path.name, "heard_first": [x["heard_first"] for x in p],
                             "ai_spoke_on_play": [x.get("ai_spoke_on_play") for x in p if "played" in x]})
                break
    return made


async def run(args) -> dict:
    p = plan()
    if args.dry_run:
        return {"dry_run": True, "missing": needed(p), "scenes": {k: [utt(x.get("wav")) if isinstance(x, dict) else x for x in v]
                                                                  for k, v in p.items()}}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for vt in args.type or VOICE_TYPES:
            for scene in args.scene or SCENES:
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, p[scene], opening_mode="recorded", use_ear=False,
                                          save_audio="all", audio_label=f"G37_{scene}_{rep}", followup_wait_s=FOLLOWUP_WAIT_S,
                                          callee_voice="swap", play_recordings=True, **FAST)
                    row = keep_call({"rep": rep, "type": vt, "scene": scene}, call, lambda c: measures(c, scene))
                except Exception as exc:
                    row = {"rep": rep, "type": vt, "scene": scene, "error": describe_error(exc)}
                rows.append(row)
                say(f"[{rep}] {vt} {scene} 値={row.get('checks')} 相づち={row.get('acks')} "
                    f"録音のターン={[(x['played'][-1][:8], x['ai_spoke']) for x in row.get('play_turns', [])]} "
                    f"録音のあと={[a['said'][:20] for a in row.get('after_play', [])]} {row.get('error', '')}")
                save_partial("G-37", {"model": args.model, "summary": summarize(rows), "rows": rows})
                await asyncio.sleep(args.pause)
    summary = summarize(rows)
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "variant": FAST, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--scene", choices=SCENES, action="append")
    p.add_argument("--listen", type=Path, help="この結果から、聞き比べの声を作る（キーを使わない）")
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    if args.listen:
        for m in make_listening(args.listen, args.out):
            print(json.dumps(m, ensure_ascii=False))
        return
    if args.dry_run:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
        return
    save_result("G-37", asyncio.run(run(args)))
    drop_partial("G-37")


if __name__ == "__main__":
    main()
