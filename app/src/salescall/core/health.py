"""回線・行・番号・契約者の状態の欄と、試行のせいの欄を、表（core/health_rules）を引いて移す入口と、それを読む口（DESIGN.md 17章。持ち主 CL-2）。

2026-10-29 管理者の承認：「回線・行・契約者の状態を、それぞれ1つの欄に持ちます。OK」。

**4段目**（いまの形）：出来事を起こす所（17-2 の「起こす所」）が、この部品の入口を呼ぶ。入口は、データベースの欄（表の作り 0145・0146）
から今の状態を鍵を取って読み（回線→契約者の順。行・番号・試行のせいはそのあと＝`ORDER`）、表を引き（health_rules.apply。呼ぶのはこの部品の
`_Run.give` の1か所）、次の状態と移った記録（どの出来事で・どの段からどの段へ・引いた表の行番号・することの並び）を書き、**することを行う**
（`_Run.save` → `_execute` の1か所。17章の決まり (1)：状態を変えるのは、表を引く入口1つだけ）。
- 行うこと（表のすることの名前 → 行う所）：
  - 知らせる（NOTIFY）→ core/notify.emit（種類は表が決めた note。契約者・管理者のあて先も表のとおり）。
  - 修正依頼（FIX）→ core/fix_requests.request（担当・種類・鍵は表が決めたもの。鍵の {tenant} は取引の契約者）。
  - こちらの不具合で終えた電話ごとの記録・知らせ・修正依頼（R-5・R-8 の3つ。中身に call）→ core/watch.record_error の1か所（エラーの記録・
    電話ごとの知らせ・同じ不具合で1つの修正依頼。今の作りの形のまま。止めるかは決めない＝止めるのは契約者の表の C-1）。
  - 新しい電話を止める（STOP_CALLS）・戻す（RESUME_CALLS）→ core/stops.stop・resume（契約者の範囲の停止の記録。門と候補は、ほかの契約者の
    停止と同じ1か所＝停止の記録で止まる。知らせは表の NOTIFY が出すので、停止の記録は知らせない）。
  - テスト番号を鳴らす（RING）→ 電話の部品を持つ回線ごとの処理（supervisor/line.py）が、回線の欄を読んで鳴らす（`tick` の答えの ring。
    回線の欄が「確かめ中 (c)」か「休みで答え待ち」なら鳴らす＝表がそう置いたことが、鳴らす頼み）。
  - 記録する（RECORD）・試行のせいを書く・試行のせいが決まった（E16）→ 移った記録と、欄そのもの（この部品が書く）。
  - 行の次の予定を頼む（PLAN）・お詫びとともにすぐかけ直す（REDIAL）・行の回数に数える（COUNT）・判定待ちの電話を待たずに締める（CLOSE）→
    締めの仕事（core/settle。待っていた電話を、行の欄が判定待ちでなくなったら締め、次の動き＝core/next_action が、試行のせいの欄と番号の
    欄を読んで予定を書く。お詫びの印・すぐのかけ直しの回数は次の動きの1か所＝salescall.resume_allowed・dial_kind）。復旧待ち・休み・声の
    かけ直し待ちの行の予定は、締めの時に次の動きが書いたまま残っていて、行の欄が予定どおりに戻れば、門と候補がその予定でかける。
- 表を引けなかったとき（誤りの値）は、状態はそのままで、誤りを移った記録に残し、直す担当（CL-2）へ修正依頼を出す（17章の決まり (1)）。
- この部品の誤り（データベースの誤りなど）は、呼んだ取引ごと失敗させる（⛔ 3段目の影は、失敗しても今の作りを続けた。いまは状態の欄が
  答えそのものなので、書けなかった出来事を黙って捨てない。ほかの取引とのぶつかりは、読み書きの層の1か所＝access が取引ごとやり直す）。

出来事と入口（17-2。入口を呼んでよい所は checks/rules.py の PY_ENTRY_CALLERS）：
- E1〜E5（電話が終わった）：`after_call`。電話が終わった所＝回線ごとの処理の通話のあと（supervisor/line.py。同じ回線の電話の順のまま渡る）と、
  通話ごとの口（こちらの不具合で終えた電話＝core/troubles.after_trouble）。締めの処理（core/settle.settle）も、締める前に呼ぶ（まだ渡して
  いなければ渡す。1つの試行の出来事は1回だけ＝試行のせいの欄の行があれば、もう渡した）。**E4（声が聞き取れずに終えた）の、行への分だけは
  締めのあと**（`after_settle`。すぐのかけ直しが続けて2回のうちか＝salescall.resume_allowed が、締めが残す「途中で切れたか」を読むため）。
  ⛔ 3段目は締めのあとに全部を渡したので、通話後の検証を待つ鳴った電話より先に、あとの鳴らなかった電話が回線へ渡りえた（回線の表は
  出来事の来た順で決める）。
- E6〜E9（テスト番号の答え・PC の部品が試さなかった・話している途中・ほかの回線が鳴らしている途中）：`tested`（core/ring_test.run の1か所）。
- E10（次に動く時刻が来た）・E18（声が通ると分かっている回線が空いた）：`tick`（回線ごとの処理の毎回の頭＝supervisor/line.one_round）。
  回線の E10、声のかけ直しを待つ行の E10・E18、印の見直しの時刻が来た番号の E10（N-7）。
- E11（その種類を直したと記録され、そのあと版が替わった＝salescall.trouble_fixed_at）：`fixed`（決まった間隔の仕事 fixed_stops。1回だけ）。
- E12（管理者か契約者が手で再開・戻す・解く）：`by_hand_task`（core/troubles.restore。休んだ行・復旧待ちの行）・`by_hand_line`（画面＝回線の
  休みを手で再開）・`by_hand_stop`（core/stops.resume。こちらの不具合で止めた契約者の停止の記録・前の作りの回線の停止の記録を解いたとき）。
- E13（回線を外した）：`removed`（core/registry.retire_line）。E14（テスト番号が入った）：`number_set`（core/settings.set_value。契約者の値）・
  `number_set_everywhere`（管理者がシステムの初めの値を入れた・変えた：自分の値を持たない契約者ごとに1回ずつ。17-3-6 の【当】(a)）。
- E15（判定待ちの行に、ほかの回線がかけた）：`claimed`（core/gate.claim が通したとき）。E17（回線が「通話中」のまま）：`stuck`（supervisor/line.loop）。
- E16（試行のせいが決まった）：表が出した「試行のせいが決まった」を、この部品の中で、その試行の行・番号・試行のせいの欄へ渡す（`_Run.follow`）。
- E19（お客様の「もうかけないで」・業務の終わり）は、表が何もしない（R-14）ので起こさない。

読む口（読み手は、欄を読むだけ。読むたびに導き直さない）：`line_of`・`task_of`・`number_of`・`blame_of`・`waiting_decision`・`number_dead`・
`number_held`・`number_wait`。データベースの中の読み手（候補・門の締め待ち・回線の空き・休んだ行の戻しの守り）は 0146 の関数が欄を読む。

**読み方**（17章に書き方の無かった所。管理者・CL-2 が見直す）
- (1) 電話の出来事の時刻は、その電話が終わった時刻（回線の続きの始まり＝最初の鳴らなかった電話の時刻・復旧待ちの「終えた電話」の終わり）。
  ほかの出来事の時刻は、取引の時刻（データベースの now()）。
- (2) 番号（Event.number）は、依頼者の連絡先（contact_id）。回線の声の続きの「違うお客様」も連絡先で数える（今の作りは番号の文字＝
  phone_e164 で数える。同じ番号を2つの依頼者が持つときだけ違う）。電話番号そのものを新しい欄に持たないため。
- (3) 表の決まりの値（health_rules.Rules）は、回線・契約者の欄は契約者の値、行・番号・試行のせいの欄は、その依頼者の値（業務の値は読まない）。
- (4) 直した版（E11）は、その契約者の欄が動き始めたあとに替わった版だけを渡す（表は E11 の時刻を見ずに待っている行を戻すので、前の版で、
  そのあとのこちらの不具合を戻さない）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, NamedTuple
from uuid import UUID

from salescall.core import errors, fix_requests, health_rules as hr, notify, settings
from salescall.core.access import Tx

__all__ = ["ORDER", "Tick", "after_call", "after_settle", "blame_of", "by_hand_line", "by_hand_stop", "by_hand_task", "claimed",
           "decode", "encode", "fixed", "line_of", "number_dead", "number_held", "number_of", "number_set", "number_set_everywhere",
           "number_wait", "removed", "stage_name", "stuck", "task_of", "tested", "tick", "waiting_decision"]

LINE, TENANT, TASK, NUMBER, ATTEMPT = "line", "tenant", "task", "number", "attempt"
ORDER = (LINE, TENANT, TASK, NUMBER, ATTEMPT)   # 鍵を取る順（17章：回線→契約者。行・番号・試行のせいはそのあと）
INITIAL: dict[str, Any] = {   # 欄が無いときの初めの状態（17-1 の、待ちでない段）
    LINE: hr.Line(hr.LineStage.HEALTHY), TENANT: hr.Tenant(hr.TenantStage.NORMAL), TASK: hr.Task(hr.TaskStage.ON_PLAN),
    NUMBER: hr.Number(hr.NumberStage.NONE), ATTEMPT: hr.Attempt(hr.Blame.EMPTY)}
TEST_RESULTS = {"rang": "E6", "silent": "E7", "unknown": "E8", "withheld": "E9"}   # テスト番号の答え → 出来事（`tested`）
BY = {"admin": "管理者", "operator": "契約者"}   # 手で再開・戻す・解く・外す（E12・E13）のは、管理者か契約者だけ
STOP_CAUSE = "program_trouble"   # 契約者の表が「新しい電話を止める」ときの停止の原因（core/stops の CAUSES の名前）


# ─────────────────────────────────────────────────────────────────────────────
# 状態の値と、データベースの欄の中身（JSON）のあいだ（表の値の形のまま。1か所）
# ─────────────────────────────────────────────────────────────────────────────

_ENUMS = {c.__name__: c for c in (hr.LineStage, hr.Wait, hr.TaskStage, hr.NumberStage, hr.TenantStage, hr.Blame)}
_CLASSES = {c.__name__: c for c in (hr.Line, hr.Task, hr.Number, hr.Tenant, hr.Attempt)}


def encode(v: Any) -> Any:
    """表の値（状態・することの中身）を、データベースの欄に書く JSON の形にする。"""
    if is_dataclass(v) and not isinstance(v, type):
        return {"$c": type(v).__name__, **{f.name: encode(getattr(v, f.name)) for f in fields(v)}}
    if isinstance(v, Enum):
        return {"$e": type(v).__name__, "v": v.name}
    if isinstance(v, datetime):
        return {"$t": v.isoformat()}
    if isinstance(v, date):
        return {"$d": v.isoformat()}
    if isinstance(v, frozenset):
        return {"$s": sorted((encode(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))}
    if isinstance(v, tuple):
        return {"$p": [encode(x) for x in v]}
    if isinstance(v, UUID):
        return str(v)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    raise TypeError(f"状態の欄に書けない値の形：{type(v).__name__}")


def decode(v: Any) -> Any:
    """`encode` の逆。"""
    if isinstance(v, list):
        return [decode(x) for x in v]
    if not isinstance(v, dict):
        return v
    if "$c" in v:
        return _CLASSES[v["$c"]](**{k: decode(x) for k, x in v.items() if k != "$c"})
    if "$e" in v:
        return _ENUMS[v["$e"]][v["v"]]
    if "$t" in v:
        return datetime.fromisoformat(v["$t"])
    if "$d" in v:
        return date.fromisoformat(v["$d"])
    if "$s" in v:
        return frozenset(decode(x) for x in v["$s"])
    if "$p" in v:
        return tuple(decode(x) for x in v["$p"])
    return {k: decode(x) for k, x in v.items()}


def stage_name(state: Any) -> str:
    """欄の段の、データベースの名前（段の名前を小文字にしたもの。表の作り 0145 の CHECK と同じ）。"""
    return state.stage.name.lower()


def _did(d: hr.Do) -> dict[str, Any]:
    return {"what": d.what, **{k: encode(v) for k, v in d.detail}}


# ─────────────────────────────────────────────────────────────────────────────
# 1つの出来事を、鍵を取った欄へ渡す（表を引くのは `give` の1か所・することを行うのは `_execute` の1か所）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Stages:
    """行の欄のうち、その段の行を全部（取引の契約者の中。依頼者をまたぐ）。"""
    names: tuple[str, ...]


@dataclass(frozen=True)
class Due:
    """番号の欄のうち、印の見直しの時刻（中身の next_at）が来たものを全部（取引の契約者の中。N-7）。"""


@dataclass
class _Box:
    subject: str
    key: str
    client: str | None
    state: Any
    created: bool
    facts: dict[str, Any] = field(default_factory=dict)   # 試行：その行・連絡先・回線・終わった時刻
    changed: bool = False


class _Given(NamedTuple):
    box: _Box
    event: hr.Event
    out: hr.Outcome | hr.Mistake


class _Run:
    """1つの取引の中の、出来事の渡し（鍵を取る・表を引く・書く・することを行う）。reports＝こちらの不具合で終えた電話の、通話ごとの口が
    作ったエラーの中身（試行 → core/errors.ErrorReport。電話ごとの記録に使う）。"""

    def __init__(self, tx: Tx, origin: str, reports: dict[str, Any] | None = None) -> None:
        row = tx.one("SELECT salescall.cur_tenant() AS t, now() AS now")
        self.tx, self.origin, self.tenant, self.now = tx, origin, row["t"], row["now"]
        self.boxes: dict[tuple[str, str], _Box] = {}
        self.moves: list[dict[str, Any]] = []
        self.given: list[_Given] = []
        self.decided: list[tuple[str, str, datetime]] = []   # 表が「試行のせいが決まった」と出した（試行, せい, 時刻）
        self.reports = reports or {}
        self._rules: dict[str | None, hr.Rules] = {}

    def take(self, *wants: tuple[str, Any]) -> list[Any]:
        """鍵を取って読む（無ければ初めの状態で作る）。順は `ORDER`（同じ取引で前に取った欄は、取り直さない）。返すのは、渡した順の欄
        （Stages・Due は、当たる欄の並び＝1つの要素がリスト）。"""
        ask: list[dict[str, Any]] = []
        for subject, key in sorted(wants, key=lambda w: ORDER.index(w[0])):
            if isinstance(key, Stages):
                ask.append({"s": subject, "stages": list(key.names)})
            elif isinstance(key, Due):
                ask.append({"s": subject, "due": True})
            elif (subject, str(key)) not in self.boxes:
                init = INITIAL[subject]
                ask.append({"s": subject, "k": str(key), "init": {"stage": stage_name(init), "state": encode(init)}})
        found: dict[int, list[tuple[str, str]]] = {}
        if ask:
            for r in self.tx.one("SELECT salescall.health_take(%s) AS r", (json.dumps(ask),))["r"]:
                if (r["s"], r["k"]) not in self.boxes:   # 同じ取引で前に取った欄は、手元の状態のまま
                    state = hr.Attempt(hr.Blame[r["stage"].upper()]) if r["s"] == ATTEMPT else decode(r["state"])
                    self.boxes[(r["s"], r["k"])] = _Box(r["s"], r["k"], r["client"], state, r["created"],
                                                        {x: r[x] for x in ("task", "contact", "line", "ended_at") if x in r})
                found.setdefault(id(r), []).append((r["s"], r["k"]))
        out: list[Any] = []
        for subject, key in wants:
            if isinstance(key, Stages):
                names = {n.upper() for n in key.names}
                out.append(sorted((b for b in self.boxes.values() if b.subject == TASK and b.state.stage.name in names),
                                  key=lambda b: b.key))
            elif isinstance(key, Due):
                out.append(sorted((b for b in self.boxes.values() if b.subject == NUMBER and hr.next_at(b.state) is not None
                                   and hr.next_at(b.state) <= self.now), key=lambda b: b.key))
            else:
                out.append(self.boxes[(subject, str(key))])
        return out

    def rules(self, client: str | None) -> hr.Rules:
        """表の決まりの値（読み方 (3)：回線・契約者は契約者の値・行・番号・試行のせいは、その依頼者の値）。"""
        if client not in self._rules:   # 取引の依頼者と違う依頼者の欄も引くので、その依頼者の範囲で読む（core/settings の1か所）
            self._rules[client] = hr.Rules.from_settings(settings.resolve_for_table(self.tx, hr.SETTING_KEYS.values(), client))
        return self._rules[client]

    def give(self, box: _Box, ev: hr.Event, *, once: str | None = None) -> hr.Outcome | hr.Mistake:
        """表を引く（1か所）：今の状態と出来事で表の1行を引き、次の状態を欄に置き、移った記録を足す。引けなければ（誤りの値）、状態は
        そのままで、誤りとして記録し、直す担当への修正依頼を出す（することは `save` のあと `_execute` が行う）。"""
        before = box.state
        out = hr.apply(before, ev, self.rules(box.client))
        move: dict[str, Any] = {"s": box.subject, "k": box.key, "event": ev.code, "at": ev.at.isoformat(), "from": stage_name(before),
                                "origin": self.origin, "once": once, "actions": [_did(d) for d in out.actions]}
        if isinstance(out, hr.Mistake):
            move.update(to=stage_name(before), rows=[], mistake={"kind": out.kind, "why": out.why, "gap": out.gap,
                                                                    "subject": out.subject, "stage": out.stage, "event": out.event})
        else:
            box.state, box.changed = out.state, box.changed or out.state != before
            move.update(to=stage_name(out.state), rows=list(out.rows), mistake=None)
            self.decided += [(d.get("attempt"), d.get("blame"), ev.at) for d in out.actions if d.what == hr.DECIDED]
        self.moves.append(move)
        self.given.append(_Given(box, ev, out))
        return out

    def follow(self) -> None:
        """表が「試行のせいが決まった」と出した試行を、その試行の行・番号・試行のせいの欄へ E16 として渡す（17-2 の E16）。"""
        while self.decided:
            attempt, blame, at = self.decided.pop(0)
            (a,) = self.take((ATTEMPT, attempt))
            task, number = self.take((TASK, a.facts["task"]), (NUMBER, a.facts["contact"]))
            ended = a.facts.get("ended_at")
            ev = hr.Event("E16", at, attempt=attempt, blame=blame,
                          call_at=datetime.fromisoformat(ended) if isinstance(ended, str) else ended)
            for box in (task, number, a):
                self.give(box, ev)

    def save(self) -> None:
        """次の状態と移った記録を書き、することを行う（同じ取引）。"""
        self.follow()
        rows = [{"s": b.subject, "k": b.key, "stage": stage_name(b.state), "state": None if b.subject == ATTEMPT else encode(b.state)}
                for b in self.boxes.values() if b.changed]
        self.tx.execute("SELECT salescall.health_save(%s, %s)", (json.dumps(rows), json.dumps(self.moves, ensure_ascii=False)))
        _execute(self)


# ─────────────────────────────────────────────────────────────────────────────
# することを行う（1か所）
# ─────────────────────────────────────────────────────────────────────────────

def _execute(run: _Run) -> None:
    """表が返したすることを、出来事の順に行う（同じ取引。行う所は、このモジュールの説明の一覧のとおり）。"""
    for g in run.given:
        made: dict[str, Any] = {}   # この答えで作ったもの（止めた停止の記録の番号など。知らせの refs に入れる）
        for d in g.out.actions:
            if d.what == hr.STOP_CALLS:
                made["stop_event_id"] = _stop_calls(run, g)
            elif d.what == hr.RESUME_CALLS:
                made["stop_event_id"] = _resume_calls(run, g)
        for d in g.out.actions:
            if d.what == hr.NOTIFY and d.get("call") is None:
                _notify(run, g, d, made)
            elif d.what == hr.FIX and d.get("call") is None:
                _fix(run, g, d)
            elif d.what == hr.RECORD and d.get("call") is not None:
                _per_call(run, d)


def _refs(run: _Run, g: _Given, d: hr.Do, made: dict[str, Any]) -> dict[str, Any]:
    out = {"subject": g.box.subject, "event": g.event.code, "origin": run.origin, **made}
    out[{LINE: "line_id", TASK: "task_id", NUMBER: "contact_id", TENANT: "tenant_id", ATTEMPT: "attempt_id"}[g.box.subject]] = g.box.key
    if d.get("cause") is not None:
        out["cause"] = d.get("cause")
    if isinstance(g.out, hr.Outcome):
        out["rows"] = list(g.out.rows)
    return out


def _notify(run: _Run, g: _Given, d: hr.Do, made: dict[str, Any]) -> None:
    to = d.get("to") or ()
    notify.emit(run.tx, d.get("note"), key=f"state:{g.box.subject}:{g.box.key}:{g.event.code}:{g.event.at.isoformat()}:{d.get('note')}",
                summary=d.get("about"), refs=_refs(run, g, d, made), admin="管理者" in to, contractor="契約者" in to)


def _fix(run: _Run, g: _Given, d: hr.Do) -> None:
    fix_requests.request(run.tx, d.get("fixer"), d.get("fix_kind"), key=d.get("fix_key").format(tenant=run.tenant),
                         summary=d.get("about"), refs=_refs(run, g, d, {}))


def _per_call(run: _Run, d: hr.Do) -> None:
    """こちらの不具合で終えた電話ごとの、記録・知らせ・修正依頼（R-5・R-8 の3つ）：今の作りの1か所 core/watch.record_error（エラーの記録・
    電話ごとの知らせ＝その行が復旧を待っていることを文に入れる・同じ不具合で1つの修正依頼）。止めるかは決めない（契約者の表の C-1）。"""
    from salescall.core import troubles, watch   # 不具合の部品・見守りの部品がこの部品を読むので、ここで読む（読み合いにしない）
    attempt = d.get("call")
    a = run.tx.one("SELECT line_id, trouble FROM salescall.attempts WHERE attempt_id = %s", (attempt,))
    report = run.reports.get(attempt) or troubles.report_of(a["trouble"], "通話ごとの口を通らずに終わった電話")
    watch.record_error(run.tx, report, line_id=a["line_id"], attempt_id=attempt)


def _stop_calls(run: _Run, g: _Given) -> int | None:
    """契約者の新しい電話を止める（話している電話は切らない）：契約者の範囲の停止の記録（core/stops.stop。まだ発信していない予約も取り消す）。
    同じ原因の停止がもうあれば、重ねない。"""
    from salescall.core import stops   # 停止の部品がこの部品を読むので、ここで読む（読み合いにしない）
    if stops.open_same(run.tx, scope="tenant", cause=STOP_CAUSE, tenant_id=run.tenant) is not None:
        return None
    kinds = "・".join(sorted(g.box.state.stopped)) if isinstance(g.box.state, hr.Tenant) else ""
    return stops.stop(run.tx, scope="tenant", cause=STOP_CAUSE, reason=f"こちらのプログラムの同じ不具合が違う行で続いた（{kinds}。17章の C-1）",
                      evidence=f"状態の欄：{g.event.code}", tenant_id=run.tenant, admin=True, notify=False)["event_id"]


def _resume_calls(run: _Run, g: _Given) -> int | None:
    """止めていた新しい電話を戻す：契約者の範囲の停止の記録を解く（core/stops.resume）。解く停止が無ければ、何もしない。"""
    from salescall.core import stops
    stop = stops.open_same(run.tx, scope="tenant", cause=STOP_CAUSE, tenant_id=run.tenant)
    if stop is None:
        return None
    return stops.resume(run.tx, stop, reason=f"状態の欄：{g.event.code}（{'・'.join(g.out.rows)}）", evidence="17章の表", notify=False)


# ─────────────────────────────────────────────────────────────────────────────
# 入口（出来事を起こす所ごと）
# ─────────────────────────────────────────────────────────────────────────────

_CALL = (
    "SELECT a.attempt_id, a.line_id, a.task_id, a.contact_id, a.claimed_at, a.ended_at, a.trouble,"
    "       salescall.rang(a.state, a.cut_off, a.attempt_id) AS rang, salescall.trouble_ours(a.trouble) AS ours,"
    "       salescall.trouble_counts_line(a.trouble) AS by_audio, salescall.trouble_kind(a.trouble) AS kind,"
    "       salescall.reached_answer(a.attempt_id) AS answered, (a.state = 'ended' AND a.trouble IS NULL) AS talked,"
    "       salescall.resume_allowed(a.attempt_id) AS resume_allowed"
    " FROM salescall.attempts a"
    " WHERE a.attempt_id = %s AND salescall.is_dialed(a.state) AND NOT salescall.is_active(a.state) AND a.ended_at IS NOT NULL")


def _call_event(a: dict[str, Any]) -> str:
    """終わった電話の出来事（1か所。17-2）：こちらの不具合で終えた＝E3／お客様のところで鳴らなかった＝E2／声が聞き取れずに終えた＝E4／
    こちらの不具合なしにお客様と話し終えた（ended まで進んだ＝salescall.last_good_talk と同じ）＝E5／ほかの鳴った電話＝E1。
    どれも今の作りの1か所（trouble_ours・rang・trouble_counts_line）の答えを読む。"""
    if a["ours"]:
        return "E3"
    if not a["rang"]:
        return "E2"
    if a["by_audio"]:
        return "E4"
    if a["talked"]:
        return "E5"
    return "E1"


def after_call(tx: Tx, attempt_id: UUID, *, report: Any = None) -> None:
    """電話が終わった（E1〜E5）。1つの試行で1回だけ（試行のせいの欄の行があれば、もう渡した）。E4 の行への分は `after_settle`。
    report＝こちらの不具合で終えた電話の、通話ごとの口が作ったエラーの中身（電話ごとの記録に使う。core/troubles.after_trouble だけが渡す）。"""
    if tx.one("SELECT 1 FROM salescall.attempt_blames WHERE attempt_id = %s", (attempt_id,)) is not None:
        return
    a = tx.one(_CALL, (attempt_id,))
    if a is None:   # 発信まで進んでいない・まだ終わっていない
        return
    code = _call_event(a)
    run = _Run(tx, "after_call", {str(attempt_id): report} if report is not None else None)
    at, me, number, line_id = a["ended_at"], str(attempt_id), str(a["contact_id"]), str(a["line_id"])
    wants: list[tuple[str, Any]] = [(LINE, line_id), (TASK, a["task_id"]), (NUMBER, number), (ATTEMPT, me)]
    if code in ("E3", "E5"):   # 契約者の欄へも渡す出来事（C-1・C-3）
        wants.append((TENANT, run.tenant))
    if code == "E5":           # 復旧を待つ行へも渡す（R-6。同じ契約者のどの行でも）
        wants.append((TASK, Stages(("await_recovery",))))
    boxes = run.take(*wants)
    line, task, num, att = boxes[:4]
    ten, recovering = (boxes[4] if len(boxes) > 4 else None), (boxes[5] if len(boxes) > 5 else [])
    if not att.created:   # ほかの取引が、もう渡した
        return
    if code == "E2":
        ev = hr.Event("E2", at, attempt=me, number=number, line=line_id)
        sends = [(line, ev), (task, ev), (num, ev), (att, ev)]
    elif code == "E3":
        ev = hr.Event("E3", at, attempt=me, kind=a["kind"], task=str(a["task_id"]), rang=a["rang"], answered=a["answered"])
        sends = [(line, ev), (task, ev), (ten, ev), (att, ev)]
    elif code == "E4":   # 行への分は締めのあと（`after_settle`）
        ev = hr.Event("E4", at, attempt=me, number=number, line=line_id)
        sends = [(line, ev), (num, hr.Event("E1", at, attempt=me, number=number))]
    elif code == "E5":
        ev = hr.Event("E5", at, attempt=me, number=number, started_at=a["claimed_at"])
        others = [t for t in recovering if t.key != task.key]
        sends = [(line, ev), (task, ev), (ten, ev), (num, hr.Event("E1", at, attempt=me, number=number))]
        sends += [(t, hr.Event("E5", at, attempt=me, number=number, started_at=a["claimed_at"], own=False)) for t in others]
    else:
        ev = hr.Event("E1", at, attempt=me, number=number)
        sends = [(line, ev), (task, ev), (num, ev)]
    for box, e in sends:
        run.give(box, e)
    run.save()


def after_settle(tx: Tx, attempt_id: UUID) -> None:
    """締めのあと：声が聞き取れずに終えた電話（E4）を、その行へ渡す（R-11。すぐのかけ直しが続けて2回のうちか＝salescall.resume_allowed が、
    締めが残した「途中で切れたか」を読む）。1つの試行で1回だけ（行の移った記録の once）。ほかの出来事・まだ締めていない電話は何もしない。"""
    if tx.one("SELECT 1 FROM salescall.attempt_blames WHERE attempt_id = %s", (attempt_id,)) is None:
        return   # まだ電話の出来事を渡していない（渡すのは `after_call`）
    a = tx.one(_CALL + " AND salescall.is_settled(a.attempt_id)", (attempt_id,))
    if a is None or _call_event(a) != "E4":
        return
    once = f"E4:{attempt_id}"
    if tx.one("SELECT 1 FROM salescall.state_moves WHERE subject = 'task' AND key = %s AND once = %s", (a["task_id"], once)) is not None:
        return
    run = _Run(tx, "after_settle")
    (task,) = run.take((TASK, a["task_id"]))
    run.give(task, hr.Event("E4", a["ended_at"], attempt=str(attempt_id), number=str(a["contact_id"]), line=str(a["line_id"]),
                            resume_allowed=bool(a["resume_allowed"])), once=once)
    run.save()


def tested(tx: Tx, line_id: UUID, result: str) -> None:
    """テスト番号の答え（E6 鳴った・E7 鳴らなかった・E8 見分けられない・E9 PC の部品が試さなかった／話している途中／ほかの回線が鳴らして
    いる途中）。"""
    run = _Run(tx, "tested")
    (line,) = run.take((LINE, line_id))
    run.give(line, hr.Event(TEST_RESULTS[result], run.now))
    run.save()


def _test_number(tx: Tx) -> bool:
    """いまテスト番号があるか（決まりは core/ring_test.usable の1か所）。"""
    from salescall.core import ring_test   # テスト番号の部品がこの部品を呼ぶので、ここで読む（読み合いにしない）
    return ring_test.usable(tx)


def _other_test_until(tx: Tx, line_id: Any, now: datetime) -> datetime | None:
    """ほかの回線がテスト番号を鳴らしている途中なら、その答えの期限（1つの契約者のテスト番号は1本ずつ＝2026-10-25 管理者）。"""
    until = None
    for r in tx.all("SELECT state FROM salescall.line_states WHERE line_id <> %s AND stage IN ('check_c', 'line_rest', 'audio_rest')",
                    (line_id,)):
        s = decode(r["state"])
        if (s.stage is hr.LineStage.CHECK_C or s.wait is hr.Wait.ANSWER) and s.next_at is not None and s.next_at > now:
            until = s.next_at if until is None else max(until, s.next_at)
    return until


@dataclass(frozen=True)
class Tick:
    """回線ごとの処理の毎回の頭の答え：回線の欄（line）と、テスト番号を鳴らすか（ring：回線の欄が「確かめ中 (c)」か「休みで答え待ち」＝
    表が鳴らす頼みを置いた）。"""
    line: hr.Line
    ring: bool

    @property
    def calls(self) -> bool:
        """この回線で、お客様へかけてよいか（元気・確かめ中。休み・使用停止はかけない）。"""
        return self.line.stage not in hr.RESTS and self.line.stage is not hr.LineStage.OUT_OF_USE

    @property
    def until(self) -> datetime | None:
        """回線の欄の、次に動く時刻（無ければ None）。"""
        return hr.next_at(self.line)


def tick(tx: Tx, line_id: UUID) -> Tick:
    """回線ごとの処理の毎回の頭（1か所）：その回線の「次に動く時刻」が来ていれば E10。その回線が元気で声の続きが0（声が通ると分かって
    いる回線＝17-3-1 の下の注）なら、声のかけ直しを待つ行へ E18。声のかけ直しを待つ行で、声が聞き取れなかった回線がこの回線で、
    待つ時刻が来ていれば E10（R-13）。印の見直しの時刻が来た番号へ E10（N-7）。答えは回線の欄と、テスト番号を鳴らすか。"""
    run = _Run(tx, "tick")
    line, voice, numbers = run.take((LINE, line_id), (TASK, Stages(("voice_redial",))), (NUMBER, Due()))
    due = hr.next_at(line.state)
    if due is not None and run.now >= due:
        run.give(line, hr.Event("E10", run.now, test_number=_test_number(tx), other_test_until=_other_test_until(tx, line_id, run.now)))
    open_ = line.state.stage is hr.LineStage.HEALTHY and line.state.m == 0
    for t in voice:
        if open_:
            run.give(t, hr.Event("E18", run.now, line=str(line_id)))
        elif t.state.line == str(line_id) and run.now >= hr.next_at(t.state):
            run.give(t, hr.Event("E10", run.now))
    for n in numbers:
        run.give(n, hr.Event("E10", run.now))
    run.save()
    s = line.state
    return Tick(s, s.stage is hr.LineStage.CHECK_C or (s.stage in hr.RESTS and s.wait is hr.Wait.ANSWER))


def stuck(tx: Tx, line_id: UUID, lasted: float) -> None:
    """回線が「通話中」と答え続けて、かけられない（E17。lasted＝かけられなくなってからの秒。かけられなくなった時刻は、取引の時刻から
    その秒だけ前＝データベースの時計にそろえる）。"""
    run = _Run(tx, "stuck")
    (line,) = run.take((LINE, line_id))
    run.give(line, hr.Event("E17", run.now, since=run.now - timedelta(seconds=lasted)))
    run.save()


def fixed(tx: Tx) -> None:
    """種類 k の修正依頼が「直した」と記録され、そのあと動いているプログラムの版が替わった（E11。時刻は今の作りの1か所
    salescall.trouble_fixed_at）。同じ「直した版」は1回だけ（契約者の移った記録の once）。その契約者の欄が動き始める前に替わった版は
    渡さない（表は E11 の時刻を見ずに、待っている行を戻す。動き始める前の版で、そのあとのこちらの不具合を戻さない）。"""
    run = _Run(tx, "fixed")
    kinds = sorted(set(errors.TROUBLES.values()))
    for r in tx.all("SELECT k, t FROM (SELECT k, salescall.trouble_fixed_at(k) AS t FROM unnest(%s::text[]) k) x WHERE isfinite(t)"
                    " AND t > (SELECT min(m.event_at) FROM salescall.state_moves m WHERE m.tenant_id = salescall.cur_tenant())"
                    " ORDER BY t, k", (kinds,)):
        once = f"E11:{r['k']}:{r['t'].isoformat()}"
        if tx.one("SELECT 1 FROM salescall.state_moves WHERE subject = 'tenant' AND key = %s AND once = %s",
                  (run.tenant, once)) is not None:
            continue
        ten, waiting = run.take((TENANT, run.tenant), (TASK, Stages(("await_recovery", "resting"))))
        ev = hr.Event("E11", run.now, kind=r["k"])
        run.give(ten, ev, once=once)
        for t in waiting:
            run.give(t, ev)
    run.save()


def _by(tx: Tx) -> tuple[str | None, str]:
    """手で動かしたのは管理者か契約者か（`BY`。システムの処理なら None）と、その名前。"""
    row = tx.one("SELECT salescall.cur_actor_kind() AS k, salescall.cur_actor() AS a")
    return BY.get(row["k"]), row["a"]


def by_hand_task(tx: Tx, task_id: UUID, reason: str) -> None:
    """管理者か契約者が、休んだ行・復旧待ちの行を戻した（E12。core/troubles.restore）。"""
    who, name = _by(tx)
    if who is None:
        return
    run = _Run(tx, "by_hand_task")
    (task,) = run.take((TASK, task_id))
    run.give(task, hr.Event("E12", run.now, by=f"{who}（{name}）", reason=reason))
    run.save()


def by_hand_line(tx: Tx, line_id: UUID, reason: str) -> None:
    """管理者か契約者が、回線の休み（回線のせい・声の不調）を、直ったことを確かめて手で再開した（E12。画面の入口。2026-10-29 管理者：
    「管理者か契約者が、直ったことを確かめて手で再開も可能」）。理由は必ず書く。休みでない回線は、記録だけ（L-32）。"""
    if not reason.strip():
        raise ValueError("理由が要ります")
    who, name = _by(tx)
    if who is None:
        raise PermissionError("回線を手で再開できるのは、管理者か契約者だけです")
    run = _Run(tx, "by_hand_line")
    (line,) = run.take((LINE, line_id))
    run.give(line, hr.Event("E12", run.now, by=f"{who}（{name}）", reason=reason))
    run.save()


def by_hand_stop(tx: Tx, stop_event_id: int, reason: str) -> None:
    """管理者か契約者が、止めた停止を解いた（core/stops.resume）。この章の状態にかかるのは、こちらのプログラムの不具合で止めた契約者
    （program_trouble）と、前の作りで回線の異常で止めた回線（line_failure）だけ（ほかの停止は 17章の外）：その契約者・回線の欄へ E12。"""
    who, name = _by(tx)
    if who is None:
        return
    s = tx.one("SELECT scope, cause::text AS cause, line_id, tenant_id FROM salescall.stop_events WHERE event_id = %s", (stop_event_id,))
    if s is None:
        return
    run = _Run(tx, "by_hand_stop")
    ev = hr.Event("E12", run.now, by=f"{who}（{name}）", reason=reason)
    if s["scope"] == "line" and s["cause"] == "line_failure" and s["line_id"] is not None:
        (box,) = run.take((LINE, s["line_id"]))
    elif s["scope"] == "tenant" and s["cause"] == STOP_CAUSE and s["tenant_id"] == run.tenant:
        (box,) = run.take((TENANT, run.tenant))
    else:
        return
    run.give(box, ev)
    run.save()


def removed(tx: Tx, line_id: UUID, reason: str) -> None:
    """管理者か契約者が、回線を外した（E13。core/registry.retire_line）。"""
    who, name = _by(tx)
    run = _Run(tx, "removed")
    (line,) = run.take((LINE, line_id))
    run.give(line, hr.Event("E13", run.now, by=f"{who or 'システム'}（{name}）", reason=reason))
    run.save()


def number_set(tx: Tx) -> None:
    """テスト番号が入った（E14。契約者が自分の番号を入れた）：取引の契約者の、欄のある回線へ。"""
    run = _Run(tx, "number_set")
    lines = [r["line_id"] for r in tx.all("SELECT line_id FROM salescall.line_states ORDER BY line_id")]
    if not lines:
        return
    boxes = run.take(*[(LINE, l) for l in lines])
    for box in boxes:
        run.give(box, hr.Event("E14", run.now, test_number=True, other_test_until=_other_test_until(tx, box.key, run.now)))
    run.save()


def number_set_everywhere(tx: Tx) -> None:
    """管理者がシステムの初めの値のテスト番号を入れた・変えた（全体の取引）：自分の値を持たない契約者ごとに1回ずつ、テスト番号が入った
    （E14）を渡す（17-2 の E14・17-3-6 の【当】(a)。⛔ 3段目：契約者の値を入れたときだけ渡したので、システムの初めの値が入っても、テスト
    番号を待つ回線が待ち続けた）。契約者の取引への切り替えは読み書きの層の1か所（Tx.as_tenant）。"""
    for r in tx.all("SELECT t AS tenant_id FROM salescall.tenants_without_own_value('line.test_number') t"):
        with tx.as_tenant(r["tenant_id"]):
            number_set(tx)


def claimed(tx: Tx, task_id: UUID, line_id: UUID) -> None:
    """門が、その行に、その回線の予約を通した：その行が、ほかの回線の判定待ちなら E15（0144。同じ回線の判定待ちの行は、門が通さない）。"""
    row = tx.one("SELECT stage, state FROM salescall.task_states WHERE task_id = %s", (task_id,))
    if row is None or row["stage"] != "await_decision" or decode(row["state"]).line == str(line_id):
        return
    run = _Run(tx, "claimed")
    (task,) = run.take((TASK, task_id))
    run.give(task, hr.Event("E15", run.now, line=str(line_id)))
    run.save()


# ─────────────────────────────────────────────────────────────────────────────
# 読む口（読み手は欄を読むだけ。欄が無ければ、初めの状態＝待ちでない段）
# ─────────────────────────────────────────────────────────────────────────────

def _read(tx: Tx, subject: str, key: Any) -> Any:
    table, col = {LINE: ("line_states", "line_id"), TASK: ("task_states", "task_id"), NUMBER: ("number_states", "contact_id")}[subject]
    row = tx.one(f"SELECT state FROM salescall.{table} WHERE {col} = %s", (key,))
    return INITIAL[subject] if row is None else decode(row["state"])


def line_of(tx: Tx, line_id: UUID) -> hr.Line:
    """回線の欄（取引の契約者の回線）。"""
    return _read(tx, LINE, line_id)


def task_of(tx: Tx, task_id: UUID) -> hr.Task:
    """行の欄（取引の依頼者の行）。"""
    return _read(tx, TASK, task_id)


def number_of(tx: Tx, contact_id: UUID) -> hr.Number:
    """番号の欄（取引の依頼者の連絡先）。"""
    return _read(tx, NUMBER, contact_id)


def blame_of(tx: Tx, attempt_id: UUID) -> str | None:
    """試行のせいの欄（17-1-5）を、次の動きが読む名前で：number＝番号のせい・line＝回線のせい・ours＝こちらのせい・pending＝まだ分からない。
    鳴った電話（こちらの不具合で、お客様のところで鳴ったあとに終えた電話も＝次の動きは「途中で切れた」の決まり）・まだ渡していない電話は
    None。"""
    row = tx.one("SELECT b.blame, salescall.rang(a.state, a.cut_off, a.attempt_id) AS rang FROM salescall.attempt_blames b"
                 " JOIN salescall.attempts a ON a.attempt_id = b.attempt_id WHERE b.attempt_id = %s", (attempt_id,))
    if row is None or (row["blame"] == "ours" and row["rang"]):
        return None
    return {"unknown": "pending", "number": "number", "line": "line", "ours": "ours"}.get(row["blame"])


def waiting_decision(tx: Tx, attempt_id: UUID) -> bool:
    """その試行の行が、その試行の判定待ち（17-1-2。番号のせいか回線のせいかが決まるまで、締めの処理は締めない）か。"""
    row = tx.one("SELECT s.state FROM salescall.task_states s JOIN salescall.attempts a ON a.task_id = s.task_id"
                 " WHERE a.attempt_id = %s AND s.stage = 'await_decision'", (attempt_id,))
    return row is not None and decode(row["state"]).attempt == str(attempt_id)


def number_dead(tx: Tx, contact_id: UUID) -> bool:
    """その番号は使えない（番号のせいの鳴らなかった電話が、並びの数に届いた。N-1）か。門と次の動きが読む。"""
    return number_of(tx, contact_id).stage is hr.NumberStage.DEAD


def number_held(tx: Tx, contact_id: UUID) -> bool:
    """その番号に、待つ印（N-4：分からない電話を番号のせいに数えると、今日の分を使い切る）があるか。門と候補が読む。"""
    return number_of(tx, contact_id).hold


class NumberWait(NamedTuple):
    """番号のせいの鳴らなかった電話が続く番号へ、次にかけてよい日（day）と、その時刻の下限（not_before）。last_end＝数えたいちばん新しい
    鳴らなかった電話の終わり（その番号の続きが、いつ伸びたか）。"""
    day: date
    not_before: datetime
    last_end: datetime


def number_wait(tx: Tx, contact_id: UUID) -> NumberWait | None:
    """番号の欄が「続きあり」なら、決まった順の次の日と時刻の下限（N-1。番号ごとの1か所＝門と次の動きが読む）。続いていない・使えない
    ときは None。"""
    n = number_of(tx, contact_id)
    if n.stage is not hr.NumberStage.RUN:
        return None
    return NumberWait(n.next_day, n.not_before, n.last_end)
