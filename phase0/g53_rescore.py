"""付け直した正解（g53_labels.json・g53_meaning.accept）で、G-53 の判定を数え直す（2026-10-18）。
使い方：python g53_rescore.py results/G-53-…json [ほかの結果 …]（同じ答えへの判定は1つにまとめる）"""
import json, sys, glob
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from g53_meaning import JUDGES, accept, dangerous, mark, g48_questions, value_questions, versus
from g14_listen import business
from flowctl import UNDECIDED
from collections import Counter
qmap = {q.id: q for q in g48_questions() + value_questions()} | {qid: business(qid).questions[0] for qid in ("Q1", "Q2", "Q3", "Q5")}
files = sys.argv[1:]
items = {}
for f in files:
    for it in json.load(open(f))["items"]:
        k = (it["set"], it["qid"], it["text"], it.get("asked"))
        items.setdefault(k, {}).update(it)
items = [i for i in items.values() if i["set"] in "ABDE"]
J = [j for j in JUDGES if any(j in i for i in items)]
for j in J:
    its = [i for i in items if j in i and i.get("maker") != j]
    c = Counter()
    for i in its:
        got = i[j]
        c["判定した"] += 1
        if got in accept(i):
            c["正しい"] += 1
        elif dangerous(i, qmap[i["qid"]], j):
            c["⛔危ない（受け取ると誤り）"] += 1
        elif got in (UNDECIDED, "unknown"):
            c["確かめに回る（要らない）"] += 1
        else:
            c["別の読み（確かめに回る）"] += 1
    secs = sorted(i[f"{j}秒"] for i in its if i.get(f"{j}秒") is not None)
    sp = f"中央{secs[len(secs)//2]:.2f}・9割{secs[int(len(secs)*.9)]:.2f}・2秒以内{sum(s <= 2 for s in secs)}/{len(secs)}" if secs else ""
    print(f"{j}: {dict(c)} {sp}")
    print("   曖昧の印:", dict(Counter(m for i in its if (m := mark(i, j)))))
    bad = [i for i in its if dangerous(i, qmap[i["qid"]], j)]
    print("   危ない:", Counter((i["set"], i.get("kind")) for i in bad).most_common(6))
    if j != "3.5 Flash Lite":
        print("   いまの判定と比べて:", versus(items, qmap, j))
