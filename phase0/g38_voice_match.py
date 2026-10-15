"""G-38：同じ声の名前（Sadachbia）でも録るたびに変わる声を、人が選んだ1本（基準の声）にそろえられるか（DESIGN.md 6-13）。

✅ 2026-10-15 ユーザー（S4 の「承知しました、」の候補を聞いて）：「02＞01＞04＞03。誠実で若々しい声でとてもよい。ただし、続く声が、
   おっさんのしゃがれた声で、年齢層や人物が違うように聞こえる。02の声に他が合わせられると一番良い」
比べること：話し方の指示に、声そのものの一文（若々しく誠実な声・しゃがれた声にしない）を足すと、
- 録るたびの声の高さのばらつきが小さくなり、基準の声（候補2）の高さに近づくか
- 低く沈む区間（しゃがれ・きしみで出やすい。voice_features.low_ratio）が減るか
- 確かめ（両方の耳で読みが合う・長さ・途中の無音・終わりの切れ）に通る割合が落ちないか
⚠️ 数は人の耳の代わりにならない。最後は聞き比べで決める（--listen で、基準の声のあとにつないだ声を作る）。
"""
from __future__ import annotations

import asyncio
import json
import statistics as st
from pathlib import Path

from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from make_ack_clips import take, words
from voice_features import features, read_wav, semitones

OUT = HERE / "results" / "voice_match"
REFERENCE = HERE / "results" / "ack" / "candidates" / "S4_shouchi_2.wav"  # ユーザーが選んだ基準の声（S4）
NOTES = {
    "A": "",
    "B": "\n声：誠実で若々しい、20代の男性会社員の声にしてください。澄んだ明るい声で話し、しゃがれた声・かすれた声・低くこもった声・"
         "年配の人のような声にはしないでください。",
}
# 比べる録音（ユーザーが「しゃがれた声」と聞いた質問と、同じ形の質問・受けの言葉）
IDS = (("script", "Q3_lead_tsuzukimashite"), ("script", "Q2_lead_soredeha"), ("ack", "arigatou"))


def table() -> dict:
    t = {}
    for unit, aid in IDS:
        tab, _ = words(unit == "script")
        t[aid] = tab[aid]
    return t


async def run(args) -> dict:
    tab = table()
    ref = features(*read_wav(REFERENCE))
    if args.dry_run:
        return {"dry_run": True, "reference": ref, "words": {a: tab[a][0] for _, a in IDS}, "notes": NOTES}
    client = make_client(pick_key(args))
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for k in range(args.takes):
        for _, aid in IDS:
            for cond in args.cond or NOTES:  # 指示の違いを交互に録る（時間による揺れを、どちらかに偏らせない）
                row, pcm, rate = await take(client, args, args.type, aid, *tab[aid], voice_note=NOTES[cond])
                row.update({"cond": cond, "take": k + 1})
                if pcm:
                    path = OUT / f"{args.type}_{aid}_{cond}{k + 1}.wav"
                    write_wav(path, pcm, rate)
                    row["wav"] = path.name
                    f = features(pcm, rate)
                    row["features"] = f
                    if f:
                        row["st_from_ref"] = round(semitones(f["pitch_hz"], ref["pitch_hz"]), 2)
                say(f"{cond}{k + 1} {aid} 合格={row['ok']} 高さの差={row.get('st_from_ref')} 低く沈む={(row.get('features') or {}).get('low_ratio')}")
                rows.append(row)
                await asyncio.sleep(args.pause)
    return {"model": args.model, "type": args.type, "reference": ref, "notes": NOTES, "summary": summarize(rows), "rows": rows}


def summarize(rows: list[dict]) -> dict:
    out = {}
    for _, aid in IDS:
        for cond in NOTES:
            rs = [r for r in rows if r["id"] == aid and r["cond"] == cond]
            fs = [r for r in rs if r.get("features")]
            if not rs:
                continue
            d = [r["st_from_ref"] for r in fs]
            out[f"{aid}_{cond}"] = {
                "takes": len(rs), "passed": sum(r["ok"] for r in rs),
                "st_from_ref_median": round(st.median(d), 2) if d else None,
                "st_from_ref_abs_median": round(st.median(abs(x) for x in d), 2) if d else None,
                "st_spread": round(max(d) - min(d), 2) if len(d) > 1 else None,
                "low_ratio_median": round(st.median(r["features"]["low_ratio"] for r in fs), 3) if fs else None}
    return out


def make_listening(result: Path, out: Path) -> list[dict]:
    """基準の声（候補2の「承知しました、」）→ 0.6秒 → 録った声、をつないだ聞き比べ。指示の違いが分からない名前にする（対応は key.json）。"""
    import random
    d = json.loads(result.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    ref_pcm, rate = read_wav(REFERENCE)
    gap = b"\x00\x00" * int(rate * 0.6)
    rows = [r for r in d["rows"] if r.get("ok") and r["id"] == "Q3_lead_tsuzukimashite"]
    random.Random(15).shuffle(rows)
    key = []
    for n, r in enumerate(rows, 1):
        pcm, r_rate = read_wav(OUT / r["wav"])
        assert r_rate == rate
        name = f"{n:02d}_承知しました→続きまして.wav"
        write_wav(out / name, ref_pcm + gap + pcm, rate)
        key.append({"file": name, "cond": r["cond"], "take": r["take"], "st_from_ref": r.get("st_from_ref"),
                    "low_ratio": (r.get("features") or {}).get("low_ratio")})
    (out / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    return key


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--type", default="S4")
    p.add_argument("--takes", type=int, default=6)
    p.add_argument("--cond", choices=sorted(NOTES), action="append")
    p.add_argument("--pause", type=float, default=2.0)
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
    save_result("G-38", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
