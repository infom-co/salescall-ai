"""設定の値の形ごとの、見せ方・入れ方・読み方の1か所（docs/DASHBOARD.md 5-9。Intent.md 5-3・5-4）。

値の形（データベースの setting_value_type）ごとに1行（`FORMS`）。画面は、ここだけを通して値を見せ、入れた欄を値に読む。
値が正しいかの最後の確かめは、データベースの1か所（salescall.setting_value_ok）。ここは、人が入れやすい形と、日本語での見せ方だけを持つ
（形が増えたのにここに無ければ、試験が落ちる）。読めない入力は、理由の文つきで断る（何も書かない）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from salescall.core import phone
from salescall.output import words
from salescall.web.actions import Refused

DAYS: tuple[tuple[str, str], ...] = (("mon", "月"), ("tue", "火"), ("wed", "水"), ("thu", "木"), ("fri", "金"), ("sat", "土"),
                                     ("sun", "日"), ("holiday", "祝日"))
_TIME = re.compile(r"^([01]?\d|2[0-4]):([0-5]\d)$")


def _time(s: str) -> str:
    m = _TIME.match(s.strip().replace("：", ":"))
    if not m or (int(m.group(1)) == 24 and m.group(2) != "00"):
        raise Refused(f"時刻「{s.strip()}」は 09:00 のように書いてください（24:00 まで）")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _range(s: str) -> list[str]:
    parts = re.split(r"\s*[-〜~－]\s*", s.strip())
    if len(parts) != 2:
        raise Refused(f"時間帯「{s.strip()}」は 09:00-18:00 のように書いてください")
    return [_time(parts[0]), _time(parts[1])]


def _ranges(s: str, *, empty_ok: bool) -> list[list[str]]:
    joined = re.sub(r"\s*([-〜~－])\s*", r"\1", s.strip())   # 「09:00 - 12:00」の区切りの前後の空白は、時間帯の中のもの
    items = [x for x in re.split(r"[、,，\s]+", joined) if x.strip()]
    if not items and not empty_ok:
        raise Refused("時間帯を1つ以上入れてください（例：09:00-12:00, 14:00-18:00）")
    return [_range(x) for x in items]


def _show_ranges(v: Any) -> str:
    return "、".join(f"{a}〜{b}" for a, b in v) if v else "なし"


def _whole(s: Any, *, least: int, what: str = "数") -> int:
    t = str(s or "").strip()
    if not re.fullmatch(r"\d+", t) or int(t) < least:
        raise Refused(f"{what}は {least} 以上の整数で入れてください")
    return int(t)


def _ints(s: str, *, least: int, most: int | None = None, increasing: bool = False) -> list[int]:
    items = [x for x in re.split(r"[、,，\s]+", s.strip()) if x]
    if not items:
        raise Refused("1つ以上入れてください（例：1, 3, 7）")
    out = [_whole(x, least=least) for x in items]
    if most is not None and any(x > most for x in out):
        raise Refused(f"{most} までの数で入れてください")
    if increasing and any(b <= a for a, b in zip(out, out[1:])):
        raise Refused("小さい順に、重ならずに入れてください")
    return out


def _gap(form: Any, p: str, special: str) -> Any:
    unit = str(form.get(f"{p}unit") or "")
    if unit == special:
        return special
    if unit not in ("hours", "days"):
        raise Refused("どれかを選んでください")
    return {unit: _whole(form.get(f"{p}n"), least=1, what="時間・日数")}


def _show_gap(v: Any) -> str:
    if isinstance(v, str) and v in words.SETTING_VALUE_WORDS:   # 特別な名前の言葉は output/words の1か所
        return words.SETTING_VALUE_WORDS[v]
    if isinstance(v, dict) and "hours" in v:
        return f"{v['hours']}時間"
    if isinstance(v, dict) and "days" in v:
        return f"{v['days']}日"
    return str(v)


@dataclass(frozen=True)
class Form:
    widget: str                                  # ひな形の入れ方の部品の名前（settings.html の macro）
    show: Callable[[Any], str]                   # 値を日本語で見せる
    read: Callable[[Any, str, Any], Any]         # (画面の欄, 欄の頭, 設定の定義) → 値。読めなければ Refused


def _choice(form: Any, p: str, d: Any) -> str:
    v = str(form.get(f"{p}v") or "")
    if v not in (d.get("choices") or []):
        raise Refused("選べるものの中から選んでください")
    return v


def _phone(form: Any, p: str, d: Any) -> str | None:
    s = str(form.get(f"{p}v") or "").strip()
    if form.get(f"{p}clear"):   # 消すのは「番号を消す」を選んだときだけ（空のまま送って、うっかり消さない）
        if s:
            raise Refused("番号を消すときは、番号の欄を空にしてください")
        return None
    if not s:
        raise Refused("番号を入れてください（消すときは「番号を消す」を選んでください）")
    n = phone.normalize(s)
    if n.e164 is None:
        raise Refused(f"番号として読めません（{n.reason or s}）")
    return n.e164


def _room(form: Any, p: str, d: Any) -> str | None:
    """チャットワークの部屋の番号（数字だけ。データベースの値の形 chatwork_room と同じ決まり）。消すのは「部屋を消す」を選んだときだけ。"""
    s = re.sub(r"\s", "", str(form.get(f"{p}v") or ""))
    if form.get(f"{p}clear"):
        if s:
            raise Refused("部屋を消すときは、部屋の番号の欄を空にしてください")
        return None
    if not re.fullmatch(r"[0-9]{1,20}", s):
        raise Refused("チャットワークの部屋の番号（数字だけ）を入れてください（消すときは「部屋を消す」を選んでください）")
    return s


def _last4(v: Any) -> str:
    """契約者・管理者の電話番号は、画面では下4桁だけ（個人情報。docs/DASHBOARD.md 5-12）。"""
    digits = re.sub(r"\D", "", str(v))
    return f"末尾が {digits[-4:]} の番号" if len(digits) >= 4 else "登録済みの番号"


def _weekly(form: Any, p: str, d: Any) -> dict[str, list[list[str]]]:
    return {k: _ranges(str(form.get(f"{p}{k}") or ""), empty_ok=True) for k, _ in DAYS}


def _limit(form: Any, p: str, d: Any) -> int | None:
    if form.get(f"{p}none"):
        return None
    return _whole(form.get(f"{p}v"), least=1)


FORMS: dict[str, Form] = {
    "boolean": Form("yesno", lambda v: "はい" if v else "いいえ", lambda f, p, d: _yes_no(f.get(f"{p}v"))),
    "integer": Form("number", lambda v: f"{v}", lambda f, p, d: _whole(f.get(f"{p}v"), least=0)),
    "seconds": Form("seconds", lambda v: f"{v}秒", lambda f, p, d: _whole(f.get(f"{p}v"), least=1, what="秒")),
    "ratio": Form("percent", lambda v: f"{round(float(v) * 100, 2):g}%",
                  lambda f, p, d: _percent(f.get(f"{p}v"))),
    "integer_list": Form("list", lambda v: "、".join(str(x) for x in v), lambda f, p, d: _ints(str(f.get(f"{p}v") or ""), least=1)),
    "hour_list": Form("list", lambda v: "、".join(f"{x}時" for x in v),
                      lambda f, p, d: _ints(str(f.get(f"{p}v") or ""), least=1, most=23, increasing=True)),
    "daily_range": Form("range", lambda v: f"{v[0]}〜{v[1]}", lambda f, p, d: _range(str(f.get(f"{p}v") or ""))),
    "daily_ranges": Form("ranges", _show_ranges, lambda f, p, d: _ranges(str(f.get(f"{p}v") or ""), empty_ok=False)),
    "weekly_hours": Form("weekly", lambda v: " ／ ".join(f"{label}：{_show_ranges(v.get(k))}" for k, label in DAYS), _weekly),
    "gap_or_never": Form("gap_never", _show_gap, lambda f, p, d: _gap(f, p, "never")),
    "day_or_gap": Form("gap_day", _show_gap, lambda f, p, d: _gap(f, p, "next_day")),
    "limit": Form("limit", lambda v: "上限なし" if v is None else f"{v}", _limit),
    "phone": Form("phone", lambda v: "まだありません" if v is None else _last4(v), _phone),
    "choice": Form("choice", lambda v: str(v), _choice),
    "chatwork_room": Form("room", lambda v: "まだありません" if v is None else "登録済みの部屋", _room),
}


def _yes_no(s: Any) -> bool:
    if str(s) not in ("1", "0"):
        raise Refused("「はい」か「いいえ」を選んでください")
    return str(s) == "1"


def _percent(s: Any) -> float:
    t = str(s or "").strip().rstrip("%％")
    if not re.fullmatch(r"\d+(\.\d+)?", t) or float(t) > 100:
        raise Refused("0〜100 の割合（%）で入れてください")
    return round(float(t) / 100, 6)


def show(value_type: str, value: Any) -> str:
    f = FORMS.get(value_type)
    return f.show(value) if f else str(value)


def read(value_type: str, form: Any, prefix: str, definition: Any) -> Any:
    f = FORMS.get(value_type)
    if f is None:
        raise Refused("この設定は、まだ画面から変えられません")
    return f.read(form, prefix, definition)
