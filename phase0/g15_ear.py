"""G-15：別の耳（会話AIとは別の音声認識）が、答えの声を正しく聞けるか・どれだけ早く文字にできるか（DESIGN.md 6-10 の (5)。2026-10-17）。

なぜ：はい／いいえ・選択肢の答えは、その場で言い直して確かめない（9-5）。会話AIの聞き取りが誤ると、そのまま依頼主へ渡る。G-14 の残りの誤りは、
どれも聞き取りそのものの誤り（「してないです」が4回とも「捨てないです」・雑音の「はい、先週しました」が「編集しました」「転送しました」）。
⚠️ この作業環境からは、ほかの会社の音声認識（クラウド・PC の中で動くもの）のモデルを取りに行けない（取得先が通信の中継所で止められている）。
   ⇒ 比べられるのは Gemini の文字起こし専用のモデル（gemini-3.5-transcribe-live。これまで「別の耳」として使ってきたもの）だけ。
- 材料：G-14 の試験集（6つの声・電話の音質。雑音を混ぜた分を含む）。続きの返事（f1・f2）も入れる。
- 見ること：(1) 別の耳の文字が、台本と同じ読みか（`listen.differences`。句読点・書き方の違いは見ない）
  (2) 会話AIの聞き取り（G-14 の記録の、その声への会話AIの文字）が台本と違った声を、別の耳は正しく聞いたか。逆に、会話AIが正しく聞いた声を、
  別の耳が誤ったか（通話の中で使うと、要らない確かめが増える）
  (3) 声の終わりから、別の耳の最初・最後の文字が届くまでの秒（通話の中で使えるかの見積もり。AIの報告は声の終わりから約0.8秒で届く＝6-15）。
"""
from __future__ import annotations

import asyncio
import glob
import json
from collections import defaultdict

from common import base_parser, describe_error, drop_partial, make_client, pick_key, read_wav_pcm16_mono, require_python, save_partial, save_result, say
from listen import differences, hear
from make_g14_set import OUT, load_all
from stitch_gap import callee_speech


def live_heard() -> dict:
    """G-14 の記録から、声ごとの会話AIの聞き取りの文字（何回ぶんでも）。"""
    out = defaultdict(list)
    for p in sorted(glob.glob(str(OUT.parent.parent / "G-14*.json"))):
        if "TALLY" in p or "partial" in p:
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for r in d.get("rows") or []:
            if not isinstance(r, dict) or not r.get("turns"):
                continue
            c = r["turns"][0].get("callee")
            if isinstance(c, dict) and r["turns"][0].get("heard"):
                out[c["wav"]].append(r["turns"][0]["heard"])
    return out


def ok(script: str, text: str) -> bool:
    return bool(text) and not differences(script, text)


def tally(rows: list[dict]) -> dict:
    done = [r for r in rows if "ear" in r]
    lat = sorted(r["after_s"] for r in done if r.get("after_s") is not None)
    first = sorted(r["first_s"] for r in done if r.get("first_s") is not None)
    live_wrong = [r for r in done if r["live_n"] and r["live_ok"] < r["live_n"]]
    s = {"声": len(done),
         "別の耳が台本どおり": sum(1 for r in done if r["ear_ok"]),
         "会話AIが誤ったことのある声": len(live_wrong),
         "そのうち別の耳は台本どおり": sum(1 for r in live_wrong if r["ear_ok"]),
         "会話AIがいつも正しかった声で、別の耳が誤った": sum(1 for r in done if r["live_n"] and r["live_ok"] == r["live_n"] and not r["ear_ok"]),
         "最後の文字が届くまで（中央・遅いほうから1割・最大）": [lat[len(lat) // 2], lat[int(len(lat) * 0.9)], lat[-1]] if lat else None,
         "最初の文字が届くまで（中央・遅いほうから1割・最大）": [first[len(first) // 2], first[int(len(first) * 0.9)], first[-1]] if first else None,
         "誤り": [r["wav"] for r in rows if "error" in r]}
    say("数え：", json.dumps(s, ensure_ascii=False))
    return s


async def run(args) -> dict:
    client = make_client(pick_key(args))
    live = live_heard()
    # 声のファイルがある台詞だけ（作るのに失敗した台詞は、台帳に行だけがある）
    items = [m for m in load_all() if m.get("wav") and (not args.voice or m["voice"] in args.voice) and (not args.only or any(m["id"].startswith(o) for o in args.only))]
    rows = []
    for rep in range(args.repeat):
        for m in items:
            wav = m["wav"]
            pcm, rate = read_wav_pcm16_mono(OUT.parent / wav)
            tail = callee_speech(wav)[2]  # 声のあとの無音（声の終わりから数えるため）
            row = {"rep": rep, "voice": m["voice"], "id": m["id"], "wav": wav, "text": m["text"]}
            try:
                timing: dict = {}
                text = await hear(client, pcm, rate, "transcribe35", timing=timing, tail_s=args.tail)
                row.update(ear=text, ear_ok=ok(m["text"], text), diffs=differences(m["text"], text),
                           first_s=round(timing["first_s"] + tail, 3) if "first_s" in timing else None,
                           after_s=round(timing["after_s"] + tail, 3) if "after_s" in timing else None)
            except Exception as exc:
                row["error"] = describe_error(exc)
            heard = live.get(wav, [])
            row.update(live_n=len(heard), live_ok=sum(1 for h in heard if ok(m["text"], h)), live_wrong=sorted({h for h in heard if not ok(m["text"], h)}))
            rows.append(row)
            say(f"[{rep}] {m['voice']} {m['id']}「{m['text']}」別の耳＝{row.get('ear')!r}（{'台本どおり' if row.get('ear_ok') else '違う'}・"
                f"最後まで{row.get('after_s')}秒）会話AI＝{row['live_ok']}/{row['live_n']} {row['live_wrong'][:2]} {row.get('error', '')}")
            save_partial(f"G-15{args.tag}", {"rows": rows})
    return {"rows": rows, "summary": tally(rows)}


def merge(paths: list[str]) -> dict:
    rows = [r for p in paths for r in json.loads(open(p, encoding="utf-8").read())["rows"]]
    return {"merged_from": paths, "rows": rows, "summary": tally(rows)}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--voice", action="append")
    p.add_argument("--only", action="append")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--tag", default="")
    # 声のあとに流す無音（秒）。そのあとに送り終わりを伝える。長くすると、別の耳が自分で話し終わりに気づいて文字を出すまでの秒が測れる
    p.add_argument("--tail", type=float, default=1.5)
    p.add_argument("--merge", nargs="+", help="保存した結果をまとめて数え直す（問い合わせはしない）")
    args = p.parse_args()
    if args.merge:
        save_result("G-15", merge(args.merge))
        return
    save_result(f"G-15{args.tag}", asyncio.run(run(args)))
    drop_partial(f"G-15{args.tag}")


if __name__ == "__main__":
    main()
