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

from salescall.core import calling_hours, recordings, spoken_time
from salescall.core.access import Access, Actor
from salescall.core.attempts import ForbiddenFinding, VerificationSummary

# 試験の判定に渡す、お客様の時間の設定（本番は gate.callee_settings が設定から読む。試験は値を自分で決めて渡す＝初めの値と同じ形：
# 営業時間 9〜19時・お客様指定の歯止め時間 7〜22時・1回分 30分＝0125・「〇時まで」の理由ごとの決め方は初めの値＝0127・言葉ごとの幅は
# 初めの値＝0088：朝〜10時・午前〜12時・午後13時〜・夕方16〜18時・夜18〜20時）。⛔ 見直し18：前は1回分を1時間のままにしていた（初めの値は
# 2026-10-23 から30分）。⛔ 見直し23 の (2)：言葉ごとの幅は、時刻の読みと判定の既定の写しに頼っていた
CALLEE_PERIODS = spoken_time.periods_from({"morning": (0, 10 * 3600), "forenoon": (0, 12 * 3600), "afternoon": (13 * 3600, 24 * 3600),
                                           "evening": (16 * 3600, 18 * 3600), "night": (18 * 3600, 20 * 3600)})
CALLEE = calling_hours.CalleeSettings((9 * 3600, 19 * 3600), (7 * 3600, 22 * 3600), 1800,
                                      (("other_party", "now"), ("unavailable", "after"), ("preparing", "slot_after")),
                                      tuple(sorted(CALLEE_PERIODS.items())))

# 試験の次の動きに渡す、かけてよい時間帯が見つからない行をもう一度確かめるまでの秒（本番は next_action.no_opening_recheck が仮の設定
# retry.no_opening_recheck から読む。試験は値を自分で決めて渡す＝初めの値と同じ1日。⛔ 見直し23 の (9)：前はコードに直に書いていた）
NO_OPENING_RECHECK = 86400
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
                end: str | None = None, check: str = "agreed"):
    """会話の報告（版3。通話の事実の形）を、試験の材料から作る。cut＝"normal" は、ふつうに締めた切れ方の材料。
    電話口の種別に目的のお客様があり、繋がらなかった理由が無ければ、CL-1 と同じく繋いだ事実（connect）も残す（目的のお客様のいる
    業務の「繋がった」は繋いだ事実で決まる＝CallReport.reached）。check＝取り消せない決定の2つ目の判定（データの形 版 1.07）。試験の
    初めの値は「そろった」（会話の側がそろったかを返す形。そろわない場面・判定の無い古い報告は、試験が渡す）。"""
    from salescall.core.call_report import CallReport, Connect, Cut, End, Party, Said
    ev = Said(2, "試験の発話")
    connects = (Connect(Said(parties.index("target"), "試験の発話"), ("ai", "judge", "judge2"), True),) \
        if "target" in parties and not_reached is None else ()
    return CallReport((ev,) if dnc else (), (ev,) if complaint else (), (ev,) if oos else (),
                      tuple(Party(p, "ai", Said(i, "試験の発話")) for i, p in enumerate(parties)),
                      () if callback is None else (callback,), () if end is None else (End(end, ev),), not_reached,
                      Cut("ai", True, False) if cut == "normal" else cut, apology_delivered, connects=connects,
                      irreversible_check=check)


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
                               for k in ("open", "incomplete", "not_reached_absent", "callback_unspecified_time")}}


def number_run(tx, contact_id) -> dict:
    """番号の欄（DESIGN.md 17-1-3）を、試験が読む形で：n_sure＝番号のせいと決まった続きの数・n＝それに、まだ分からない電話を足した数・
    first_day・last_end（4段目の前の salescall.failed_run と同じ名前。0147 で外した。読むのは状態の欄だけ）。"""
    from salescall.core import health
    num = health.number_of(tx, contact_id)
    return {"n_sure": num.n, "n": num.n + len(num.unknown), "first_day": num.first_day, "last_end": num.last_end}


def line_checking(tx, line_id) -> bool:
    """その回線が、番号のせいか回線のせいかを確かめ中か（回線の欄＝DESIGN.md 17-1-1）。"""
    from salescall.core import health, health_rules
    return health.line_of(tx, line_id).stage in health_rules.CHECKING


def task_stage(tx, task_id) -> str:
    """行の欄の段（データベースの名前。DESIGN.md 17-1-2）。"""
    from salescall.core import health
    return health.stage_name(health.task_of(tx, task_id))


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
                       voices: tuple[str, ...] = VOICES, *, parts: tuple[str, ...] | None = None) -> None:
        """声ごとに、録音の組を作って承認する（契約者の単位＝版なし／依頼者の単位＝その版。架空の録音）。parts＝組に入れる部分の
        名前（無ければ、契約者の単位は契約者の単位の部分のすべて＝recordings.CONTRACTOR_PARTS、依頼者の単位は名乗り）。"""
        unit = "contractor" if version is None else "business"
        names = parts if parts is not None else (recordings.CONTRACTOR_PARTS if unit == "contractor" else ("intro",))
        def work(tx):
            for v in voices:
                sid = recordings.create_set(tx, unit=unit, voice_name=v, version_id=version,
                                            parts=[recordings.Part(n, f"架空の録音（{n}）。", f"test://{v}/{n}", "0" * 64)
                                                   for n in names])
                recordings.approve(tx, sid)
        if version is None:
            self.a.in_tenant(OPERATOR, tenant, work)
            if parts is None:   # 名前の録音の台帳も、試験の業務の初めの目的のお客様（役職のみ「総務部長」）の分と続く言葉をそろえる
                self.approve_names(tenant, [recordings.NameKey(role=DEFAULT_ROLE)], voices)
        else:
            self.a.in_client(OPERATOR, tenant, client, work)

    def approve_names(self, tenant: UUID | None, keys: list, voices: tuple[str, ...] = VOICES, *, roles: tuple[str, ...] = (),
                      tails: tuple[str, ...] | None = None, audio: str = "test") -> None:
        """名前の録音の台帳に、名前の部分（keys＝recordings.NameKey の並び）・役職の部分（roles）・続く言葉（無ければ全部）を作って承認する
        （架空の録音。切れ目＝助詞の置き場は決め打ちしない＝0137。CL-1 の G-65 とユーザーの判断待ち）。tenant が None なら、管理者がシステムの取引で
        作るシステムの単位（すべての契約者が読む。0134）。"""
        P = recordings.Part

        def work(tx):
            for v in voices:
                for r in roles:
                    recordings.approve(tx, recordings.add_name_role(
                        tx, voice_name=v, role=r, part=P("role", f"{r}の", f"{audio}://{v}/role/{r}", "0" * 64)))
                for k in keys:
                    text = f"{k.role}様" if k.reading is None else f"「{k.reading}」様"
                    recordings.approve(tx, recordings.add_name_part(
                        tx, voice_name=v, key=k, part=P("name", text, f"{audio}://{v}/name", "0" * 64)))
                for t in (recordings.NAME_TAILS if tails is None else tails):
                    recordings.approve(tx, recordings.add_name_tail(
                        tx, voice_name=v, tail=t, part=P(t, f"架空の続く言葉（{t}）。", f"{audio}://{v}/{t}", "0" * 64)))
        if tenant is None:
            self.a.in_system(ADMIN, work)
        else:
            self.a.in_tenant(OPERATOR, tenant, work)

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
            tx.execute("INSERT INTO salescall.business_versions (tenant_id, client_id, business_id, version_id, created_by)"
                       " VALUES (%s, %s, %s, %s, %s)", (tenant, client, bid, vid, OPERATOR.name))   # 版の番号はデータベースが決める
            tx.execute("INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
                       " VALUES (%s, %s, %s, 'Q1', 1, 'datetime', true)", (tenant, client, vid))
            put_content(tx, tenant, client, vid)
        self.a.in_client(OPERATOR, tenant, client, work)
        self.approve_voices(tenant, client, vid)
        return bid, vid

    def add_version(self, tenant: UUID, client: UUID, business: UUID) -> UUID:
        """その業務に次の版を足す（版の番号はデータベースが決める＝next_seq。0123）。"""
        vid = uuid.uuid4()

        def work(tx):
            tx.execute("INSERT INTO salescall.business_versions (tenant_id, client_id, business_id, version_id, created_by)"
                       " VALUES (%s, %s, %s, %s, %s)", (tenant, client, business, vid, OPERATOR.name))
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

    def talked(self, s: dict) -> UUID:
        """同じ契約者（s の依頼者）で、こちらの不具合なしにお客様と話し終えた電話を1本足す（別の架空の行。こちらの不具合のあとの「復旧」の
        証拠＝表の E5。DESIGN.md 17章）。電話が終わった所と同じく、その電話の出来事を状態の表を引く入口へ渡す（core/health.after_call）。
        足した試行を返す。"""
        from salescall.core.phone import normalize
        k = self.contact(s["tenant"], s["client"], normalize(fictional_jp_fixed()).e164)
        other = dict(s, task=self.task(s["tenant"], s["client"], s["run"], k), contact=k)
        other["phone"] = self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.one(
            "SELECT phone_e164 FROM salescall.contacts WHERE contact_id = %s", (k,))["phone_e164"])
        aid = self.insert_claim(other)
        for state in ("dialing", "ringing", "answered", "in_conversation", "ending", "ended"):
            self.move(other, aid, state)
        from salescall.core import health
        self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: health.after_call(tx, aid))
        return aid

    def _test_answer(self, s: dict, line: UUID | None, rang: bool) -> bool:
        """その回線のテスト番号の答えを、いま渡す（記録と、状態の表の入口＝core/ring_test.run と同じ順）。確かめ中の回線は、表が鳴らす頼みを
        置いた段（確かめ中 (c)）にしてから渡す（試験の場面を作るため、回線の欄を直に書く。本番で書くのは表を引く入口だけ）。確かめ中
        でなければ何もしない（False）。決め方そのものを試さない試験が、締める前に使う。"""
        import json
        from dataclasses import replace
        from salescall.core import health, health_rules
        line_id = line or s["line"]
        row = owner_access().in_system(SYSTEM, lambda tx: tx.one("SELECT state FROM salescall.line_states WHERE line_id = %s",
                                                                 (line_id,)))
        state = None if row is None else health.decode(row["state"])
        if state is None or state.stage not in health_rules.CHECKING:
            return False
        at = owner_access().in_system(SYSTEM, lambda tx: tx.one("SELECT now() AS t")["t"])
        c = replace(state, stage=health_rules.LineStage.CHECK_C, wait=None, next_at=at + timedelta(minutes=1))
        owner_access().in_system(SYSTEM, lambda tx: tx.execute(
            "UPDATE salescall.line_states SET stage = 'check_c', state = %s WHERE line_id = %s", (json.dumps(health.encode(c)), line_id)))
        owner_access().in_system(SYSTEM, lambda tx: tx.execute(
            "INSERT INTO salescall.line_ring_tests (tenant_id, line_id, purpose, rang) VALUES (%s, %s, 'no_rows', %s)",
            (s["tenant"], line_id, rang)))
        self.a.in_tenant(SYSTEM, s["tenant"], lambda tx: health.tested(tx, line_id, "rang" if rang else "silent"))
        return True

    def test_number_rang(self, s: dict, *, line: UUID | None = None) -> None:
        """その回線のテスト番号が、いま鳴った：その前の、まだ分からない鳴らなかった電話は番号のせいと決まる（表の L-9）。"""
        self._test_answer(s, line, True)

    def test_number_silent(self, s: dict, *, line: UUID | None = None) -> bool:
        """その回線のテスト番号が、いま鳴らなかった：その前の、まだ分からない鳴らなかった電話は回線のせいと決まる（表の L-10）。決めたら
        True。"""
        return self._test_answer(s, line, False)

    def move(self, s: dict, attempt: UUID, state: str, hangup_side: str | None = None) -> None:
        self.a.in_client(SYSTEM, s["tenant"], s["client"], lambda tx: tx.execute(
            "UPDATE salescall.attempts SET state = %s, hangup_side = %s WHERE attempt_id = %s",
            (state, hangup_side, attempt)))


DEFAULT_ROLE = "総務部長"   # 試験の業務の初めの目的のお客様（役職のみ）


def content(target_person: dict | None = None) -> dict:
    """試験の業務設定の中身（架空。質問は Q1＝日時・必須の1つ。業務の決まりは試験ごとに business_rules.save で置く）。"""
    return {"purpose": "架空の業務の目的", "target_class": "corporate",
            "caller": {"company": {"text": "株式会社テスト", "reading": "かぶしきがいしゃてすと"},
                       "person": {"text": "山田", "reading": "やまだ"}, "purpose_line": None},
            "target_person": target_person or {"kind": "role", "role": DEFAULT_ROLE}, "busy_line": None, "voicemail_message": None,
            "ai_answer": None, "answers": [], "voice": "S2",
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
        tx.execute("INSERT INTO salescall.business_versions (tenant_id, client_id, business_id, version_id, created_by)"
                   " VALUES (%s, %s, %s, %s, %s)", (s["tenant"], s["client"], s["business"], vid, OPERATOR.name))
        tx.execute("INSERT INTO salescall.questions (tenant_id, client_id, version_id, question_id, position, type_name, required)"
                   " VALUES (%s, %s, %s, 'Q1', 1, 'datetime', true)", (s["tenant"], s["client"], vid))
        if content_too:
            put_content(tx, s["tenant"], s["client"], vid, target_person)
        business_rules.save(tx, vid, rules_raw())
    w.a.in_client(OPERATOR, s["tenant"], s["client"], work)
    w.approve_voices(s["tenant"], s["client"], vid)
    return vid


def schedule_call(tx, task_id, call_at, *, contact_id, cause: str, promised: bool, purpose: str = "normal",
                  wrong_contact_id=None, callee_at=None, window=None, successor: bool = False,
                  number_again: str | None = None) -> int:
    """試験の場面を作る：次の動きの記録に、かける予定を直に書く（本番では、予定を書くのは次の動きの決め方だけ）。"""
    from salescall.core import next_action
    return next_action._append(tx, task_id, call_at=call_at, end_reason=None, cause=cause, promised=promised,
                               target=next_action.Target(contact_id, None, purpose, wrong_contact_id, successor=successor,
                                                         number_again=number_again or ("wrong_party" if purpose == "ask_number_again"
                                                                                       else None)), callee_at=callee_at,
                               window=window)
