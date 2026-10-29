"""試行（1回の発信）の状態を移す処理（DESIGN.md 4-2）。持ち主 CL-2。

試行の行を書き換えるのは、この部品だけ（I-12・I-27。app/checks が見張る）。呼ぶ側と、使ってよい処理：
- 回線の部品（呼び出し中・応答・会話中・締め・終わり）と復旧（結果不明）  ⇒ `move`
- 発信の出口（予約 → 発信）                                              ⇒ `start_dialing`
- 門（期限切れの予約を戻す・門の前提が変わった予約を取り消す）           ⇒ `release`
- 通話後の検証（Q21。待ち → 済み）                                        ⇒ `record_verification`
- 締めの処理（core/settle.py。判定が決めた切れ方・途中で切れたかを1回だけ）  ⇒ `record_disconnect`・`record_cut_off`
- 発信の出口（PC の部品がかけなかった＝お客様の電話は鳴っていない）          ⇒ `withheld`
移ってよい組・期限・時刻・検証の状態の決まりは、データベースが持つ（ここで2つ目の決まりを作らない）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

from salescall.core import business_rules, errors, fix_requests, number_recheck, settings, target_recheck, watch
from salescall.core.access import Tx
from salescall.core.errors import ErrorReport

def dialed(table: str = "") -> str:
    """発信まで進んだ試行の条件（SQL。予約のまま・かけずに取り消した予約は入れない）。決まりはデータベースの salescall.is_dialed の
    1か所（データベースの関数も同じものを使う）。table＝表の別名（無ければ state）。回数・間隔・「その回にかけたか」・続けて何回
    かけたか などは、どれもこれを使う。"""
    return f"salescall.is_dialed({table + '.' if table else ''}state)"


DIALED = dialed()


def placed(table: str = "") -> str:
    """発信まで進み、回線の部品が番号を受け付けなかった（call_failed）ものを除いた試行の条件（SQL。1か所）。回線を使った電話：
    回線の1日の上限・報酬の「かけた1本」が使う。番号の続き（salescall.failed_run）・約束の回に「かけたか」・その回にかけた数は
    これでなく、数え方の種類（`reached`・`rang`）で決める（⛔ 見直し17c）。"""
    return f"salescall.is_placed({table + '.' if table else ''}state)"   # 決まりはデータベースの1か所（0109）


def kind(table: str = "") -> str:
    """発信の数え方の種類（SQL。決まりはデータベースの salescall.dial_kind の1か所＝0116）：NULL＝発信まで進んでいない／
    not_placed＝発信に失敗（鳴っていない）／line_failure＝お客様が出る前に回線・こちらの側で終わった／cut_off＝お客様が出たあと、
    お客様の意思と関係なく途中で切れた／reached＝お客様に届いた1回。"""
    t = table + "." if table else ""
    return f"salescall.dial_kind({t}state, {t}cut_off, {t}attempt_id)"


def kind_of(tx: Tx, attempt_id: UUID) -> str | None:
    """その試行の数え方の種類（`kind` と同じ決まり＝salescall.dial_kind）。"""
    return tx.one(f"SELECT {kind()} AS k FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))["k"]


def resume_allowed(tx: Tx, task_id: UUID) -> bool:
    """その行のいちばん新しい発信のあと、お詫びしてすぐかけ直してよいか（決まりはデータベースの1か所 salescall.resume_allowed＝
    その発信が途中で切れた通話で、続けて切れた数が salescall.resume_in_a_row() 回まで。2026-10-18 ユーザー 案A。数え方の dial_kind も
    同じ数を読む＝すぐかけ直す回を超えて切れた通話は、ふつうの電話の回数に数える）。こちらのプログラムの不具合で終えた通話（is_ours）は、
    いつもお詫びとともにかけ直すが、いつかけるか（復旧次第）は門と候補を出す処理が salescall.awaiting_recovery の1か所で待たせる（0141）。"""
    row = tx.one(f"SELECT salescall.resume_allowed(attempt_id) AS ok FROM salescall.attempts WHERE task_id = %s AND {DIALED}"
                 " ORDER BY claimed_at DESC LIMIT 1", (task_id,))
    return bool(row and row["ok"])


def failed_run(tx: Tx, contact_id: UUID) -> dict[str, Any]:
    """その番号（依頼者の連絡先。どの行・どの回線でも）への、いちばん新しい鳴った発信より後の、お客様のところで電話が鳴らなかった
    （回線の部品がかけられなかった・お客様が出る前に回線やこちらの側で終わった＝`rang` でない）発信の続き（決まりはデータベースの
    1か所 salescall.failed_run＝0129）。回線のせいと分かった発信（`blame` が line）は数えない（続きを切りもしない）：
    n＝番号のせい（number）と、まだ分からない（pending）の数（同じ番号のほかの行の、まだ決まっていない発信も入る＝`number_unconfirmed`）・
    n_sure＝番号のせいの数（次にかける時刻の決まった順＝next_action._failed_retry_at と、その番号へかけないと決めるのは、こちらだけ。
    0143：日をまたいで送るのは番号のせいと分かった数だけ）・first_day＝数えた最初の日（日本時間）・last_end＝数えたいちばん新しい発信の
    終わり（無ければ None）。2026-10-24 ユーザー：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は行は取りやめで
    なく一時中止だけ」。"""
    return tx.one("SELECT n, n_sure, first_day, last_end FROM salescall.failed_run(%s)", (contact_id,))


def blame(tx: Tx, attempt_id: UUID) -> str | None:
    """お客様のところで鳴らなかった発信が、だれのせいか（決まりはデータベースの1か所 salescall.unrung_blame＝0129・0143。電話の部品の
    言い分では決めず、同じ回線のあとの証拠で決める。2026-10-24 管理者：「次の行に進んで、次の行がつながればすぐに回線の問題か番号の
    問題か判断できますよね」）：number＝同じ回線が、そのあとお客様の電話を鳴らせた・テスト番号が鳴った（番号のせい）／line＝テスト番号が
    鳴らなかった・回線を使わなくした（回線のせい）／ours＝こちらのせい（こちらのプログラムの不具合で終えた＝trouble。いつも、番号にも回線
    にも行の回数にも数えない＝salescall.is_ours。0139。同じ行で続けば、その行を休ませる＝core/troubles）／pending＝まだ分からない（その
    回線がつぎにお客様の電話を鳴らすか、テスト番号で決まる。⛔ 0129〜0141：違う番号が2つ続けて鳴らなかったら回線のせいとした＝決定に
    無い決まり。0143 で外した）。鳴った・終わっていない・発信まで進んでいない発信は None。まだ分からない発信は締めない（`settle.settle`。
    その行は、決まるまで休む）。"""
    return tx.one("SELECT salescall.unrung_blame(%s) AS b", (attempt_id,))["b"]


def line_counted_after(tx: Tx, attempt_id: UUID) -> bool:
    """その発信のあと、回線ごとの処理が、回線の異常を数え直すか（1か所）：お客様のところで鳴らなかった発信（`blame` がある）か、
    回線の異常に数えうる理由で終えた発信（声が聞き取れない。どの理由かはデータベースの salescall.trouble_counts_line の1か所。0143）。"""
    row = tx.one("SELECT salescall.unrung_blame(attempt_id) IS NOT NULL OR salescall.trouble_counts_line(trouble) AS c"
                 " FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
    return bool(row and row["c"])


# 番号のせいでないと分かった鳴らなかった発信（`blame` の値）：かけた予定のまま、どの数にも数えずにかけ直す（next_action の1か所）
REDO_AS_PLANNED = ("line", "ours")


def line_pending(tx: Tx, line_id: UUID) -> dict[str, Any] | None:
    """その回線の、番号のせいか回線のせいかがまだ分からない続き（since＝続きの最初の電話の時刻・phones＝続きの番号の並び）。違う番号の
    数にかかわらない（0143）。無ければ None（決まりはデータベースの1か所 salescall.line_pending。`line_run_pending` も同じものを読む）。"""
    return tx.one("SELECT since, phones FROM salescall.line_pending(%s)", (line_id,))


def line_run_pending(tx: Tx, line_id: UUID) -> bool:
    """その回線に、番号のせいか回線のせいかがまだ分からない続き（`line_pending`）があるか（決まりはデータベースの1か所
    salescall.line_pending＝0136・0141。区切りは鳴った電話・鳴ったテスト・閉じた印。こちらのせいの電話は数えない）。"""
    return tx.one("SELECT salescall.line_run_pending(%s) AS p", (line_id,))["p"]


def close_unrung_run(tx: Tx, line_id: UUID, reason: str) -> bool:
    """まだ分からない続きを閉じて、回線のせいと決める（1か所。決め方はデータベースの1か所 salescall.close_unrung_run＝0143）。
    reason＝retired（その回線を使わなくした。呼ぶのは core/registry）・test_silent（テスト番号が鳴らなかった。呼ぶのは core/ring_test の
    1か所）。閉じたら True。⛔ 0131〜0141 の no_rows（テスト番号で決められないとき、前の区切りで決める）は外した：前の区切りで番号のせいと
    決めると、壊れた回線のお客様の行を失う。テスト番号で決められないあいだは決めずに待つ（supervisor/line.py の `_decide_if_stuck`）。"""
    return tx.one("SELECT salescall.close_unrung_run(%s, %s) AS c", (line_id, reason))["c"]


def line_faults_unrecorded(tx: Tx, line_id: UUID) -> list[dict[str, Any]]:
    """その回線の、回線のせいと分かったのに、まだ回線の異常として記録していない発信（attempt_id・client_id・by_audio＝声が聞き取れない
    電話が違うお客様で続いたもの。ほかは鳴らなかった電話で回線のせい（`blame` が line）。かけた順。決まりはデータベースの1か所
    salescall.line_faults_unrecorded＝0143）。回線ごとの処理が読んで1つずつ記録する（記録済みかは記録の attempt_id で見る＝何度読んでも
    二重に数えない）。"""
    return tx.all("SELECT attempt_id, client_id, by_audio FROM salescall.line_faults_unrecorded(%s)", (line_id,))


def failed_retry_days(tx: Tx, **scope: Any) -> list[int]:
    """発信に失敗が続いたときに、同じ番号へ何日目にかけるかの並び（設定 dial.failed_retry_days。1＝最初に失敗した日。並びの数が、
    続けて受け付けられなければその番号へかけない回数）。"""
    return [int(d) for d in settings.resolve(tx, "dial.failed_retry_days", **scope)]


def number_unusable(tx: Tx, contact_id: UUID, **scope: Any) -> bool:
    """その番号へは、もうかけないか（1か所。門と次の動きの両方が読む）：番号のせいと分かった、お客様のところで鳴らなかった発信
    （`failed_run` の n_sure）が、並び（`failed_retry_days`）の数だけ続いた（2026-10-22 深夜 ユーザー：「同じ番号で続けて当日2回＆
    翌日1回、さらに2日後1回の計4回受け付けられなかったら、その番号へはかけない」。2026-10-23 ユーザー：お客様が出る前に毎回終わる
    番号も「番号を受け付けないときと同じ」。2026-10-24 ユーザー：「明らかに番号に問題がある場合は、その行は取りやめ」＝回線のせいか
    まだ分からない発信では、やめない＝`number_unconfirmed`）。"""
    return failed_run(tx, contact_id)["n_sure"] >= len(failed_retry_days(tx, **scope))


def number_unconfirmed(tx: Tx, contact_id: UUID, **scope: Any) -> bool:
    """鳴らなかった発信が並びの数だけ続いたが、番号のせいか回線のせいかが、まだ分からないものがある（1か所。門と、候補を出す処理
    （gate.waits_unknown）が読む）：その行はかけずに待つ（候補に出さない）。分からない発信の行そのものは、決まるまで締めないので
    （settle.settle）、ここで待つのは、同じ番号のほかの行（同じ番号が2つの行にある）。決まれば、番号のせいなら `number_unusable`、回線の
    せいなら数から外れ、決まった順の続きでかける。"""
    run = failed_run(tx, contact_id)
    if run["n"] == run["n_sure"]:   # まだ分からない電話が無い（候補を出す処理が行ごとに読むので、設定を読む前に返す）
        return False
    return run["n"] >= len(failed_retry_days(tx, **scope)) > run["n_sure"]


def reached(table: str = "") -> str:
    """お客様に届いた1回の条件（SQL。1か所）：同じお客様への最小の間隔・番号の聞き直しとお詫びの電話の回数・業務ごとの1日の
    上限が数える（お客様の意思と関係なく繋がらなかった・切れた発信と、発信に失敗は数えない）。"""
    return f"{kind(table)} = 'reached'"


def rang(table: str = "") -> str:
    """お客様のところで電話が鳴った試行の条件（SQL。1か所）：数え方の種類 dial_kind が「お客様に届いた1回」か「途中で切れた」
    （発信に失敗・お客様が出る前の回線の失敗は、かけられなかったのと同じ）。約束の回に、一度もかけられなかったか（お詫びの印）を
    決めるのに使う（next_action._missed）。本数を方針と比べる所は `reached`（同じ決まりの、数える側）。"""
    t = table + "." if table else ""
    return f"salescall.rang({t}state, {t}cut_off, {t}attempt_id)"   # 決まりはデータベースの1か所（0124）。番号の続き・回線の証拠も同じ


def counts_as_try(table: str = "") -> str:
    """行の回数に数える発信の条件（SQL。1か所）：お客様に届いた1回（`reached`）で、お客様と約束したかけ直しでないもの。門（行の
    回数の上限・同じお客様の1日の回数）と、次の動きの再発信の方針の回数の両方が読む（2026-10-14 ユーザー：「統一した方がよくない？」）。
    決まりはデータベースの salescall.counts_as_try の1か所（0116）。"""
    t = table + "." if table else ""
    return f"salescall.counts_as_try({t}state, {t}cut_off, {t}promised, {t}attempt_id)"


# 回線の部品と復旧が移してよい先（予約 → 発信・予約の取り消しは、それぞれ専用の処理だけ）
LineState = Literal["ringing", "answered", "in_conversation", "ending", "ended",
                    "busy", "no_answer", "call_failed", "dropped", "technical_error"]
HangupSide = Literal["callee", "line", "unknown"]


class AttemptNotFound(Exception):
    """その試行は無い（または、取引の依頼者の範囲の外）。"""


def move(tx: Tx, attempt_id: UUID, to_state: LineState, *, hangup_side: HangupSide | None = None) -> dict[str, Any]:
    """回線の部品・復旧が、試行の状態を移す。移れない組なら、データベースが拒む（Rejected）。"""
    if to_state in ("claimed", "dialing", "released"):
        raise ValueError(f"{to_state} へは、この処理では移せません（予約は門、発信は出口、取り消しは門の処理）")
    row = tx.one(
        "UPDATE salescall.attempts SET state = %s, hangup_side = %s WHERE attempt_id = %s"
        " RETURNING attempt_id, state, verification, hangup_side, ended_at",
        (to_state, hangup_side, attempt_id),
    )
    if row is None:
        raise AttemptNotFound(str(attempt_id))
    return row


def start_dialing(tx: Tx, attempt_id: UUID) -> bool:
    """発信の出口だけが呼ぶ。予約が「まだ予約のまま・期限内」なら発信中にし True。それ以外は False（発信しない）。

    確かめと書き換えを1つの書き換えで行う（間に、ほかの処理が割り込めない。DESIGN.md 5-3）。
    """
    row = tx.one(
        "UPDATE salescall.attempts SET state = 'dialing'"
        " WHERE attempt_id = %s AND state = 'claimed' AND claim_expires_at > now() RETURNING attempt_id",
        (attempt_id,),
    )
    return row is not None


def release(tx: Tx, attempt_ids: list[UUID]) -> int:
    """まだ発信していない予約を取り消す（期限切れ・門の前提が変わった＝停止・発信禁止・行を終えた・版を替えた・緊急停止の印。
    DESIGN.md 5-2-1 の2・3）。"""
    if not attempt_ids:
        return 0
    return tx.execute(
        "UPDATE salescall.attempts SET state = 'released' WHERE attempt_id = ANY(%s) AND state = 'claimed'",
        (list(attempt_ids),),
    ).rowcount


# 使ったキーの種類（試行の keys_used。CL-1 の連絡帳 4-34 の 4-67。形の守りはデータベースの salescall.keys_used_ok）
KEY_SERVICES = ("live", "judge", "ear", "own_check")


def record_keys(tx: Tx, attempt_id: UUID, keys: list[dict[str, str]]) -> None:
    """その通話で使ったキーの識別名を足す（書き込む入口 conversation_io.record_keys から。前に残したものは消さず、同じものは
    重ねない）。形は {"service": KEY_SERVICES のどれか, "key": "名前#指紋8桁"}。値は残さない（形の確かめはデータベース）。"""
    if tx.one("UPDATE salescall.attempts SET keys_used ="
              " (SELECT coalesce(jsonb_agg(DISTINCT k), '[]'::jsonb)"
              "  FROM jsonb_array_elements(coalesce(keys_used, '[]'::jsonb) || %s::jsonb) k)"
              " WHERE attempt_id = %s RETURNING attempt_id", (json.dumps(keys), attempt_id)) is None:
        raise AttemptNotFound(str(attempt_id))


# 通話をお詫びして終えた理由（一覧の正本は core/errors.ENDS の1か所）。どれがこちらのプログラムの不具合か（こちらのせい＝attempts.blame の
# ours。番号・回線・行の回数のどれにも数えない。同じ行で続けば、その行を休ませる＝core/troubles）は、データベースの salescall.trouble_ours の
# 1か所（`is_ours`。0141：「声が聞き取れない」は、こちらの不具合に数えない）
TROUBLES = errors.ENDS


def is_ours(tx: Tx, attempt_id: UUID) -> bool:
    """その試行が、こちらのプログラムの不具合で終えた電話か（決まりはデータベースの1か所 salescall.is_ours→trouble_ours。0141）。"""
    return tx.one("SELECT salescall.is_ours(%s) AS o", (attempt_id,))["o"]


WHY_MAX = 200   # 通話を終えた根拠の文の長さの上限（データベースの守り attempts_trouble_why と同じ）


def end_for_trouble(tx: Tx, attempt_id: UUID, reason: str, why: str | None = None) -> bool:
    """甚大な問題で、進行中の通話を「結果不明」（technical_error）にし、なぜ終えたか（理由と根拠の短い文 why。お客様の言葉・名前は
    入れない＝CL-1 の連絡帳 4-59）を残す（1か所。0143。お詫びして切るのは回線ごとの処理＝supervisor/line.py の `_Call`。お客様が出た
    あとなら、判定は「途中で切れた」と読み＝judge.cut_off、復旧次第お詫びとともにかけ直す）。もう終わっていれば何もしない（False）。"""
    if reason not in TROUBLES:
        raise ValueError(f"通話を終えた理由は {TROUBLES} のどれかです：{reason!r}")
    why = None if why is None else why.strip()[:WHY_MAX] or None
    return tx.one("UPDATE salescall.attempts SET state = 'technical_error', trouble = %s, trouble_why = %s"
                  " WHERE attempt_id = %s AND salescall.is_active(state) AND salescall.is_dialed(state)"
                  " RETURNING attempt_id", (reason, why, attempt_id)) is not None


def withheld(tx: Tx, attempt_id: UUID) -> None:
    """発信の出口だけが呼ぶ。PC の部品が電話をかけようとしなかった（PC の上の緊急停止・PC がつながっていない・前の頼みを待つうちに
    答えの期限を過ぎた＝お客様の電話は鳴っていないし、番号も回線も試していない。かけようとして、かけられなかったものは「発信に失敗」
    にして、だれのせいかを証拠で決める＝`blame`。⛔ 0129：前は「回線のせいか分からない」もここで取り消し、番号には数えず回線の異常と
    して数えたので、1つの番号だけが続けてかけられないと回線が止まり続けた）：発信まで進まなかった予約と同じく取り消す（判定しない・どの数にも入らない。行の予定はそのまま＝いまの予定のまま、
    ほかの回線か、少しあとにかけ直す）。⛔ 2026-10-22 の見直し14：前は「発信に失敗」にして、締めが「会話に至らなかった」と判定し、
    再発信の方針の回数を使い、続き・番号の聞き直しの目的も消えた。"""
    if tx.one("UPDATE salescall.attempts SET state = 'released' WHERE attempt_id = %s AND state = 'dialing'"
              " RETURNING attempt_id", (attempt_id,)) is None:
        raise AttemptNotFound(str(attempt_id))


class RecheckMissing(Exception):
    """番号の確かめ（DESIGN.md 3-5）・目的のお客様の確かめ直し（6-12 の (e)）が要る通話なのに、記録が無い。"""


@dataclass(frozen=True)
class ForbiddenFinding:
    """言ってしまった、言ってはいけないこと（2つの耳で確かめたもの）。"""
    item: str                 # 業務の決まりの、言ってはいけないことの名前（core/business_rules.py の forbidden）
    said: str                 # AI が実際に言ったこと（AI の発話だけ。お客様の発話は入れない）


@dataclass(frozen=True)
class VerificationSummary:
    """通話後の検証の結果の数（品質の段の指標になる。DESIGN.md 10-1）。"""
    items_checked: int        # 2つの耳で確かめた項目の数
    items_disagreed: int      # そのうち、2つの耳が食い違った項目の数
    forbidden_speech: int     # 言ってはいけないこと（2つの耳で確かめたもの）の数
    findings: tuple[ForbiddenFinding, ...] = ()   # その中身（数と同じだけ。お詫びの電話の文面を作るのに使う）
    recording_missing: bool = False  # 録音が届かないまま待つ時間を過ぎ、確かめずに済ませた（`finish_without_recording` だけが付ける）

    def __post_init__(self) -> None:
        if len(self.findings) != self.forbidden_speech:
            raise ValueError("言ってはいけないことの数と、その中身（findings）の数が合いません")
        if self.recording_missing and (self.items_checked or self.items_disagreed or self.forbidden_speech):
            raise ValueError("録音なしで済ませた確かめは、確かめた数を持ちません")


def record_verification(tx: Tx, attempt_id: UUID, *, summary: VerificationSummary) -> None:
    """通話後の検証（Q21）だけが呼ぶ。「待ち」から「済み」へ。検証の結果の数（summary）を必ず一緒に残す（データベースも守る）。
    - 確かめができないときは、ここを呼ばない（「待ち」のまま）。できない原因は仕組みの不具合（別の耳につながらない・誤り・録音が
      無い）なので、エラーの入口（core/watch.py の `after_error`）に渡し、直ってからやり直す（2026-10-17 ユーザー：「そんな状態自体が
      問題では？」。前は「確かめられなかった」で、その行を終えていた）。
    - お客様が言った番号へかけて番号違いだった通話は、番号の確かめ（core/number_recheck.py）を記録してからでないと済ませられない
      （付け合わせられなかったときも cannot_check として記録する）。
    - 判定なしで目的のお客様に繋いだ通話は、目的のお客様の確かめ直し（core/target_recheck.py）を記録してからでないと済ませられない
      （確かめられなかったときも cannot_check。どちらも、その通話の答えは「-」になる）。"""
    if summary is None:
        raise ValueError("検証の結果の数を、必ず渡します")
    if number_recheck.needed(tx, attempt_id) and number_recheck.load(tx, attempt_id) is None:
        raise RecheckMissing("番号違いだった番号の確かめ（証拠との付け合わせ）を、先に記録してください")
    if target_recheck.missing(tx, attempt_id):
        raise RecheckMissing("判定なしで繋いだ目的のお客様の確かめ直し（録音から）を、先に記録してください")
    tx.execute(
        "INSERT INTO salescall.verification_summaries (tenant_id, client_id, attempt_id, items_checked, items_disagreed,"
        " forbidden_speech, recording_missing) SELECT tenant_id, client_id, attempt_id, %s, %s, %s, %s FROM salescall.attempts"
        " WHERE attempt_id = %s",
        (summary.items_checked, summary.items_disagreed, summary.forbidden_speech, summary.recording_missing, attempt_id))
    if summary.findings:
        version = tx.one("SELECT version_id FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))["version_id"]
        known = business_rules.load(tx, version).forbidden
        for n, f in enumerate(summary.findings, 1):
            if f.item not in known:
                raise ValueError(f"言ってはいけないことの名前 {f.item!r} は、この版の業務の決まりにありません")
            if not f.said.strip():
                raise ValueError("言ってしまったこと（said）が空です")
            tx.execute("INSERT INTO salescall.forbidden_findings (tenant_id, client_id, attempt_id, seq, item, said)"
                       " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s)",
                       (attempt_id, n, f.item, f.said.strip()))
    row = tx.one(
        "UPDATE salescall.attempts SET verification = 'done' WHERE attempt_id = %s RETURNING attempt_id",
        (attempt_id,),
    )
    if row is None:
        raise AttemptNotFound(str(attempt_id))


STUCK_AFTER = timedelta(hours=1)   # 通話の終わりから、録音を待つ時間にこれを足しても検証が済まなければ「止まっている」


def watch_stuck_verifications(tx: Tx) -> int:
    """取引の依頼者の通話で、通話後の検証が止まっているもの（録音を待つ時間＋1時間たっても「待ち」）を数え、あれば CL-1 へ修正依頼を
    出す（同じ不具合は直したと報告するまで1つ。⛔ 2026-10-22 の見直し：検証の部品が動かないと、行もお客様も待ち続け、だれも
    気づかなかった）。数を返す。"""
    wait = timedelta(seconds=int(settings.system_value(tx, "verification.recording_wait"))) + STUCK_AFTER
    row = tx.one("SELECT count(*) AS n, min(ended_at) AS oldest FROM salescall.attempts"
                 " WHERE verification = 'pending' AND ended_at < now() - %s", (wait,))
    if row["n"]:
        fix_requests.request(tx, "CL-1", "verification_stuck", key="verification_stuck",
                             summary="通話後の検証が終わらない通話があります。その行とお客様は、検証が済むまでかけられません",
                             refs={"count": row["n"], "oldest_ended_at": row["oldest"]})
    return row["n"]


class RecordingWaitNotOver(Exception):
    """まだ録音を待つ時間の中（録音なしでは済ませない）。"""


def finish_without_recording(tx: Tx, attempt_id: UUID) -> None:
    """録音が届かないまま、通話の終わりから待つ時間（設定 verification.recording_wait＝10分）を過ぎた通話の確かめを、「録音なし」で
    済ませる（通話後の検証が呼ぶ。2026-10-18 ユーザー決定）。そのあと締めの処理が、通話中の報告だけで結果を決める。
    - 待つ時間を過ぎたかを決めるのは、ここの1か所。まだなら RecordingWaitNotOver（待ちのまま）。
    - 番号違いの確かめが要る通話は、付け合わせられないので cannot_check として記録する。
    - 録音が残らないこと自体は仕組みの不具合なので、エラーの入口に渡す（修正依頼。その通話は待たない）。"""
    wait = int(settings.system_value(tx, "verification.recording_wait"))
    row = tx.one("SELECT verification, ended_at IS NOT NULL AND ended_at + make_interval(secs => %s) <= now() AS over"
                 " FROM salescall.attempts WHERE attempt_id = %s", (wait, attempt_id))
    if row is None:
        raise AttemptNotFound(str(attempt_id))
    if row["verification"] != "pending":
        raise ValueError(f"確かめが待ちの通話ではありません（{row['verification']}）")
    if not row["over"]:
        raise RecordingWaitNotOver(f"通話の終わりから {wait} 秒たっていません（録音を待ちます）")
    if number_recheck.needed(tx, attempt_id) and number_recheck.load(tx, attempt_id) is None:
        number_recheck.record(tx, attempt_id, "cannot_check", corrected=None, evidence="録音が届かなかった（付け合わせられない）",
                              checked_by="通話後の検証（録音なし）")
    if target_recheck.missing(tx, attempt_id):   # 確かめられない＝その通話の答えは戻す・「-」
        target_recheck.record(tx, attempt_id, "cannot_check", evidence="録音が届かなかった（確かめ直せない）",
                              checked_by="通話後の検証（録音なし）")
    watch.record_error(tx, ErrorReport("recording", None, "録音が届かないまま、待つ時間を過ぎた"), attempt_id=attempt_id)
    record_verification(tx, attempt_id, summary=VerificationSummary(0, 0, 0, recording_missing=True))


def forbidden_speech(tx: Tx, attempt_id: UUID) -> bool:
    """その通話で、言ってはいけないことを言ったか（通話後の検証の数。判断はデータベースの1か所 salescall.said_forbidden）。"""
    return tx.one("SELECT salescall.said_forbidden(%s) AS f", (attempt_id,))["f"]


def record_disconnect(tx: Tx, attempt_id: UUID, disconnect: Literal["normal", "refused", "interrupted"]) -> None:
    """締めの処理（core/settle.py）だけが呼ぶ。判定が決めた切れ方（DESIGN.md 7-1c）を、終わった試行に1回だけ記録する。"""
    row = tx.one("UPDATE salescall.attempts SET disconnect = %s WHERE attempt_id = %s RETURNING attempt_id",
                 (disconnect, attempt_id))
    if row is None:
        raise AttemptNotFound(str(attempt_id))


def record_cut_off(tx: Tx, attempt_id: UUID, cut_off: bool) -> None:
    """締めの処理（core/settle.py）だけが呼ぶ。途中で切れたか（決めるのは judge.cut_off の1か所）を、終わった試行に1回だけ
    記録する（続けて何回、途中で切れたかを数える＝データベースの salescall.cuts_in_a_row）。"""
    row = tx.one("UPDATE salescall.attempts SET cut_off = %s WHERE attempt_id = %s RETURNING attempt_id", (cut_off, attempt_id))
    if row is None:
        raise AttemptNotFound(str(attempt_id))
