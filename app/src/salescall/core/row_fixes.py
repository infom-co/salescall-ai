"""お客様リストの行の直し（ダッシュボード。データベースは 0178。DESIGN.md 3-4-1）。持ち主 CL-2。

✅ 2026-11-05 ユーザー：「それらはダッシュボードに取込、ダッシュボードでも進捗確認や修正ができるようにすること。」

- 直せる欄は `FIELDS`（会社名・お呼びする方の名前・フルネーム・役職・部署）。直しは記録を足していくだけで、取込の値は変えない。
  行のいまの値は見方 `salescall.tasks_now` の1か所（会話が読む business_settings.target_of・門・材料の数えも、この見方を読む）。
- 名前の欄の読みは、取込と同じ決め方（core/readings.py の1か所）：直した方が入れた読み（確かめた読み＝person）＞ AI の読み ＞
  決まらない（候補つき。会話の中で確かめる）。検証モードの業務では、AI の読みは契約者が確かめるので、読みも一緒に入れていただく
  （Intent.md 6-8 の決まり。入れなければ `FixInvalid`）。
- 名前を直したら、直す前に会話の中でお客様に確かめた読みは、その名前には効かない（tasks_now の *_fixed_at より後に確かめた読みだけ＝
  business_settings.target_of の1か所）。
- 役職の欄は、取込・版の割り当て・門と同じ1か所（業務設定の `TargetNeeds.role_unclear`）で確かめる（その行の業務が一覧の役職でお呼びするとき、「ご担当者」だけ＝何の担当か無い役職は直せない）。
- 直せるのは管理者か契約者だけ（データベースが守る）。電話番号・連絡先の直しは core/customer_contacts.py。
- かける番号の直し（`fix_dial_number`）と、前の担当の方が新しい約束をしたあとの選び（`choose_old_promise`。計画書 7-1 (23)・0202）も、ここ。
- お呼びする方の名前・フルネーム・役職・部署の中身を「担当の方は違う」で直したとき（担当の期が替わる＝計画書 7-1 (19)「違う相手」・(28)。値が変わらない直しは替わらない＝0208）も、前の担当の方との約束が残っていれば、
  番号の「違う」の直しと同じ選び（7-1 (21)(25)）を受ける（0206）。選びが要るか・選べるかは `_old_promise_choice` の1か所（データベースの
  salescall.task_old_promise_choice_needed を読む。守りも同じ関数）。画面が「2つ目の選び（前の担当の方との約束も守るか）が要るか」を読む入口は
  `old_promise_choice_needed`。直したあとの予定は next_action の1か所（番号＝`after_dial_fix`・名前など＝`after_person_fix`）。
- 欄の値の整え方（前後の空白を除く・空なら消す）と、その直しでお呼びする方が替わるか（表記の揺れ＝全角・半角・空白の違いだけなら替わらない。
  データベースの salescall.person_fix_changes の1か所＝0209）は `_person_fix` の1か所（直す入口 `fix` と、画面の入口
  `old_promise_choice_needed`・`same_person_choice_needed` が通る＝画面が選びを出すのに、直す入口が選びを断る、の食い違いを作らない）。
- 名前などの直しでも「担当の方は同じか・違うか」を契約者が選ぶ（✅ 2026-11-08 ユーザー＝計画書 7-1 (28)。0210）：値が本当に変わる直し
  （空欄を埋める・消すも入る＝データベースの salescall.person_fix_differs）は必ず選ぶ（same_person）。「同じ」（同じ方の誤字の直しなど）なら
  担当の期も予定も約束もそのまま。「違う」のときだけ、お呼びする方が替わる（7-1 (19) の「違う相手」）。値が変わらない・表記の揺れだけの直しは
  選ばない（渡せば `FixInvalid`。データベースの守り task_fix_guard も同じ1か所）。画面の問いは「いまの担当の方と同じ方か」（番号を「違う」で
  直したあと、名前を新しい担当の方の名前に直すときは「同じ」）。
- 「同じ」の直しでも、受付の方に「お名前だけではどの方か分かりかねます」と言われて終えた行（依頼者に埋めていただく欄＝
  business_settings.who_unclear_blanks）で、その欄を埋めたら、行を戻して、埋めたお名前・部署でお呼びする（✅ 2026-11-08 ユーザー＝計画書 7-1 (29)
  「埋めたらかけ直す」。0213。戻すかは next_action.reopens_on_fix の1か所）。
- 前の担当の方に戻す（✅ 2026-11-08 ユーザー＝計画書 7-1 (30)「前の方へ戻す選び」。0213）：「担当の方は違う」を選んだ直し（名前など・番号）で、
  戻す先の期（back_to。画面の入口 `back_choices` の鍵）を渡すと、新しい期を作らず、その期がまた「いまの期」になる（データベースの
  salescall.task_person_changes の1か所）。その期の通話の答え（用件の断りなど）も戻るので、その答えで終わっていた行は終わる
  （next_action._after_fix）。戻せるのは、その行の前の期だけ（いまの期・まだ無い期には戻せない＝`FixInvalid`。データベースも同じ1か所
  salescall.task_back_ok で守る）。
- 「約束も守る」を選べるかは1か所（データベースの salescall.task_promise_keepable＝その約束の番号で前の担当の方にお呼びできるか。本体の決定。0213）：
  名前などの直しでは、直す前からの会社の番号（いまかける番号）でも選べる（会話は、その通話の期のお名前でお呼びする）。番号の「違う」の直しで、約束の
  番号が直す番号（新しい担当の方だけの番号）なら選べない。前の担当の方の新しい約束の選び（`choose_old_promise`）も同じ1か所を読む。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from salescall.core import approval, business_settings, next_action, readings, target_policy, tasks
from salescall.core.access import Rejected, Tx
from salescall.core.phone import normalize

FIELDS = ("source_name", "target_name", "target_full_name", "target_role", "target_department")   # 0178 の CHECK と同じ
NAME_FIELDS = ("source_name", "target_name", "target_full_name")                                  # 読みを持つ欄
FIELD_NAMES = {"source_name": "会社名", "target_name": "お呼びする方の名前", "target_full_name": "お呼びする方のフルネーム",
               "target_role": "お呼びする方の役職", "target_department": "お呼びする方の部署"}
PERSON_FIELDS = ("target_name", "target_full_name", "target_role", "target_department")   # 戻せる期の一覧で見せる、その期のお呼びする方の値
FIRST_PERIOD = "first"   # 戻す選びの鍵：最初の担当の方の期（ほかの期の鍵は、その期の始まりの時刻＝ISO の形。`back_choices` が返す）


class FixInvalid(ValueError):
    """その直しは受けられない（理由は日本語）。何も書かない。"""


def fix(tx: Tx, task_id: UUID, field: str, value: str | None, *, reading: str | None = None,
        suggest: readings.Suggester | None = None, same_person: bool | None = None,
        keep_old_promise: bool | None = None, back_to: str | None = None) -> dict[str, Any]:
    """行の1つの欄を直す（書くのはここだけ）。value が空なら、その欄を消す。直したあとの、その欄のいまの値を返す。
    お呼びする方の名前・フルネーム・役職・部署（お呼びする方が替わる欄＝データベースの salescall.person_fix_field の1か所）の中身を直し、契約者が
    「担当の方は違う」を選ぶと、担当の期が替わる（計画書 7-1 (19)(28)。0210）。値が変わらない直し（同じ名前のまま読みだけを直す・同じ値を入れ直す）は、お呼びする方の替わりにしない（期も予定も
    変えない・選びは要らない＝渡せば `FixInvalid`。0208）。表記の揺れだけの直し（全角・半角・半角カナ・空白の違いだけ）も替わらない（0209）。
    値の整え方と、替わるかは `_person_fix` の1か所（画面の入口 `old_promise_choice_needed` も通る。替わるかの決まりはデータベースの
    salescall.person_fix_changes）。same_person＝値が本当に変わる直し（空欄を埋める・消すも入る）で、契約者が画面で選ぶ「いまの担当の方と同じ方か」
    （✅ 2026-11-08 ユーザー＝計画書 7-1 (28)。番号の直しの `fix_dial_number` の same_person と同じ意味。要るときは必ず渡す・要らないときは渡さない
    ＝`FixInvalid`。0210）。真＝同じ方（誤字の直しなど）：担当の期も予定も約束もそのまま。偽＝違う方：お呼びする方が替わる。
    keep_old_promise＝そのとき前の担当の方とのかけ直しの約束が残っていて、約束の番号がいまかける番号でない（前の担当の方
    の携帯など）ときに、契約者が画面で選ぶ「前の担当の方との約束も守るか」（番号の直しの `fix_dial_number` の keep_old_promise と同じ意味・同じ
    1か所 `_old_promise_choice`。要るときは必ず渡す・要らないときは渡さない＝渡さなければ／渡せば `FixInvalid`。データベースも同じ1か所で守る＝
    0206 の task_fix_guard）。「守る」を選べるかは、その約束の番号で前の担当の方にお呼びできるか（データベースの salescall.task_promise_keepable の
    1か所。0213）：直す前からの会社の番号（いまかける番号）でも選べる（会話は、その通話の期のお名前でお呼びする）。⛔ 0206〜0212：約束の番号が
    いまかける番号なら選べなかった（通話中の直しのあとの前の担当の方の新しい約束では、同じ番号でも選べた＝2つの道で答えが違った）。
    back_to＝「担当の方は違う」で、前の担当の方に戻すときの戻す先の期（画面の入口 `back_choices` の鍵。✅ 2026-11-08 ユーザー＝計画書 7-1 (30)。
    戻せるのは、その行の前の期だけ＝`_back`）。
    直したあとの予定は next_action.after_person_fix の1か所が決める（値が本当に変わる直しなら「同じ」でも呼ぶ：依頼者に埋めていただく欄を埋めた行は
    戻す＝計画書 7-1 (29)。ほかの「同じ」の直しでは、予定も約束もそのまま）。"""
    _known(tx, task_id, field)
    text = _text(value)
    r = readings.UNDECIDED
    if field in NAME_FIELDS and text is not None:
        given = reading.strip() if isinstance(reading, str) and reading.strip() else None
        if given is not None:
            if readings.normalize(given) is None:
                raise FixInvalid(f"{FIELD_NAMES[field]}の読み「{given}」は、ひらがなかカタカナで入れてください")
            r = readings.resolve(text, None, confirmed={text: given}, ai={})
        else:
            business = tx.one("SELECT r.business_id FROM salescall.tasks t JOIN salescall.runs r ON r.run_id = t.run_id"
                              " WHERE t.task_id = %s", (task_id,))
            if business is None:
                raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
            if approval.human_for_business(tx, business["business_id"]):
                raise FixInvalid(f"検証モードの業務では、{FIELD_NAMES[field]}の読みも入れてください（AI の読みは契約者が確かめる決まり）")
            r = readings.resolve(text, None, confirmed={}, ai=readings.ask([text], suggest))
    elif reading is not None and str(reading).strip():
        raise FixInvalid(f"読みを持つのは名前の欄だけです（{FIELD_NAMES[field]}）")
    if field == "target_role" and text is not None:
        # 「ご担当者」だけの役職は、取込・版の割り当て・門と同じ1か所（業務設定の TargetNeeds.role_unclear＝その行の実施回のいまの版が
        # 一覧の役職でお呼びするときだけ）で断る（⛔ 2026-11-07 の第三者の見直し (2) の7：前はここだけ通していた／その直しのあとは、
        # ここだけ業務によらず断っていた）。あとで版が替わって役職でお呼びするようになれば、門と版の割り当てが同じ1か所で見る
        version = tx.one("SELECT salescall.run_current_version(t.run_id) AS v FROM salescall.tasks t WHERE t.task_id = %s", (task_id,))
        if version is None:
            raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
        why = business_settings.load(tx, version["v"]).target_needs().role_unclear(text)
        if why is not None:
            raise FixInvalid(why)
    # 担当の方は同じか・違うかの選びと、お呼びする方が替わるか（欄の値そのものの確かめ＝読み・役職のあと。1か所＝`_person_fix`）
    _, changes = _person_fix(tx, task_id, field, value, same_person)
    back = _back(tx, task_id, back_to, same_person)
    _old_promise_choice(tx, task_id, changes, None, back, keep_old_promise)   # 名前などの直しでは、新しい担当の方だけの番号は無い
    tx.execute(
        "INSERT INTO salescall.task_fixes (tenant_id, client_id, task_id, field, value, reading, reading_by, candidates, likely,"
        "                                  recorded_by, same_person, keep_old_promise, back_to)"
        " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, %s, %s, %s, %s, salescall.cur_actor(), %s, %s, %s::timestamptz"
        " FROM salescall.tasks t WHERE t.task_id = %s",
        (field, text, r.reading, r.by if r.reading else None, list(r.candidates), r.likely, same_person, keep_old_promise, back, task_id))
    row = tx.one(f"SELECT {field} AS value FROM salescall.tasks_now WHERE task_id = %s", (task_id,))
    if row is None:
        raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
    if same_person is not None:   # 値が本当に変わる直し（「同じ」でも、依頼者に埋めていただく欄を埋めた行は戻す＝計画書 7-1 (29)）
        next_action.after_person_fix(tx, task_id, same_person=same_person, back=back is not None,
                                     cause=f"ダッシュボードで、{FIELD_NAMES[field]}を直した（担当の方は"
                                           f"{'同じ' if same_person else '違う'}{'・前の担当の方に戻す' if back else ''}）")
    return {"field": field, "value": row["value"], "reading": r.reading if text is not None else None}


def same_person_choice_needed(tx: Tx, task_id: UUID, *, field: str, value: str | None) -> bool:
    """画面の入口（CL-3）：名前などを直す画面で、1つ目の選び「いまの担当の方と同じ方か」を出すか（✅ 2026-11-08 ユーザー＝計画書 7-1 (28)。0210）。
    field・value は `fix` に渡すものと同じ（value が空なら、その欄を消す直し）。値が本当に変わる直し（空欄を埋める・消すも入る）なら真＝選ばなければ
    直せない（`fix` の same_person）。値が変わらない・表記の揺れだけ・会社名の直しなら偽＝選びを出さない（渡せば `fix` が断る）。決めるのは直す
    入口と同じ1か所（`_person_fix`。データベースの salescall.person_fix_differs）。直せない欄は `fix` と同じく `FixInvalid`。"""
    return _person_fix(tx, task_id, field, value, None, ask=True)[1]


def back_choices(tx: Tx, task_id: UUID) -> list[dict[str, Any]]:
    """画面の入口（CL-3）：名前などを直す画面・番号を直す画面で、「担当の方は違う」のときに出す「前の担当の方に戻す」の期の一覧（✅ 2026-11-08
    ユーザー＝計画書 7-1 (30)「直す画面に『前の担当の方に戻す』を出し…」。0213）。その行の前の期だけ（いまの期は入らない。替わったことが無い行は空
    ＝選びを出さない）。新しく終わった期の順。1つずつ、戻す選びの鍵 back_to（`fix`・`fix_dial_number`・`old_promise_choice_needed` に渡す。
    最初の担当の方の期は `FIRST_PERIOD`、ほかは、その期の始まりの時刻の ISO の形）・その期の始まり since（最初の期は None）・その期のお呼びする方の
    値 values（名前・フルネーム・役職・部署＝その期の終わりの直前の値。データベースの salescall.task_person_values の1か所）。決めるのは
    データベースの salescall.task_back_periods の1か所（直す入口の確かめ `_back` と守りも同じ）。"""
    if tx.one("SELECT 1 AS x FROM salescall.tasks WHERE task_id = %s", (task_id,)) is None:
        raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
    rows = tx.all("SELECT b.since, " + ", ".join(f"v.{f}" for f in PERSON_FIELDS) +
                  " FROM salescall.task_back_periods(%s) b CROSS JOIN LATERAL salescall.task_person_values(%s, b.since) v"
                  " ORDER BY b.last_until DESC", (task_id, task_id))
    return [{"back_to": FIRST_PERIOD if r["since"] is None else r["since"].isoformat(), "since": r["since"],
             "values": {f: r[f] for f in PERSON_FIELDS}} for r in rows]


def _back(tx: Tx, task_id: UUID, back_to: str | None, same_person: bool | None) -> str | None:
    """前の担当の方に戻す選びの確かめ（1か所。`fix`・`fix_dial_number`・`old_promise_choice_needed` が通る）。戻す先の期の鍵（`back_choices` の
    back_to）を、データベースの値の形（最初の担当の方の期は '-infinity'・ほかは、その期の始まりの時刻）にして返す。戻さなければ None。
    戻せるのは「担当の方は違う」を選んだ直しで、その行の前の期だけ（いまの期・まだ無い期には戻せない＝データベースの salescall.task_back_ok の
    1か所。守り task_person_back_guard も同じ）。受けられなければ `FixInvalid`（何も書かない）。"""
    if back_to is None:
        return None
    if same_person is not False:
        raise FixInvalid("前の担当の方に戻すのは、「担当の方は違う」を選んだ直しだけです")
    if back_to == FIRST_PERIOD:
        value = "-infinity"
    else:
        try:
            at = datetime.fromisoformat(back_to)
        except (TypeError, ValueError):
            raise FixInvalid(f"戻す先の期の鍵として読めません（{back_to!r}。画面の入口 back_choices の back_to を渡してください）") from None
        value = at.isoformat()
    if not tx.one("SELECT salescall.task_back_ok(%s, %s::timestamptz) AS ok", (task_id, value))["ok"]:
        raise FixInvalid("戻せるのは、その行の前の担当の方の期だけです（いまの期・まだ無い期には戻せません）")
    return value


def old_promise_choice_needed(tx: Tx, task_id: UUID, phone: str | None = None, *, field: str | None = None,
                              value: str | None = None, same_person: bool | None = None, back_to: str | None = None) -> bool:
    """画面の入口（CL-3）：番号を直す画面・名前などを直す画面で、2つ目の選び「前の担当の方との約束も守るか」を出すか（✅ 2026-11-07 ユーザー＝
    計画書 7-1 (21)(25)。0206）。渡すのは、番号か、欄と値の、どちらか一方だけ（両方・どちらも無しは `TypeError`）。
    - phone＝番号を直す画面で「担当の方は違う」を選んだときの新しい番号（番号の読み方は phone.normalize の1か所。読めなければ `FixInvalid`）。
    - field・value＝名前・フルネーム・役職・部署などを直す画面の、直す欄と、入れた値（`fix` に渡すものと同じ。value が空なら、その欄を消す直し）。
      その直しでお呼びする方が替わるか（値の整え方と、表記の揺れだけの違いを替わりとしない決まり）は `fix` と同じ1か所 `_person_fix` で決める
      （替わらない直し＝値が同じ・表記の揺れだけ・会社名の直し、は偽）。替わる直しなら、番号は変わらないので、いまかける番号で見る。
      same_person＝1つ目の選び（`same_person_choice_needed` が真のときに契約者が選んだ「いまの担当の方と同じ方か」。`fix` に渡すものと同じ）。
      値が本当に変わる直しで渡さなければ `FixInvalid`（`fix` と同じ）。真（同じ方）なら偽＝2つ目の選びは出さない（0210）。
      ⛔ 0208 まで：欄も値も受けず、名前などの直しは、いつも替わる直しとして答えた（値が変わらない直しでも真を返し、画面が選びを出すのに、
      `fix` は選びを断った）。
    back_to＝前の担当の方に戻すときの戻す先の期（`back_choices` の鍵。`fix`・`fix_dial_number` に渡すものと同じ。0213）：戻す先の方の約束なら、
    その方の約束としてまた生きるので偽（選びは出さない）。
    真なら、選ばなければ直せない（`fix_dial_number`・`fix` の keep_old_promise）。偽なら、選びを出さない（約束が無い・約束の番号で前の担当の方に
    お呼びできない＝番号の「違う」の直しで、約束の番号が直す番号（新しい担当の方だけの番号）・お呼びする方が替わらない直し）。名前などの直しでは、
    約束の番号が直す前からの会社の番号（いまかける番号）でも真（「守る」を選べる。0213＝本体の決定）。決めるのはデータベースの
    salescall.task_old_promise_choice_needed の1か所（包むだけ）。"""
    if (phone is None) == (field is None) or (field is None and (value is not None or same_person is not None)):
        raise TypeError("old_promise_choice_needed には、番号（phone）か、直す欄と値（field・value・same_person）の、どちらか一方だけを渡します")
    if field is not None:
        _, changes = _person_fix(tx, task_id, field, value, same_person)
        new_number = None   # 名前などの直しでは、新しい担当の方だけの番号は無い（番号は変わらない）
    else:
        number = normalize(phone or "")
        if number.e164 is None:
            raise FixInvalid(f"電話番号として読めません（{number.reason or '番号が不明です'}）")
        changes, new_number, same_person = True, number.e164, False   # 番号を直す画面は「担当の方は違う」のときに問う
    back = _back(tx, task_id, back_to, same_person)
    return changes and _choice_needed(tx, task_id, new_number, back)


def _person_fix(tx: Tx, task_id: UUID, field: str, value: str | None, same_person: bool | None, *,
                ask: bool = False) -> tuple[str | None, bool]:
    """行の欄の直しの、値の整え方と、担当の方は同じか・違うかの選びと、お呼びする方が替わるかの1か所（直す入口 `fix` と画面の入口
    `old_promise_choice_needed`・`same_person_choice_needed` が通る）。（書く値, お呼びする方が替わるか）を返す。書く値は、前後の空白を除いた値
    （空なら None＝その欄を消す。値そのものは契約者が入れたまま残す）。
    - 値が本当に変わる直しか（データベースの salescall.person_fix_differs の1か所＝お呼びする方が替わる欄で、値を、直しの直前の値と、表記をそろえた
      形＝0209 の salescall.compare_form で比べる。空欄を埋める・消すも入る）。変わる直しなら、契約者の選び same_person（いまの担当の方と同じ方か。
      計画書 7-1 (28)）を必ず受ける（無ければ `FixInvalid`）。変わらない直し（読みだけ・同じ値の入れ直し・表記の揺れだけ・会社名の直し）は受けない
      （渡せば `FixInvalid`）。データベースの守り task_fix_guard も同じ関数を読む。
    - お呼びする方が替わるのは、値が本当に変わり、契約者が「違う」を選んだ直し（salescall.person_fix_changes）だけ。
    ask＝画面の入口 `same_person_choice_needed` の問い（選びはまだ無い）：選びが要るか（値が本当に変わるか）を返す（選びを確かめない）。"""
    _known(tx, task_id, field)
    text = _text(value)
    differs = bool(tx.one("SELECT salescall.person_fix_differs(%s, %s, %s, NULL) AS d", (task_id, field, text))["d"])
    if ask:
        return text, differs
    if differs and same_person is None:
        raise FixInvalid(f"{FIELD_NAMES[field]}の値が変わります。いまの担当の方と同じ方か（同じ方の誤字の直しなど）、違う方かを選んでください")
    if not differs and same_person is not None:
        raise FixInvalid("担当の方は同じか・違うかを選ぶのは、お呼びする方の名前・フルネーム・役職・部署の値が本当に変わる直し（空欄を埋める・"
                         "消すも入ります）だけです（読みだけの直し・同じ値の入れ直し・表記の揺れだけの直しは選びません）")
    changes = bool(tx.one("SELECT salescall.person_fix_changes(%s, %s, %s, NULL, %s) AS p", (task_id, field, text, same_person))["p"])
    return text, changes


def _known(tx: Tx, task_id: UUID, field: str) -> None:
    """直せる欄か・行があるか（`fix`・`_person_fix` が通る）。"""
    if field not in FIELDS:
        raise FixInvalid(f"直せる欄は {[FIELD_NAMES[f] for f in FIELDS]} です（{field!r}）")
    if tx.one("SELECT 1 AS x FROM salescall.tasks WHERE task_id = %s", (task_id,)) is None:
        raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")


def _text(value: str | None) -> str | None:
    """書く値（1か所）：前後の空白を除いた値。空なら None＝その欄を消す（値そのものは契約者が入れたまま残す）。"""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _choice_needed(tx: Tx, task_id: UUID, new_number: str | None, back: str | None) -> bool:
    return bool(tx.one("SELECT salescall.task_old_promise_choice_needed(%s, %s, %s::timestamptz) AS n", (task_id, new_number, back))["n"])


def _old_promise_choice(tx: Tx, task_id: UUID, changes: bool, new_number: str | None, back: str | None,
                        keep_old_promise: bool | None) -> None:
    """担当の方が替わる直し（changes＝番号の「違う」・名前などの「違う」の直し）で、前の担当の方との約束を守るかの選びを確かめる（1か所。
    `fix_dial_number`・`fix` が通る。データベースの守り task_dial_fix_guard・task_fix_guard も同じ salescall.task_old_promise_choice_needed を読む）。
    new_number＝その直しで新しい担当の方だけの番号になる番号（番号の直しの番号。名前などの直しは None）・back＝戻す先の期（`_back`。戻さなければ None）。
    選びが要るのは、約束が残っていて（約束の日・幅が残っている＝1回かけたあとでも）、その約束の番号で前の担当の方にお呼びできて（データベースの
    salescall.task_promise_keepable＝本体の決定。0213）、戻す先の方の約束でないとき。要るのに選ばなければ・要らないのに選べば `FixInvalid`（何も書かない）。"""
    needs = changes and _choice_needed(tx, task_id, new_number, back)
    if needs and keep_old_promise is None:
        raise FixInvalid("前の担当の方とのかけ直しの約束が、まだ残っています（約束の日・幅が残っています）。前の担当の方との約束も"
                         "守るか、次の担当の方にだけかけるかを選んでください")
    if not needs and keep_old_promise is not None:
        if changes and next_action.promise_alive(tx, task_id):
            raise FixInvalid("前の担当の方との約束の電話の番号が、新しい担当の方だけの番号です（または、戻す先の方の約束で、その方の約束としてまた"
                             "生きます）。前の担当の方へかける道が無いので、「約束も守る」は選べません（約束はやめ、新しい担当の方へ引き継ぎを確かめる"
                             "電話をかけます）。選ばずに直してください")
        raise FixInvalid("前の担当の方との約束を守るかを選ぶのは、お呼びする方が替わる直し（「担当の方は違う」を選んだ番号の直し・お呼びする方の"
                         "名前・フルネーム・役職・部署の「担当の方は違う」を選んだ直し）で、約束の電話が残っているときだけです")


def fix_dial_number(tx: Tx, task_id: UUID, phone: str, *, same_person: bool, keep_old_promise: bool | None = None,
                    back_to: str | None = None) -> dict[str, Any]:
    """いまかける番号を直す（ダッシュボード。2026-11-06 ユーザー：「取り込んだ電話番号は残したまま、新しい電話番号が旧番号のすぐ下に入力
    できるようにします。新番号を登録すると、旧電話番号はグレーアウトして、新番号がくっきり表示されます。」）。直した番号は、取込と同じ確かめ
    （番号の形・かけない種類・発信禁止・同じ業務で断られたお客様＝target_policy.block_reason の1か所）を通す。通らなければ `FixInvalid`
    （何も書かない）。いまかける番号と同じ番号なら何も書かない。取り込んだ番号は残る（かけなくなる＝表示は前の番号）。行の予定は
    next_action.after_dial_fix の1か所が決める（約束していない予定は付け替える・約束の電話はまず前の番号で・番号のせいや決めた回数で
    終えた行は戻す。2026-11-06 ユーザー決定）。行の回数は、直したときから数え直す（0180）。直したあとの、いまかける番号を返す。
    same_person＝契約者が画面で選ぶ「新しい番号の先の担当の方は、前と同じ方か」（✅ 2026-11-07 ユーザー：「番号を直す画面で選ぶ」＝
    計画書 7-1 (19)。選ばなければ直せない＝必ず渡す。違う方なら、約束は引き継がず最初から・お詫びはせず引き継ぎを確かめて営業を続ける＝
    next_action._target_changed_since の1か所が読む）。
    keep_old_promise＝「担当の方は違う」を選び、前の担当の方とのかけ直しの約束がまだ残っている（約束の日・幅が残っている約束の電話の予定がある＝
    next_action.promise_alive の1か所。1回かけたあとでも＝2026-11-07 ユーザーの新しい決定）ときに（その約束の番号が、いま直す番号のときは選べない＝前の担当の方へかける道が無い。0202）、契約者が画面で選ぶ「前の担当の方との約束も守るか」（✅ 2026-11-07 ユーザー＝計画書 7-1 (21)：
    「前の担当者とのかけ直しの約束があったが、次の担当にだけかけるのかを契約者に問うて、選択してもらう。」）。真＝約束も守る（約束の電話は
    いまの決まりのとおり、まず前の番号へ前の担当の方宛てに。そのあとは新しい番号の担当の方へ、引き継ぎを確かめる電話）・偽＝次の担当の方に
    だけかける（約束をやめて、新しい番号へすぐかけてよい、引き継ぎを確かめる電話）。要るときは必ず渡す（渡さなければ `FixInvalid`）・要らない
    ときは渡さない（渡せば `FixInvalid`。データベースも同じ1か所で守る＝0201 の task_dial_fix_guard）。選んだものは直しの記録に残る。
    back_to＝「担当の方は違う」で、前の担当の方に戻すときの戻す先の期（`back_choices` の鍵。✅ 2026-11-08 ユーザー＝計画書 7-1 (30)。0213）：新しい
    期を作らず、その期がまた「いまの期」になる（その期の答えで終わっていた行は終わる＝next_action._after_fix）。いまかける番号と同じ番号へは戻せない
    （直しを書かないので。名前などを直す画面で戻す）。"""
    number = normalize(phone or "")
    if number.e164 is None:
        raise FixInvalid(f"電話番号として読めません（{number.reason or '番号が不明です'}）")
    t = tx.one("SELECT client_ref FROM salescall.tasks WHERE task_id = %s", (task_id,))
    if t is None:
        raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
    now = tasks.dial_number(tx, task_id)
    back = _back(tx, task_id, back_to, same_person)
    if now["phone_e164"] == number.e164:
        if back is not None:
            raise FixInvalid("いまかける番号と同じ番号です。前の担当の方に戻すときは、その方の番号へ直すか、名前などを直す画面で戻してください")
        return now
    # 選びが要るか・選べるか（名前などの直しと同じ1か所。「担当の方は違う」なら、いま直す番号は新しい担当の方だけの番号。直す番号が約束の
    # 番号なら、前の担当の方へかける道が無いので「守る」は選べない＝本体の決定。0202・0213）
    _old_promise_choice(tx, task_id, not same_person, number.e164, back, keep_old_promise)
    blocked = target_policy.block_reason(tx, number, client_ref=t["client_ref"], **tasks.scope(tx, task_id))
    if blocked is not None:
        raise FixInvalid(f"この番号にはかけられません：{blocked[1]}")
    try:
        tasks.fix_dial_number(tx, task_id, number, "mobile" if number.kind == "mobile" else "company", same_person=same_person,
                              keep_old_promise=keep_old_promise, back_to=back)
    except Rejected as e:
        if e.constraint != "task_dial_fix_one_row_per_number":   # 同じ実施回のほかの行がかけうる番号（データベースの1か所＝0193 の task_dialable）
            raise
        raise FixInvalid("その番号は、同じ実施回のほかの行がかけうる番号です（取り込んだ番号・いまかける番号・サブ・まだかけていない予定の番号・"
                         "お客様が指定した番号。1つの実施回で、同じお客様は1行）") from e
    next_action.after_dial_fix(tx, task_id, back=back is not None,
                               cause="ダッシュボードで、かける番号を直した" + ("（前の担当の方に戻す）" if back else ""))
    return tasks.dial_number(tx, task_id)


def choose_old_promise(tx: Tx, task_id: UUID, *, keep_old_promise: bool) -> dict[str, Any]:
    """前の担当の方が新しいかけ直しの約束をした行で、契約者が選ぶ（ダッシュボード。✅ 2026-11-07 ユーザー＝計画書 7-1 (23)「契約者に知らせて
    選んでもらう」）。選びは番号を直すときの選び（`fix_dial_number` の keep_old_promise）と同じ意味：真＝前の担当の方との約束を守る（前の番号へ、
    新しい約束どおりかける。そのあとは同じ決まり）・偽＝新しい担当の方へかける（約束をやめ、新しい番号へ引き継ぎを確かめる電話）。
    選べるのは、選びを待っている行だけ（tasks.old_promise_waiting の1か所。待っていなければ `FixInvalid`＝何も書かない。データベースも同じ
    1か所で守る＝0202 の task_old_promise_choice_guard）。「守る」を選べるのは、その約束の番号で前の担当の方にお呼びできるときだけ（直すときの選びと
    同じ1か所＝`old_promise_keepable`。データベースの守り task_old_promise_keep_guard。0213）。選びは記録に残り（task_old_promise_choices）、行の予定は
    next_action.after_old_promise_choice の1か所が決める。選んだものと、そのあとの予定を返す。"""
    waiting = tasks.old_promise_waiting(tx, task_id)
    if waiting is None:
        raise FixInvalid("この行は、前の担当の方との約束を守るかの選びを待っていません（前の担当の方が新しいかけ直しの約束をした行だけ選べます）")
    if keep_old_promise and not old_promise_keepable(tx, task_id):
        raise FixInvalid("その約束の番号は、新しい担当の方の番号です。前の担当の方へかける道が無いので、「約束も守る」は選べません"
                         "（新しい担当の方へかけるを選んでください）")
    tx.execute("INSERT INTO salescall.task_old_promise_choices (tenant_id, client_id, task_id, attempt_id, keep_old_promise, recorded_by)"
               " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, salescall.cur_actor() FROM salescall.tasks t WHERE t.task_id = %s",
               (waiting, keep_old_promise, task_id))
    next_action.after_old_promise_choice(tx, task_id, keep_old_promise=keep_old_promise,
                                         cause="ダッシュボードで、前の担当の方の新しい約束より、新しい担当の方へかけると選んだ")
    return {"attempt_id": waiting, "keep_old_promise": keep_old_promise, "next": next_action.current(tx, task_id)}


def old_promise_keepable(tx: Tx, task_id: UUID) -> bool:
    """画面の入口（CL-3）：前の担当の方の新しい約束の選び（`choose_old_promise`）で、「約束も守る」を出すか（本体の決定。0213）。決めるのは、直すときの
    選び（`old_promise_choice_needed`）と同じデータベースの1か所 salescall.task_promise_keepable（いまの約束の番号で、その約束の担当の方＝前の担当の方に
    お呼びできるか：ほかの期の担当の方の番号でない。名前などの直しのあとは、直す前からの会社の番号でもお呼びできる＝会話は、その通話の期のお名前で
    お呼びする）。"""
    return bool(tx.one("SELECT salescall.task_promise_keepable(%s, NULL) AS k", (task_id,))["k"])
