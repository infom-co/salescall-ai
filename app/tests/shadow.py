"""試験の土台：今の作りの答えと、状態の欄の影（core/health。DESIGN.md 17章の3段目）を、電話の一生の場面の各時点で比べる（持ち主 CL-2）。

比べるもの（17-6「表に置き換えて外すもの」の一覧のうち、答えが同じ問いのもの）：
- 試行のせい：今の作り＝こちらのせい（salescall.is_ours）か、鳴らなかった電話のせい（salescall.unrung_blame）／影＝試行のせいの欄。
- 行：判定待ち（salescall.task_awaiting_decision。回線ごと）・休み（task_resting）・復旧待ち（awaiting_recovery）／影＝行の欄の段。
- 回線：止めた（回線の異常で止めた停止がある）・まだ分からない続き（line_run_pending）・使わなくした（retired_at）・声で止めた続きの回数
  （audio_stops_in_a_row）／影＝回線の欄の段と中身 j。
- 番号：番号のせいの続き（failed_run の n_sure）・使えない（n_sure が並びの数）・分からない電話のあいだ待つ印（attempts.number_unconfirmed）／
  影＝番号の欄の n・段・印。
- 契約者：こちらの不具合で止めた（停止がある）・種類ごとの違う行の数（consecutive_errors の program_fixed）／影＝契約者の欄。
- 影が表を引けなかった（誤りの値）・影の書き込みが失敗した（移った記録）。
食い違いは（何を・どれの・今の作り・影）の組。どれかは、比べる試験の分け方の表（tests/test_life.py の SHADOW_KNOWN）で、今の作りの誤りか、
表の誤りに見えるものかに分ける。⚠️ 試験のデータは架空のものだけ。
"""

from __future__ import annotations

from typing import Any, NamedTuple
from unittest import mock
from uuid import UUID

from salescall.core import attempts, errors, health
from salescall.core.health_rules import LineStage, NumberStage, TaskStage, TenantStage
from tests.support import SYSTEM, owner_access


class Diff(NamedTuple):
    what: str       # 何を比べたか
    label: str      # どれの（場面の中の名前：行1・回線1 など）
    current: Any    # 今の作りの答え
    shadow: Any     # 影の答え


KINDS = tuple(sorted(set(errors.TROUBLES.values())))   # こちらのプログラムの不具合の種類 k


class Labels:
    """場面の中の名前（初めて見た順に、行1・回線1…。試験ごと）と、もう比べなくてよい試行（両方が決まった同じ答え＝あとで変わらない）。"""

    def __init__(self) -> None:
        self.names: dict[tuple[str, Any], str] = {}
        self.settled: set[UUID] = set()
        self.last_move = 0   # 前の時点までに見た、移った記録の番号
        self.mark: tuple[Any, ...] | None = None   # 前の時点の、答えが変わりうるものの数（`observe`）

    def of(self, kind: str, key: Any) -> str:
        k = (kind, str(key))
        if k not in self.names:
            self.names[k] = f"{kind}{1 + sum(1 for x in self.names if x[0] == kind)}"
        return self.names[k]


FINAL = ("empty", "number", "line", "ours")   # 試行のせいの、あとで変わらない答え


def observe(tenant: UUID, labels: Labels) -> list[Diff] | None:
    """いまの時点で、今の作りと影の答えが違うものの一覧。前の時点から、どちらの答えも変わりえない（その契約者の電話・締め・テスト番号・
    停止・移った記録・直した記録・プログラムの版・戻した記録が増えておらず、日本の日付も同じ）なら、比べない（None）。"""
    return owner_access().in_tenant(SYSTEM, tenant, lambda tx: _observe_if_moved(tx, labels))


def _observe_if_moved(tx: Any, labels: Labels) -> list[Diff] | None:
    mark = tuple(tx.one(
        "SELECT salescall.jst(now())::date AS d,"
        " (SELECT count(*) FROM salescall.attempts a WHERE a.tenant_id = salescall.cur_tenant() AND a.ended_at IS NOT NULL) AS a,"
        " (SELECT count(*) FROM salescall.attempts a WHERE a.tenant_id = salescall.cur_tenant()) AS c,"
        " (SELECT count(*) FROM salescall.task_results r WHERE r.tenant_id = salescall.cur_tenant()) AS r,"
        " (SELECT count(*) FROM salescall.apology_outcomes o WHERE o.tenant_id = salescall.cur_tenant()) AS o,"
        " (SELECT max(m.move_id) FROM salescall.state_moves m WHERE m.tenant_id = salescall.cur_tenant()) AS m,"
        " (SELECT max(t.test_id) FROM salescall.line_ring_tests t WHERE t.tenant_id = salescall.cur_tenant()) AS t,"
        " (SELECT max(e.event_id) FROM salescall.stop_events e WHERE e.tenant_id = salescall.cur_tenant()) AS e,"
        " (SELECT count(*) FROM salescall.fix_reports) AS f, (SELECT count(*) FROM salescall.program_versions) AS v,"
        " (SELECT count(*) FROM salescall.task_trouble_restores x WHERE x.tenant_id = salescall.cur_tenant()) AS x,"
        " (SELECT count(*) FROM salescall.lines l WHERE l.tenant_id = salescall.cur_tenant() AND l.retired_at IS NOT NULL) AS l"
    ).values())
    if mark == labels.mark:
        return None
    labels.mark = mark
    # 番号の続き（salescall.failed_run）は、1つの時点で番号ごとに1回だけ読む（今の作りの number_unusable・number_unconfirmed も
    # 同じ答えを読む。読み方は変えない）
    memo: dict[Any, Any] = {}
    real = attempts.failed_run

    def failed_run(tx_: Any, contact_id: Any) -> Any:
        if contact_id not in memo:
            memo[contact_id] = real(tx_, contact_id)
        return memo[contact_id]
    with mock.patch.object(attempts, "failed_run", failed_run):
        return _observe(tx, labels)


def _nth(tx: Any, attempt_id: UUID) -> int:
    return tx.one("SELECT count(*) AS n FROM salescall.attempts x, salescall.attempts a WHERE a.attempt_id = %s"
                  " AND x.task_id = a.task_id AND salescall.is_dialed(x.state)"
                  " AND (x.claimed_at, x.attempt_id) <= (a.claimed_at, a.attempt_id)", (attempt_id,))["n"]


def _observe(tx: Any, labels: Labels) -> list[Diff]:
    out: list[Diff] = []
    # 行・回線の名前を、かけた順に付ける（食い違いの無いものにも付ける＝名前が場面の進み方で変わらない）
    for r in tx.all("SELECT a.task_id, a.line_id FROM salescall.attempts a WHERE a.tenant_id = salescall.cur_tenant()"
                    " AND salescall.is_dialed(a.state) ORDER BY a.claimed_at, a.attempt_id"):
        labels.of("回線", r["line_id"])
        labels.of("行", r["task_id"])
    # 試行のせい（影の欄がある試行＝出来事を渡した試行だけ。両方が決まって同じなら、もう比べない）
    for r in tx.all(
            "SELECT a.attempt_id, a.task_id,"
            " CASE WHEN salescall.is_ours(a.attempt_id) THEN 'ours' ELSE coalesce(salescall.unrung_blame(a.attempt_id), 'empty') END AS cur,"
            " b.blame AS shadow FROM salescall.attempts a JOIN salescall.attempt_blames b ON b.attempt_id = a.attempt_id"
            " WHERE a.tenant_id = salescall.cur_tenant() AND NOT (a.attempt_id = ANY(%s))", (list(labels.settled),)):
        cur = {"pending": "unknown"}.get(r["cur"], r["cur"])
        if cur != r["shadow"]:
            out.append(Diff("試行のせい", f"{labels.of('行', r['task_id'])}の{_nth(tx, r['attempt_id'])}本目", cur, r["shadow"]))
        elif cur in FINAL:
            labels.settled.add(r["attempt_id"])
    lines = tx.all(
        "SELECT l.line_id, l.retired_at IS NOT NULL AS retired, salescall.line_run_pending(l.line_id) AS pending,"
        " (SELECT s.event_id FROM salescall.open_stop_events s WHERE s.scope = 'line' AND s.line_id = l.line_id"
        "  AND s.cause::text = 'line_failure' ORDER BY s.event_id DESC LIMIT 1) AS stop, x.stage, x.state"
        " FROM salescall.lines l JOIN salescall.line_states x ON x.line_id = l.line_id"   # 影の欄がまだ無い回線は比べない
        " WHERE l.tenant_id = salescall.cur_tenant() ORDER BY l.line_id")
    for r in lines:
        name = labels.of("回線", r["line_id"])
        s = health.decode(r["state"])
        stopped = r["stop"] is not None
        resting = s.stage in (LineStage.LINE_REST, LineStage.AUDIO_REST)
        checking = s.stage in (LineStage.CHECK_A, LineStage.CHECK_B, LineStage.CHECK_C)
        if stopped != resting:
            out.append(Diff("回線を止めているか", name, stopped, s.stage.value))
        if r["pending"] != checking:
            out.append(Diff("回線のまだ分からない続き", name, r["pending"], s.stage.value))
        if r["retired"] != (s.stage is LineStage.OUT_OF_USE):
            out.append(Diff("回線を使わなくしたか", name, r["retired"], s.stage.value))
        j_cur = tx.one("SELECT salescall.audio_stops_in_a_row(%s) AS j", (r["stop"],))["j"] if stopped else 0
        j_shadow = s.j if s.stage is LineStage.AUDIO_REST else 0
        if j_cur != j_shadow:
            out.append(Diff("声で止めた続きの回数", name, j_cur, j_shadow))
    # 行（判定待ちは回線ごと。比べるのは、影が判定待ちか、締めていない発信がある行だけ＝ほかはどちらも待っていない）
    for r in tx.all(
            "SELECT t.task_id, t.stage, t.state, salescall.task_resting(t.task_id) AS resting,"
            " salescall.awaiting_recovery(t.task_id) AS recovering,"
            " (t.stage = 'await_decision' OR EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.task_id = t.task_id"
            "   AND salescall.is_dialed(a.state) AND NOT salescall.is_settled(a.attempt_id))) AS open"
            " FROM salescall.task_states t WHERE t.tenant_id = salescall.cur_tenant() ORDER BY t.task_id"):
        name = labels.of("行", r["task_id"])
        s = health.decode(r["state"])
        if r["resting"] != (s.stage is TaskStage.RESTING):
            out.append(Diff("行の休み", name, r["resting"], s.stage.value))
        if r["recovering"] != (s.stage is TaskStage.AWAIT_RECOVERY):
            out.append(Diff("行の復旧待ち", name, r["recovering"], s.stage.value))
        for line in lines if r["open"] else ():
            cur = tx.one("SELECT salescall.task_awaiting_decision(%s, %s) AS w", (r["task_id"], line["line_id"]))["w"]
            mine = s.stage is TaskStage.AWAIT_DECISION and s.line == str(line["line_id"])
            if cur != mine:
                out.append(Diff("行の判定待ち（回線ごと）", f"{name}・{labels.of('回線', line['line_id'])}", cur, s.stage.value))
    # 番号（比べるのは、影に続きがあるか、鳴らなかった電話のある番号だけ）
    tenant = tx.one("SELECT salescall.cur_tenant() AS t")["t"]
    for r in tx.all("SELECT n.contact_id, n.client_id, n.state, (SELECT t.task_id FROM salescall.tasks t WHERE t.contact_id = n.contact_id"
                    " ORDER BY t.task_id LIMIT 1) AS task_id FROM salescall.number_states n"
                    " WHERE n.tenant_id = salescall.cur_tenant() AND (n.stage <> 'none' OR n.state::text LIKE '%%\"hold\": true%%'"
                    "   OR EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.contact_id = n.contact_id AND salescall.is_dialed(a.state)"
                    "              AND a.ended_at IS NOT NULL AND NOT salescall.rang(a.state, a.cut_off, a.attempt_id)))"
                    " ORDER BY n.contact_id"):
        name = f"{labels.of('行', r['task_id'])}の番号"
        s = health.decode(r["state"])
        scope = dict(tenant_id=tenant, client_id=r["client_id"], business_id=None)
        run = attempts.failed_run(tx, r["contact_id"])
        if run["n_sure"] != s.n:
            out.append(Diff("番号のせいの続きの数", name, run["n_sure"], s.n))
        dead = attempts.number_unusable(tx, r["contact_id"], **scope)
        if dead != (s.stage is NumberStage.DEAD):
            out.append(Diff("番号が使えない", name, dead, s.stage.value))
        hold = attempts.number_unconfirmed(tx, r["contact_id"], **scope)
        if hold != s.hold:
            out.append(Diff("番号の分からない電話の待ち（N-4 の印）", name, hold, s.hold))
    # 契約者
    for r in tx.all("SELECT t.stage, t.state, EXISTS (SELECT 1 FROM salescall.open_stop_events s WHERE s.scope = 'tenant'"
                    " AND s.tenant_id = t.tenant_id AND s.cause::text = 'program_trouble') AS stopped FROM salescall.tenant_states t"
                    " WHERE t.tenant_id = salescall.cur_tenant()"):
        s = health.decode(r["state"])
        if r["stopped"] != (s.stage is TenantStage.STOPPED):
            out.append(Diff("契約者を止めているか", "契約者", r["stopped"], s.stage.value))
        for k in KINDS:
            n = tx.one("SELECT salescall.consecutive_errors(ARRAY[%s], 'program_trouble', 'program_fixed') AS n", (k,))["n"]
            if n != len(s.r(k)):
                out.append(Diff(f"こちらの不具合の違う行の数（{k}）", "契約者", n, len(s.r(k))))
    # 影が表を引けなかった・影の書き込みが失敗した（前の時点のあとに足された記録だけ）
    for r in tx.all("SELECT m.move_id, m.subject, m.from_stage, m.event, m.mistake->>'kind' AS kind, m.mistake->>'gap' AS gap,"
                    " m.failure FROM salescall.state_moves m WHERE m.tenant_id = salescall.cur_tenant() AND m.move_id > %s"
                    " AND (m.mistake IS NOT NULL OR m.failure IS NOT NULL) ORDER BY m.move_id", (labels.last_move,)):
        labels.last_move = r["move_id"]
        if r["failure"] is not None:
            out.append(Diff("影の書き込みの失敗", "影", r["failure"][:120], None))
        else:
            out.append(Diff("表を引けない", f"{r['subject']}・{r['from_stage']}・{r['event']}", r["kind"], r["gap"]))
    return out
