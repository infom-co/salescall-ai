"""回線・行・番号・契約者の状態の欄と、試行のせいの欄を、表（core/health_rules）を引いて移す入口（DESIGN.md 17章。持ち主 CL-2）。

2026-10-29 管理者の承認：「回線・行・契約者の状態を、それぞれ1つの欄に持ちます。OK」。

**3段目＝影**（いまの形）：出来事を起こす所（17-2 の「起こす所」）が、今の作りの処理の横で、この部品の入口を呼ぶ。入口は、データベースの
欄（表の作り 0145）から今の状態を鍵を取って読み（回線→契約者の順。行・番号・試行のせいはそのあと＝`ORDER`）、表を引き
（health_rules.apply。呼ぶのはこの部品の `_Run.give` の1か所）、次の状態と移った記録（どの出来事で・どの段からどの段へ・引いた表の行番号・
することの並び）を書く。
- **することの並びは記録するだけで行わない**（知らせ・修正依頼・予定・テスト番号を鳴らす・止める は、今の作りのまま出る）。表を引けなかった
  とき（誤りの値）も、記録するだけ（直す担当への修正依頼も記録だけ）。今の作りの答え（unrung_blame・task_awaiting_decision・task_resting・
  awaiting_recovery・consecutive_errors・回線の停止 など）は何も変えない（読み手を表に替えるのは4段目）。
- **影の書き込みが失敗しても、今の作りは続く**：入口は取引の中の区切り（Tx.savepoint）で動き、失敗したら、その区切りだけ取り消して、
  失敗を移った記録に残す（`_failed`）。ほかの取引とのぶつかりと、データベースに届かない誤りだけは受け止めない（取引ごとやり直すのは
  読み書きの層の1か所＝access）。取引の外から呼ぶ所（回線ごとの処理・テスト番号の部品）は `in_tenant` を通す（届かなくても止めない）。

出来事と入口（17-2。入口を呼んでよい所は checks/rules.py の PY_ENTRY_CALLERS）：
- E1〜E5（電話が終わった）：`after_call`。通話ごとの口（こちらの不具合で終えた電話＝core/troubles.after_trouble）と、締めの仕事
  （supervisor/jobs.settle_ready。締めのあと＝途中で切れたかが決まったあと。E4 の「すぐのかけ直しが続けて2回のうちか」は今の作りの1か所
  salescall.resume_allowed の答えを受ける＝health_rules の読み方 (5)。それが途中で切れたかの記録を読むため）。どの出来事かは `_call_event`
  の1か所。1つの試行の出来事は1回だけ（試行のせいの欄の行があれば、もう渡した）。
- E6〜E9（テスト番号の答え・PC の部品が試さなかった・話している途中）：`tested`（core/ring_test.run の1か所）。
- E10（次に動く時刻が来た）・E18（声が通ると分かっている回線が空いた）：`tick`（回線ごとの処理の毎回の頭＝supervisor/line.one_round）。
- E11（その種類を直したと記録され、そのあと版が替わった＝salescall.trouble_fixed_at）：`fixed`（決まった間隔の仕事 fixed_stops。1回だけ）。
- E12（管理者か契約者が手で再開・戻す・解く）：`by_hand_task`（core/troubles.restore）・`by_hand_stop`（core/stops.resume。回線の異常で
  止めた回線・こちらの不具合で止めた契約者を、管理者か契約者が解いたとき）。
- E13（回線を外した）：`removed`（core/registry.retire_line）。E14（テスト番号が入った）：`number_set`（core/settings.set_value。契約者の値）。
- E15（判定待ちの行に、ほかの回線がかけた）：`claimed`（core/gate.claim が通したとき）。E17（回線が「通話中」のまま）：`stuck`（supervisor/line.loop）。
- E16（試行のせいが決まった）：表が出した「試行のせいが決まった」を、この部品の中で、その試行の行・番号・試行のせいの欄へ渡す（`_Run.follow`）。
- E19（お客様の「もうかけないで」・業務の終わり）は、表が何もしない（R-14）ので起こさない。

**読み方**（17章に書き方の無かった所。4段目の前に管理者・CL-2 が見直す）
- (1) 電話の出来事の時刻は、その電話が終わった時刻（回線の続きの始まり＝最初の鳴らなかった電話の時刻・復旧待ちの「終えた電話」の終わり）。
  ほかの出来事の時刻は、取引の時刻（データベースの now()）。
- (2) 番号（Event.number）は、依頼者の連絡先（contact_id）。回線の声の続きの「違うお客様」も連絡先で数える（今の作りは番号の文字＝
  phone_e164 で数える。同じ番号を2つの依頼者が持つときだけ違う）。電話番号そのものを新しい欄に持たないため。
- (3) 表の決まりの値（health_rules.Rules）は、回線・契約者の欄は契約者の値、行・番号・試行のせいの欄は、その依頼者の値（業務の値は読まない）。
- (4) テスト番号が入った（E14）は、契約者の値を入れたときだけ起こす（管理者がシステムの初めの値を入れたときは、契約者をまたぐ入口が無い
  ＝4段目で決める）。
- (5) 直した版（E11）は、その契約者の影が動き始めたあとに替わった版だけを渡す（表は E11 の時刻を見ずに待っている行を戻すので、前の版で、
  そのあとのこちらの不具合を戻さない）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from functools import wraps
from typing import Any
from uuid import UUID

from salescall.core import access as layer
from salescall.core import errors, health_rules as hr, settings
from salescall.core.access import Access, Actor, Tx

__all__ = ["ORDER", "after_call", "by_hand_stop", "by_hand_task", "claimed", "decode", "encode", "fixed", "in_tenant", "number_set",
           "removed", "stage_name", "stuck", "tested", "tick"]

LINE, TENANT, TASK, NUMBER, ATTEMPT = "line", "tenant", "task", "number", "attempt"
ORDER = (LINE, TENANT, TASK, NUMBER, ATTEMPT)   # 鍵を取る順（17章：回線→契約者。行・番号・試行のせいはそのあと）
INITIAL: dict[str, Any] = {   # 欄が無いときの初めの状態（17-1 の、待ちでない段）
    LINE: hr.Line(hr.LineStage.HEALTHY), TENANT: hr.Tenant(hr.TenantStage.NORMAL), TASK: hr.Task(hr.TaskStage.ON_PLAN),
    NUMBER: hr.Number(hr.NumberStage.NONE), ATTEMPT: hr.Attempt(hr.Blame.EMPTY)}
TEST_RESULTS = {"rang": "E6", "silent": "E7", "unknown": "E8", "withheld": "E9"}   # テスト番号の答え → 出来事（`tested`）
BY = {"admin": "管理者", "operator": "契約者"}   # 手で再開・戻す・解く・外す（E12・E13）のは、管理者か契約者だけ


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
# 1つの出来事を、鍵を取った欄へ渡す（表を引くのは `give` の1か所）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Stages:
    """行の欄のうち、その段の行を全部（取引の契約者の中。依頼者をまたぐ）。"""
    names: tuple[str, ...]


@dataclass
class _Box:
    subject: str
    key: str
    client: str | None
    state: Any
    created: bool
    facts: dict[str, Any] = field(default_factory=dict)   # 試行：その行・連絡先・回線・終わった時刻
    changed: bool = False


class _Run:
    """1つの取引の中の、出来事の渡し（鍵を取る・表を引く・書く）。"""

    def __init__(self, tx: Tx, origin: str) -> None:
        row = tx.one("SELECT salescall.cur_tenant() AS t, now() AS now")
        self.tx, self.origin, self.tenant, self.now = tx, origin, row["t"], row["now"]
        self.boxes: dict[tuple[str, str], _Box] = {}
        self.moves: list[dict[str, Any]] = []
        self.decided: list[tuple[str, str, datetime]] = []   # 表が「試行のせいが決まった」と出した（試行, せい, 時刻）
        self._rules: dict[str | None, hr.Rules] = {}

    def take(self, *wants: tuple[str, Any]) -> list[_Box]:
        """鍵を取って読む（無ければ初めの状態で作る）。順は `ORDER`（同じ取引で前に取った欄は、取り直さない）。返すのは、渡した順の欄
        （Stages はその段の行の並び＝1つの要素がリスト）。"""
        ask: list[dict[str, Any]] = []
        for subject, key in sorted(wants, key=lambda w: ORDER.index(w[0])):
            if isinstance(key, Stages):
                ask.append({"s": subject, "stages": list(key.names)})
            elif (subject, str(key)) not in self.boxes:
                init = INITIAL[subject]
                ask.append({"s": subject, "k": str(key), "init": {"stage": stage_name(init), "state": encode(init)}})
        if ask:
            for r in self.tx.one("SELECT salescall.health_take(%s) AS r", (json.dumps(ask),))["r"]:
                if (r["s"], r["k"]) not in self.boxes:   # 同じ取引で前に取った欄は、手元の状態のまま
                    state = hr.Attempt(hr.Blame[r["stage"].upper()]) if r["s"] == ATTEMPT else decode(r["state"])
                    self.boxes[(r["s"], r["k"])] = _Box(r["s"], r["k"], r["client"], state, r["created"],
                                                        {x: r[x] for x in ("task", "contact", "line", "ended_at") if x in r})
        out: list[Any] = []
        for subject, key in wants:
            if isinstance(key, Stages):
                names = {n.upper() for n in key.names}
                out.append(sorted((b for b in self.boxes.values() if b.subject == TASK and b.state.stage.name in names),
                                  key=lambda b: b.key))
            else:
                out.append(self.boxes[(subject, str(key))])
        return out

    def rules(self, client: str | None) -> hr.Rules:
        """表の決まりの値（読み方 (3)：回線・契約者は契約者の値・行・番号・試行のせいは、その依頼者の値）。"""
        if client not in self._rules:
            scope = dict(tenant_id=self.tenant, client_id=client, business_id=None)
            self._rules[client] = hr.Rules.from_settings(
                {key: settings.resolve(self.tx, key, **scope) for key in hr.SETTING_KEYS.values()})
        return self._rules[client]

    def give(self, box: _Box, ev: hr.Event, *, once: str | None = None) -> hr.Outcome | hr.Mistake:
        """表を引く（1か所）：今の状態と出来事で表の1行を引き、次の状態を欄に置き、移った記録を足す。引けなければ（誤りの値）、状態は
        そのままで、誤りとして記録する（影なので、することの並びも直す担当への修正依頼も、記録だけ）。"""
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
        self.follow()
        rows = [{"s": b.subject, "k": b.key, "stage": stage_name(b.state), "state": None if b.subject == ATTEMPT else encode(b.state)}
                for b in self.boxes.values() if b.changed]
        self.tx.execute("SELECT salescall.health_save(%s, %s)", (json.dumps(rows), json.dumps(self.moves, ensure_ascii=False)))


# ─────────────────────────────────────────────────────────────────────────────
# 影：失敗しても、今の作りは続く（1か所）
# ─────────────────────────────────────────────────────────────────────────────

def _failed(tx: Tx, origin: str, error: BaseException) -> None:
    """影の書き込みが失敗したことを、移った記録に残す（残せなければ、あきらめる。今の作りは止めない）。"""
    what = f"{type(error).__name__}：{str(error)[:300]}"
    try:
        with tx.savepoint():
            tx.execute("SELECT salescall.health_save('[]', %s)", (json.dumps([{"origin": origin, "failure": what}], ensure_ascii=False),))
    except Exception as e:  # noqa: BLE001 記録もできない（契約者の取引でない など）：今の作りを止めない
        if layer.conflict(e) or layer.unreachable(e):
            raise


def _shadow(entry: Callable[..., None]) -> Callable[..., None]:
    """入口を、取引の中の区切りで動かす（失敗したら、その区切りだけ取り消し、記録して戻る）。ぶつかり・届かない誤りは返す。"""
    @wraps(entry)
    def run(tx: Tx, *args: Any, **kwargs: Any) -> None:
        try:
            with tx.savepoint():
                entry(tx, *args, **kwargs)
        except Exception as e:  # noqa: BLE001 影の失敗で、今の作りを止めない
            if layer.conflict(e) or layer.unreachable(e):
                raise
            _failed(tx, entry.__name__, e)
    return run


def in_tenant(access: Access, actor: Actor, tenant_id: UUID, entry: Callable[..., None], *args: Any) -> None:
    """取引の外から入口を呼ぶ（その契約者の取引で）。データベースに届かない・ぶつかりが続く ときも、呼んだ側を止めない（記録できれば記録）。"""
    try:
        access.in_tenant(actor, tenant_id, lambda tx: entry(tx, *args))
    except Exception as e:  # noqa: BLE001 影の失敗で、今の作りを止めない
        try:
            access.in_tenant(actor, tenant_id, lambda tx: _failed(tx, getattr(entry, "__name__", "影"), e))
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 入口（出来事を起こす所ごと）
# ─────────────────────────────────────────────────────────────────────────────

_CALL = (
    "SELECT a.attempt_id, a.line_id, a.task_id, a.contact_id, a.claimed_at, a.ended_at,"
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


@_shadow
def after_call(tx: Tx, attempt_id: UUID) -> None:
    """電話が終わった（E1〜E5）。1つの試行で1回だけ（試行のせいの欄の行があれば、もう渡した）。"""
    if tx.one("SELECT 1 FROM salescall.attempt_blames WHERE attempt_id = %s", (attempt_id,)) is not None:
        return
    a = tx.one(_CALL, (attempt_id,))
    if a is None:   # 発信まで進んでいない・まだ終わっていない
        return
    code = _call_event(a)
    run = _Run(tx, "after_call")
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
    elif code == "E4":
        ev = hr.Event("E4", at, attempt=me, number=number, line=line_id, resume_allowed=a["resume_allowed"])
        sends = [(line, ev), (task, ev), (num, hr.Event("E1", at, attempt=me, number=number))]
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


@_shadow
def tested(tx: Tx, line_id: UUID, result: str) -> None:
    """テスト番号の答え（E6 鳴った・E7 鳴らなかった・E8 見分けられない・E9 PC の部品が試さなかった／話している途中）。"""
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
    for r in tx.all("SELECT state FROM salescall.line_states WHERE line_id <> %s", (line_id,)):
        s = decode(r["state"])
        if (s.stage is hr.LineStage.CHECK_C or s.wait is hr.Wait.ANSWER) and s.next_at is not None and s.next_at > now:
            until = s.next_at if until is None else max(until, s.next_at)
    return until


@_shadow
def tick(tx: Tx, line_id: UUID) -> None:
    """回線ごとの処理の毎回の頭（1か所）：その回線の「次に動く時刻」が来ていれば E10。その回線が元気で声の続きが0（声が通ると分かって
    いる回線＝17-3-1 の下の注）なら、声のかけ直しを待つ行へ E18。声のかけ直しを待つ行で、声が聞き取れなかった回線がこの回線で、
    待つ時刻が来ていれば E10（R-13）。"""
    run = _Run(tx, "tick")
    line, voice = run.take((LINE, line_id), (TASK, Stages(("voice_redial",))))
    due = hr.next_at(line.state)
    if due is not None and run.now >= due:
        run.give(line, hr.Event("E10", run.now, test_number=_test_number(tx), other_test_until=_other_test_until(tx, line_id, run.now)))
    open_ = line.state.stage is hr.LineStage.HEALTHY and line.state.m == 0
    for t in voice:
        if open_:
            run.give(t, hr.Event("E18", run.now, line=str(line_id)))
        elif t.state.line == str(line_id) and run.now >= hr.next_at(t.state):
            run.give(t, hr.Event("E10", run.now))
    run.save()


@_shadow
def stuck(tx: Tx, line_id: UUID, lasted: float) -> None:
    """回線が「通話中」と答え続けて、かけられない（E17。lasted＝かけられなくなってからの秒。かけられなくなった時刻は、取引の時刻から
    その秒だけ前＝データベースの時計にそろえる）。"""
    run = _Run(tx, "stuck")
    (line,) = run.take((LINE, line_id))
    run.give(line, hr.Event("E17", run.now, since=run.now - timedelta(seconds=lasted)))
    run.save()


@_shadow
def fixed(tx: Tx) -> None:
    """種類 k の修正依頼が「直した」と記録され、そのあと動いているプログラムの版が替わった（E11。時刻は今の作りの1か所
    salescall.trouble_fixed_at）。同じ「直した版」は1回だけ（契約者の移った記録の once）。その契約者の影が動き始める前に替わった版は
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


@_shadow
def by_hand_task(tx: Tx, task_id: UUID, reason: str) -> None:
    """管理者か契約者が、休んだ行を戻した（E12。core/troubles.restore）。"""
    who, name = _by(tx)
    if who is None:
        return
    run = _Run(tx, "by_hand_task")
    (task,) = run.take((TASK, task_id))
    run.give(task, hr.Event("E12", run.now, by=f"{who}（{name}）", reason=reason))
    run.save()


@_shadow
def by_hand_stop(tx: Tx, stop_event_id: int, reason: str) -> None:
    """管理者か契約者が、止めた停止を解いた（core/stops.resume）。この章の状態にかかるのは、回線の異常で止めた回線（line_failure）と、
    こちらのプログラムの不具合で止めた契約者（program_trouble）だけ（ほかの停止は 17章の外）：その回線・契約者の欄へ E12。"""
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
    elif s["scope"] == "tenant" and s["cause"] == "program_trouble" and s["tenant_id"] == run.tenant:
        (box,) = run.take((TENANT, run.tenant))
    else:
        return
    run.give(box, ev)
    run.save()


@_shadow
def removed(tx: Tx, line_id: UUID, reason: str) -> None:
    """管理者か契約者が、回線を外した（E13。core/registry.retire_line）。"""
    who, name = _by(tx)
    run = _Run(tx, "removed")
    (line,) = run.take((LINE, line_id))
    run.give(line, hr.Event("E13", run.now, by=f"{who or 'システム'}（{name}）", reason=reason))
    run.save()


@_shadow
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


@_shadow
def claimed(tx: Tx, task_id: UUID, line_id: UUID) -> None:
    """門が、その行に、その回線の予約を通した：その行が、ほかの回線の判定待ちなら E15（0144。同じ回線の判定待ちの行は、門が通さない）。"""
    row = tx.one("SELECT stage, state FROM salescall.task_states WHERE task_id = %s", (task_id,))
    if row is None or row["stage"] != "await_decision" or decode(row["state"]).line == str(line_id):
        return
    run = _Run(tx, "claimed")
    (task,) = run.take((TASK, task_id))
    run.give(task, hr.Event("E15", run.now, line=str(line_id)))
    run.save()
