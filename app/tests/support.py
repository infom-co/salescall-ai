"""試験の土台：架空の契約者・依頼者・業務などを作る（試験の中だけで使う）。

⚠️ 電話番号は、架空の番号として決められている範囲（英国 Ofcom がドラマ用に取ってある +44 7700 900000〜900999）
だけを使う。実在の会社・お客様・番号は使わない。データベースへは、本番と同じ読み書きの層（Access）だけを通す。
"""

from __future__ import annotations

import itertools
import os
import json
import uuid
from datetime import timedelta
from uuid import UUID

from salescall.core import recordings
from salescall.core.access import Access, Actor
from salescall.core.attempts import ForbiddenFinding, VerificationSummary

OPERATOR = Actor("試験の契約者", "operator")
CLEAN = VerificationSummary(items_checked=1, items_disagreed=0, forbidden_speech=0)   # 検証で何も見つからなかった
SYSTEM = Actor("試験のシステムの処理", "system")
ADMIN = Actor("試験の管理者", "admin")
VOICES = ("Leda", "Sadachbia")   # 試験の土台は、どちらの声も録音を承認してある（話してよい声）

_numbers = itertools.count(int(uuid.uuid4().int % 500))


def fictional_phone() -> str:
    """架空の番号（+447700900000〜+447700900999）。試験の中で重ならないよう順に配る。"""
    return f"+447700900{next(_numbers) % 1000:03d}"


_jp_numbers = itertools.count(int(uuid.uuid4().int % 5000))


_caller_numbers = itertools.count(int(uuid.uuid4().int % 90_000_000))


def fictional_caller() -> str:
    """架空の発信番号（070-0000-0000〜。試験の回線にだけ使う。電話はかけない）。"""
    n = next(_caller_numbers) % 100_000_000
    return f"070-{n // 10000:04d}-{n % 10000:04d}"


def fictional_jp_fixed() -> str:
    """架空の日本の固定電話の番号（03-0000-0000〜03-0000-9999。局番のあとが 0 で始まる番号は日本に無い）。"""
    return f"03-0000-{next(_jp_numbers) % 10000:04d}"


def said(checked: int = 2, disagreed: int = 0) -> "VerificationSummary":
    """言ってはいけないこと（業務の決まりの F1）を1件言った、通話後の検証の結果。"""
    return VerificationSummary(checked, disagreed, 1, (ForbiddenFinding("F1", "初月は無料です"),))


def time_words(at) -> str:
    """時刻を、お客様が言うような言葉にする（会話の報告のかけ直しの時刻。読むのは core/spoken_time）。日本の時刻で書く。"""
    from salescall.core.spoken_time import JST
    t = at.astimezone(JST)
    hour = f"午前{t.hour}時" if t.hour < 12 else f"{t.hour}時"
    return f"{t.month}月{t.day}日の{hour}{t.minute}分"


def callback(reason: str, at=None, number: str | None = None, person: str | None = None, *, time: str | None = None):
    """会話の報告のかけ直しの申し出（at＝時刻を言葉にして渡す。time＝言葉をそのまま渡す）。"""
    from salescall.core.call_report import Callback, Said
    return Callback(reason, time if time is not None else (None if at is None else time_words(at)), number, person,
                    Said(3, "かけ直してください"))


def make_report(parties=("target",), *, dnc: bool = False, oos: bool = False, complaint: bool = False,
                not_reached: str | None = None, callback=None, cut="normal", apology_delivered: bool | None = None,
                end: str | None = None):
    """会話の報告（版3。通話の事実の形）を、試験の材料から作る。cut＝"normal" は、ふつうに締めた切れ方の材料。"""
    from salescall.core.call_report import CallReport, Cut, End, Party, Said
    ev = Said(2, "試験の発話")
    return CallReport((ev,) if dnc else (), (ev,) if complaint else (), (ev,) if oos else (),
                      tuple(Party(p, "report", Said(i, "試験の発話")) for i, p in enumerate(parties)),
                      () if callback is None else (callback,), () if end is None else (End(end, ev),), not_reached,
                      Cut("ai", True, False) if cut == "normal" else cut, apology_delivered)


def rules_raw(required=("Q1",), *, decline=None, retry=None, handling=None) -> dict:
    """試験の業務の決まり（すべての組に宣言がある形）。既定：わからない＝日を改める、答えない＝結論「回答不可」、
    再発信は どれも「かけ直す（3回まで・1時間おき）」。"""
    def per_q():
        return {"dont_know": {r: {"kind": "retry"} for r in ("not_in_charge", "undecided", "unknown_now", "unspecified")},
                "refused": {r: {"kind": "conclude", "conclusion": "回答不可"} for r in ("policy", "unwilling")}}
    return {"decline": decline or {},
            "forbidden": {"F1": {"say_not": "初月は無料です", "correct": "初月は半額です"},
                          "F2": {"say_not": "失礼な言葉", "correct": None}},
            "handling": handling or {q: per_q() for q in required},
            "retry": retry or {k: {"action": "call", "max_times": 3, "interval": {"hours": 1}}
                               for k in ("open", "incomplete", "not_reached_absent", "not_reached_not_transferred",
                                         "callback_unspecified_time")}}


def app_access() -> Access:
    return Access(os.environ["SALESCALL_TEST_APP_DSN"])


def owner_access() -> Access:
    """表の持ち主の役（行の制限を受けない）。データベースの仕掛けそのものを試すときだけ使う。"""
    return Access(os.environ["SALESCALL_TEST_OWNER_DSN"])


class World:
    """架空のデータを、本番と同じ読み書きの層で作る。"""

    def __init__(self, access: Access | None = None) -> None:
        self.a = access or app_access()

    def tenant(self, *, voices: tuple[str, ...] = VOICES) -> UUID:
        tid = uuid.uuid4()
        self.a.in_tenant(OPERATOR, tid, lambda tx: tx.execute(
            "INSERT INTO salescall.tenants (tenant_id, name) VALUES (%s, %s)", (tid, "架空の契約者")))
        self.approve_voices(tid, None, None, voices)
        return tid

    def approve_voices(self, tenant: UUID, client: UUID | None, version: UUID | None,
                       voices: tuple[str, ...] = VOICES) -> None:
        """声ごとに、録音の組を作って承認する（契約者の単位＝版なし／依頼者の単位＝その版。架空の録音）。"""
        def work(tx):
            for v in voices:
                sid = recordings.create_set(tx, unit="contractor" if version is None else "business", voice_name=v,
                                            parts=[recordings.Part("greeting", "お忙しいところ恐れ入ります。",
                                                                   f"test://{v}/greeting", "0" * 64)],
                                            version_id=version)
                recordings.approve(tx, sid)
        if version is None:
            self.a.in_tenant(OPERATOR, tenant, work)
        else:
            self.a.in_client(OPERATOR, tenant, client, work)

    def client(self, tenant: UUID) -> UUID:
        cid = uuid.uuid4()
        self.a.in_tenant(OPERATOR, tenant, lambda tx: tx.execute(
            "INSERT INTO salescall.clients (tenant_id, client_id, name) VALUES (%s, %s, %s)", (tenant, cid, "架空の依頼者")))
        return cid

    def line(self, tenant: UUID) -> UUID:
        from salescall.core import registry
        # 回線の方式の初期は Rakuten Link（2026-10-16 ユーザー）。発信番号は架空の携帯の番号（試験の中で重ならないよう順に配る）
        return self.a.in_tenant(OPERATOR, tenant, lambda tx: registry.create_line(
            tx, "架空の回線", method="rakuten_link", caller=fictional_caller()))

    def business_with_version(self, tenant: UUID, client: UUID) -> tuple[UUID, UUID]:
        bid, vid = uuid.uuid4(), uuid.uuid4()

        def work(tx):
            tx.execute("INSERT INTO salescall.businesses (tenant_id, client_id, business_id, name)"
                       " VALUES (%s, %s, %s, %s)", (tenant, client, bid, "架空の業務"))
            tx.execute("INSERT INTO salescall.business_versions (tenant_id, client_id, business_id, version_id, version_no, created_by)"
                       " VALUES (%s, %s, %s, %s, 1, %s)", (tenant, client, bid, vid, OPERATOR.name))
            tx.execute("INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
                       " VALUES (%s, %s, %s, 'Q1', 1, 'datetime', true)", (tenant, client, vid))
            put_content(tx, tenant, client, vid)
        self.a.in_client(OPERATOR, tenant, client, work)
        self.approve_voices(tenant, client, vid)
        return bid, vid

    def add_version(self, tenant: UUID, client: UUID, business: UUID, version_no: int) -> UUID:
        vid = uuid.uuid4()

        def work(tx):
            tx.execute("INSERT INTO salescall.business_versions (tenant_id, client_id, business_id, version_id, version_no, created_by)"
                       " VALUES (%s, %s, %s, %s, %s, %s)", (tenant, client, business, vid, version_no, OPERATOR.name))
            put_content(tx, tenant, client, vid)
        self.a.in_client(OPERATOR, tenant, client, work)
        self.approve_voices(tenant, client, vid)
        return vid

    def run(self, tenant: UUID, client: UUID, business: UUID, version: UUID) -> UUID:
        rid = uuid.uuid4()

        def work(tx):
            tx.execute("INSERT INTO salescall.runs (tenant_id, client_id, business_id, run_id, name, created_by)"
                       " VALUES (%s, %s, %s, %s, %s, %s)", (tenant, client, business, rid, "架空の実施回", OPERATOR.name))
            tx.execute("INSERT INTO salescall.run_versions (tenant_id, client_id, run_id, business_id, seq, version_id, assigned_by, reason)"
                       " VALUES (%s, %s, %s, %s, 1, %s, %s, %s)", (tenant, client, rid, business, version, OPERATOR.name, "最初の版"))
        self.a.in_client(OPERATOR, tenant, client, work)
        return rid

    def contact(self, tenant: UUID, client: UUID, phone: str) -> UUID:
        cid = uuid.uuid4()
        self.a.in_client(OPERATOR, tenant, client, lambda tx: tx.execute(
            "INSERT INTO salescall.contacts (tenant_id, client_id, contact_id, phone_e164) VALUES (%s, %s, %s, %s)",
            (tenant, client, cid, phone)))
        return cid

    def task(self, tenant: UUID, client: UUID, run: UUID, contact: UUID) -> UUID:
        tid = uuid.uuid4()
        self.a.in_client(OPERATOR, tenant, client, lambda tx: tx.execute(
            "INSERT INTO salescall.tasks (tenant_id, client_id, task_id, run_id, contact_id) VALUES (%s, %s, %s, %s, %s)",
            (tenant, client, tid, run, contact)))
        return tid

    def setup(self, phone: str | None = None, tenant: UUID | None = None) -> dict[str, UUID | str]:
        """契約者〜タスクまで、ひとそろい作る。"""
        t = tenant or self.tenant()
        c = self.client(t)
        b, v = self.business_with_version(t, c)
        r = self.run(t, c, b, v)
        p = phone or fictional_phone()
        k = self.contact(t, c, p)
        task = self.task(t, c, r, k)
        line = self.line(t)
        return dict(tenant=t, client=c, business=b, version=v, run=r, phone=p, contact=k, task=task, line=line)

    def insert_claim(self, s: dict, *, line: UUID | None = None, version: UUID | None = None,
                     expires_in: timedelta = timedelta(minutes=1)) -> UUID:
        """試行の行を「予約」として足す（データベースの約束だけを試すため。本番で予約を作るのは門だけ）。"""
        aid = uuid.uuid4()
        self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
            "INSERT INTO salescall.attempts (tenant_id, client_id, attempt_id, task_id, contact_id, phone_e164, line_id,"
            " version_id, state, claimed_at, claim_expires_at, purpose)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'claimed', now(), now() + %s, 'normal')",
            (s["tenant"], s["client"], aid, s["task"], s["contact"], s["phone"], line or s["line"],
             version or s["version"], expires_in)))
        return aid

    def move(self, s: dict, attempt: UUID, state: str, hangup_side: str | None = None) -> None:
        self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
            "UPDATE salescall.attempts SET state = %s, hangup_side = %s WHERE attempt_id = %s",
            (state, hangup_side, attempt)))


def content(target_person: dict | None = None) -> dict:
    """試験の業務設定の中身（架空。質問は Q1＝日時・必須の1つ。業務の決まりは試験ごとに business_rules.save で置く）。"""
    return {"purpose": "架空の業務の目的", "target_class": "corporate",
            "caller": {"company": {"text": "株式会社テスト", "reading": "かぶしきがいしゃてすと"},
                       "person": {"text": "山田", "reading": "やまだ"}, "purpose_line": None},
            "target_person": target_person or {"kind": "anyone", "role": None}, "busy_line": None, "voicemail_message": None, "ai_answer": None, "voice": "S2",
            "questions": [{"id": "Q1", "text": "ご希望の日時をお聞かせください。", "ask_about": None, "type": "datetime",
                           "required": True, "choices": None, "next": None, "unit": None, "meanings": None, "replies": None}],
            "names": [{"text": "株式会社テスト", "reading": "かぶしきがいしゃてすと", "source": "caller", "reading_by": "given",
                       "candidates": [], "likely": None},
                      {"text": "山田", "reading": "やまだ", "source": "caller", "reading_by": "given",
                       "candidates": [], "likely": None}]}


def put_content(tx, tenant, client, version, target_person: dict | None = None) -> None:
    """試験の版に、業務設定の中身を置く（本番では business_settings.save_version が置く）。"""
    tx.execute("INSERT INTO salescall.business_contents (tenant_id, client_id, version_id, content, saved_by)"
               " VALUES (%s, %s, %s, %s, %s)",
               (tenant, client, version, json.dumps(content(target_person), ensure_ascii=False), OPERATOR.name))


def ensure_rules(w: "World", s: dict, version=None) -> None:
    """試験の版に、業務の決まり（rules_raw の既定）が無ければ置く（本番の版は、必ず業務の決まりと中身を持つ）。"""
    from salescall.core import business_rules
    vid = version or s["version"]

    def work(tx):
        if tx.one("SELECT 1 FROM salescall.business_rules WHERE version_id = %s", (vid,)) is None:
            business_rules.save(tx, vid, rules_raw())
    w.a.in_client(OPERATOR, s["tenant"], s["client"], work)


def full_version(w: "World", s: dict, target_person: dict | None = None, *, content_too: bool = True):
    """試験の業務に、ひとそろいの版（質問 Q1・業務設定の中身・業務の決まり）を足す（版の番号は次の番号）。中身を置かない版も作れる。"""
    from salescall.core import business_rules
    vid = uuid.uuid4()

    def work(tx):
        tx.execute("INSERT INTO salescall.business_versions (tenant_id, client_id, business_id, version_id, version_no,"
                   " created_by) SELECT %s, %s, %s, %s, max(version_no) + 1, %s FROM salescall.business_versions"
                   " WHERE business_id = %s", (s["tenant"], s["client"], s["business"], vid, OPERATOR.name, s["business"]))
        tx.execute("INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
                   " VALUES (%s, %s, %s, 'Q1', 1, 'datetime', true)", (s["tenant"], s["client"], vid))
        if content_too:
            put_content(tx, s["tenant"], s["client"], vid, target_person)
        business_rules.save(tx, vid, rules_raw())
    w.a.in_client(OPERATOR, s["tenant"], s["client"], work)
    w.approve_voices(s["tenant"], s["client"], vid)
    return vid


def schedule_call(tx, task_id, call_at, *, contact_id, cause: str, promised: bool, purpose: str = "normal",
                  wrong_contact_id=None, callee_at=None, window=None) -> int:
    """試験の場面を作る：次の動きの記録に、かける予定を直に書く（本番では、予定を書くのは次の動きの決め方だけ）。"""
    from salescall.core import next_action
    return next_action._append(tx, task_id, call_at=call_at, end_reason=None, cause=cause, promised=promised,
                               target=next_action.Target(contact_id, None, purpose, wrong_contact_id), callee_at=callee_at,
                               window=window)
