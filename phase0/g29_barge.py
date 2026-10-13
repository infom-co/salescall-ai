"""G-29：録音の名乗りを流している間に相手が話したとき、見張り（barge.py。DESIGN.md 6-13 の案）が決めた止め方で、会話がつながるか。

G-22 は、録音を文の切れ目でぴったり止めた形だけを試した。ここでは、相手が話し始めた時刻から、見張りが「流し続ける／止める・どこまで届いたか」
を決め、その結果のとおりに会話AIへ知らせて続ける。G-22 に無かった形：
- C2 社名の文の途中で話し始めた（途中で止めた文は「まだ」）
- C4 最初の質問の文の途中で答え始めた（最初の質問は「まだ」のまま、答えが届く）
- C5 最初の質問の文の途中の短い「はい。」（流し切ったあと、答えとして届く）
- C1 名乗りの途中の相づち「はい。」（捨てる。相手はそのあと最初の質問に答える）
採点は G-22 と同じもの（g22_opening.judge）を使う。⚠️ 録音の文の長さは、AIの話す速さ（G-22：1秒あたり 7.66音）から見積もった。
⚠️ 本物の電話の雑音・こだま・本物の人の相づちの長さは、ここでは確かめられない（R・H）。
"""
from __future__ import annotations

import asyncio
import json

from barge import part_lengths, watch
from common import (base_parser, describe_error, drop_partial, make_client, pick_key, read_wav_pcm16_mono, require_python,
                    save_partial, save_result, say)
from convo import AUDIO_DIR, run_call
from g22_opening import BUSINESS, NAME, PARTS, W, judge
from instruction import VOICE_TYPES
from listen import hear_feeds
from make_callee_samples import verified

MORAE_PER_S = 7.66  # G-22 の AIの話す速さの中央
SHORT_S = 0.6       # 相づちとみなす長さ（設定の値の案。⚠️ 本物の人の声で決める）
LENGTHS = part_lengths(PARTS, MORAE_PER_S)
STARTS = [sum(d for _, d in LENGTHS[:i]) for i in range(len(LENGTHS))]


def mid(i: int) -> float:
    """i 番目の文の、まん中の時刻。"""
    return STARTS[i] + LENGTHS[i][1] / 2


# first：録音の間に相手が言う声と、話し始める時刻。after：そのあとの相手の声。
SCENES = {
    "C1_名乗りの途中の相づち": {"first": ("backchannel", mid(1)), "after": [W("yes_reserved"), W("datetime_pm"), W("affirm")],
                        "need_any": [["日時", "日にち", "お日にち"]], "q1_answered_first": True, "no_reintro": True},
    "C2_社名の途中で話し始めた": {"first": ("barge_who", mid(1)), "after": [W("yes_reserved")],
                         "need_any": [NAME, ["佐藤"], ["予約"]], "then_q2": True},
    "C4_最初の質問の途中で答えた": {"first": ("barge_yes2", STARTS[3] + 0.2), "after": [W("datetime_pm"), W("affirm")],
                          "need_any": [["日時", "日にち", "お日にち"]], "q1_answered_first": True, "no_reintro": True},
    "C5_最初の質問の途中の相づち": {"first": ("backchannel", mid(3)), "after": [W("datetime_pm"), W("affirm")],
                          "need_any": [["日時", "日にち", "お日にち"]], "q1_answered_first": True, "no_reintro": True},
}


def plan(scene: dict, voice: str = "Puck") -> dict:
    """見張りの決めたこと（止めたか・どこまで届いたか・相手の声を会話AIへ送るか）と、会話AIへ渡す相手の声の並び。"""
    name, onset = scene["first"]
    pcm, rate = read_wav_pcm16_mono(AUDIO_DIR.parent / "callee" / f"{voice}_{name}.wav")
    d = watch(LENGTHS, pcm, rate, onset, short_s=SHORT_S)
    send_first = d.stopped or d.send_from_s is not None  # 捨てた相づちは送らない
    return {"decision": {"stopped": d.stopped, "stop_at_s": d.stop_at_s, "done": d.done, "rest": d.rest,
                         "send_from_s": d.send_from_s, "dropped": d.dropped},
            "barge": (d.done, d.rest) if d.stopped else None,
            "callee": ([W(name)] if send_first else []) + scene["after"]}


async def run(args) -> dict:
    plans = {k: plan(v) for k, v in SCENES.items()}
    say("録音の文の長さ（見積もり）：", [(t[:8], round(x, 2)) for t, x in LENGTHS])
    for k, p in plans.items():
        say(k, json.dumps(p["decision"], ensure_ascii=False))
    if args.dry_run:
        return {"dry_run": True, "plans": plans}
    client = make_client(pick_key(args))
    rows = []
    for rep in range(args.repeat):
        for name, scene in SCENES.items():
            p = plans[name]
            if not all(verified(c["wav"]) for c in p["callee"]):
                rows.append({"rep": rep, "scene": name, "skipped": "相手役の声が確かめに通っていない"})
                continue
            for vt in args.type or VOICE_TYPES:
                try:
                    call = await run_call(client, args.model, BUSINESS, vt, p["callee"], opening_mode="recorded",
                                          barge_in=p["barge"], save_audio="all", audio_label=f"G29_{name}_{rep}")
                    if call["turns"] and call["turns"][0].get("audio_file"):
                        pcm, prate = read_wav_pcm16_mono(AUDIO_DIR / call["turns"][0]["audio_file"])
                        call["first_heard"] = await hear_feeds(client, pcm, prate)
                    row = {"rep": rep, "scene": name, "type": vt, "plan": p["decision"], "checks": judge(scene, call), **call}
                except Exception as exc:
                    row = {"rep": rep, "scene": name, "type": vt, "error": describe_error(exc)}
                rows.append(row)
                c = row.get("checks", {})
                say(f"[{rep}] {name} {vt} 合格={c.get('passed')} Q1={c.get('q1_yes')} 次の質問={c.get('asked_q2')} "
                    f"最初の返事={(c.get('first_said') or '')[:40]} {row.get('error', '')}")
                save_partial("G-29", {"model": args.model, "rows": rows})
                await asyncio.sleep(args.pause)
    by = {}
    for r in rows:
        if "checks" in r:
            by.setdefault(r["scene"], []).append(r["checks"]["passed"])
    summary = {"runs": sum(len(v) for v in by.values()), "passed": sum(sum(v) for v in by.values()), "by_scene": by}
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"model": args.model, "short_s": SHORT_S, "morae_per_s": MORAE_PER_S, "lengths": LENGTHS, "plans": plans,
            "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--pause", type=float, default=4.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    args = p.parse_args()
    save_result("G-29", asyncio.run(run(args)))
    drop_partial("G-29")


if __name__ == "__main__":
    main()
