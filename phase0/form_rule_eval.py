"""日時の型の「値の形」の決まり（flowctl.form_problem）を、これまでの報告の値と、自然な言い方の例に当てる（DESIGN.md 6-2）。"""
import collections, glob, json, re, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from flowctl import normalize_value

from flowctl import form_problem  # 決まりは流れの制御の1か所から読む（ここに書き写さない）


def ok(v):
    return form_problem("datetime", v) is None


def reported(qid, qtype):
    """これまでの試験で、その質問に報告された値（形をそろえたあと）と回数。"""
    vals = collections.Counter()
    def walk(x):
        if isinstance(x, dict):
            fl = x.get('flow_log')
            if isinstance(fl, list):
                for e in fl:
                    a = (e or {}).get('args') or {}
                    if a.get('question_id') == qid and e.get('op') in ('record_answer', 'confirm_field'):
                        v = a.get('value') if e['op'] == 'record_answer' else a.get('correction')
                        if v and a.get('condition_id') not in ('unknown', 'refused'):
                            vals[normalize_value(qtype, v)] += 1
            for k, v in x.items():
                if k != 'flow_log':
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for f in glob.glob(str(__import__('pathlib').Path(__file__).parent / 'results' / 'G-*.json')):
        try:
            walk(json.load(open(f)))
        except Exception:
            pass
    return vals


vals = reported('Q2', 'datetime')
print("■ 日時（Q2）")
WRONG = {"3人", "はい", "答えなかった", "はいそう", "はい、そう"}  # 形をそろえたあとの値
stopped_wrong = sum(n for v, n in vals.items() if v in WRONG and not ok(v))
stopped_right = [(v, n) for v, n in vals.items() if v not in WRONG and not ok(v)]
print("これまでの値：取り違え", sum(n for v, n in vals.items() if v in WRONG), "回のうち止めた", stopped_wrong,
      "／正しい答え", sum(n for v, n in vals.items() if v not in WRONG), "回のうち止めた", sum(n for _, n in stopped_right), stopped_right)
NATURAL = ["15日の午後2時", "来週の火曜日の午後", "明日の朝10時", "あさっての夕方", "月曜の午前中", "今週末", "15時", "午後3時半",
           "10月15日", "3日後", "週明け", "年明け", "正午", "夜7時ごろ", "来月の初め", "金曜以降", "なるべく早く", "いつでも",
           "土日", "平日の夕方", "1時", "二時", "十五日", "あしたの3時"]
print("自然な言い方：止めるもの", [v for v in NATURAL if not ok(v)])
WRONG_FORMS = ["3人", "4名", "3人です", "はい", "いいえ", "答えなかった", "二人", "大丈夫です", "そうです", "わかりません"]
print("取り違えの形：止めないもの", [v for v in WRONG_FORMS if ok(v)])

# ── 数の型（Q3＝ご来店の人数）
print("■ 数（Q3）")
nvals = reported('Q3', 'number')
N_WRONG = {"はい", "いつでも大丈夫", "はいそう", "本人です。はい、そう", "家族"}  # 形をそろえたあとの値（「家族」は G-36 の場面「人数に別の答え」）
DATE_IN_NUMBER = {"15日の午後2時"}  # 日時の答え：数字があるので、この決まりでは止められない（数の型は単位を持たない）
def n_ok(v):
    return form_problem("number", v) is None
print("これまでの値：数になっていない値", sum(n for v, n in nvals.items() if v in N_WRONG), "回のうち止めた",
      sum(n for v, n in nvals.items() if v in N_WRONG and not n_ok(v)),
      "／正しい答え", sum(n for v, n in nvals.items() if v not in N_WRONG | DATE_IN_NUMBER), "回のうち止めた",
      [(v, n) for v, n in nvals.items() if v not in N_WRONG | DATE_IN_NUMBER and not n_ok(v)],
      "／日時の答え（止められない）", [(v, n, not n_ok(v)) for v, n in nvals.items() if v in DATE_IN_NUMBER],
      "／ほかの値", [v for v in nvals if v not in N_WRONG | DATE_IN_NUMBER and not __import__('re').search(r"[0-9人名]", v)])
N_NATURAL = ["3人", "3名", "3名様", "ふたり", "ひとりです", "大人2人と子ども1人", "5人くらい", "十人", "三名", "さんにん", "よにん",
             "2", "4人か5人", "10名前後", "数名", "家族3人", "二人です"]
print("自然な言い方：止めるもの", [v for v in N_NATURAL if not n_ok(v)])
N_WRONG_FORMS = ["はい", "いいえ", "いつでも", "なるべく早く", "大丈夫です", "そうです", "本人です", "わかりません", "答えなかった",
                 "ひとまずお願いします", "やっぱりいいです", "家族で"]
print("数になっていない言い方：止めないもの", [v for v in N_WRONG_FORMS if n_ok(v)])
