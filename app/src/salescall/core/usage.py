"""利用量の台帳と予算（Q16「いくら使ったか」。DESIGN.md 12-1・9-1 の budget）。持ち主 CL-2。

- 台帳に書くのは、この部品の `record` だけ（I-12。app/checks が見張る）。単価を足すのは `set_price` だけ。
- **費用の単価は契約者ごと**（2026-10-16 ユーザー：「契約者　使用するAIのAPIと回線を選べます。楽天リンクは初期設定」「APIも無料枠と
  有料枠が選択可能」）：単価は、サービス（会話AIの API・別の耳・回線の方式）と、会話AI・別の耳の枠（無料・有料）ごと。契約者が
  入れた単価が、管理者が入れた初めの値より強い。意味の判定に使う AI（judge.models）も、枠ごとの単価を持つ（0102）。どの単価を使うかは `price_for` の1か所。報酬の単価は依頼者ごと（core/reward.py）。
- 1行ごとに：出どころ（会話AI・別の耳・意味の判定＝AI の判定。AI の自分の言葉の中身の判定も、同じモデル・同じ単価なのでここ＝CL-1 の 4-30・回線）・実測か推定か・量・単位・使った単価・金額。金額は記録したときの単価で決まる。
  単価が無い組でも止めない：量だけ記録し、金額は空（推測で 0円にしない）。契約者へ「設定がされていません」の注意を知らせる
  （1日1回。2026-10-16 ユーザー：「止めるというのは、このまま進めると、お客様に対して、影響が出てしまうということだけ」）。
- 回線：Rakuten Link は明細を返さないので、締めの処理が「話していた秒数 × 単価」の推定を書く（`record_line_estimate`）。
  会話AI・別の耳・意味の判定：返ってきた量を実測として、返らなければ推定として、会話の処理・検証・判定（CL-1）が `record` を呼ぶ。
- 予算（1日の上限。日本時間の日）：依頼者の上限を超えたら、その依頼者の契約者へ注意を知らせる（1日1回）。契約者の上限（その契約者の
  すべての依頼者の合計）は、見守りが呼ぶ `tick` がその契約者へ注意を知らせる。**どちらも止めない**（2026-10-16 ユーザー：「注意喚起のみ」。
  お客様には影響しない）。**費用の見張りは契約者ごと**（2026-10-18 ユーザー：「費用の見張りと控えは、契約者ごとですよ」＝費用は契約者が
  払う。`tick` は中央の見守り役が、契約者ごとに、その契約者の分だけをする。すべての契約者の合計は、誰が払う額でもないので見ない）。
- **報酬に対する費用の割合**（2026-10-15 ユーザー決定。今月の数は core/reward.py の `month`）：記録のたびに、その依頼者の今月を見る。
  どちらの判断も、成果・話せた本数のぶれの余裕を足した報酬（`upper`。余裕は依頼者ごとの報酬の決まりの「赤字の知らせの早さ」）で行う
  ＝判断し始める本数を決め打ちにせず、報酬と費用から逆算される。
  - 赤字（今月の費用 ＞ 多めに見込んだ報酬）⇒ まず警告して契約者へ知らせる。期限（設定 budget.deficit_grace＝1時間）までに
    管理者か契約者が確認ボタンを押さなければ、もう一度、契約者と管理者へ知らせる（1回。**止めない**＝2026-10-22 深夜 ユーザー：
    「赤字で、知らせてから1時間たっても確認ボタンが押されないとき：止めない。知らせるだけでいい」。⛔ 前は、その依頼者を止めていた
    ＝2026-10-16 の「1時間押さなければ止める」を変えた）。確認は設定 budget.deficit_confirm_valid の間だけ効き、そのあともまだ赤字
    なら、もう一度警告する。進め方の判断は `_deficit_step` の1か所（利用量の記録のたびと、見守りの `tick` が呼ぶ＝記録が無くても
    期限が来たら知らせる）。
  - 報酬の決まりが無い依頼者は、赤字かを判断できないので、止めずに「設定がされていません」の注意を知らせる（1日1回。2026-10-16
    ユーザー：止めるのは、お客様に影響が出るときだけ。前は報酬 0円とみなして止めていた）。
  - 赤字でなく、割合（費用 ÷ 多めに見込んだ報酬）が上限（budget.max_cost_ratio。既定 30％。依頼者ごとに選べる）を超えた
    ⇒ 知らせるだけ（1日1回）。
- 画面は台帳を読み、実測と推定を分けて出す（通話時間から料金を計算し直さない）。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from salescall.core import notify, reward, settings
from salescall.core.access import Access, Actor, Tx


class PriceMissing(Exception):
    """その出どころ・単位の単価が無い（記録できない）。"""


def set_price(tx: Tx, *, source: str, service: str, tier: str | None, unit: str, yen_per_unit: Decimal, valid_from: Any,
              note: str) -> int:
    """単価を足す。全体の取引なら、どの契約者にも効く初めの値（管理者だけ）。契約者の取引なら、その契約者の単価（契約者が自分の
    選んだサービスの単価を入れる。初めの値より強い）。あとから足した単価は、その時刻からの記録に使う。
    service：会話AIの API・別の耳・意味の判定の AI・回線の方式（例 gemini-3.8-live・rakuten_link）。tier：会話AI・別の耳・
    意味の判定の無料枠（free）か有料枠（paid）。回線は None。"""
    return tx.one("INSERT INTO salescall.price_versions (tenant_id, source, service, tier, unit, yen_per_unit, valid_from, note,"
                  " set_by) VALUES (salescall.cur_tenant(), %s, %s, %s, %s, %s, %s, %s, salescall.cur_actor())"
                  " RETURNING price_id", (source, service, tier, unit, yen_per_unit, valid_from, note))["price_id"]


def price_table(tx: Tx) -> list[dict[str, Any]]:
    """画面の単価の表（2026-10-21 ユーザー：「ダッシュボードでも変更や保存ができるようにしておいてください」）：出どころ・サービス・
    枠・単位ごとに、いま使う単価（決め方は `price_for` と同じ）と、それが契約者の入れた値か初めの値か。変えるときは `set_price`
    で足す（前の値は消さずに残る）。全体の取引なら初めの値だけ。"""
    return tx.all(
        "SELECT DISTINCT ON (source, service, tier, unit) source, service, tier, unit, yen_per_unit, valid_from,"
        " tenant_id IS NOT NULL AS by_contractor, note FROM salescall.price_versions"
        " WHERE valid_from <= now() AND (tenant_id IS NULL OR tenant_id = salescall.cur_tenant())"
        " ORDER BY source, service, tier NULLS FIRST, unit, tenant_id IS NULL, valid_from DESC")


def price_for(tx: Tx, *, source: str, service: str, tier: str | None, unit: str) -> dict[str, Any]:
    """いま使う単価（決め方はここだけ）：取引の契約者が入れた単価 ＞ 管理者が入れた初めの値。無ければ PriceMissing
    （推測で 0円にしない）。"""
    price = tx.one("SELECT price_id, yen_per_unit FROM salescall.price_versions WHERE source = %s AND service = %s"
                   " AND tier IS NOT DISTINCT FROM %s AND unit = %s AND valid_from <= now()"
                   " AND (tenant_id IS NULL OR tenant_id = salescall.cur_tenant())"
                   " ORDER BY tenant_id IS NULL, valid_from DESC LIMIT 1", (source, service, tier, unit))
    if price is None:
        raise PriceMissing(f"単価がありません：{source}・{service}・{tier or '-'}・{unit}")
    return price


def record(tx: Tx, *, source: str, service: str, tier: str | None, kind: str, quantity: Decimal | int, unit: str,
           attempt_id: UUID | None = None) -> dict[str, Any]:
    """利用量を1行記録する（取引の依頼者の分）。使ったサービスと枠（無料・有料）の単価で金額を決める（price_for）。
    単価が入っていなければ、量だけ記録して金額は空にし、注意を知らせる（止めない）。依頼者の1日の上限を超えたら注意を知らせる
    （止めない）。赤字なら警告し、確認されなければ期限のあとで、もう一度知らせる（`_deficit_step`。止めない）。"""
    try:
        price = price_for(tx, source=source, service=service, tier=tier, unit=unit)
    except PriceMissing:
        price = {"price_id": None, "yen_per_unit": None}
        day = tx.today()
        notify.emit(tx, "setting_missing", key=f"price_missing:{source}:{service}:{tier}:{unit}:{day}",
                    summary=f"費用の単価が設定されていません（{source}・{service}・{tier or '-'}・{unit}）。費用は量だけ記録しています",
                    refs={"source": source, "service": service, "tier": tier, "unit": unit})
    amount = None if price["yen_per_unit"] is None else Decimal(quantity) * Decimal(price["yen_per_unit"])
    row = tx.one(
        "INSERT INTO salescall.usage_entries (tenant_id, client_id, attempt_id, source, service, tier, kind, quantity, unit,"
        " price_id, amount_yen) VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING entry_id, tenant_id, client_id",
        (attempt_id, source, service, tier, kind, quantity, unit, price["price_id"], amount))
    _check_client_budget(tx, row["tenant_id"], row["client_id"])
    _check_cost_ratio(tx, row["tenant_id"], row["client_id"])
    return {"entry_id": row["entry_id"], "amount_yen": amount}


def spent_today(tx: Tx) -> Decimal:
    """取引の依頼者の、今日（日本時間）の金額。"""
    return tx.one("SELECT coalesce(sum(amount_yen), 0) AS y FROM salescall.usage_entries"
                  " WHERE salescall.jst(occurred_at)::date = salescall.jst(now())::date")["y"]


def _check_client_budget(tx: Tx, tenant_id: UUID, client_id: UUID) -> None:
    """依頼者の今日の費用が上限を超えたら、注意を知らせる（1日1回。止めない＝2026-10-16 ユーザー：「注意喚起のみ」）。"""
    limit = Decimal(settings.resolve(tx, "budget.client_daily_yen", tenant_id=tenant_id, client_id=client_id,
                                     business_id=None))
    spent = spent_today(tx)
    if spent > limit:
        day = tx.today()
        notify.emit(tx, "budget", key=f"client_daily:{client_id}:{day}",
                    summary=f"今日の費用が上限を超えました（{spent}円 ＞ {limit}円）。発信は止めていません",
                    refs={"client_id": client_id, "spent": str(spent), "limit": str(limit)})


DEFICIT, NOT_DEFICIT, UNKNOWN = "deficit", "not_deficit", "unknown"


def _deficit(tx: Tx, tenant_id: UUID, client_id: UUID) -> tuple[str, dict[str, Any]]:
    """取引の依頼者が、いま赤字か（**判断は1か所**：費用 ＞ 多めに見込んだ報酬）と、今月の数。いま効いている報酬の決まりが無ければ
    判断できない（UNKNOWN）：警告を新しくはしない（赤字では止めない＝2026-10-22 ユーザー「止めない。知らせるだけでいい」）。設定の
    抜けとして知らせる（`_check_cost_ratio`）。2026-10-18 の見直し：前は、決まりが無いときの扱いが3か所で違った。"""
    m = reward.month(tx)
    if not m["has_terms"]:
        return UNKNOWN, m
    return (DEFICIT if m["cost"] > m["upper"] else NOT_DEFICIT), m


def _setting(tx: Tx, key: str, tenant_id: UUID, client_id: UUID) -> Any:
    return settings.resolve(tx, key, tenant_id=tenant_id, client_id=client_id, business_id=None)


def _deficit_step(tx: Tx, tenant_id: UUID, client_id: UUID) -> int | None:
    """赤字の警告の進め方（判断はここだけ。利用量の記録のたびと、見守りの tick が呼ぶ）。期限が過ぎて、もう一度知らせたら、その
    警告の番号を返す。赤字でない ⇒ 開いている警告を閉じる／赤字で警告が無い・確認が切れた ⇒ 警告して知らせる／警告の期限までに
    確認されていない ⇒ もう一度、契約者と管理者へ知らせる（1回。止めない＝2026-10-22 深夜 ユーザー）／確認済みで確認が効いて
    いるあいだ ⇒ 何もしない。"""
    state, m = _deficit(tx, tenant_id, client_id)
    figures = f"今月の費用 {m['cost']:.2f}円・報酬 {m['revenue']:.2f}円"
    w = tx.one("SELECT warning_id, deadline, confirmed_at, reminded_at, now() AS now FROM salescall.deficit_warnings"
               " WHERE closed_at IS NULL")
    if state != DEFICIT:
        if w is not None:
            tx.execute("UPDATE salescall.deficit_warnings SET closed_at = now(), closed_why = 'not_deficit'"
                       " WHERE warning_id = %s", (w["warning_id"],))
        return None
    if w is not None and w["confirmed_at"] is not None:
        valid = timedelta(seconds=int(_setting(tx, "budget.deficit_confirm_valid", tenant_id, client_id)))
        if w["now"] < w["confirmed_at"] + valid:
            return None                                  # 確認が効いている
        tx.execute("UPDATE salescall.deficit_warnings SET closed_at = now(), closed_why = 'confirm_expired'"
                   " WHERE warning_id = %s", (w["warning_id"],))   # 確認が切れた：まだ赤字なので、もう一度警告する
        w = None
    if w is None:
        grace = int(_setting(tx, "budget.deficit_grace", tenant_id, client_id))
        row = tx.one("INSERT INTO salescall.deficit_warnings (tenant_id, client_id, deadline, figures)"
                     " VALUES (salescall.cur_tenant(), salescall.cur_client(), now() + %s * interval '1 second', %s)"
                     " RETURNING warning_id, deadline", (grace, figures))
        notify.emit(tx, "deficit_warning", key=f"deficit_warning:{row['warning_id']}",
                    summary=f"赤字です（{figures}）。確認ボタンを押してください（発信は止めていません）",
                    refs={"client_id": client_id, "warning_id": row["warning_id"]})
        return None
    if w["now"] < w["deadline"] or w["reminded_at"] is not None:
        return None                                      # 確認を待っている・もう一度知らせた
    tx.execute("UPDATE salescall.deficit_warnings SET reminded_at = now() WHERE warning_id = %s", (w["warning_id"],))
    notify.emit(tx, "deficit_warning", key=f"deficit_reminder:{w['warning_id']}", admin=True,
                summary=f"赤字の知らせが、期限までに確認されていません（{figures}。発信は止めていません）",
                refs={"client_id": client_id, "warning_id": w["warning_id"]})
    return w["warning_id"]


def confirm_deficit(tx: Tx, warning_id: int) -> None:
    """赤字の警告を確認する（管理者か契約者だけ。データベースが守る）。確認したら、確認が効いているあいだ（設定
    budget.deficit_confirm_valid）は新しく警告しない（赤字では止めない＝2026-10-22 ユーザー「止めない。知らせるだけでいい」。
    確認されないまま期限を過ぎたら、契約者と管理者へもう一度だけ知らせる）。"""
    row = tx.one("UPDATE salescall.deficit_warnings SET confirmed_at = now(), confirmed_by = salescall.cur_actor()"
                 " WHERE warning_id = %s AND closed_at IS NULL AND confirmed_at IS NULL RETURNING warning_id", (warning_id,))
    if row is None:
        raise LookupError(f"確認できる赤字の警告がありません：{warning_id}")


def _check_cost_ratio(tx: Tx, tenant_id: UUID, client_id: UUID) -> None:
    state, m = _deficit(tx, tenant_id, client_id)
    figures = f"今月の費用 {m['cost']:.2f}円・報酬 {m['revenue']:.2f}円"
    day = tx.today()
    if state == UNKNOWN:
        # 報酬の決まりが無いのは設定の抜け（お客様には影響しない）：止めずに注意を知らせる（2026-10-16 ユーザー）
        notify.emit(tx, "setting_missing", key=f"reward_terms_missing:{client_id}:{day}",
                    summary=f"報酬の決まりが設定されていません（赤字かを判断できません。{figures}）", refs={"client_id": client_id})
        return None
    if state == DEFICIT:
        _deficit_step(tx, tenant_id, client_id)
        return None
    _deficit_step(tx, tenant_id, client_id)              # 赤字でなくなった：開いている警告を閉じる
    limit = Decimal(settings.resolve(tx, "budget.max_cost_ratio", tenant_id=tenant_id, client_id=client_id,
                                     business_id=None))
    if m["upper"] > 0 and m["cost"] / m["upper"] > limit:
        ratio = m["cost"] / m["upper"]
        notify.emit(tx, "budget", key=f"cost_ratio:{client_id}:{day}",
                    summary=f"報酬に対する費用の割合が上限を超えました（多めに見込んでも {ratio:.0%} ＞ {limit:.0%}。{figures}）",
                    refs={"client_id": client_id, "ratio": str(ratio), "limit": str(limit)})
    return None


def record_line_estimate(tx: Tx, attempt_id: UUID) -> dict[str, Any] | None:
    """回線の料金の推定（締めの処理が呼ぶ）：お客様が出てから終わるまでの秒数 × 単価。お客様が出なかった通話は書かない。"""
    row = tx.one(
        "SELECT extract(epoch FROM a.ended_at - h.at) AS seconds, l.method FROM salescall.attempts a"
        " JOIN salescall.lines l ON l.tenant_id = a.tenant_id AND l.line_id = a.line_id"
        " JOIN LATERAL (SELECT min(at) AS at FROM salescall.attempt_state_history"
        "               WHERE attempt_id = a.attempt_id AND to_state = 'answered') h ON true"
        " WHERE a.attempt_id = %s AND a.ended_at IS NOT NULL AND h.at IS NOT NULL", (attempt_id,))
    if row is None:
        return None
    return record(tx, source="line", service=row["method"], tier=None, kind="estimated",
                  quantity=Decimal(row["seconds"]).quantize(Decimal("0.001")), unit="seconds", attempt_id=attempt_id)


def tick(access: Access, actor: Actor, tenant_id: UUID) -> dict[str, Any]:
    """中央の見守り役（サーバー）が契約者ごとに決まった間隔で呼ぶ、その契約者の費用の見張り：今日の費用が契約者の上限を超えたら、その契約者へ
    注意を知らせる（止めない）。赤字の警告の期限を見て、確認されていなければ、もう一度知らせる（利用量の記録が無いあいだも期限が
    来たら知らせるため。止めない）。読むのは、その契約者の取引から見えるその契約者の分だけ。"""
    def tenant(tx: Tx) -> list[dict[str, Any]]:
        limit = Decimal(settings.resolve(tx, "budget.tenant_daily_yen", tenant_id=tenant_id, client_id=None, business_id=None))
        spent = tx.one("SELECT salescall.spent_today_tenant() AS y")["y"]
        if spent > limit:
            day = tx.today()
            notify.emit(tx, "budget", key=f"tenant_daily:{day}",
                        summary=f"今日の費用（すべての依頼者の合計）が上限を超えました（{spent}円 ＞ {limit}円）。発信は止めていません",
                        refs={"spent": str(spent), "limit": str(limit)})
        return tx.all("SELECT * FROM salescall.open_deficit_warnings()")
    warnings = access.in_tenant(actor, tenant_id, tenant)
    reminded, failed = [], []
    for w in warnings:
        # 依頼者ごとに別の取引。1つの依頼者の誤りで、ほかの依頼者の「期限が来たので知らせる」を止めない（2026-10-18 の見直し）。
        # 誤りは最後にまとめて外へ出す（見守り役が記録する）
        try:
            got = access.in_client(actor, tenant_id, w["client_id"], lambda tx, w=w: _deficit_step(tx, tenant_id, w["client_id"]))
        except Exception as e:  # noqa: BLE001
            failed.append(e)
            continue
        if got is not None:
            reminded.append(got)
    if failed:
        raise ExceptionGroup("費用の見張りで、一部の依頼者の分ができませんでした", failed)
    return {"reminded": reminded}
