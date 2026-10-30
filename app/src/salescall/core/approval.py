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
- **取り消せない決定の保留と答え**（2026-10-30 管理者のご決定 (6)(7)。表の作り 0152。書くのは、ここの1か所）：お客様の断り・番号違い・
  怒っていた（会話の報告で発信禁止や行の終わりになるもの。どれが取り消せない決定で、2つ目の判定とそろったかは core/judge.irreversible）
  を、その場で決めるか・保留にするかは `decision_wait` の1か所：
  - 検証モードの業務：そろっていても保留（approval）。契約者が録音とその場のやり取りを確かめて承認（`confirm_decision`）を押したときだけ
    確定し、差し戻し（`send_back_decision`＝断りではない）を押したら、その決定をその通話の報告から外して、ふつうに締める。
  - そのほか：会話AIの報告と2つ目の判定がそろったときだけ、その場で決める。そろわなければ保留（recording）。通話後の録音の確かめ
    （録音から作り直した報告）で決まったら、締めの処理が `answer_from_recording` を呼ぶ。
  - 誤って入れた発信禁止を取り消した（core/dnc.revoke・revoke_source＝依頼者からの正式な求めのときだけ）：その通話の決定を外して
    締め直す（`withdraw_revoked`）。
  保留のあいだは、発信禁止を登録しない・行を終えない（締めの処理と次の動き）・結果を確定しない（`after_judged`）・同じお客様のほかの
  行も待たせる（データベースの salescall.decision_pending＝「まだ決まっていない」の1か所。切れ方が分からない通話の確かめのかけ直しも
  同じ1か所）。答えはどれも `_answer` の1か所を通り、締め直しは締めの処理の1か所（core/settle.resettle）。
- **行が終わったとき**（`after_ended`。次の動きが行を終えるたびに呼ぶ）：まだ確定の印の無い、いちばん新しい判定の版を、`after_judged`
  の同じ決まりで確かめ直す（切れ方が分からず、確かめのかけ直しが一度もかからないまま行が終わった通話の版は、もう決まっていない
  ものを待たない＝営業拒否の証拠が無いので、その通話の結果どおりで確定する。前の作業担当の残した問い (8)）。
"""

from __future__ import annotations

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
    ユーザー。出力の型 CL-3 が、この印で分ける。2026-10-17 の計画書との突き合わせで足した）。"""
    stage = "verification" if _human(tx, task_id) else "production"
    tx.execute("INSERT INTO salescall.result_releases (tenant_id, client_id, task_id, version_no, released_by, stage)"
               " SELECT tenant_id, client_id, task_id, version_no, salescall.cur_actor(), %s FROM salescall.task_results"
               " WHERE task_id = %s AND version_no = %s ON CONFLICT DO NOTHING", (stage, task_id, version_no))


def after_judged(tx: Tx, task_id: UUID, version_no: int, version_id: UUID) -> list[tuple[UUID, int]]:
    """締めの処理が呼ぶ（判定の版を足し、品質の段を評価したあと）。フルオートなら、その版に「確定した」の印を付ける。
    付けた（行, 版の番号）を返す（付けなければ空）。"""
    if _human(tx, task_id):
        return []                                  # 検証モード：契約者が確かめる
    if tx.one("SELECT business_state::text AS s FROM salescall.task_results WHERE task_id = %s AND version_no = %s",
              (task_id, version_no))["s"] == "unknown":
        return []                                  # 判定できなかった結果は確定しない（判定し直した版が確定する）
    if tx.one("SELECT salescall.decision_pending(r.attempt_id) AS p FROM salescall.task_results r"
              " WHERE r.task_id = %s AND r.version_no = %s", (task_id, version_no))["p"]:
        # 取り消せない決定がまだ決まっていない通話の結果は確定しない（切れ方が分からず確かめのかけ直しを待っている・2つ目の判定と
        # そろわず録音の確かめを待っている。2026-10-30 管理者の決定。決まったときの締めの版が確定する。決まりはデータベースの
        # salescall.decision_pending の1か所）
        return []
    _release(tx, task_id, version_no)
    return [(task_id, version_no)]


def approve_result(tx: Tx, task_id: UUID, version_no: int) -> None:
    """契約者の承認：契約者が、その結果の版を確かめて「送ってよい」にする。システムが判定した版なら、判定どおりが正解。"""
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


# ── 取り消せない決定の保留と答え（2026-10-30 管理者のご決定 (6)(7)。表の作り 0152）
WAIT_KINDS = ("recording", "approval")   # 録音の確かめ待ち／契約者の承認待ち


def decides_on_spot(tx: Tx, task_id: UUID) -> bool:
    """その行の取り消せない決定を、その場で決めてよい業務か（検証モードでないか）。通話の途中の「もうかけないで」の申告を、その場で
    台帳へ入れてよいかを決める（core/dnc.register_declared が読む。検証モードは、契約者が承認を押した場合だけ営業拒否と確定）。"""
    return not _human(tx, task_id)


def decision_wait(tx: Tx, task_id: UUID, attempt_id: UUID, irr: Irreversible | None) -> str | None:
    """その通話の取り消せない決定を、保留にするか（1か所。締めの処理が判定のあとに呼ぶ）。保留の種類（WAIT_KINDS）か、その場で決める
    なら None。もう答えのある通話（承認・差し戻し・録音の確かめ・取り消しのあと）は保留にしない（答えの中身は締めの処理が読む）。
    - 検証モードの業務：取り消せない決定があれば、そろっていても approval（契約者の承認待ち）
    - そのほか：会話AIの報告と2つ目の判定がそろわなかった（食い違った・判定が無い）中身があれば recording（録音の確かめ待ち）"""
    if irr is None or answered(tx, attempt_id):
        return None
    if _human(tx, task_id):
        return "approval"
    return None if irr.check == "agreed" else "recording"


def answered(tx: Tx, attempt_id: UUID) -> bool:
    """その通話の取り消せない決定に、もう答えがあるか。"""
    return tx.one("SELECT 1 FROM salescall.decision_answers WHERE attempt_id = %s LIMIT 1", (attempt_id,)) is not None


def withdrawn(tx: Tx, attempt_id: UUID) -> frozenset[str]:
    """その通話の報告から外す、取り消せない決定の中身（差し戻し・録音の確かめで決まらなかった・取り消した。締めの処理が読む1か所）。"""
    return frozenset(p for r in tx.all("SELECT parts FROM salescall.decision_answers WHERE attempt_id = %s AND outcome = 'withdrawn'",
                                       (attempt_id,)) for p in r["parts"])


def hold_decision(tx: Tx, a: dict[str, Any], kind: str, irr: Irreversible, version_no: int | None) -> None:
    """保留を始める（締めの処理が呼ぶ1か所。同じ通話に保留は1つ）。契約者の承認待ちは、契約者へ知らせる（承認するまで、そのお客様に
    かけない）。録音の確かめ待ちで、通話後の検証がもう済んでいる（検証が録音で確かめずに済ませた）なら、止めずに会話の担当へ修正依頼
    （録音から作り直した報告で返してもらう。CL-2 の連絡帳 4-167 の頼み3・5。同じ不具合は、直したと報告するまで1つ）。"""
    if kind not in WAIT_KINDS:
        raise ValueError(f"保留の種類は {WAIT_KINDS} のどれかです：{kind!r}")
    parts = irr.parts if kind == "approval" else irr.unsure
    tx.execute("INSERT INTO salescall.decision_waits (tenant_id, client_id, attempt_id, task_id, kind, parts, checked, version_no)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s) ON CONFLICT (attempt_id) DO NOTHING",
               (a["attempt_id"], a["task_id"], kind, sorted(parts), irr.check, version_no))
    refs = {"attempt_id": a["attempt_id"], "task_id": a["task_id"], "parts": sorted(parts)}
    if kind == "approval":
        notify.emit(tx, "decision_approval", key=f"decision_approval:{a['attempt_id']}", refs=refs,
                    summary="検証モードの業務で、お客様の断り・番号違い・怒っていたを確かめる通話があります（録音とやり取りを確かめて、"
                            "承認か差し戻しを押してください。押すまで、このお客様にはかけません）。")
    elif tx.one("SELECT verification::text AS v FROM salescall.attempts WHERE attempt_id = %s", (a["attempt_id"],))["v"] == "done":
        fix_requests.request(tx, "CL-1", "irreversible_unchecked", key="irreversible_unchecked", refs=refs,
                             summary="会話AIの報告と2つ目の判定がそろわなかった（食い違った・判定が無い）お客様の断り・番号違い・怒っていたを、"
                                     "通話後の検証が録音で確かめないまま済ませました（録音から作り直した報告で返してください。それまで、"
                                     "そのお客様にはかけません）")


def pending_decisions(tx: Tx) -> list[dict[str, Any]]:
    """答えを待っている保留の一覧（取引の依頼者の分。画面＝CL-3 が、承認の一覧に出す）。種類・中身・2つ目の判定・保留にした結果の版。"""
    return tx.all("SELECT w.attempt_id, w.task_id, w.kind, w.parts, w.checked, w.version_no, w.waited_at FROM salescall.decision_waits w"
                  " WHERE NOT EXISTS (SELECT 1 FROM salescall.decision_answers x WHERE x.attempt_id = w.attempt_id)"
                  " ORDER BY w.waited_at, w.attempt_id")


def _wait(tx: Tx, attempt_id: UUID, kind: str) -> dict[str, Any]:
    w = tx.one("SELECT attempt_id, task_id, kind, parts FROM salescall.decision_waits w WHERE attempt_id = %s AND kind = %s"
               " AND NOT EXISTS (SELECT 1 FROM salescall.decision_answers x WHERE x.attempt_id = w.attempt_id)", (attempt_id, kind))
    if w is None:
        raise LookupError(f"その通話に、答えを待っている保留（{kind}）はありません：{attempt_id}")
    return w


def _answer(tx: Tx, attempt_id: UUID, *, outcome: str, parts: list[str], by: str, why: str | None = None) -> dict[str, Any]:
    """答えを残して、その通話を締め直す（答えの入口はどれもここを通る。締め直しは締めの処理の1か所 core/settle.resettle）。"""
    from salescall.core import settle   # 締めの処理がこの部品を読むので、ここで読む（読み合いにしない）
    tx.execute("INSERT INTO salescall.decision_answers (tenant_id, client_id, attempt_id, outcome, parts, by_kind, why, answered_by)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, salescall.cur_actor())",
               (attempt_id, outcome, sorted(set(parts)), by, why))
    return settle.resettle(tx, attempt_id)


def confirm_decision(tx: Tx, attempt_id: UUID) -> dict[str, Any]:
    """契約者の承認（検証モード）：録音とその場のやり取りを確かめて、その通話の断り・番号違い・怒っていたを確定する。発信禁止の登録・
    行の終わりは締め直しが行い、締め直した版に確定の印を付ける（システムの判定どおりが正解＝`approve_result`）。締め直しの答えを返す。"""
    w = _wait(tx, attempt_id, "approval")
    out = _answer(tx, attempt_id, outcome="confirmed", parts=w["parts"], by="approval")
    if out.get("version_no") is not None:
        approve_result(tx, w["task_id"], out["version_no"])
    return out


def send_back_decision(tx: Tx, attempt_id: UUID) -> dict[str, Any]:
    """契約者の差し戻し（検証モード）：断り（番号違い・怒っていた）ではなかった。その決定をその通話の報告から外して、ふつうに締め直す
    （発信禁止を登録しない・行はふつうの次の動き）。締め直した版は、ほかの結果と同じく契約者が承認する。締め直しの答えを返す。"""
    w = _wait(tx, attempt_id, "approval")
    return _answer(tx, attempt_id, outcome="withdrawn", parts=w["parts"], by="approval")


def answer_from_recording(tx: Tx, attempt_id: UUID, *, confirmed: bool) -> dict[str, Any]:
    """録音の確かめの答え（締めの処理の決まった間隔の仕事が、録音から作り直した報告を読んで呼ぶ。システムの処理だけ）：その決定が2つ目の
    判定とそろった（confirmed）＝決まりどおり／報告から消えた＝元に戻す（作り直した報告のまま、ふつうに締める）。"""
    w = _wait(tx, attempt_id, "recording")
    return _answer(tx, attempt_id, outcome="confirmed" if confirmed else "withdrawn", parts=w["parts"], by="recording")


def withdraw_revoked(tx: Tx, attempt_id: UUID, parts: list[str], why: str) -> dict[str, Any]:
    """誤って入れた発信禁止を取り消した（core/dnc の取り消しの1か所が呼ぶ。その通話から入れた発信禁止がすべて取り消されたとき）：その
    通話の決定を報告から外して締め直す（その発信禁止で終えた行は予定に戻り、依頼者へ渡す結果の「営業拒否」も外れる＝2026-10-30 (7)）。"""
    return _answer(tx, attempt_id, outcome="withdrawn", parts=parts, by="revoke", why=why)
