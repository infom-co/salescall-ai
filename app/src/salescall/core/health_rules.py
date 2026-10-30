"""回線・行・番号・契約者・試行のせいの、状態と出来事の表（DESIGN.md 17章）。持ち主 CL-2。

2026-10-29 管理者の承認：「回線・行・契約者の状態を、それぞれ1つの欄に持ちます。OK」。

- **データベースに触らない**。状態は値（`Line`・`Task`・`Number`・`Tenant`・`Attempt`）、出来事も値（`Event`。E1〜E17）。
- **入口は `apply` の1つだけ**：`apply(今の状態, 出来事, 決まりの値)` が、表（`TABLE`。17-3 の行番号つき）の行を引き、
  `Outcome(次の状態, することの並び, 引いた表の行番号)` を返す。することは「何をするか」の値（`Do`）を並べるだけで、知らせ・修正依頼・
  記録・試行のせいを書く・テスト番号を鳴らす、などは、この部品の外（表を引く入口 core/health の1か所）が行う。知らせには種類（note＝
  core/notify の KINDS の名前）、修正依頼には種類と鍵（fix_kind・fix_key）を、ここで決めて持たせる（入口は中身を見て決め直さない）。
- **表に無い組み合わせは「何もしない」と表に書いたものだけ**（17章の決まり (1)）。それ以外が来たら、決めずに誤りの値
  （`Mistake`）を返す。誤りの値は「記録する」と「直す担当（CL-2）への修正依頼」を持つ。表に行の無い組み合わせは、その段では起きない
  出来事だけ（`GAPS`。来れば誤りの値）。17章を作るうちに見つけた穴は、2026-10-29 に、決定の言葉からの当てはめ【当】の行で埋めた
  （L-29〜L-33・L-20・R-15〜R-21・N-6・C-9・C-10 と、L-12・L-19・C-8・V-2 を広げた所）。
- **時刻の数は表に書かない**：テスト番号をあける長さの並び（設定 `line.test_ring_gaps`）・鳴らしてよい時間（`line.test_ring_hours`）・
  違う行の数（`stop.trouble.stop_after_rows`）・番号の決まった順（`dial.failed_retry_days`）・同じお客様への最短の間隔
  （`gate.same_contact_min_interval`）は、決まりの値（`Rules`）で受け取る（`Rules.from_settings` は設定の名前のまま受ける）。
  一時的な異常が「続いている」とみなす長さ（core/errors の LASTING）と、PC の答えを待つ長さ（link/protocol の REPLY_WAIT）は、
  それぞれの1か所から読む。
- **17-7-2 の (1)・(2)・(4) と N-4 は、2026-10-29 の管理者の決定のとおり1つの形にした**（切り替えの引数は持たない）：
  (1) 回線のせい・休みの知らせは案B（休みに入った時からお客様へかけない。知らせは、次のテストでも鳴らなかったとき＝L-17。休みの中身に
  「知らせたか」を持つ）／(2) 声で止める違うお客様の数は2人（`AUDIO_STOP_AFTER_CUSTOMERS`。回線の異常3件の基準は通さない）／
  (4) 声のかけ直しで声の通る回線が来ないときは案A（声が聞き取れなかった電話から並びの1つ目のうちに来なければ、同じ回線でお詫びと
  ともにかけ直す。続けて2回までのまま）／N-4 は案A（今の形どおり。分からない電話を番号のせいに数えると今日の分を使い切るあいだ、
  その番号の行はどの回線でもかけない印を付け、決まれば外す）。
- 4段目（2026-10-29）：入口（core/health）が、することを行い、読み手（締め・次の動き・門・候補・回線ごとの処理）は状態の欄を読む。

**読み方**（17章に書き方の無かった所を、書いてある決まりのとおりに読んだもの。管理者のご判断・17章の直しで変える）
- (1) L-20（休みが鳴らしてよい時間で1時間）は、休みの中身に、その時刻（休みに入った時刻＋鳴らしてよい時間で1時間＝fix_at）を
  持ち、次のテストの時刻と早いほうで E10 を引く（あける長さの分だけ遅れない）。
- (2) L-28（E17）の「この続き・この休みで1回」は、元気の回線には続きも休みも無いので、かけられなくなった時刻（E17 の since）ごとに1回。
- (3) 声の続き m・声で止めた回数 j は、どの段でも持ち越す（17-1-1 の確かめ中の中身の列に無いが、L-5 が続けて L-3 を引くため）。
  元気の中身は m・j だけなので、元気へ戻すとテストの回数 t は持たない（L-24 は t を書いていない）。
- (4) 行のこちらの不具合の続き n とその種類は、判定待ち・復旧待ち・休みでも持ち越す（17-1-2 の中身の列に無いが、R-6「n＝1 のまま」）。
  R-7（復旧待ちを手で戻す）は n を書いていないので変えない。
- (5) すぐのかけ直しの「同じ行で続けて2回まで」（R-6・R-11）は、予定どおりの中身に回数が無いので、今の作りの1か所
  （salescall.resume_allowed）の答えを E4 で受ける（R-6・R-7・R-10 は「お詫びとともにすぐかけ直す」を頼み、2回までは受け手が当てる）。
- (6) R-6 の「終えた電話より後」の E5 は、こちらの不具合で終えた電話の終わり以降に話し始めた電話。
- (7) 休みの回線でテスト番号を鳴らしたあとは、答えを待つ長さ（REPLY_WAIT）を次に動く時刻にする（休みの中身に答え待ちの時刻が無い）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, NamedTuple

from salescall.core import calling_hours
from salescall.core.errors import LASTING, PHONE_FIXER
from salescall.link.protocol import REPLY_WAIT

__all__ = ["AUDIO_STOP_AFTER_CUSTOMERS", "Attempt", "Blame", "Do", "Event", "GAPS", "Line", "LineStage", "Mistake", "NO_NUMBER_ABOUT",
           "Number", "NumberStage", "Outcome", "RECEIVES", "Rules", "SETTING_KEYS", "TABLE", "Task", "TaskStage", "Tenant", "TenantStage", "Wait",
           "apply", "next_at", "subject_of"]


# ─────────────────────────────────────────────────────────────────────────────
# 決まりの値（時刻の数と、管理者に伺っている所の答え）
# ─────────────────────────────────────────────────────────────────────────────

# 表が読む設定の名前（今の設定の名前のまま。`Rules.from_settings` が、この名前で受ける）
SETTING_KEYS = {
    "test_ring_gaps": "line.test_ring_gaps",              # テスト番号をあける長さの並び（秒。初め 15→30→60→120→240分）
    "test_ring_hours": "line.test_ring_hours",            # テスト番号を鳴らしてよい時間（"HH:MM" の組の並び。初め 7:00〜22:00）
    "trouble_stop_after_rows": "stop.trouble.stop_after_rows",   # こちらの不具合で止める、違う行の数（初め3）
    "failed_retry_days": "dial.failed_retry_days",        # 番号の決まった順（何日目にかけるかの並び。初め [1, 1, 2, 4]）
    "same_contact_min_interval": "gate.same_contact_min_interval",   # 同じお客様への最短の間隔（秒。初め5分）
}


# 声が聞き取れずに終えた電話が、違うお客様で何本続いたら回線を止めるか（1か所。設定は作らない）。2026-10-29 管理者の決定：
# 「2人」（違うお客様の電話が2本続いたら声の不調・休み。回線の異常3件の基準は通さない）
AUDIO_STOP_AFTER_CUSTOMERS = 2


@dataclass(frozen=True)
class Rules:
    """表が読む決まりの値（時刻の数）。どれも初めの値を持たない（設定から読んで渡す＝`from_settings`）。"""
    test_ring_gaps: tuple[timedelta, ...]
    test_ring_hours: tuple[tuple[int, int], ...]   # 0時からの秒の組（終わりが始まりより前なら日をまたぐ。calling_hours.daily_spans）
    trouble_stop_after_rows: int
    failed_retry_days: tuple[int, ...]
    same_contact_min_interval: timedelta
    lasting: timedelta          # 一時的な異常が「続いている」とみなす長さ（core/errors の LASTING）
    reply_wait: timedelta       # テスト番号の答えを待つ長さ（PC とのやり取りの期限。link/protocol の REPLY_WAIT）
    audio_stop_after_customers: int   # 声で止める違うお客様の数（`AUDIO_STOP_AFTER_CUSTOMERS`）

    @classmethod
    def from_settings(cls, values: Mapping[str, Any]) -> Rules:
        """設定の名前（`SETTING_KEYS` の右）と、設定の値の形のまま受ける（秒の数・"HH:MM" の組・日の並び）。"""
        v = {name: values[key] for name, key in SETTING_KEYS.items()}
        return cls(test_ring_gaps=tuple(timedelta(seconds=float(g)) for g in v["test_ring_gaps"]),
                   test_ring_hours=tuple((calling_hours.clock_seconds(a), calling_hours.clock_seconds(b))
                                         for a, b in v["test_ring_hours"]),
                   trouble_stop_after_rows=int(v["trouble_stop_after_rows"]),
                   failed_retry_days=tuple(int(d) for d in v["failed_retry_days"]),
                   same_contact_min_interval=timedelta(seconds=float(v["same_contact_min_interval"])),
                   lasting=LASTING, reply_wait=timedelta(seconds=REPLY_WAIT),
                   audio_stop_after_customers=AUDIO_STOP_AFTER_CUSTOMERS)

    def gap(self, t: int) -> timedelta:
        """続けて t 回（1から）鳴らなかった・見分けられなかったあとにあける長さ：並びの t 番目（並びの最後で止まる）。1か所。"""
        return self.test_ring_gaps[min(t, len(self.test_ring_gaps)) - 1]

    def first_gap(self) -> timedelta:
        """並びの1つ目（初め15分）。確かめ中 (a) の長さ・PC が試さなかったあと・試せないまま知らせるまで（【当(5)】【当(9)】【当(10)】）。"""
        return self.test_ring_gaps[0]


# ─────────────────────────────────────────────────────────────────────────────
# 状態（17-1）
# ─────────────────────────────────────────────────────────────────────────────

class LineStage(Enum):
    HEALTHY = "元気"
    CHECK_A = "確かめ中 (a)"        # 同じ回線の次のお客様の電話を待つ
    CHECK_B = "確かめ中 (b)"        # テスト番号を鳴らす時を待つ
    CHECK_C = "確かめ中 (c)"        # テスト番号の答えを待つ
    LINE_REST = "回線のせい・休み"
    AUDIO_REST = "声の不調・休み"
    OUT_OF_USE = "使用停止"   # 2026-10-29 管理者：「使用停止でOK」（前の名前は「外した」）


CHECKING = frozenset({LineStage.CHECK_A, LineStage.CHECK_B, LineStage.CHECK_C})
RESTS = frozenset({LineStage.LINE_REST, LineStage.AUDIO_REST})


class Wait(Enum):
    """確かめ中 (b) と休みの、待つ理由（1つ）。"""
    NO_NUMBER = "テスト番号が無い"          # 時刻を持たない（入るときに知らせ、E14 を待つ）
    OFF_HOURS = "鳴らしてよい時間の外"      # 次＝鳴らしてよい時間の始まり
    GAP = "あける長さ"                      # 次＝前のテスト＋並びの t 番目
    WITHHELD = "PC の部品が試さなかった"    # 次＝並びの1つ目のあと（【当(9)】）
    OTHER_TEST = "ほかの回線がテスト番号を鳴らしている途中"   # 次＝そのテストの答えのあと
    ANSWER = "テスト番号の答え"              # 休みでテスト番号を鳴らした：次＝答えの期限（過ぎたら E10 で L-19。確かめ中は (c) の段）


@dataclass(frozen=True)
class Line:
    """回線の欄（17-1-1）。声の続き（audio＝違うお客様の番号の組。m はその数）と、声で止めた続きの回数 j は、回線が外れるまで
    どの段でも持ち越す（L-5 が続けて L-3 を引くため）。テストの回数 t・続き・待つ理由・次に動く時刻は、元気でない段だけが持つ。"""
    stage: LineStage
    audio: frozenset[str] = frozenset()
    j: int = 0
    t: int = 0
    run_start: datetime | None = None                 # 続きの始まり（最初の鳴らなかった電話の時刻）
    run: tuple[tuple[str, str], ...] = ()             # 続きの電話（試行, 番号）
    wait: Wait | None = None
    next_at: datetime | None = None
    entered_at: datetime | None = None                # 休みに入った時刻
    tested_at: datetime | None = None                 # 最後にテスト番号の答え（鳴った・鳴らなかった・見分けられない）が来た時刻
    told: frozenset[str] = frozenset()                # この続き・この休みで、もう出した知らせ・修正依頼（1回だけのもの）
    fix_at: datetime | None = None                    # 休み：入った時刻＋鳴らしてよい時間で1時間（L-20 の修正依頼の時刻）
    removed: tuple[str, datetime, str] | None = None  # 外した管理者か契約者・時刻・理由

    @property
    def m(self) -> int:
        return len(self.audio)


class TaskStage(Enum):
    ON_PLAN = "予定どおり"
    AWAIT_DECISION = "判定待ち"
    AWAIT_RECOVERY = "復旧待ち"
    RESTING = "休み"
    VOICE_REDIAL = "声のかけ直し待ち"


@dataclass(frozen=True)
class Task:
    """行の欄（17-1-2。この章の分だけ）。次の予定（plan）は 7-2 の持ち物で、この表は中を見ずにそのまま持つ。
    こちらの不具合の続き n（0か1）とその種類は、どの段でも持ち越す（R-6 の「n＝1 のまま」）。"""
    stage: TaskStage
    plan: Any = None
    n: int = 0
    n_kind: str | None = None
    line: str | None = None             # 判定待ちの回線・声が聞き取れなかった回線
    attempt: str | None = None          # 判定待ちの試行・こちらの不具合で終えた試行
    kind: str | None = None             # 復旧待ちの不具合の種類 k
    kinds: frozenset[str] = frozenset()   # 休みの、続いた種類
    answered: bool = False              # お客様が出たあとに終えたか
    ended_at: datetime | None = None    # こちらの不具合で終えた電話の終わり
    since: datetime | None = None       # 休み始めた・声が聞き取れなかった時刻
    next_at: datetime | None = None     # 声のかけ直し待ちの、同じ回線でかけ直す時刻（2026-10-29 決定 (4) 案A）


class NumberStage(Enum):
    NONE = "続き無し"
    RUN = "続きあり"
    DEAD = "使えない"


@dataclass(frozen=True)
class Number:
    """番号の欄（17-1-3）。n は番号のせいと決まった数だけ。unknown は、まだ分からない試行（N-4 の印の材料）。"""
    stage: NumberStage
    n: int = 0
    first_day: date | None = None
    last_end: datetime | None = None
    next_day: date | None = None          # 決まった順の次の日
    not_before: datetime | None = None    # 同じ日なら、最後の失敗の終わり＋同じお客様への最短の間隔
    unknown: frozenset[str] = frozenset()
    unknown_since: datetime | None = None   # まだ分からない試行の、いちばん早い電話の終わり（印の「最初の失敗の日」の材料）
    unknown_last: datetime | None = None    # まだ分からない試行の、いちばん遅い電話の終わり（印の「前の失敗の日」の材料）
    hold: bool = False                    # N-4 の印（2026-10-29 決定 案A）
    dead_at: datetime | None = None
    next_at: datetime | None = None       # 印があるあいだ：印を見直す時刻＝次の日の始まり（N-7。17-3-6 の【当】）


class TenantStage(Enum):
    NORMAL = "通常"
    STOPPED = "こちらの不具合で止め"


@dataclass(frozen=True)
class Tenant:
    """契約者の欄（17-1-4）。rows は種類 k ごとの「違う行」の組（r_k はその数）。stopped は止めている種類の組（【当(8)】）。"""
    stage: TenantStage
    rows: tuple[tuple[str, frozenset[str]], ...] = ()
    stopped: frozenset[str] = frozenset()
    since: datetime | None = None

    def r(self, k: str) -> frozenset[str]:
        return dict(self.rows).get(k, frozenset())

    def with_r(self, k: str, tasks: frozenset[str]) -> tuple[tuple[str, frozenset[str]], ...]:
        d = dict(self.rows)
        if tasks:
            d[k] = tasks
        else:
            d.pop(k, None)
        return tuple(sorted(d.items()))


class Blame(Enum):
    EMPTY = "空"
    UNKNOWN = "分からない"
    NUMBER = "番号"
    LINE = "回線"
    OURS = "こちら"


@dataclass(frozen=True)
class Attempt:
    """試行のせいの欄（17-1-5）。書けるのは入口だけ・1回だけ（番号・回線・こちらになったら変えない）。"""
    stage: Blame


SUBJECTS = {Line: "回線", Task: "行", Number: "番号", Tenant: "契約者", Attempt: "試行のせい"}


def subject_of(state: Any) -> str | None:
    return SUBJECTS.get(type(state))


# ─────────────────────────────────────────────────────────────────────────────
# 出来事（17-2）
# ─────────────────────────────────────────────────────────────────────────────

EVENT_NAMES = {
    "E1": "お客様のところで電話が鳴った（こちらの不具合で終えた電話は除く）",
    "E2": "お客様のところで鳴らなかった（こちらの不具合ではない）",
    "E3": "こちらのプログラムの不具合で終えた（種類 k）",
    "E4": "声が聞き取れずに終えた（E1 でもある）",
    "E5": "こちらの不具合なしにお客様と話し終えた（E1 でもある）",
    "E6": "テスト番号が鳴った",
    "E7": "テスト番号が鳴らなかった",
    "E8": "テスト番号の答えを見分けられない",
    "E9": "PC の部品がテストを試さなかった（話している途中・つながっていない・PC の上の緊急停止）",
    "E10": "次に動く時刻が来た（鳴らしてよい時間の始まり・テスト番号の答えの期限を過ぎた、を含む）",
    "E11": "種類 k を直したと記録され、そのあとプログラムの版が替わった",
    "E12": "管理者か契約者が、理由を書いて手で再開する・戻す・解く",
    "E13": "管理者か契約者が、回線を外した",
    "E14": "テスト番号が入った",
    "E15": "判定待ちの行に、ほかの回線がかけた",
    "E16": "試行のせいが決まった（分からない → 番号／回線）",
    "E17": "回線が「通話中」と答え続けて、かけられない",
    # 17-2 に記号の無かった出来事（表の R-12・R-14 が使う。2026-10-29 に E18・E19 の名前を付けた）
    "E18": "声が通ると分かっている回線が空いた（R-12）",
    "E19": "お客様の「もうかけないで」・業務の終わり（R-14）",
}

COMPOSITE = frozenset({"E4", "E5"})   # E1 でもある出来事（自分の電話のとき。読み方：E1 の行を先に、続けて E4・E5 の行）


@dataclass(frozen=True)
class Event:
    """出来事の値。code は EVENT_NAMES の鍵。中身は出来事ごとに要るものだけを入れる（`_EVENT_NEEDS` が確かめる）。"""
    code: str
    at: datetime
    attempt: str | None = None
    number: str | None = None
    line: str | None = None
    task: str | None = None
    kind: str | None = None                 # E3・E11：不具合の種類 k
    rang: bool | None = None                # E3：お客様のところで鳴ったあとか
    answered: bool | None = None            # E3：お客様が出たあとか
    own: bool = True                        # E5：受ける行の電話か（行の表。ほかの行の電話なら False）
    started_at: datetime | None = None      # E5：話し始めの時刻
    blame: str | None = None                # E16：「番号」か「回線」
    call_at: datetime | None = None         # E16：その試行の電話の終わり
    test_number: bool | None = None         # E10・E14：いまテスト番号があるか
    other_test_until: datetime | None = None   # E10・E14：ほかの回線のテストの答えの期限（鳴らしている途中なら）
    since: datetime | None = None           # E17：かけられなくなった時刻
    by: str | None = None                   # E12・E13：管理者か契約者
    reason: str | None = None               # E12・E13：書いた理由
    resume_allowed: bool | None = None      # E4：すぐのかけ直しが、同じ行で続けて2回のうちか（今の作り salescall.resume_allowed の答え）


_EVENT_NEEDS: dict[str, tuple[str, ...]] = {
    "E1": ("attempt", "number"), "E2": ("attempt", "number", "line"), "E3": ("attempt", "kind", "task", "rang", "answered"),
    "E4": ("attempt", "number", "line"), "E5": ("attempt", "number", "started_at"),
    "E6": (), "E7": (), "E8": (), "E9": (), "E10": (), "E11": ("kind",), "E12": ("by", "reason"), "E13": ("by", "reason"),
    "E14": (), "E15": ("line",), "E16": ("attempt", "blame", "call_at"), "E17": ("since",), "E18": ("line",),
    "E19": (),
}


# ─────────────────────────────────────────────────────────────────────────────
# すること（値で返すだけ）・答え・誤り
# ─────────────────────────────────────────────────────────────────────────────

NOTIFY = "知らせる"
FIX = "修正依頼"
RECORD = "記録する"
WRITE_BLAME = "試行のせいを書く"
DECIDED = "試行のせいが決まった（E16 を出す）"
RING = "テスト番号を鳴らす"
STOP_CALLS = "新しい電話を止める（話している電話は切らない）"
RESUME_CALLS = "新しい電話を戻す"
PLAN = "行の次の予定を頼む"
REDIAL = "お詫びとともにすぐかけ直す"
COUNT = "行の回数に数える"
CLOSE = "判定待ちの電話を待たずに締める（予定は書かない）"
DOS = (NOTIFY, FIX, RECORD, WRITE_BLAME, DECIDED, RING, STOP_CALLS, RESUME_CALLS, PLAN, REDIAL, COUNT, CLOSE)

BOTH = ("契約者", "管理者")
TABLE_FIXER = "CL-2"   # 表に無い組み合わせの直す担当（17章の決まり (1)）


@dataclass(frozen=True)
class Do:
    """することの1つ（値だけ。データベースにも電話にも触らない）。what は DOS のどれか。"""
    what: str
    detail: tuple[tuple[str, Any], ...] = ()

    def get(self, key: str) -> Any:
        return dict(self.detail).get(key)


def _do(what: str, **detail: Any) -> Do:
    if what not in DOS:
        raise ValueError(f"することの名前が表に無い：{what!r}")
    return Do(what, tuple(sorted(detail.items())))


class Outcome(NamedTuple):
    state: Any
    actions: tuple[Do, ...]
    rows: tuple[str, ...]


@dataclass(frozen=True)
class Mistake:
    """表を引けなかった（決めずに返す誤りの値）。kind：
    「表に無い」＝表に行も「何もしない」も無い組み合わせ（gap は GAPS の番号。分かっている穴なら）／「重なり」＝2つの行が当たった／「受けない出来事」＝その欄の表が受けない出来事／「時刻の前」＝次に動く時刻を
    持たない・まだ来ていないのに E10／「中身の誤り」＝状態か出来事の中身が足りない・段と合わない。"""
    kind: str
    why: str
    subject: str | None
    stage: str | None
    event: str
    gap: str | None = None

    @property
    def actions(self) -> tuple[Do, ...]:
        """誤りとして記録し、直す担当（CL-2）へ修正依頼を出す（17章の決まり (1)）。"""
        about = f"表を引けない（{self.kind}）：{self.subject}・{self.stage}・{self.event}。{self.why}"
        return (_do(RECORD, about=about), _do(FIX, fixer=TABLE_FIXER, fix_kind="state_table",
                                              fix_key=f"state_table:{self.subject}:{self.stage}:{self.event}:{self.kind}", about=about))


# ─────────────────────────────────────────────────────────────────────────────
# 時刻の決まり（1か所ずつ）
# ─────────────────────────────────────────────────────────────────────────────

def _in_hours(rules: Rules, at: datetime) -> bool:
    return calling_hours.in_daily(calling_hours.local(at), rules.test_ring_hours)


def _hours_start(rules: Rules, at: datetime) -> datetime:
    """at のあとで、鳴らしてよい時間が始まる時刻（時間の外で呼ぶ。範囲の読み方は calling_hours.daily_spans の1か所）。"""
    here = calling_hours.local(at)
    starts = [datetime.combine(here.date() + timedelta(days=d), time()) + timedelta(seconds=a)
              for d in (0, 1) for a, _ in calling_hours.daily_spans(rules.test_ring_hours)]
    return calling_hours.from_local(min(s for s in starts if s > here))


def _lasting_at(rules: Rules, start: datetime) -> datetime:
    """start から、鳴らしてよい時間で数えて「続いている」とみなす長さ（1時間）がたつ時刻（L-20。calling_hours.daily_seconds の
    逆。範囲の読み方は calling_hours.daily_spans の1か所）。"""
    here, left = calling_hours.local(start), rules.lasting.total_seconds()
    spans = calling_hours.daily_spans(rules.test_ring_hours)
    for d in range(0, 8):
        day = datetime.combine(here.date() + timedelta(days=d), time())
        for a, b in spans:
            lo, hi = max(here, day + timedelta(seconds=a)), day + timedelta(seconds=b)
            if hi <= lo:
                continue
            if (hi - lo).total_seconds() >= left:
                return calling_hours.from_local(lo + timedelta(seconds=left))
            left -= (hi - lo).total_seconds()
    raise ValueError("鳴らしてよい時間が無い")


def _rest(stage: LineStage, rules: Rules, at: datetime, **contents: Any) -> Line:
    """休みに入れる（L-3・L-10・L-25 の1か所）：入った時刻と、L-20 の時刻を持つ。"""
    return Line(stage, entered_at=at, fix_at=_lasting_at(rules, at), **contents)


def _local_day(at: datetime) -> date:
    return calling_hours.local(at).date()


def _number_day(rules: Rules, first: date, last: date, c: int) -> date:
    """番号のせいが c 回続いたあと、次にかけてよい日（N-1。1か所）：「最初の失敗の日＋並び」と「前の失敗の日＋並びの差」の遅いほう。"""
    days = rules.failed_retry_days
    return max(first + timedelta(days=days[c] - 1), last + timedelta(days=days[c] - days[c - 1]))


# ─────────────────────────────────────────────────────────────────────────────
# 表の行（17-3）
# ─────────────────────────────────────────────────────────────────────────────

class _Step(NamedTuple):
    state: Any
    actions: tuple[Do, ...] = ()
    also: tuple[str, ...] = ()     # この行と一緒に当たった行（L-20・N-4 など）


def _once(told: frozenset[str], key: str, acts: tuple[Do, ...]) -> tuple[frozenset[str], tuple[Do, ...]]:
    """「この続き・この休みで1回だけ」の知らせ・修正依頼（1か所）。"""
    if key in told:
        return told, ()
    return told | {key}, acts


# ── 回線 ──

def _decide_run(line: Line, blame: str) -> tuple[Do, ...]:
    """続きの分からない試行を、すべて番号か回線にする（E16 を出す。L-5・L-9・L-10・L-14 の1か所）。"""
    return tuple(_do(DECIDED, attempt=a, blame=blame) for a, _ in line.run)


def _healthy(line: Line) -> Line:
    """元気にする（中身は m と j だけ。t・続き・待つ理由・次に動く時刻・知らせた印は持たない）。"""
    return Line(LineStage.HEALTHY, audio=line.audio, j=line.j)


# テスト番号が無いときの知らせの文（1か所。L-7・L-8・L-15・L-23）。実際に起きることだけを書く（見直し22 の 4）。2026-10-29 管理者：「一時的な
# testcallは環境設定に追加」＝管理者がシステムの初めの値に仮の番号を入れ、契約者が自分の番号を入れたら、そちらを使う
NO_NUMBER_ABOUT = ("テスト番号が無いので、この回線の確かめ（回線のせいか番号のせいか・休んだ回線を再開してよいか）を決められません。"
                   "管理者か契約者がテスト番号を入れてください（管理者が初めの値として入れた番号を、契約者が自分の番号を入れるまで使います）。"
                   "入るまで、この回線の確かめは待ちます（まだ分からない電話の行は、番号の回数にも回線の異常にも数えません）。")


def _choose_wait(line: Line, ev: Event, rules: Rules) -> tuple[Wait | None, datetime | None, tuple[Do, ...], frozenset[str]]:
    """テスト番号を鳴らす前の、待つ理由を見直す（L-7・L-8・L-13・L-15・L-23 の1か所）。順は L-8 のとおり。
    待つ理由が無ければ (None, …)＝いま鳴らす。"""
    told = line.told
    if not (ev.code == "E14" or ev.test_number):   # E14＝テスト番号が入った
        told, acts = _once(told, "テスト番号が無い",
                           (_do(NOTIFY, to=BOTH, note="line_undecided",
                                about=NO_NUMBER_ABOUT),))
        return Wait.NO_NUMBER, None, acts, told
    if not _in_hours(rules, ev.at):
        return Wait.OFF_HOURS, _hours_start(rules, ev.at), (), told
    if line.t >= 1 and line.tested_at is not None and ev.at < line.tested_at + rules.gap(line.t):
        return Wait.GAP, line.tested_at + rules.gap(line.t), (), told
    if ev.other_test_until is not None and ev.other_test_until > ev.at:
        return Wait.OTHER_TEST, ev.other_test_until, (), told
    return None, None, (), told


def _ring_or_wait(line: Line, ev: Event, rules: Rules, *, checking: bool) -> _Step:
    """待つ理由を見直し、無ければテスト番号を鳴らす（確かめ中は (c) へ。休みは休みのまま答えを待つ）。"""
    wait, nxt, acts, told = _choose_wait(line, ev, rules)
    if wait is not None:
        stage = LineStage.CHECK_B if checking else line.stage
        return _Step(replace(line, stage=stage, wait=wait, next_at=nxt, told=told), acts)
    stage, wait = (LineStage.CHECK_C, None) if checking else (line.stage, Wait.ANSWER)
    return _Step(replace(line, stage=stage, wait=wait, next_at=ev.at + rules.reply_wait, told=told), acts + (_do(RING),))


def _untried(line: Line, since: datetime, at: datetime, rules: Rules, key: str,
             fix_kind: str = "test_ring_undecided") -> tuple[frozenset[str], tuple[Do, ...]]:
    """試せない・かけられないまま並びの1つ目（初め15分）を過ぎたら、1回知らせて修正依頼（L-12・L-19・L-28 の1か所。【当(9)(10)】）。
    修正依頼の種類は、テスト番号を試せない（test_ring_undecided。見分けられないと同じ1つ）と、回線が「通話中」のまま（line_in_call）。"""
    if at - since < rules.first_gap():
        return line.told, ()
    return _once(line.told, key, (_do(NOTIFY, to=BOTH, note="line_undecided", about="テスト番号を試せない・かけられないまま続いています"),
                                  _do(FIX, fixer=PHONE_FIXER, fix_kind=fix_kind, fix_key=fix_kind,
                                      about="電話をつなぐ部品：試せない・かけられないまま続いている")))


def _untried_since(line: Line) -> datetime:
    """試せないまま、を数え始める時刻：続きの始まりか休みに入った時刻（テストを試していれば、そのあと）。"""
    start = line.run_start if line.stage in CHECKING else line.entered_at
    return max(t for t in (start, line.tested_at) if t is not None)


def _l1(line: Line, ev: Event, rules: Rules) -> _Step:
    return _Step(replace(line, audio=frozenset(), j=0 if ev.code == "E5" else line.j))


def _l2(line: Line, ev: Event, rules: Rules) -> _Step:
    return _Step(Line(LineStage.CHECK_A, audio=line.audio, j=line.j, t=0, run_start=ev.at,
                      run=((ev.attempt, ev.number),), next_at=ev.at + rules.first_gap()))


def _l3(line: Line, ev: Event, rules: Rules) -> _Step:
    audio = line.audio | {ev.number}
    if line.j >= 1:   # 声で再開したあと、まだ声が聞き取れて話し終えた電話が無い：1本で止め直す（【当(3)】）
        stop = True
    else:
        stop = len(audio) >= rules.audio_stop_after_customers
    if not stop:
        return _Step(replace(line, audio=audio))
    j = line.j + 1
    nxt = ev.at if j == 1 else ev.at + rules.gap(j - 1)
    return _Step(_rest(LineStage.AUDIO_REST, rules, ev.at, audio=audio, j=j, t=0, next_at=nxt),
                 (_do(RECORD, about="声が聞き取れずに終えた電話が違うお客様で続いたので、回線を休ませた"),
                  _do(NOTIFY, to=BOTH, note="stopped", cause="poor_audio",
                      about="声が聞き取れない電話が続いたので、この回線を休ませます（テスト番号が鳴れば再開します）")))


def _l5(line: Line, ev: Event, rules: Rules) -> _Step:
    return _Step(_healthy(line), _decide_run(line, "番号"))


def _l6(line: Line, ev: Event, rules: Rules) -> _Step:
    return _Step(replace(line, run=line.run + ((ev.attempt, ev.number),)))


def _l7_l8(line: Line, ev: Event, rules: Rules) -> _Step:
    return _ring_or_wait(line, ev, rules, checking=True)


def _l9(line: Line, ev: Event, rules: Rules) -> _Step:
    return _Step(_healthy(line), _decide_run(line, "番号"))


def _l10(line: Line, ev: Event, rules: Rules) -> _Step:
    t = line.t + 1
    # 知らせは、まだ出さない（次のテストでも鳴らなかったとき＝L-17。2026-10-29 決定 (1) 案B）
    acts = _decide_run(line, "回線") + (_do(RECORD, about="テスト番号が鳴らなかったので、回線のせいで休ませた"),)
    return _Step(_rest(LineStage.LINE_REST, rules, ev.at, audio=line.audio, j=line.j, t=t, next_at=ev.at + rules.gap(t),
                       tested_at=ev.at), acts)


def _l11(line: Line, ev: Event, rules: Rules) -> _Step:
    t = line.t + 1
    told, acts = _once(line.told, "見分けられない",
                       (_do(NOTIFY, to=BOTH, note="line_undecided", about="テスト番号の答えを見分けられませんでした"),
                        _do(FIX, fixer=PHONE_FIXER, fix_kind="test_ring_undecided", fix_key="test_ring_undecided",
                            about="電話をつなぐ部品：テスト番号の答えを見分けられない")))
    return _Step(replace(line, stage=LineStage.CHECK_B, t=t, wait=Wait.GAP, next_at=ev.at + rules.gap(t), tested_at=ev.at,
                         told=told), acts)


def _l12(line: Line, ev: Event, rules: Rules) -> _Step:
    told, acts = _untried(line, _untried_since(line), ev.at, rules, "試せない")
    return _Step(replace(line, stage=LineStage.CHECK_B, wait=Wait.WITHHELD, next_at=ev.at + rules.first_gap(), told=told), acts)


def _l14_l22(line: Line, ev: Event, rules: Rules) -> _Step:
    return _Step(Line(LineStage.OUT_OF_USE, removed=(ev.by, ev.at, ev.reason)),
                 _decide_run(line, "回線") + (_do(RECORD, about=f"回線を外した：{ev.reason}"),))


def _with_l20(line: Line, ev: Event, rules: Rules, step: _Step) -> _Step:
    """L-20（1か所）：休みに入ってから、鳴らしてよい時間で数えて1時間（errors.LASTING）たっても鳴らない：電話をつなぐ部品の直す担当へ
    修正依頼（1つ）。休みの E10 のどの行でも見る。"""
    lasting = calling_hours.daily_seconds(calling_hours.local(line.entered_at), calling_hours.local(ev.at), rules.test_ring_hours)
    if lasting < rules.lasting.total_seconds():
        return step
    told, acts = _once(step.state.told, "1時間", (_do(FIX, fixer=PHONE_FIXER, fix_kind="line", fix_key="error:line:{tenant}",
                                                      about="電話をつなぐ部品：回線が1時間以上休んだまま"),))
    return _Step(replace(step.state, told=told), step.actions + acts, step.also + (("L-20",) if acts else ()))


def _test_due(line: Line, ev: Event) -> bool:
    """休みの E10 が、次のテスト（か答えの期限）の時刻のものか（L-20 の時刻だけのものでないか）。"""
    return line.next_at is not None and ev.at >= line.next_at


def _l15_l23(line: Line, ev: Event, rules: Rules) -> _Step:
    return _with_l20(line, ev, rules, _ring_or_wait(line, ev, rules, checking=False))


def _l20(line: Line, ev: Event, rules: Rules) -> _Step:
    step = _with_l20(line, ev, rules, _Step(line))
    return _Step(step.state, step.actions)   # この行そのものが L-20


def _l16(line: Line, ev: Event, rules: Rules) -> _Step:
    return _Step(_healthy(line), (_do(RECORD, about="テスト番号が鳴ったので、回線を再開した"),
                                  _do(NOTIFY, to=("契約者",), note="resumed", about="テスト番号が鳴ったので、この回線を再開しました")))


def _l17(line: Line, ev: Event, rules: Rules) -> _Step:
    t = line.t + 1
    # 次のテストでも鳴らなかった：この休みで1回、契約者と管理者に知らせる（休みの中身の「知らせたか」＝told の「回線のせい」）
    told, acts = _once(line.told, "回線のせい", (_do(NOTIFY, to=BOTH, note="stopped", cause="line_failure",
                                                     about="テスト番号が続けて鳴らないので、この回線を休ませています"),))
    return _Step(replace(line, t=t, wait=None, next_at=ev.at + rules.gap(t), tested_at=ev.at, told=told), acts)


def _l18_l26(line: Line, ev: Event, rules: Rules) -> _Step:
    t = line.t + 1
    told, acts = _once(line.told, "見分けられない",
                       (_do(NOTIFY, to=BOTH, note="line_undecided", about="テスト番号の答えを見分けられませんでした"),
                        _do(FIX, fixer=PHONE_FIXER, fix_kind="test_ring_undecided", fix_key="test_ring_undecided",
                            about="電話をつなぐ部品：テスト番号の答えを見分けられない")))
    return _Step(replace(line, t=t, wait=None, next_at=ev.at + rules.gap(t), tested_at=ev.at, told=told), acts)


def _l19(line: Line, ev: Event, rules: Rules) -> _Step:
    told, acts = _untried(line, _untried_since(line), ev.at, rules, "試せない")
    step = _Step(replace(line, wait=Wait.WITHHELD, next_at=ev.at + rules.first_gap(), told=told), acts)
    return _with_l20(line, ev, rules, step) if ev.code == "E10" else step


def _l30(line: Line, ev: Event, rules: Rules) -> _Step:
    # テスト番号が入った：休みのまま、次に動く時刻＝いま（L-15・L-23 をすぐ引く）
    return _Step(replace(line, wait=None, next_at=ev.at))


def _record_only(state: Any, ev: Event, rules: Rules) -> _Step:
    """何もしない（理由の記録だけ。L-32・R-21・C-9）。"""
    return _Step(state, (_do(RECORD, about=f"{ev.by}が手で再開・戻す・解くを押したが、この状態では何もしない：{ev.reason}"),))


def _l33(line: Line, ev: Event, rules: Rules) -> _Step:
    # 外す前からかけていた電話が鳴らずに終わった：その試行のせい＝回線（L-14 の文のとおり。E16 を出す）
    return _Step(line, (_do(DECIDED, attempt=ev.attempt, blame="回線"),))


def _l21(line: Line, ev: Event, rules: Rules) -> _Step:
    return _Step(Line(LineStage.HEALTHY), (_do(RECORD, about=f"{ev.by}が手で再開した：{ev.reason}"),
                                           _do(NOTIFY, to=("契約者",), note="resumed", about="この回線を手で再開しました")))


def _l24(line: Line, ev: Event, rules: Rules) -> _Step:
    return _Step(Line(LineStage.HEALTHY, audio=frozenset(), j=line.j),
                 (_do(RECORD, about="テスト番号が鳴ったので、声で休ませた回線を再開した"),
                  _do(NOTIFY, to=("契約者",), note="resumed", about="テスト番号が鳴ったので、この回線を再開しました")))


def _l25(line: Line, ev: Event, rules: Rules) -> _Step:
    t = line.t + 1
    return _Step(_rest(LineStage.LINE_REST, rules, ev.at, audio=line.audio, j=line.j, t=t, next_at=ev.at + rules.gap(t),
                       tested_at=ev.at), (_do(RECORD, about="声で休ませた回線で、テスト番号が鳴らなかった（回線のせい）"),))


def _l28(line: Line, ev: Event, rules: Rules) -> _Step:
    # E9 と同じ1か所（_untried）。元気には「続き・休み」が無いので、かけられなくなった時刻ごとに1回（下の「読み方」の (2)）
    key = "試せない" if line.stage is not LineStage.HEALTHY else f"かけられない:{ev.since.isoformat()}"
    told, acts = _untried(line, ev.since, ev.at, rules, key, "line_in_call")
    return _Step(replace(line, told=told), acts)


# ── 行 ──

def _per_call(ev: Event) -> tuple[Do, ...]:
    """こちらの不具合で終えた電話ごとの、記録・知らせ・修正依頼（R-5・R-8 の1か所。修正依頼は同じ不具合で1つ＝受け手が束ねる）。"""
    return (_do(RECORD, call=ev.attempt, kind=ev.kind, about=f"こちらのプログラムの不具合で電話を終えた（{ev.kind}）"),
            _do(NOTIFY, to=BOTH, call=ev.attempt, note="error_notice",
                about="こちらのプログラムの不具合で電話を終えました。この行は復旧を待っています"),
            _do(FIX, fixer=f"種類 {ev.kind} の直す担当", call=ev.attempt, kind=ev.kind, about="こちらのプログラムの不具合で電話を終えた"))


def _back(task: Task, **keep: Any) -> Task:
    """予定どおりへ戻す（予定・n・その種類は、行が持ったまま）。"""
    return Task(TaskStage.ON_PLAN, plan=task.plan, n=keep.get("n", task.n), n_kind=keep.get("n_kind", task.n_kind))


def _r1(task: Task, ev: Event, rules: Rules) -> _Step:
    return _Step(replace(_back(task), stage=TaskStage.AWAIT_DECISION, line=ev.line, attempt=ev.attempt))


def _r2(task: Task, ev: Event, rules: Rules) -> _Step:
    return _Step(_back(task), (_do(PLAN, how="番号の決まった順（番号が使えなくなったら、お客様が言った別の番号・伺い直し・行を終える）"),))


def _r3(task: Task, ev: Event, rules: Rules) -> _Step:
    return _Step(_back(task), (_do(PLAN, how="かけた予定のまま、いまから（どの数にも数えない）"),))


def _r4(task: Task, ev: Event, rules: Rules) -> _Step:
    return _Step(_back(task), (_do(CLOSE, attempt=task.attempt),))


def _r5(task: Task, ev: Event, rules: Rules) -> _Step:
    return _Step(replace(_back(task, n=1, n_kind=ev.kind), stage=TaskStage.AWAIT_RECOVERY, kind=ev.kind, attempt=ev.attempt,
                         answered=bool(ev.answered), ended_at=ev.at), _per_call(ev))


def _r6(task: Task, ev: Event, rules: Rules) -> _Step:
    act = _do(REDIAL, line=None) if task.answered else _do(PLAN, how="かけた予定のまま")
    return _Step(_back(task), (act,))


def _r7(task: Task, ev: Event, rules: Rules) -> _Step:
    acts = (_do(RECORD, about=f"{ev.by}が手で戻した：{ev.reason}"),)
    return _Step(_back(task), acts + ((_do(REDIAL, line=None),) if task.answered else ()))


def _r8(task: Task, ev: Event, rules: Rules) -> _Step:
    rest = (_do(NOTIFY, to=BOTH, note="task_resting",
                about="こちらのプログラムの不具合で、同じ行の電話が続けて終わりました。この行は休ませ、次の行へ進みます"
                      "（その不具合を直したと記録され、そのあとプログラムが替わったら、自動で戻ります。管理者・契約者が戻すこともできます）。"),)
    return _Step(replace(_back(task), stage=TaskStage.RESTING, kinds=frozenset({task.n_kind, ev.kind}), attempt=ev.attempt,
                         answered=bool(ev.answered), since=ev.at), _per_call(ev) + rest)


def _r9(task: Task, ev: Event, rules: Rules) -> _Step:
    return _Step(_back(task, n=0, n_kind=None))


def _r10(task: Task, ev: Event, rules: Rules) -> _Step:
    acts = (_do(RECORD, about=f"{ev.by}が手で戻した：{ev.reason}"),) if ev.code == "E12" else ()
    return _Step(_back(task, n=0, n_kind=None), acts + ((_do(REDIAL, line=None),) if task.answered else ()))


def _r11(task: Task, ev: Event, rules: Rules) -> _Step:
    if not ev.resume_allowed:   # すぐのかけ直しが続けて2回を超えた：ふつうの再発信の決まりで、行の回数に数える
        return _Step(_back(task), (_do(COUNT), _do(PLAN, how="ふつうの再発信の決まり")))
    # 声の通る回線を待つ。並びの1つ目のうちに来なければ R-13（2026-10-29 決定 (4) 案A。新しい設定は作らない）
    return _Step(replace(_back(task), stage=TaskStage.VOICE_REDIAL, line=ev.line, since=ev.at, next_at=ev.at + rules.first_gap()))


def _r12(task: Task, ev: Event, rules: Rules) -> _Step:
    return _Step(_back(task), (_do(REDIAL, line=ev.line),))


def _r13(task: Task, ev: Event, rules: Rules) -> _Step:
    # はさむ行も、ほかの元気な回線も来ないまま、並びの1つ目がたった：同じ回線でお詫びとともにかけ直す（続けて2回までは R-11 のまま）
    return _Step(_back(task), (_do(REDIAL, line=task.line),))


# ── 番号 ──

def _hold(num: Number, rules: Rules, at: datetime) -> bool:
    """N-4 の印（1か所。2026-10-29 決定 案A＝今の形）：まだ分からない試行を番号のせいに数えると、今日の分を使い切る（次の日以降に
    なる・並びを使い切る）か。分からない試行が無ければ印は無い。"""
    if not num.unknown:
        return False
    c = num.n + len(num.unknown)
    if c >= len(rules.failed_retry_days):
        return True
    # 数えたときの「最初の失敗の日」と「前の失敗の日」は、分からない電話の日（番号の欄の待つ印 N-4＝core/health.number_held と同じ。次の日の始まりで
    # 見直す＝N-7 のとき、前の日の分からない電話を今日の失敗として数えない）
    today = _local_day(at)
    first = num.first_day or (_local_day(num.unknown_since) if num.unknown_since else today)
    last = max([_local_day(t) for t in (num.last_end, num.unknown_last) if t is not None] or [today])
    return _number_day(rules, first, last, c) > today


def _next_day_start(at: datetime) -> datetime:
    """at の次の日の始まり（日本時間の0時。日付の決まりは calling_hours.local・from_local の1か所）。"""
    return calling_hours.from_local(datetime.combine(_local_day(at) + timedelta(days=1), time()))


def _with_unknown(num: Number, unknown: frozenset[str], rules: Rules, at: datetime,
                  call_at: datetime | None = None) -> tuple[Number, tuple[str, ...]]:
    """まだ分からない試行の組を置き、N-4 の印を見直す（1か所。call_at＝足した分からない電話の終わり）。印があれば、番号の中身に「次に
    動く時刻」＝次の日の始まり（分からない電話を数えても今日の分が残りうる日の始まり）を持つ（17-3-6 の【当】。3段目の比べで、日付が
    替わっても印が残る形だった）。"""
    if not unknown:
        since = last = None
    else:
        since = min(t for t in (num.unknown_since, call_at) if t is not None) if (num.unknown_since or call_at) else None
        last = max(t for t in (num.unknown_last, call_at) if t is not None) if (num.unknown_last or call_at) else None
    new = replace(num, unknown=unknown, unknown_since=since, unknown_last=last)
    hold = _hold(new, rules, at)
    return replace(new, hold=hold, next_at=_next_day_start(at) if hold else None), (("N-4",) if hold != num.hold else ())


def _n1(num: Number, ev: Event, rules: Rules) -> _Step:
    c = num.n + 1
    last = _local_day(ev.call_at)
    first = num.first_day or last
    unknown = num.unknown - {ev.attempt}
    if c >= len(rules.failed_retry_days):
        return _Step(Number(NumberStage.DEAD, n=c, first_day=first, last_end=ev.call_at, dead_at=ev.at),
                     (_do(RECORD, about="番号のせいの電話が並びの数に届いたので、この番号へはかけない"),),
                     ("N-4",) if num.hold else ())
    day = _number_day(rules, first, last, c)
    new = Number(NumberStage.RUN, n=c, first_day=first, last_end=ev.call_at, next_day=day,
                 not_before=ev.call_at + rules.same_contact_min_interval, hold=num.hold,
                 unknown_since=num.unknown_since, unknown_last=num.unknown_last)
    new, also = _with_unknown(new, unknown, rules, ev.at)
    return _Step(new, (), also)


def _n2(num: Number, ev: Event, rules: Rules) -> _Step:
    new, also = _with_unknown(Number(NumberStage.NONE, hold=num.hold, unknown_since=num.unknown_since,
                                     unknown_last=num.unknown_last), num.unknown, rules, ev.at)
    return _Step(new, (), also)


def _n3(num: Number, ev: Event, rules: Rules) -> _Step:
    new, also = _with_unknown(num, num.unknown - {ev.attempt}, rules, ev.at)
    return _Step(new, (), also)


def _n4(num: Number, ev: Event, rules: Rules) -> _Step:
    new, _ = _with_unknown(num, num.unknown | {ev.attempt}, rules, ev.at, ev.at)
    return _Step(new)


def _n7(num: Number, ev: Event, rules: Rules) -> _Step:
    """N-4 の印の見直しの時刻（次の日の始まり）が来た：その日で印を見直す（外れれば外し、まだなら次の日の始まりを持つ）。"""
    new, _ = _with_unknown(num, num.unknown, rules, ev.at)
    return _Step(new)


# ── 契約者 ──

def _c1(ten: Tenant, ev: Event, rules: Rules) -> _Step:
    tasks = ten.r(ev.kind) | {ev.task}
    rows = ten.with_r(ev.kind, tasks)
    if len(tasks) < rules.trouble_stop_after_rows:
        return _Step(replace(ten, rows=rows))
    return _Step(Tenant(TenantStage.STOPPED, rows=rows, stopped=frozenset({ev.kind}), since=ev.at),
                 (_do(STOP_CALLS), _do(RECORD, about=f"こちらの不具合（{ev.kind}）が違う行で続いたので、新しい電話を止めた"),
                  _do(NOTIFY, to=BOTH, note="stopped", cause="program_trouble",
                      about="こちらのプログラムの同じ不具合が違う行で続いたので、新しい電話を止めました")))


def _c3_c7(ten: Tenant, ev: Event, rules: Rules) -> _Step:
    return _Step(replace(ten, rows=()))


def _c4(ten: Tenant, ev: Event, rules: Rules) -> _Step:
    return _Step(replace(ten, rows=ten.with_r(ev.kind, frozenset())))


def _c5(ten: Tenant, ev: Event, rules: Rules) -> _Step:
    left = ten.stopped - {ev.kind}
    rows = ten.with_r(ev.kind, frozenset())
    acts = (_do(RECORD, about=f"こちらの不具合（{ev.kind}）を直した版になったので、その止めを解いた"),
            _do(NOTIFY, to=BOTH, note="resumed", cause="program_trouble", about="不具合を直した版になったので、止めを解きました"))
    if left:
        return _Step(replace(ten, rows=rows, stopped=left), acts)
    return _Step(Tenant(TenantStage.NORMAL, rows=rows), (_do(RESUME_CALLS),) + acts)


def _c6(ten: Tenant, ev: Event, rules: Rules) -> _Step:
    return _Step(Tenant(TenantStage.NORMAL), (_do(RESUME_CALLS), _do(RECORD, about=f"{ev.by}が手で解いた：{ev.reason}")))


def _c8(ten: Tenant, ev: Event, rules: Rules) -> _Step:
    """止めのあいだの E3（C-8 を一般にした形1つ）：通常のときと同じ数え方（C-1・C-2）で r_k を数え、3行目に届いた種類は止めている
    種類の組に足す。止めている種類の E3 も r_k を数えるだけ（組はそのまま）。"""
    if ev.task in ten.r(ev.kind):   # 同じ行の2回目：数えない（C-2 と同じ）
        return _Step(ten)
    tasks = ten.r(ev.kind) | {ev.task}
    rows = ten.with_r(ev.kind, tasks)
    if len(tasks) < rules.trouble_stop_after_rows or ev.kind in ten.stopped:
        return _Step(replace(ten, rows=rows))
    return _Step(replace(ten, rows=rows, stopped=ten.stopped | {ev.kind}),
                 (_do(RECORD, about=f"こちらの不具合（{ev.kind}）も違う行で続いたので、止める種類に足した"),
                  _do(NOTIFY, to=BOTH, note="stopped", cause="program_trouble", about="こちらのプログラムの別の不具合も違う行で続きました")))


# ── 試行のせい ──

def _v(to: Blame) -> Callable[[Attempt, Event, Rules], _Step]:
    def then(att: Attempt, ev: Event, rules: Rules) -> _Step:
        return _Step(Attempt(to), (_do(WRITE_BLAME, attempt=ev.attempt, blame=to.value),))
    return then


def _nothing(state: Any, ev: Event, rules: Rules) -> _Step:
    return _Step(state)


# ─────────────────────────────────────────────────────────────────────────────
# 表（1か所）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TableRow:
    """表の1行。stages と events に当たり、when（場合分け。無ければいつも）が真なら、then が次の状態とすることを決める。
    chain＝E1 の意味の行（E4・E5 が自分の電話のとき、この行のあとに E4・E5 の行を続けて引く）。nothing＝「何もしない」の行。
    """
    id: str
    subject: str
    stages: frozenset[Any]
    events: frozenset[str]
    then: Callable[[Any, Event, Rules], _Step]
    basis: str
    when: Callable[[Any, Event, Rules], bool] | None = None
    chain: bool = False
    nothing: bool = False


def _row(id: str, subject: str, stages: Any, events: Any, then: Callable[[Any, Event, Rules], _Step], basis: str,
         **more: Any) -> TableRow:
    st = frozenset(stages) if isinstance(stages, (set, frozenset, tuple, list)) else frozenset({stages})
    evs = frozenset(events) if isinstance(events, (set, frozenset, tuple, list)) else frozenset({events})
    return TableRow(id, subject, st, evs, then, basis, **more)


L, T, N, C, V = "回線", "行", "番号", "契約者", "試行のせい"
LS, TS, NS, CS = LineStage, TaskStage, NumberStage, TenantStage
ALL_LINE_EVENTS = ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9", "E10", "E12", "E13", "E14", "E17")
IN_USE = frozenset(LineStage) - {LineStage.OUT_OF_USE}


TABLE: tuple[TableRow, ...] = (
    # ── 17-3-1 回線 ──
    _row("L-1", L, LS.HEALTHY, ("E1", "E5"), _l1, "【決 10-25 夜・10-29】"),
    _row("L-2", L, LS.HEALTHY, "E2", _l2, "【決 10-24・10-25 夜】・【当(5)】"),
    _row("L-3", L, LS.HEALTHY, "E4", _l3, "【決 10-25 夜 案A・10-29（2人）】・【当(3)】"),
    _row("L-4", L, {LS.HEALTHY} | CHECKING, "E3", _nothing, "【決 10-25】", nothing=True),
    _row("L-5", L, CHECKING, ("E1", "E4", "E5"), _l5, "【決 10-24】", chain=True),
    _row("L-6", L, CHECKING, "E2", _l6, "【決 10-24】"),
    _row("L-7", L, LS.CHECK_A, "E10", _l7_l8, "【決 10-24・10-25 夜】・【当(5)】"),
    _row("L-8", L, LS.CHECK_B, "E10", _l7_l8, "【決 10-24 夜・10-25・10-25 夜・10-29】"),
    _row("L-9", L, LS.CHECK_C, "E6", _l9, "【決 10-24・10-25 夜】"),
    _row("L-10", L, LS.CHECK_C, "E7", _l10, "【決 10-24・10-25・10-29（案B：ここでは知らせない）】"),
    _row("L-11", L, LS.CHECK_C, "E8", _l11, "【決 10-25 夜「OK」】"),
    _row("L-12", L, LS.CHECK_C, ("E9", "E10"), _l12, "【決 10-25 夜】・【今 0144 の (4)】・【当(9)】・【当】（答えの期限は E10 で来る）"),
    _row("L-13", L, LS.CHECK_B, "E14", _l7_l8, "【決 10-29】", when=lambda s, e, r: s.wait is Wait.NO_NUMBER),
    _row("L-14", L, CHECKING, "E13", _l14_l22, "【決 10-24・10-29】"),
    _row("L-15", L, LS.LINE_REST, "E10", _l15_l23, "【決 10-24「再開の確かめ：OK」・10-24 夜・10-29】",
         when=lambda s, e, r: _test_due(s, e) and s.wait is not Wait.ANSWER),
    _row("L-16", L, LS.LINE_REST, "E6", _l16, "【決 10-25 夜】・【今 9-1】"),
    _row("L-17", L, LS.LINE_REST, "E7", _l17, "【決 10-25・10-29（案B：ここで知らせる）】"),
    _row("L-18", L, LS.LINE_REST, "E8", _l18_l26, "【決 10-25 夜】"),
    _row("L-19", L, RESTS, ("E9", "E10"), _l19, "【決 10-25 夜】・【当(9)】・【当】（答えの期限は E10 で来る）",
         when=lambda s, e, r: e.code == "E9" or (_test_due(s, e) and s.wait is Wait.ANSWER)),
    _row("L-20", L, RESTS, "E10", _l20, "【決 10-18】・【今 見直し8・0135】・【当】（休みの中身に時刻を持つ）",
         when=lambda s, e, r: not _test_due(s, e)),
    _row("L-21", L, RESTS, "E12", _l21, "【決 10-15・10-29】"),
    _row("L-22", L, RESTS, "E13", _l14_l22, "【決 10-29】"),
    _row("L-23", L, LS.AUDIO_REST, "E10", _l15_l23, "【決 10-29】", when=lambda s, e, r: _test_due(s, e) and s.wait is not Wait.ANSWER),
    _row("L-24", L, LS.AUDIO_REST, "E6", _l24, "【決 10-29「テスト番号が鳴れば再開」】"),
    _row("L-25", L, LS.AUDIO_REST, "E7", _l25, "【決 10-24（8-8 の回線のせい）】"),
    _row("L-26", L, LS.AUDIO_REST, "E8", _l18_l26, "【決 10-25 夜】"),
    _row("L-27", L, LS.OUT_OF_USE, tuple(e for e in ALL_LINE_EVENTS if e != "E2"), _nothing, "【決 10-29】", nothing=True),
    _row("L-28", L, IN_USE, "E17", _l28, "【当(10)】"),
    # 2026-10-29：見つけた穴を、決定の言葉からの当てはめで埋めた行（17章の決まり (3)）
    _row("L-29", L, LS.HEALTHY, "E13", _l14_l22, "【当】（L-14 と同じ。続きは無いので試行は書かない）"),
    _row("L-30", L, RESTS, "E14", _l30, "【当】（休みでテスト番号が無いとき：次に動く時刻＝いま）",
         when=lambda s, e, r: s.wait is Wait.NO_NUMBER),
    _row("L-31", L, IN_USE, "E14", _nothing, "【当】（テスト番号を待っていない段：何もしない）",
         when=lambda s, e, r: s.wait is not Wait.NO_NUMBER, nothing=True),
    _row("L-32", L, {LS.HEALTHY} | CHECKING, "E12", _record_only, "【当】（手の再開は休みの状態だけ＝決定 10-29「手で再開」）"),
    _row("L-33", L, LS.OUT_OF_USE, "E2", _l33, "【当】（L-14「かけている途中の電話も、終わって鳴っていなければ回線」）"),

    # ── 17-3-2 行 ──
    _row("R-1", T, TS.ON_PLAN, "E2", _r1, "【決 10-24】"),
    _row("R-2", T, TS.AWAIT_DECISION, "E16", _r2, "【決 10-22 深夜・10-23・10-24・10-25】",
         when=lambda s, e, r: e.blame == "番号" and e.attempt == s.attempt),
    _row("R-3", T, TS.AWAIT_DECISION, "E16", _r3, "【決 10-24】", when=lambda s, e, r: e.blame == "回線" and e.attempt == s.attempt),
    _row("R-4", T, TS.AWAIT_DECISION, "E15", _r4, "【今 0144 の (1)】"),
    _row("R-5", T, TS.ON_PLAN, "E3", _r5, "【決 10-25・10-25 夜】", when=lambda s, e, r: s.n == 0),
    _row("R-6", T, TS.AWAIT_RECOVERY, ("E5", "E11"), _r6, "【決 10-17・10-18 案A・10-25 夜】",
         when=lambda s, e, r: (e.code == "E5" and not e.own and e.started_at >= s.ended_at) or (e.code == "E11" and e.kind == s.kind)),
    _row("R-7", T, TS.AWAIT_RECOVERY, "E12", _r7, "【当(7)】・【決 10-15】"),
    _row("R-8", T, TS.ON_PLAN, "E3", _r8, "【決 10-25】・【当(11)】", when=lambda s, e, r: s.n == 1),
    _row("R-9", T, TS.ON_PLAN, ("E1", "E4", "E5"), _r9, "【今 0139】", when=lambda s, e, r: s.n == 1 and e.own, chain=True),
    _row("R-10", T, TS.RESTING, ("E11", "E12"), _r10, "【決 10-25 夜 案A (2)】",
         when=lambda s, e, r: e.code == "E12" or e.kind in s.kinds),
    _row("R-11", T, TS.ON_PLAN, "E4", _r11, "【決 10-25 夜 案A・10-18 案A・10-29】"),
    _row("R-12", T, TS.VOICE_REDIAL, "E18", _r12, "【決 10-29】"),
    _row("R-13", T, TS.VOICE_REDIAL, "E10", _r13, "【決 10-29 (4) 案A】"),
    _row("R-14", T, frozenset(TaskStage), "E19", _nothing, "―（4-1・7-2 のまま。この章では変えない）", nothing=True),
    _row("R-15", T, TS.ON_PLAN, ("E1", "E5"), _nothing, "【当】（予定どおり n＝0 の行の電話が鳴った：何もしない）",
         when=lambda s, e, r: s.n == 0 and e.own, nothing=True),
    _row("R-16", T, (TS.ON_PLAN, TS.AWAIT_DECISION, TS.RESTING, TS.VOICE_REDIAL), "E5", _nothing,
         "【当】（ほかの行の話し終えた電話は、復旧待ちのほかには効かない）", when=lambda s, e, r: not e.own, nothing=True),
    _row("R-17", T, (TS.ON_PLAN, TS.AWAIT_DECISION, TS.VOICE_REDIAL), "E11", _nothing, "【当】（直した版は、待っている行のほかには効かない）",
         nothing=True),
    _row("R-18", T, TS.AWAIT_RECOVERY, "E5", _nothing, "【当】（R-6 の「終えた電話より後」のとおり）",
         when=lambda s, e, r: not e.own and e.started_at < s.ended_at, nothing=True),
    _row("R-19", T, (TS.AWAIT_RECOVERY, TS.RESTING), "E11", _nothing, "【当】（待っている種類と違う種類の直した版）",
         when=lambda s, e, r: (s.stage is TaskStage.AWAIT_RECOVERY and e.kind != s.kind)
         or (s.stage is TaskStage.RESTING and e.kind not in s.kinds), nothing=True),
    _row("R-20", T, frozenset(TaskStage), "E16", _nothing, "【当】（行は何もしない。番号の表は試行ごとにそのまま引く）",
         when=lambda s, e, r: s.stage is not TaskStage.AWAIT_DECISION or e.attempt != s.attempt, nothing=True),
    _row("R-21", T, (TS.ON_PLAN, TS.AWAIT_DECISION, TS.VOICE_REDIAL), "E12", _record_only, "【当】（戻すものが無い：記録だけ）"),

    # ── 17-3-3 番号 ──
    _row("N-1", N, (NS.NONE, NS.RUN), "E16", _n1, "【決 10-22 深夜・10-23】・【今 見直し18c】", when=lambda s, e, r: e.blame == "番号"),
    _row("N-2", N, NS.RUN, "E1", _n2, "【決 10-22 深夜】"),
    _row("N-3", N, (NS.NONE, NS.RUN), "E16", _n3, "【決 10-24】", when=lambda s, e, r: e.blame == "回線"),
    _row("N-4", N, (NS.NONE, NS.RUN), "E2", _n4, "【今 0144 の (3)】・【決 10-29 案A】"),
    _row("N-5", N, NS.DEAD, ("E1", "E2", "E16"), _nothing, "【決 10-22 深夜】", nothing=True),
    _row("N-6", N, NS.NONE, "E1", _nothing, "【当】（続きが無い番号で鳴った：何もしない）", nothing=True),
    _row("N-7", N, (NS.NONE, NS.RUN), "E10", _n7, "【当】（17-3-6：番号の待つ印は、次の日の始まりで見直す。4段目）"),

    # ── 17-3-4 契約者 ──
    _row("C-1", C, CS.NORMAL, "E3", _c1, "【決 10-25・10-24】", when=lambda s, e, r: e.task not in s.r(e.kind)),
    _row("C-2", C, CS.NORMAL, "E3", _nothing, "【決 10-25】", when=lambda s, e, r: e.task in s.r(e.kind), nothing=True),
    _row("C-3", C, CS.NORMAL, "E5", _c3_c7, "【決 10-25 夜 案A (3)】"),
    _row("C-4", C, CS.NORMAL, "E11", _c4, "【決 10-25 夜 案A (2)】"),
    _row("C-5", C, CS.STOPPED, "E11", _c5, "【決 10-25 夜 案A (2)】・【当(8)】", when=lambda s, e, r: e.kind in s.stopped),
    _row("C-6", C, CS.STOPPED, "E12", _c6, "【決 10-15・10-25 夜】"),
    _row("C-7", C, CS.STOPPED, "E5", _c3_c7, "【決 10-25 夜 案A (2)(3)】"),
    _row("C-8", C, CS.STOPPED, "E3", _c8, "【当(8)】・【当】（止めのあいだも C-1・C-2 と同じ数え方）"),
    _row("C-9", C, CS.NORMAL, "E12", _record_only, "【当】（解くものが無い：記録だけ）"),
    _row("C-10", C, CS.STOPPED, "E11", _c4, "【当】（止めていない種類の直した版：C-4 と同じ）", when=lambda s, e, r: e.kind not in s.stopped),

    # ── 17-3-5 試行のせい ──
    _row("V-1", V, Blame.EMPTY, "E2", _v(Blame.UNKNOWN), "【決 10-24】"),
    _row("V-2", V, Blame.EMPTY, "E3", _v(Blame.OURS), "【決 10-25】・【当】（鳴った・鳴らないで分けない。17-1-5 のとおり）"),
    _row("V-3", V, Blame.UNKNOWN, "E16", _v(Blame.NUMBER), "【決 10-24】", when=lambda s, e, r: e.blame == "番号"),
    _row("V-4", V, Blame.UNKNOWN, "E16", _v(Blame.LINE), "【決 10-24】", when=lambda s, e, r: e.blame == "回線"),
    _row("V-5", V, (Blame.NUMBER, Blame.LINE, Blame.OURS), ("E2", "E3", "E16"), _nothing, "【今 この章の決まり (1)】",
         nothing=True),
)

# その欄の表が受ける出来事（表の行の出来事の列から）。ほかの出来事は、その欄へは配らない（来たら「受けない出来事」）
RECEIVES: dict[str, frozenset[str]] = {
    s: frozenset(e for r in TABLE if r.subject == s for e in r.events) for s in (L, T, N, C, V)}


# ─────────────────────────────────────────────────────────────────────────────
# 表に行の無い組み合わせ＝その段では起きない出来事だけ（来れば誤りの値。見つけた穴は 2026-10-29 に【当】の行で埋めた）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Gap:
    """表に行の無い組み合わせ。kind：「起きない」＝その段ではその出来事が起きない（例：休みの回線はお客様へかけないので、お客様の
    電話の出来事は来ない）。入口は誤りの値を返す（「穴」＝起きうるのに行の無いものは、作らない）。"""
    id: str
    subject: str
    stages: frozenset[Any]
    events: frozenset[str]
    kind: str
    why: str
    when: Callable[[Any, Event, Rules], bool] | None = None


def _gap(id: str, subject: str, stages: Any, events: Any, kind: str, why: str, when: Any = None) -> Gap:
    r = _row(id, subject, stages, events, _nothing, "")
    return Gap(id, subject, r.stages, r.events, kind, why, when)


GAPS: tuple[Gap, ...] = (
    # 回線
    _gap("G-L6", L, ({LS.HEALTHY, LS.CHECK_A, LS.CHECK_B}), ("E6", "E7", "E8", "E9"), "起きない",
         "テスト番号を鳴らしていない段に、テスト番号の答えは来ない"),
    _gap("G-L7", L, RESTS, ("E1", "E2", "E3", "E4", "E5"), "起きない", "休みの回線はお客様へかけないので、お客様の電話の出来事は来ない"),
    # 行
    _gap("G-R10", T, (TS.ON_PLAN, TS.AWAIT_RECOVERY, TS.RESTING, TS.VOICE_REDIAL), "E15", "起きない",
         "判定待ちでない行に、E15（判定待ちの行に、ほかの回線がかけた）は来ない"),
    _gap("G-R11", T, (TS.AWAIT_DECISION, TS.AWAIT_RECOVERY, TS.RESTING, TS.VOICE_REDIAL), ("E1", "E2", "E3", "E4"), "起きない",
         "予定どおりでない行はかけないので、その行の電話の出来事は来ない"),
    _gap("G-R12", T, (TS.AWAIT_DECISION, TS.AWAIT_RECOVERY, TS.RESTING, TS.VOICE_REDIAL), "E5", "起きない",
         "予定どおりでない行はかけないので、その行の電話の E5 は来ない", when=lambda s, e, r: e.own),
    _gap("G-R13", T, (TS.ON_PLAN, TS.AWAIT_DECISION, TS.AWAIT_RECOVERY, TS.RESTING), "E18", "起きない",
         "声のかけ直し待ちでない行は、声の通る回線を待っていない"),
    # 試行のせい
    _gap("G-V2", V, Blame.EMPTY, "E16", "起きない", "空の試行に E16（分からない → 番号／回線）は来ない"),
    _gap("G-V3", V, Blame.UNKNOWN, ("E2", "E3"), "起きない", "分からないの試行は電話が終わっているので、E2・E3 は来ない"),
)


# ─────────────────────────────────────────────────────────────────────────────
# 入口（1つ）
# ─────────────────────────────────────────────────────────────────────────────

def next_at(state: Any) -> datetime | None:
    """その状態の「次に動く時刻」（E10 が来る時刻。無ければ None）。行の判定待ちは回線の欄の時刻を読むので、ここでは None。"""
    if isinstance(state, Line):
        fix = state.fix_at if state.stage in RESTS and "1時間" not in state.told else None
        times = [t for t in (state.next_at, fix) if t is not None]
        return min(times) if times else None
    if isinstance(state, Task) and state.stage is TaskStage.VOICE_REDIAL:
        return state.next_at
    if isinstance(state, Number) and state.hold:
        return state.next_at
    return None


def _contents_wrong(state: Any) -> str | None:
    """状態の中身が、その段と合わないか（合わなければ理由）。"""
    if isinstance(state, Line):
        s = state.stage
        if s is LineStage.OUT_OF_USE:
            return None if state.removed is not None else "使用停止なのに、外した管理者か契約者・時刻・理由が無い"
        if state.removed is not None:
            return "使用停止でないのに、外した印がある"
        if s is LineStage.HEALTHY:
            bad = state.t or state.run or state.wait or state.next_at or state.entered_at or state.tested_at
            return "元気なのに、確かめ中・休みの中身がある" if bad else None
        if state.next_at is None and state.wait is not Wait.NO_NUMBER:
            return "待つ状態なのに、次に動く時刻が無い（テスト番号が無いときだけ時刻を持たない）"
        if s in CHECKING:
            if not state.run or state.run_start is None:
                return "確かめ中なのに、続きの電話・続きの始まりが無い"
            if (s is LineStage.CHECK_B) != (state.wait is not None):
                return "待つ理由を持つのは (b) だけ"
            return None
        if state.entered_at is None or state.run:
            return "休みなのに、入った時刻が無い・続きの電話がある"
        if s is LineStage.LINE_REST and state.t < 1:
            return "回線のせい・休みなのに、鳴らなかったテストの回数が無い"
        if s is LineStage.AUDIO_REST and state.j < 1:
            return "声の不調・休みなのに、声で止めた回数が無い"
        return None
    if isinstance(state, Task):
        if state.n not in (0, 1) or (state.n == 1) != (state.n_kind is not None):
            return "こちらの不具合の続き n は0か1で、1のときだけ種類を持つ"
        need = {TaskStage.AWAIT_DECISION: state.line and state.attempt, TaskStage.AWAIT_RECOVERY: state.kind and state.ended_at,
                TaskStage.RESTING: state.kinds and state.since, TaskStage.VOICE_REDIAL: state.line and state.since and state.next_at}
        if state.stage in need and not need[state.stage]:
            return f"{state.stage.value}の中身が足りない"
        return None
    if isinstance(state, Number):
        if state.stage is NumberStage.RUN and not (1 <= state.n and state.first_day and state.next_day):
            return "続きありなのに、回数・最初の失敗の日・次の日が無い"
        if state.stage is NumberStage.NONE and state.n:
            return "続き無しなのに、回数がある"
        if state.hold != (state.next_at is not None):
            return "番号の待つ印と、印を見直す時刻は組で持つ"
        return None
    if isinstance(state, Tenant):
        if (state.stage is TenantStage.STOPPED) != bool(state.stopped):
            return "止めている種類の組を持つのは、こちらの不具合で止めだけ"
        return None
    return None


def _event_wrong(ev: Event) -> str | None:
    if ev.code not in EVENT_NAMES:
        return f"出来事の記号が一覧に無い：{ev.code}"
    missing = [k for k in _EVENT_NEEDS[ev.code] if getattr(ev, k) is None]
    if missing:
        return f"{ev.code} の中身が足りない：{'・'.join(missing)}"
    if ev.code == "E16" and ev.blame not in ("番号", "回線"):
        return "E16 のせいは「番号」か「回線」"
    return None


def _stage_name(state: Any) -> str | None:
    stage = getattr(state, "stage", None)
    return stage.value if isinstance(stage, Enum) else None


def _gap_of(subject: str, state: Any, ev: Event, rules: Rules) -> Gap | None:
    for g in GAPS:
        if g.subject == subject and state.stage in g.stages and ev.code in g.events and (g.when is None or g.when(state, ev, rules)):
            return g
    return None


def _match(subject: str, state: Any, ev: Event, rules: Rules, *, chain: bool) -> list[TableRow]:
    return [r for r in TABLE if r.subject == subject and r.chain == chain and state.stage in r.stages and ev.code in r.events
            and (r.when is None or r.when(state, ev, rules))]


def apply(state: Any, event: Event, rules: Rules) -> Outcome | Mistake:
    """表を引く入口（1つ）。今の状態と出来事で表の行を引き、次の状態・することの並び・引いた表の行番号を返す。引けなければ、
    決めずに誤りの値（Mistake）を返す。E4・E5（自分の電話）は E1 でもあるので、E1 の意味の行（chain）を先に、続けて E4・E5 の行を引く。"""
    subject = subject_of(state)
    stage = _stage_name(state)

    def mistake(kind: str, why: str, **more: Any) -> Mistake:
        return Mistake(kind, why, subject, stage, event.code, **more)

    if subject is None:
        return mistake("中身の誤り", f"表の欄ではない値：{type(state).__name__}")
    for wrong in (_contents_wrong(state), _event_wrong(event)):
        if wrong:
            return mistake("中身の誤り", wrong)
    if event.code not in RECEIVES[subject]:
        return mistake("受けない出来事", f"{subject}の表は {event.code} を受けない")
    if subject == L and event.code == "E10" and event.test_number is None:
        return mistake("中身の誤り", "回線の E10 には、いまテスト番号があるか（test_number）が要る")
    if subject == T and event.code == "E4" and event.resume_allowed is None:
        return mistake("中身の誤り", "行の E4 には、すぐのかけ直しが続けて2回のうちか（resume_allowed）が要る（締めのあとに渡す）")
    if event.code == "E10":
        due = next_at(state)
        if due is None or event.at < due:
            return mistake("時刻の前", "次に動く時刻を持たない・まだ来ていないのに E10 が来た")
    return _apply(subject, state, event, rules, mistake)


def _apply(subject: str, state: Any, event: Event, rules: Rules, mistake: Callable[..., Mistake]) -> Outcome | Mistake:
    hit: list[str] = []
    acts: tuple[Do, ...] = ()
    phases = (True, False)
    for chain in phases:
        found = _match(subject, state, event, rules, chain=chain)
        if len(found) > 1:
            return mistake("重なり", f"表の行が2つ当たった：{'・'.join(r.id for r in found)}")
        if not found:
            continue
        row = found[0]
        step = row.then(state, event, rules)
        state, acts, hit = step.state, acts + step.actions, hit + [row.id, *step.also]
        if chain and not (event.code in COMPOSITE and event.own):
            break   # E1 そのもの（E4・E5 でない）は、E1 の意味の行だけ
    if not hit:
        gap = _gap_of(subject, state, event, rules)
        why = gap.why if gap else "17章の表に、次の状態も「何もしない」も無い組み合わせ"
        return mistake("表に無い", why, gap=gap.id if gap else None)
    return Outcome(state, acts, tuple(hit))
