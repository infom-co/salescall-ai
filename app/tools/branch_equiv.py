"""分岐のシートの置き換え（business_settings._branch_flow）が、会話の側（CL-1 の flowctl.follow_ups_in・gate_of・held と、断られたら
それだけで果たせない最終目的の締め）で分岐のシートどおりの質問を伺うかを、ランダムな分岐で全部の答えの並びについて比べる作業の道具（CL-2 の
連絡帳 4-207。「どれも要らない」は決まり＝ほかのご希望が先、なので比べない）。使い方（app で）：.venv/bin/python tools/branch_equiv.py ../phase0/flowctl.py 20000"""
import random, sys, importlib.util
from dataclasses import dataclass
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
import salescall.core.business_settings as bs
spec = importlib.util.spec_from_file_location("fc", sys.argv[1]); 
# flowctl は phase0 の依存が多いので、要る関数だけを抜き出して使う
src = open(sys.argv[1]).read()
ns = {"TAKEN": ("confirmed", "accepted", "dont_know", "refused"), "GOT": ("confirmed", "accepted")}
import re
for name in ("business_next", "inserted", "follow_ups_in", "gate_of", "held"):
    m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.S | re.M)
    exec(m.group(0), ns)

@dataclass
class CQ:
    id: str; ask_if: tuple | None; ends_on: tuple; ask_if_hedged: bool = False; callback: bool = False; reading_of: object = None

def run(seed):
    rnd = random.Random(seed)
    n = rnd.randint(2, 7)
    ids = [f"Q{i+1}" for i in range(n)]
    qs, nexts = [], {}
    for i, qid in enumerate(ids):
        kind = rnd.choice(["yes_no", "choice", "choice", "value"])
        if kind == "yes_no":
            q = bs.Question(qid, "t", None, "yes_no", rnd.random() < .7, bs.YES_NO_CHOICES, asks_fact=rnd.random() < .5)
        elif kind == "choice":
            q = bs.Question(qid, "t", None, "choice", rnd.random() < .7, ("A", "B", "C")[:rnd.randint(2, 3)], asks_fact=rnd.random() < .5)
        else:
            q = bs.Question(qid, "t", None, "text", rnd.random() < .7, ("答えた",))
        if q.type in bs.CHOICE_TYPES and not q.asks_fact:
            q = bs.replace(q, away=tuple(c for c in q.choices if rnd.random() < .4))
        qs.append(q)
        if rnd.random() < .6:
            later = ids[i + 1:] + ["END"]
            ans = [a for a in bs.possible_answers(q) if a not in bs.SYSTEM_CHOICES]
            table = {a: rnd.choice(later) for a in ans if rnd.random() < .6}
            table["*"] = rnd.choice(later)
            nexts[qid] = table
    problems = []
    out = bs._branch_flow(list(qs), nexts, problems)
    if problems:
        return "rejected", problems
    flow_answers = {q.id: [a for a in bs.possible_answers(q) if a not in bs.SYSTEM_CHOICES and a != bs.NONE_CHOICE] for q in qs}
    at = {q: i for i, q in enumerate(ids)}
    def to(qid, a):
        t = nexts.get(qid) or {}
        nx = ids[at[qid] + 1] if at[qid] + 1 < n else "END"
        return t.get(a, t.get("*", nx)) if t else nx
    cqs = [CQ(q.id, (q.ask_if.after, frozenset(q.ask_if.answers)) if q.ask_if else None, q.ends_on) for q in out]
    by = {q.id: q for q in out}
    mismatches = []
    def dfs(answers: dict, gpath: list, gcur, forder):
        # 会話の側：次に伺う質問
        read = lambda x: ("confirmed", answers[x], False) if x in answers else ("empty", None, False)
        # 最終目的（断られたらそれだけで果たせない）を断られたら締める
        if any(by[x].goal_must and answers[x] in by[x].declines for x in answers):
            nxt_flow = None
        else:
            order = ns["follow_ups_in"](forder, cqs, read)
            hold = set(ns["held"](cqs, order, read))
            rem = [x for x in order if x not in answers and x not in hold]
            nxt_flow = rem[0] if rem else None
        nxt_graph = None if gcur == "END" else gcur
        if nxt_flow != nxt_graph:
            mismatches.append((dict(answers), nxt_graph, nxt_flow)); return
        if nxt_flow is None:
            return
        for a in flow_answers[nxt_flow]:
            dfs({**answers, nxt_flow: a}, gpath + [nxt_flow], to(nxt_flow, a), order)
    dfs({}, [], ids[0], [q.id for q in out if not q.ask_if])
    return ("ok" if not mismatches else "MISMATCH"), (mismatches[:2], nexts, [(q.id, q.ask_if, q.ends_on, q.final_goal, q.declines, q.goal_must, q.away) for q in out])

from collections import Counter
c = Counter(); bad = []
for seed in range(int(sys.argv[2])):
    r, info = run(seed)
    c[r] += 1
    if r == "MISMATCH" and len(bad) < 3:
        bad.append((seed, info))
print(c)
for b in bad:
    print(b)
import re as _re
rc = Counter()
for seed in range(int(sys.argv[2])):
    r, info = run(seed)
    if r == "rejected":
        for p in set(_re.sub(r"Q\d+|「[^」]*」|\[[^\]]*\]", "_", x) for x in info):
            rc[p[:70]] += 1
for k, v in rc.most_common(12):
    print(v, k)
