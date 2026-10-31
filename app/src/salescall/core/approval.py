"""承認の部分（検証モード。DESIGN.md 10-1 の3・10-2）。持ち主 CL-2。

- **いまどちらのモードか（設定 approval.human）を読むのは、この部品だけ**（I-13。app/checks が見張る）。門・締めの処理は、
  ここに問い合わせるだけ（判定の処理はモードを読まない＝両モードで1文字も変わらない）。業務設定の版を作るとき、AI の名前の
  読みの案を契約者が確かめるか（検証モードのあいだだけ。2026-10-16 ユーザー：「検証中はAIが認識したものを私が確認します」）も、
  ここに問い合わせる（`human_for_business`。検証の切り替えは1つ）。
- 発信の前（`task_cleared`。門が呼ぶ）：契約者の承認を使う業務では、契約者が承認した行だけを通す（承認の記録は `approve_task`）。
- 「確定した」の印（result_releases。名前は前のまま「送ってよい印」）を付けるのも、ここだけ。**この印は、依頼者へ送ることでは
  ない**：依頼者へ渡すのは、依頼者ごとの形に整えた最終のもの（報告・依頼者のシステムへの書き込み）だけで、いつ・どの形で渡すかは
  出力の型（CL-3。DESIGN.md 7-3）が決める。印の付いた版だけが、その最終のものに入る。検証の段階では、依頼者へは何も渡さない。
  依頼者に、途中の作業や仕組みのことを知らせない（2026-10-16 ユーザー：「依頼者にが途中作業していることは絶対に知らせてはいけない」
  「知れると契約破棄の可能性があります」）。
  - フルオート（`after_judged`。締めの処理が判定のあとに呼ぶ）：判定した版に自動で付ける（品質で止まっても、取れた結果は
    ふつうどおり。止めるのは新しい発信。2026-10-15 ユーザー：「取れた結果は、ふつうどおり扱う」）。
    ⛔ 2026-10-16 に外したもの：最初の段の結果を、段が進むまで待たせていた。段が埋まらない（一覧の行が少ない・版を替えた）と、
    いつまでも確定しなかった（ユーザー：「検証時は案B、本格稼働してから…送る」＝検証モードは契約者が確かめ、本格稼働は自動）。
  - 契約者の承認を使う業務（検証モード）：契約者が承認した版（`approve_result`）か、直した版（`correct_result`。契約者の版を足す。
    システムの版は残す）にだけ付ける。
  - **承認・直したことは、誤判定率の正解になる**（Intent.md 16-3「検証モードで契約者が直した結果も、正解として使う」）：システムが
    判定した版を承認したら「判定どおりが正解」、直したら「直した中身が正解」として `measure.record_check` に入れる（入れる所は
    measure の1か所。その通話の「測り終わるまで消さない」印も、そこで外れる）。
- **取り消せない決定の保留と答え**（2026-10-30 管理者のご決定 (6)(7)。表の作り 0152〜0154。書くのは、ここの1か所）：お客様の断り・番号違い・
  怒っていた（会話の報告で発信禁止や行の終わりになるもの。どれが取り消せない決定で、2つ目の判定とそろったかは core/judge.irreversible）
  のどの中身を待つかは `decision_wait` の1か所、保留を書くのは `record_wait` の1か所（締めの処理が判定するたびに呼ぶ。いまの報告と答えから
  待つ中身を決め、前の版と違えば版を足す＝報告が変わったら保留も決め直す。待たなくなったら中身が空の版）：
  - 検証モードの業務：そろっていても保留（approval）。契約者か管理者が録音とその場のやり取りを確かめて承認（`approve_decision`）を押した
    ときだけ確定し、差し戻し（`send_back_decision`＝断りではない）を押したら、その中身をその通話の報告から外して、ふつうに締める。
  - そのほか：会話AIの報告と2つ目の判定がそろったときだけ、その場で決める。そろわなければ保留（recording）。録音から作り直した報告が
    届いたら、締めの処理が締め直して保留を決め直す。締めは通話後の検証のあとなので、保留にした時点で録音の確かめでは決められなかった：
    契約者に録音とやり取りを確かめてもらう（`chase_recording`。録音が届かなかった・作り直してもそろわない・検証が作り直さなかった＝
    会話の担当へ修正依頼も）。⏳ フルオートの業務で契約者に求めることは、管理者に伺っている。
  - 誤って入れた発信禁止を取り消した（core/dnc.revoke・revoke_source＝依頼者からの正式な求めのときだけ）：その発信禁止の中身だけを
    外して締め直す（`withdraw_revoked`。ほかの中身の保留はそのまま）。
  答えは中身ごとに効き（承認した中身はもう待たない・外した中身は報告から外す）、答えたら締め直しが保留の版を決め直す。いまの保留の中身は
  データベースの salescall.decision_open の1か所（いちばん新しい版の中身）。保留のあいだは、発信禁止を登録しない・行を終えない（締めの処理と次の動き）・結果を確定しない（`after_judged`・確定の印の守り）・
  同じお客様の行（この行も、ほかの行も）を門で待たせる（データベースの salescall.decision_pending＝「まだ決まっていない」の1か所。切れ方が
  分からない通話の確かめのかけ直しも同じ1か所）。答えはどれも `_answer` の1か所を通り、締め直しは締めの処理の1か所（core/settle.resettle）。
- **行が終わったとき**（`after_ended`。次の動きが行を終えるたびに呼ぶ）：まだ確定の印の無い、いちばん新しい判定の版を、`after_judged`
  の同じ決まりで確かめ直す（切れ方が分からず、確かめのかけ直しが一度もかからないまま行が終わった通話の版は、もう決まっていない
  ものを待たない＝営業拒否の証拠が無いので、その通話の結果どおりで確定する。前の作業担当の残した問い (8)）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import fix_requests, measure, notify, results, settings, tasks
from salescall.core.access import Tx
from salescall.core.judge import Irreversible


def human_for_business(tx: Tx, business_id: UUID) -> bool:
    """その業務が検証モード（契約者の承認を使う）か。"""
    b = tx.one("SELECT tenant_id, client_id FROM salescall.businesses WHERE business_id = %s", (business_id,))
    if b is None:
        raise LookupError(f"その業務は無い（または、取引の範囲の外）：{business_id}")
    return bool(settings.resolve(tx, "approval.human", tenant_id=b["tenant_id"], client_id=b["client_id"],
                                 business_id=business_id))


def _human(tx: Tx, task_id: UUID) -> bool:
    return human_for_business(tx, tasks.scope(tx, task_id)["business_id"])


def task_cleared(tx: Tx, task_id: UUID) -> bool:
    """門が呼ぶ：この行へ発信してよいか（承認の分だけ）。フルオートなら常に通す。"""
    if not _human(tx, task_id):
        return True
    return tx.one("SELECT 1 FROM salescall.task_approvals WHERE task_id = %s", (task_id,)) is not None


def approve_task(tx: Tx, task_id: UUID) -> None:
    """契約者が行を承認する（契約者だけ。データベースが守る）。"""
    tx.execute("INSERT INTO salescall.task_approvals (tenant_id, client_id, task_id, approved_by)"
               " SELECT tenant_id, client_id, task_id, salescall.cur_actor() FROM salescall.tasks WHERE task_id = %s",
               (task_id,))


def _release(tx: Tx, task_id: UUID, version_no: int) -> None:
    """「確定した」の印を付ける（1か所）。検証の段階か本番かも印に持たせる（検証の段階の結果は、依頼者へ何も渡さない＝2026-10-16
    ユーザー。出力の型 CL-3 が、この印で分ける。2026-10-17 の計画書との突き合わせで足した）。段は、その版の通話をかけたときのモード
    （データベースの salescall.version_in_verification の1か所。守りも同じものを確かめる＝0161）。"""
    stage = "verification" if _in_verification(tx, task_id, version_no) else "production"
    tx.execute("INSERT INTO salescall.result_releases (tenant_id, client_id, task_id, version_no, released_by, stage)"
               " SELECT tenant_id, client_id, task_id, version_no, salescall.cur_actor(), %s FROM salescall.task_results"
               " WHERE task_id = %s AND version_no = %s ON CONFLICT DO NOTHING", (stage, task_id, version_no))


def _in_verification(tx: Tx, task_id: UUID, version_no: int) -> bool:
    """その版が検証の段か（その版の通話を、検証モードでかけたか。データベースの salescall.version_in_verification の1か所）。"""
    return tx.one("SELECT salescall.version_in_verification(%s, %s) AS v", (task_id, version_no))["v"]


def after_judged(tx: Tx, task_id: UUID, version_no: int, version_id: UUID) -> list[tuple[UUID, int]]:
    """締めの処理が呼ぶ（判定の版を足し、品質の段を評価したあと）。フルオートでかけた通話の版なら、その版に「確定した」の印を付ける。
    付けた（行, 版の番号）を返す（付けなければ空）。"""
    if _in_verification(tx, task_id, version_no):
        return []                                  # 検証モードでかけた通話：契約者が確かめる（かけたあとにモードを替えても同じ）
    if tx.one("SELECT business_state::text AS s FROM salescall.task_results WHERE task_id = %s AND version_no = %s",
              (task_id, version_no))["s"] == "unknown":
        return []                                  # 判定できなかった結果は確定しない（判定し直した版が確定する）
    if _blocked(tx, task_id, version_no):
        return []
    _release(tx, task_id, version_no)
    return [(task_id, version_no)]


def _blocked(tx: Tx, task_id: UUID, version_no: int) -> bool:
    """その版に確定の印を付けられないか（データベースの salescall.release_blocked の1か所。印の表の守りも同じものを読む）：その版の通話の
    取り消せない決定がまだ決まっていない（切れ方の確かめのかけ直し・保留。決まったときの締めの版が確定する）・あとの通話の結果がある前の
    通話の版（行の結果は、あとの通話の結果。断り・怒っていたで終えた版は除く。2026-10-30 の見直し24）。"""
    return tx.one("SELECT salescall.release_blocked(%s, %s) AS b", (task_id, version_no))["b"]


def approve_result(tx: Tx, task_id: UUID, version_no: int) -> None:
    """契約者の承認：契約者が、その結果の版を確かめて「送ってよい」にする。システムが判定した版なら、判定どおりが正解。確定の印を付け
    られない版（`_blocked`）は断る（ValueError）。"""
    if _blocked(tx, task_id, version_no):
        raise ValueError(f"その版には確定の印を付けられません（取り消せない決定がまだ決まっていない・あとの通話の結果がある）：{task_id} の版 {version_no}")
    _release(tx, task_id, version_no)
    v = tx.one("SELECT business_state::text AS state FROM salescall.task_results WHERE task_id = %s AND version_no = %s",
               (task_id, version_no))
    if v["state"] != "unknown":   # 判定できなかった版を承認しても、正解（業務の状態）は分からない
        measure.record_check(tx, task_id, version_no, truth_state=v["state"], wrong_items=[])


def correct_result(tx: Tx, task_id: UUID, *, checked_version: int, business_state: str, wrong_items: list[str],
                   complaint: bool, snapshot: dict[str, Any], detail: dict[str, Any] | None = None) -> int:
    """契約者の承認：契約者が、システムが判定した版（checked_version）を確かめて直す。契約者の版を足して（システムが判定した版は
    残す）、その版を送ってよいにする。直した中身（正しい業務の状態・違っていた項目＝質問の番号）は、その版の正解になる。"""
    v = tx.one("SELECT kind::text AS kind FROM salescall.task_results WHERE task_id = %s AND version_no = %s",
               (task_id, checked_version))
    if v is None or v["kind"] not in measure.SYSTEM_KINDS:   # システムが判定した版（決まりは measure の1か所）
        raise LookupError(f"直すのは、システムが判定した結果の版です：{task_id} の版 {checked_version}")
    if _blocked(tx, task_id, checked_version):
        # 確定の印を付けられない版（取り消せない決定がまだ決まっていない・あとの通話の結果がある・その通話の新しい版がある）は、直して
        # 確定しない（保留を、結果を直す道で越えない＝承認・差し戻しで答える。古い画面の版を直して、新しい結果を上書きしない＝0161）
        raise ValueError(f"その版は直して確定できません（取り消せない決定がまだ決まっていない・あとの通話の結果がある・その通話の新しい版が"
                         f"ある）：{task_id} の版 {checked_version}")
    version_no = results.append(tx, task_id, kind="human", business_state=business_state, attempt_id=None,
                                judge_version=None, complaint=complaint, snapshot=snapshot, decided_by="契約者",
                                detail=detail)
    _release(tx, task_id, version_no)
    measure.record_check(tx, task_id, checked_version, truth_state=business_state, wrong_items=wrong_items)
    return version_no


def after_ended(tx: Tx, task_id: UUID) -> list[tuple[UUID, int]]:
    """行が終わった（次の動きの end が呼ぶ）：いちばん新しい結果の版がシステムの判定の版で、まだ確定の印が無ければ、`after_judged` の
    同じ決まりで確かめ直す（1か所）。切れ方が分からず、確かめのかけ直しが一度もかからないまま行が終わった（門が終えた など）通話は、
    もう「まだ決まっていない」ではない（データベースの salescall.unclear_check_pending：行が終われば待たない）。営業拒否の証拠が無いので、
    営業拒否にせず、その通話の結果どおりで確定する（前の作業担当が残した問い (8)）。付けた（行, 版の番号）を返す。"""
    r = results.latest(tx, task_id)
    if r is None or r["kind"] not in measure.SYSTEM_KINDS or r["attempt_id"] is None:
        return []
    if tx.one("SELECT 1 FROM salescall.result_releases WHERE task_id = %s AND version_no = %s",
              (task_id, r["version_no"])) is not None:
        return []
    return after_judged(tx, task_id, r["version_no"], None)


# ── 取り消せない決定の保留と答え（2026-10-30 管理者のご決定 (6)(7)。表の作り 0152〜0154）
WAIT_KINDS = ("recording", "approval")   # 録音の確かめ待ち（録音で決められなければ契約者に求める）／契約者の承認待ち（検証モード）
ASK_WHY = {"recording_missing": "録音が届かず、録音で確かめられなかった",
           "recording_undecided": "録音から確かめ直しても、2つの判定がそろわなかった",
           "recording_unchecked": "通話後の検証が録音で確かめないまま済み、会話の担当へ直しを頼んだ"}


@dataclass(frozen=True)
class Wait:
    """その通話で待つ取り消せない決定（`decision_wait` の1か所が決める）。parts＝待つ中身（空なら待たない）。"""
    kind: str                    # WAIT_KINDS
    parts: frozenset[str]
    check: str                   # 会話の報告の、2つ目の判定がそろったか（judge.Irreversible.check）


def in_verification(tx: Tx, attempt_id: UUID) -> bool:
    """その通話を、検証モードでかけたか（データベースの salescall.attempt_in_verification の1か所＝門が、かけたときのモードで付けた検証の
    印。かけたあとにモードを替えても、その通話は、かけたときのモードのまま。確定の印の段も同じ印で決まる＝0161）。"""
    return tx.one("SELECT salescall.attempt_in_verification(%s) AS v", (attempt_id,))["v"]


def decides_on_spot(tx: Tx, attempt_id: UUID) -> bool:
    """その通話の取り消せない決定を、その場で決めてよいか（検証モードでかけた通話でないか＝`in_verification`）。通話の途中の「もうかけないで」
    の申告を、その場で台帳へ入れてよいかを決める（core/dnc.register_declared が読む。検証モードは、契約者が承認を押した場合だけ営業拒否と
    確定）。"""
    return not in_verification(tx, attempt_id)


def _answered(tx: Tx, attempt_id: UUID, outcome: str) -> frozenset[str]:
    return frozenset(p for r in tx.all("SELECT parts FROM salescall.decision_answers WHERE attempt_id = %s AND outcome = %s",
                                       (attempt_id, outcome)) for p in r["parts"])


def withdrawn(tx: Tx, attempt_id: UUID) -> frozenset[str]:
    """その通話の報告から外す、取り消せない決定の中身（差し戻し・取り消した。締めの処理が読む1か所）。"""
    return _answered(tx, attempt_id, "withdrawn")


def decision_wait(tx: Tx, task_id: UUID, attempt_id: UUID, irr: Irreversible | None) -> Wait | None:
    """その通話で、取り消せない決定のどの中身を待つか（1か所。締めの処理が判定するたびに呼ぶ。irr は、外すと答えた中身を報告から外した
    判定のもの）。契約者が承認した中身は、もう待たない（そろった扱い）。待たなければ None。
    - 検証モードの業務：取り消せない決定の中身すべて（そろっていても approval＝契約者の承認待ち）
    - そのほか：会話AIの報告と2つ目の判定がそろわなかった（食い違った・判定が無い）中身（recording＝録音の確かめ待ち）。そろわない中身が
      1つでもあれば、その通話の決定は全部、決まるまで行わない（発信禁止・行の終わり。締めの処理）"""
    if irr is None:
        return None
    confirmed = _answered(tx, attempt_id, "confirmed")
    if in_verification(tx, attempt_id):   # 検証モードでかけた通話（締めるときの業務のモードでなく、かけたときのモード＝0161）
        need, kind = irr.parts - confirmed, "approval"
    else:
        need, kind = (frozenset() if irr.check == "agreed" else irr.unsure - confirmed), "recording"
    return Wait(kind, frozenset(need), irr.check) if need else None


def record_wait(tx: Tx, a: dict[str, Any], wait: Wait | None, version_no: int | None, report_source: str | None) -> None:
    """保留を書く1か所（締めの処理が、判定するたびに呼ぶ）。いまの保留（いちばん新しい版）と、待つ中身・種類・判定に使った報告が違えば、
    新しい版を足す（待たなくなったら、中身が空の版＝もう待たない）。新しい承認待ちは契約者へ知らせ、新しい録音の確かめ待ちは、録音の
    確かめで決められるかを、すぐ確かめる（`chase_recording`）。"""
    cur = tx.one("SELECT seq, kind, parts, report_source FROM salescall.decision_waits WHERE attempt_id = %s"
                 " ORDER BY seq DESC LIMIT 1", (a["attempt_id"],))
    parts = sorted(wait.parts) if wait is not None else []
    if cur is None and wait is None:
        return
    if cur is not None and sorted(cur["parts"]) == parts and cur["report_source"] == report_source:
        return   # 種類（録音の確かめ待ち・承認待ち）は、その通話をかけたときのモードで決まり、変わらない（`in_verification`）
    if wait is None and cur is not None and not cur["parts"]:
        return
    seq = 1 if cur is None else cur["seq"] + 1
    tx.execute("SELECT salescall.settling(%s)", (a["attempt_id"],))   # 締めの入口の印（データベースの守りが確かめる＝0161）
    tx.execute("INSERT INTO salescall.decision_waits (tenant_id, client_id, attempt_id, seq, task_id, kind, parts, checked, version_no,"
               " report_source) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, %s)",
               (a["attempt_id"], seq, a["task_id"], cur["kind"] if wait is None else wait.kind, parts,
                "agreed" if wait is None else wait.check, version_no, report_source))
    if wait is None:
        return
    if wait.kind == "approval":
        _tell_contractor(tx, a["attempt_id"], seq, "検証モードの業務で、お客様の断り・番号違い・怒っていたを確かめる通話があります")
    else:
        chase_recording(tx, a["attempt_id"])


def chase_recording(tx: Tx, attempt_id: UUID) -> None:
    """録音の確かめ待ちの保留を、録音の確かめで決められないまま待たせない（1か所。保留の版を足したとき・決まった間隔の仕事が呼ぶ）。
    締めの処理は通話後の検証が済んでから締めるので、保留にした報告は、検証が録音で確かめたあとのもの。録音で決められない理由を見て、
    契約者に録音（あれば）とその場のやり取りの文字を確かめてもらう（`_ask_contractor`）：
    - 録音が届かなかった（通話後の検証が録音なしで済んだ）＝recording_missing
    - 録音から作り直した報告がある（作り直してもそろわない）＝recording_undecided
    - 録音はあるのに、通話後の検証が作り直した報告を返さないまま済んだ＝recording_unchecked（あわせて会話の担当へ修正依頼。直して作り直した
      報告が届けば、締め直しで決まる。CL-2 の連絡帳 4-167 の頼み3・5。同じ不具合は、直したと報告するまで1つ）"""
    v = tx.one("SELECT a.task_id, coalesce(s.recording_missing, false) AS missing,"
               "       EXISTS (SELECT 1 FROM salescall.call_report_rebuilds b WHERE b.attempt_id = a.attempt_id) AS rebuilt"
               " FROM salescall.attempts a LEFT JOIN salescall.verification_summaries s ON s.attempt_id = a.attempt_id"
               " WHERE a.attempt_id = %s", (attempt_id,))
    if v["missing"]:
        why = "recording_missing"
    elif v["rebuilt"]:
        why = "recording_undecided"
    else:
        why = "recording_unchecked"
        fix_requests.request(tx, "CL-1", "irreversible_unchecked", key="irreversible_unchecked",
                             refs={"attempt_id": attempt_id, "task_id": v["task_id"]},
                             summary="会話AIの報告と2つ目の判定がそろわなかった（食い違った・判定が無い）お客様の断り・番号違い・怒っていたを、"
                                     "通話後の検証が録音で確かめないまま済ませました（録音から作り直した報告で返してください。それまで、"
                                     "契約者の確かめを待ち、そのお客様にはかけません）")
    _ask_contractor(tx, attempt_id, why)


def _ask_contractor(tx: Tx, attempt_id: UUID, why: str) -> None:
    """いまの録音の確かめ待ちの保留を、契約者に確かめてもらう印（1か所。0153・0154）。印のある保留は、検証モードの承認待ちと同じく、
    契約者が録音（あれば）とその場のやり取りの文字を確かめて、承認か差し戻しを押すまで保留のまま（2026-10-30 管理者のご決定 (6)「人が
    録音やその場のやり取りを確認できる状態にする」）。同じ版に印は1つ。"""
    if why not in ASK_WHY:
        raise ValueError(f"確かめてもらう理由は {tuple(ASK_WHY)} のどれかです：{why!r}")
    seq = tx.one("SELECT salescall.decision_wait_seq(%s) AS s", (attempt_id,))["s"]
    if tx.one("INSERT INTO salescall.decision_asks (tenant_id, client_id, attempt_id, seq, why)"
              " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s) ON CONFLICT (attempt_id, seq) DO NOTHING"
              " RETURNING attempt_id", (attempt_id, seq, why)) is not None:
        _tell_contractor(tx, attempt_id, seq, f"お客様の断り・番号違い・怒っていたを、{ASK_WHY[why]}通話があります")


def _tell_contractor(tx: Tx, attempt_id: UUID, seq: int, what: str) -> None:
    """契約者に、承認か差し戻しを押してもらう知らせ（1か所。保留の版1つに1つ。プログラムの誤りで作り直されないときも含むので、管理者へも）。"""
    w = tx.one("SELECT task_id, parts FROM salescall.decision_waits WHERE attempt_id = %s AND seq = %s", (attempt_id, seq))
    notify.emit(tx, "decision_approval", key=f"decision_approval:{attempt_id}:{seq}", admin=True,
                refs={"attempt_id": attempt_id, "seq": seq, "task_id": w["task_id"], "parts": sorted(w["parts"])},
                summary=f"{what}（録音とやり取りを確かめて、承認か差し戻しを押してください。押すまで、このお客様にはかけません）。")


def pending_decisions(tx: Tx) -> list[dict[str, Any]]:
    """答えを待っている保留の一覧（取引の依頼者の分。画面＝CL-3 が、承認の一覧に出す）。いまの保留の版ごとに：通話・行・版（seq。承認と
    差し戻しに渡す）・種類・待っている中身（parts）・2つ目の判定・保留にした結果の版・時刻・契約者に答えを求めているか（ask＝承認か
    差し戻しのボタンを出す）・録音で確かめられなかった理由（ask_why＝ASK_WHY）。決まりはデータベースの salescall.decision_open・
    decision_asks_contractor の1か所。"""
    return tx.all("SELECT w.attempt_id, w.task_id, w.seq, w.kind, salescall.decision_open(w.attempt_id) AS parts, w.checked,"
                  "       w.version_no, w.waited_at, salescall.decision_asks_contractor(w.attempt_id) AS ask, k.why AS ask_why"
                  " FROM salescall.decision_waits w"
                  " LEFT JOIN salescall.decision_asks k ON k.attempt_id = w.attempt_id AND k.seq = w.seq"
                  " WHERE w.seq = salescall.decision_wait_seq(w.attempt_id) AND cardinality(salescall.decision_open(w.attempt_id)) > 0"
                  " ORDER BY w.waited_at, w.attempt_id")


def _open_for_contractor(tx: Tx, attempt_id: UUID, seq: int) -> dict[str, Any]:
    """契約者に答えを求めている、いまの保留の版（画面が見た版＝seq でなければ断る：報告が変わって版が替わった）。"""
    w = tx.one("SELECT w.task_id, salescall.decision_open(w.attempt_id) AS parts FROM salescall.decision_waits w"
               " WHERE w.attempt_id = %s AND w.seq = %s AND w.seq = salescall.decision_wait_seq(w.attempt_id)"
               "   AND salescall.decision_asks_contractor(w.attempt_id)", (attempt_id, seq))
    if w is None:
        raise LookupError(f"その通話に、承認か差し戻しを求めている、いまの保留（版 {seq}）はありません：{attempt_id}")
    return w


def _answer(tx: Tx, attempt_id: UUID, *, outcome: str, parts: list[str], by: str, why: str | None = None,
            seq: int | None = None) -> dict[str, Any]:
    """答えを残して、その通話を締め直す（答えの入口はどれもここを通る。締め直しは締めの処理の1か所 core/settle.resettle）。"""
    from salescall.core import settle   # 締めの処理がこの部品を読むので、ここで読む（読み合いにしない）
    tx.execute("INSERT INTO salescall.decision_answers (tenant_id, client_id, attempt_id, outcome, parts, by_kind, why, answered_by,"
               " wait_seq) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, salescall.cur_actor(), %s)",
               (attempt_id, outcome, sorted(set(parts)), by, why, seq))
    return settle.resettle(tx, attempt_id)


def approve_decision(tx: Tx, attempt_id: UUID, seq: int) -> dict[str, Any]:
    """契約者か管理者の承認（契約者に答えを求めている保留）：録音とその場のやり取りを確かめて、その版の中身（断り・番号違い・怒っていた）を
    確定する。発信禁止の登録・行の終わりは締め直しが行う。結果の版の承認ではない（確かめたのは取り消せない決定だけ。
    締め直した版は、検証モードでは、ほかの結果と同じく確かめる）。締め直しの答えを返す。"""
    w = _open_for_contractor(tx, attempt_id, seq)
    return _answer(tx, attempt_id, outcome="confirmed", parts=w["parts"], by="approval", seq=seq)


def send_back_decision(tx: Tx, attempt_id: UUID, seq: int) -> dict[str, Any]:
    """契約者か管理者の差し戻し（契約者に答えを求めている保留）：その版の中身は、断り（番号違い・怒っていた）ではなかった。
    その中身をその通話の報告から外して、ふつうに締め直す（発信禁止を登録しない・行はふつうの次の動き）。その場で決めていたはずの決定
    （2つ目の判定とそろっていた）を差し戻したことは、その通話の正解に「違っていた」として必ず入る（`sent_back_on_spot` を
    `measure.record_check` が読む）。締め直しの答えを返す。"""
    w = _open_for_contractor(tx, attempt_id, seq)
    return _answer(tx, attempt_id, outcome="withdrawn", parts=w["parts"], by="approval", seq=seq)


def sent_back_on_spot(tx: Tx, attempt_id: UUID) -> frozenset[str]:
    """その通話で、契約者が差し戻した中身のうち、システムがその場で決めていたはずのもの（答えた保留の版で、2つ目の判定とそろっていた＝
    フルオートなら、そのまま発信禁止・行の終わりになっていた）。誤判定率の正解に「違っていた」として入れる（measure の1か所が読む）。"""
    return frozenset(p for r in tx.all(
        "SELECT x.parts FROM salescall.decision_answers x"
        " JOIN salescall.decision_waits w ON w.attempt_id = x.attempt_id AND w.seq = x.wait_seq"
        " WHERE x.attempt_id = %s AND x.by_kind = 'approval' AND x.outcome = 'withdrawn' AND w.checked = 'agreed'",
        (attempt_id,)) for p in r["parts"])


def withdraw_revoked(tx: Tx, attempt_id: UUID, parts: list[str], why: str) -> dict[str, Any]:
    """誤って入れた発信禁止を取り消した（core/dnc の取り消しの1か所が呼ぶ。その通話から入れた発信禁止がすべて取り消されたとき）：その
    通話の決定のうち、その発信禁止の中身だけを報告から外して締め直す（ほかの中身の保留は、そのまま。その発信禁止で終えた行は予定に戻り、
    依頼者へ渡す結果の「営業拒否」も外れる＝2026-10-30 (7)）。"""
    return _answer(tx, attempt_id, outcome="withdrawn", parts=parts, by="revoke", why=why)
