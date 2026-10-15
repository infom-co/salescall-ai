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
--candidates N：確かめに通った録り方を N 本、候補として candidates/ に残す（使っている録音は置き換えない）。人が聞いて選ぶ。
--adopt：人が選んだ候補（例 S4_shouchi_2）を、使う録音にする。
  ✅ 2026-10-15 ユーザー：「S4 の『承知しました、』02＞01＞04＞03（03のみ最後が途切れている）」
  ⚠️ 2026-10-15 ユーザー：「S4 の最初の『承知しました。』が外国人が話すようなイントネーション」＝読みは合っていて、耳の確かめでは落とせない
⚠️ 本番で使うなら、決まった発話の録音（6-13）と同じく、人（運営者）が聞いて承認する。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
import time

from barge import voice_runs, voiced
from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from instruction import VOICE_TYPES, voice_style
from jtext import compare_reading
from listen import hear_feeds
from live_util import build_config, collect_turn, send_text
from phrases import RECORDED
from voice_features import read_wav

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
       # ⚠️ 2026-10-15 ユーザー：「『お忙しいところ、お時間をいただきまして、ありがとうございました。それでは、失礼いたします。』に
       #    なって、少しテンポが悪い」（文の途中に 0.2〜0.4秒の無音が入っていた）⇒ 区切らずに読ませ、文の途中の長い無音は確かめで落とす
       "closing": "電話の最後に、お客様に感謝を伝えるように、前向きで明るく、心を込めて、落ち着いた調子で。言葉の途中で区切らずに、なめらかに、ひと続きで",
       "wait": "電話で、お客様に少し待っていただくことを、申し訳なく思いながら、柔らかく丁寧にお願いするように",
       "question": "電話で、お客様に丁寧に、柔らかくお聞きするように"}
DEFAULT_HOW = "電話で、相手の話に相づちを打つように、短く"
MAX_S = {"hai": 1.0, "shouchi": 1.4, "arigatou": 1.6, "shitsurei": 1.6, "moushiwake": 1.6, "lead_soredeha": 1.0,
         "lead_tsuzukimashite": 1.2, "lead_deha": 0.8, "closing": 8.0, "wait": 3.5, "question": 5.0}


END_SILENCE_S = 0.1  # 声のあとに、これだけの無音が無ければ、声の途中で切れた録音として落とす
# ⛔ 2026-10-15：S4 の「承知しました、」の候補3は、声が鳴っている途中で終わっていた（声のあとの無音 0秒・最後の20ミリ秒が -18dB）。
#    耳の確かめは「承知しました。」と聞いて通した。ほかの録音は、どれも声のあとに 0.2秒以上の無音がある。


def ends_in_silence(pcm: bytes, rate: int) -> bool:
    """声のあとに無音があるか（声の途中で切れていないか）。"""
    runs = voice_runs(voiced(pcm, rate), 0.05)
    return bool(runs) and len(pcm) / 2 / rate - runs[-1][1] >= END_SILENCE_S


LONG_PAUSE_S = 0.25  # 文の途中に入れてよい無音の長さの上限（これより長い無音は、文の切れ目「。」「？」・頭のつなぎの言葉のあとだけ）


def long_pauses_ok(text: str, pcm: bytes, rate: int) -> bool:
    """文の途中に長い無音が無いか（✅ 2026-10-15 ユーザー：句読点のような間・テンポの悪さ）。文の切れ目と、頭のつなぎの言葉のあとの間だけは許す。"""
    from phrases import LEAD_IN
    runs = voice_runs(voiced(pcm, rate), 0.05)
    long = sum(1 for a, b in zip(runs, runs[1:]) if b[0] - a[1] >= LONG_PAUSE_S)
    allowed = len(re.findall(r"[。？?！!]", text.rstrip("。？?！!、")))
    allowed += any(text.startswith(w) for forms in LEAD_IN.values() for w in forms)
    return long <= allowed


def heard_ok(text: str, heard: dict) -> bool:
    """2通りの聞き方の両方で、読みが合うか（句読点・言い終わりの違いは比べない）。"""
    want = compare_reading(text)
    return bool(heard) and all(v is not None and compare_reading(v) == want for v in heard.values())


def words(script: bool) -> tuple[dict, Path]:
    """作る録音の表 {中の名前: (言葉, 読み方と長さの上限の鍵)} と、置き場所。script＝依頼主の単位（試験の業務設定の質問の文）。"""
    if script:
        from flowctl import spoken_question
        from g22_opening import BUSINESS
        from phrases import LEAD_RECORDED
        table = {}
        for q in BUSINESS.questions:  # つなぎの言葉と質問を1本で録音する（順番で決まるつなぎの言葉。flowctl.spoken_question）
            text, lead = spoken_question(BUSINESS.questions, q.id)
            table[f"{q.id}_{LEAD_RECORDED[lead]}" if lead else q.id] = (text, "question")
        return table, SCRIPT_OUT
    return {aid: (text, aid) for aid, text in RECORDED.items()}, OUT


def adopt(names: list[str], script: bool) -> dict:
    """人が選んだ候補を、使う録音にする（候補の確かめの結果も、そのまま台帳に移す）。"""
    table, out = words(script)
    index_path = out / "index.json"
    made = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    # 候補の置き場所：この単位の candidates/ と、G-38 の録音（results/voice_match/。✅ 2026-10-15 ユーザーが 06＝A5 を選んだ）
    cands = {r["wav"][:-4]: (out / "candidates", r) for r in json.loads((out / "candidates" / "index.json").read_text(encoding="utf-8"))
             if r.get("wav")} if (out / "candidates" / "index.json").exists() else {}
    for g in sorted(HERE.glob("results/G-38_*.json")):
        cands.update({r["wav"][:-4]: (HERE / "results" / "voice_match", r) for r in json.loads(g.read_text(encoding="utf-8"))["rows"]
                      if r.get("wav") and r.get("ok") and r["id"] in table})
    done = []
    for name in names:
        where, row = cands[name]
        row = {k: v for k, v in row.items() if k not in ("features", "st_from_ref", "cond", "take")}
        assert row["text"] == table[row["id"]][0], (name, row["text"])  # 文が変わったあとの古い候補は使わない
        pcm, rate = read_wav(where / row["wav"])
        assert ends_in_silence(pcm, rate), name  # 終わりの切れた候補は使わない（候補3）
        path = out / f"{row['type']}_{row['id']}.wav"
        write_wav(path, pcm, rate)
        row.update({"wav": path.name, "chosen_from": name, "chosen_by": "人"})
        made = [m for m in made if (m["type"], m["id"]) != (row["type"], row["id"])] + [row]
        done.append(row)
    index_path.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"adopted": done}


async def run(args) -> dict:
    table, out = words(args.script)
    ids = args.id or list(table)
    assert set(ids) <= set(table), set(ids) - set(table)
    index_path = out / "index.json"
    made = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    # 作り済みでも、文が変わった録音は作り直す（⛔ 2026-10-15：読点を外した質問の文が、作り済みとして飛ばされていた）
    have = {(m["type"], m["id"]) for m in made if m.get("ok") and m.get("text") == table.get(m["id"], (None,))[0]}
    todo = [(vt, aid) for vt in (args.type or VOICE_TYPES) for aid in ids if (vt, aid) not in have or aid in (args.redo or [])]
    if args.candidates:  # 候補は、作り済みかどうかに関わらず、名指しした録音を作る
        todo = [(vt, aid) for vt in (args.type or VOICE_TYPES) for aid in ids]
    if args.dry_run:
        return {"dry_run": True, "todo": todo, "candidates": args.candidates, "words": {a: table[a][0] for a in ids}}
    client = make_client(pick_key(args))
    out.mkdir(parents=True, exist_ok=True)
    if args.candidates:
        return await make_candidates(client, args, table, todo, out / "candidates")
    for vt, aid in todo:
        row = None
        for n in range(args.tries):
            row, pcm, rate = await take(client, args, vt, aid, *table[aid])
            row["tries"] = n + 1
            if row["ok"]:
                path = out / f"{vt}_{aid}.wav"
                write_wav(path, pcm, rate)
                row["wav"] = path.name
                break
        made = [m for m in made if (m["type"], m["id"]) != (vt, aid)] + [row]
        index_path.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")  # 1本ごとに残す（途中で止まっても失わない）
        await asyncio.sleep(args.pause)
    return {"model": args.model, "made": made, "missing": [(m["type"], m["id"]) for m in made if not m.get("ok")]}


async def take(client, args, vt: str, aid: str, text: str, how: str, voice_note: str = "") -> tuple[dict, bytes, int]:
    """1回読ませて、確かめる（両方の耳で読みが合う・声だけの長さが上限以内・文の途中に長い無音が無い・声の途中で切れていない）。
    voice_note＝声そのものの指示（G-38 で比べる。空なら足さない）。"""
    voice, style = voice_style(vt)
    try:
        inst = (f"{style}{voice_note}\nあなたは音声の材料を作る係です。渡された言葉だけを、{HOW.get(how, DEFAULT_HOW)}、そのまま読み上げます。"
                "言葉を足しません。")
        async with client.aio.live.connect(model=args.model, config=build_config(voice=voice, system_instruction=inst)) as s:
            t = time.monotonic()
            await send_text(s, f"読み上げる言葉：{text}")
            turn = await collect_turn(s, started_at=t, timeout_s=60)
        pcm, rate = bytes(turn.audio), turn.audio_rate or 24000
        seconds = speech_seconds(pcm, rate) if pcm else 0.0
        heard = await hear_feeds(client, pcm, rate) if pcm else {}
        ok = (bool(pcm) and heard_ok(text, heard) and seconds <= MAX_S[how] and long_pauses_ok(text, pcm, rate)
              and ends_in_silence(pcm, rate))
        say(f"{vt} {text}：聞こえた {heard} 長さ {seconds:.2f}秒 合格={ok}")
        return {"type": vt, "voice": voice, "id": aid, "text": text, "heard": heard, "ok": ok, "seconds": round(seconds, 2)}, pcm, rate
    except Exception as exc:
        return {"type": vt, "id": aid, "text": text, "ok": False, "error": describe_error(exc)}, b"", 0


async def make_candidates(client, args, table: dict, todo: list, out: Path) -> dict:
    """確かめに通った録り方を args.candidates 本ずつ残す（{声の型}_{中の名前}_{番号}.wav）。録る回数は 本数×tries まで。"""
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for vt, aid in todo:
        for old in out.glob(f"{vt}_{aid}_*.wav"):  # 前の候補は残さない（どれを聞いて選んだか、取り違えないため）
            old.unlink()
        kept, n = 0, 0
        while kept < args.candidates and n < args.candidates * args.tries:
            n += 1
            row, pcm, rate = await take(client, args, vt, aid, *table[aid])
            row["try"] = n
            if row["ok"]:
                kept += 1
                path = out / f"{vt}_{aid}_{kept}.wav"
                write_wav(path, pcm, rate)
                row["wav"] = path.name
            rows.append(row)
            (out / "index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            await asyncio.sleep(args.pause)
    return {"model": args.model, "candidates": rows,
            "kept": {f"{vt}_{aid}": sum(1 for r in rows if r.get("wav") and (r["type"], r["id"]) == (vt, aid)) for vt, aid in todo}}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--tries", type=int, default=3)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--script", action="store_true", help="依頼主の単位の録音（試験の業務設定の質問の文）を作る")
    p.add_argument("--id", action="append", help="作る録音（中の名前。質問なら Q2 など）。無ければ全部")
    p.add_argument("--redo", action="append", help="作ってある録音でも作り直す（名指ししたものだけ）")
    p.add_argument("--candidates", type=int, default=0, help="確かめに通った録り方を、この本数だけ候補として残す（人が聞いて選ぶ）")
    p.add_argument("--adopt", action="append", help="人が選んだ候補（例 S4_shouchi_2）を使う録音にする（キーを使わない）")
    args = p.parse_args()
    if args.adopt:
        print(json.dumps(adopt(args.adopt, args.script), ensure_ascii=False))
        return
    if args.dry_run:  # 下見は保存しない（G-36 と同じ）
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
        return
    save_result("ACK-CLIPS", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
