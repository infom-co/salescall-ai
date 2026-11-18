"""G-42：取込の名前の洗い出しと読みを、人を介さずに決められるかを測る（names.py。G-23 のやり直し。2026-10-16 ユーザー指示）。

✅ ユーザー：「人は基本的に介在しない」「（名前の読みと聞くことを）人を介さない形で進めてください」
- 材料：8つの業務の台本（SCRIPTS）。正解は前もって決めた。読みの難しい名前（小鳥遊・放出・御徒町・枚方）・文の中の手がかりで読みが
  決まる名前（大阪の日本橋＝にっぽんばし・兵庫県の三田市＝さんだし・大和ハウス工業＝だいわ）・台本に読みがなが書いてある名前
  （東海林（しょうじ））・⛔文からは読みが決まらない名前（中島・山崎・角田・河野・新垣・場所の手がかりの無い三田）・名前の無い文を混ぜた。
- 正解の形：alts＝名前の書き方（どれか1つに当たればよい）、readings＝正しい読み、fixed＝文から読みが1つに決まるか。
- 数え方（名前ごと）：
  見落とし／⛔読みを誤って決めた（fixed の名前に違う読み・fixed でない名前に1つの読みを決めた）／読みが正しい／
  「決まらない」とした（fixed でない名前なら正しい。fixed の名前なら、決められたのに決めなかった＝安全な取りこぼし）。
  余分（名前でないものを名前にした）も数える。⛔「」以外の文字が変わる回は、プログラムがくくる形（instruction.quote_in_text）では起きない
  ことを、試験（test_offline.QuoteInTextTest）で確かめてある。
- 比べる：単語分け（by_tokenizer）／文字のモデル（3.8・3.7・3.6 Flash は2回、3.5 Flash Lite は2回）／その組み合わせ。
1日の上限（Flash は各20回）に収まるよう、1回の問い合わせで全部の台本を見させる。上限に当たったら次のキーで続ける（common.KeyRing）。
"""
from __future__ import annotations

import itertools
import json
import time

from common import base_parser, call_with_retry, describe_error, drop_partial, make_client, pick_key, require_python, save_partial, \
    save_result, say
from g23_names import find_models
from jtext import compare_reading
from names import by_model_scripts, by_tokenizer, decide, given_reading, norm_surface


def N(alts, readings, fixed=True):
    return {"alts": list(alts), "readings": list(readings), "fixed": fixed}


SCRIPTS = [
    ("飲食の予約の確認", ["お忙しいところ恐れ入ります。株式会社アオバ物流の佐藤と申します。",
                         "先日は大阪の日本橋店にお越しいただきありがとうございました。",
                         "本日はご予約の内容の確認でお電話いたしました。"],
     [N(["アオバ物流"], ["あおばぶつりゅう"]), N(["佐藤"], ["さとう"]), N(["大阪"], ["おおさか"]),
      N(["日本橋", "日本橋店"], ["にっぽんばし", "にっぽんばしてん"])]),          # 大阪の日本橋＝にっぽんばし（文の手がかりで決まる）
    ("勤怠サービスの法人営業", ["株式会社ＡＢＣホールディングス営業部の小鳥遊と申します。",
                               "弊社のクラウド勤怠サービス『スマートリンクPro』についてご案内させていただきたくお電話いたしました。",
                               "すでに大和ハウス工業様や三井住友銀行様にもご導入いただいております。",
                               "ご担当の長谷川様はいらっしゃいますでしょうか。"],
     [N(["ABCホールディングス"], ["えーびーしーほーるでぃんぐす", "えいびいしいほーるでぃんぐす"]), N(["小鳥遊"], ["たかなし"]),
      N(["スマートリンクPro"], ["すまーとりんくぷろ"]),
      N(["大和ハウス工業", "大和ハウス"], ["だいわはうすこうぎょう", "だいわはうす"]),    # よく知られた会社名＝だいわ（「やまと」ではない）
      N(["三井住友銀行", "三井住友"], ["みついすみともぎんこう", "みついすみとも"]), N(["長谷川"], ["はせがわ"])]),
    ("不動産のご案内", ["みらい不動産の中島と申します。",
                       "川崎市中原区の物件についてご連絡いたしました。",
                       "上野原の土地もあわせてご紹介できます。",
                       "三田の本社から担当の角田が改めてご連絡いたします。"],
     [N(["みらい不動産"], ["みらいふどうさん"]), N(["中島"], ["なかじま", "なかしま"], fixed=False),
      N(["川崎市中原区", "川崎市", "川崎"], ["かわさきしなかはらく", "かわさきし", "かわさき"]),
      N(["川崎市中原区", "中原区", "中原"], ["かわさきしなかはらく", "なかはらく", "なかはら"]),
      N(["上野原"], ["うえのはら"]),                                                     # ⚠️ 中の「上野」をくくらない
      N(["三田"], ["みた", "さんだ"], fixed=False),                                      # 場所の手がかりが無い
      N(["角田"], ["かくた", "つのだ", "すみだ", "かどた"], fixed=False)]),
    ("学習塾の体験授業", ["ひまわり学院の山崎でございます。",
                         "吹田校と枚方校で体験授業を行っております。",
                         "講師の東海林（しょうじ）が担当いたします。",
                         "新しい英語コース「エーゴ・ステップ」のご案内です。"],
     [N(["ひまわり学院"], ["ひまわりがくいん"]), N(["山崎"], ["やまざき", "やまさき"], fixed=False),
      N(["吹田", "吹田校"], ["すいた", "すいたこう"]), N(["枚方", "枚方校"], ["ひらかた", "ひらかたこう"]),
      N(["東海林"], ["しょうじ"]),                                                       # 台本に読みがなが書いてある
      N(["エーゴ・ステップ", "エーゴステップ"], ["えーごすてっぷ"])]),
    ("設備の点検", ["有限会社グリーン・テック設備の渡辺と申します。",
                   "御徒町と神田の事務所で点検をしております。",
                   "河野様から、こちらの番号をお伺いしました。",
                   "点検の日程を確認させていただけますでしょうか。"],
     [N(["グリーン・テック設備", "グリーンテック設備"], ["ぐりーんてっくせつび"]), N(["渡辺"], ["わたなべ"]),
      N(["御徒町"], ["おかちまち"]), N(["神田"], ["かんだ"]), N(["河野"], ["こうの", "かわの"], fixed=False)]),
    ("光回線のご案内", ["NTT東日本の代理店、ネクストリンク株式会社の高橋です。",
                       "光回線「光プラスＷｉｄｅ」のご案内でお電話いたしました。",
                       "品川と京橋に窓口がございます。"],
     [N(["NTT東日本"], ["えぬてぃーてぃーひがしにほん", "えぬてぃてぃひがしにほん", "えぬてぃーてぃーひがしにっぽん"]),
      N(["ネクストリンク"], ["ねくすとりんく"]), N(["高橋"], ["たかはし"]),
      N(["光プラスWide"], ["ひかりぷらすわいど"]), N(["品川"], ["しながわ"]), N(["京橋"], ["きょうばし"])]),
    ("セミナーのご案内", ["渋谷ヒカリエで開催するセミナーのご案内です。",
                         "講師は株式会社たくみ工房の新垣が務めます。",
                         "兵庫県三田市の会場でも同じ内容で行います。",
                         "お申し込みは来週の金曜日までにお願いいたします。"],
     [N(["渋谷ヒカリエ", "ヒカリエ"], ["しぶやひかりえ", "ひかりえ"]), N(["たくみ工房"], ["たくみこうぼう"]),
      N(["新垣"], ["あらがき", "しんがき", "にいがき"], fixed=False),
      # ⚠️ 2026-10-16 正解を直した：「兵庫県三田市」を1つの名前として出すのも正しい（「川崎市中原区」と同じ扱い）。はじめは分けた形だけを
      #    正解にしていたので、モデルが1つにまとめて正しく「ひょうごけんさんだし」と読んだのに、見落とし2・余分1と数えていた
      N(["兵庫県三田市", "兵庫県", "兵庫"], ["ひょうごけんさんだし", "ひょうごけん", "ひょうご"]),
      N(["兵庫県三田市", "三田市", "三田"], ["ひょうごけんさんだし", "さんだし", "さんだ"])]),  # 兵庫県の三田市＝さんだし（文の手がかりで決まる）
    ("訪問介護のご相談", ["ケアサポートつばさの菅原と申します。",
                         "札幌市内でご利用の林様のご紹介でお電話いたしました。",
                         "訪問は看護師の一ノ瀬が伺います。",
                         "大阪の放出にも事業所がございます。"],
     [N(["ケアサポートつばさ"], ["けあさぽーとつばさ"]), N(["菅原"], ["すがわら"]), N(["札幌市", "札幌"], ["さっぽろし", "さっぽろ"]),
      N(["林"], ["はやし"]), N(["一ノ瀬"], ["いちのせ"]), N(["大阪"], ["おおさか"]), N(["放出"], ["はなてん"])]),
]
MODELS = {"3.8 Flash": 2, "3.7 Flash": 2, "3.6 Flash": 2, "3.5 Flash Lite": 2}


def rd(s: str) -> str:
    return compare_reading(s or "").replace(" ", "")


def text_of(sentences: list[str]) -> str:
    return "".join(sentences)


# ---------------------------------------------------------------- 決め方（どれを採るかは、この数で決める）
def _same_reading(a: str, b: str) -> bool:
    return bool(a) and rd(a) == rd(b)


def decided(text: str, tok: list[dict], runs: list[list[dict]], need: str, reading_rule: str) -> list[dict]:
    """名前の一覧と読みを決める。need＝名前に入れる決まり（"両方のモデル"・"どれかのモデル"・"モデルと単語分け"・"単語分けだけ"）、
    reading_rule＝読みを決める決まり（"モデル1つ"・"モデル2つが同じ"・"モデル2つと単語分けが同じ"）。
    読みは、台本に読みがなが書いてあればそれ（given_reading）。決められなければ reading を空（読みが決まらない）にする。"""
    found: dict[str, list] = {}
    for src, rows in [("tok", tok)] + [(f"m{i}", r) for i, r in enumerate(runs)]:
        for x in rows:
            key = norm_surface(x.get("surface", ""))
            if key:
                found.setdefault(key, []).append((src, x))
    out = []
    for key, hits in found.items():
        srcs = {s for s, _ in hits}
        models = [x for s, x in hits if s != "tok"]
        msrc = {s for s in srcs if s != "tok"}
        ok = {"両方のモデル": len(msrc) >= 2, "どれかのモデル": len(msrc) >= 1,
              "モデルと単語分け": len(msrc) >= 2 or (len(msrc) >= 1 and "tok" in srcs), "単語分けだけ": "tok" in srcs}[need]
        if not ok:
            continue
        surface = (models or [hits[0][1]])[0].get("surface", key)
        given = given_reading(text, surface)
        reading = given
        if not given:
            sure = [x for x in models if not x.get("uncertain")]
            if reading_rule == "モデル1つ":
                reading = sure[0]["reading"] if models and sure and sure[0] is models[0] else ""
            else:
                both = len(msrc) >= 2 and len(sure) == len(models) and all(_same_reading(models[0]["reading"], x["reading"])
                                                                         for x in models[1:])
                reading = models[0]["reading"] if both else ""
                if reading and reading_rule == "モデル2つと単語分けが同じ":
                    reading = reading if rd(reading) == rd(surface) else ""  # 単語分けの読み（rd）と同じときだけ
            if need == "単語分けだけ":
                reading = next((x["reading"] for s, x in hits if s == "tok"), "")
        out.append({"surface": surface, "reading": reading})
    return out


def score(gold: list[dict], got: list[dict]) -> dict:
    s = {"名前": len(gold), "見落とし": 0, "読みが正しい": 0, "⛔読みを誤って決めた": 0, "決まらないとした_正しい": 0,
         "決まらないとした_決められた": 0, "余分": 0, "誤りの例": [], "余分の例": [], "見落としの例": []}
    used = set()
    for g in gold:
        alts = {norm_surface(a) for a in g["alts"]}
        hit = next((i for i, x in enumerate(got) if norm_surface(x["surface"]) in alts), None)
        if hit is None:
            s["見落とし"] += 1
            s["見落としの例"].append(g["alts"][0])
            continue
        used.add(hit)
        r = got[hit]["reading"]
        if not r:
            s["決まらないとした_正しい" if not g["fixed"] else "決まらないとした_決められた"] += 1
        elif g["fixed"] and rd(r) in {rd(x) for x in g["readings"]}:
            s["読みが正しい"] += 1
        else:
            s["⛔読みを誤って決めた"] += 1
            s["誤りの例"].append(f"{got[hit]['surface']}＝{r}（正解：{'・'.join(g['readings'])}{'' if g['fixed'] else '＝決まらない'}）")
    for i, x in enumerate(got):
        if i not in used and not any(norm_surface(x["surface"]) in {norm_surface(a) for a in g["alts"]} for g in gold):
            s["余分"] += 1
            s["余分の例"].append(x["surface"])
    return s


def total(rows: list[dict]) -> dict:
    keys = ["名前", "見落とし", "読みが正しい", "⛔読みを誤って決めた", "決まらないとした_正しい", "決まらないとした_決められた", "余分"]
    t = {k: sum(r[k] for r in rows) for k in keys}
    for k in ("誤りの例", "余分の例", "見落としの例"):
        t[k] = [e for r in rows for e in r[k]]
    return t


def compare(raw: dict) -> dict:
    tok = [by_tokenizer(text_of(ss)) for _, ss, _ in SCRIPTS]
    runs = {k: v["rows"] for k, v in raw.items() if "rows" in v}
    out = {"単語分けだけ": total([score(g, decided(text_of(ss), t, [], "単語分けだけ", "")) for (_, ss, g), t in zip(SCRIPTS, tok)])}
    for k, v in runs.items():
        out[f"{k}だけ"] = total([score(g, decided(text_of(ss), [], [r], "どれかのモデル", "モデル1つ"))
                               for (_, ss, g), r in zip(SCRIPTS, v)])
    for a, b in itertools.combinations(runs, 2):  # 採った決め方（names.decide そのもの）
        out[f"決め方（names.decide）{a}×{b}"] = total(
            [score(g, [{"surface": x["surface"], "reading": x["reading"]} for x in decide(text_of(ss), [ra, rb])])
             for (_, ss, g), ra, rb in zip(SCRIPTS, runs[a], runs[b])])
    for a, b in itertools.combinations(runs, 2):
        for need in ("両方のモデル", "モデルと単語分け", "どれかのモデル"):
            for rule in ("モデル2つが同じ", "モデル2つと単語分けが同じ"):
                out[f"{a}×{b}／名前＝{need}／読み＝{rule}"] = total(
                    [score(g, decided(text_of(ss), t, [ra, rb], need, rule))
                     for (_, ss, g), t, ra, rb in zip(SCRIPTS, tok, runs[a], runs[b])])
    return out


def show(cmp: dict, only_head: bool = False) -> None:
    for k, v in cmp.items():
        say(k, f"見落とし {v['見落とし']}／⛔誤り {v['⛔読みを誤って決めた']}／正しい {v['読みが正しい']}／"
               f"決まらない(正) {v['決まらないとした_正しい']}／決まらない(取りこぼし) {v['決まらないとした_決められた']}／余分 {v['余分']}",
            "" if only_head else v["誤りの例"])


def run(args) -> dict:
    out = {"scripts": [{"business": b, "sentences": ss, "gold": g} for b, ss, g in SCRIPTS]}
    if args.dry_run:
        out["compare"] = compare({})
        show(out["compare"])
        return out
    client = make_client(pick_key(args))
    ids, flash_names = find_models(client)
    out["model_ids"], out["flash_display_names"] = ids, flash_names
    say("モデルID：", ids)
    plan = [(k, rep) for k, n in MODELS.items() if k in ids and (not args.model_key or k in args.model_key) for rep in range(n)]
    texts = [text_of(ss) for _, ss, _ in SCRIPTS]
    raw = {}
    for k, rep in sorted(plan, key=lambda x: x[1]):
        name = f"{k}#{rep}"
        try:
            got, retries = call_with_retry(lambda: by_model_scripts(client, ids[k], texts))
            raw[name] = {"rows": got, "retries": retries}
            t = total([score(g, decided(tx, [], [r], "どれかのモデル", "モデル1つ")) for (_, _, g), tx, r in zip(SCRIPTS, texts, got)])
            say(name, f"見落とし {t['見落とし']}／⛔誤り {t['⛔読みを誤って決めた']}／正しい {t['読みが正しい']}／余分 {t['余分']}", t["誤りの例"])
        except Exception as exc:
            raw[name] = {"error": describe_error(exc)}
            msg = raw[name]["error"].get("message", "")
            say(name, "誤りで止まりました：", msg[:200])
            if "RESOURCE_EXHAUSTED" in msg or "429" in str(raw[name]["error"].get("code", "")):
                say("⛔ 上限に当たったので、ここで止めます（やり直さない）")
                break
        save_partial("G-42", {"raw": raw})
        time.sleep(args.pause)
    out["raw"] = raw
    out["compare"] = compare(raw)
    show(out["compare"], only_head=True)
    return out


def rescore(path: str) -> dict:
    d = json.loads(open(path, encoding="utf-8").read())
    out = {"rescored_from": path, "scripts": [{"business": b, "sentences": ss, "gold": g} for b, ss, g in SCRIPTS],
           "model_ids": d.get("model_ids"), "raw": d["raw"], "compare": compare(d["raw"])}
    show(out["compare"], only_head=True)
    return out


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--pause", type=float, default=15.0)
    p.add_argument("--model-key", choices=list(MODELS), action="append")
    p.add_argument("--rescore", help="保存した結果を、いまの決め方と正解で数え直す（問い合わせはしない）")
    args = p.parse_args()
    save_result("G-42", rescore(args.rescore) if args.rescore else run(args))
    drop_partial("G-42")


if __name__ == "__main__":
    main()
