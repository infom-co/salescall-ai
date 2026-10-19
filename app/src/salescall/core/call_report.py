"""会話から返る報告（試行ごとに1つ）。持ち主 CL-2（形は CL-1 の連絡帳 2-2「会話から返すもの」・版 0.4 の「通話の事実」）。

- 形と確かめは、この1か所（`from_json`）。書く（`save`。書き込む入口 core/conversation_io.py から呼ぶ）ときも、
  読む（`load`。締めの処理 core/settle.py が読む）ときも、同じ確かめを通す。合わない報告は受け取らない（ReportInvalid）。
- 「わからない」を既定値で埋めない（原則10）：分からないものは None のまま。判定が「決められない」として扱う。
- 名前（電話口の種別など）は DESIGN.md 4-3・6-2・7-1c のもの。
- **版 3（2026-10-19。CL-1 の連絡帳 4-22）**：会話の側が決める事実を、CL-1 の「通話の事実」の形のまま受け取る（`facts`＝種類 →
  起きた順の記録の並び。どの記録にも**根拠の発話**＝相手の発話の番号 `turn` とその文字 `said`（80字まで）。Intent.md 9-5・11-1）。
  発信禁止の申告・苦情・範囲外の求め・電話口の種別・かけ直しの申し出・締めた理由。判定が読む「申告があったか」「電話口の種別の
  並び」「かけ直し」などは、**この事実から読む**（`do_not_call`・`parties`・`callback` など。同じことを別の欄で二度受け取らない＝
  食い違いようがない）。
  - かけ直しの番号と時刻は、お客様が言ったまま（確かめ済みの言葉）を受け取る。番号をそろえるのは番号の正規化（Q1。
    `phone.normalize(spoken=True)`）、時刻に直すのは `spoken_time.read`（判定が通話の時刻を起点に読む）。どちらも1か所。
  - 版 2 から外したもの：急いでいて終えたか（締めた理由 `end` から読む＝`hurried`）・そろえた番号（言ったままの番号から読む）・
    かけ直しの時刻（言ったままの言葉から読む）。
- お詫びの電話で、文面を伝えられたか（apology_delivered。無ければ None。版 2 から）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core.access import Tx
from salescall.core.phone import normalize

VERSION = 3
FACT_KINDS = ("do_not_call", "complaint", "out_of_scope", "party", "callback", "ai_question", "voicemail_message", "end")
# voicemail_message：留守番電話に伝言を残した（2026-10-19 ユーザー：「留守番電話に入れるのは最初の1回だけ」。同じ依頼者の同じ番号へは
# 2回目を残さない＝`voicemail_left` の1か所。CL-2 の連絡帳 4-106）
# ai_question：「AIですか？」「人間ですか？」と聞かれた（聞かれるたびに1つ。CL-1 の 2-2 の版 0.5＝2026-10-19。判定には使わない。記録だけ）
PARTY_KINDS = ("reception", "target", "voicemail", "ivr", "fax", "wrong_party", "person")   # 4-3（区間ごと）
# person＝お客様が出て話したが、役（受付か目的のお客様か）はまだ分からない（2026-10-19 の見直しで足した。CL-1 の連絡帳への頼み
# ＝CL-2 の 4-105）。⚠️ 版3の事実には「お客様が出た」が無く、目的の人のある業務で役が分かる前に切られた区間と、「誰でも」の業務
# （最初に出たお客様が目的のお客様＝相手を報告しない）の区間は、相手が空になり、判定が「会話に至らなかった」と読んで、黙って切った
# お客様の拒否を登録しなかった（原則14）
PARTY_SOURCES = ("ai", "signal")                         # 会話AIの報告で分かった／音・回線の信号で分かった（CL-1 の `flowctl._party`
#   の by と同じ値。⚠️ 前は "report" を受け、CL-1 の "ai" の報告をまるごと断っていた＝CL-2 の 4-101 の約束と食い違い。見直し6）
CALLBACK_REASONS = ("absent", "other_number", "later", "ask_who", "ask_when", "our_promise")
# かけ直しの日を選んだ理由の周期（お客様が言ったときだけ）：month＝月ぎめの業務・締め・振り込みなど月単位の理由（2026-10-19 ユーザー：
# 「月ぎめの業務の都合や振り込みなどの入金の間隔などの話の場合は、月単位で考えた方がいい」）。無ければ、言葉だけで読む（spoken_time）
CALLBACK_CYCLES = ("month",)
# 不在・別の番号・あとで（お客様に頼まれた。6-2）／分かるお客様・分かる時を聞いた（15-2-3 の a1・a2）／こちらが約束した
END_REASONS = ("done", "callee_hurry", "callee_refused_call", "callee_complaint", "wrong_party", "voicemail", "ivr", "fax",
               "callback", "ai_question")                # 締めた理由（CL-1 の END_LABEL の名前。ai_question＝AIかを重ねて聞かれて終えた）
HURRIED_ENDS = frozenset({"callee_hurry", "callee_refused_call", "callee_complaint"})   # 急いでいる・断られた・怒っている（CL-1 の Flow.hurried と同じ）
NOT_REACHED_REASONS = ("absent", "not_transferred")      # 目的のお客様に繋がらなかった理由（7-1）
SPEAKERS = ("callee", "ai", "none")                      # 切れた瞬間に話していた側（7-1c）
SAID_MAX = 80                                            # 根拠の発話の文字の長さの上限（CL-1 の _note と同じ）


class ReportInvalid(Exception):
    """報告の形が違う（理由は日本語）。"""


@dataclass(frozen=True)
class Said:
    """根拠の発話：相手の発話の番号（通話の初めから数えた番号）と、その文字（会話AIの聞き取り。無ければ空）。"""
    turn: int
    said: str


@dataclass(frozen=True)
class Party:
    party: str                   # 電話口の種別
    by: str                      # report（会話AIの報告）／signal（音・回線の信号）
    evidence: Said


@dataclass(frozen=True)
class Callback:
    reason: str
    time_said: str | None        # かけ直す時刻：お客様が言ったまま（確かめ済み。取れなかったら None）。時刻に直すのは spoken_time.read
    number_said: str | None      # 別の番号：お客様が言ったまま（確かめ済み。取れなかったら None。reason = other_number のときだけ）
    person: str | None           # 分かるお客様（a1）
    evidence: Said
    cycle: str | None = None     # その日を選んだ理由の周期（CALLBACK_CYCLES。お客様が言ったときだけ）

    @property
    def number_e164(self) -> str | None:
        """番号の正規化（Q1）でそろえた番号（そろえられたときだけ。内線つき・桁が足りないなども、言ったままの番号は依頼者へ渡す。
        DESIGN.md 3-5）。"""
        return None if self.number_said is None else normalize(self.number_said, spoken=True).e164


@dataclass(frozen=True)
class End:
    reason: str
    evidence: Said


@dataclass(frozen=True)
class Cut:
    speaker: str | None          # 切れた瞬間に話していた側（分からなければ None）
    in_closing: bool             # 締めの言葉に入っていた・締めのあとだった
    callee_refused_last: bool    # お客様の最後の発話が、断りの言葉だった


@dataclass(frozen=True)
class CallReport:
    # 通話の事実（CL-1 の facts。種類ごとに、起きた順の記録）
    refusals: tuple[Said, ...]           # do_not_call：発信の拒否（営業拒否）の申告（原則14）
    complaints: tuple[Said, ...]         # complaint：苦情・怒り
    out_of_scope_requests: tuple[Said, ...]   # out_of_scope：範囲外の求めで終えた
    party_reports: tuple[Party, ...]     # party：電話口の種別（区間ごと）
    callbacks: tuple[Callback, ...]      # callback：かけ直しの申し出（いちばん新しいものが効く）
    ends: tuple[End, ...]                # end：締めた理由（いちばん新しいものが効く）
    # 会話の側の、そのほかの材料
    not_reached: str | None              # 目的のお客様に繋がらなかった理由
    cut: Cut | None                      # 切れ方の材料（お客様が出なかった通話は None）
    apology_delivered: bool | None = None   # お詫びの電話だけ：その通話で話したお客様に、文面を伝えられたか（10-1-2）
    ai_questions: tuple[Said, ...] = ()  # 事実 ai_question：「AIですか？」と聞かれた（記録だけ。CL-1 の版 0.5）
    voicemail_messages: tuple[Said, ...] = ()   # 事実 voicemail_message：留守番電話に伝言を残した

    # ── 判定が読むもの（どれも事実から読む。ここの1か所）
    @property
    def do_not_call(self) -> bool:
        return bool(self.refusals)

    @property
    def complaint(self) -> bool:
        return bool(self.complaints)

    @property
    def out_of_scope(self) -> bool:
        return bool(self.out_of_scope_requests)

    @property
    def parties(self) -> tuple[str, ...]:
        return tuple(p.party for p in self.party_reports)

    @property
    def reached_target(self) -> bool:
        """目的のお客様と話せたか（1か所。判定の「目的のお客様に繋がらなかった」と、約束の日のお詫びの印を外す締めが読む）。"""
        return "target" in self.parties

    @property
    def callback(self) -> Callback | None:
        return self.callbacks[-1] if self.callbacks else None

    @property
    def end_reason(self) -> str | None:
        return self.ends[-1].reason if self.ends else None

    @property
    def hurried(self) -> bool:
        """お客様が急いでいる・通話を断られた・怒っている、で締めたか。"""
        return self.end_reason in HURRIED_ENDS

    def to_json(self) -> dict[str, Any]:
        def said(s: Said) -> dict[str, Any]:
            return {"turn": s.turn, "said": s.said}
        facts: dict[str, list[dict[str, Any]]] = {
            "do_not_call": [said(s) for s in self.refusals],
            "complaint": [said(s) for s in self.complaints],
            "out_of_scope": [said(s) for s in self.out_of_scope_requests],
            "party": [{**said(p.evidence), "party": p.party, "by": p.by} for p in self.party_reports],
            "callback": [{**said(c.evidence), "why": c.reason, "time": c.time_said, "number": c.number_said,
                          "person": c.person, **({"cycle": c.cycle} if c.cycle else {})} for c in self.callbacks],
            "ai_question": [said(q) for q in self.ai_questions],
            "voicemail_message": [said(v) for v in self.voicemail_messages],
            "end": [{**said(e.evidence), "reason": e.reason} for e in self.ends]}
        return {"version": VERSION, "facts": {k: v for k, v in facts.items() if v}, "not_reached": self.not_reached,
                "cut": None if self.cut is None else {"speaker": self.cut.speaker, "in_closing": self.cut.in_closing,
                                                      "callee_refused_last": self.cut.callee_refused_last},
                "apology_delivered": self.apology_delivered}


def _bool(d: dict, key: str) -> bool:
    v = d.get(key)
    if not isinstance(v, bool):
        raise ReportInvalid(f"{key} は true / false です")
    return v


def _choice(v: Any, allowed: tuple[str, ...], key: str, *, optional: bool = False) -> str | None:
    if v is None and optional:
        return None
    if v not in allowed:
        raise ReportInvalid(f"{key} は {allowed} のどれかです（{v!r}）")
    return v


def _keys(d: Any, allowed: set[str], where: str) -> dict:
    if not isinstance(d, dict):
        raise ReportInvalid(f"{where} の形が違います")
    extra = set(d) - allowed
    if extra:
        raise ReportInvalid(f"{where} に知らない項目があります：{sorted(extra)}")
    return d


def _text(v: Any, key: str) -> str | None:
    """言ったままの言葉（無ければ None。空の文字は受け取らない）。"""
    if v is not None and (not isinstance(v, str) or not v.strip()):
        raise ReportInvalid(f"{key} は、お客様が言ったままの言葉（文字）です")
    return v


def _records(facts: dict, kind: str, extra: set[str]) -> list[tuple[dict, Said]]:
    """その種類の記録の並び（根拠の発話を確かめて取り出す）。"""
    rows = facts.get(kind, [])
    if not isinstance(rows, list):
        raise ReportInvalid(f"facts.{kind} は記録の並びです")
    out = []
    for i, r in enumerate(rows):
        where = f"facts.{kind}[{i}]"
        r = _keys(r, {"turn", "said"} | extra, where)
        turn, said = r.get("turn"), r.get("said")
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 0:
            raise ReportInvalid(f"{where}.turn は相手の発話の番号（0 以上の整数）です")
        if not isinstance(said, str) or len(said) > SAID_MAX:
            raise ReportInvalid(f"{where}.said は根拠の発話の文字（{SAID_MAX} 字まで）です")
        out.append((r, Said(turn, said)))
    return out


def from_json(raw: Any) -> CallReport:
    """報告を確かめて読む（ただ1つの確かめ）。"""
    d = _keys(raw, {"version", "facts", "not_reached", "cut", "apology_delivered"}, "報告")
    if d.get("version") != VERSION:
        raise ReportInvalid(f"報告の版が違います（{d.get('version')!r}。この部品は {VERSION}）")
    facts = _keys(d.get("facts"), set(FACT_KINDS), "facts")
    refusals = tuple(s for _, s in _records(facts, "do_not_call", set()))
    complaints = tuple(s for _, s in _records(facts, "complaint", set()))
    oos = tuple(s for _, s in _records(facts, "out_of_scope", set()))
    parties = tuple(Party(_choice(r.get("party"), PARTY_KINDS, "facts.party.party"),
                          _choice(r.get("by"), PARTY_SOURCES, "facts.party.by"), s)
                    for r, s in _records(facts, "party", {"party", "by"}))
    callbacks = []
    for r, s in _records(facts, "callback", {"why", "time", "number", "person", "cycle"}):
        reason = _choice(r.get("why"), CALLBACK_REASONS, "facts.callback.why")
        number = _text(r.get("number"), "facts.callback.number")
        if number is not None and reason != "other_number":
            raise ReportInvalid("番号を持つのは、別の番号のかけ直しだけです")
        callbacks.append(Callback(reason, _text(r.get("time"), "facts.callback.time"), number,
                                  _text(r.get("person"), "facts.callback.person"), s,
                                  _choice(r.get("cycle"), CALLBACK_CYCLES, "facts.callback.cycle", optional=True)))
    ends = tuple(End(_choice(r.get("reason"), END_REASONS, "facts.end.reason"), s)
                 for r, s in _records(facts, "end", {"reason"}))
    not_reached = _choice(d.get("not_reached"), NOT_REACHED_REASONS, "not_reached", optional=True)
    if not_reached is not None and any(p.party == "target" for p in parties):
        raise ReportInvalid("目的のお客様に繋がったのに、繋がらなかった理由があります")
    cut = None
    if d.get("cut") is not None:
        c = _keys(d["cut"], {"speaker", "in_closing", "callee_refused_last"}, "cut")
        cut = Cut(_choice(c.get("speaker"), SPEAKERS, "cut.speaker", optional=True), _bool(c, "in_closing"),
                  _bool(c, "callee_refused_last"))
    delivered = d.get("apology_delivered")
    if delivered is not None and not isinstance(delivered, bool):
        raise ReportInvalid("apology_delivered は true / false です")
    ai_questions = tuple(s for _, s in _records(facts, "ai_question", set()))
    voicemail = tuple(s for _, s in _records(facts, "voicemail_message", set()))
    if voicemail and "voicemail" not in (p.party for p in parties):
        raise ReportInvalid("留守番電話の伝言は、電話口の種別が留守番電話の通話だけです")
    return CallReport(refusals, complaints, oos, parties, tuple(callbacks), ends, not_reached, cut, delivered, ai_questions,
                      voicemail)


def save(tx: Tx, attempt_id: UUID, report: CallReport) -> None:
    """試行の報告を置く（1回だけ。変更不可）。形はもう一度確かめる（書く前の確かめも、読むときと同じ1つ）。"""
    data = from_json(report.to_json()).to_json()
    tx.execute("INSERT INTO salescall.call_reports (tenant_id, client_id, attempt_id, report)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s)",
               (attempt_id, json.dumps(data, ensure_ascii=False)))


def save_rebuilt(tx: Tx, attempt_id: UUID, report: CallReport, *, evidence: str) -> int:
    """通話の録音から作り直した報告を置く（会話の担当が呼ぶ。「判定できない」の行を判定し直すため。0021）。元の報告は変えない。
    形の確かめは、ふつうの報告と同じ1つ。"""
    data = from_json(report.to_json()).to_json()
    return tx.one("INSERT INTO salescall.call_report_rebuilds (tenant_id, client_id, attempt_id, report, evidence, rebuilt_by)"
                  " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, salescall.cur_actor())"
                  " RETURNING rebuild_id", (attempt_id, json.dumps(data, ensure_ascii=False), evidence))["rebuild_id"]


def load_with_source(tx: Tx, attempt_id: UUID) -> tuple[CallReport | None, str | None]:
    """報告と、その出どころ（original＝元の報告／rebuild:番号＝作り直し）。作り直しがあれば、いちばん新しいものを使う。"""
    row = tx.one("SELECT report, rebuild_id FROM salescall.call_report_rebuilds WHERE attempt_id = %s"
                 " ORDER BY rebuild_id DESC LIMIT 1", (attempt_id,))
    if row is not None:
        return from_json(row["report"]), f"rebuild:{row['rebuild_id']}"
    row = tx.one("SELECT report FROM salescall.call_reports WHERE attempt_id = %s", (attempt_id,))
    return (None, None) if row is None else (from_json(row["report"]), "original")


def load(tx: Tx, attempt_id: UUID) -> CallReport | None:
    return load_with_source(tx, attempt_id)[0]


def voicemail_left(tx: Tx, phone_e164: str) -> bool:
    """同じ依頼者（取引の依頼者）の、その番号へ、もう留守番電話の伝言を残したか（1か所。2026-10-19 ユーザー：「留守番電話に入れるのは
    最初の1回だけ」・数える単位は「同じ依頼者のすべての一覧で、同じお客様（同じ番号）」）。会話の処理は、伝言を残す前にこれを読み、
    残していれば何も言わずに切る（依頼者をまたいでは数えない＝サービスが別）。作り直した報告（call_report_rebuilds）も見る。"""
    return tx.one(
        "SELECT EXISTS (SELECT 1 FROM salescall.attempts a WHERE a.phone_e164 = %s AND ("
        "  EXISTS (SELECT 1 FROM salescall.call_reports r WHERE r.attempt_id = a.attempt_id"
        "          AND jsonb_array_length(coalesce(r.report -> 'facts' -> 'voicemail_message', '[]'::jsonb)) > 0)"
        "  OR EXISTS (SELECT 1 FROM salescall.call_report_rebuilds b WHERE b.attempt_id = a.attempt_id"
        "          AND jsonb_array_length(coalesce(b.report -> 'facts' -> 'voicemail_message', '[]'::jsonb)) > 0))) AS left",
        (phone_e164,))["left"]
