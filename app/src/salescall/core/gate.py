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
from typing import Any, NamedTuple
from uuid import UUID

from salescall.core import (approval, attempts, business_rules, business_settings, calling_hours, dnc, notify, quality_stage,
                            recordings, settings, stops, target_policy)
from salescall.core.access import Rejected, Tx
from salescall.core.calendar import CalendarOutOfRange, holiday_checker
from salescall.core.phone import normalize
from salescall.core.tasks import TaskNotFound

# 不許可の理由（名前 → 日本語・種類・扱い）。名前の一覧の正本はここだけ（画面・次の動き・回線ごとの処理・候補を出す処理は、これを読む）。
# 扱い（HANDLINGS）は、断られた行の予定をどうするかの1か所（⛔ 見直し17a：前は理由ごとの集まりを別々に書き、どれにも入らない理由は
# 次の動きが予定を「決まった間（5分）のあと」へ書き直し、待つあいだ記録が1行あたり1日288ずつ増え続けた）：
#   plan＝次の動き（next_action.after_denial）が予定を決める。終える・分かっている時刻（次の時間帯の始まり・間隔が空く時刻・約束の幅）
#        へ1回書く・いまの予定に任せる（何も書かない）のどれか。決まった間のあとへ書き直し続けることは無い
#   plan_if_known＝待つ時刻が分かれば plan と同じ。分からない（ほかの契約者の約束・時刻を過ぎたのにまだかかっていない約束）あいだは
#        候補に出さない（waits_unknown）
#   unoffered＝候補を出す処理が出さない（いつ変わるか分からない。変われば、また出る。決まりは門と同じ関数＝waits_unknown・
#        候補のデータベースの条件）
#   line＝その回線を休ませる（ほかの回線は、その行をかけられる）／client＝その依頼者の行を、この回だけ飛ばす／business＝その業務の
#        行を、この回だけ飛ばす／run＝その実施回の行を、この回だけ飛ばす（版の理由・暦・設定の値＝実施回のどの行も同じ答え）／
#        stage＝品質の段の理由：決まった文面だけの電話のほかを、この回だけ飛ばす／task＝その行を、この回だけ飛ばす（進行中の発信がある）
#   plan・plan_if_known のほかは、行の予定を書き直さない（PLAN_KEPT）。
HANDLINGS = ("plan", "plan_if_known", "unoffered", "line", "client", "business", "run", "stage", "task")


class Reason(NamedTuple):
    message: str
    kind: str        # permanent＝待っても変わらない／wait＝待てば変わる／cannot_judge＝判断の材料が無い
    handling: str    # HANDLINGS のどれか


REASONS: dict[str, Reason] = {
    "no_schedule": Reason("次の予定がありません", "cannot_judge", "plan"),
    "task_ended": Reason("この行は処理済みです", "permanent", "plan"),
    "judge_pending": Reason("前の通話が判定できず、判定し直しを待っています", "wait", "plan"),
    "not_due": Reason("次の予定の時刻より前です", "wait", "plan"),
    "awaiting_decision": Reason("前の発信の判定と次の予定が、まだ済んでいません", "wait", "plan"),
    "awaiting_approval": Reason("契約者の承認を使う業務で、この行がまだ承認されていません", "wait", "unoffered"),
    "number_unknown": Reason("番号が不明です", "permanent", "plan"),
    "number_kind_forbidden": Reason("かけない種類の番号です", "permanent", "plan"),
    "number_kind_not_allowed": Reason("この業務の設定でかけない種類の番号です", "permanent", "plan"),
    "number_unusable": Reason("この番号は、お客様のところで電話が鳴らない電話が続きました（番号を受け付けない・お客様が出る前に終わる、が決まった数だけ続いた）", "permanent", "plan"),
    "number_unconfirmed": Reason("この番号は、お客様のところで鳴らない電話が決まった数だけ続きましたが、番号のせいか回線のせいかが、まだ分かりません（その回線がほかの番号へかけると決まります）", "wait", "unoffered"),
    "dnc_client": Reason("発信禁止です", "permanent", "plan"),
    "dnc_contractor": Reason("契約者の共通の発信禁止です", "permanent", "plan"),
    "other_contractor_called": Reason("ほかの契約者がかけた番号です（契約者の設定で、かけない）", "permanent", "plan"),
    "other_client_promise_never": Reason("同じ番号へ、ほかの約束の電話がかかります（契約者の設定で、この行はかけない）", "permanent", "plan"),
    "dnc_client_ref": Reason("この管理番号のお客様は発信禁止です", "permanent", "plan"),
    "max_attempts": Reason("この行に発信してよい回数を使い切りました", "permanent", "plan"),
    "no_version": Reason("実施回に業務設定の版が割り当てられていません", "cannot_judge", "run"),
    "rules_missing": Reason("この版の業務の決まり（断りの答え・理由×扱い・再発信の方針）がありません", "cannot_judge", "run"),
    "settings_missing": Reason("この版の業務設定（名乗り・質問・目的のお客様など）が無いか、確かめに通りません", "cannot_judge", "run"),
    "target_name_missing": Reason("この業務は目的のお客様を名前で呼びますが、この行に目的のお客様の名前がありません", "cannot_judge", "unoffered"),
    "target_role_missing": Reason("この業務は目的のお客様を役職で呼びますが、この行に目的のお客様の役職がありません", "cannot_judge", "unoffered"),
    "version_not_ready": Reason("この版は、まだかけられる段に入っていません（模擬通話・最初の段の前）", "wait", "stage"),
    "stage_full": Reason("いまの段の件数に達しました（次の段へ進むのを待っています）", "wait", "stage"),
    "stopped": Reason("止まっています", "wait", "unoffered"),
    "line_stopped": Reason("この回線が止まっています（回線の異常など。行は、ほかの回線でかけられる）", "wait", "line"),   # 回線の状態
    "client_inactive": Reason("この依頼者は無効にしています", "wait", "client"),
    "line_retired": Reason("使わなくなった回線です", "wait", "line"),   # 回線の状態（行は、ほかの回線でかけられる）
    "outside_hours": Reason("かけてよい時間帯の外です", "wait", "plan"),
    "callback_window": Reason("お客様が言ったかけ直しの時間（「夕方」「3時まで」など）の外です", "wait", "plan"),
    "calendar_out_of_range": Reason("祝日の暦が無い日です", "cannot_judge", "run"),
    "related_unsettled": Reason("このお客様にかかわる前の発信の締め（断られたかの確かめ）が、まだ済んでいません", "wait", "unoffered"),
    "same_contact_interval": Reason("同じお客様への前の発信から、間隔が空いていません", "wait", "plan"),
    "daily_attempts": Reason("同じお客様へ、今日かけてよい回数を使い切りました", "wait", "plan"),
    "cross_client_interval_same_tenant": Reason("ほかの依頼者が同じ番号へかけたばかりです", "wait", "plan"),
    "cross_client_interval_other_tenant": Reason("同じ番号へかけたばかりです", "wait", "plan"),   # 契約者の外は、中身を出さない
    "other_client_promise": Reason("同じ番号へ、ほかの約束の電話がかかります（契約者の設定で、その電話が済むまで待ちます）", "wait", "plan_if_known"),
    "number_promise_soon": Reason("同じ番号へ、お客様が時刻を指定した約束の電話が、もうすぐかかります（その電話が済むまで待ちます）", "wait", "plan_if_known"),
    "line_unknown": Reason("その回線はありません", "cannot_judge", "line"),
    "line_not_leased": Reason("回線を借りていません（または、借用の期限が予約証より先に切れます）", "wait", "line"),
    "line_daily_limit": Reason("この回線の、今日の発信の上限に達しました", "wait", "line"),
    "promise_soon": Reason("お客様が時刻を指定した約束が近いので、この回線では、この業務の電話（平均の長さでは約束までに終わらない）を始めません", "wait", "business"),
    "excluded": Reason("「かけない」か「強制かけない」にしている行です（依頼者・契約者の都合。ダッシュボードで戻せます）", "wait", "unoffered"),
    "business_daily_limit": Reason("この業務の、今日の発信の上限に達しました（業務ごとの設定）", "wait", "business"),
    "line_other_client": Reason("この回線は、ほかの依頼者に割り当てた発信番号です", "wait", "line"),
    "line_used_by_other_client": Reason("この発信番号で、ほかの依頼者がこの番号へかけたことがあります（依頼者が替わったら発信番号を替える）",
                                  "wait", "unoffered"),
    "line_busy": Reason("回線が使用中です", "wait", "line"),
    "voice_not_ready": Reason("この業務の、承認した録音のある声がありません", "wait", "run"),
    "voice_used_by_other_client": Reason("ほかの依頼者がこの番号へ同じ声でかけています（別の声の録音がありません）", "wait", "unoffered"),
    "client_concurrency": Reason("この依頼者の同時の本数の上限です", "wait", "client"),
    "contact_active": Reason("同じ番号のお客様（この依頼者の別の行を含む）に、進行中の発信があります", "wait", "task"),
    "task_active": Reason("この行（ほかの番号を含む）に、進行中の発信があります", "wait", "task"),
    "number_active_same_client": Reason("同じ番号に、進行中の発信があります", "wait", "task"),
    "number_active_same_tenant": Reason("同じ番号に、ほかの依頼者の進行中の発信があります", "wait", "task"),
    "number_active_other_tenant": Reason("同じ番号に、進行中の発信があります", "wait", "task"),   # 契約者の外は、中身を出さない
    "setting_missing": Reason("設定の値が決まっていません", "cannot_judge", "run"),
}

# 同じお客様への最小の間隔に数える発信：お客様に届いた1回（お客様の意思と関係なく繋がらなかった・切れたもの、発信に失敗は数えない。
# 2026-10-14 ユーザー決定。決まりはデータベースの1か所 salescall.dial_kind＝0116）
_COUNTED = attempts.reached()
# 行の回数の上限・1日の回数に数える発信：上に加えて、お客様と約束したかけ直しは数えない（2026-10-14 ユーザー：「かけ直しの約束を
# 相手側にした場合やかけ直しを依頼された場合は対象外」。2026-10-17 の計画書との突き合わせ：前は約束したかけ直しを止めないだけで、
# かけたあとは回数に数えていたので、そのあとのふつうのかけ直しが減った）。最小の間隔は、約束したかけ直しも起点に数える（歯止め）
_COUNTED_TIMES = attempts.counts_as_try()   # 決まりはデータベースの1か所（0116。次の動きの再発信の方針の回数も同じものを読む）
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
    purpose: str                 # 発信の目的（normal / ask_number_again / apology / resume＝途中で切れた通話のかけ直し。会話の処理が
    #                              読んで話す。DESIGN.md 3-5・10-1-2・7-2）
    wrong_phone_e164: str | None   # 番号の聞き直しのとき：番号違いだった番号（「お聞きした番号が…」と告げる）
    voice_type: str              # この通話で話す声の型（`choose_voice` の1か所で決めた。会話の処理は、この声の型で話す）
    apology: tuple[str, ...] = ()  # お客様につながったら、まずお詫びすること（理由。次の予定の apology_reasons を写す。0113）：
    #                                missed_promise＝約束した日（回）に一度もかけられなかった（2026-10-19 ユーザー：「お約束の日に
    #                                かけることができなかったことを深くお詫びする必要」）／cut_off＝前の電話が途中で切れた（2026-10-17
    #                                ユーザー：「復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」）。目的のお客様と話せた
    #                                締めで外れる＝core/next_action の _apologised。会話の処理（CL-1）は、これを読んで最初にお詫びする
    promised: bool = False         # お客様と約束したかけ直しの電話か（試行の印 promised と同じ）。会話の処理は、約束の電話で受付の方に
    #                                事情で取り次げないと言われたら、締めの前に「後ほど改めてお掛けしてもよろしいでしょうか」と聞く
    #                                （答えで、当日の約束の幅の中でかけ直すかが決まる＝core/next_action。2026-10-22 ユーザー）
    successor: bool = False        # 目的のお客様の代わりに、後任のご担当者様をお呼びする（前の電話で、お呼びした方が在籍しておらず、
    #                                業務の応じ方が「後任の方をお呼びする」だった。次の予定の印 successor を写す。0114。2026-10-22
    #                                ユーザー：「基本は後任へつないでもらう」）。会話の処理（CL-1）は、これを読んで後任の方をお呼びする
    number_again: str | None = None  # 番号の聞き直しのとき：なぜ伺い直すか（wrong_party＝お客様が言った番号が別の方のお電話だった／
    #                                unreachable＝繋がらなかった。次の予定の印 number_again を写す。0130。CL-1 の連絡帳 4-41 の (3)）。
    #                                会話の処理は、理由ごとの一言（number_again_wrong_party・number_again_unreachable）で番号を伺う


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
    known: bool = True     # earliest が、待てば変わると分かっている時刻か（偽＝ほかの契約者の約束で時刻が分からない・約束の時刻を
    #                        過ぎたのにまだかかっていない：決まった間のあとを仮に置いている。候補を出す処理が出さない＝waits_unknown）


def denied(reason: str) -> Denied:
    """その理由の不許可（日本語と種類は REASONS の1か所から）。"""
    r = REASONS[reason]
    return Denied(reason, r.message, r.kind)


_deny = denied


def _task(tx: Tx, task_id: UUID, contact_id: UUID | None = None) -> dict[str, Any]:
    """行と、かける番号（contact_id・phone_e164）。番号は、渡されればそれ、無ければいまの予定の番号
    （予定が無い・終えた行は一覧の番号。どちらにしても門は通さない）。行の番号の表にある番号だけ。"""
    row = tx.one(
        "SELECT t.task_id, t.tenant_id, t.client_id, t.run_id, t.client_ref, t.target_name, t.target_role, r.business_id,"
        "       n.contact_id, n.phone_e164"
        " FROM salescall.tasks t"
        " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id"
        " JOIN salescall.task_numbers n ON n.task_id = t.task_id AND n.contact_id = coalesce(%s,"
        "   (SELECT x.contact_id FROM salescall.current_next_actions x WHERE x.task_id = t.task_id),"
        "   t.contact_id)"
        " WHERE t.task_id = %s", (contact_id, task_id))
    if row is None:
        raise TaskNotFound(str(task_id))
    return row


def _hours_settings(tx: Tx, scope: dict[str, Any]) -> tuple[dict, dict]:
    """時間帯の設定を読む1か所（かけてよい時間帯 calling.hours〔枠の内側〕と、かけない時間＝NG時間 calling.ng_hours）。
    ほかで直接読まない（NG時間の引き忘れが起きる。機械が見張る）。"""
    return settings.resolve(tx, "calling.hours", **scope), settings.resolve(tx, "calling.ng_hours", **scope)


def next_opening(tx: Tx, after: datetime, *, tenant_id: UUID, client_id: UUID, business_id: UUID | None) -> datetime | None:
    """after 以降で、ふつうの電話をかけてよい時間帯（`calling_hours_for`）に、予約から予約証の期限までがまるごと入る、最も早い始まり
    （1か所。門の「今日かけてよい回数を使い切った」の待ちと、次の動きの「時間帯の外」が読む。⛔ 見直し17a：前は1日の回数の待ちを
    次の日の0時にしたので、0時に門へ渡し、時間帯の外で断られて、次の動きがもう一度書き直した）。31日のうちに無ければ None。
    暦の無い日に当たれば、決めつけず、after 以降の最初の日の始まり（その日に門がもう一度確かめる。暦が無ければ門は実施回の理由で
    断り、予定は書き直さない＝1日に1回まで）。"""
    lifetime = timedelta(seconds=int(settings.resolve(tx, "gate.claim_lifetime", tenant_id=tenant_id, client_id=client_id,
                                                      business_id=business_id)))
    local = calling_hours.local(after)
    try:
        found = calling_hours.next_opening(calling_hours_for(tx, tenant_id=tenant_id, client_id=client_id,
                                                             business_id=business_id), holiday_checker(tx), local, lifetime)
    except CalendarOutOfRange:
        day = datetime.combine(local.date(), datetime.min.time())
        found = day if day == local else day + timedelta(days=1)
    return None if found is None else calling_hours.from_local(found)


def calling_hours_for(tx: Tx, *, tenant_id: UUID, client_id: UUID, business_id: UUID | None) -> dict:
    """かけてよい時間帯（曜日・祝日ごと）：かけてよい時間帯から NG時間を引いた残り（次の始まりを探すのに使う）。お客様が言った
    幅の約束の電話は `window_hours`（依頼者の時間帯・NG時間の外でもかける）。"""
    hours, ng = _hours_settings(tx, dict(tenant_id=tenant_id, client_id=client_id, business_id=business_id))
    return calling_hours.subtract(hours, ng)


def window_hours(tx: Tx, *, tenant_id: UUID, client_id: UUID, business_id: UUID | None,
                 window: calling_hours.CalleeWindow) -> dict:
    """お客様が言った幅の約束でかける電話の、かけてよい時間帯（`calling_hours.callee_hours` の1か所＝門の時刻の確かめと同じ決まり。
    次の動きが幅の中を探すのに使う）。依頼者の深夜早朝の歯止め（設定 calling.hours_limit。日だけの幅で依頼者の時間帯が1日も無い
    とき）と、お客様指定の歯止め時間（設定 callee.designated_limit。業務ごと。お客様が言った時間の幅の端を埋めて切る）も読む。"""
    scope = dict(tenant_id=tenant_id, client_id=client_id, business_id=business_id)
    hours, ng = _hours_settings(tx, scope)
    return calling_hours.callee_hours(hours, ng, settings.resolve(tx, "calling.hours_limit", **scope),
                                      callee_settings(tx, **scope).designated_limit, window)


def callee_settings(tx: Tx, *, tenant_id: UUID, client_id: UUID, business_id: UUID | None) -> calling_hours.CalleeSettings:
    """お客様の時間の設定（営業時間・お客様指定の歯止め時間・1回分の長さ・「〇時まで」の理由ごとの決め方）を読む1か所（門・締め＝
    判定・次の動きが使う）。"""
    get = lambda key: settings.resolve(tx, key, tenant_id=tenant_id, client_id=client_id, business_id=business_id)   # noqa: E731
    sec = lambda pair: (calling_hours.clock_seconds(pair[0]), calling_hours.clock_seconds(pair[1]))   # noqa: E731
    return calling_hours.CalleeSettings(sec(get("callee.business_hours")), sec(get("callee.designated_limit")),
                                        int(get("callback.exact_slot")),
                                        tuple((why, get(key)) for why, key in calling_hours.UNTIL_SETTINGS.items()))


def _setting(tx: Tx, key: str, task: dict[str, Any]) -> Any:
    return settings.resolve(tx, key, tenant_id=task["tenant_id"], client_id=task["client_id"],
                            business_id=task["business_id"])


CALL_LENGTH_DAYS = 30   # 業務の平均通話時間を測る期間（直近の日数）


def promise_guard_seconds(tx: Tx, task: dict[str, Any]) -> int:
    """時刻を指定された約束の前に回線を空ける長さ（秒。1か所）：これから始める電話（時刻を指定された約束の電話のほかすべて）の
    業務の平均通話時間（お客様が出て、ふつうに終えた・お客様が切った通話の、予約から終わりまで。直近 CALL_LENGTH_DAYS 日）。数が設定 gate.exact_promise_guard_samples より少ないうちは、設定
    gate.exact_promise_guard（初め5分）。2026-10-19 ユーザー：「業務ごとに平均通話時間を測って、それを利用するといいでしょう。
    30分かかる業務と1分で終わる業務では、同じにできないからです」。"""
    got = tx.one("SELECT seconds, calls FROM salescall.business_call_seconds(%s, %s)", (task["business_id"], CALL_LENGTH_DAYS))
    if got["calls"] > 0 and got["calls"] >= int(_setting(tx, "gate.exact_promise_guard_samples", task)):   # 0本なら初めの値
        return got["seconds"]
    return int(_setting(tx, "gate.exact_promise_guard", task))


def _pacing(tx: Tx, task: dict[str, Any], now: datetime, assume_promised: bool | None = None) -> Pacing:
    # 「かけた回数」の数え方は1つ（行の回数の上限・1日の回数・最小の間隔のどれも）：お客様の意思と関係なく繋がらなかった・
    # 切れた発信は数えない。お客様と約束したかけ直しは、どの上限でも止めない（2026-10-14 ユーザー：「ほぼ同じことでしょ。統一」）
    counted = tx.one(f"SELECT count(*) AS n FROM salescall.attempts WHERE task_id = %s AND {_COUNTED_TIMES}",
                     (task["task_id"],))["n"]
    remaining = max(0, int(_setting(tx, "retry.max_attempts_per_task", task)) - counted)
    if assume_promised is None:
        nxt = tx.one("SELECT promised FROM salescall.current_next_actions WHERE task_id = %s", (task["task_id"],))
        promised = bool(nxt and nxt["promised"])
    else:
        promised = assume_promised
    # 同じお客様（止める範囲が重なる行）の発信で、断られたかがまだ決まっていないものがあれば、決まるまで待つ（dnc の1か所を読む）
    if dnc.undecided_reach_overlaps(tx, task["task_id"]):
        return Pacing(remaining, None, "related_unsettled", promised)
    same = (task["contact_id"], task["task_id"])   # 同じお客様＝かける番号が同じ発信、または同じ行の発信
    limits: list[tuple[datetime, str, bool]] = []   # 待つ時刻・理由・その時刻が分かっているか（Pacing.known）
    if not promised:
        last = tx.one(f"SELECT max(ended_at) AS at FROM salescall.attempts WHERE {_SAME} AND {_COUNTED}", same)["at"]
        if last is not None:
            limits.append((last + timedelta(seconds=int(_setting(tx, "gate.same_contact_min_interval", task))),
                           "same_contact_interval", True))
        today = tx.one(
            f"SELECT count(*) AS n, salescall.jst_day_start(salescall.jst(now())::date + 1) AS tomorrow"
            f" FROM salescall.attempts WHERE {_SAME} AND {_COUNTED_TIMES}"
            f"  AND salescall.jst(claimed_at)::date = salescall.jst(now())::date", same)
        if today["n"] >= int(_setting(tx, "gate.max_dials_per_contact_per_day", task)):
            # 次の日の、かけてよい時間帯の始まり（`next_opening` の1か所。次の日の0時にしない）
            limits.append((next_opening(tx, today["tomorrow"], tenant_id=task["tenant_id"], client_id=task["client_id"],
                                        business_id=task["business_id"]) or today["tomorrow"], "daily_attempts", True))
    # こちらの不具合で切れた通話（結果不明）は、回線が途中で切れた通話と同じ（数えない＝数え方の1か所 salescall.dial_kind の
    # line_failure・cut_off）。前は、お客様が出たあとなら1時間あけていた（2026-10-17 ユーザー：「たどり着いていない場合は、その理由で違うよね」＝次は理由ごとの再発信の
    # 方針で決める。断っていたかもしれないことは、通話後の確かめが受け持つ）
    # 同じ番号へ、ほかの依頼者がかけたとき：同じ契約者の中＝システムの間隔／ほかの契約者＝契約者の設定（2026-10-16 ユーザー。
    # 「かけない」は target_policy が答える）。お客様と約束したかけ直し・途中で切れたあとのかけ直し（promised）は止めない（お客様が
    # 頼んだ時刻を守る。⛔ 2026-10-22 の見直し：前は、ほかの依頼者がかけたあとだと、約束の「15時」を止めて約3日後にずらした）
    #  間隔の計算はデータベースの中（ほかの契約者の発信の時刻は出さない。待つ時刻は1時間単位に切り上げて返る＝0116）
    if not promised:
        v = _setting(tx, "gate.other_contractor_interval", task)
        other_gap = None if v == "never" else timedelta(hours=v["hours"]) if "hours" in v else timedelta(days=v["days"])
        for r in tx.all("SELECT relation, wait_until FROM salescall.number_dial_wait_elsewhere(%s, %s, %s)",
                        (task["phone_e164"], timedelta(seconds=int(_setting(tx, "gate.cross_client_interval", task))),
                         other_gap)):
            limits.append((r["wait_until"], "cross_client_interval_" + r["relation"], True))
    # 同じ番号へ、お客様が時刻を指定した約束の電話（同じ契約者のほかの行。同じ依頼者・ほかの依頼者とも）が、約束の前に回線を空ける
    # 長さ（promise_guard_seconds＝この業務の平均通話時間）のうちに来るなら、約束でない電話は待つ（約束の電話より先に、同じお客様へ
    # ほかの電話が届かない。⛔ 見直し15：前は回線の取り置きだけで、同じ番号へのふつうの電話は約束の直前でもかかった）。約束の時刻を
    # 過ぎても、まだかけていなければ待つ（決まった間 gate.denied_retry_wait で見直す）。ほかの契約者の約束は、下の 0111 の形
    if not promised:
        soon = tx.one("SELECT salescall.number_promise_soon(%s) AS at", (task["phone_e164"],))["at"]   # 約束でない電話の行は入らない
        if soon is not None and soon <= now + timedelta(seconds=promise_guard_seconds(tx, task)):   # 約束が無ければ測らない
            limits.append((soon if soon > now else now + timedelta(seconds=int(_setting(tx, "gate.denied_retry_wait", task))),
                           "number_promise_soon", soon > now))
    # ほかの依頼者がお客様と約束している番号（約束がまだ済んでいない）：基本は声を替えてかける（choose_voice）。契約者が選べば、
    # 約束でない電話は、待つ（wait）か、かけずにその行を終える（never）。決めるのはここ1か所（2026-10-22 ユーザー：「基本は声を
    # 変え、契約者ごとに設定で選べるといいですね」「『同じ番号には送らない』も選べた方がいい」）。約束の時刻を過ぎても、まだかけて
    # いなければ待つ。ほかの契約者の約束は時刻が分からないので、決まった間（gate.denied_retry_wait）で見直す
    choice, waits = _other_promises(tx, task, promised)
    for w in waits:
        again = now + timedelta(seconds=int(_setting(tx, "gate.denied_retry_wait", task)))
        until = again if w is None or w <= now else w
        if choice == "never":   # かけない：ほかの間隔より先に答える（待つ理由に隠れて、行が待ち続けない）
            return Pacing(remaining, until, "other_client_promise_never", promised)
        limits.append((until, "other_client_promise", w is not None and w > now))
    binding = [x for x in limits if x[0] > now]
    if not binding:
        return Pacing(remaining, now, None, promised)
    at, reason, known = max(binding, key=lambda x: x[0])
    return Pacing(remaining, at, reason, promised, known)


def _other_promises(tx: Tx, task: dict[str, Any], promised: bool) -> tuple[str, list[datetime | None]]:
    """ほかの依頼者がお客様と約束している番号か（1か所）：契約者の設定（約束でない電話を、声を替えてかける／待つ／かけない）と、
    その約束の、待つ時刻の並び（None＝ほかの契約者の約束で、時刻が分からない）。約束の電話は、声を替えてかける（待たない）。"""
    choice = "call_other_voice" if promised else _setting(tx, "gate.before_other_promise", task)
    if choice == "call_other_voice":
        return choice, []
    return choice, [r["wait_until"] for r in tx.all(
        "SELECT wait_until FROM salescall.number_next_promise_elsewhere(%s)", (task["phone_e164"],))]


def _content(tx: Tx, version: UUID) -> Any:
    """その版の業務設定（確かめに通らなければ None＝門は settings_missing で断る。読むのは会話と同じ1か所）。"""
    try:
        content = business_settings.load(tx, version)
    except (LookupError, business_settings.SettingsInvalid):
        return None
    return content if content.voice in business_settings.voice_types(tx) else None


def _target_short(tx: Tx, task: dict[str, Any], keeping: str, content: Any) -> str | None:
    """目的のお客様を呼ぶ材料が足りないか（足りなければ理由の名前。門と候補を出す処理の1か所）。業務の話し方を使わない電話（決まった
    文面だけ＝お詫びの電話。どれがそれに当たるかはデータベースの salescall.fixed_text_kind の1か所）は見ない（前の通話で話した
    お客様に、決まった文面を伝える）。足りるかの決まりは業務設定の1か所（取込・版の割り当てと同じ）。"""
    if tx.one("SELECT salescall.fixed_text_kind(%s) AS p", (keeping,))["p"]:
        return None
    short = content.target_needs().missing(name=task["target_name"], role=task["target_role"])
    return None if short is None else short[0]


def _line_taken(tx: Tx, task: dict[str, Any], line_id: UUID, today: Any) -> bool:
    """その回線の発信番号で、ほかの依頼者がこの番号へかけたことがあるか（あれば、この回線ではかけない。1か所＝門と候補を出す処理）。
    依頼者が替わったら、期間に関係なく発信番号を替える（2026-10-17 ユーザー：「依頼者が違うと、期間に関係なく、声と発信番号は変えた
    方がいい」「依頼者ごとに発信番号があることがデフォルト。ただし、検証時は発信番号は1つでOK」＝検証モードの業務は見ない）。替えられる
    回線が無いときは、ほかの回線が空くのを待つ（その依頼者の専用の回線がまだ無いときだけ、契約者に知らせる。専用の回線が使用中で共有の
    回線が回ってきたときは知らせない。設定の抜けは注意だけ）。⛔ 見直し17a：前は門だけが見て、共有の回線が回るたびに断り、次の動きが
    予定を5分ずつ先へ書き直し続けた（その回線では、いつまで待っても変わらない）。"""
    if approval.human_for_business(tx, task["business_id"]) or not tx.one(
            "SELECT salescall.line_called_number_elsewhere(%s, %s) AS hit", (line_id, task["phone_e164"]))["hit"]:
        return False
    if tx.one("SELECT 1 FROM salescall.lines WHERE client_id = %s LIMIT 1", (task["client_id"],)) is None:
        notify.emit(tx, "setting_missing", key=f"own_line:{task['client_id']}:{today}",
                    summary=OWN_LINE_MISSING, refs={"client_id": task["client_id"], "line_id": line_id})
    return True


def waits_unknown(tx: Tx, task_id: UUID, line_id: UUID | None = None) -> str | None:
    """門が必ず断り、いつまで待てば変わるか分からない行の理由（無ければ None。1か所）：候補を出す処理（scheduler.due_tasks）が、
    門に渡さずに飛ばす（門に渡すと、次の動きが決まった間ごとに予定を書き直し、記録が増え続けた＝⛔ 見直し16・17a。待つ時刻が分かる
    理由は、次の動きがその時刻へ1回書くので、ここには入れない）。見るのは、理由の表の扱いが unoffered・plan_if_known の理由のうち、
    候補のデータベースの条件で除けないもの（`UNOFFERED_HERE`）。どれも門と同じ関数を読む：契約者の承認を待つ（approval.task_cleared）・
    同じお客様の発信で、断られたかがまだ決まっていない（dnc.undecided_reach_overlaps）・鳴らない電話が決まった数だけ続いた番号の、
    番号のせいか回線のせいかがまだ分からない（attempts.number_unconfirmed）・目的のお客様を呼ぶ材料が無い（`_target_short`）・
    話してよい別の声が無い（`choose_voice`）・その回線の発信番号で、ほかの依頼者がこの番号へかけた（`_line_taken`。line_id を渡した
    ときだけ。ほかの回線には出す）・待つ時刻が分からない約束（`pacing` の known）。"""
    if not approval.task_cleared(tx, task_id):
        return "awaiting_approval"
    if dnc.undecided_reach_overlaps(tx, task_id):
        return "related_unsettled"
    task = _task(tx, task_id)
    if attempts.number_unconfirmed(tx, task["contact_id"], tenant_id=task["tenant_id"], client_id=task["client_id"],
                                   business_id=task["business_id"]):
        return "number_unconfirmed"
    nxt = tx.one("SELECT promised, salescall.keeping_kind(purpose, promised) AS keeping, now() AS now,"
                 " salescall.jst(now())::date AS today FROM salescall.current_next_actions WHERE task_id = %s", (task_id,))
    if nxt is None:
        return None
    pace = _pacing(tx, task, nxt["now"])
    if pace.binding in PLAN_IF_KNOWN and not pace.known:
        return pace.binding
    version = tx.one("SELECT salescall.run_current_version(%s) AS v", (task["run_id"],))["v"]
    content = None if version is None else _content(tx, version)
    if content is not None:   # 版・業務設定が無ければ、門が実施回の理由で断る（ここでは見ない）
        short = _target_short(tx, task, nxt["keeping"], content)
        if short is not None:
            return short
        voice = choose_voice(tx, task, version, content.voice, nxt["today"])
        if isinstance(voice, Denied) and voice.reason in UNOFFERED_HERE:
            return voice.reason
    if line_id is not None and _line_taken(tx, task, line_id, nxt["today"]):
        return "line_used_by_other_client"
    return None


@dataclass(frozen=True)
class PurposeDial:
    at: datetime        # かけた時刻（日本時間。時間帯はこれで決める）
    ended: datetime     # 終わった時刻（日本時間。最小の間隔はここから数える＝門の数え方と同じ）
    counted: bool = True   # 数える発信か（`purpose_dials` は数える発信だけを返すので、いつも真）


def purpose_dials(tx: Tx, task_id: UUID, *, purpose: str) -> list[PurposeDial]:
    """その行で、その目的（番号の聞き直し・お詫び）でかけた発信のうち、数えるもの（お客様に届いた1回。ふつうの電話の回数と同じ
    決まり＝salescall.dial_kind の1か所。発信に失敗・お客様が出る前の回線の失敗・お客様の意思と関係なく切れた通話は数えない＝
    ⛔ 見直し15：前は発信まで進んだもの全部を回数に数え、ふつうの電話と決まりが違った）の、かけた時刻と終わった時刻。
    別の時間帯にかけ直す電話の、回数・試した時間帯・最小の間隔を決めるのに使う（next_action._new_band_retry の1か所。DESIGN.md 3-5）。"""
    return [PurposeDial(r["t"], r["e"]) for r in tx.all(
        "SELECT salescall.jst(claimed_at) AS t, salescall.jst(coalesce(ended_at, claimed_at)) AS e FROM salescall.attempts"
        f" WHERE task_id = %s AND purpose = %s AND {_COUNTED} ORDER BY claimed_at", (task_id, purpose))]


# 依頼者専用の発信番号が無いときの注意（2026-10-17 ユーザー決定の文。設定する場所は、画面の依頼者タブの「回線（発信番号）」＝CL-3）。
# 注意には短い文だけ。詳しい説明は、登録画面の i マーク（core/help_texts.py の "line.client"。2026-10-17 ユーザー）
OWN_LINE_MISSING = "この依頼者に専用の発信番号がありません。発信番号を依頼者タブの「回線（発信番号）」で設定してください。"


VOICE_NOT_READY = "この業務の録音が、まだ承認されていません。録音を聞いて承認してください。"
VOICE_PARTS_MISSING = ("承認した録音（契約者の単位）に、足りない録音があります（{}）。足りない録音を入れた組を作り、聞いて承認して"
                       "ください（それまで、その声ではかけません）。")
OTHER_VOICE_MISSING = "別の声の録音がありません。ほかの依頼者がかけた番号へは、同じ声ではかけません。別の声の録音を承認してください。"


def choose_voice(tx: Tx, task: dict[str, Any], version_id: UUID, business_voice: str, today: Any) -> str | Denied:
    """この通話で話す声の型（決めるのはここだけ。DESIGN.md 5-2-2 の順10。2026-10-17 ユーザー：声を決める場所は門＝案A）。
    話してよい声は、承認した録音のある声だけ（`recordings.ready_voices` の1か所。事前の録音チェック）。
    (1) この行で前に話した声の型があり、その声が話してよい声なら、それ（同じお客様へのかけ直し・お詫びの電話は同じ声）。
    (2) ほかの依頼者（ほかの契約者を含む）がこの番号へかけてお客様が聞いた声とは、別の声（2026-10-17 ユーザー：「依頼者が違うと、
        期間に関係なく、声…は変えた方がいい」「同じ男性ならサービスを聞く前に嫌悪感を持たれるので、Ledaが対応するなど」）。
        別の性別の声があれば、その中から（声の一覧の性別の表示名。見直し16）。業務設定の声の型が使えればそれ、使えなければ、話して
        よいほかの声の初めの声の型（声の名前の順。声が増えても同じ決まり）。
    話してよい声が無い・別の声が無いときは、通さずに待つ（発信番号を替えられないときと同じ）。契約者に知らせる（止めない）。
    契約者の組に、契約者の単位の部分（`recordings.CONTRACTOR_PARTS`）がそろっている声だけで話す（話してよい声の1か所）。承認した
    契約者の組に足りない部分があれば、知らせに足りない部分の名前を並べる（⛔ 2026-10-24 CL-1 の連絡帳 4-39：前は予約証の印ごとに一部の
    部分を見て、約束の電話には業務の組に入らない promise_again を求めたので、約束の電話が1本もかからなかった）。"""
    ready = recordings.ready_voices(tx, version_id)
    types = {r["code"]: r for r in tx.all("SELECT code, voice_name, default_for_voice FROM salescall.voice_types")}
    # お詫びの電話は、契約者が聞いて承認した録音と同じ声だけ（録音の文を流すので、声がまじらない。0131）。その声で話せなければ待つ
    sorry = tx.one("SELECT purpose = 'apology' AS yes FROM salescall.current_next_actions WHERE task_id = %s", (task["task_id"],))
    must = recordings.apology_voice(tx, task["task_id"]) if sorry is not None and sorry["yes"] else None
    if must is not None:
        ready = [v for v in ready if v == must]
    mine = tx.one(f"SELECT voice_type FROM salescall.attempts WHERE task_id = %s AND voice_type IS NOT NULL AND {attempts.DIALED}"
                  " ORDER BY claimed_at DESC LIMIT 1", (task["task_id"],))   # お客様が聞いた声だけ（発信しなかった予約は数えない）
    if mine is not None and types[mine["voice_type"]]["voice_name"] in ready:
        return mine["voice_type"]
    before = tx.one("SELECT salescall.number_last_voice_elsewhere(%s) AS v", (task["phone_e164"],))["v"]
    usable = [v for v in ready if v != before]
    if before is not None:
        # お客様が前に聞いた声と、別の性別の声があれば、それだけにする（2026-10-17 ユーザー：「同じ男性ならサービスを聞く前に嫌悪感を
        # 持たれるので、Ledaが対応するなど」。性別は声の一覧の表示名＝0118。分からない声は、別の性別とはみなさない）
        gender = {r["voice_name"]: r["display_gender"] for r in tx.all("SELECT voice_name, display_gender FROM salescall.voices")}
        other = [v for v in usable if gender.get(v) is not None and gender.get(before) is not None and gender[v] != gender[before]]
        usable = other or usable
    own = types[business_voice]["voice_name"]
    if own in usable:
        return business_voice
    if usable:
        return next(code for code, t in sorted(types.items()) if t["voice_name"] == usable[0] and t["default_for_voice"])
    if ready:
        reason, summary, key = "voice_used_by_other_client", OTHER_VOICE_MISSING, ""
    elif missing := recordings.missing_parts(tx):   # 承認した契約者の組はあるが、部分が足りない
        reason, summary, key = "voice_not_ready", VOICE_PARTS_MISSING.format("・".join(missing)), ":" + ",".join(missing)
    else:
        reason, summary, key = "voice_not_ready", VOICE_NOT_READY, ""
    notify.emit(tx, "setting_missing", key=f"{reason}:{version_id}{key}:{today}", summary=summary,
                refs={"client_id": task["client_id"], "version_id": version_id})
    return _deny(reason)


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
        # 記録は、その行の断られた理由が変わったときだけ（同じ理由で何度も門へ渡し直しても、行を増やし続けない。⛔ 2026-10-22 の
        # 見直し：同時の上限のあいだ、数秒ごとに1行ずつ増え、回線100本なら1日に百万行を超えた。理由ごとの数は、断られ始めた回で数える）
        denied_line = None if outcome.reason == "line_unknown" else line_id
        tx.execute("INSERT INTO salescall.gate_denials (tenant_id, client_id, task_id, line_id, reason)"
                   " SELECT salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s"
                   " WHERE (SELECT d.reason FROM salescall.gate_denials d WHERE d.task_id = %s"
                   "        ORDER BY d.denial_id DESC LIMIT 1) IS DISTINCT FROM %s",
                   (task_id, denied_line, outcome.reason, task_id, outcome.reason))
        if outcome.reason in SETTING_GAPS:   # 設定の抜けで待っている：黙って待たせず、契約者に知らせる（止めない。原則6）
            notify.emit(tx, "setting_missing", key=f"gate:{outcome.reason}:{task['client_id']}:{task['run_id']}",
                        summary=SETTING_GAPS[outcome.reason], refs={"client_id": task["client_id"], "run_id": task["run_id"],
                                                                     "reason": outcome.reason})
    return outcome


def _handled(handling: str) -> frozenset[str]:
    return frozenset(name for name, r in REASONS.items() if r.handling == handling)


# 扱いごとの理由の集まり（どれも REASONS の扱いの列から作る。ここで別に書かない。DESIGN.md 5-4）
LINE_REASONS = _handled("line")
CLIENT_REASONS = _handled("client")
BUSINESS_REASONS = _handled("business")   # 業務の理由＝その業務の行を、この回だけ飛ばす（ほかの業務の行はかける）
# 実施回の理由（版が割り当てられていない・業務の決まり／業務設定が無い・承認した録音のある声が無い・暦が無い・設定の値が無い＝その
# 実施回のどの行も同じ答え。直るまで続く）：その実施回の行を、この回だけ飛ばし、予定は書き直さない（門は回ごとに1回は見るので、
# 知らせも出る）。品質の段の理由（まだかけられる段の前・段の件数）は、決まった文面だけの電話（お詫びの電話）を通すので、それ以外の
# 行だけを飛ばす。⛔ 見直し16：前は門が断るたびに、次の動きが行ごとに予定を5分先へ書き直し、待つあいだ記録が1行あたり1日288ずつ増えた
RUN_REASONS = _handled("run")
STAGE_REASONS = _handled("stage")
# 行の理由のうち、すぐに変わるもの（同じ行・同じお客様・同じ番号に、いま進行中の発信がある）：その行を、この回だけ飛ばす（予定は
# 書き直さない。⛔ 見直し15：前は次の動きへ渡し、約束の時刻を5分先へ書き直していた）。候補を出す処理（scheduler.due_tasks）も、
# データベースの「進行中は1本」の守りと同じ条件で、こういう行を初めから出さない（ここへ来るのは、同時に予約がぶつかったときだけ）
TASK_REASONS = _handled("task")
# 候補に出さない理由（ここへ来るのは、候補を出したあとで変わったときだけ）
UNOFFERED_REASONS = _handled("unoffered")
PLAN_IF_KNOWN = _handled("plan_if_known")
# 候補に出さない理由のうち、候補のデータベースの条件（scheduler._candidates）で除くもの（門と同じデータベースの関数：
# salescall.task_excluded_now・salescall.blocking_stops）。のこりは `waits_unknown` が門と同じ関数で見る
UNOFFERED_IN_CANDIDATES = frozenset({"excluded", "stopped"})
UNOFFERED_HERE = UNOFFERED_REASONS - UNOFFERED_IN_CANDIDATES
# 行の予定を書き直さない理由のすべて（回線ごとの処理は、これに入らない理由だけを次の動き after_denial へ渡す）
PLAN_KEPT = frozenset(REASONS) - _handled("plan") - _handled("plan_if_known")


# 門が設定の抜けで待たせる理由と、契約者への注意の短い文（2026-10-17 の見直し：前は黙って待たせていた）
SETTING_GAPS: dict[str, str] = {
    "no_version": "実施回に業務設定の版が割り当てられていません。業務設定の版を割り当ててください。",
    "rules_missing": "業務設定の版に、業務の決まり（断りの答え・理由ごとの扱い・かけ直し）がありません。業務設定を直してください。",
    "settings_missing": "業務設定の版が確かめに通りません。業務設定を直してください。",
    "setting_missing": "設定の値が決まっていない項目があります。設定を確かめてください。",
}


def _judge_and_claim(tx: Tx, task: dict[str, Any], line_id: UUID, lease_holder: str) -> Claim | Denied:
    clock = tx.one("SELECT now() AS now, salescall.jst(now()) AS local")
    now, local = clock["now"], clock["local"]

    # (1) 次の予定（Q4 の記録を読むだけ）
    nxt = tx.one("SELECT kind, call_at, decided_at, purpose, wrong_contact_id, promised, salescall.jst(callee_at) AS callee_at,"
                 "       salescall.jst(window_until) AS window_until, window_daily, window_days, apology_reasons, successor,"
                 "       number_again,"
                 "       salescall.jst(window_from) AS window_from, window_said, window_day_said,"
                 "       salescall.keeping_kind(purpose, promised) AS keeping"
                 " FROM salescall.current_next_actions WHERE task_id = %s", (task["task_id"],))
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
    if tx.one("SELECT salescall.task_awaiting_decision(%s) AS w", (task["task_id"],))["w"]:   # 決まりは1か所（0063）
        return _deny("awaiting_decision")
    # 「かけない」にしている行（一覧の列・ダッシュボード。2026-10-18 ユーザー決定）。お詫び・途中で切れたあとのかけ直し・約束した
    # かけ直しは、「かけない」の例外＝かける（2026-10-18 ユーザー）。「強制かけない」は例外なし（2026-10-19 ユーザー）。
    # 決まりはデータベースの1か所 task_excluded_now（0078・0085）
    if tx.one("SELECT salescall.task_excluded_now(%s) AS x", (task["task_id"],))["x"]:
        return _deny("excluded")

    # (1b) 依頼者を無効にしていないか（Intent.md 4章 A。判断はデータベースの1か所 salescall.client_active）。業務設定の確かめより
    #      先に見る（無効の依頼者の設定の抜けを、契約者に知らせない＝2026-10-18 の見直し）
    if not tx.one("SELECT salescall.client_active(%s) AS a", (task["client_id"],))["a"]:
        return _deny("client_inactive")

    # (2) データで決まる「かけない理由」（番号の種類・発信禁止の番号と管理番号。取込と同じ1か所）
    blocked = target_policy.block_reason(tx, normalize(task["phone_e164"]), client_ref=task["client_ref"],
                                         tenant_id=task["tenant_id"], client_id=task["client_id"],
                                         business_id=task["business_id"])
    if blocked is not None:
        return _deny(blocked[0])
    # 発信に失敗（回線の部品が番号を受け付けない）が、決まった数だけ続いた番号（同じ番号のほかの行も、それぞれ失敗するまでかけない。
    # 決まりは attempts.number_unusable の1か所＝次の動きと同じ。お客様が言った別の番号へ替えるか終えるかは、次の動きが決める。
    # ⛔ 見直し16：前は通話のあとの締めでだけ見て、同じ番号のほかの行は、それぞれ1回ずつ失敗して回線の異常に数えられた）
    if attempts.number_unusable(tx, task["contact_id"], tenant_id=task["tenant_id"], client_id=task["client_id"],
                                business_id=task["business_id"]):
        return _deny("number_unusable")
    # 並びの数だけ続いたが、番号のせいか回線のせいかが、まだ分からない：かけずに待つ（決まりは attempts.number_unconfirmed の1か所。
    # 候補を出す処理も同じ関数で出さない＝waits_unknown。2026-10-24 ユーザー：行をやめるのは「明らかに番号に問題がある場合」だけ）
    if attempts.number_unconfirmed(tx, task["contact_id"], tenant_id=task["tenant_id"], client_id=task["client_id"],
                                   business_id=task["business_id"]):
        return _deny("number_unconfirmed")

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
    # 話す中身の無い通話をしない：その版の業務設定が確かめに通らない・声の型が一覧から外れた（置いた時は一覧にあった）なら通さない
    content = _content(tx, version)
    if content is None:
        return _deny("settings_missing")
    # 業務の話し方を使わない電話（決まった文面だけ＝お詫びの電話。DESIGN.md 10-1-2）は、品質の段（段の件数・まだかけられる段に
    # 入っていない）と、目的のお客様を呼ぶ材料を見ない（前の通話で話したお客様に、決まった文面を伝える）。どの電話がそれに当たるかは、
    # データベースの1か所 salescall.fixed_text_kind（0120。⛔ 見直し16b：前は「品質の停止でも通す電話」の表の行で決めていて、2つの
    # 別の問いがつながっていた。見直し15の前は、ここに「お詫び」を直に書いていた）
    fixed_text = tx.one("SELECT salescall.fixed_text_kind(%s) AS p", (nxt["keeping"],))["p"]
    # 目的のお客様を呼ぶ材料が足りない行にかけない（`_target_short` の1か所。候補を出す処理も同じものを読んで、初めから出さない）
    short = _target_short(tx, task, nxt["keeping"], content)
    if short is not None:
        return _deny(short)
    ok, why = (True, None) if fixed_text else quality_stage.allowance(tx, version)
    if not ok:
        return _deny(why)

    # (5) 止まっているか（全体・契約者・依頼者・業務・回線）
    #     止めているあいだも、お客様との約束を果たす電話（約束・途中で切れたあとのお詫びのかけ直し・お詫びの電話）を通すかは、
    #     停止の原因ごとの決まり（データベースの表 salescall.stop_lets_through の1か所＝stops.blocking。いまは、品質の停止でお詫びの
    #     電話だけ。赤字では止めない＝2026-10-22 深夜 ユーザー）
    open_now = stops.blocking(tx, tenant_id=task["tenant_id"], client_id=task["client_id"], business_id=task["business_id"],
                              line_id=line_id, kind=nxt["keeping"])
    if open_now:
        # 止まっているのが、この回線だけ（回線の停止）なら、回線の理由：この回線は休み、行の予定は書き直さない（ほかの回線は、
        # その行をかけられる）。⛔ 2026-10-22 の見直し14：前は「止まっている」で断り、止まった回線が行の予定を5分ずつ先へ書き
        # 直し続け、約束の電話を先送りし、元気な回線もかけられなかった
        return _deny("line_stopped" if all(s["scope"] == "line" for s in open_now) else "stopped")

    # (6) お客様が言ったかけ直しの時間の幅（「夕方」「3時まで」など。2026-10-19 ユーザー。表の作り 0086）：幅の外ではかけない。
    #      幅の決まりは calling_hours.in_window の1か所。話せなかったときの次の回（お客様がその日を選んだ理由に合わせる）は次の動き（`_keep_promise`）が決める
    daily = None if nxt["window_daily"] is None else tuple(nxt["window_daily"])
    days = None if nxt["window_days"] is None else tuple(nxt["window_days"])
    if not calling_hours.in_window(local, nxt["window_until"], daily, days, holiday_checker(tx), since_local=nxt["window_from"]):
        return _deny("callback_window")

    # (6b) 時間帯：予約から予約証の期限まで、まるごと内側（5-2-1 の4）。祝日の暦の外は判断できない（5-2-1 の5）
    #      お客様が言った幅の約束の電話（次の予定が幅を持つ）は、お客様の指定なので、依頼者の時間帯・NG時間の外でもかける（端の決まって
    #      いない側はお客様指定の歯止め時間で埋めて切る。口にした時刻はその外でも。`calling_hours.callee_hours` の1か所。2026-10-22・10-23 ユーザー）
    lifetime = timedelta(seconds=int(_setting(tx, "gate.claim_lifetime", task)))
    hours, ng = _hours_settings(tx, dict(tenant_id=task["tenant_id"], client_id=task["client_id"],
                                         business_id=task["business_id"]))
    w = calling_hours.callee_window(nxt)
    callee = None if w is None else window_hours(tx, tenant_id=task["tenant_id"], client_id=task["client_id"],
                                                 business_id=task["business_id"], window=w)
    if not calling_hours.allowed(hours, ng, holiday_checker(tx), local, local + lifetime, callee_at=nxt["callee_at"],
                                 window=callee):
        return _deny("outside_hours")

    # (7) 間隔の待ち
    if pace.earliest is None:
        return _deny(pace.binding)
    if pace.binding is not None:
        return _deny(pace.binding)

    # (7b) 業務ごとの1日の上限（2026-10-18 ユーザー決定：「業務ごと、デフォルトは上限なし」。null＝上限なし）。数えるのも止めるのも、
    #      営業として新しくかける電話だけ（お詫び・途中で切れたあとのかけ直し・約束したかけ直しは、数えず止めない。決まりは
    #      データベースの1か所 salescall.is_new_call＝0078）
    cap = _setting(tx, "calling.business_daily_dials", task)
    if cap is not None and tx.one("SELECT salescall.is_new_call(%s, %s) AS n", (nxt["purpose"], nxt["promised"]))["n"]:
        # 数えるのは、お客様に届いた1回（数え方の1か所 salescall.dial_kind。発信に失敗・回線の失敗・こちらの側で切れた通話は
        # 数えない）と、いま生きている予約（予約してまだかけていない試行を数えないと、同時に動く回線の本数
        # まで上限を超える＝2026-10-18 の見直し。予約も数えると、同時の2つの門は直列化でぶつかり、片方がやり直す）
        made = tx.one(
            "SELECT count(*) AS n FROM salescall.attempts a"
            " JOIN salescall.tasks t ON t.client_id = a.client_id AND t.task_id = a.task_id"
            " JOIN salescall.runs r ON r.client_id = t.client_id AND r.run_id = t.run_id"
            " WHERE r.business_id = %s AND a.client_id = %s"
            f"   AND ({attempts.reached('a')} OR salescall.is_live(a.state, a.claim_expires_at))"
            "   AND salescall.is_new_call(a.purpose, a.promised)"
            "   AND a.claimed_at >= salescall.jst_day_start(%s) AND a.claimed_at < salescall.jst_day_start(%s)",
            (task["business_id"], task["client_id"], local.date(),
             local.date() + timedelta(days=1)))["n"]
        if made >= int(cap):
            return _deny("business_daily_limit")

    # (8) 回線：借りている本人で、借用の期限が予約証の期限より先。回線ごとの1日の上限（Intent.md 8-6）
    line = tx.one("SELECT lease_holder, lease_expires_at, client_id, retired_at FROM salescall.lines WHERE line_id = %s",
                  (line_id,))
    if line is None:
        return _deny("line_unknown")
    if line["retired_at"] is not None:
        return _deny("line_retired")
    if line["lease_holder"] != lease_holder or line["lease_expires_at"] < now + lifetime:
        return _deny("line_not_leased")
    # 発信番号：依頼者に割り当てた回線は、その依頼者だけ。依頼者が替わったら、期間に関係なく発信番号を替える（2026-10-17
    # ユーザー：「依頼者が違うと、期間に関係なく、声と発信番号は変えた方がいい」「依頼者ごとに発信番号があることがデフォルト。
    # ただし、検証時は発信番号は1つでOK」）。替えられる回線が無いときは、ほかの回線が空くのを待つ（契約者に、依頼者ごとの
    # 発信番号が無いことを知らせる。設定の抜けは注意だけ）
    if not tx.one("SELECT salescall.line_serves(%s, %s) AS ok", (line["client_id"], task["client_id"]))["ok"]:
        return _deny("line_other_client")   # 決まりはデータベースの1か所 line_serves（0094）
    if _line_taken(tx, task, line_id, local.date()):   # `_line_taken` の1か所（候補を出す処理も、この回線には出さない）
        return _deny("line_used_by_other_client")
    # 回線の1日の上限：今日の約束の電話の分は取り置く（ふつうの電話は使わない。2026-10-19 ユーザー：「その日の約束の電話には
    # 目印が付き、それを処理してから、通常の順序の電話を始めた方がいい」。数えるのは salescall.line_promised_due の1か所＝0089）
    dials = tx.one("SELECT salescall.line_dials_on(%s, %s) AS n", (line_id, local.date()))["n"]
    if not nxt["promised"]:
        dials += tx.one("SELECT salescall.line_promised_due(%s, %s, %s) AS n", (line_id, local.date(), task["task_id"]))["n"]
    if dials >= int(_setting(tx, "line.max_dials_per_day", task)):
        return _deny("line_daily_limit")
    # お客様が時刻を指定した約束（時刻ちょうど・幅の無い指定）が、これから始める電話が終わるより前に来るなら、約束の数だけの
    # 回線を空けておく（約束の時刻に回線をふさがない。2026-10-19 ユーザー：「約束の数分前からふつうの電話を始めない」「1本は
    # 通常業務に使っていいでしょ…該当の回線を空けておくだけでいい」）。空けるのは、時刻を指定された約束の電話のほかのすべて
    # （幅の約束・お詫び・続き・聞き直しも、約束の時刻に回線をふさぐのは同じ）。長さは業務の平均通話時間なので、業務の理由
    # （短い業務の電話なら始められる）。決めるのは line_kept_for_promise の1か所（契約者の回線全体の割り当て＝0092）
    if not (nxt["promised"] and nxt["callee_at"] is not None):
        soonest = tx.one("SELECT salescall.next_customer_promise_at(%s) AS at", (task["task_id"],))["at"]
        if soonest is not None:   # 時刻を指定された約束が無ければ、平均通話時間を測らない
            until = now + timedelta(seconds=promise_guard_seconds(tx, task))
            if soonest <= until and tx.one("SELECT salescall.line_kept_for_promise(%s, %s, %s) AS keep",
                                           (line_id, until, task["task_id"]))["keep"]:
                return _deny("promise_soon")

    # (9) 同時の本数：依頼者ごとの上限。会話AIの1分の上限では待たせない（2026-10-17 ユーザー：「1分制限を超えそうになったら、
    #     きりのいいところで、次のキーと併用して進んだらいい」＝キーの選択 Q15 が次のキーを併せて使う。5-2 の9）
    active = tx.one("SELECT count(*) AS n FROM salescall.attempts WHERE salescall.is_live(state, claim_expires_at)")["n"]
    if active >= int(_setting(tx, "calling.max_concurrent_calls", task)):
        return _deny("client_concurrency")

    # (10) 話す声（承認した録音のある声だけ。ほかの依頼者がかけた番号へは別の声。choose_voice の1か所。ほかの理由で通さない
    #      ときに知らせを出さないよう、最後に決める）
    voice = choose_voice(tx, task, version, content.voice, local.date())
    if isinstance(voice, Denied):
        return voice

    # (11) 予約。同じお客様・番号・回線の進行中は、データベースの部分一意が拒む（その部分だけ取り消して理由を返す）
    try:
        with tx.savepoint():
            row = tx.one(
                "INSERT INTO salescall.attempts (tenant_id, client_id, task_id, contact_id, phone_e164, line_id, version_id,"
                "                                state, claimed_at, claim_expires_at, purpose, wrong_contact_id, voice_type,"
                "                                promised)"
                " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, 'claimed', now(), now() + %s,"
                "         %s, %s, %s, %s)"
                " RETURNING attempt_id, claim_expires_at,"
                "   (SELECT phone_e164 FROM salescall.task_numbers WHERE task_id = %s AND contact_id = %s) AS wrong_phone",
                (task["task_id"], task["contact_id"], task["phone_e164"], line_id, version, lifetime,
                 nxt["purpose"], nxt["wrong_contact_id"], voice, bool(nxt["promised"]), task["task_id"],
                 nxt["wrong_contact_id"]))
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
    # 検証モードでかけた通話の録音は、測り終わるまで消さない（Intent.md 11-4 第2版・2026-10-17 ユーザー 案A）。印は、かけたときの
    # モードで付ける（締めるときのモードで決めると、検証の終わり際の通話に印が付かなかった＝2026-10-18 の見直し）
    if approval.human_for_business(tx, task["business_id"]):
        recordings.hold(tx, row["attempt_id"], "verification")
    return Claim(row["attempt_id"], task["task_id"], line_id, task["phone_e164"], version, row["claim_expires_at"],
                 nxt["purpose"], row["wrong_phone"], voice, tuple(nxt["apology_reasons"]), bool(nxt["promised"]),
                 bool(nxt["successor"]), nxt["number_again"])
