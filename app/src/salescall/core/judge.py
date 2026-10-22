"""業務の判定（Q5「この業務は完了か」）。持ち主 CL-2。DESIGN.md 7-1・7-1c。

- **入力から結果が1つに決まる処理**（データベースを読まない・書かない。時計も読まない）。同じ入力からは、いつも同じ結果。
  版（`JUDGE_VERSION`）を結果と一緒に記録する（判定の決まりを変えたら版を上げる）。
- 入力を集めて結果を書くのは、締めの処理（core/settle.py）の1か所だけ。
- 「わからない」を値で埋めない（原則10）：入力に抜けがあれば `unknown`（判定の処理か入力の誤り）。原因には決まった名前
  （`UNKNOWN_CAUSES`。中身の `unknown_cause`）を付ける：同じ原因が何件かを数えるため（品質の段。2026-10-16 ユーザー決定）。
  その行は終えずに「判定待ち」で残し、原因が直ったら判定し直す（次の動き・締めの処理）。
- 切れ方の判定（7-1c）も、ここの1か所（`classify_cut`）。門の「数えない発信」は、その結果（試行に記録）を読む。

上から順に、最初に当たったものが答え：
  (0) FAX の回線（電話口が FAX だけ。お客様の声が無い）                               ⇒ out_of_scope（FAX）。その番号は以後かけない
      （発信禁止 fax_line。その番号だけ。Intent.md 9-4。2026-10-17 の計画書との突き合わせ：前は「会話に至らなかった」として
      かけ直していた）。お客様が言った番号が FAX だったときは、番号違いと同じく行を終えずに確かめて聞き直す
  (1) 番号違い（電話口の種別に wrong_party）                                          ⇒ out_of_scope（番号違い）
      ただし、お客様が言った番号へかけた通話なら、行は終えない（2026-10-15 ユーザー。DESIGN.md 3-5）
                                                                                  ⇒ not_reached（言われた番号が番号違い）
      番号違いの電話口が断った・何も言わずに切ったときも、ここ（止めるのは、その番号だけ。行のお客様が断ったのではない
      ので、行の番号すべて・管理番号へ広げない。DESIGN.md 3-6。2026-10-16 見直し：前は断りを先に見て、番号違いの相手の
      断りで、行のお客様ごと止めていた）
  (2) 発信の拒否（通話中の申告・通話後の検証で見つかった・切れ方の判定が「断った」）  ⇒ do_not_call
      (1)(2) は `refusal` の1か所（お詫びの電話の締めも使う）。はっきりした拒否は、切れ方の材料が無くても拒否
  (3) 範囲外の求めで終えた                                                          ⇒ out_of_scope
  (4) 用件を断られた（業務の決まりの「断りの答え」が取れた）                          ⇒ declined
  (5) 必須の質問がすべて取れた（confirmed・accepted。4-4 の定義）                     ⇒ completed
  (6) 取れていない必須の質問の扱いが「結論として終える」                              ⇒ concluded（結論の名前）
  (7) かけ直しの約束（お客様に頼まれた・分かるお客様／時を聞いた・こちらが約束した）          ⇒ callback_scheduled
      読むのは、いちばん新しい申し出（その質問の途中で目的のお客様に繋がった申し出＝要らなくなったものは読まない。
      call_report.CallReport.callback の1か所。2026-10-22 CL-1 の連絡帳 4-27）
      時刻は、お客様が言ったままの言葉を通話の時刻から「かけてよい時間の幅」として読む（core/spoken_time.read の1か所。
      2026-10-19 ユーザー）。ちょうどの時刻・幅 ⇒ その幅の中でかける。時刻を任せる答え・時刻を聞けなかった ⇒ 時刻の決まっていない
      約束（再発信の方針）。1つに決まらない言い方（「昼」など）を通話の中で確かめないまま終わった ⇒ unknown（直すのは会話＝CL-1）。
      読めない言い方 ⇒ unknown（直すのは読み方＝CL-2）。どちらも推し量って別の時刻にかけない（直したら判定し直す）
      ただし、不在で戻る時刻が取れなかった申し出（理由 absent・時刻の言葉が無い／任せる）は約束にしない ⇒ not_reached（不在。2026-10-13
      ユーザー：「不在」はかけ直しの回数の上限までかけ直す）
  (8) お客様と話したが、目的のお客様に繋がらなかった                                          ⇒ not_reached（不在／取り次いでもらえなかった）
      繋がったか・話したかは call_report の1か所（`reached_target`・`talked`）：目的のお客様のある通話は、繋いだ事実（connect）だけで
      繋がったと読む。繋がらなかった理由・かけ直しの申し出がある報告は、電話口の種別が無くても「話した」。取り次いでもらえなかった
      （事情があり取り次げなかった）⇒ システムが日を改めて1回（business_rules.SYSTEM_POLICIES。2026-10-22 ユーザー）
  (9) 会話に至らなかった（お客様が出なかった・お客様の声でなかった）                              ⇒ open
  (10) 必須の質問が1つでも取れていない                                               ⇒ incomplete
  (11) どれでもない（入力の誤り）                                                      ⇒ unknown
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from salescall.core import spoken_time
from salescall.core.business_rules import Rules
from salescall.core.call_report import CallReport

# 判定の決まりの版（変えたら上げる）。かけ直しの時刻の読み方の版も入れる（読み方を直すと、判定待ちの行が判定し直される＝settle.rejudge）
JUDGE_VERSION = f"judge-2026-10-22.4+time-{spoken_time.VERSION}"   # 2026-10-22.4：不在で戻る時刻が取れなかった申し出は「不在」（約束にしない）。.3：CL-1 の 10-22 の報告を受ける（目的のお客様のある通話は繋いだ事実だけで繋がった・繋がらなかった理由／かけ直しの申し出があれば話した・繋がって要らなくなったかけ直しは読まない。読み方は call_report の1か所）（.2：確かめ直しで目的のお客様だと確かめられなかった通話は「取り次いでもらえなかった」。.1：繋いだ事実で話した・繋がったと読む）
# 「判定できない」の原因の名前（同じ原因かを数える単位）と意味。プログラムの誤りか（直す担当へ修正依頼を出し、管理者にも
# 知らせる＝確かめ・承認のため。2026-10-16・10-17 ユーザー）も、ここで決まる（1か所）。業務設定の抜けは契約者が直す（いまは無い：業務の決まりは、必須の質問ごとに、
# 決まった理由のすべてに扱いが無いと保存できない＝business_rules。2026-10-16 見直し：前は「理由×扱いが決まらない」を業務設定の
# 抜けとしていたが、起きるのは会話の処理が理由を書かなかった・一覧に無い理由を書いたときだけ）。
UNKNOWN_CAUSES: dict[str, str] = {
    "report_missing": "お客様が出た通話なのに、会話の報告が無い（会話の処理の誤り。直すのは CL-1）",
    "cut_missing": "お客様と話した通話なのに、切れ方の材料が報告に無い（会話の処理の誤り。直すのは CL-1）",
    "reason_missing": "取れていない必須の質問の「わからない」「答えない」に、理由が無い・決まった理由のどれでもない"
                      "（会話の処理の誤り。直すのは CL-1）",
    "not_reached_reason_missing": "目的のお客様に繋がらなかった理由が報告に無い（会話の処理の誤り。直すのは CL-1）",
    "callback_time_unreadable": "お客様が言ったかけ直しの時刻の言葉を、時刻に読めない（読み方 core/spoken_time の抜け。直すのは CL-2）",
    "callback_time_unconfirmed": "お客様が言ったかけ直しの時刻が1つに決まらない言い方（「昼」・午前か午後か など）のまま、通話の中で"
                                 "確かめずに終わった（会話の処理の誤り。直すのは CL-1。確かめるかは spoken_time.read が答える）",
}
# プログラムの誤りと、直す担当（止めずに修正依頼を出す。人は確かめ・承認だけ＝2026-10-17 ユーザー）
UNKNOWN_CAUSE_FIXERS: dict[str, str] = {"report_missing": "CL-1", "cut_missing": "CL-1", "reason_missing": "CL-1",
                                        "not_reached_reason_missing": "CL-1", "callback_time_unreadable": "CL-2",
                                        "callback_time_unconfirmed": "CL-1"}
PROGRAM_CAUSES = frozenset(UNKNOWN_CAUSE_FIXERS)
TAKEN = frozenset({"confirmed", "accepted"})                  # 「取れた」の定義（4-4。ここで2つ目を作らない）


@dataclass(frozen=True)
class Item:
    state: str
    condition: str | None        # 選んだ選択肢
    reason: str | None           # 「わからない」「答えない」の理由（15-2-3）


@dataclass(frozen=True)
class Facts:
    """試行の事実（データベースの試行の記録から）。"""
    end_state: str
    hangup_side: str | None      # 回線の信号（dropped のときだけ。callee / line / unknown）
    reached_answer: bool         # お客様が出た（応答まで進んだ）ことがあるか
    dialed_callee_number: bool   # かけた番号が、お客様が言った別の番号か（行の番号の表で callee。3-5）
    called_at: datetime          # 通話の終わりの時刻（お客様が言った「明日」「30分後」などの起点。お客様が言った時刻にいちばん
                                 # 近い。⚠️ 前は発信の時刻で、長い通話では「30分後」が言った時から早まった＝2026-10-19 の見直し）


@dataclass(frozen=True)
class JudgeInput:
    facts: Facts
    report: CallReport | None    # 会話から返る報告（会話が無かった試行は None）
    items: dict[str, Item]       # 行の項目（試行をまたいでたまったもの）
    rules: Rules
    refused_after: bool          # 通話後の検証で、拒否の聞き逃しが見つかった（Q21 が発信禁止に登録した）
    unclear_is_refusal: bool     # 切れ方を決められないとき、営業拒否として扱うか（設定 judge.unclear_disconnect_is_refusal）
    callee_hours: tuple[int, int] = spoken_time.DEFAULT_BUSINESS_HOURS   # お客様の営業時間（設定 callee.business_hours。午前・午後を決める）
    periods: dict[str, tuple[int, int]] = field(default_factory=lambda: spoken_time.PERIODS)   # 言葉ごとの幅（依頼者ごとの設定）
    # 通話の途中の「もうかけないで」が、その場で台帳に入っている（dnc.register_declared）。⚠️ 会話の処理が報告を書く前に
    # 落ちても、申告は台帳にある：報告だけを見ると、断りを見落として、行のほかの番号へ広げず、お詫びしてかけ直そうとした
    # （2026-10-19 の見直し）
    declared_in_call: bool = False
    # 目的のお客様に判定なしで繋いだ通話を確かめ直して、目的のお客様だと確かめられなかった（別の方・確かめられなかった。
    # core/target_recheck.py）。その通話の答えは、もう戻されている・「-」になっている。結果の中身に残す
    target_unconfirmed: bool = False

    @property
    def reached_target(self) -> bool:
        """目的のお客様と話せたか（判定の (8)「目的のお客様に繋がらなかった」と、締めの約束の日のお詫びの印を外す所が読む1か所）：
        会話の報告で繋がっていて（call_report.CallReport.reached_target）、確かめ直しで目的のお客様だと確かめられなかった通話でない
        （2026-10-22 の見直し：前は判定が「繋がった」、締めが「繋がらなかった」と別々に読み、別の方だった約束の電話を「一部だけ取れた」
        にして、約束の幅を外していた）。"""
        return self.report is not None and self.report.reached_target and not self.target_unconfirmed


@dataclass(frozen=True)
class Judgement:
    state: str                   # 業務の状態（4-1）
    complaint: bool
    disconnect: str | None       # 試行に記録する切れ方（normal / refused / interrupted。決められないものは設定で決めたほう）
    dnc_reason: str | None       # 発信禁止に登録する理由（refused_in_call / refused_found_after / wrong_party / fax_line）
    detail: dict[str, Any]       # 判定の中身（依頼者への報告・次の動きが読む。policy_key＝再発信の方針の鍵）


def classify_cut(facts: Facts, report: CallReport | None) -> str | None:
    """切れ方の判定（7-1c。2026-10-14 ユーザー承認）。お客様と話した通話で、ふつうに終えたのでないときだけ決める。
    返す値：normal / refused / interrupted / unclear（材料はあるが決められない）／missing（材料が報告されていない＝入力の誤り）。
    対象でなければ None。⚠️ missing を unclear と同じに扱わない（報告の書き損じで、ふつうに終えたお客様を営業拒否にしない）。"""
    if not facts.reached_answer or facts.end_state not in ("ended", "dropped") or report is None:
        return None
    if not report.talked:
        return None                              # 留守電・自動音声・FAX は、お客様の意思が無い
    cut = report.cut
    if cut is None:
        return "missing"
    if cut.in_closing:
        return "normal"                          # 締めの言葉に入っていた・締めのあと
    if cut.callee_refused_last:
        return "refused"                         # 断りの言葉のあとに切れた
    if cut.speaker == "callee":
        return "interrupted"                     # お客様の言葉が途中で途切れた
    if facts.hangup_side == "line":
        return "interrupted"                     # 回線の信号で、回線の側で切れたと分かる
    if cut.speaker in ("ai", "none"):
        return "refused"                         # AIが話している途中・話し終えたあとの間に、お客様が何も言わずに切った
    return "unclear"


def cut_off(*, reached_answer: bool, end_state: str, raw_cut: str | None, hangup_side: str | None) -> bool:
    """途中で切れた＝お客様が出たあと、お客様の意思と関係なく切れた（こちらの仕組みが止まった／回線・言葉の途中で切れた）。復旧次第、
    お詫びとともにすぐかけ直す（2026-10-17 ユーザー：「仕組みが途中で止まった通話や、回線が途中で切れた通話は復旧次第、お詫びと
    ともにすぐかけ直さないといけないでしょ！」）。**決めるのはこの1か所**。入れないもの（2026-10-18 の見直し）：回線の信号が
    「お客様が切った」もの（お客様が自分で切った）・切れ方を決められなかったもの（依頼者の設定で「断り」としない場合も、
    途中で切れたとは分からない）。raw_cut＝依頼者の設定で写す前の切れ方（`classify_cut`）。"""
    if not reached_answer:
        return False
    return end_state == "technical_error" or (raw_cut == "interrupted" and hangup_side != "callee")


def judge(inp: JudgeInput) -> Judgement:
    return refusal(inp) or _business(inp)


def _cut(inp: JudgeInput) -> tuple[str | None, str | None]:
    raw_cut = classify_cut(inp.facts, inp.report)
    return raw_cut, {"unclear": "refused" if inp.unclear_is_refusal else "interrupted", "missing": None}.get(raw_cut, raw_cut)


def refusal(inp: JudgeInput) -> Judgement | None:
    """発信の拒否と番号違いの決め方（判定の (1)(2)。この1か所。お詫びの電話の締めも、これを使う＝2026-10-16 の見直し J6）。
    決まれば結果、拒否も番号違いも無ければ None（業務の中身の判定へ進む）。断ったかを決められない（会話の報告が無い・
    切れ方の材料が無い）ときは unknown。⚠️ はっきりした拒否（通話中の申告・通話後の検証で見つかった）は、切れ方の材料が
    無くても拒否（前は材料の抜けを先に見て unknown にし、発信禁止を登録しなかった＝2026-10-16 の見直し J9）。"""
    r = inp.report
    raw_cut, cut = _cut(inp)
    complaint = bool(r and r.complaint)
    detail: dict[str, Any] = _detail(inp, raw_cut, cut)

    def done(state: str, *, dnc: str | None = None, **more: Any) -> Judgement:
        return Judgement(state, complaint, cut, dnc, {**detail, **more})

    def unknown(cause: str, error: str) -> Judgement:
        return Judgement("unknown", complaint, None, None, {**detail, "unknown_cause": cause, "error": error})

    # お客様が出たのに会話の報告が無い：結果不明（処理が落ちた）なら、何も分からないまま扱う（途中で切れた通話として、復旧次第
    # お詫びとともにすぐかけ直す＝`cut_off`。2026-10-17 ユーザー）。
    # それ以外は入力の誤り（報告を書き損ねた）。決めつけない
    if inp.facts.reached_answer and r is None and inp.facts.end_state != "technical_error":
        if inp.declared_in_call:   # 報告は無くても、通話の途中の申告が台帳にある：はっきりした拒否が勝つ（2026-10-19 の見直し）
            return done("do_not_call", dnc="refused_in_call", refusal="in_call")
        if inp.refused_after:   # 報告は無くても、通話後の検証が録音から拒否を見つけた：はっきりした拒否が勝つ（2026-10-17 の見直し）
            return done("do_not_call", dnc="refused_found_after", refusal="found_after")
        return unknown("report_missing", "お客様が出た通話なのに、会話の報告がありません")
    # (0) FAX の回線：お客様に届かない番号。その番号だけ止める（番号違いと同じ扱い。行のお客様が断ったのではない）
    if r and "fax" in r.parties and not r.talked:
        if inp.facts.dialed_callee_number:   # 言われた番号が FAX だった：その番号だけ止め、番号を確かめて聞き直す（次の動き）
            return done("not_reached", dnc="fax_line", not_reached="wrong_alternate", fax_line=True)
        return done("out_of_scope", dnc="fax_line", out_of_scope="fax_line")
    # (1) 番号違い（その番号の持ち主の断りも、その番号だけを止める）
    if r and "wrong_party" in r.parties:
        refused = {"wrong_party_refused": True} if r.do_not_call or inp.declared_in_call or cut == "refused" \
            or inp.refused_after else {}
        if inp.facts.dialed_callee_number:   # 言われた番号が違った：その番号だけ止め、番号を確かめて聞き直す（次の動き）
            return done("not_reached", dnc="wrong_party", not_reached="wrong_alternate", **refused)
        return done("out_of_scope", dnc="wrong_party", out_of_scope="wrong_party", **refused)
    # (2) 発信の拒否（原則14）：はっきりした拒否が先、切れ方はそのあと
    if (r and r.do_not_call) or inp.declared_in_call:   # 申告の台帳も見る（報告が無い・報告から抜けた＝結果不明の通話も）
        return done("do_not_call", dnc="refused_in_call", refusal="in_call")
    if inp.refused_after:
        return done("do_not_call", dnc="refused_found_after", refusal="found_after")
    if raw_cut == "missing":
        return unknown("cut_missing", "お客様と話した通話なのに、切れ方の材料（切れた瞬間に話していた側など）が報告されていません")
    if cut == "refused":
        return done("do_not_call", dnc="refused_in_call", refusal="cut")
    return None


def _detail(inp: JudgeInput, raw_cut: str | None, cut: str | None) -> dict[str, Any]:
    """判定の中身の初め（切れ方と、目的のお客様だと確かめられなかったか。1か所）。"""
    return {"cut": raw_cut, "cut_recorded": cut, **({"target_unconfirmed": True} if inp.target_unconfirmed else {})}


def _business(inp: JudgeInput) -> Judgement:
    """業務の中身の判定（(3) から。発信の拒否・番号違いが無かった通話）。"""
    r, rules = inp.report, inp.rules
    raw_cut, cut = _cut(inp)
    complaint = bool(r and r.complaint)
    detail: dict[str, Any] = _detail(inp, raw_cut, cut)

    def done(state: str, *, dnc: str | None = None, **more: Any) -> Judgement:
        return Judgement(state, complaint, cut, dnc, {**detail, **more})

    def unknown(cause: str, error: str) -> Judgement:
        return Judgement("unknown", complaint, None, None, {**detail, "unknown_cause": cause, "error": error})

    # (3) 範囲外
    if r and r.out_of_scope:
        return done("out_of_scope", out_of_scope="request")
    # (4) 用件を断られた（断りの答えが「取れた」）
    for q, answers in rules.decline.items():
        it = inp.items.get(q)
        if it is not None and it.state in TAKEN and it.condition in answers:
            return done("declined", declined_by=q)
    # (5) 必須の質問がすべて取れた
    missing = [q for q in rules.required if q not in inp.items or inp.items[q].state not in TAKEN]
    if not missing:
        return done("completed")
    # (6) 取れていない必須の質問の扱い（「わからない」「答えない」は理由×扱いの宣言どおり。宣言が無ければ入力の誤り）。
    #     扱いが「結論として終える」の項目があれば、結論で終える（会話の側は、その扱いで丁寧に締めて終えるので、あとの質問は
    #     聞かれない。⛔ 2026-10-17 取り下げ：前日に「取れていない必須のどれもが結論のときだけ」に変えたが、扱いの表
    #     ＝DESIGN.md 15-2-3「結論として終える：丁寧に締める・終える」と食い違い、聞かれなかった質問のために上限までかけ直した）
    for q in missing:
        it = inp.items.get(q)
        if it is None or it.state not in ("dont_know", "refused"):
            continue
        h = rules.handling.get((q, it.state, it.reason)) if it.reason is not None else None
        if h is None:
            return unknown("reason_missing", f"質問 {q} の {it.state} に、決まった理由が無い（理由 {it.reason!r}）")
        if h.kind == "conclude":
            return done("concluded", conclusion=h.conclusion, concluded_by=q)
    # (7) かけ直しの約束
    if r and r.callback is not None:
        cb = r.callback
        when = spoken_time.read(cb.time_said, inp.facts.called_at, inp.callee_hours, inp.periods, monthly=cb.cycle == "month")
        if when.kind == spoken_time.UNREADABLE:
            return unknown("callback_time_unreadable", f"かけ直しの時刻「{cb.time_said}」を読めません（{when.why}）")
        if when.kind == spoken_time.CONFIRM:
            return unknown("callback_time_unconfirmed", f"かけ直しの時刻「{cb.time_said}」が1つに決まりません（{when.why}）")
        if cb.reason == "absent" and when.kind == spoken_time.UNSPECIFIED:
            # 不在で、戻る時刻が取れなかった：約束ではなく「不在」（再発信の方針 not_reached_absent＝かけ直しの回数の上限まで）。
            # 2026-10-13 ユーザー：「戻る時刻が聞けたときは『折り返し予定』…⇒はい」「『不在』のときは、かけ直しの回数の上限までかけ直す
            # 案です。⇒OK」。CL-1 は不在のとき、戻る時刻を聞けなくても、かけ直しの申し出（理由 absent）を残す。⛔ 2026-10-22 の見直し13：
            # 前は時刻の決まっていない約束（callback_unspecified_time）にして、回数の上限の外でかけ続けえた
            return done("not_reached", not_reached="absent", policy_key="not_reached_absent")
        iso = lambda t: None if t is None else t.isoformat()   # noqa: E731
        # at＝幅の始まり（ちょうどの時刻ならその時刻）・until＝幅の終わり（無ければ終わり無し）・daily＝1日の中の幅（秒）・
        # exact＝お客様が時刻を指定した（その時刻を含むNG時間の枠でもかけてよい）・since＝その回の本当の始まり・repeat＝話せなかった
        # ときの次の回（お客様がその日を選んだ理由。spoken_time.Reading.repeat）
        return done("callback_scheduled", callback={"reason": cb.reason, "at": iso(when.start), "until": iso(when.end),
                                                     "since": iso(when.since), "repeat": when.repeat, "cycle": cb.cycle,
                                                     "daily": None if when.daily is None else list(when.daily),
                                                     "days": None if when.days is None else list(when.days),
                                                     "exact": when.exact, "time_said": cb.time_said,
                                                     "number_said": cb.number_said, "number_e164": cb.number_e164,
                                                     "person": cb.person},
                    policy_key=None if when.start else "callback_unspecified_time")
    human = bool(r and r.talked)
    conversation = inp.facts.reached_answer and human
    # (8) お客様と話したが、目的のお客様に繋がらなかった。確かめ直しで目的のお客様だと確かめられなかった通話（別の方が「はい」と
    #     答えた・確かめられなかった）は、目的のお客様に取り次いでもらえなかったのと同じに扱う（その方から目的のお客様へは届いていない）
    if conversation and not inp.reached_target:
        if inp.target_unconfirmed:
            return done("not_reached", not_reached="not_transferred", policy_key="not_reached_not_transferred")
        if r.not_reached is None:
            return unknown("not_reached_reason_missing", "目的のお客様に繋がらなかった理由がありません")
        return done("not_reached", not_reached=r.not_reached, policy_key="not_reached_" + r.not_reached)
    # (9) 会話に至らなかった
    if not conversation:
        return done("open", policy_key="open")
    # (10) 必須の質問が1つでも取れていない
    return done("incomplete", missing=missing, policy_key="incomplete")
