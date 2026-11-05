"""お客様の連絡先（お客様リストの1行＝タスクごと。DESIGN.md 3-4・3-5。データベースは 0175）。持ち主 CL-2。

✅ 2026-11-05 ユーザー：「携帯電話番号とLINE IDの項目も必要でないですか？お客様が電話で答えてくれた場合は、このシートを元にした
データベースに加える必要があるでしょ？後、オンライン面談ようのID（ズームなど）」・「その会社の目的の担当にかけたい業務の場合は、
会社の番号のみ。お問い合わせフォームからの返信や、相手からの問い合わせで携帯電話番号が指定されている場合は、携帯番号が優先番号になります。」

- **かける番号の決め方は `dial_label` の1か所**：お客様が指定した電話番号があればその番号（designated）、無ければ会社の番号（company）。
  取込が、この答えを行の一覧の番号にする。
- **かけない番号**（記録だけ。会社の番号のうち、かける番号でないもの・お客様が指定していない携帯電話番号・LINE のご招待を送る番号・
  ほかのご連絡の方法で伺えたお電話の番号）は、行の番号の表に source record で持つ（`add_number`）。そのお客様が断ったら、記録だけの
  番号も止める（発信禁止は行の番号の表の1か所を読む＝原則14）。試行・次の予定はデータベースが断る（0175 の dial_number_guard）。
- **番号ではない連絡先**（メールアドレス・ファクス番号・住所・LINE ID・オンライン面談用の ID とサービス名）は `add` の1か所で
  足していく（最新が正本＝`now`）。出どころは3つ：一覧の取込（list）・通話（call。報告の事実から `from_report` が書く）・
  ダッシュボードの直し（dashboard。管理者か契約者だけ＝データベースが守る）。
- 値の形の決まりは `problem` の1か所（取込・ダッシュボード・通話のどれも同じ決まりで受ける）。
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from salescall.core import tasks
from salescall.core.access import Tx
from salescall.core.phone import PhoneNumber, normalize

KINDS = ("email", "fax", "address", "line_id", "meeting")          # 番号ではない連絡先の種類（0175 の CHECK と同じ）
KIND_NAMES = {"email": "メールアドレス", "fax": "ファクス番号", "address": "住所", "line_id": "LINE ID",
              "meeting": "オンライン面談用の ID"}
NUMBER_LABELS = ("company", "designated", "mobile", "line", "said")  # 行の番号の名札（0175 の CHECK と同じ）
DIAL_LABELS = ("company", "designated")                            # かける番号（一覧の番号）になれる名札
RECORD_LABELS = ("company", "mobile", "line", "said")               # 記録だけの番号の名札
ORIGINS = ("list", "call", "dashboard")

_EMAIL = re.compile(r"[^@\s]+@[^@\s.]+(\.[^@\s.]+)+")
_NO_SPACE = re.compile(r"\S+")


class ContactInvalid(ValueError):
    """連絡先の値の形が合わない（理由は日本語）。"""


def dial_label(*, designated: bool) -> str:
    """一覧の行のかける番号の名札（決め方はここだけ）：お客様が指定した電話番号があればそれ、無ければ会社の番号。"""
    return "designated" if designated else "company"


def problem(kind: str, value: str | None, service: str | None = None) -> str | None:
    """その種類の連絡先として使えない理由（使えれば None）。値は前後の空白を除いて見る。"""
    if kind not in KINDS:
        return f"連絡先の種類は {list(KINDS)} のどれかです（{kind!r}）"
    v = value.strip() if isinstance(value, str) else ""
    if not v:
        return f"{KIND_NAMES[kind]}が空です"
    if kind == "email" and not _EMAIL.fullmatch(v):
        return f"メールアドレスの形ではありません（「{v}」）"
    if kind == "fax" and normalize(v).kind == "unknown":
        return f"ファクス番号として読めません（「{v}」）"
    if kind == "line_id" and not _NO_SPACE.fullmatch(v):
        return f"LINE ID に空白は入りません（「{v}」）"
    if kind == "meeting" and not (isinstance(service, str) and service.strip()):
        return "オンライン面談用の ID には、サービスの名前（Zoom など）も要ります"
    if kind != "meeting" and service is not None:
        return "サービスの名前は、オンライン面談用の ID にだけ付けます"
    return None


def add(tx: Tx, task_id: UUID, kind: str, value: str, *, origin: str, service: str | None = None,
        import_id: UUID | None = None, source_row: int | None = None, attempt_id: UUID | None = None,
        turn: int | None = None) -> None:
    """番号ではない連絡先を、その行に足す（書くのはここだけ）。値の形が合わなければ ContactInvalid（何も書かない）。
    ダッシュボードからは、記録した方＝いま操作している方（データベースが守る）。"""
    why = problem(kind, value, service)
    if why is not None:
        raise ContactInvalid(why)
    if origin not in ORIGINS:
        raise ValueError(f"出どころは {list(ORIGINS)} のどれかです（{origin!r}）")
    tx.execute(
        "INSERT INTO salescall.task_contacts (tenant_id, client_id, task_id, kind, value, service, origin, import_id, source_row,"
        "                                     attempt_id, turn, recorded_by)"
        " SELECT t.tenant_id, t.client_id, t.task_id, %s, %s, %s, %s, %s, %s, %s, %s,"
        "        CASE WHEN %s = 'dashboard' THEN salescall.cur_actor() END"
        " FROM salescall.tasks t WHERE t.task_id = %s",
        (kind, value.strip(), service.strip() if service else None, origin, import_id, source_row, attempt_id, turn,
         origin, task_id))


def add_number(tx: Tx, task_id: UUID, number: PhoneNumber, label: str, *, origin: str,
               attempt_id: UUID | None = None) -> UUID:
    """記録だけの番号を、その行の番号に足す（足すのはここだけ。かける番号は取込の一覧の番号と、お客様が言った別の番号＝tasks）。
    同じ番号がもう行にあれば何もしない（かける番号と同じ番号を書いた・伺った場合も）。その番号の連絡先を返す。"""
    if label not in RECORD_LABELS:
        raise ValueError(f"記録だけの番号の名札は {list(RECORD_LABELS)} のどれかです（{label!r}）")
    if origin not in ORIGINS:
        raise ValueError(f"出どころは {list(ORIGINS)} のどれかです（{origin!r}）")
    return tasks.add_record_number(tx, task_id, number, label, origin=origin, attempt_id=attempt_id)


def now(tx: Tx, task_id: UUID) -> dict[str, Any]:
    """その行のいまの連絡先：{"numbers": [{phone, label, source, dialing}], "contacts": [{kind, value, service, origin}]}（読むのはここだけ）。
    dialing＝いまかける番号か（ダッシュボードで直したら、取り込んだ番号は偽＝画面で灰色にする番号。0179）。"""
    numbers = [{"phone": r["phone_e164"], "label": r["label"], "source": r["source"], "dialing": r["dialing"]} for r in tx.all(
        "SELECT phone_e164, label, source, contact_id = salescall.task_dial_contact(task_id) AS dialing"
        " FROM salescall.task_numbers WHERE task_id = %s"
        " ORDER BY source = 'list' DESC, added_at, phone_e164", (task_id,))]
    rows = tx.all("SELECT kind, value, service, origin FROM salescall.task_contacts_now WHERE task_id = %s"
                  " ORDER BY kind, coalesce(service, '')", (task_id,))
    return {"numbers": numbers, "contacts": [dict(r) for r in rows]}


# 連絡先の種類 → 会話の側の連絡先の役目（flowctl.CONTACT_ROLES・LINE_HOW の役目。重ねて伺わない＝flowctl.known_contact。CL-1 の連絡帳 4-92 の (5)）
_ROLE_OF_KIND = {"email": "email", "fax": "fax", "address": "address", "line_id": "line"}
_MOBILE_LABELS = ("mobile", "line")   # 携帯電話の番号の役目（SMS でご招待する番号）に当たる記録だけの番号の名札


def by_role(tx: Tx, task_id: UUID) -> dict[str, str]:
    """その行のいまの連絡先を、会話の側の連絡先の役目の名前で（line＝LINE の ID・mobile＝携帯電話の番号・email・fax・address）。会話の側が、
    分かっている連絡先を重ねて伺わないために読む（読み方は `now` の1か所。携帯電話の番号は、かけない番号のうち携帯電話・LINE のご招待の番号の
    いちばん新しいもの、無ければ、お客様が指定した番号が携帯電話の番号のとき、その番号）。オンライン面談用の ID は、会話で伺う連絡先ではない。"""
    got = now(tx, task_id)
    out = {_ROLE_OF_KIND[c["kind"]]: c["value"] for c in got["contacts"] if c["kind"] in _ROLE_OF_KIND}
    phones = [n for n in got["numbers"] if n["label"] in _MOBILE_LABELS] or \
             [n for n in got["numbers"] if n["label"] == "designated" and normalize(n["phone"]).kind == "mobile"]
    if phones:
        out["mobile"] = phones[-1]["phone"]
    return out


# 通話で伺えた連絡先（会話の報告の事実）→ 連絡先の種類。ほかのご希望のご連絡の方法（other_wish の means・LINE は via）と、
# かけ直しの申し出で伺えた宛先（callback の means）。郵送＝住所。伝言（message）は連絡先ではない
_WISH_KIND = {"email": "email", "fax": "fax", "post": "address", "online": "email"}   # オンライン＝ご案内の URL をお送りするメールアドレス（4-91）
_LINE_VIA_KIND = {"id": "line_id", "email": "email"}
_CALLBACK_KIND = {"email": "email", "fax": "fax", "address": "address"}


def from_report(tx: Tx, task_id: UUID, attempt_id: UUID, report: Any) -> None:
    """その通話の報告で伺えた連絡先を、その行に足す（通話から書くのはここだけ。報告を受け取った取引の中で1回）。
    番号（LINE のご招待を送る番号・お電話の番号）は記録だけの番号に、ほかは番号ではない連絡先に。形の合わない値は足さない
    （伺えた値はそのまま報告に残り、依頼者へも渡る＝判定の中身）。"""
    for w in report.other_wishes:
        if not w.contact:
            continue
        if w.means == "line" and w.via == "sms" or w.means == "phone":
            n = normalize(w.contact)
            if n.e164 is not None:
                add_number(tx, task_id, n, "line" if w.means == "line" else "said", origin="call", attempt_id=attempt_id)
            continue
        kind = _LINE_VIA_KIND.get(w.via) if w.means == "line" else _WISH_KIND.get(w.means or "")
        if kind is not None and problem(kind, w.contact) is None:
            add(tx, task_id, kind, w.contact, origin="call", attempt_id=attempt_id, turn=w.evidence.turn)
    for c in report.callbacks:
        for key, value in c.means:
            kind = _CALLBACK_KIND.get(key)
            if kind is not None and value and problem(kind, value) is None:
                add(tx, task_id, kind, value, origin="call", attempt_id=attempt_id, turn=c.evidence.turn)
