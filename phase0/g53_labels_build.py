"""G-53 の正解の付け直し（2026-10-18・CL-1 が1つずつ読んだもの）を、phase0/g53_labels.json に書く。"""
import json, sys
from pathlib import Path
HERE_ = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE_))
from g53_meaning import truth, g48_questions, value_questions
from flowctl import ANY, UNDECIDED
qmap = {q.id: q for q in g48_questions() + value_questions()}
d = json.load(open(str(HERE_ / "results" / "G-53-others2_20261018_144241.json")))["items"]
NT = [UNDECIDED, "unknown"]
def key(i): return f"{i['set']}|{i['qid']}|{i['text']}|{i.get('asked') or ''}"
def other(i):
    q = qmap[i["qid"]]
    rest = [c for c in q.conditions if c != i.get("asked_choice")]
    return rest[0] if len(rest) == 1 else None
labels = {}
def put(i, accept, why, hedged=None):
    labels[key(i)] = {"accept": accept, "why": why, "was": truth(i)} | ({} if hedged is None else {"hedged": hedged})

# (1) 言い直しへの「違います」だけの返事：選択肢が2つなら、もう一方が答え（3つ以上なら決められない）
for i in d:
    if i["set"] == "D" and i.get("kind") == "違うだけ":
        q = qmap[i["qid"]]
        if q.type == "choice" and len(q.conditions) == 2 and truth(i) == UNDECIDED:
            put(i, [other(i)], "選択肢が2つの質問で言い直しを否定＝もう一方（作らせたモデルは決められないと付けていた）")

# (2) 言い直しへの「迷う」返事：ユーザーの決まり（望みが言えていれば答え・どちらとも言っていなければ決められない）。
#     ✅ 2026-10-18 ユーザー：「『急ぎと言えば急ぎですが』『まあ、できれば早くとは思います』『さらっとは見たんですが』『していないはずですが…』
#        『5万円くらいかなとは思いますが』なんかは、『yes,but 曖昧』のような返答にしておけばいいんじゃないですか？」
#     ⇒ 寄っている答えがあれば、その答え（曖昧の印＝hedged）。それまでの「事実がはっきりしなければ確かめる」（CL-1 の案）は、寄っている向きを
#        消していた ⇒ 「確かめる」と付けた200個を1つずつ読み直した（下の LEAN）
LEAN = {  # (質問, 返事) ⇒ A＝言い直した答えに寄っている／AU＝寄っているとも、寄っていないとも読める／OU＝言い直しと逆に寄っているとも…
    ('G1', '確か予約したと思うのですが…'): 'A',
    ('G1', 'どうだったかな、たぶん。'): 'A',
    ('G1', 'どうでしたっけ、取ってあったかな…'): 'OU',
    ('G2', '届いているかもしれませんが、見てないです。'): 'AU',
    ('G2', '家族が受け取ったかもしれないです。'): 'AU',
    ('G2', 'まだ届いていないのかな。'): 'AU',
    ('G3', '契約が残っているかもしれません。'): 'OU',
    ('G4', '昔一回行ったような気もしますが…'): 'OU',
    ('G4', '初めてだったかな。'): 'AU',
    ('G4', '前に行ったのは別の店舗かもしれません。'): 'OU',
    ('G6', '使わない予定ですが、置ければ置きたいかも。'): 'A',
    ('G7', '折り返してもらっても出られるかどうか…'): 'AU',
    ('G7', '要らない気もしますが、どうかな…'): 'AU',
    ('G8', '無しでいいとは思いますが、迷います。'): 'AU',
    ('G10', 'ずらすかもしれません。'): 'AU',
    ('G11', '大丈夫だと思いますが、怪しいです。'): 'AU',
    ('G13', 'リースだったと思うのですが曖昧です。'): 'A',
    ('G13', 'リースじゃなかった気もしますが…'): 'AU',
    ('G14', '見ていないような気もします。'): 'AU',
    ('G15', 'できれば今月中がいいですが迷っています。'): 'AU',
    ('G17', 'メールだと見落とすかもしれないですし…'): 'OU',
    ('G19', 'プレミアムはやりすぎかもと迷い中で。'): 'OU',
    ('G21', '同伴者が吸うかもしれないですが…'): 'OU',
    ('G21', '加熱式タバコなのですが喫煙室でしょうか。'): 'AU',
    ('G23', '光だったと思いますが定かではないです。'): 'A',
    ('G24', '郵送のほうがいいか迷います。'): 'OU',
    ('G29', '社内にはいると思いますが席にいるか…'): 'A',
    ('G29', '会議中かもしれません。'): 'AU',
    ('G30', '一緒に行くかもしれませんが未定です。'): 'AU',
    ('G31', '予約できているかな。'): 'AU',
    ('G31', '予約した気でいましたがどうだったか…'): 'OU',
    ('V3', '携帯の番号だったかな。'): 'AU',
    ('G1', '多分そうだと思うんですが'): 'A',
    ('G1', 'したような気もするんですが'): 'OU',
    ('G2', '届いているはずなんですが'): 'A',
    ('G2', '多分まだだとは思うんですが'): 'A',
    ('G3', '多分あると思うんですが'): 'A',
    ('G3', 'ないとは思うんですが'): 'A',
    ('G4', '昔行ったことがあるような…'): 'OU',
    ('G4', '初めてだったかなあ'): 'AU',
    ('G4', '多分そうだと思うんですが'): 'A',
    ('G4', '前に行ったかしら'): 'AU',
    ('G5', '多分そうなると思いますが'): 'A',
    ('G6', '使うかもしれませんが…'): 'AU',
    ('G6', '多分使わないとは思うんですが'): 'A',
    ('G10', '変えるかもしれません'): 'OU',
    ('G10', '多分あると思うんですが'): 'A',
    ('G11', 'あったような気もします'): 'AU',
    ('G12', '多分まだだと思います'): 'A',
    ('G13', '多分そうだと思うんですが'): 'A',
    ('G13', 'リースだったかしら'): 'AU',
    ('G14', '見たような気もします'): 'AU',
    ('G14', '多分まだだとは思うんですが'): 'A',
    ('G15', '多分そうなると思いますが'): 'A',
    ('G19', 'プレミアムにしようか検討中です'): 'AU',
    ('G22', '多分それくらいだとは思うんですが'): 'A',
    ('G22', '大体それくらいですかね'): 'A',
    ('G22', '多分それくらいだと思いますが'): 'A',
    ('G23', '光だと思うんですが…'): 'A',
    ('G23', '多分モバイル回線のほうだと思います'): 'A',
    ('G28', '多分ないとは思うんですが'): 'A',
    ('G28', 'あるかもしれないですね'): 'AU',
    ('G29', '多分いると思うんですが'): 'A',
    ('G29', 'いないとは思うんですが'): 'A',
    ('G30', '一緒になるかもしれませんが…'): 'AU',
    ('G31', '多分していると思うんですが'): 'A',
    ('V1', 'その時間だったかな…'): 'AU',
    ('V2', '3人だったと思うんですが…'): 'A',
    ('V3', 'その番号だったかな…'): 'AU',
    ('V5', '火曜日だったかな…'): 'AU',
    ('V6', '多分そうだと思うんですが'): 'A',
}

m = [i for i in d if i["set"] == "D" and i.get("kind") == "迷う"]
asked = {20, 27, 46, 96, 100, 105, 106, 157, 161, 163, 164, 165, 172, 192, 198, 201, 202, 206, 208, 212, 214, 216, 218, 230, 232, 234, 236}
oth = {45, 78}
anys = {29, 61, 64, 68, 76, 81, 94, 98, 99, 200, 213, 217, 219}
b_asked = {18, 28, 33, 34, 52, 102, 103, 104, 109, 118, 166, 168, 170, 176, 180, 182, 188, 238, 242, 244, 256, 260, 268}
b_other = {37, 41, 44, 47, 59, 127}
b_any = {204: "asked", 205: None, 235: None}
for n, i in enumerate(m):
    a = i.get("asked_choice") or "affirmed"
    if n in asked:
        put(i, [a], "望みが言えている＝言い直した答えを認めた", hedged=False)
    elif n in oth:
        put(i, [other(i)], "望みが言えている＝もう一方", hedged=False)
    elif n in anys:
        put(i, [ANY], "どちらでもよいと言った")
    elif n in b_asked:
        put(i, [a], "yes, but 曖昧（言い直した答えに寄っているが、言い切っていない）", hedged=True)
    elif n in b_other:
        if i["qid"] == "V2":  # 「増えるかもしれません」（3人でよろしいか）＝3人に寄っているとも、違う値とも読める
            put(i, ["affirmed", "corrected"] + NT, "⏳ 境目（3人に寄っているとも、違う値とも、決められないとも読める）", hedged=True)
        else:
            put(i, [other(i)], "no, but 曖昧（言い直しと逆の答えに寄っているが、言い切っていない）", hedged=True)
    elif n in b_any:
        put(i, ([a] if b_any[n] else []) + [ANY] + NT, "⏳ 境目（どちらでも、とも読める）")
    elif (code := LEAN.get((i["qid"], i["text"]))):
        to = a if code.startswith("A") else other(i)
        if to is None:  # 選択肢が3つ以上で、逆の向きの答えが1つに決まらない
            put(i, NT, "どちらとも言っていない＝決められない")
        elif code in ("A", "O"):
            put(i, [to], "yes, but 曖昧（寄っている答えがあるが、言い切っていない）", hedged=True)
        else:
            put(i, [to] + NT, "⏳ 境目（寄っているとも、どちらとも言っていないとも読める）", hedged=True)
    else:
        put(i, NT, "どちらとも言っていない＝決められない")

# (3) ほかの組で、1つずつ読んで直したもの（review_rest.txt の番号）
rest = [i for i in json.load(open(str(HERE_ / "results" / "G-53-review_20261018.json")))
        if not (i["set"] == "D" and i["kind"] in ("迷う", "違うだけ"))]
byid = {key(i): i for i in d}
def item(n):
    r = rest[n]
    s, qid, text, a = r["id"].split("|", 3)
    return byid[f"{s}|{qid}|{text}|{'' if a == 'None' else a}"]
def cond(i, label):  # 選択肢の意味の言葉から中の名前へ
    from meaning_judge import options
    return next(c for nm, _, c in options(qmap.get(i["qid"]) or __import__("g14_listen").business(i["qid"]).questions[0], i.get("asked")) if nm == label)
fix = {
    (0, 9, 2, 3, 5, 6, 11, 13, 14, 15): (lambda i: NT, "会話AIの聞き取りの文字が崩れていて、文字からは決められない（記録の通話の言葉）"),
    (4, 12): (lambda i: [truth(i)] + NT, "「家まだです」＝崩れた文字。「いえ、まだです」とも読める"),
    (1, 10): (lambda i: NT, "わからない（どちらで受け取っても値は渡らない）"),
    (7,): (lambda i: [ANY], "「どちらでも大丈夫です」は答え（10-17 ユーザー）"),
    (35, 38): (lambda i: [truth(i), "unknown"], "「確認できていない」＝届いていない、とも、わからない、とも読める"),
    (41, 45): (lambda i: [UNDECIDED], "選択肢が3つの質問で、2つに絞れない"),
    (49, 50): (lambda i: [truth(i), ANY], "「テーブルでなくても」＝カウンター、とも、どちらでも、とも読める"),
    (62, 66): (lambda i: [truth(i), "取り次ぎを待つ"], "代わる＝在席とも、取り次ぎとも読める"),
    (69, 72, 73, 144): (lambda i: [truth(i), UNDECIDED], "問い返しや、一方を打ち消しただけで、言い切っていない"),
    (79, 82): (lambda i: [truth(i), UNDECIDED], "「今は電話に出られません」＝折り返しを断ったとも、決められないとも読める"),
    (90,): (lambda i: [cond(i, "ご予約日は決まっていないということ"), "unknown"], "「予定が立ちません」＝決まっていない"),
    (91,): (lambda i: [cond(i, "ご予約日は決まっていないということ")] + NT, "「見通しが立ちません」＝決まっていない"),
    (94,): (lambda i: [i.get("asked_choice"), "unknown"], "⏳ 境目「把握しておりません」＝わからない、とも、見ていない、とも読める"),
    (98, 104, 107, 109): (lambda i: [ANY, "unknown"], "「特に希望はない」＝どちらでも、とも読める"),
    (99, 106, 108): (lambda i: [ANY], "「どちらでもいい」は答え"),
    (110,): (lambda i: NT + [cond(i, "ご不明な点がおありということ")], "「理解できているか不明」＝不明な点がある、とも読める"),
    (117, 118): (lambda i: ["取り次ぎを待つ", "unknown"], "「確認してまいります」＝待つように言われた"),
    (141, 146, 147): (lambda i: [truth(i)] + NT, "「〜かもしれません」＝言い切っていない"),
}
for ns, (f, why) in fix.items():
    for n in ns:
        i = item(n)
        put(i, [x for x in f(i) if x], why)
# (4) Claude Sonnet 5.5 を足して、危ない向きと数えたものを読み直した（2026-10-18）：付けた正解が狭すぎた2個
def find(qid, text):
    return next(i for i in d if i["qid"] == qid and i["text"] == text and i["set"] == "D")
put(find("G8", "送ってもらってもいいですし、どちらでも。"), [ANY, "はい"], "⏳ 境目「送ってもらってもいい」＝送ってよいとも、どちらでも、とも読める")
put(find("G26", "何が分からないかも分かりません"), [cond(find("G26", "何が分からないかも分かりません"), "ご不明な点がおありということ")] + NT,
    "⏳ 境目「何が分からないかも分からない」＝不明な点がある、とも、わからない、とも読める")
out = HERE_ / "g53_labels.json"
json.dump({"note": "G-53 の正解の付け直し（2026-10-18 CL-1 が1つずつ読んだ。ユーザーの決まり：望みが言えていれば答え・寄っている答えがあれば、言い切って"
                   "いなくてもその答え（hedged＝曖昧の印。「yes,but 曖昧」）・どちらとも言っていなければ決められない。⏳ の付いたものは境目＝"
                   "どちらの読みも正解として数える）", "labels": labels},
          open(out, "w"), ensure_ascii=False, indent=1)
from collections import Counter
print(len(labels), Counter(v["why"][:20] for v in labels.values()).most_common(30))
