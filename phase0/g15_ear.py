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

from common import KeyRing, base_parser, drop_partial, read_wav_pcm16_mono, require_python, save_partial, save_result, say
from ears import EAR_KINDS, GEMINI_EAR_MODEL, open_ear
from jtext import compare_reading
from listen import differences
from make_g14_set import OUT, load_all
from stitch_gap import callee_speech


def live_calls() -> list[dict]:
    """G-14 の記録の、1回の通話ごとの、最初の答えの声・会話AIの聞き取りの文字・AIの最初の報告（op・condition）・正解（want。無い記録は None）・質問。"""
    out = []
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
                out.append({"wav": c["wav"], "heard": r["turns"][0]["heard"], "op": r.get("op"), "condition": r.get("condition"),
                            "want": r.get("want"), "question": r.get("question")})
    return out


def live_heard() -> dict:
    """G-14 の記録から、声ごとの会話AIの聞き取りの文字（何回ぶんでも）。"""
    out = defaultdict(list)
    for c in live_calls():
        out[c["wav"]].append(c["heard"])
    return out


def ok(script: str, text: str) -> bool:
    return bool(text) and not differences(script, text)


def carried_over(prev_script: str, script: str, ear: str | None) -> bool:
    """この発話の別の耳の文字の頭に、前の発話の終わりの言葉が入っているか（読みで比べる）。前の発話の文字が遅れて届いた・耳が前の発話の
    終わりを確定し損ねた、のどちらでも起きる。⛔ 2026-10-17 の見直しで見つけた：Soniox「です。 いや、これからなんです。」（前の「してないです。」の
    「です。」）・Gemini の耳「はい。予約済みです。」（前の「はい。」が5秒の待ちを過ぎて届いた）。それまでは「混ざりは0回」と書いていた。"""
    p, s, e = compare_reading(prev_script), compare_reading(script), compare_reading(ear or "")
    return any(e.startswith(p[-k:]) and e[k:].startswith(s[:2]) and not s.startswith(p[-k:]) for k in range(1, len(p) + 1))


def tally(rows: list[dict]) -> dict:
    done = [r for r in rows if "ear" in r]
    pairs = [(a, b) for a, b in zip(done, done[1:]) if (a.get("rep"), a["voice"]) == (b.get("rep"), b["voice"])]   # 同じつなぎの中の、続く2つの発話
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
         "前の発話の文字が混ざった": [b["wav"] for a, b in pairs if carried_over(a["text"], b["text"], b.get("ear"))],
         "誤り": [r["wav"] for r in rows if "error" in r]}
    say("数え：", json.dumps(s, ensure_ascii=False))
    return s


async def run(args) -> dict:
    """耳をつなぎ（通話の中と同じ形＝ears.open_ear）、答えの声を1本ずつ流して、その発話の確定した文字を待つ。相手役の声ごとに1本つなぎ直す
    （1本の通話に、同じ相手の答えが何回か続く形。1本のつなぎは2分ほど）。
    ⚠️ 2026-10-17 の1回目・2回目（transcribe35 の 165本・声のあとに5秒の無音の形）は、声ごとに耳へつなぎ直す形（listen.hear）で測った。
       3回目からは、どの耳も同じこの形で測る。
    ⛔ 2026-10-17：165本を1本のつなぎのまま流すと、Gemini の耳がつなぎの上限（約10分）で切られ、最後の10本の文字が届かなかった（試験の作りの誤り。
       実際の通話は1本ごとに短い）⇒ 相手役の声ごとにつなぎ直す。誤りの記録も、その発話のあいだに起きたものだけを残す。"""
    # Gemini の耳はキーを使う：上限・止められたキーは飛ばし、その声のつなぎを初めからやり直す（common.KeyRing.acall。通話の中の試験と同じ）。
    # ⛔ 2026-10-17 の見直しで見つけた：1つのキーだけでつないでいて、止められたキーに当たると、そこで試験が止まった
    ring = KeyRing(args) if args.ear == "transcribe35" else None
    live = live_heard()
    # 声のファイルがある台詞だけ（作るのに失敗した台詞は、台帳に行だけがある）
    items = [m for m in load_all() if m.get("wav") and (not args.voice or m["voice"] in args.voice) and (not args.only or any(m["id"].startswith(o) for o in args.only))]
    voices = list(dict.fromkeys(m["voice"] for m in items))
    rows = []
    loop = asyncio.get_running_loop()

    async def one_voice(client, rep: int, voice: str) -> list[dict]:
        out = []
        async with open_ear(args.ear, client, args.env) as ear:
            for m in (m for m in items if m["voice"] == voice):
                wav = m["wav"]
                pcm, rate = read_wav_pcm16_mono(OUT.parent / wav)
                row = {"rep": rep, "ear_kind": args.ear, "voice": m["voice"], "id": m["id"], "wav": wav, "text": m["text"]}
                tail = callee_speech(wav)[2]
                speech_end = loop.time() + len(pcm) / 2 / rate - tail  # 声の終わり（声のあとの無音を引く）
                seen = len(ear.errors)
                await ear.feed(pcm, rate, tail)
                text = await ear.wait(speech_end + args.wait - loop.time())
                row.update(ear=text, ear_ok=ok(m["text"], text or ""), diffs=differences(m["text"], text or ""),
                           first_s=round(ear.first_at - speech_end, 3) if ear.first_at else None)
                row["after_s"] = row["first_s"]
                if ear.errors[seen:]:
                    row["error"] = ear.errors[seen:]
                heard = live.get(wav, [])
                row.update(live_n=len(heard), live_ok=sum(1 for h in heard if ok(m["text"], h)),
                           live_wrong=sorted({h for h in heard if not ok(m["text"], h)}))
                out.append(row)
                say(f"[{rep}] {args.ear} {m['voice']} {m['id']}「{m['text']}」別の耳＝{row.get('ear')!r}（{'台本どおり' if row.get('ear_ok') else '違う'}・"
                    f"{row.get('first_s')}秒）会話AI＝{row['live_ok']}/{row['live_n']} {row['live_wrong'][:2]} {row.get('error', '')}")
                save_partial(f"G-15{args.tag}", {"rows": rows + out})
                await asyncio.sleep(args.gap)  # 次の発話までの間（その間も、耳には無音が流れる）
        return out

    for rep in range(args.repeat):
        for voice in voices:
            if ring is None:
                rows += await one_voice(None, rep, voice)
            else:
                got, _ = await ring.acall(GEMINI_EAR_MODEL, lambda client: one_voice(client, rep, voice))
                rows += got
    return {"ear_kind": args.ear, "rows": rows, "summary": tally(rows)}


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
    p.add_argument("--ear", default="transcribe35", choices=EAR_KINDS, help="別の耳の名前（ears.EAR_KINDS）")
    p.add_argument("--wait", type=float, default=5.0, help="話し終わりから、文字を待つ秒（速さを測るので、通話の中の待ち 2.0秒より長くする）")
    p.add_argument("--gap", type=float, default=1.0, help="次の声を流すまでの秒")
    p.add_argument("--merge", nargs="+", help="保存した結果をまとめて数え直す（問い合わせはしない）")
    args = p.parse_args()
    if args.merge:
        save_result("G-15", merge(args.merge))
        return
    save_result(f"G-15{args.tag}", asyncio.run(run(args)))
    drop_partial(f"G-15{args.tag}")


if __name__ == "__main__":
    main()
