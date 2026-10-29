"""回線・行・番号・契約者・試行のせいの、状態と出来事の表の試験（core/health_rules.py。DESIGN.md 17章の 17-5・15-2 の考え方）。

データベースに触らない（表は値だけを返す）。試験は3種類：
- 総当たり（`Exhaustive`）：全部の状態（段と、中身の境目の値）× その欄の表が受ける全部の出来事に、次の状態か「何もしない」が
  決まっている。誤りの値になるのは、その段では起きない出来事（`GAPS`。どれも「起きない」）と、時刻を持たない状態への E10 だけ。
- 抜け道（`WaysOut`）：どの状態からも、出来事を続けて与えると元気（または終わり）へ行けるか、入るときに知らせがあり手の出口
  （E12・E13・E14）がある。時刻を持たない待ちは5つだけ（17-5 の4つと、休みでテスト番号が無いとき＝2026-10-29 の当てはめ）。
- 決定の例（`Decisions`・`Decided1029`・`Filled1029`）：17-4 の管理者の決定の文1つにつき1つ以上と、17-7-2 の伺いへの
  2026-10-29 のご決定、見つけた穴を埋めた当てはめ【当】の行（試験の説明に決定の日付と言葉）。
⚠️ 試験のデータは架空のもの（実在の会社・お客様・電話番号を使わない）。
"""

from __future__ import annotations

import itertools
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from salescall.core import health_rules as h
from salescall.core.health_rules import (Attempt, Blame, Event, Line, LineStage as LS, Mistake, Number,
                                         NumberStage as NS, Outcome, Rules, Task, TaskStage as TS, Tenant,
                                         TenantStage as CS, Wait)
from salescall.link.jst import JST

T0 = datetime(2026, 10, 29, 10, 0, tzinfo=JST)   # 鳴らしてよい時間（7:00〜22:00）の内
MIN = timedelta(minutes=1)
SETTINGS = {   # 設定の名前と値の形のまま（初めの値と同じ数）
    "line.test_ring_gaps": [900, 1800, 3600, 7200, 14400],
    "line.test_ring_hours": [["07:00", "22:00"]],
    "stop.trouble.stop_after_rows": 3,
    "dial.failed_retry_days": [1, 1, 2, 4],
    "gate.same_contact_min_interval": 300,
}


def rules(**settings) -> Rules:
    return Rules.from_settings({**SETTINGS, **settings})


R = rules()
GAP = [timedelta(seconds=g) for g in SETTINGS["line.test_ring_gaps"]]


def ev(code: str, at: datetime = T0, **kw) -> Event:
    """出来事を作る（要る中身の、試験の初めの値を入れる）。"""
    base = {"E1": dict(attempt="a9", number="n9"), "E2": dict(attempt="a9", number="n9", line="L1"),
            "E3": dict(attempt="a9", kind="frozen", task="t9", rang=False, answered=False),
            "E4": dict(attempt="a9", number="n9", line="L1", resume_allowed=True),
            "E5": dict(attempt="a9", number="n9", started_at=at - 5 * MIN), "E10": dict(test_number=True),
            "E11": dict(kind="frozen"), "E12": dict(by="管理者", reason="直ったことを確かめた"),
            "E13": dict(by="契約者", reason="スマホを替えた"), "E15": dict(line="L2"),
            "E16": dict(attempt="a1", blame="番号", call_at=T0), "E17": dict(since=at - MIN), "E18": dict(line="L2")}
    return Event(code, at, **{**base.get(code, {}), **kw})


def ok(test: unittest.TestCase, out) -> Outcome:
    test.assertIsInstance(out, Outcome, out)
    return out


def chain(test: unittest.TestCase, state, *events: Event, r: Rules = R) -> list[Outcome]:
    """出来事を順に与え、どれも表を引けたことを確かめて、答えの並びを返す。"""
    outs = []
    for e in events:
        out = ok(test, h.apply(state, e, r))
        outs.append(out)
        state = out.state
    return outs


def whats(out: Outcome) -> list[str]:
    return [a.what for a in out.actions]


def latest(state) -> datetime:
    """状態の中の、いちばん遅い時刻（出来事の時刻を、その後に置くため）。"""
    times = [v for v in vars(state).values() if isinstance(v, datetime)]
    removed = getattr(state, "removed", None)
    if removed:
        times.append(removed[1])
    return max(times + [T0])


# ─────────────────────────────────────────────────────────────────────────────
# 状態と出来事の、総当たりの材料（段ごとに、中身の境目の値を含める）
# ─────────────────────────────────────────────────────────────────────────────

RUN = (("a1", "n1"),)


def line_states():
    yield Line(LS.HEALTHY)
    yield Line(LS.HEALTHY, audio=frozenset({"n7"}))                    # m＝1（次の違うお客様で止める＝2人）
    yield Line(LS.HEALTHY, audio=frozenset({"n7", "n8"}))              # m＝2（試験が作った境目の外の値）
    yield Line(LS.HEALTHY, j=1)                                        # 声で再開したあと（1本で止め直す）
    yield Line(LS.HEALTHY, audio=frozenset({"n7"}), j=3)
    yield Line(LS.HEALTHY, told=frozenset({"かけられない:x"}))
    yield Line(LS.CHECK_A, run_start=T0, run=RUN, next_at=T0 + GAP[0])
    yield Line(LS.CHECK_A, run_start=T0, run=RUN + (("a2", "n2"),), next_at=T0 + GAP[0], audio=frozenset({"n7"}), j=1)
    late = T0.replace(hour=22, minute=50)
    yield Line(LS.CHECK_A, run_start=late - GAP[0], run=RUN, next_at=late)        # 時刻が来たら、鳴らしてよい時間の外
    for wait, t in itertools.product(set(Wait) - {Wait.ANSWER}, (0, 1, 6)):
        tested = T0 - GAP[min(t, 5) - 1] if t else None
        nxt = None if wait is Wait.NO_NUMBER else T0
        yield Line(LS.CHECK_B, run_start=T0 - 2 * GAP[0], run=RUN, wait=wait, next_at=nxt, t=t, tested_at=tested,
                   told=frozenset({"テスト番号が無い"}) if wait is Wait.NO_NUMBER else frozenset())
    for t, told in itertools.product((0, 2), (frozenset(), frozenset({"見分けられない", "試せない"}))):
        yield Line(LS.CHECK_C, run_start=T0 - 20 * MIN, run=RUN, t=t, next_at=T0 + timedelta(seconds=20),
                   tested_at=T0 - 5 * MIN if t else None, told=told)
    for stage, t, wait in itertools.product(sorted(h.RESTS, key=lambda s: s.value), (1, 6), (None, Wait.NO_NUMBER, Wait.OFF_HOURS,
                                                                                         Wait.WITHHELD, Wait.OTHER_TEST, Wait.ANSWER)):
        for entered, told in ((T0 - 10 * MIN, frozenset()), (T0 - 2 * 60 * MIN, frozenset({"回線のせい", "見分けられない"})),
                              (T0 - 2 * 60 * MIN, frozenset({"1時間"}))):
            yield Line(stage, t=t if stage is LS.LINE_REST else t - 1, j=1 if stage is LS.AUDIO_REST else 0, wait=wait,
                       next_at=None if wait is Wait.NO_NUMBER else T0, entered_at=entered, fix_at=entered + 60 * MIN,
                       tested_at=entered if stage is LS.LINE_REST else None, told=told)
    yield Line(LS.AUDIO_REST, j=2, audio=frozenset({"n7"}), next_at=T0, entered_at=T0 - 20 * MIN)
    yield Line(LS.REMOVED, removed=("契約者", T0 - MIN, "スマホを替えた"))


def line_events(state):
    now = latest(state) + MIN
    yield "E1", ev("E1", now)
    yield "E2", ev("E2", now)
    yield "E3 鳴らずに", ev("E3", now)
    yield "E3 出たあと", ev("E3", now, rang=True, answered=True)
    yield "E4 違うお客様", ev("E4", now, number="n9")
    if state.audio:
        yield "E4 同じお客様", ev("E4", now, number=sorted(state.audio)[0])
    yield "E5", ev("E5", now)
    for code in ("E6", "E7", "E8", "E9", "E12", "E13", "E14"):
        yield code, ev(code, now)
    due = h.next_at(state)
    at = due if due is not None else now
    yield "E10 テスト番号あり", ev("E10", at)
    yield "E10 テスト番号なし", ev("E10", at, test_number=False)
    yield "E10 ほかの回線のテスト中", ev("E10", at, other_test_until=at + 10 * MIN)
    yield "E17 短い", ev("E17", now, since=now - MIN)
    yield "E17 長い", ev("E17", now, since=now - 20 * MIN)


def task_states():
    for n in (0, 1):
        k = "frozen" if n else None
        yield Task(TS.ON_PLAN, plan="予定", n=n, n_kind=k)
        yield Task(TS.AWAIT_DECISION, plan="予定", n=n, n_kind=k, line="L1", attempt="a1")
    for answered in (False, True):
        yield Task(TS.AWAIT_RECOVERY, plan="予定", n=1, n_kind="frozen", kind="frozen", attempt="a1", answered=answered,
                   ended_at=T0)
        yield Task(TS.RESTING, plan="予定", n=1, n_kind="frozen", kinds=frozenset({"frozen", "runner_error"}), attempt="a1",
                   answered=answered, since=T0)
    yield Task(TS.VOICE_REDIAL, plan="予定", line="L1", since=T0, next_at=T0 + GAP[0])


def task_events(state):
    now = latest(state) + MIN
    yield "E1", ev("E1", now)
    yield "E2", ev("E2", now)
    yield "E3 鳴らずに", ev("E3", now)
    yield "E3 出たあと・別の種類", ev("E3", now, kind="runner_error", rang=True, answered=True)
    yield "E4 2回のうち", ev("E4", now)
    yield "E4 2回を超えた", ev("E4", now, resume_allowed=False)
    yield "E5 自分", ev("E5", now)
    yield "E5 ほかの行・あと", ev("E5", now, own=False, started_at=now)
    yield "E5 ほかの行・前", ev("E5", now, own=False, started_at=T0 - 10 * MIN)
    yield "E11 frozen", ev("E11", now)
    yield "E11 ほかの種類", ev("E11", now, kind="poor_db")
    yield "E12", ev("E12", now)
    yield "E15", ev("E15", now)
    for blame, attempt in itertools.product(("番号", "回線"), ("a1", "a8")):
        yield f"E16 {blame} {attempt}", ev("E16", now, blame=blame, attempt=attempt)
    due = h.next_at(state)
    yield "E10", ev("E10", due if due is not None else now)
    yield "E18", ev("E18", now)
    yield "E19", ev("E19", now)


def number_states():
    day = T0.date()
    yield Number(NS.NONE)
    yield Number(NS.NONE, unknown=frozenset({"a1"}))
    for n in (1, 2, 3):
        for unknown in (frozenset(), frozenset({"a1"}), frozenset({"a1", "a2"})):
            yield Number(NS.RUN, n=n, first_day=day, last_end=T0, next_day=day, not_before=T0 + 5 * MIN, unknown=unknown)
    yield Number(NS.DEAD, n=4, dead_at=T0)


def number_events(state):
    now = latest(state) + MIN
    yield "E1", ev("E1", now)
    yield "E2", ev("E2", now, attempt="a5")
    for blame in ("番号", "回線"):
        yield f"E16 {blame}", ev("E16", now, blame=blame, attempt="a1", call_at=now - MIN)


def tenant_states():
    yield Tenant(CS.NORMAL)
    yield Tenant(CS.NORMAL, rows=(("frozen", frozenset({"t1", "t2"})),))
    yield Tenant(CS.STOPPED, rows=(("frozen", frozenset({"t1", "t2", "t3"})), ("runner_error", frozenset({"t4", "t5"}))),
                 stopped=frozenset({"frozen"}), since=T0)
    yield Tenant(CS.STOPPED, rows=(("runner_error", frozenset({"t4"})),), stopped=frozenset({"frozen", "poor_db"}), since=T0)


def tenant_events(state):
    now = latest(state) + MIN
    for kind, task in itertools.product(("frozen", "runner_error"), ("t1", "t4", "t9")):
        yield f"E3 {kind} {task}", ev("E3", now, kind=kind, task=task)
    yield "E5", ev("E5", now)
    for kind in ("frozen", "runner_error", "poor_db"):
        yield f"E11 {kind}", ev("E11", now, kind=kind)
    yield "E12", ev("E12", now)


def attempt_states():
    yield from (Attempt(b) for b in Blame)


def attempt_events(state):
    yield "E2", ev("E2")
    yield "E3 鳴らずに", ev("E3")
    yield "E3 鳴ったあと", ev("E3", rang=True, answered=True)
    for blame in ("番号", "回線"):
        yield f"E16 {blame}", ev("E16", blame=blame)


SUBJECTS = {"回線": (line_states, line_events), "行": (task_states, task_events), "番号": (number_states, number_events),
            "契約者": (tenant_states, tenant_events), "試行のせい": (attempt_states, attempt_events)}


def every_combination():
    """欄 × 状態 × 出来事 の全部と、その答え。"""
    for subject, (states, events) in SUBJECTS.items():
        for state in states():
            for label, e in events(state):
                yield R, subject, state, label, e, h.apply(state, e, R)


# ─────────────────────────────────────────────────────────────────────────────
# 総当たり
# ─────────────────────────────────────────────────────────────────────────────

class Exhaustive(unittest.TestCase):
    """全部の状態 × 全部の出来事に、次の状態か「何もしない」が決まっている。誤りの値は、分かっている穴（GAPS）と、時刻を持たない
    状態に E10 が来たとき（来ない出来事）だけ。"""

    @classmethod
    def setUpClass(cls):
        cls.results = list(every_combination())

    def test_every_combination_is_decided_or_a_known_gap(self):
        unknown = []
        for r, subject, state, label, e, out in self.results:
            if isinstance(out, Outcome):
                continue
            known = (out.kind == "表に無い" and out.gap is not None) or \
                    (out.kind == "時刻の前" and h.next_at(state) is None)
            if not known:
                unknown.append((subject, out.stage, label, out.kind, out.why))
        self.assertEqual(unknown, [], "分かっていない誤りの値（表が閉じていない・重なりなど）")

    def test_every_known_gap_is_really_met_so_the_list_does_not_go_stale(self):
        met = {out.gap for *_, out in self.results if isinstance(out, Mistake) and out.gap}
        self.assertEqual(met, {g.id for g in h.GAPS})

    def test_the_gaps_are_only_events_that_cannot_happen(self):
        """2026-10-29：見つけた穴は、決定か、そのまま当てはまる「何もしない」で埋めた。残るのは、その段では起きない出来事だけ。"""
        self.assertEqual({g.kind for g in h.GAPS}, {"起きない"})

    def test_every_table_row_is_used(self):
        used = {row for *_, out in self.results if isinstance(out, Outcome) for row in out.rows}
        self.assertEqual(used, {r.id for r in h.TABLE})

    def test_the_table_holds_the_rows_of_chapter_17_once_each(self):
        ids = [r.id for r in h.TABLE]
        expected = ([f"L-{i}" for i in range(1, 34)] + [f"R-{i}" for i in range(1, 22)] +
                    [f"N-{i}" for i in range(1, 7)] + [f"C-{i}" for i in range(1, 11)] + [f"V-{i}" for i in range(1, 6)])
        self.assertEqual(sorted(ids), sorted(expected))   # 17章の60行と、2026-10-29 に穴を埋めた【当】の行

    def test_every_answer_is_a_whole_state_and_known_actions(self):
        for r, subject, state, label, e, out in self.results:
            if not isinstance(out, Outcome):
                continue
            with self.subTest(subject=subject, stage=state.stage.value, event=label):
                self.assertIsNone(h._contents_wrong(out.state))
                self.assertEqual(h.subject_of(out.state), subject)
                self.assertTrue(out.rows)
                self.assertTrue(all(a.what in h.DOS for a in out.actions))

    def test_nothing_rows_change_nothing(self):
        for *_, state, label, e, out in self.results:
            if isinstance(out, Outcome) and all(next(x for x in h.TABLE if x.id == row).nothing for row in out.rows):
                self.assertEqual((out.state, out.actions), (state, ()), (state, label))

    def test_a_mistake_asks_the_fixer_and_is_recorded(self):
        out = h.apply(Line(LS.HEALTHY), ev("E6"), R)
        self.assertIsInstance(out, Mistake)
        self.assertEqual((out.kind, out.gap), ("表に無い", "G-L6"))
        self.assertEqual([a.what for a in out.actions], [h.RECORD, h.FIX])
        self.assertEqual(out.actions[1].get("fixer"), "CL-2")

    def test_events_a_table_does_not_receive_and_broken_contents_are_mistakes(self):
        self.assertEqual(h.apply(Number(NS.NONE), ev("E6"), R).kind, "受けない出来事")
        self.assertEqual(h.apply(Line(LS.HEALTHY, t=2), ev("E1"), R).kind, "中身の誤り")
        self.assertEqual(h.apply(Line(LS.CHECK_A, run=RUN, run_start=T0, next_at=T0), Event("E10", T0), R).kind, "中身の誤り")
        self.assertEqual(h.apply(Line(LS.HEALTHY), Event("E2", T0), R).kind, "中身の誤り")
        self.assertEqual(h.apply(Line(LS.CHECK_A, run=RUN, run_start=T0, next_at=T0 + MIN), ev("E10", T0), R).kind, "時刻の前")

    def test_no_row_is_left_pending(self):
        """伺い中だった所（17-7-2 の (1)(2)(4)・N-4）は、2026-10-29 の管理者の決定で1つの形にした（切り替えの引数は持たない）。"""
        self.assertFalse([r.id for r in h.TABLE if "⏳" in r.basis])
        self.assertFalse(hasattr(h, "Awaiting"))
        self.assertEqual((h.AUDIO_STOP_AFTER_CUSTOMERS, R.audio_stop_after_customers), (2, 2))

    def test_the_settings_are_received_by_their_names(self):
        self.assertEqual(set(h.SETTING_KEYS.values()), set(SETTINGS))
        custom = rules(**{"line.test_ring_gaps": [60, 120]})
        self.assertEqual([custom.gap(t) for t in (1, 2, 3, 9)], [timedelta(seconds=s) for s in (60, 120, 120, 120)])
        self.assertEqual(R.lasting, timedelta(hours=1))


# ─────────────────────────────────────────────────────────────────────────────
# 抜け道
# ─────────────────────────────────────────────────────────────────────────────

# 時刻を持たない待ちは5つだけ：17-5 の4つ（確かめ中 (b) の「テスト番号が無い」・復旧待ち・休み・こちらの不具合で止め）と、
# 休みでテスト番号が無いとき（2026-10-29 の当てはめ。L-15・L-23 が「入るとき1回知らせて E14 を待つ」。手の出口は再開・外す・
# テスト番号を入れる）
TIMELESS = {("回線", "確かめ中 (b)", "テスト番号が無い"), ("行", "復旧待ち", None), ("行", "休み", None),
            ("契約者", "こちらの不具合で止め", None), ("回線", "回線のせい・休み", "テスト番号が無い"),
            ("回線", "声の不調・休み", "テスト番号が無い")}


def timeless_key(state):
    """待つ状態で、次に動く時刻を持たないなら、その鍵（欄・段・待つ理由）。待ちでない・時刻があるなら None。"""
    subject = h.subject_of(state)
    if isinstance(state, Line):
        waiting = state.stage not in (LS.HEALTHY, LS.REMOVED)
        return (subject, state.stage.value, state.wait.value if state.wait else None) if waiting and state.next_at is None else None
    if isinstance(state, Task):   # 判定待ちは回線の欄の時刻を読む（17-1-2）
        waiting = state.stage in (TS.AWAIT_RECOVERY, TS.RESTING, TS.VOICE_REDIAL)
        return (subject, state.stage.value, None) if waiting and h.next_at(state) is None else None
    if isinstance(state, Tenant):
        return (subject, state.stage.value, None) if state.stage is CS.STOPPED else None
    return None   # 番号の続きありは次の日を持つ（中身の確かめ）。試行のせいは待ちではない


def goal(state) -> bool:
    if isinstance(state, Line):
        return state.stage in (LS.HEALTHY, LS.REMOVED)
    if isinstance(state, Task):
        return state.stage is TS.ON_PLAN
    if isinstance(state, Number):
        return state.stage in (NS.NONE, NS.DEAD)
    if isinstance(state, Tenant):
        return state.stage is CS.NORMAL
    return True


def auto_events(state, ringing: bool, has_number: bool):
    """手で起こすもの（E12・E13・E14）を除いた、その状態で起こりうる出来事。"""
    now = latest(state) + MIN
    due = h.next_at(state)
    if isinstance(state, Line):
        if state.stage in {LS.HEALTHY} | h.CHECKING:
            yield from (ev("E1", now), ev("E2", now), ev("E4", now, number="n9"), ev("E5", now), ev("E3", now))
        if state.stage is LS.CHECK_C or ringing or state.wait is Wait.ANSWER:
            yield from (ev(c, now) for c in ("E6", "E7", "E8", "E9"))
        if due is not None:
            yield ev("E10", due, test_number=has_number)
    elif isinstance(state, Task):
        yield from (ev("E1", now), ev("E2", now), ev("E3", now), ev("E4", now), ev("E5", now), ev("E15", now),
                    ev("E5", now, own=False, started_at=now), ev("E18", now))
        for k in ({state.kind} | set(state.kinds)) - {None}:
            yield ev("E11", now, kind=k)
        for blame in ("番号", "回線"):
            yield ev("E16", now, blame=blame, attempt=state.attempt or "a1")
        if due is not None:
            yield ev("E10", due)
    elif isinstance(state, Number):
        yield from (ev("E1", now), ev("E2", now, attempt="a5"))
        for a in state.unknown:
            yield ev("E16", now, attempt=a, blame="回線", call_at=now - MIN)
    elif isinstance(state, Tenant):
        yield ev("E5", now)
        for k in state.stopped:
            yield ev("E11", now, kind=k)


def shape(state) -> tuple:
    """探す範囲を小さくするための、状態の形（時刻を除き、続きの電話は「あるか」だけ）。"""
    return (type(state).__name__,) + tuple((k, bool(v) if k == "run" else v) for k, v in vars(state).items()
                                            if not isinstance(v, datetime) and k != "removed")


def reaches_goal(state, r: Rules, has_number: bool, depth: int = 6) -> bool:
    frontier, seen = [(state, False)], set()
    for _ in range(depth):
        nxt = []
        for s, ringing in frontier:
            if goal(s):
                return True
            for e in auto_events(s, ringing, has_number):
                out = h.apply(s, e, r)
                if not isinstance(out, Outcome):
                    continue
                key = (shape(out.state), h.RING in whats(out))
                if key not in seen:
                    seen.add(key)
                    nxt.append((out.state, h.RING in whats(out)))
        frontier = nxt
    return any(goal(s) for s, _ in frontier)


def moved(state, out) -> bool:
    """表が次の状態を作った（引けて、状態が変わった）か。試験が手で作った状態をそのまま返しただけのもの（何もしない）は数えない。"""
    return isinstance(out, Outcome) and out.state != state


MANUAL = {"回線": ("E12", "E13", "E14"), "行": ("E12",), "契約者": ("E12",)}


class WaysOut(unittest.TestCase):
    """どの状態からも、元気（または終わり）へ行けるか、入るときに知らせがあり手の出口がある（17-5）。"""

    @classmethod
    def setUpClass(cls):
        cls.results = list(every_combination())

    def test_waits_without_a_time_are_only_the_five(self):
        found = {timeless_key(out.state) for *_, state, _, _, out in self.results if moved(state, out)} - {None}
        self.assertEqual(found, TIMELESS)

    def test_entering_a_wait_without_a_time_tells_the_contractor_and_the_administrator(self):
        for r, subject, state, label, e, out in self.results:
            if not isinstance(out, Outcome) or timeless_key(out.state) is None or timeless_key(state) == timeless_key(out.state):
                continue
            with self.subTest(subject=subject, stage=state.stage.value, event=label):
                told_now = any(a.what == h.NOTIFY and set(a.get("to")) == set(h.BOTH) for a in out.actions)
                told_before = "テスト番号が無い" in getattr(out.state, "told", frozenset())   # この続き・この休みで、もう1回知らせた
                self.assertTrue(told_now or told_before)

    def test_every_wait_without_a_time_has_a_manual_way_out(self):
        for r, subject, state, label, e, out in self.results:
            if not moved(state, out) or timeless_key(out.state) is None:
                continue
            with self.subTest(subject=subject, stage=out.state.stage.value, event=label):
                exits = [h.apply(out.state, ev(code, latest(out.state) + MIN), r) for code in MANUAL[subject]]
                self.assertTrue(any(isinstance(x, Outcome) and (goal(x.state) or timeless_key(x.state) is None) for x in exits))

    def test_every_state_reaches_healthy_or_the_end_or_is_a_told_wait_with_a_manual_way_out(self):
        states = {out.state for *_, out in self.results if isinstance(out, Outcome)}
        for subject, (make, _) in SUBJECTS.items():
            states |= set(make())
        for state, has_number in itertools.product(states, (True, False)):
            if reaches_goal(state, R, has_number):
                continue
            with self.subTest(state=state, has_number=has_number):
                # 自動で抜けられないのは、時刻を持たない待ち（入るときに知らせる＝上の試験）か、テスト番号の無い世界の、テスト番号を
                # 待つ回線だけ。どれも手の出口がある
                key = timeless_key(state)
                no_number = isinstance(state, Line) and not has_number
                self.assertTrue(key is not None or no_number)
                exits = [h.apply(state, ev(code, latest(state) + MIN), R) for code in MANUAL[h.subject_of(state)]]
                self.assertTrue(any(isinstance(x, Outcome) and goal(x.state) for x in exits))

    def test_every_wait_of_the_line_has_a_next_time_except_the_listed(self):
        for *_, out in self.results:
            if isinstance(out, Outcome) and isinstance(out.state, Line) and out.state.stage not in (LS.HEALTHY, LS.REMOVED):
                self.assertTrue(out.state.next_at is not None or out.state.wait is Wait.NO_NUMBER, out.state)


# ─────────────────────────────────────────────────────────────────────────────
# 決定の例（17-4。決定の文1つにつき1つ以上）
# ─────────────────────────────────────────────────────────────────────────────

def checking_c(t: int = 0, run=RUN) -> Line:
    return Line(LS.CHECK_C, run_start=T0 - 20 * MIN, run=run, t=t, next_at=T0 + MIN)


class Decisions(unittest.TestCase):

    def test_1015_a_person_in_charge_can_restart_by_hand_and_counts_start_again(self):
        """2026-10-15 管理者：「何で止まったのかは確認できるようにしてあるの？人の判断でが再開できる仕組みは？」（L-21・C-6）"""
        rest = Line(LS.AUDIO_REST, j=2, t=3, audio=frozenset({"n7"}), next_at=T0, entered_at=T0 - 60 * MIN)
        out = ok(self, h.apply(rest, ev("E12"), R))
        self.assertEqual((out.state, out.rows), (Line(LS.HEALTHY), ("L-21",)))   # t・m・j を0に
        self.assertIn("直ったことを確かめた", out.actions[0].get("about"))         # 理由を記録する
        stopped = Tenant(CS.STOPPED, rows=(("frozen", frozenset({"t1", "t2", "t3"})),), stopped=frozenset({"frozen"}), since=T0)
        out = ok(self, h.apply(stopped, ev("E12"), R))
        self.assertEqual((out.state, out.rows), (Tenant(CS.NORMAL), ("C-6",)))
        self.assertIn(h.RESUME_CALLS, whats(out))

    def test_1016_stopping_is_only_for_what_would_harm_customers(self):
        """2026-10-16 管理者：「止めるというのは、このまま進めると、お客様に対して、影響が出てしまうということだけですよ。」
        （休み・止めの入り口は L-3・L-10・C-1 だけ。こちらの不具合は回線を止めない。止めるのは新しい電話だけ）"""
        self.assertEqual(ok(self, h.apply(Line(LS.HEALTHY), ev("E3"), R)), (Line(LS.HEALTHY), (), ("L-4",)))
        out = ok(self, h.apply(Line(LS.HEALTHY), ev("E2"), R))
        self.assertEqual(out.state.stage, LS.CHECK_A)   # 鳴らなかった1本では止めない（次の行へかける）
        ok(self, h.apply(out.state, ev("E1", T0 + MIN), R))
        entries = set()   # 総当たりの中で、お客様へかけない状態（休み・止め）に入れた行（止める種類を足したのも入れる）
        for *_, state, label, e, out in every_combination():
            if not isinstance(out, Outcome):
                continue
            into_rest = isinstance(out.state, Line) and out.state.stage in h.RESTS and state.stage is not out.state.stage
            into_stop = isinstance(out.state, Tenant) and not out.state.stopped <= state.stopped
            if into_rest or into_stop:
                entries.add(out.rows[-1])   # E1 でもある E4 は L-5 のあとに L-3 を引く：入れたのは後ろの行
        self.assertEqual(entries, {"L-3", "L-10", "C-1", "L-25", "C-8"})
        three = Tenant(CS.NORMAL, rows=(("frozen", frozenset({"t1", "t2"})),))
        out = ok(self, h.apply(three, ev("E3", task="t3"), R))
        self.assertIn("話している電話は切らない", h.STOP_CALLS)
        self.assertEqual(whats(out)[0], h.STOP_CALLS)

    def test_1017_after_recovery_call_back_at_once_with_an_apology(self):
        """2026-10-17 管理者：「復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」（R-6）"""
        out = ok(self, h.apply(Task(TS.ON_PLAN, plan="p"), ev("E3", answered=True, rang=True), R))
        self.assertEqual((out.state.stage, out.rows), (TS.AWAIT_RECOVERY, ("R-5",)))
        back = ok(self, h.apply(out.state, ev("E5", T0 + 10 * MIN, own=False, started_at=T0 + MIN), R))
        self.assertEqual((back.state.stage, back.state.n, whats(back), back.rows), (TS.ON_PLAN, 1, [h.REDIAL], ("R-6",)))
        fixed = ok(self, h.apply(out.state, ev("E11", T0 + MIN), R))
        self.assertEqual(whats(fixed), [h.REDIAL])
        before = ok(self, h.apply(Task(TS.ON_PLAN, plan="p"), ev("E3"), R))     # お客様が出る前に終えた：かけた予定のまま
        self.assertEqual(ok(self, h.apply(before.state, ev("E11", T0 + MIN), R)).actions[0].get("how"), "かけた予定のまま")

    def test_1018_plan_a_immediate_apology_calls_twice_in_a_row_then_the_usual_rule(self):
        """2026-10-18 管理者 案A：お詫びのすぐのかけ直しは、同じ行で続けて2回まで。それでも切れたら、ふつうのかけ直しの決まり（R-11）。"""
        within = ok(self, h.apply(Task(TS.ON_PLAN), ev("E4"), R))
        self.assertEqual((within.state.stage, within.rows), (TS.VOICE_REDIAL, ("R-11",)))
        over = ok(self, h.apply(Task(TS.ON_PLAN), ev("E4", resume_allowed=False), R))
        self.assertEqual((over.state.stage, whats(over)), (TS.ON_PLAN, [h.COUNT, h.PLAN]))

    def test_1018_a_rest_lasting_an_hour_in_ringing_hours_asks_the_fixer_once(self):
        """2026-10-18 管理者：一時的な異常は待って自動で続ける（1時間以上続けば、直す担当へ修正依頼）。夜の待ちは数えない（L-20）。"""
        rest = Line(LS.LINE_REST, t=2, next_at=T0, entered_at=T0 - 30 * MIN, tested_at=T0 - 30 * MIN)
        self.assertNotIn("L-20", ok(self, h.apply(rest, ev("E10"), R)).rows)
        long = replace(rest, entered_at=T0 - 70 * MIN)
        out = ok(self, h.apply(long, ev("E10"), R))
        self.assertEqual(out.rows, ("L-15", "L-20"))
        self.assertIn(h.FIX, whats(out))
        again = ok(self, h.apply(replace(out.state, next_at=T0 + GAP[0]), ev("E10", T0 + GAP[0]), R))
        self.assertNotIn("L-20", again.rows)   # 同じ不具合で1つ
        night = replace(rest, entered_at=T0.replace(hour=6, minute=0) - timedelta(hours=10))   # 前の夜20時から：鳴らしてよい時間で数える
        night_out = ok(self, h.apply(replace(night, next_at=T0.replace(hour=7, minute=30)), ev("E10", T0.replace(hour=7, minute=30)), R))
        self.assertIn("L-20", night_out.rows)   # 20〜22時の2時間＋7:00〜7:30
        early = replace(rest, entered_at=T0.replace(hour=6) - timedelta(hours=8, minutes=30))  # 前の夜21:30から
        self.assertNotIn("L-20", ok(self, h.apply(replace(early, next_at=T0.replace(hour=7, minute=20)),
                                                 ev("E10", T0.replace(hour=7, minute=20)), R)).rows)   # 30分＋20分

    def test_1018_the_hour_is_a_time_the_rest_holds_so_it_is_not_late(self):
        """2026-10-18 管理者：1時間以上続けば修正依頼（L-20）。2026-10-29 の当てはめ：休みの中身に「入った時刻＋鳴らしてよい時間で
        1時間」を持ち、次のテストと早いほうで引く（あける長さの分だけ遅れない）。"""
        rest = ok(self, h.apply(checking_c(), ev("E7"), R)).state
        self.assertEqual((rest.fix_at, h.next_at(rest)), (T0 + 60 * MIN, T0 + GAP[0]))   # テストのほうが早い
        long = replace(rest, t=4, next_at=T0 + 2 * 60 * MIN)                               # テストが1時間より先
        self.assertEqual(h.next_at(long), T0 + 60 * MIN)
        out = ok(self, h.apply(long, ev("E10", T0 + 60 * MIN), R))
        self.assertEqual((out.rows, whats(out), out.state.next_at), (("L-20",), [h.FIX], T0 + 2 * 60 * MIN))   # テストは鳴らさない
        self.assertEqual(h.next_at(out.state), T0 + 2 * 60 * MIN)                          # 同じ不具合で1つ
        late = ok(self, h.apply(checking_c(), ev("E7", T0.replace(hour=21, minute=30)), R)).state
        self.assertEqual(late.fix_at, (T0 + timedelta(days=1)).replace(hour=7, minute=30))   # 21:30〜22:00 と 7:00〜7:30

    def test_1022_late_night_a_line_whose_test_does_not_ring_stops_calling_customers(self):
        """2026-10-22 深夜 管理者：「数えない分の歯止めは、回線の異常が続いたら回線を止める仕組み：OK」（L-10・L-17）"""
        out = ok(self, h.apply(checking_c(), ev("E7"), R))
        self.assertEqual((out.state.stage, out.state.t, out.state.next_at), (LS.LINE_REST, 1, T0 + GAP[0]))
        for code in ("E1", "E2", "E4"):   # 休みの回線はお客様へかけない（お客様の電話の出来事は来ない）
            self.assertEqual(h.apply(out.state, ev(code, T0 + MIN), R).gap, "G-L7")
        again = ok(self, h.apply(out.state, ev("E7", T0 + GAP[0]), R))
        self.assertEqual((again.state.stage, again.state.t, again.state.next_at), (LS.LINE_REST, 2, T0 + GAP[0] + GAP[1]))

    def test_1022_late_night_four_failures_in_a_row_on_a_number_end_it(self):
        """2026-10-22 深夜 管理者：「同じ番号で続けて当日2回＆翌日1回、さらに2日後1回の計4回受け付けられなかったら、その番号へは
        かけない」（N-1・N-2・N-5）"""
        num, days = Number(NS.NONE), []
        at = T0
        for i in range(3):
            out = ok(self, h.apply(num, ev("E16", at + MIN, attempt=f"a{i}", call_at=at), R))
            num = out.state
            days.append((num.next_day - T0.date()).days)
            at = datetime.combine(num.next_day, at.timetz()) if num.next_day > at.date() else at + 10 * MIN
        self.assertEqual(days, [0, 1, 3])   # 当日・翌日・その2日後
        out = ok(self, h.apply(num, ev("E16", at + MIN, attempt="a3", call_at=at), R))
        self.assertEqual((out.state.stage, out.rows), (NS.DEAD, ("N-1",)))
        self.assertEqual(ok(self, h.apply(out.state, ev("E1"), R)).rows, ("N-5",))
        run = ok(self, h.apply(Number(NS.NONE), ev("E16", call_at=T0), R)).state
        self.assertEqual(run.not_before, T0 + timedelta(seconds=300))   # 同じ日は同じお客様への最短の間隔のあと
        self.assertEqual(ok(self, h.apply(run, ev("E1"), R)).state, Number(NS.NONE))   # 続けて、でなくなれば数え直す

    def test_1023_calls_ending_before_the_customer_answers_count_the_same_way(self):
        """2026-10-23 管理者：「番号を受け付けないときと同じ。当日2回・翌日1回・その2日後1回の計4回続いたら、その番号へはかけない」
        （E2 の範囲。分からないの試行が番号のせいと決まれば、番号に数える）"""
        att = ok(self, h.apply(Attempt(Blame.EMPTY), ev("E2"), R))
        self.assertEqual((att.state, att.rows), (Attempt(Blame.UNKNOWN), ("V-1",)))
        num = ok(self, h.apply(Number(NS.NONE), ev("E2", attempt="a1"), R)).state
        self.assertEqual((num.stage, num.unknown), (NS.NONE, frozenset({"a1"})))   # まだ数えない
        num = ok(self, h.apply(num, ev("E16", attempt="a1", call_at=T0), R)).state
        self.assertEqual((num.stage, num.n, num.unknown), (NS.RUN, 1, frozenset()))

    def test_1024_number_fault_ends_the_row_line_fault_only_pauses_it(self):
        """2026-10-24 管理者：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は行は取りやめでなく
        一時中止だけですよ。」（R-2・R-3・L-10・N-3・V-3・V-4）"""
        waiting = Task(TS.AWAIT_DECISION, plan="p", line="L1", attempt="a1")
        by_number = ok(self, h.apply(waiting, ev("E16", blame="番号"), R))
        self.assertEqual((by_number.state, by_number.rows), (Task(TS.ON_PLAN, plan="p"), ("R-2",)))
        self.assertIn("番号の決まった順", by_number.actions[0].get("how"))
        by_line = ok(self, h.apply(waiting, ev("E16", blame="回線"), R))
        self.assertEqual(by_line.actions[0].get("how"), "かけた予定のまま、いまから（どの数にも数えない）")
        self.assertEqual(ok(self, h.apply(Number(NS.NONE), ev("E16", blame="回線"), R)).state, Number(NS.NONE))   # N-3 数えない
        self.assertEqual(ok(self, h.apply(Attempt(Blame.UNKNOWN), ev("E16", blame="番号"), R)).state, Attempt(Blame.NUMBER))
        self.assertEqual(ok(self, h.apply(Attempt(Blame.UNKNOWN), ev("E16", blame="回線"), R)).state, Attempt(Blame.LINE))
        out = ok(self, h.apply(checking_c(run=RUN + (("a2", "n2"),)), ev("E7"), R))
        self.assertEqual([(a.get("attempt"), a.get("blame")) for a in out.actions if a.what == h.DECIDED],
                         [("a1", "回線"), ("a2", "回線")])

    def test_1024_an_unknown_call_moves_on_to_the_next_row_from_the_first_time(self):
        """2026-10-24 管理者：「回線か番号かどちらかわからない場合は、その行に固執するのでなく、1回目から、次の行に進むようにして
        ください。次の行に進んで、次の行がつながればすぐに回線の問題か番号の問題か判断できますよね。一つの行で止まり続けることがない
        ようにしてください。」（L-2・L-5・L-6・R-1）"""
        first = ok(self, h.apply(Line(LS.HEALTHY), ev("E2", attempt="a1", number="n1"), R))
        self.assertEqual((first.state.stage, first.state.run), (LS.CHECK_A, (("a1", "n1"),)))
        second = ok(self, h.apply(first.state, ev("E2", T0 + MIN, attempt="a2", number="n2"), R))   # 確かめ中も次の行へかける
        self.assertEqual((second.rows, second.state.next_at), (("L-6",), T0 + GAP[0]))
        rang = ok(self, h.apply(second.state, ev("E1", T0 + 2 * MIN), R))                            # 次の行がつながった
        self.assertEqual(rang.state.stage, LS.HEALTHY)
        self.assertEqual([(a.get("attempt"), a.get("blame")) for a in rang.actions], [("a1", "番号"), ("a2", "番号")])
        row = ok(self, h.apply(Task(TS.ON_PLAN, plan="予定と約束の印"), ev("E2"), R))
        self.assertEqual((row.state.stage, row.state.plan), (TS.AWAIT_DECISION, "予定と約束の印"))   # 取りやめも次の日へ送りもしない

    def test_1024_with_no_rows_left_the_test_number_decides(self):
        """2026-10-24 管理者：「その回線にかける行が残っていないときは、『テスト番号』で決めます。前に鳴った電話があれば番号のせい、
        一度も鳴らせていなければ回線のせいです。（テスト番号はフェーズ1で知らせます。）」（L-7〜L-10）"""
        a = Line(LS.CHECK_A, run_start=T0, run=RUN, next_at=T0 + GAP[0])
        c = ok(self, h.apply(a, ev("E10", T0 + GAP[0]), R))
        self.assertEqual((c.state.stage, whats(c), c.rows), (LS.CHECK_C, [h.RING], ("L-7",)))
        self.assertEqual(c.state.next_at, T0 + GAP[0] + R.reply_wait)
        rang = ok(self, h.apply(c.state, ev("E6", T0 + GAP[0] + MIN), R))
        self.assertEqual((rang.state.stage, rang.actions[0].get("blame")), (LS.HEALTHY, "番号"))
        silent = ok(self, h.apply(c.state, ev("E7", T0 + GAP[0] + MIN), R))
        self.assertEqual((silent.state.stage, silent.actions[0].get("blame")), (LS.LINE_REST, "回線"))

    def test_1024_resuming_a_stopped_line_is_checked_with_the_test_number(self):
        """2026-10-24 管理者：「OK」（回線が止まったあとの再開の確かめも、テスト番号で。アプリが応答するかだけでは再開しない）
        （L-15・L-16・L-23・L-24）"""
        for rest in (Line(LS.LINE_REST, t=1, next_at=T0, entered_at=T0 - GAP[0], tested_at=T0 - GAP[0]),
                     Line(LS.AUDIO_REST, j=1, next_at=T0, entered_at=T0)):
            ring = ok(self, h.apply(rest, ev("E10"), R))
            self.assertEqual((ring.state.stage, whats(ring)), (rest.stage, [h.RING]))
            back = ok(self, h.apply(ring.state, ev("E6", T0 + MIN), R))
            self.assertEqual(back.state.stage, LS.HEALTHY)
            self.assertEqual([a.get("to") for a in back.actions if a.what == h.NOTIFY], [("契約者",)])
        self.assertNotIn("E1", {e for r in h.TABLE if r.stages & h.RESTS for e in r.events})   # ほかの道では再開しない

    def test_1024_night_wait_until_the_ringing_hours_begin(self):
        """2026-10-24 夜 管理者：「私の電話です。軌道に乗れば、新しい電話をもうひとつ契約予定なのでその後に考えます。この仕事専用なら
        夜でもOK」（L-8・L-15：鳴らしてよい時間の外は始まりまで待つ）"""
        late = T0.replace(hour=22, minute=30)
        a = Line(LS.CHECK_A, run_start=late - GAP[0], run=RUN, next_at=late)
        out = ok(self, h.apply(a, ev("E10", late), R))
        self.assertEqual((out.state.stage, out.state.wait, out.state.next_at, out.actions),
                         (LS.CHECK_B, Wait.OFF_HOURS, (T0 + timedelta(days=1)).replace(hour=7, minute=0), ()))
        rest = Line(LS.LINE_REST, t=1, next_at=late, entered_at=late - GAP[0], tested_at=late - GAP[0])
        self.assertEqual(ok(self, h.apply(rest, ev("E10", late), R)).state.next_at, (T0 + timedelta(days=1)).replace(hour=7))

    def test_1024_night_serious_trouble_and_poor_audio_are_told_apart(self):
        """2026-10-24 夜 管理者 甚大な問題：「プログラムやAIがこちらの意図しない相手がおかしいと思うような状況に陥った場合全般や、
        回線が悪く、声がお互いや片方がほとんど聞き取れない場合など規定通りの会話が進められない状態です。」（E3 と E4 の分け方）"""
        self.assertNotIn("E4", h.RECEIVES["契約者"])   # 声が聞き取れないは、こちらの不具合に数えない
        self.assertEqual(ok(self, h.apply(Line(LS.HEALTHY), ev("E3"), R)).rows, ("L-4",))   # こちらの不具合は回線に数えない
        self.assertEqual(ok(self, h.apply(Task(TS.ON_PLAN), ev("E3"), R)).state.stage, TS.AWAIT_RECOVERY)
        self.assertEqual(ok(self, h.apply(Task(TS.ON_PLAN), ev("E4"), R)).state.stage, TS.VOICE_REDIAL)

    def test_1025_the_contractor_chooses_the_ringing_hours_from_0_to_24(self):
        """2026-10-25 管理者：「止める回線を2択だけじゃなく、こちらが決められるようにして。昼間に私は寝ていることがあるし、夜中でも
        起きていることがある。0～24時の選択は残したうえで。」（L-8・L-15：鳴らしてよい時間は契約者の設定）"""
        night = T0.replace(hour=2)
        a = Line(LS.CHECK_A, run_start=night - GAP[0], run=RUN, next_at=night)
        allday = rules(**{"line.test_ring_hours": [["00:00", "24:00"]]})
        self.assertEqual(ok(self, h.apply(a, ev("E10", night), allday)).state.stage, LS.CHECK_C)
        split = rules(**{"line.test_ring_hours": [["22:00", "03:00"], ["12:00", "13:00"]]})   # 1日に何か所でも・日をまたいでも
        self.assertEqual(ok(self, h.apply(a, ev("E10", night), split)).state.stage, LS.CHECK_C)
        out = ok(self, h.apply(a, ev("E10", night.replace(hour=4)), split))
        self.assertEqual(out.state.next_at, night.replace(hour=12))

    def test_1025_the_gaps_are_15_30_60_120_240_minutes_set_per_contractor(self):
        """2026-10-25 管理者：「15⇒30⇒60⇒120⇒240分　がデフォルト契約者ごとに設定」（L-10・L-11・L-17・L-18・L-26・L-3）"""
        state, at, gaps = checking_c(), T0, []
        out = ok(self, h.apply(state, ev("E7", at), R))
        state = out.state
        gaps.append(state.next_at - at)
        for _ in range(6):
            at = state.next_at
            state = ok(self, h.apply(state, ev("E7", at), R)).state
            gaps.append(state.next_at - at)
        self.assertEqual([g.total_seconds() / 60 for g in gaps], [15, 30, 60, 120, 240, 240, 240])   # 240分で止まる
        unsure = ok(self, h.apply(checking_c(t=1), ev("E8"), R)).state
        self.assertEqual((unsure.wait, unsure.next_at), (Wait.GAP, T0 + GAP[1]))
        own = rules(**{"line.test_ring_gaps": [600, 1200]})   # 契約者ごとの設定
        self.assertEqual(ok(self, h.apply(checking_c(), ev("E7"), own)).state.next_at, T0 + timedelta(seconds=600))
        audio = Line(LS.AUDIO_REST, j=1, t=0, next_at=T0, entered_at=T0)
        self.assertEqual(ok(self, h.apply(audio, ev("E8"), R)).state.next_at, T0 + GAP[0])   # L-26

    def test_1025_the_same_row_twice_moves_on_three_rows_is_serious(self):
        """2026-10-25 管理者：「同じ行で、こちらのプログラムの不具合が続くとき：その行だけの問題なのか、すべての行にかかわる問題なのかを
        見極めないといけないでしょ？その行だけなら、契約者(管理者含む)と、開発AIの担当、記録にそれを伝え、2回続いたら、次の行へ。
        3行同じ問題なら、甚大な問題と判断。」（R-5・R-8・C-1・C-2）"""
        first = ok(self, h.apply(Task(TS.ON_PLAN), ev("E3"), R))
        self.assertEqual(whats(first), [h.RECORD, h.NOTIFY, h.FIX])   # 記録・契約者と管理者・開発AIの担当
        self.assertEqual(first.actions[1].get("to"), ("契約者", "管理者"))
        back = ok(self, h.apply(first.state, ev("E11", T0 + MIN), R)).state
        second = ok(self, h.apply(back, ev("E3", T0 + 2 * MIN), R))
        self.assertEqual((second.state.stage, second.rows), (TS.RESTING, ("R-8",)))   # 2回続いたら次の行へ
        self.assertIn("次の行へ進みます", second.actions[-1].get("about"))
        ten = Tenant(CS.NORMAL)
        for i, task in enumerate(("t1", "t1", "t2")):
            ten = ok(self, h.apply(ten, ev("E3", task=task), R)).state
        self.assertEqual((ten.stage, len(ten.r("frozen"))), (CS.NORMAL, 2))   # 同じ行の2回目は数えない（C-2）
        out = ok(self, h.apply(ten, ev("E3", task="t3"), R))
        self.assertEqual((out.state.stage, out.rows), (CS.STOPPED, ("C-1",)))
        other = ok(self, h.apply(Tenant(CS.NORMAL, rows=(("frozen", frozenset({"t1", "t2"})),)),
                                 ev("E3", kind="runner_error", task="t3"), R))
        self.assertEqual(other.state.stage, CS.NORMAL)   # 同じ問題（種類）で数える

    def test_1025_night_a_ringing_test_is_just_ok(self):
        """2026-10-25 夜 管理者：「鳴らなかった場合に、15分→30分→60分→120分→240分といったよね？…鳴ったら、テストＯＫなだけじゃない
        の？」（L-9・L-16・L-24：鳴ったらあけない・1回で再開）"""
        out = ok(self, h.apply(checking_c(t=3), ev("E6"), R))
        self.assertEqual(out.state, Line(LS.HEALTHY))   # t を持たない
        again = ok(self, h.apply(out.state, ev("E2", T0 + MIN), R)).state
        nxt = ok(self, h.apply(again, ev("E10", again.next_at), R))
        self.assertEqual(nxt.state.stage, LS.CHECK_C)   # 前のテストからあける長さは無い
        rest = Line(LS.LINE_REST, t=4, next_at=T0, entered_at=T0 - 5 * 60 * MIN, tested_at=T0 - 120 * MIN)
        self.assertEqual(ok(self, h.apply(rest, ev("E6"), R)).state, Line(LS.HEALTHY))

    def test_1025_night_fifteen_minutes_with_no_new_setting(self):
        """2026-10-25 夜 管理者：「その回線で次にかけるのは15分後と決めたんだから、15分になるんじゃないの？新たな設定はいらないでしょ？」
        （L-2・L-7：段 (a) は最初の鳴らなかった電話から、並びの1つ目）"""
        a = ok(self, h.apply(Line(LS.HEALTHY), ev("E2"), R)).state
        self.assertEqual(a.next_at, T0 + GAP[0])
        later = ok(self, h.apply(a, ev("E2", T0 + 10 * MIN, attempt="a2"), R)).state
        self.assertEqual(later.next_at, T0 + GAP[0])   # 続きに足しても、数え始めは最初の電話のまま【当(5)】
        own = rules(**{"line.test_ring_gaps": [300, 600]})
        self.assertEqual(ok(self, h.apply(Line(LS.HEALTHY), ev("E2"), own)).state.next_at, T0 + timedelta(seconds=300))

    def test_1025_night_busy_power_off_or_out_of_range_means_the_line_works(self):
        """2026-10-25 夜 管理者：「テスト番号が話し中・電源オフ・圏外だと分かるのなら、回線の問題じゃないんじゃないの？」（E6 の範囲：
        発信できた答えは、どれも E6＝回線は元気、続きの電話は番号のせい）"""
        out = ok(self, h.apply(checking_c(run=RUN + (("a2", "n2"),)), ev("E6"), R))
        self.assertEqual((out.state.stage, {a.get("blame") for a in out.actions}), (LS.HEALTHY, {"番号"}))
        self.assertEqual(h.EVENT_NAMES["E6"], "テスト番号が鳴った")

    def test_1025_night_an_unclear_answer_is_told_at_once_one_time(self):
        """2026-10-25 夜 管理者：「OK」（見分けられないと返ったら、すぐ1回知らせる）（L-11・L-18・L-26）"""
        first = ok(self, h.apply(checking_c(), ev("E8"), R))
        self.assertEqual(whats(first), [h.NOTIFY, h.FIX])
        self.assertEqual(first.actions[1].get("fixer"), "CL-2")   # 電話をつなぐ部品の直す担当（errors.PHONE_FIXER）
        c = replace(first.state, stage=LS.CHECK_C, wait=None, next_at=T0 + 20 * MIN)
        self.assertEqual(ok(self, h.apply(c, ev("E8", T0 + 20 * MIN), R)).actions, ())   # この続きで1回だけ
        for rest in (Line(LS.LINE_REST, t=1, next_at=T0, entered_at=T0, tested_at=T0), Line(LS.AUDIO_REST, j=1, next_at=T0, entered_at=T0)):
            out = ok(self, h.apply(rest, ev("E8"), R))
            self.assertEqual(whats(out), [h.NOTIFY, h.FIX])
            self.assertEqual(ok(self, h.apply(out.state, ev("E8", T0 + GAP[0]), R)).actions, ())

    def test_1025_night_plan_a_poor_audio_counts_against_the_line_and_fixed_versions_restore(self):
        """2026-10-25 夜 管理者：(1)「声が聞き取れない」の数え方「案A」・(2) 戻す条件「案A」・(3) 3行の数え方「案A」
        （L-3・R-11・R-10・C-5・C-3）"""
        r = R
        line = ok(self, h.apply(Line(LS.HEALTHY), ev("E4", number="n1"), r)).state
        self.assertEqual((line.stage, line.m), (LS.HEALTHY, 1))
        same = ok(self, h.apply(line, ev("E4", T0 + MIN, number="n1"), r)).state
        self.assertEqual(same.m, 1)   # 同じお客様は数えない（違うお客様で続けば）
        stop = ok(self, h.apply(same, ev("E4", T0 + 2 * MIN, number="n2"), r))
        self.assertEqual((stop.state.stage, stop.state.j), (LS.AUDIO_REST, 1))
        resting = Task(TS.RESTING, n=1, n_kind="frozen", kinds=frozenset({"frozen", "runner_error"}), attempt="a1", answered=True,
                       since=T0)
        back = ok(self, h.apply(resting, ev("E11", kind="runner_error"), r))
        self.assertEqual((back.state.stage, back.state.n, whats(back)), (TS.ON_PLAN, 0, [h.REDIAL]))
        unmoved = ok(self, h.apply(resting, ev("E5", own=False, started_at=T0 + MIN), r))   # 話し終えた電話では戻らない
        self.assertEqual((unmoved.state, unmoved.rows), (resting, ("R-16",)))
        stopped = Tenant(CS.STOPPED, rows=(("frozen", frozenset({"t1", "t2", "t3"})),), stopped=frozenset({"frozen"}), since=T0)
        self.assertEqual(ok(self, h.apply(stopped, ev("E11"), r)).state, Tenant(CS.NORMAL))   # C-5
        counted = Tenant(CS.NORMAL, rows=(("frozen", frozenset({"t1", "t2"})), ("poor_db", frozenset({"t3"}))))
        self.assertEqual(ok(self, h.apply(counted, ev("E5"), r)).state, Tenant(CS.NORMAL))   # C-3 話し終えた電話で数え直す

    def test_1029_after_a_ringing_test_one_more_poor_call_stops_it_again_and_the_gaps_grow(self):
        """2026-10-29 管理者：「テスト番号が鳴れば再開のあと、また聞き取れなければ止め直し、次に試すまで 15→30→60→120→240分
        あけます。+管理者か契約者が、直ったことを確かめて手で再開も可能」（L-3・L-23・L-24・L-21・【当(3)】）"""
        for r in (R,):
            rest = Line(LS.AUDIO_REST, j=1, audio=frozenset({"n1", "n2"}), next_at=T0, entered_at=T0)
            resumed = chain(self, rest, ev("E10"), ev("E6", T0 + MIN), r=r)[-1].state
            self.assertEqual((resumed.stage, resumed.j, resumed.m), (LS.HEALTHY, 1, 0))   # j は残す
            again = ok(self, h.apply(resumed, ev("E4", T0 + 2 * MIN, number="n3"), r))   # 1本で止め直す
            self.assertEqual((again.state.stage, again.state.j, again.state.next_at),
                             (LS.AUDIO_REST, 2, T0 + 2 * MIN + GAP[0]))
            third = chain(self, again.state, ev("E10", again.state.next_at), ev("E6", again.state.next_at + MIN),
                          ev("E4", again.state.next_at + 2 * MIN, number="n4"), r=r)[-1]
            self.assertEqual(third.state.next_at - (again.state.next_at + 2 * MIN), GAP[1])   # 15→30
            talked = ok(self, h.apply(resumed, ev("E5", T0 + 3 * MIN), r)).state
            self.assertEqual(talked.j, 0)   # 声が聞き取れて話し終えた電話で0に戻る
        manual = ok(self, h.apply(Line(LS.AUDIO_REST, j=3, next_at=T0, entered_at=T0), ev("E12"), R))
        self.assertEqual(manual.state, Line(LS.HEALTHY))

    def test_1029_a_temporary_test_number_in_the_settings_and_tell_when_none(self):
        """2026-10-29 管理者：「一時的なtestcallは環境設定に追加」（L-8・L-13・L-15：テスト番号が無いときは知らせて待つ。E14 で動く）"""
        a = Line(LS.CHECK_A, run_start=T0, run=RUN, next_at=T0 + GAP[0])
        none = ok(self, h.apply(a, ev("E10", T0 + GAP[0], test_number=False), R))
        self.assertEqual((none.state.stage, none.state.wait, none.state.next_at), (LS.CHECK_B, Wait.NO_NUMBER, None))
        self.assertEqual(whats(none), [h.NOTIFY])
        self.assertIn("テスト番号を入れてください", none.actions[0].get("about"))
        given = ok(self, h.apply(none.state, ev("E14", T0 + GAP[0] + MIN), R))
        self.assertEqual((given.state.stage, whats(given), given.rows), (LS.CHECK_C, [h.RING], ("L-13",)))
        rest = Line(LS.LINE_REST, t=1, next_at=T0, entered_at=T0 - GAP[0], tested_at=T0 - GAP[0])
        waiting = ok(self, h.apply(rest, ev("E10", test_number=False), R))
        self.assertEqual((waiting.state.wait, waiting.state.next_at, whats(waiting)), (Wait.NO_NUMBER, None, [h.NOTIFY]))

    def test_1029_one_column_each_for_line_row_and_contractor(self):
        """2026-10-29 管理者：「回線・行・契約者の状態を、それぞれ1つの欄に持ちます。OK」（17-1 全部：欄ごとに段は1つ、次の状態は
        入口が返す値だけ）"""
        for cls, stage_cls in ((Line, h.LineStage), (Task, h.TaskStage), (Tenant, h.TenantStage), (Number, h.NumberStage)):
            fields = [f for f, t in cls.__annotations__.items() if "Stage" in str(t)]
            self.assertEqual(fields, ["stage"], cls)
            self.assertTrue(all(isinstance(s.value, str) for s in stage_cls))
        state = Line(LS.HEALTHY)
        out = ok(self, h.apply(state, ev("E2"), R))
        self.assertEqual(state, Line(LS.HEALTHY))   # 入口は今の状態を書き換えず、次の状態を値で返す

    def test_1029_every_check_knows_which_step_and_how_many_times(self):
        """2026-10-29 管理者：「分からないで放置はないんじゃないですか？どの確認中で何回目かなどが分かるんじゃないの？」（確かめ中の段
        (a)(b)(c)・中身の t と次に動く時刻・この章の決まり (2)）"""
        steps = chain(self, Line(LS.HEALTHY), ev("E2"), ev("E10", T0 + GAP[0]), ev("E8", T0 + GAP[0] + MIN))
        a, c, b = (o.state for o in steps)
        self.assertEqual((a.stage, a.t, a.next_at), (LS.CHECK_A, 0, T0 + GAP[0]))
        self.assertEqual((c.stage, c.t), (LS.CHECK_C, 0))
        self.assertEqual((b.stage, b.wait, b.t, b.next_at), (LS.CHECK_B, Wait.GAP, 1, T0 + GAP[0] + MIN + GAP[0]))
        withheld = ok(self, h.apply(c, ev("E9", T0 + GAP[0] + MIN), R)).state
        self.assertEqual((withheld.wait, withheld.t, withheld.next_at), (Wait.WITHHELD, 0, T0 + GAP[0] + MIN + GAP[0]))

    def test_1029_removed_means_taken_off_by_a_person_in_charge(self):
        """2026-10-29 管理者：「使わないとはどういうこと？」（外した＝契約者か管理者がその回線を外した終わりの状態。L-14・L-22・L-27）"""
        out = ok(self, h.apply(Line(LS.CHECK_B, run_start=T0, run=RUN, wait=Wait.GAP, next_at=T0 + MIN), ev("E13"), R))
        self.assertEqual((out.state.stage, out.state.removed), (LS.REMOVED, ("契約者", T0, "スマホを替えた")))
        self.assertEqual([(a.get("attempt"), a.get("blame")) for a in out.actions if a.what == h.DECIDED], [("a1", "回線")])
        rest = ok(self, h.apply(Line(LS.LINE_REST, t=1, next_at=T0, entered_at=T0, tested_at=T0), ev("E13"), R))
        self.assertEqual((rest.state.stage, rest.rows), (LS.REMOVED, ("L-22",)))
        for code in ("E1", "E6", "E12"):   # 終わり：システムは入れない・抜けない
            self.assertEqual(ok(self, h.apply(out.state, ev(code, T0 + MIN), R)), (out.state, (), ("L-27",)))

    def test_1029_once_the_lines_are_ready_plan_a_is_fine(self):
        """2026-10-29 管理者：「回線が整えば、案Aでいい」（R-11・R-12：声が通ると分かっている回線から、お詫びとともにすぐかける）"""
        wait = ok(self, h.apply(Task(TS.ON_PLAN, plan="p"), ev("E4", line="L1"), R)).state
        self.assertEqual((wait.stage, wait.line), (TS.VOICE_REDIAL, "L1"))
        out = ok(self, h.apply(wait, ev("E18", T0 + MIN, line="L2"), R))
        self.assertEqual((out.state.stage, out.rows, out.actions[0].what, out.actions[0].get("line")),
                         (TS.ON_PLAN, ("R-12",), h.REDIAL, "L2"))


class Decided1029(unittest.TestCase):
    """17-7-2 の伺いへの、2026-10-29 の管理者の決定（伺い中の切り替えは外し、決まった形1つ）。"""

    def test_1_a_line_fault_is_told_when_the_next_test_does_not_ring_either(self):
        """2026-10-29 管理者の決定 (1) 案B：休みに入った時からお客様へかけないのは同じ。知らせは、次のテスト（15分後）でも鳴らなかった
        とき（L-17 で知らせる。休みの中身に「知らせたか」を持つ）。"""
        first = ok(self, h.apply(checking_c(), ev("E7"), R))
        self.assertEqual((first.state.stage, first.state.next_at), (LS.LINE_REST, T0 + GAP[0]))   # お客様へかけない
        self.assertNotIn(h.NOTIFY, whats(first))
        self.assertNotIn("回線のせい", first.state.told)
        second = ok(self, h.apply(first.state, ev("E7", first.state.next_at), R))
        self.assertEqual([a.get("to") for a in second.actions if a.what == h.NOTIFY], [("契約者", "管理者")])
        self.assertIn("回線のせい", second.state.told)   # 知らせたか
        third = ok(self, h.apply(second.state, ev("E7", second.state.next_at), R))
        self.assertNotIn(h.NOTIFY, whats(third))   # この休みで1回
        audio = Line(LS.AUDIO_REST, j=1, next_at=T0, entered_at=T0)   # 声の休みから回線のせいへ（L-25）も、次で知らせる
        moved_in = ok(self, h.apply(audio, ev("E7"), R)).state
        self.assertIn(h.NOTIFY, whats(ok(self, h.apply(moved_in, ev("E7", moved_in.next_at), R))))

    def test_2_two_different_customers_stop_a_line(self):
        """2026-10-29 管理者の決定 (2)：2人（違うお客様の電話が2本続いたら声の不調・休み。回線の異常3件の基準は通さない）。"""
        one = ok(self, h.apply(Line(LS.HEALTHY), ev("E4", number="n1"), R)).state
        self.assertEqual((one.stage, one.m), (LS.HEALTHY, 1))
        two = ok(self, h.apply(one, ev("E4", T0 + MIN, number="n2"), R))
        self.assertEqual((two.state.stage, two.state.m, two.rows), (LS.AUDIO_REST, 2, ("L-3",)))
        self.assertIn(h.NOTIFY, whats(two))

    def test_4_with_no_line_known_to_carry_voice_call_back_on_the_same_line_after_15_minutes(self):
        """2026-10-29 管理者の決定 (4) 案A：声が聞き取れなかった電話から15分（並びの1つ目。新しい設定は作らない）のうちに、はさむ行も
        ほかの元気な回線も無ければ、同じ回線でお詫びとともにかけ直す（続けて2回までのまま）。"""
        wait = ok(self, h.apply(Task(TS.ON_PLAN), ev("E4", line="L1"), R))
        self.assertEqual((wait.state.next_at, wait.actions), (T0 + GAP[0], ()))
        out = ok(self, h.apply(wait.state, ev("E10", wait.state.next_at), R))
        self.assertEqual((out.state.stage, out.rows, whats(out), out.actions[0].get("line")), (TS.ON_PLAN, ("R-13",), [h.REDIAL], "L1"))
        sooner = ok(self, h.apply(wait.state, ev("E18", T0 + 5 * MIN, line="L2"), R))   # 15分のうちに声の通る回線が来た
        self.assertEqual((sooner.rows, sooner.actions[0].get("line")), (("R-12",), "L2"))
        self.assertEqual(h.apply(wait.state, ev("E10", T0 + 5 * MIN), R).kind, "時刻の前")
        own = rules(**{"line.test_ring_gaps": [600, 1200]})   # 並びの1つ目（契約者の設定）
        self.assertEqual(ok(self, h.apply(Task(TS.ON_PLAN), ev("E4"), own)).state.next_at, T0 + timedelta(seconds=600))
        over = ok(self, h.apply(Task(TS.ON_PLAN), ev("E4", resume_allowed=False), R))
        self.assertEqual(over.state.stage, TS.ON_PLAN)   # 続けて2回を超えたら、ふつうの再発信の決まり

    def test_n4_hold_the_number_while_counting_the_unknown_call_would_use_up_today(self):
        """2026-10-29 管理者の決定 N-4 案A：今の形どおり（分からない電話を番号のせいに数えると今日の分を使い切るあいだ、その番号の行は
        どの回線でもかけない。決まれば外す）。"""
        num = Number(NS.RUN, n=1, first_day=T0.date(), last_end=T0, next_day=T0.date(), not_before=T0 + 5 * MIN)
        marked = ok(self, h.apply(num, ev("E2", T0 + 10 * MIN, attempt="a2"), R)).state   # 数えると2回目＝今日の分を使い切る
        self.assertTrue(marked.hold)
        cleared = ok(self, h.apply(marked, ev("E16", T0 + 20 * MIN, attempt="a2", blame="回線", call_at=T0 + 10 * MIN), R))
        self.assertEqual((cleared.state.hold, cleared.rows), (False, ("N-3", "N-4")))   # 決まれば外す
        counted = ok(self, h.apply(marked, ev("E16", T0 + 20 * MIN, attempt="a2", blame="番号", call_at=T0 + 10 * MIN), R))
        self.assertEqual((counted.state.n, counted.state.hold), (2, False))
        fresh = ok(self, h.apply(Number(NS.NONE), ev("E2"), R)).state
        self.assertFalse(fresh.hold)   # 1回目は、数えても今日のうちにもう1回かけられる



class Filled1029(unittest.TestCase):
    """2026-10-29：見つけた穴を、管理者の決定か、そのまま当てはまる「何もしない」で埋めた行（根拠【当】。17章の決まり (3)）。"""

    def test_line_rows(self):
        self.assertEqual(ok(self, h.apply(Line(LS.HEALTHY), ev("E13"), R)).rows, ("L-29",))   # 元気でも外せる（試行は書かない）
        self.assertEqual(ok(self, h.apply(Line(LS.HEALTHY), ev("E13"), R)).state.stage, LS.REMOVED)
        for stage in (LS.LINE_REST, LS.AUDIO_REST):   # 休みでテスト番号が無い × E14：次に動く時刻＝いま → すぐ鳴らす
            rest = Line(stage, t=1 if stage is LS.LINE_REST else 0, j=1 if stage is LS.AUDIO_REST else 0, wait=Wait.NO_NUMBER,
                        entered_at=T0 - 20 * MIN, tested_at=T0 - 20 * MIN if stage is LS.LINE_REST else None,
                        told=frozenset({"テスト番号が無い"}))
            given = ok(self, h.apply(rest, ev("E14"), R))
            self.assertEqual((given.rows, given.state.next_at, given.state.wait), (("L-30",), T0, None))
            self.assertEqual(whats(ok(self, h.apply(given.state, ev("E10"), R))), [h.RING])
        self.assertEqual(ok(self, h.apply(Line(LS.HEALTHY), ev("E14"), R)), (Line(LS.HEALTHY), (), ("L-31",)))
        for state in (Line(LS.HEALTHY), checking_c()):   # 手の再開は休みの状態だけ：記録だけ
            out = ok(self, h.apply(state, ev("E12"), R))
            self.assertEqual((out.state, whats(out), out.rows), (state, [h.RECORD], ("L-32",)))
        deadline = ok(self, h.apply(checking_c(), ev("E10", T0 + MIN), R))   # (c) の答えの期限：E9 と同じ1か所
        self.assertEqual((deadline.rows, deadline.state.wait), (("L-12",), Wait.WITHHELD))
        ringing = ok(self, h.apply(Line(LS.LINE_REST, t=1, next_at=T0, entered_at=T0 - GAP[0], tested_at=T0 - GAP[0]),
                                 ev("E10"), R)).state
        self.assertEqual(ringing.wait, Wait.ANSWER)                          # 休みで鳴らした：答えを待つ
        self.assertEqual(ok(self, h.apply(ringing, ev("E10", ringing.next_at), R)).rows, ("L-19",))   # 休みの答えの期限も同じ形
        removed = Line(LS.REMOVED, removed=("契約者", T0, "x"))
        out = ok(self, h.apply(removed, ev("E2", T0 + MIN, attempt="a7"), R))   # 外す前からかけていた電話が鳴らずに終わった
        self.assertEqual([(a.what, a.get("attempt"), a.get("blame")) for a in out.actions], [(h.DECIDED, "a7", "回線")])
        self.assertIn("答えの期限", h.EVENT_NAMES["E10"])
        self.assertNotIn("期限", h.EVENT_NAMES["E9"])

    def test_row_rows(self):
        cases = [(Task(TS.ON_PLAN), ev("E1"), "R-15"), (Task(TS.ON_PLAN), ev("E5"), "R-15"),
                 (Task(TS.AWAIT_DECISION, line="L1", attempt="a1"), ev("E5", own=False, started_at=T0), "R-16"),
                 (Task(TS.ON_PLAN), ev("E11"), "R-17"),
                 (Task(TS.AWAIT_RECOVERY, n=1, n_kind="frozen", kind="frozen", attempt="a1", ended_at=T0),
                  ev("E5", own=False, started_at=T0 - MIN), "R-18"),
                 (Task(TS.RESTING, n=1, n_kind="frozen", kinds=frozenset({"frozen"}), attempt="a1", since=T0),
                  ev("E11", kind="poor_db"), "R-19"),
                 (Task(TS.ON_PLAN), ev("E16"), "R-20"),
                 (Task(TS.AWAIT_DECISION, line="L1", attempt="a1"), ev("E16", attempt="a2"), "R-20")]
        for state, e, row in cases:
            with self.subTest(row=row, stage=state.stage.value, event=e.code):
                self.assertEqual(ok(self, h.apply(state, e, R)), (state, (), (row,)))
        for state in (Task(TS.ON_PLAN), Task(TS.AWAIT_DECISION, line="L1", attempt="a1"),
                      Task(TS.VOICE_REDIAL, line="L1", since=T0, next_at=T0 + GAP[0])):
            out = ok(self, h.apply(state, ev("E12"), R))
            self.assertEqual((out.state, whats(out), out.rows), (state, [h.RECORD], ("R-21",)))

    def test_number_contractor_and_attempt_rows(self):
        self.assertEqual(ok(self, h.apply(Number(NS.NONE), ev("E1"), R)), (Number(NS.NONE), (), ("N-6",)))
        normal = Tenant(CS.NORMAL)
        self.assertEqual(whats(ok(self, h.apply(normal, ev("E12"), R))), [h.RECORD])   # C-9
        stopped = Tenant(CS.STOPPED, rows=(("frozen", frozenset({"t1", "t2", "t3"})), ("poor_db", frozenset({"t4"}))),
                         stopped=frozenset({"frozen"}), since=T0)
        other = ok(self, h.apply(stopped, ev("E11", kind="poor_db"), R))   # C-10：止めていない種類の r_k を0に
        self.assertEqual((other.rows, other.state.r("poor_db"), other.state.stopped), (("C-10",), frozenset(), frozenset({"frozen"})))
        one = ok(self, h.apply(stopped, ev("E3", kind="poor_db", task="t5"), R))   # C-8 を一般に：数えるだけ
        self.assertEqual((one.state.r("poor_db"), one.state.stopped, one.actions), (frozenset({"t4", "t5"}), frozenset({"frozen"}), ()))
        third = ok(self, h.apply(one.state, ev("E3", kind="poor_db", task="t6"), R))   # 3行目：止める種類に足す
        self.assertEqual(third.state.stopped, frozenset({"frozen", "poor_db"}))
        self.assertIn(h.NOTIFY, whats(third))
        same = ok(self, h.apply(stopped, ev("E3", kind="frozen", task="t9"), R))   # 止めている種類：数えるだけ
        self.assertEqual((len(same.state.r("frozen")), same.state.stopped, same.actions), (4, frozenset({"frozen"}), ()))
        self.assertEqual(ok(self, h.apply(stopped, ev("E3", kind="frozen", task="t1"), R)).state, stopped)   # 同じ行の2回目
        rang = ok(self, h.apply(Attempt(Blame.EMPTY), ev("E3", rang=True, answered=True), R))
        self.assertEqual((rang.state, rang.rows), (Attempt(Blame.OURS), ("V-2",)))   # 鳴った・鳴らないで分けない


if __name__ == "__main__":
    unittest.main()
