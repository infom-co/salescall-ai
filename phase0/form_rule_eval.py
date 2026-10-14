"""日時の型の「値の形」の決まり（flowctl.form_problem）を、これまでの報告の値と、自然な言い方の例に当てる（DESIGN.md 6-2）。"""
import collections, glob, json, re, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from flowctl import normalize_value

from flowctl import form_problem  # 決まりは流れの制御の1か所から読む（ここに書き写さない）


def ok(v):
    return form_problem("datetime", v) is None


vals = collections.Counter()
def walk(x):
    if isinstance(x, dict):
        fl = x.get('flow_log')
        if isinstance(fl, list):
            for e in fl:
                a = (e or {}).get('args') or {}
                if a.get('question_id') == 'Q2' and e.get('op') in ('record_answer', 'confirm_field'):
                    v = a.get('value') if e['op'] == 'record_answer' else a.get('correction')
                    if v and a.get('condition_id') not in ('unknown', 'refused'):
                        vals[normalize_value('datetime', v)] += 1
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
WRONG = {"3人", "はい", "答えなかった", "はいそう"}
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
