"""発信の門（Q2「今このお客様にかけてよいか」）。持ち主 CL-2。DESIGN.md 5-2・5-2-1。

- **判定と予約を1つの取引で行う**（直列化の水準。読み書きの層が始め、ぶつかったら最初からやり直す）。すべて通ったときだけ
  試行（予約 `claimed`）を作り、予約証を返す。**1つでも判断できなければ通さない**（設定が無い・暦の範囲の外・材料が無い）。
- 予約を作るのは、この部品の `claim` だけ（I-12。app/checks が見張る）。発信へ進めるのは出口（core/dial_exit.py）だけ。
- 時計はデータベースの時刻1つ（5-2-1 の1）。日本時間への直し方も、データベースの関数1つ（`salescall.jst`）。時間帯の決まりは core/calling_hours.py、データで決まる「かけない理由」は
  core/target_policy.py、止まっているかは停止の記録、段は core/quality_stage.py を読む（ここで2つ目の判断を作らない）。
- 回数と間隔の数え方は `pacing` の1か所（5-2-1 の8）。門も、次の動き（Q4）も、画面も、これを読む。
- 不許可は、理由の名前（下の REASONS が一覧の正本）を返し、記録に残す（画面で理由ごとに数える）。理由の種類：
  permanent＝待っても変わらない（次の動きは終える）／wait＝待てば変わる（次の動きは待ってかけ直す）／
  cannot_judge＝判断の材料が無い（通さない。契約者が直す。暦の取り込み・設定の値・業務の版）。
- 依頼者の範囲の外（ほかの依頼者・契約者の試行）は、中身を返さない確かめの関数だけで見る（「同じ契約者か、外か」だけ）。
- **どの番号へかけるか**は、いまの予定（次の動きの記録の最新）が持つ番号（行の番号の表にある番号だけ。DESIGN.md 3-5）。
  門は決め直さない。「同じお客様」は、かける番号が同じ発信と、同じ行（一覧の1行）の発信の両方（番号を変えて回数を増やさない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from salescall.core import (approval, business_rules, business_settings, calling_hours, dnc, quality_stage, settings, stops,
                            target_policy)
from salescall.core.access import Rejected, Tx
from salescall.core.calendar import CalendarOutOfRange, holiday_checker
from salescall.core.phone import normalize
from salescall.core.tasks import TaskNotFound

# 不許可の理由（名前 → 日本語・種類）。名前の一覧の正本はここだけ（画面・次の動きは、これを読む）。
REASONS: dict[str, tuple[str, str]] = {
    "no_schedule": ("次の予定がありません", "cannot_judge"),
    "task_ended": ("この行は処理済みです", "permanent"),
    "judge_pending": ("前の通話が判定できず、判定し直しを待っています", "wait"),
    "not_due": ("次の予定の時刻より前です", "wait"),
    "awaiting_decision": ("前の発信の判定と次の予定が、まだ済んでいません", "wait"),
    "awaiting_approval": ("契約者の承認を使う業務で、この行がまだ承認されていません", "wait"),
    "number_unknown": ("番号が不明です", "permanent"),
    "number_kind_forbidden": ("かけない種類の番号です", "permanent"),
    "number_kind_not_allowed": ("この業務の設定でかけない種類の番号です", "permanent"),
    "dnc_client": ("発信禁止です", "permanent"),
    "dnc_contractor": ("契約者の共通の発信禁止です", "permanent"),
    "other_contractor_called": ("ほかの契約者がかけた番号です（契約者の設定で、かけない）", "permanent"),
    "dnc_client_ref": ("この管理番号のお客様は発信禁止です", "permanent"),
    "max_attempts": ("この行に発信してよい回数を使い切りました", "permanent"),
    "no_version": ("実施回に業務設定の版が割り当てられていません", "cannot_judge"),
    "rules_missing": ("この版の業務の決まり（断りの答え・理由×扱い・再発信の方針）がありません", "cannot_judge"),
    "settings_missing": ("この版の業務設定（名乗り・質問・目的のお客様など）が無いか、確かめに通りません", "cannot_judge"),
    "target_name_missing": ("この業務は目的のお客様を名前で呼びますが、この行に目的のお客様の名前がありません", "cannot_judge"),
    "target_role_missing": ("この業務は目的のお客様を役職で呼びますが、この行に目的のお客様の役職がありません", "cannot_judge"),
    "version_not_ready": ("この版は、まだかけられる段に入っていません（模擬通話・最初の段の前）", "wait"),
    "stage_full": ("いまの段の件数に達しました（次の段へ進むのを待っています）", "wait"),
    "stopped": ("止まっています", "wait"),
    "outside_hours": ("かけてよい時間帯の外です", "wait"),
    "calendar_out_of_range": ("祝日の暦が無い日です", "cannot_judge"),
    "related_unsettled": ("このお客様にかかわる前の発信の締め（断られたかの確かめ）が、まだ済んでいません", "wait"),
    "same_contact_interval": ("同じお客様への前の発信から、間隔が空いていません", "wait"),
    "unknown_result_interval": ("結果が分からない前の発信から、間隔が空いていません", "wait"),
    "daily_attempts": ("同じお客様へ、今日かけてよい回数を使い切りました", "wait"),
    "cross_client_interval_same_tenant": ("ほかの依頼者が同じ番号へかけたばかりです", "wait"),
    "cross_client_interval_other_tenant": ("同じ番号へかけたばかりです", "wait"),   # 契約者の外は、中身を出さない
    "line_unknown": ("その回線はありません", "cannot_judge"),
    "line_not_leased": ("回線を借りていません（または、借用の期限が予約証より先に切れます）", "wait"),
    "line_daily_limit": ("この回線の、今日の発信の上限に達しました", "wait"),
    "line_busy": ("回線が使用中です", "wait"),
    "client_concurrency": ("この依頼者の同時の本数の上限です", "wait"),
    "contact_active": ("同じ番号のお客様（この依頼者の別の行を含む）に、進行中の発信があります", "wait"),
    "task_active": ("この行（ほかの番号を含む）に、進行中の発信があります", "wait"),
    "number_active_same_client": ("同じ番号に、進行中の発信があります", "wait"),
    "number_active_same_tenant": ("同じ番号に、ほかの依頼者の進行中の発信があります", "wait"),
    "number_active_other_tenant": ("同じ番号に、進行中の発信があります", "wait"),   # 契約者の外は、中身を出さない
    "setting_missing": ("設定の値が決まっていません", "cannot_judge"),
}

_DIALED = "state NOT IN ('claimed', 'released')"   # 発信まで進んだ試行（回数・間隔に数えるのは、これだけ。5-2-1 の8）
# 同じお客様への最小の間隔・1日の回数に数える発信：お客様の意思と関係なく繋がらなかった・切れたものは数えない（2026-10-14 ユーザー決定。
# 決まりはデータベースの1か所 salescall.not_by_callee）
_COUNTED = _DIALED + " AND NOT salescall.not_by_callee(state, disconnect, attempt_id)"
_DIALED_A = "a.state NOT IN ('claimed', 'released')"   # 同じ決まり（表の別名つき）
_SAME = "(contact_id = %s OR task_id = %s)"             # 同じお客様：かける番号が同じ発信、または同じ行の発信


@dataclass(frozen=True)
class Claim:
    """予約証。期限までに出口（core/dial_exit.py）で発信へ進めなければ、門が次の取引で取り消す。"""
    attempt_id: UUID
    task_id: UUID
    line_id: UUID
    phone_e164: str
    version_id: UUID
    expires_at: datetime
    purpose: str                 # 発信の目的（normal / ask_number_again / apology。会話の処理が読んで話す。DESIGN.md 3-5・10-1-2）
    wrong_phone_e164: str | None   # 番号の聞き直しのとき：番号違いだった番号（「お聞きした番号が…」と告げる）


@dataclass(frozen=True)
class Denied:
    reason: str
    message: str
    kind: str          # permanent / wait / cannot_judge


@dataclass(frozen=True)
class Pacing:
    """回数と間隔（5-2-1 の8）。earliest が None ＝ このお客様にかかわる前の発信の締め待ち（いつかけられるか、まだ決まらない）。"""
    remaining: int
    earliest: datetime | None
    binding: str | None    # earliest を決めた理由の名前（今すぐかけられるなら None）
    promised: bool         # いまの予定が、お客様と約束したかけ直しか（回数の上限・最小の間隔・1日の回数の対象外）


def _deny(reason: str) -> Denied:
    message, kind = REASONS[reason]
    return Denied(reason, message, kind)


def _task(tx: Tx, task_id: UUID, contact_id: UUID | None = None) -> dict[str, Any]:
    """行と、かける番号（contact_id・phone_e164）。番号は、渡されればそれ、無ければいまの予定の番号
    （予定が無い・終えた行は一覧の番号。どちらにしても門は通さない）。行の番号の表にある番号だけ。"""
    row = tx.one(
        "SELECT t.task_id, t.tenant_id, t.client_id, t.run_id, t.client_ref, t.target_name, t.target_role, r.business_id,"
        "       n.contact_id, n.phone_e164"
        " FROM salescall.tasks t"
        " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id"
        " JOIN salescall.task_numbers n ON n.task_id = t.task_id AND n.contact_id = coalesce(%s,"
        "   (SELECT x.contact_id FROM salescall.task_next_actions x WHERE x.task_id = t.task_id ORDER BY x.seq DESC LIMIT 1),"
        "   t.contact_id)"
        " WHERE t.task_id = %s", (contact_id, task_id))
    if row is None:
        raise TaskNotFound(str(task_id))
    return row


def _hours_settings(tx: Tx, scope: dict[str, Any]) -> tuple[dict, dict]:
    """時間帯の設定を読む1か所（かけてよい時間帯 calling.hours〔枠の内側〕と、かけない時間＝NG時間 calling.ng_hours）。
    ほかで直接読まない（NG時間の引き忘れが起きる。機械が見張る）。"""
    return settings.resolve(tx, "calling.hours", **scope), settings.resolve(tx, "calling.ng_hours", **scope)


def calling_hours_for(tx: Tx, *, tenant_id: UUID, client_id: UUID, business_id: UUID | None,
                      callee_time: bool = False) -> dict:
    """かけてよい時間帯（曜日・祝日ごと）。ふつうは、かけてよい時間帯から NG時間を引いた残り（次の始まりを探すのに使う）。
    callee_time＝お客様が自分で指定した時刻を確かめるとき（会話の側の確かめなど）：NG時間を引かない
    （2026-10-15 ユーザー：「NG時間は通常は控えるが、相手が自分で指定した時刻なら、かけてよい」）。"""
    hours, ng = _hours_settings(tx, dict(tenant_id=tenant_id, client_id=client_id, business_id=business_id))
    return hours if callee_time else calling_hours.subtract(hours, ng)


def _setting(tx: Tx, key: str, task: dict[str, Any]) -> Any:
    return settings.resolve(tx, key, tenant_id=task["tenant_id"], client_id=task["client_id"],
                            business_id=task["business_id"])


def _pacing(tx: Tx, task: dict[str, Any], now: datetime, assume_promised: bool | None = None) -> Pacing:
    # 「かけた回数」の数え方は1つ（行の回数の上限・1日の回数・最小の間隔のどれも）：お客様の意思と関係なく繋がらなかった・
    # 切れた発信は数えない。お客様と約束したかけ直しは、どの上限でも止めない（2026-10-14 ユーザー：「ほぼ同じことでしょ。統一」）
    counted = tx.one(f"SELECT count(*) AS n FROM salescall.attempts WHERE task_id = %s AND {_COUNTED}",
                     (task["task_id"],))["n"]
    remaining = max(0, int(_setting(tx, "retry.max_attempts_per_task", task)) - counted)
    if assume_promised is None:
        nxt = tx.one("SELECT promised FROM salescall.task_next_actions WHERE task_id = %s ORDER BY seq DESC LIMIT 1",
                     (task["task_id"],))
        promised = bool(nxt and nxt["promised"])
    else:
        promised = assume_promised
    # 同じお客様（止める範囲が重なる行）の発信で、断られたかがまだ決まっていないものがあれば、決まるまで待つ（dnc の1か所を読む）
    if dnc.undecided_reach_overlaps(tx, task["task_id"]):
        return Pacing(remaining, None, "related_unsettled", promised)
    same = (task["contact_id"], task["task_id"])   # 同じお客様＝かける番号が同じ発信、または同じ行の発信
    limits: list[tuple[datetime, str]] = []
    if not promised:
        last = tx.one(f"SELECT max(ended_at) AS at FROM salescall.attempts WHERE {_SAME} AND {_COUNTED}", same)["at"]
        if last is not None:
            limits.append((last + timedelta(seconds=int(_setting(tx, "gate.same_contact_min_interval", task))),
                           "same_contact_interval"))
        today = tx.one(
            f"SELECT count(*) AS n, salescall.jst_day_start(salescall.jst(now())::date + 1) AS tomorrow"
            f" FROM salescall.attempts WHERE {_SAME} AND {_COUNTED}"
            f"  AND salescall.jst(claimed_at)::date = salescall.jst(now())::date", same)
        if today["n"] >= int(_setting(tx, "gate.max_dials_per_contact_per_day", task)):
            limits.append((today["tomorrow"], "daily_attempts"))
    # 結果不明のあとの長い間隔は、お客様が出たあと（話していたかもしれない）の結果不明だけ（お客様が出る前なら、誰とも話していない）
    unknown = tx.one(f"SELECT max(ended_at) AS at FROM salescall.attempts WHERE {_SAME} AND state = 'technical_error'"
                     " AND salescall.reached_answer(attempt_id)", same)["at"]
    if unknown is not None:
        limits.append((unknown + timedelta(seconds=int(_setting(tx, "gate.unknown_result_min_interval", task))),
                       "unknown_result_interval"))
    # 同じ番号へ、ほかの依頼者がかけたとき：同じ契約者の中＝システムの間隔／ほかの契約者＝契約者の設定（2026-10-16 ユーザー。
    # 「かけない」は target_policy が答える）
    for r in tx.all("SELECT relation, last_at FROM salescall.number_last_dial_elsewhere(%s)", (task["phone_e164"],)):
        if r["relation"] == "same_tenant":
            gap = timedelta(seconds=int(_setting(tx, "gate.cross_client_interval", task)))
        else:
            v = _setting(tx, "gate.other_contractor_interval", task)
            if v == "never":
                continue
            gap = timedelta(hours=v["hours"]) if "hours" in v else timedelta(days=v["days"])
        limits.append((r["last_at"] + gap, "cross_client_interval_" + r["relation"]))
    binding = [x for x in limits if x[0] > now]
    if not binding:
        return Pacing(remaining, now, None, promised)
    at, reason = max(binding, key=lambda x: x[0])
    return Pacing(remaining, at, reason, promised)


@dataclass(frozen=True)
class PurposeDial:
    at: datetime        # かけた時刻（日本時間。時間帯はこれで決める）
    ended: datetime     # 終わった時刻（日本時間。最小の間隔はここから数える＝門の数え方と同じ）
    counted: bool       # お客様が出られた発信か（上の数え方。回線の側で繋がらなかった・切れたものは False）


def purpose_dials(tx: Tx, task_id: UUID, *, purpose: str) -> list[PurposeDial]:
    """その行で、その目的（番号の聞き直し・お詫び）でかけた発信（発信まで進んだもの）の、かけた時刻と、数える発信か。
    別の時間帯にかけ直す電話の、回数・試した時間帯・最小の間隔を決めるのに使う（next_action._new_band_retry の1か所。DESIGN.md 3-5）。"""
    return [PurposeDial(r["t"], r["e"], r["counted"]) for r in tx.all(
        f"SELECT salescall.jst(claimed_at) AS t, salescall.jst(coalesce(ended_at, claimed_at)) AS e, ({_COUNTED}) AS counted"
        f" FROM salescall.attempts"
        f" WHERE task_id = %s AND purpose = %s AND {_DIALED} ORDER BY claimed_at", (task_id, purpose))]


def pacing(tx: Tx, task_id: UUID, *, contact_id: UUID | None = None, promised: bool | None = None) -> Pacing:
    """その行（タスク）の、残りの回数と、次にかけてよい最も早い時刻（門・次の動き・画面が読む1か所）。
    contact_id：その番号へかける場合で数える（次の動きが、決める前に使う）。None ならいまの予定の番号。
    promised：次の予定を「お客様と約束したかけ直し」にした場合で数える（同上）。None ならいまの予定のまま。"""
    task = _task(tx, task_id, contact_id)
    return _pacing(tx, task, tx.one("SELECT now() AS now")["now"], promised)


def claim(tx: Tx, *, task_id: UUID, line_id: UUID, lease_holder: str) -> Claim | Denied:
    """その行に、その回線で発信してよいかを判定し、よければ予約する（1つの取引）。不許可は記録して返す。"""
    task = _task(tx, task_id)
    tx.one("SELECT salescall.release_expired_claims(%s, %s, %s, %s)",
           (task["task_id"], task["contact_id"], task["phone_e164"], line_id))
    try:
        outcome = _judge_and_claim(tx, task, line_id, lease_holder)
    except settings.SettingMissing:
        outcome = _deny("setting_missing")
    except CalendarOutOfRange:
        outcome = _deny("calendar_out_of_range")
    if isinstance(outcome, Denied):
        tx.execute("INSERT INTO salescall.gate_denials (tenant_id, client_id, task_id, line_id, reason)"
                   " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s)",
                   (task_id, None if outcome.reason == "line_unknown" else line_id, outcome.reason))
    return outcome


def _judge_and_claim(tx: Tx, task: dict[str, Any], line_id: UUID, lease_holder: str) -> Claim | Denied:
    clock = tx.one("SELECT now() AS now, salescall.jst(now()) AS local")
    now, local = clock["now"], clock["local"]

    # (1) 次の予定（Q4 の記録を読むだけ）
    nxt = tx.one("SELECT kind, call_at, decided_at, purpose, wrong_contact_id, salescall.jst(callee_at) AS callee_at"
                 " FROM salescall.task_next_actions WHERE task_id = %s ORDER BY seq DESC LIMIT 1", (task["task_id"],))
    if nxt is None:
        return _deny("no_schedule")
    if nxt["kind"] == "end":
        return _deny("task_ended")
    if nxt["kind"] == "hold":
        return _deny("judge_pending")
    if nxt["call_at"] > now:
        return _deny("not_due")
    if not approval.task_cleared(tx, task["task_id"]):   # 契約者の承認を使う業務：契約者が承認した行だけ（モードは承認の部分が読む）
        return _deny("awaiting_approval")
    # 前の発信の締め（判定の結果の版）が済むまで、次をかけない（予定を誰が書き直しても抜け道にならないよう、
    # 「前の発信に判定の結果があるか」で決める。判定と次の予定は、締めの処理 core/settle.py が1つの取引で書く）
    unsettled = tx.one(
        f"SELECT 1 FROM salescall.attempts a WHERE a.task_id = %s AND {_DIALED_A}"
        " AND NOT salescall.is_settled(a.attempt_id) LIMIT 1",
        (task["task_id"],))
    if unsettled is not None:
        return _deny("awaiting_decision")

    # (2) データで決まる「かけない理由」（番号の種類・発信禁止の番号と管理番号。取込と同じ1か所）
    blocked = target_policy.block_reason(tx, normalize(task["phone_e164"]), client_ref=task["client_ref"],
                                         tenant_id=task["tenant_id"], client_id=task["client_id"],
                                         business_id=task["business_id"])
    if blocked is not None:
        return _deny(blocked[0])

    # (3) 回数と間隔（pacing の1か所）
    pace = _pacing(tx, task, now)
    if pace.remaining <= 0 and not pace.promised:
        return _deny("max_attempts")

    # (4) 業務設定の版と段
    version = tx.one("SELECT salescall.run_current_version(%s) AS v", (task["run_id"],))["v"]
    if version is None:
        return _deny("no_version")
    try:   # 判定できない通話をしない：その版の業務の決まりが揃っていなければ通さない（読むのは判定と同じ1か所）
        business_rules.load(tx, version)
    except (business_rules.RulesMissing, business_rules.RulesInvalid):
        return _deny("rules_missing")
    try:   # 話す中身の無い通話をしない：その版の業務設定が確かめに通らなければ通さない（読むのは会話と同じ1か所）
        content = business_settings.load(tx, version)
    except (LookupError, business_settings.SettingsInvalid):
        return _deny("settings_missing")
    # お詫びの電話（DESIGN.md 10-1-2）は、業務の話し方を使わない（決まった文面だけ）：品質の段の件数に数えず、業務の品質の停止
    # （言ってはいけないこと・品質の基準・判定できない）では止めない。ほかの停止（全体・管理者か契約者が押した緊急停止・費用・回線など）では止める
    apology = nxt["purpose"] == "apology"
    # 目的のお客様を呼ぶ材料が足りない行にかけない（足りるかの決まりは業務設定の1か所。取込・版の割り当てと同じ。お詫びは、
    # 前の通話で話したお客様に伝えるので、業務の呼び方を使わない）
    short = None if apology else content.target_needs().missing(name=task["target_name"], role=task["target_role"])
    if short is not None:
        return _deny(short[0])
    ok, why = (True, None) if apology else quality_stage.allowance(tx, version)
    if not ok:
        return _deny(why)

    # (5) 止まっているか（全体・契約者・依頼者・業務・回線）
    open_now = stops.open_stops(tx, tenant_id=task["tenant_id"], client_id=task["client_id"],
                                business_id=task["business_id"], line_id=line_id)
    if apology:
        open_now = [s for s in open_now if not (s["scope"] == "business" and s["cause"] in quality_stage.STOP_CAUSES)]
    if open_now:
        return _deny("stopped")

    # (6) 時間帯：予約から予約証の期限まで、まるごと内側（5-2-1 の4）。祝日の暦の外は判断できない（5-2-1 の5）
    lifetime = timedelta(seconds=int(_setting(tx, "gate.claim_lifetime", task)))
    hours, ng = _hours_settings(tx, dict(tenant_id=task["tenant_id"], client_id=task["client_id"],
                                         business_id=task["business_id"]))
    if not calling_hours.allowed(hours, ng, holiday_checker(tx), local, local + lifetime, callee_at=nxt["callee_at"]):
        return _deny("outside_hours")

    # (7) 間隔の待ち
    if pace.earliest is None:
        return _deny(pace.binding)
    if pace.binding is not None:
        return _deny(pace.binding)

    # (8) 回線：借りている本人で、借用の期限が予約証の期限より先。回線ごとの1日の上限（Intent.md 8-6）
    line = tx.one("SELECT lease_holder, lease_expires_at FROM salescall.lines WHERE line_id = %s", (line_id,))
    if line is None:
        return _deny("line_unknown")
    if line["lease_holder"] != lease_holder or line["lease_expires_at"] < now + lifetime:
        return _deny("line_not_leased")
    dials = tx.one("SELECT salescall.line_dials_on(%s, %s) AS n", (line_id, local.date()))["n"]
    if dials >= int(_setting(tx, "line.max_dials_per_day", task)):
        return _deny("line_daily_limit")

    # (9) 同時の本数：依頼者ごとの上限。会話AIの1分の上限では待たせない（2026-10-17 ユーザー：「1分制限を超えそうになったら、
    #     きりのいいところで、次のキーと併用して進んだらいい」＝キーの選択 Q15 が次のキーを併せて使う。5-2 の9）
    active = tx.one("SELECT count(*) AS n FROM salescall.attempts WHERE salescall.is_live(state, claim_expires_at)")["n"]
    if active >= int(_setting(tx, "calling.max_concurrent_calls", task)):
        return _deny("client_concurrency")

    # (10) 予約。同じお客様・番号・回線の進行中は、データベースの部分一意が拒む（その部分だけ取り消して理由を返す）
    try:
        with tx.savepoint():
            row = tx.one(
                "INSERT INTO salescall.attempts (tenant_id, client_id, task_id, contact_id, phone_e164, line_id, version_id,"
                "                                state, claimed_at, claim_expires_at, purpose, wrong_contact_id)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, 'claimed', now(), now() + %s,"
                "         %s, %s)"
                " RETURNING attempt_id, claim_expires_at,"
                "   (SELECT phone_e164 FROM salescall.task_numbers WHERE task_id = %s AND contact_id = %s) AS wrong_phone",
                (task["task_id"], task["contact_id"], task["phone_e164"], line_id, version, lifetime,
                 nxt["purpose"], nxt["wrong_contact_id"], task["task_id"], nxt["wrong_contact_id"]))
    except Rejected as e:
        if e.constraint == "attempts_one_active_per_contact":
            return _deny("contact_active")
        if e.constraint == "attempts_one_active_per_task":
            return _deny("task_active")
        if e.constraint == "attempts_one_active_per_line":
            return _deny("line_busy")
        if e.constraint == "attempts_one_active_per_number":
            relation = tx.one("SELECT salescall.number_active_relation(%s) AS r", (task["phone_e164"],))["r"]
            return _deny("number_active_" + (relation or "other_tenant"))
        raise
    return Claim(row["attempt_id"], task["task_id"], line_id, task["phone_e164"], version, row["claim_expires_at"],
                 nxt["purpose"], row["wrong_phone"])
