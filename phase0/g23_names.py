"""G-23：取込のときの名前の洗い出し（Intent.md 6-8）を、文字のモデルと、AIを使わない単語分けで比べる（2026-10-13 ユーザー指示）。

ユーザー：「テストしたらいいだけ。簡単な作業なら、どれもほとんど変わらないはず。Lite は慎重にテストして（明らかに性能が劣る）」
- 材料：正解を前もって決めた台本の文（SCRIPTS）。読みの難しい名前・文脈で読みが決まる名前・名前の無い文を混ぜた
- 比べる：Gemini 3.6 Flash／3.7 Flash／3.8 Flash／3 Flash／3.5 Flash Lite（Lite は同じ材料を3回）／単語分け（fugashi）
- 見ること：見落とし（拾えた割合）・名前でないものを拾った数・読みの正しさ・読みが決まらない名前に「要確認」を付けたか・モデルどうしの一致
「」でくくる作業も確かめる（2026-10-13 ユーザー指示「固有名詞に「」をつけさせる作業なども確認した方がいい」）：
- 設計の形（pipeline）：洗い出した名前の一覧を、プログラム（instruction.quote_in_text）が文に当てて「」を付ける。問い合わせは増えない
- 比べる形（ai_quote）：AIに、文へ直接「」を付けさせる（names.quote_by_model）。QUOTE_SCRIPTS の文だけ
- 見ること：くくった名前（拾えた・余分）と、⛔「」以外の文字が1文字でも変わっていないか（変わった回は、変わった所を残す）
1日の上限（Flash は各20回）に収まるよう、1回の問い合わせで1つの文を見させる（洗い出し11回＋直接くくる6回＝17回）。1つのキーで1回ずつ順番に。
"""
from __future__ import annotations

import difflib
import json
import re
import time

from common import base_parser, describe_error, make_client, pick_key, require_python, save_result, say
from jtext import reading
from instruction import quote_in_text
from names import by_model, by_tokenizer, norm_surface, quote_by_model

# 正解：alts＝名前の書き方（どれか1つに当たればよい）、readings＝正しい読み、must_flag＝読みが文から決まらず「要確認」が要る
SCRIPTS = [
    ("お忙しいところ恐れ入ります。株式会社アオバ物流の佐藤と申します。ご予約の確認でお電話いたしました。",
     [{"alts": ["アオバ物流"], "readings": ["あおばぶつりゅう"]}, {"alts": ["佐藤"], "readings": ["さとう"]}]),
    ("東海林様はいらっしゃいますでしょうか。先日、大阪の日本橋店にお越しいただいた件でございます。",
     [{"alts": ["東海林"], "readings": ["しょうじ", "とうかいりん"]}, {"alts": ["大阪"], "readings": ["おおさか"]},
      {"alts": ["日本橋", "日本橋店"], "readings": ["にっぽんばし", "にっぽんばしてん"]}]),  # 大阪の日本橋＝にっぽんばし（文脈で決まる）
    ("弊社のクラウド勤怠サービス『スマートリンクPro』についてご案内させていただきたく、お電話いたしました。",
     [{"alts": ["スマートリンクPro"], "readings": ["すまーとりんくぷろ"]}]),
    ("株式会社ＡＢＣホールディングス営業部の小鳥遊と申します。",
     [{"alts": ["ABCホールディングス"], "readings": ["えーびーしーほーるでぃんぐす", "えいびいしいほーるでぃんぐす"]},
      {"alts": ["小鳥遊"], "readings": ["たかなし"]}]),
    ("三田の本社から、担当の角田が改めてご連絡いたします。",
     [{"alts": ["三田"], "readings": ["みた", "さんだ"], "must_flag": True},     # 東京の三田＝みた、兵庫の三田＝さんだ
      {"alts": ["角田"], "readings": ["かくた", "つのだ", "すみだ", "かどた"], "must_flag": True}]),
    ("来週の火曜日に、神田の事務所で打ち合わせをお願いできますでしょうか。",
     [{"alts": ["神田"], "readings": ["かんだ"]}]),
    ("ご担当の長谷川様に、資料をお送りしてもよろしいでしょうか。",
     [{"alts": ["長谷川"], "readings": ["はせがわ"]}]),
    ("このたびは、株式会社みらい不動産をご利用いただき、ありがとうございます。",
     [{"alts": ["みらい不動産"], "readings": ["みらいふどうさん"]}]),
    ("お住まいの地域が、川崎市中原区でお間違いないか確認させてください。",
     [{"alts": ["川崎市中原区", "川崎市", "川崎"], "readings": ["かわさきしなかはらく", "かわさきし", "かわさき"]},
      {"alts": ["川崎市中原区", "中原区", "中原"], "readings": ["かわさきしなかはらく", "なかはらく", "なかはら"]}]),
    ("本日は、ご予約の内容とお支払い方法について、確認のお電話をいたしました。", []),  # 名前は無い
    ("株式会社日本橋商事の上野と申します。",
     [{"alts": ["日本橋商事"], "readings": ["にほんばししょうじ"]}, {"alts": ["上野"], "readings": ["うえの"]}]),
]
QUOTE_SCRIPTS = [0, 1, 2, 3, 8, 9]  # AIに直接「」を付けさせる文（社名の前後・読みの難しい名前・もともとの『』・営業部の前・地名の重なり・名前の無い文）
MODELS = {"3.6 Flash": "Gemini 3.6 Flash", "3.7 Flash": "Gemini 3.7 Flash", "3.8 Flash": "Gemini 3.8 Flash",
          "3 Flash": "Gemini 3 Flash", "3.5 Flash Lite": "Gemini 3.5 Flash Lite"}


def rd(s: str) -> str:
    return reading(s or "").replace(" ", "")


def score(gold: list[dict], got: list[dict]) -> dict:
    used, found, read_ok, flag_ok, flag_need = set(), 0, 0, 0, 0
    detail = []
    for g in gold:
        alts = {norm_surface(a) for a in g["alts"]}
        hit = next((i for i, x in enumerate(got) if norm_surface(x.get("surface", "")) in alts), None)
        if hit is None:
            detail.append({"gold": g["alts"][0], "found": False})
            if g.get("must_flag"):
                flag_need += 1
            continue
        used.add(hit)
        found += 1
        x = got[hit]
        ok_read = rd(x.get("reading")) in {rd(r) for r in g["readings"]}
        flagged_ok = bool(x.get("uncertain")) and any(rd(c) in {rd(r) for r in g["readings"]} for c in (x.get("candidates") or []) + [x.get("reading", "")])
        if g.get("must_flag"):
            flag_need += 1
            flag_ok += bool(x.get("uncertain"))
            good = bool(x.get("uncertain"))
        else:
            good = ok_read or flagged_ok
        read_ok += good
        detail.append({"gold": g["alts"][0], "found": True, "reading": x.get("reading"), "uncertain": x.get("uncertain"),
                       "candidates": x.get("candidates"), "ok": good})
    extras = [x.get("surface") for i, x in enumerate(got) if i not in used
              and not any(norm_surface(x.get("surface", "")) in {norm_surface(a) for a in g["alts"]} for g in gold)]
    return {"gold": len(gold), "found": found, "reading_ok": read_ok, "flag_need": flag_need, "flag_ok": flag_ok,
            "extras": extras, "detail": detail}


_QUOTED = re.compile(r"[「『]([^「」『』]*)[」』]")


def strip_quotes(s: str) -> str:
    return s.replace("「", "").replace("」", "")


def quote_score(gold: list[dict], script: str, quoted: str) -> dict:
    """くくった後の文を見る：くくられた名前が正解に当たるか、余分か、⛔「」以外の文字が変わっていないか。"""
    before = {norm_surface(x) for x in _QUOTED.findall(script)}   # もともと台本でくくられていた名前（見つけた数に入れない）
    spans = [norm_surface(x) for x in _QUOTED.findall(quoted)]
    pre = [g for g in gold if any(norm_surface(a) in before for a in g["alts"])]
    todo = [g for g in gold if g not in pre]
    found = sum(1 for g in todo if any(norm_surface(a) in spans for a in g["alts"]))
    kept = sum(1 for g in pre if any(norm_surface(a) in spans for a in g["alts"]))
    golds = {norm_surface(a) for g in gold for a in g["alts"]}
    changed = strip_quotes(quoted) != strip_quotes(script)
    diff = [] if not changed else [d for d in difflib.ndiff([strip_quotes(script)], [strip_quotes(quoted)]) if d[:1] in "+-?"]
    return {"gold": len(todo), "found": found, "pre_bracketed": len(pre), "pre_kept": kept,
            "extras": [s for s in spans if s not in golds], "changed_other": changed, "diff": diff, "quoted": quoted}


def quote_total(rows: list[dict]) -> dict:
    s = {"scripts": 0, "gold": 0, "found": 0, "pre_bracketed": 0, "pre_kept": 0, "extras": 0, "changed_other": 0, "errors": 0}
    for r in rows:
        if "error" in r:
            s["errors"] += 1
            continue
        q = r["quote"]
        s["scripts"] += 1
        s["gold"] += q["gold"]
        s["found"] += q["found"]
        s["pre_bracketed"] += q["pre_bracketed"]
        s["pre_kept"] += q["pre_kept"]
        s["extras"] += len(q["extras"])
        s["changed_other"] += q["changed_other"]
    return s


def pipeline(rows: list[dict]) -> dict:
    """設計の形：洗い出した名前の一覧を、プログラムが文に当てて「」を付ける（問い合わせは増えない）。"""
    out = []
    for (s, g), r in zip(SCRIPTS, rows):
        if "error" in r:
            out.append({"script": s, "error": r["error"]})
            continue
        quoted = quote_in_text(s, [x.get("surface", "") for x in r["got"]])
        out.append({"script": s, "quote": quote_score(g, s, quoted)})
    return {"total": quote_total(out), "rows": out}


def total(rows: list[dict]) -> dict:
    s = {"gold": 0, "found": 0, "reading_ok": 0, "flag_need": 0, "flag_ok": 0, "extras": 0, "errors": 0}
    for r in rows:
        if "error" in r:
            s["errors"] += 1
            continue
        for k in ("gold", "found", "reading_ok", "flag_need", "flag_ok"):
            s[k] += r["score"][k]
        s["extras"] += len(r["score"]["extras"])
    return s


def find_models(client) -> tuple[dict, list[str]]:
    """表示名からモデルIDを決める（IDを記憶で書かない）。見つからないときのために、名前に Flash を含むモデルの表示名も返す。"""
    found, flash = {}, []
    for m in client.models.list():
        name = (getattr(m, "display_name", "") or "").strip()
        if "flash" in name.lower():
            flash.append(name)
        for key, disp in MODELS.items():
            if name == disp and "generateContent" in (getattr(m, "supported_actions", None) or []):
                found[key] = m.name.split("/")[-1]
    return found, sorted(set(flash))


def run(args) -> dict:
    tok_rows = [{"script": s, "got": by_tokenizer(s), "score": score(g, by_tokenizer(s))} for s, g in SCRIPTS]
    out = {"tokenizer": {"total": total(tok_rows), "rows": tok_rows, "pipeline": pipeline(tok_rows)}}
    say("単語分け：", json.dumps(out["tokenizer"]["total"], ensure_ascii=False))
    say("単語分け→プログラムがくくる：", json.dumps(out["tokenizer"]["pipeline"]["total"], ensure_ascii=False))
    if args.dry_run:
        return {"dry_run": True, **out}
    client = make_client(pick_key(args))
    ids, flash_names = find_models(client)
    say("モデルID：", ids)
    out["model_ids"], out["flash_display_names"] = ids, flash_names
    missing = [k for k in (args.model_key or list(MODELS)) if k not in ids]
    if missing:
        say("⚠️ 見つからないモデル：", missing, "／名前に Flash を含むモデル：", flash_names)
    plan = [(k, rep) for k in (args.model_key or list(MODELS)) if k in ids
            for rep in range(args.lite_repeat if "Lite" in k else 1)]
    results = {f"{k}#{rep}": [] for k, rep in plan}
    quoted = {f"{k}#{rep}": [] for k, rep in plan}
    for n, (s, g) in enumerate(SCRIPTS):  # 文ごとに、モデルを順に回す（同じモデルへの問い合わせの間を空ける＝1分あたりの上限）
        for k, rep in plan:
            try:
                got = by_model(client, ids[k], s)
                row = {"script": s, "got": got, "score": score(g, got)}
            except Exception as exc:
                row = {"script": s, "error": describe_error(exc)}
            results[f"{k}#{rep}"].append(row)
            say(f"{k}#{rep} {s[:16]} 拾えた={row.get('score', {}).get('found')}/{len(g)} 読み={row.get('score', {}).get('reading_ok')} "
                f"余分={row.get('score', {}).get('extras')} {row.get('error', '')}")
            time.sleep(args.pause)
            if n not in QUOTE_SCRIPTS or args.no_ai_quote:
                continue
            try:
                q = quote_by_model(client, ids[k], s)
                qrow = {"script": s, "quote": quote_score(g, s, q)}
            except Exception as exc:
                qrow = {"script": s, "error": describe_error(exc)}
            quoted[f"{k}#{rep}"].append(qrow)
            qq = qrow.get("quote", {})
            say(f"{k}#{rep} 直接くくる：{qq.get('quoted', '')} 変わった={qq.get('changed_other')} {qrow.get('error', '')}")
            time.sleep(args.pause)
    out["models"] = {k: {"total": total(v), "rows": v, "pipeline": pipeline(v),
                         "ai_quote": {"total": quote_total(quoted[k]), "rows": quoted[k]}} for k, v in results.items()}
    for k, v in out["models"].items():
        say(k, "洗い出し", json.dumps(v["total"], ensure_ascii=False))
        say(k, "プログラムがくくる", json.dumps(v["pipeline"]["total"], ensure_ascii=False))
        say(k, "AIが直接くくる", json.dumps(v["ai_quote"]["total"], ensure_ascii=False))
    return out


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--lite-repeat", type=int, default=3)
    p.add_argument("--model-key", choices=list(MODELS), action="append")
    p.add_argument("--no-ai-quote", action="store_true", help="AIに直接「」を付けさせる比べをしない（問い合わせを減らす）")
    args = p.parse_args()
    save_result("G-23", run(args))


if __name__ == "__main__":
    main()
