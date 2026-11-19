"""G-59（お呼びする段の判定）の正解を付けるために、答えを伏せた読み手に渡す束を作る（0円＝判定にも外にも問い合わせない）。頼み rv123/Y3 の (4)。
- 束 a：正解の表の候補（g59_calling_judge.candidates＝分身 M の5場面50行。ご事情を伺った一言・詳しい呼ぶ一言・確かめたあとに答えてお呼びし直した一言のあと）の、
  目的のお客様の判定の全部の項目と、尋ねているか
- 束 b：正解の表（g59_calling_judge.cases）の全部の行の、相手の申し出（flowctl.offer_choices）と電話に出られない理由（flowctl.absent_why_choices）
  （2026-11-18 の rv92/H で足した2つの項目。2026-11-19 日本時間21時台に、この束の読み（rv150/readers/y3 の A_b・B_b）で正解の表に正解を付けた）
読み手に見せるのは、判定が見る文（説明の文と選べるもの＝meaning_judge.prompt_parts の1か所）と、行の場面の会話（本番の流れの制御で組んだ場面＝
g59_flow_check.row_scene）だけ。正解の表の正解・前の言葉の読み（BEFORE_READ）は渡さない。答えの欄の書き方の例は、形だけの置き場（実在の行を使わない）。
使い方：python g59_readers.py --out <書き出す場所> --dry-run（束 packet_a.md・packet_b.md・頼みの文 request_a.md・request_b.md・行の名札 key_a.json・key_b.json）
"""
from __future__ import annotations

import json
from pathlib import Path

from common import base_parser, require_python, say

PACKETS = {"a": "正解の表の候補（ご事情を伺った一言・詳しい呼ぶ一言・確かめたあとに答えてお呼びし直した一言のあと）",
           "b": "正解の表の全部の行の、相手の申し出と電話に出られない理由"}
ASKS = "asks"   # 尋ねているか（別の問い合わせ＝Flow.asks_context）の項目の鍵（束 a だけ）


def _rows(name: str) -> list[dict]:
    """束の行（測りの行の形＝row_scene・judge_questions にそのまま渡せる）。束 b は正解の表の行（正解は捨てる）。"""
    import g59_calling_judge as g
    if name == "a":
        return g.candidates()
    return [{"id": f"T{i}", "text": t, "before": b, "asked": bool(a), "successor": s, "full": False}
            for i, (t, _, b, a, s, _) in enumerate(g.cases("確かめる一言"), 1)]


def _spoken(rep: dict) -> str:
    """流れの制御の返事のうち、お客様に聞こえる言葉（相づち・流す録音・会話AIに言わせる文）。"""
    from phrases import ACK
    return ACK.get(rep.get("ack"), "") + "".join(p.get("text", "") for p in rep.get("play") or []) + (rep.get("say") or "")


def conversation(r: dict) -> list[tuple[str, str]]:
    """行 r の場面の会話（お客様に聞こえた順）：本番の流れの制御で組んだ場面（g59_flow_check.row_scene）の、名乗りと呼ぶ一言（場面は流し終えた所から始まる）・
    お客様の言葉ごとのこちらの返事（流れの制御の記録 log）・行の言葉。こちらが何も言わなかったお言葉は「（何も言わない）」。"""
    import g59_flow_check as fc
    f = fc.row_scene(r)
    parts = (fc.b_full if r.get("full") else fc.b).replay_parts()
    out = [("こちら", "".join(parts[k] for k in ("greeting", "intro", "target")))]
    for turn, words in sorted(f.calling_words.items()):
        out.append(("お客様", words))
        out.append(("こちら", "".join(_spoken(x["reply"]) for x in f.log if x["callee_turns"] == turn) or "（何も言わない）"))
    return out + [("お客様", r["text"])]


def packet(name: str) -> dict:
    """束（読み手に見せるものだけ）：判定が見る文の型（P＝目的のお客様の判定・A＝尋ねているか）・答える項目の鍵・行ごとの会話と、判定が見る文の続き（その前に
    相手が言ったこと・お客様の言葉）。行の名札（key）は読み手に渡さない（まとめ役が答えを表へ戻すのに使う）。"""
    import g59_calling_judge as g
    from flowctl import ABSENT_WHY, OFFER
    from meaning_judge import prompt_parts
    variants: dict[str, str] = {}

    def vid(head: str, text: str) -> str:
        for k, v in variants.items():
            if v == text and k.startswith(head):
                return k
        k = f"{head}{sum(1 for x in variants if x.startswith(head)) + 1}"
        variants[k] = text
        return k
    rows, items = [], [OFFER, ABSENT_WHY]
    for no, r in enumerate(_rows(name), 1):
        q, aq = g.judge_questions(r)
        fixed, tail = prompt_parts(q, r["text"], None, r["before"])
        row = {"no": no, "p": vid("P", fixed), "talk": conversation(r), "tail": tail.rstrip(),
               "key": {k: r.get(k) for k in ("id", "text", "before", "asked", "successor", "full")}}
        if name == "a":
            afixed, atail = prompt_parts(aq, r["text"], None, r["before"])
            row["asks"] = vid("A", afixed)
            if atail.rstrip() != tail.rstrip():
                row["asks_tail"] = atail.rstrip()
        rows.append(row)
    if name == "a":
        items = list(dict.fromkeys(fc.key for r in _rows("a") for fc in g.judge_questions(r)[0].facets)) + [ASKS]
    return {"name": name, "title": PACKETS[name], "items": items, "variants": variants, "rows": rows}


def packet_text(name: str) -> str:
    """束の文（Markdown）。"""
    p = packet(name)
    out = [f"# {p['title']}", "",
           "前半は「判定が見る文」の型（P＝お呼びしている方かなどを読む判定・A＝こちらに尋ねているかを読む判定）。判定は、その型の文のあとに、行ごとの「判定が見る文の続き」"
           "（その前に相手が言ったこと・お客様の言葉）を続けた文だけを見て選びます。後半は行。行ごとに、その場面の会話（お客様に聞こえた順）と、判定が見る文の続きがあります。", ""]
    out += ["# 判定が見る文の型", ""]
    for k, v in p["variants"].items():
        out += [f"## {k}", "", "```", v, "```", ""]
    out += ["# 行", ""]
    for row in p["rows"]:
        tag = f"［{row['p']}" + (f"・{row['asks']}" if "asks" in row else "") + "］"
        out += [f"## #{row['no']}{tag}「{row['talk'][-1][1]}」", "", "会話（お客様に聞こえた順）："]
        out += [f"- {who}：「{w}」" if not w.startswith("（") else f"- {who}：{w}" for who, w in row["talk"]]
        out += ["", "判定が見る文の続き：", "```", row["tail"], "```"]
        if "asks_tail" in row:
            out += ["尋ねているかの判定が見る文の続き：", "```", row["asks_tail"], "```"]
        out.append("")
    return "\n".join(out)


def request_text(name: str, out_dir: str) -> str:
    """読み手への頼みの文（rv56/readers/request.md の形。答えの例は形だけの置き場）。"""
    p = packet(name)
    keys = "・".join(p["items"])
    which = ("型の文にある項目の全部（" + keys + "。asks は A の型の文の選べるもの）" if name == "a"
             else "相手の申し出（offer）と電話に出られない理由（absent_why）の2つだけ（型の文のほかの項目は答えません）")
    return "\n".join([
        "あなたは、電話の会話の言葉を読む「読み手」です。答えは日本語で書いてください。（使い方：〈印〉を A か B に置き換えて、2人に同じ束を別々に頼む。）",
        "",
        f"材料：{out_dir}/packet_{name}.md （Read で、切らずに全部読んでください。長いので、行の番号を変えながら何回かに分けて、最後まで読んでください）。",
        "- 前半に「判定が見る文の型」があります。こちらは目的の方をお呼びする電話をかけ、電話に出た方が何か言いました。判定（AI）は、型の文に行ごとの"
        "「判定が見る文の続き」を続けた文だけを見て、項目ごとに選べるものを選びます。",
        "- 後半に行（#1〜）があります。各行は「番号・使う型・会話・判定が見る文の続き」です。会話は場面をつかむためのものです。判定は、こちらが前に言った一言は、"
        "型の文の質問（こちらが最後に言った一言）のほかは見ていません。",
        "",
        "頼み：あなたは日本語を母語とする、注意深い人として、判定と同じ情報だけ（型の文と、判定が見る文の続き）から、各行を読んでください。正解の表は渡して"
        "いません。ほかの人の読みも渡していません。自分で読んでください。",
        "",
        f"各行について、{which}を、次のとおり書いてください。",
        "1. items：項目の鍵ごとに、注意深い人がこの情報だけから無理なく選びうるものを、すべて挙げる（名前は文に書いてあるとおりに写す）。項目は1つも抜かさない。",
        "2. 項目それぞれについて、言葉から1つに決まるなら \"一つに決まる\"、2つ以上がどれも無理なく取れるなら \"どちらとも取れる\" と書き、どちらとも取れるときは、"
        "相手が言いたかったかもしれない意味を2つ以上、短く書く（例の形：「(1) 〜という意味 (2) 〜という意味」）。",
        "3. missing：相手が言いたかったかもしれない意味のうち、どの選べるものにもうまく当たらないものがあれば、どの項目のことかと、その意味を書く（無ければ空）。",
        "4. context：会話（判定が見ていない、こちらの前の一言）が分かれば、どれか1つに決まりそうなら、どの項目が、何が分かれば決まるかを書く（無ければ空）。",
        "5. desc：選べるものの説明の文（項目の選べるものの説明・決まりの文）のうち、言葉からは言えない選び方へ読み手を引っ張りそうな所や、意味の境目が分かりにくい"
        "所があれば、その説明の文の言葉を引いて書く（無ければ空）。",
        "",
        "決まり：",
        "- 「無理なく選びうる」は、日本語として自然に取れる意味だけ。こじつけは入れない。逆に、自分ならこちらと思っても、もう一方も自然に取れるなら両方挙げる。",
        "- 選べるものの説明の文に書いてある決まりは、判定が従う決まりとして当てて読む。ただし、その決まりを当てても2つ以上に取れるなら、両方挙げる。",
        "- 外の道具・ネットワークは使わない。リポジトリのファイルは読まない・書き換えない。使うのは、材料を読む Read と、答えを書く Write だけ。",
        "",
        f"答えの形：{out_dir}/〈印〉_{name}.jsonl に、1行に1つの JSON（束の行の数＝{len(p['rows'])} 行）を Write で書く。形だけの例（中身は置き場）：",
        '{"no": 0, "items": {"（項目の鍵1）": {"names": ["（名前）"], "kind": "一つに決まる", "meanings": ""}, "（項目の鍵2）": {"names": ["（名前）", "（名前）"], '
        '"kind": "どちらとも取れる", "meanings": "(1) （意味） (2) （意味）"}}, "missing": "", "context": "", "desc": ""}',
        "書き終えたら、最後の返事に、\"どちらとも取れる\" にした行の番号の一覧（項目の鍵ごと）と、missing・desc に書いた行の番号だけを短く書いてください。",
        ""])


def write(out: Path) -> dict[str, int]:
    """束・頼みの文・行の名札を書き出す。返す：束ごとの行の数。"""
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name in PACKETS:
        p = packet(name)
        (out / f"packet_{name}.md").write_text(packet_text(name), encoding="utf-8")
        (out / f"request_{name}.md").write_text(request_text(name, str(out)), encoding="utf-8")
        (out / f"key_{name}.json").write_text(json.dumps([{"no": r["no"], **r["key"]} for r in p["rows"]], ensure_ascii=False, indent=1),
                                              encoding="utf-8")
        counts[name] = len(p["rows"])
    return counts


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--out", required=True, help="束を書き出す場所")
    args = p.parse_args()
    for name, n in write(Path(args.out)).items():
        say(f"束 {name}（{PACKETS[name]}）：{n} 行")


if __name__ == "__main__":
    main()
