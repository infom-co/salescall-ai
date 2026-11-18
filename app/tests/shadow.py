"""試験の土台：読み手の答えと、状態の欄（core/health。DESIGN.md 17章）を、電話の一生の場面の各時点で比べる（持ち主 CL-2）。

4段目（2026-10-29）：読み手（締め・次の動き・門・候補・回線ごとの処理）は、状態の欄を読むだけになった。3段目は「今の作り（読むたびに
導き直す関数）」と「影（状態の欄）」を比べ、食い違いを今の作りの誤り（17-8）に分けたが、4段目では読み手そのものが表なので、食い違いは
0のはず（分け方の一覧は無し）。比べるもの：
- 試行のせい：次の動きが受け取る名前（core/health.blame_of）と、試行のせいの欄。
- 行：締めの処理が待つか（core/health.waiting_decision）と、行の欄の判定待ち。候補（scheduler.due_tasks。回線ごと）に、行の欄が待つ段の行・
  その回線の判定待ちの行・番号の欄が「使えない」か待つ印の行が出ていないか。
- 回線：休み・使用停止の回線を、回線ごとの処理がお客様へかけさせないか（core/health.tick の答えの calls＝回線の欄）。
- 番号：門と次の動きが読む口（number_dead・number_held・number_wait）と、番号の欄。
- 表を引けなかった（誤りの値）・入口の失敗（移った記録）：場面のどの時点でも無いこと。
食い違いは（何を・どれの・読み手の答え・欄）の組。⚠️ 試験のデータは架空のものだけ。
"""

from __future__ import annotations

from typing import Any, NamedTuple
from uuid import UUID

from salescall.core import gate, health, scheduler
from salescall.core.health_rules import RESTS, LineStage, NumberStage
from tests.support import SYSTEM, owner_access


class Diff(NamedTuple):
    what: str       # 何を比べたか
    label: str      # どれの（場面の中の名前：行1・回線1 など）
    current: Any    # 読み手の答え
    shadow: Any     # 状態の欄


class Labels:
    """場面の中の名前（初めて見た順に、行1・回線1…。試験ごと）。"""

    def __init__(self) -> None:
        self.names: dict[tuple[str, Any], str] = {}
        self.last_move = 0   # 前の時点までに見た、移った記録の番号
        self.mark: tuple[Any, ...] | None = None   # 前の時点の、答えが変わりうるものの数（`observe`）

    def of(self, kind: str, key: Any) -> str:
        k = (kind, str(key))
        if k not in self.names:
            self.names[k] = f"{kind}{1 + sum(1 for x in self.names if x[0] == kind)}"
        return self.names[k]


def observe(tenant: UUID, labels: Labels) -> list[Diff] | None:
    """いまの時点で、読み手の答えと状態の欄が違うものの一覧。前の時点から、どちらも変わりえない（その契約者の電話・締め・移った記録・
    日本の日付が同じ）なら、比べない（None）。"""
    return owner_access().in_tenant(SYSTEM, tenant, lambda tx: _observe_if_moved(tx, labels))


def _observe_if_moved(tx: Any, labels: Labels) -> list[Diff] | None:
    mark = tuple(tx.one(
        "SELECT salescall.jst(now())::date AS d,"
        " (SELECT count(*) FROM salescall.attempts a WHERE a.tenant_id = salescall.cur_tenant() AND a.ended_at IS NOT NULL) AS a,"
        " (SELECT count(*) FROM salescall.attempts a WHERE a.tenant_id = salescall.cur_tenant()) AS c,"
        " (SELECT count(*) FROM salescall.task_results r WHERE r.tenant_id = salescall.cur_tenant()) AS r,"
        " (SELECT max(m.move_id) FROM salescall.state_moves m WHERE m.tenant_id = salescall.cur_tenant()) AS m"
    ).values())
    if mark == labels.mark:
        return None
    labels.mark = mark
    return _observe(tx, labels)


def _observe(tx: Any, labels: Labels) -> list[Diff]:
    out: list[Diff] = []
    for r in tx.all("SELECT a.task_id, a.line_id FROM salescall.attempts a WHERE a.tenant_id = salescall.cur_tenant()"
                    " AND salescall.is_dialed(a.state) ORDER BY a.claimed_at, a.attempt_id"):
        labels.of("回線", r["line_id"])
        labels.of("行", r["task_id"])
    # 試行のせい：次の動きが受け取る名前
    names = {"empty": None, "unknown": "pending", "number": "number", "line": "line", "ours": "ours"}
    for r in tx.all("SELECT b.attempt_id, b.blame, a.task_id, salescall.rang(a.state, a.cut_off, a.attempt_id) AS rang"
                    " FROM salescall.attempt_blames b"
                    " JOIN salescall.attempts a ON a.attempt_id = b.attempt_id WHERE b.tenant_id = salescall.cur_tenant()"):
        got = health.blame_of(tx, r["attempt_id"])
        if got != (None if r["rang"] and r["blame"] == "ours" else names[r["blame"]]):   # 鳴ったあとのこちらの不具合は「途中で切れた」
            out.append(Diff("試行のせい", labels.of("行", r["task_id"]), got, r["blame"]))
    # 行：締めの処理が待つか
    for r in tx.all("SELECT t.task_id, t.state FROM salescall.task_states t WHERE t.tenant_id = salescall.cur_tenant()"
                    " AND t.stage = 'await_decision'"):
        s = health.decode(r["state"])
        if not health.waiting_decision(tx, s.attempt):
            out.append(Diff("締めの待ち", labels.of("行", r["task_id"]), False, s.stage.value))
    # 番号：門と次の動きが読む口
    for r in tx.all("SELECT n.contact_id, n.state FROM salescall.number_states n WHERE n.tenant_id = salescall.cur_tenant()"):
        s = health.decode(r["state"])
        name = f"番号{r['contact_id']}"[:12]
        if health.number_dead(tx, r["contact_id"]) != (s.stage is NumberStage.DEAD):
            out.append(Diff("番号が使えない", name, not s.stage is NumberStage.DEAD, s.stage.value))
        if health.number_held(tx, r["contact_id"]) != s.hold:
            out.append(Diff("番号の待つ印", name, not s.hold, s.hold))
        if (health.number_wait(tx, r["contact_id"]) is None) != (s.stage is not NumberStage.RUN):
            out.append(Diff("番号の決まった順", name, s.stage is NumberStage.RUN, s.stage.value))
    # 候補：回線ごとに、待つ行・その回線の判定待ちの行・使えない番号・待つ印の番号の行が出ていないか
    lines = tx.all("SELECT l.line_id, x.stage, x.state FROM salescall.lines l LEFT JOIN salescall.line_states x ON x.line_id = l.line_id"
                   " WHERE l.tenant_id = salescall.cur_tenant() ORDER BY l.line_id")
    clients = [c["client_id"] for c in tx.all("SELECT client_id FROM salescall.clients WHERE tenant_id = salescall.cur_tenant()")]
    for ln in lines:
        name = labels.of("回線", ln["line_id"])
        s = health.decode(ln["state"]) if ln["state"] is not None else None
        resting = s is not None and (s.stage in RESTS or s.stage is LineStage.OUT_OF_USE)
        for client in clients:
            for t in _candidates(tx, client, ln["line_id"]):
                ts = _task_state(t["task_id"])
                waits = ts is not None and (gate.TASK_STAGE_REASONS.get(ts["stage"]) is not None
                                            or (ts["stage"] == "await_decision" and health.decode(ts["state"]).line == str(ln["line_id"])))
                if waits:
                    out.append(Diff("候補に、行の欄が待つ行", f"{labels.of('行', t['task_id'])}・{name}", True, ts["stage"]))
        if resting and _offered(ln["line_id"]):
            out.append(Diff("休みの回線がお客様へかけた", name, True, s.stage.value))
    # 表を引けなかった・入口の失敗（前の時点のあとに足された記録だけ）
    for r in tx.all("SELECT m.move_id, m.subject, m.from_stage, m.event, m.mistake->>'kind' AS kind, m.mistake->>'gap' AS gap,"
                    " m.failure FROM salescall.state_moves m WHERE m.tenant_id = salescall.cur_tenant() AND m.move_id > %s"
                    " AND (m.mistake IS NOT NULL OR m.failure IS NOT NULL) ORDER BY m.move_id", (labels.last_move,)):
        labels.last_move = r["move_id"]
        if r["failure"] is not None:
            out.append(Diff("入口の失敗", "入口", r["failure"][:120], None))
        else:
            out.append(Diff("表を引けない", f"{r['subject']}・{r['from_stage']}・{r['event']}", r["kind"], r["gap"]))
    labels.last_move = max(labels.last_move, tx.one("SELECT coalesce(max(move_id), 0) AS m FROM salescall.state_moves"
                                                    " WHERE tenant_id = salescall.cur_tenant()")["m"])
    return out


def _candidates(tx: Any, client: UUID, line_id: UUID) -> list[dict[str, Any]]:
    """その依頼者の、その回線の候補（回線ごとの処理が門に渡す行。依頼者の取引で読む）。"""
    tenant = tx.one("SELECT salescall.cur_tenant() AS t")["t"]
    return owner_access().in_client(SYSTEM, tenant, client, lambda ctx: scheduler.due_tasks(ctx, 50, line_id=line_id))


def _task_state(task_id: UUID) -> dict[str, Any] | None:
    return owner_access().in_system(SYSTEM, lambda tx: tx.one("SELECT stage, state FROM salescall.task_states WHERE task_id = %s",
                                                              (task_id,)))


def _offered(line_id: UUID) -> bool:
    """休みの回線で、休みに入ったあとに、お客様への発信が始まったか。"""
    return owner_access().in_system(SYSTEM, lambda tx: tx.one(
        "SELECT EXISTS (SELECT 1 FROM salescall.attempts a, salescall.line_states s WHERE s.line_id = %s AND a.line_id = s.line_id"
        " AND salescall.is_dialed(a.state) AND a.claimed_at > s.moved_at) AS o", (line_id,))["o"])
