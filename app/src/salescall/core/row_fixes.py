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
- お呼びする方の名前・フルネーム・役職・部署の直し（担当の期が替わる＝計画書 7-1 (19)「違う相手」）でも、前の担当の方との約束が残っていれば、
  番号の「違う」の直しと同じ選び（7-1 (21)(25)）を受ける（0206）。選びが要るか・選べるかは `_old_promise_choice` の1か所（データベースの
  salescall.task_old_promise_choice_needed を読む。守りも同じ関数）。画面が「2つ目の選び（前の担当の方との約束も守るか）が要るか」を読む入口は
  `old_promise_choice_needed`。直したあとの予定は next_action の1か所（番号＝`after_dial_fix`・名前など＝`after_person_fix`）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import approval, business_settings, next_action, readings, target_policy, tasks
from salescall.core.access import Rejected, Tx
from salescall.core.phone import normalize

FIELDS = ("source_name", "target_name", "target_full_name", "target_role", "target_department")   # 0178 の CHECK と同じ
NAME_FIELDS = ("source_name", "target_name", "target_full_name")                                  # 読みを持つ欄
FIELD_NAMES = {"source_name": "会社名", "target_name": "お呼びする方の名前", "target_full_name": "お呼びする方のフルネーム",
               "target_role": "お呼びする方の役職", "target_department": "お呼びする方の部署"}


class FixInvalid(ValueError):
    """その直しは受けられない（理由は日本語）。何も書かない。"""


def fix(tx: Tx, task_id: UUID, field: str, value: str | None, *, reading: str | None = None,
        suggest: readings.Suggester | None = None, keep_old_promise: bool | None = None) -> dict[str, Any]:
    """行の1つの欄を直す（書くのはここだけ）。value が空なら、その欄を消す。直したあとの、その欄のいまの値を返す。
    お呼びする方の名前・フルネーム・役職・部署（お呼びする方が替わる欄＝データベースの salescall.person_fix_field の1か所）を直すと、担当の期が
    替わる（計画書 7-1 (19)）。keep_old_promise＝そのとき前の担当の方とのかけ直しの約束が残っていて、約束の番号がいまかける番号でない（前の担当の方
    の携帯など）ときに、契約者が画面で選ぶ「前の担当の方との約束も守るか」（番号の直しの `fix_dial_number` の keep_old_promise と同じ意味・同じ
    1か所 `_old_promise_choice`。要るときは必ず渡す・要らないときは渡さない＝渡さなければ／渡せば `FixInvalid`。データベースも同じ1か所で守る＝
    0206 の task_fix_guard）。約束の番号がいまかける番号なら選べない（前の担当の方へかける道が無いので、約束をやめ、引き継ぎを確かめる電話）。
    直したあとの予定は next_action.after_person_fix の1か所が決める。"""
    if field not in FIELDS:
        raise FixInvalid(f"直せる欄は {[FIELD_NAMES[f] for f in FIELDS]} です（{field!r}）")
    text = value.strip() if isinstance(value, str) and value.strip() else None
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
    changes_person = bool(tx.one("SELECT salescall.person_fix_field(%s) AS p", (field,))["p"])
    if changes_person and tx.one("SELECT 1 AS x FROM salescall.tasks WHERE task_id = %s", (task_id,)) is None:
        raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
    # 名前などの直しでは番号は変わらない：新しい担当の方へかける番号は、いまかける番号
    _old_promise_choice(tx, task_id, tasks.dial_number(tx, task_id)["phone_e164"] if changes_person else None, keep_old_promise)
    tx.execute(
        "INSERT INTO salescall.task_fixes (tenant_id, client_id, task_id, field, value, reading, reading_by, candidates, likely,"
        "                                  recorded_by, keep_old_promise)"
        " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, %s, %s, %s, %s, salescall.cur_actor(), %s"
        " FROM salescall.tasks t WHERE t.task_id = %s",
        (field, text, r.reading, r.by if r.reading else None, list(r.candidates), r.likely, keep_old_promise, task_id))
    row = tx.one(f"SELECT {field} AS value FROM salescall.tasks_now WHERE task_id = %s", (task_id,))
    if row is None:
        raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
    if changes_person:
        next_action.after_person_fix(tx, task_id, cause=f"ダッシュボードで、{FIELD_NAMES[field]}を直した（担当の期が替わる直し）")
    return {"field": field, "value": row["value"], "reading": r.reading if text is not None else None}


def old_promise_choice_needed(tx: Tx, task_id: UUID, phone: str | None = None) -> bool:
    """画面の入口（CL-3）：番号を直す画面・名前などを直す画面で、2つ目の選び「前の担当の方との約束も守るか」を出すか（✅ 2026-11-07 ユーザー＝
    計画書 7-1 (21)(25)。0206）。phone＝番号を直す画面で「担当の方は違う」を選んだときの新しい番号（番号の読み方は phone.normalize の1か所。読めな
    ければ `FixInvalid`）。名前・フルネーム・役職・部署を直す画面は渡さない（番号は変わらないので、いまかける番号で見る）。真なら、選ばなければ
    直せない（`fix_dial_number`・`fix` の keep_old_promise）。偽なら、選びを出さない（約束が無い・約束の番号が新しい担当の方の番号＝「守る」は
    選べず、約束をやめて引き継ぎを確かめる電話）。決めるのはデータベースの salescall.task_old_promise_choice_needed の1か所（包むだけ）。"""
    if phone is None:
        e164 = tasks.dial_number(tx, task_id)["phone_e164"]
    else:
        number = normalize(phone)
        if number.e164 is None:
            raise FixInvalid(f"電話番号として読めません（{number.reason or '番号が不明です'}）")
        e164 = number.e164
    return _choice_needed(tx, task_id, e164)


def _choice_needed(tx: Tx, task_id: UUID, e164: str) -> bool:
    return bool(tx.one("SELECT salescall.task_old_promise_choice_needed(%s, %s) AS n", (task_id, e164))["n"])


def _old_promise_choice(tx: Tx, task_id: UUID, e164: str | None, keep_old_promise: bool | None) -> None:
    """担当の方が替わる直し（番号の「違う」・名前などの直し。e164＝新しい担当の方へかける番号。替わらない直しは None）で、前の担当の方との約束を
    守るかの選びを確かめる（1か所。`fix_dial_number`・`fix` が通る。データベースの守り task_dial_fix_guard・task_fix_guard も同じ
    salescall.task_old_promise_choice_needed を読む）。選びが要るのは、約束が残っていて（約束の日・幅が残っている＝1回かけたあとでも）、その約束の
    番号が新しい担当の方へかける番号でないとき。要るのに選ばなければ・要らないのに選べば `FixInvalid`（何も書かない）。"""
    needs = e164 is not None and _choice_needed(tx, task_id, e164)
    if needs and keep_old_promise is None:
        raise FixInvalid("前の担当の方とのかけ直しの約束が、まだ残っています（約束の日・幅が残っています）。前の担当の方との約束も"
                         "守るか、次の担当の方にだけかけるかを選んでください")
    if not needs and keep_old_promise is not None:
        if e164 is not None and next_action.promise_alive(tx, task_id):
            raise FixInvalid("前の担当の方との約束の電話の番号が、新しい担当の方へかける番号です。前の担当の方へかける道が無いので、"
                             "「約束も守る」は選べません（約束はやめ、新しい担当の方へ引き継ぎを確かめる電話をかけます）。選ばずに直してください")
        raise FixInvalid("前の担当の方との約束を守るかを選ぶのは、お呼びする方が替わる直し（「担当の方は違う」を選んだ番号の直し・お呼びする方の"
                         "名前・フルネーム・役職・部署の直し）で、約束の電話が残っているときだけです")


def fix_dial_number(tx: Tx, task_id: UUID, phone: str, *, same_person: bool, keep_old_promise: bool | None = None) -> dict[str, Any]:
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
    ときは渡さない（渡せば `FixInvalid`。データベースも同じ1か所で守る＝0201 の task_dial_fix_guard）。選んだものは直しの記録に残る。"""
    number = normalize(phone or "")
    if number.e164 is None:
        raise FixInvalid(f"電話番号として読めません（{number.reason or '番号が不明です'}）")
    t = tx.one("SELECT client_ref FROM salescall.tasks WHERE task_id = %s", (task_id,))
    if t is None:
        raise LookupError(f"その行は無い（または、取引の範囲の外）：{task_id}")
    now = tasks.dial_number(tx, task_id)
    if now["phone_e164"] == number.e164:
        return now
    # 選びが要るか・選べるか（名前などの直しと同じ1か所。「担当の方は違う」なら、新しい担当の方へかける番号は、いま直す番号。直す番号が約束の
    # 番号なら、前の担当の方へかける道が無いので「守る」は選べない＝本体の決定。0202）
    _old_promise_choice(tx, task_id, None if same_person else number.e164, keep_old_promise)
    blocked = target_policy.block_reason(tx, number, client_ref=t["client_ref"], **tasks.scope(tx, task_id))
    if blocked is not None:
        raise FixInvalid(f"この番号にはかけられません：{blocked[1]}")
    try:
        tasks.fix_dial_number(tx, task_id, number, "mobile" if number.kind == "mobile" else "company", same_person=same_person,
                              keep_old_promise=keep_old_promise)
    except Rejected as e:
        if e.constraint != "task_dial_fix_one_row_per_number":   # 同じ実施回のほかの行がかけうる番号（データベースの1か所＝0193 の task_dialable）
            raise
        raise FixInvalid("その番号は、同じ実施回のほかの行がかけうる番号です（取り込んだ番号・いまかける番号・サブ・まだかけていない予定の番号・"
                         "お客様が指定した番号。1つの実施回で、同じお客様は1行）") from e
    next_action.after_dial_fix(tx, task_id, cause="ダッシュボードで、かける番号を直した")
    return tasks.dial_number(tx, task_id)


def choose_old_promise(tx: Tx, task_id: UUID, *, keep_old_promise: bool) -> dict[str, Any]:
    """前の担当の方が新しいかけ直しの約束をした行で、契約者が選ぶ（ダッシュボード。✅ 2026-11-07 ユーザー＝計画書 7-1 (23)「契約者に知らせて
    選んでもらう」）。選びは番号を直すときの選び（`fix_dial_number` の keep_old_promise）と同じ意味：真＝前の担当の方との約束を守る（前の番号へ、
    新しい約束どおりかける。そのあとは同じ決まり）・偽＝新しい担当の方へかける（約束をやめ、新しい番号へ引き継ぎを確かめる電話）。
    選べるのは、選びを待っている行だけ（tasks.old_promise_waiting の1か所。待っていなければ `FixInvalid`＝何も書かない。データベースも同じ
    1か所で守る＝0202 の task_old_promise_choice_guard）。選びは記録に残り（task_old_promise_choices）、行の予定は
    next_action.after_old_promise_choice の1か所が決める。選んだものと、そのあとの予定を返す。"""
    waiting = tasks.old_promise_waiting(tx, task_id)
    if waiting is None:
        raise FixInvalid("この行は、前の担当の方との約束を守るかの選びを待っていません（前の担当の方が新しいかけ直しの約束をした行だけ選べます）")
    tx.execute("INSERT INTO salescall.task_old_promise_choices (tenant_id, client_id, task_id, attempt_id, keep_old_promise, recorded_by)"
               " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, salescall.cur_actor() FROM salescall.tasks t WHERE t.task_id = %s",
               (waiting, keep_old_promise, task_id))
    next_action.after_old_promise_choice(tx, task_id, keep_old_promise=keep_old_promise,
                                         cause="ダッシュボードで、前の担当の方の新しい約束より、新しい担当の方へかけると選んだ")
    return {"attempt_id": waiting, "keep_old_promise": keep_old_promise, "next": next_action.current(tx, task_id)}
