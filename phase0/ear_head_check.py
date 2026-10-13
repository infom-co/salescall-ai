"""別の耳の確かめ：1ターンの声を、前に無音を置かずに聞かせると、頭の音を聞き落とすか（2026-10-13）。

疑い：会話の試験（convo）は、AIの1ターンの声を、前に無音を置かずに別の耳へ送っていた。その結果、604ターン中 67ターンで
ターンの頭が食い違った（「ご来店」が「来店」、言い直しの「15日」が「4日」「いつもの」など）。一方、通話の声をつなげて
聞かせた試験（G-19。途中のターンの前には無音がある）では、言い直しの「15日」は崩れていない。
確かめ方：G-19 で保存した通話の声（results/g19_audio/）を、ターンの間の無音（ちょうど0の 0.6秒）でターンごとに切り分け、
同じ1ターンの声を「無音なし（lead_s=0）」と「無音あり（listen.LEAD_S）」で別の耳に聞かせ、言うつもりの言葉
（AI自身の文字）と比べる。どちらを先に聞かせるかは、1つずつ入れ替える。
見ること：ターンの頭（読みの最初の4音）の食い違いの数。声は同じなので、差が出れば、それは別の耳の聞き方の違い。
--opening：AIが名乗る形の通話の、最初のターン（名乗り）だけを「無音あり」で聞き直す（6-13 の前提＝名乗りの崩れの割合の測り直し）。
--convo 結果のファイル：会話の試験（convo）で残したターンの声（results/convo_audio/）を聞き比べる。
  ⇒ 2026-10-13：G-19 の声では、無音なしでも19本中1本しか食い違わなかった。会話の試験との違いは、AIが操作（答えの報告）の
     返事を受けてから話し始めるターンが多いこと。⇒ 声の頭そのものが欠けているのか（AIの誤り）、別の耳の聞き落としなのかを分ける。
     声の頭の無音の長さ（lead_silence_s）も記録する。
"""
from __future__ import annotations

import asyncio
import glob
import json

import numpy as np

from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, read_wav_pcm16_mono
from listen import LEAD_S, differences, hear

AUDIO = HERE / "results" / "g19_audio"
CONVO_AUDIO = HERE / "results" / "convo_audio"
HEAD = 4  # ターンの頭＝読みの最初の何音か


def split_turns(pcm16: bytes, rate: int) -> list[bytes]:
    """G-19 の通話の声を、ターンの間に入れた無音（ちょうど0が 0.6秒）で切り分ける。"""
    x = np.frombuffer(pcm16, dtype="<i2")
    z = np.concatenate([[0], (x == 0).astype(np.int8), [0]])
    edges = np.flatnonzero(np.diff(z))
    gaps = [(s, e) for s, e in zip(edges[0::2], edges[1::2]) if e - s >= int(rate * 0.6)]
    out, pos = [], 0
    for s, e in gaps:
        out.append(x[pos:s].tobytes())
        pos = e
    out.append(x[pos:].tobytes())
    return out


def clips(first_only_ai_opening: bool) -> list[dict]:
    """（通話、ターンの番号、言うつもりの言葉、声）の一覧。言うつもりの言葉が欠けたターンは除く。"""
    out = []
    for f in sorted(glob.glob(str(HERE / "results" / "G-19_*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        for r in d["rows"]:
            if not r.get("wav"):
                continue
            pcm, rate = read_wav_pcm16_mono(AUDIO / r["wav"])
            voiced = [t for t in r["turns"] if t.get("check", {}).get("voiced_s", 0) > 0]
            parts = split_turns(pcm, rate)
            if len(parts) != len(voiced):
                continue
            for n, (t, p) in enumerate(zip(voiced, parts)):
                if not t.get("intended"):
                    continue
                # 名乗りの形の印が無い通話は、印を付ける前の試験＝すべてAIが名乗る形
                if first_only_ai_opening and not (n == 0 and (r.get("opening_mode") or "ai") == "ai"):
                    continue
                out.append({"call": r["id"], "type": r["type"], "opening_mode": r.get("opening_mode") or "ai", "turn": n,
                            "intended": t["intended"], "pcm": p, "rate": rate})
    return out


def convo_clips(path: str) -> list[dict]:
    """会話の試験の結果から、声を残したターンの一覧（言うつもりの言葉と、その時点の別の耳の文字つき）。"""
    out = []
    for r in json.load(open(path, encoding="utf-8"))["rows"]:
        for t in r.get("turns", []) or []:
            if t.get("audio_file") and t.get("intended"):
                pcm, rate = read_wav_pcm16_mono(CONVO_AUDIO / t["audio_file"])
                out.append({"call": t["audio_file"], "type": r.get("type"), "turn": t["index"], "intended": t["intended"],
                            "after_tool": bool(t.get("tool_events")), "ear_in_test": t.get("ear"), "pcm": pcm, "rate": rate})
    return out


def lead_silence_s(pcm16: bytes, rate: int) -> float:
    """声の頭の、声が無い長さ（秒）。playback と同じ見分け方（20ミリ秒ごとの大きさ）。"""
    from playback import FRAME_S, speech_frames

    v = speech_frames(pcm16, rate)
    return round(float(np.argmax(v)) * FRAME_S, 3) if v.any() else round(len(v) * FRAME_S, 3)


def head_diffs(intended: str, heard: str) -> list[dict]:
    return [d for d in differences(intended, heard) if d["at"] < HEAD]


def pick(items: list[dict], n: int) -> list[dict]:
    """途中のターン（頭の音を見る的）を、いろいろな書き出しから偏らずに選ぶ（書き出しの2文字ごとに順に1つずつ）。"""
    mid = [c for c in items if c["turn"] > 0]
    groups: dict[str, list[dict]] = {}
    for c in mid:
        groups.setdefault(c["intended"][:2], []).append(c)
    out = []
    while len(out) < n and any(groups.values()):
        for k in sorted(groups):
            if groups[k] and len(out) < n:
                out.append(groups[k].pop(0))
    return out


async def run(args) -> dict:
    if args.convo:
        cs = convo_clips(args.convo)  # 操作の返事のあとのターンと、そうでないターンを、交互に選ぶ
        a, b = [c for c in cs if c["after_tool"]], [c for c in cs if not c["after_tool"]]
        targets = [x for pair in zip(a, b) for x in pair][:args.n] or cs[:args.n]
        if len(targets) < args.n:
            targets += [c for c in cs if c not in targets][:args.n - len(targets)]
        leads = [0.0, LEAD_S]
    elif args.opening:
        targets = clips(first_only_ai_opening=True)
        leads = [LEAD_S]
    else:
        targets = pick(clips(first_only_ai_opening=False), args.n)
        leads = [0.0, LEAD_S]
    say(f"聞かせるターン：{len(targets)} 本 × 無音 {leads}")
    if args.dry_run:
        return {"dry_run": True, "targets": [{k: v for k, v in c.items() if k != "pcm"} for c in targets]}
    client = make_client(pick_key(args))
    rows = []
    for i, c in enumerate(targets):
        row = {k: v for k, v in c.items() if k != "pcm"}
        row["lead_silence_s"] = lead_silence_s(c["pcm"], c["rate"])
        order = leads if i % 2 == 0 else list(reversed(leads))  # どちらを先に聞かせるかを入れ替える
        for lead in order:
            try:
                heard = await hear(client, c["pcm"], c["rate"], "transcribe35", lead_s=lead)
                row[f"lead_{lead}"] = {"heard": heard, "head": head_diffs(c["intended"], heard),
                                       "all": len(differences(c["intended"], heard))}
            except Exception as exc:
                row[f"lead_{lead}"] = {"error": describe_error(exc)}
            await asyncio.sleep(args.pause)
        rows.append(row)
        say(f"{i + 1}/{len(targets)} {c['intended'][:20]} | " +
            " | ".join(f"無音{lead}：{row[f'lead_{lead}'].get('heard', '')[:20]} 頭={len(row[f'lead_{lead}'].get('head', [])) if 'head' in row[f'lead_{lead}'] else '誤り'}"
                       for lead in leads))
    summary = {}
    for lead in leads:
        got = [r[f"lead_{lead}"] for r in rows if "head" in r[f"lead_{lead}"]]
        summary[f"lead_{lead}"] = {"heard": len(got), "head_mismatch": sum(1 for g in got if g["head"]),
                                   "any_mismatch": sum(1 for g in got if g["all"])}
    say("まとめ：", json.dumps(summary, ensure_ascii=False))
    return {"head_morae": HEAD, "leads": leads, "summary": summary, "rows": rows}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--n", type=int, default=30, help="聞かせる途中のターンの数")
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--opening", action="store_true", help="AIが名乗る形の通話の名乗りだけを、無音ありで聞き直す")
    p.add_argument("--convo", help="会話の試験の結果のファイル（声を残したターンを聞き比べる）")
    args = p.parse_args()
    save_result("EAR-HEAD" + ("-convo" if args.convo else "-opening" if args.opening else ""), asyncio.run(run(args)))


if __name__ == "__main__":
    main()
