"""CL-1 の試験（phase0/test_offline.py）で作られた会話の流れ（Flow）すべてについて、試験の終わりに通話の報告（Flow.report）を作り、CL-2 の読み手
call_report.from_json に通して、断られた報告を数える道具（CL-2 の連絡帳 4-408。phase0 は変えない）。名前ごとの比べる試験の外の食い違い（新しい鍵・
空の文・値の組み合わせ）を、実際の報告で見つける。比べる試験の CL2_PENDING・CL2_RETIRED は空にして流す（入れた鍵も読み手に通す）。
使い方（phase0 で、phase0 の試験と同じ Python で）：python ../app/tools/report_probe.py 断られた報告の見本の置き場.json
"""
import sys, os, unittest, collections, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "..", "app", "src"))
import flowctl
from salescall.core import call_report
import test_offline as t
t.CL2_PENDING = frozenset(); t.CL2_RETIRED = frozenset()
flows = []
orig_init = flowctl.Flow.__init__
def init(self, *a, **k):
    orig_init(self, *a, **k); flows.append(self)
flowctl.Flow.__init__ = init
seen = collections.Counter(); samples = {}; where = collections.defaultdict(list)
def check(test_id):
    for f in flows:
        try:
            r = f.report()
        except Exception as e:
            seen["(報告を作れない) " + type(e).__name__] += 1; continue
        try:
            call_report.from_json(json.loads(json.dumps(r, ensure_ascii=False, default=str)))
            seen["ok"] += 1
        except Exception as e:
            m = f"{type(e).__name__}: {e}"; seen[m] += 1; samples.setdefault(m, r); where[m].append(test_id)
    flows.clear()
orig_run = unittest.TestCase.run
def run(self, result=None):
    flows.clear()
    out = orig_run(self, result)
    check(self.id())
    return out
unittest.TestCase.run = run
suite = unittest.TestLoader().loadTestsFromModule(t)
unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(suite)
print("通った報告:", seen.pop("ok", 0))
for m, n in seen.most_common():
    print(n, m[:400], "|", where[m][:2])
json.dump({m: samples[m] for m in samples}, open(sys.argv[1], "w"), ensure_ascii=False, default=str)
