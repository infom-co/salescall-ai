"""G-44：決めた名前の読みが、本当に声になるか。会話AIに名前をどう渡すと、読み違えないかを比べる（Intent.md 6-8 の ⏳）。

きっかけ：G-42 で、取込の名前の読みを人を介さずに決められた（読みの誤り0）。ただし、読みを会話AI・録音へどう伝えるかは決めていない
（いまは漢字のまま「」でくくるだけ）。決めた読みが声にならなければ、決めた意味が無い。
- 材料：読み違えやすい名前12（文の手がかりで決まる読み・めずらしい読み・よくある読みでない方の人名）と、ふつうの名前2（比べる元）。
- 渡し方（どれも録音を作るときと同じ形で、1文を読ませる。声の型 S4。録音の確かめは使わない）：
  K＝漢字のまま「」（いまの形）／KN＝漢字のまま「」＋指示に「名前の読み：「日本橋」＝にっぽんばし」／
  P＝「日本橋（にっぽんばし）」と文に読みを添える／KATA＝「ニッポンバシ」とカタカナで書く／HIRA＝「にっぽんばし」とひらがなで書く。
- 確かめ方：読ませた声を、文字のモデル2つ（3.8 Flash・3.7 Flash）に「ひらがなだけで書き起こして」と頼み、正しい読みが入っているか、
  名前を2回言っていないか（P）を数える。2つの書き起こしが食い違う回は「判定できない」として別に数える。
  ⚠️ 書き起こしのモデルも読み違えうる（例：「にっぽんばし」と言ったのに「にほんばし」と書く）。KATA・HIRA は読みが文に書いてあるので、
  ここで正しい読みが出ない割合が、書き起こしの側の誤りの目安になる。
Live（会話のモデル）は1日の回数の上限が無い。書き起こしは Flash を使うので、まとめて数回で済ませる。1つのキーで1回ずつ順番に。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from common import HERE, base_parser, call_with_retry, describe_error, make_client, pick_key, require_python, \
    save_partial, save_result, say, write_wav
from g23_names import find_models
from instruction import voice_style
from jtext import compare_reading
from live_util import build_config, collect_turn, send_text

# (名前の表記, 正しい読み, よくある読み違え, 文（{n} に名前を入れる）)
NAMES = [
    ("日本橋", "にっぽんばし", "にほんばし", "先日は大阪の{n}店にお越しいただきありがとうございました。"),
    ("兵庫県三田市", "ひょうごけんさんだし", "みたし", "{n}の会場でも同じ内容で行います。"),
    ("放出", "はなてん", "ほうしゅつ", "大阪の{n}にも事業所がございます。"),
    ("大和ハウス工業", "だいわはうすこうぎょう", "やまとはうす", "すでに{n}様にもご導入いただいております。"),
    ("東海林", "しょうじ", "とうかいりん", "講師の{n}が担当いたします。"),
    ("小鳥遊", "たかなし", "ことりあそび", "営業部の{n}と申します。"),
    ("枚方", "ひらかた", "まいかた", "{n}校で体験授業を行っております。"),
    ("御徒町", "おかちまち", "ごとちょう", "{n}の事務所で点検をしております。"),
    ("角田", "つのだ", "かくた", "担当の{n}が改めてご連絡いたします。"),       # よくある読みでない方（台本に読みがながある、などで決まった読み）
    ("中島", "なかしま", "なかじま", "みらい不動産の{n}と申します。"),
    ("山崎", "やまさき", "やまざき", "ひまわり学院の{n}でございます。"),
    ("新垣", "しんがき", "あらがき", "講師は{n}が務めます。"),
    ("佐藤", "さとう", "", "株式会社アオバ物流の{n}と申します。"),            # ふつうの名前（比べる元）
    ("アオバ物流", "あおばぶつりゅう", "", "株式会社{n}の佐藤と申します。"),
]
# 英字・カタカナの名前（2026-10-16 追加。CL-1 が「まだ測っていない」と報告したもの）。読みが決まった名前は、くくる処理がひらがなに置き換える
# （instruction.quote_in_text）が、⚠️ 英字・カタカナは漢字のような読み違えが無いかもしれず、ひらがなにすると、かえって崩れる（伸ばす音・
# 外来語の調子）おそれもある ⇒ もとの書き方（K）・カタカナ（KATA）・ひらがな（HIRA）で比べる。よくある読み違え＝英字を1文字ずつ読む・ローマ字読み
NAMES_ALNUM = [
    ("ＡＢＣホールディングス", "えーびーしーほーるでぃんぐす", "あぶく", "すでに{n}様にもご導入いただいております。"),
    ("スマートリンクPro", "すまーとりんくぷろ", "ぴーあーるおー", "新しいサービスの{n}のご案内です。"),
    ("NTT東日本", "えぬてぃーてぃーひがしにほん", "えぬてぃーてぃーとうにほん", "回線は{n}をお使いでしょうか。"),
    ("KDDI", "けーでぃーでぃーあい", "かでぃ", "{n}の回線と同じ料金です。"),
    ("freee", "ふりー", "ふりーいーいー", "会計ソフトの{n}と連携しております。"),
    ("Sansan", "さんさん", "えすえーえぬ", "名刺管理の{n}をお使いの会社様が多いです。"),
    ("LIXIL", "りくしる", "えるあいえっくす", "{n}様の製品を扱っております。"),
    ("au", "えーゆー", "あう", "携帯は{n}をお使いでしょうか。"),
    ("IKEA", "いけあ", "あいけあ", "{n}の近くに事務所がございます。"),
    ("コージーコーナー", "こーじーこーなー", "", "{n}様のお近くです。"),       # ふつうのカタカナ（比べる元）
]
METHODS = ["K", "KN", "P", "KATA", "HIRA"]
NAME_SETS = {"kanji": (NAMES, METHODS), "alnum": (NAMES_ALNUM, ["K", "KATA", "HIRA"])}
HOW = "電話で、お客様に丁寧に話すように"
OUT = HERE / "results" / "reading"


def kata(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def text_for(method: str, surface: str, reading: str, sentence: str) -> tuple[str, str]:
    """(読ませる文, 指示に足す一文)。名前はどれも「」でくくる（くくるのは instruction.quote と同じ形）。"""
    shown = {"K": surface, "KN": surface, "P": f"{surface}（{reading}）", "KATA": kata(reading), "HIRA": reading}[method]
    note = f"名前の読み：「{surface}」＝{reading}。" if method == "KN" else ""
    return sentence.format(n=f"「{shown}」"), note


async def read_one(client, args, text: str, note: str) -> tuple[bytes, int]:
    voice, style = voice_style("S4")
    inst = f"{style}\nあなたは音声の材料を作る係です。渡された言葉だけを、{HOW}、そのまま読み上げます。言葉を足しません。{note}"
    async with client.aio.live.connect(model=args.model, config=build_config(voice=voice, system_instruction=inst)) as s:
        t = time.monotonic()
        await send_text(s, f"読み上げる言葉：{text}")
        turn = await collect_turn(s, started_at=t, timeout_s=60)
    return bytes(turn.audio), turn.audio_rate or 24000


TRANSCRIBE = ("次の音声はどれも、電話で話している日本語の1文です。音声ごとに、聞こえたとおりの音を、ひらがなだけで書き起こしてください。"
              "漢字・カタカナ・英字は使いません。名前も、聞こえた音のとおりに書きます（知っている読みに直さない）。")


def transcribe(client, model: str, clips: list[Path]) -> list[str]:
    from google.genai import types

    parts = [types.Part.from_text(text=TRANSCRIBE)]
    for n, c in enumerate(clips, 1):
        parts += [types.Part.from_text(text=f"音声{n}："), types.Part.from_bytes(data=c.read_bytes(), mime_type="audio/wav")]
    schema = types.Schema(type=types.Type.OBJECT, properties={"items": types.Schema(type=types.Type.ARRAY, items=types.Schema(
        type=types.Type.OBJECT, properties={"no": types.Schema(type=types.Type.INTEGER), "kana": types.Schema(type=types.Type.STRING)},
        required=["no", "kana"]))}, required=["items"])
    r = client.models.generate_content(model=model, contents=[types.Content(role="user", parts=parts)],
                                       config=types.GenerateContentConfig(response_mime_type="application/json",
                                                                          response_schema=schema, temperature=0))
    got = {x.get("no"): x.get("kana", "") for x in json.loads(r.text).get("items", [])}
    return [got.get(n, "") for n in range(1, len(clips) + 1)]


def h(s: str) -> str:
    return compare_reading(s or "").replace(" ", "").replace("ー", "")


def judge(kana: str, reading: str, wrong: str, method: str) -> str:
    k, r = h(kana), h(reading)
    if not k:
        return "書き起こし無し"
    if method == "P" and (k.count(r) >= 2 or (r in k and wrong and h(wrong) in k)):
        return "2回言った"  # 「とうかいりんしょうじ」のように、漢字の読みと添えた読みの両方を言った回も
    if r in k:
        return "正しい"
    if wrong and h(wrong) in k:
        return "読み違え"
    return "ほかの読み"


async def record(args) -> list[dict]:
    client = make_client(pick_key(args))
    OUT.mkdir(parents=True, exist_ok=True)
    names, methods = NAME_SETS[args.set]
    tag = "" if args.set == "kanji" else f"_{args.set}"
    rows = []
    for rep in range(args.reps):
        for i, (surface, reading, wrong, sentence) in enumerate(names):
            for m in methods:
                text, note = text_for(m, surface, reading, sentence)
                row = {"set": args.set, "name": surface, "reading": reading, "wrong": wrong, "method": m, "rep": rep, "text": text,
                       "note": note}
                try:
                    pcm, rate = await read_one(client, args, text, note)
                    if pcm:
                        path = OUT / f"G44{tag}_{i:02d}_{m}_{rep}.wav"
                        write_wav(path, pcm, rate)
                        row["wav"] = path.name
                except Exception as exc:
                    row["error"] = describe_error(exc)
                rows.append(row)
                say(f"{surface} {m} #{rep}：{'録れた' if row.get('wav') else '録れない'}")
                save_partial("G-44", {"rows": rows})
                await asyncio.sleep(args.pause)
    return rows


# 書き起こしに使える、取込の比べ（g23_names.MODELS）に無いモデル（表示名で引く）。⚠️ 2026-10-16：3.8 Flash が混んでいて答えが返らず、
# 3.7 Flash・3.6 Flash は1日の上限に達していたため足した。相手役の名前の声（G-46）を、5本とも台本どおりの読みで書き起こした
EXTRA_VERIFIERS = {"3 Flash": "Gemini 3 Flash Preview"}


def verify(args, rows: list[dict]) -> dict:
    client = make_client(pick_key(args))
    ids, _ = find_models(client)
    want = {k: d for k, d in EXTRA_VERIFIERS.items() if k in (args.verifier or [])}
    if want:
        for m in client.models.list():
            for k, d in want.items():
                if (getattr(m, "display_name", "") or "").strip() == d:
                    ids[k] = m.name.split("/")[-1]
    clips = [r for r in rows if r.get("wav")]
    for key in (args.verifier or ["3.8 Flash", "3.7 Flash"]):
        if key not in ids:
            continue
        for start in range(0, len(clips), args.batch):
            part = clips[start:start + args.batch]
            try:
                kana, _ = call_with_retry(lambda: transcribe(client, ids[key], [OUT / r["wav"] for r in part]))
                for r, k in zip(part, kana):
                    r.setdefault("kana", {})[key] = k
            except Exception as exc:  # ⛔ 上限（429）はやり直さない。そのモデルの書き起こしはここまで
                say(key, "書き起こしが止まりました：", describe_error(exc).get("message", "")[:200])
                break
            save_partial("G-44", {"rows": rows})
            time.sleep(args.pause_flash)
    return tally(rows)


def tally(rows: list[dict], use: list[str] | None = None) -> dict:
    """書き起こしのモデルが1つなら、その判定。2つ以上なら、全部が同じときだけ判定し、食い違えば「判定できない」。
    use＝数えに使う書き起こしのモデル（⛔ 3.5 Flash Lite は使えなかった：漢字が混じり、別の音声と同じ文を返した＝G-44）。"""
    out = {}
    for r in rows:
        ks = {k: v for k, v in (r.get("kana") or {}).items() if not use or k in use}
        js = {k: judge(v, r["reading"], r["wrong"], r["method"]) for k, v in ks.items()}
        r["judge"] = js
        vals = set(js.values())
        r["verdict"] = vals.pop() if len(vals) == 1 else ("判定できない" if js else "書き起こし無し")
        out.setdefault(r["method"], {}).setdefault(r["verdict"], 0)
        out[r["method"]][r["verdict"]] += 1
    return out


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--set", choices=sorted(NAME_SETS), default="kanji", help="名前の組（kanji＝読み違えやすい漢字の名前・alnum＝英字とカタカナの名前）")
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--pause-flash", type=float, default=15.0)
    p.add_argument("--batch", type=int, default=70)
    p.add_argument("--verify-only", help="録った結果（G-44 の保存した結果）を書き起こして数える")
    p.add_argument("--record-only", action="store_true", help="録るだけ（書き起こしは --verify-only であとで）")
    p.add_argument("--verifier", action="append", help="書き起こしに使うモデル（既定は 3.8 Flash と 3.7 Flash）")
    p.add_argument("--tally-only", help="保存した結果を、--verifier のモデルの書き起こしだけで数え直す（問い合わせはしない）")
    args = p.parse_args()
    if args.tally_only:
        rows = json.loads(Path(args.tally_only).read_text(encoding="utf-8"))["rows"]
        summary = tally(rows, args.verifier)
        for m, v in summary.items():
            say(m, v)
        save_result("G-44", {"names": NAMES, "methods": METHODS, "rows": rows, "summary": summary, "tallied_from": args.tally_only,
                             "verifiers": args.verifier})
        return
    if args.verify_only:
        rows = json.loads(Path(args.verify_only).read_text(encoding="utf-8"))["rows"]
    else:
        rows = asyncio.run(record(args))
    if args.record_only:
        save_result("G-44", {"set": args.set, "names": NAME_SETS[args.set][0], "methods": NAME_SETS[args.set][1], "rows": rows,
                             "recorded_only": True})
        return
    summary = verify(args, rows)
    for m, v in summary.items():
        say(m, v)
    save_result("G-44", {"names": NAMES, "methods": METHODS, "rows": rows, "summary": summary})


if __name__ == "__main__":
    main()
