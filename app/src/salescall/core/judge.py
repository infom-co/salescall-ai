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
  (2) 発信の拒否（通話中の申告・切れ方の判定が「断った」＝営業拒否の言葉のあとに切れた・
      切れ方の分からない通話のあとの確かめのかけ直しが鳴ったのに、お客様が出て話さなかった＝2026-10-30 管理者の決定）  ⇒ do_not_call
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
      話したかは call_report の1か所（`talked`＝人の証の表）、繋がったかは `JudgeInput.reached_target` の1か所：目的のお客様のいる
      業務は、繋いだ事実（connect）だけで繋がったと読む。「誰でも」の業務は、話した＝繋がった（業務設定から。見直し18b）。取り次いでもらえなかった
      （事情があり取り次げなかった）⇒ システムが日を改めて1回（business_rules.SYSTEM_POLICIES。2026-10-22 ユーザー）
  (9) 会話に至らなかった（お客様が出なかった・お客様の声でなかった）                              ⇒ open
  (10) 必須の質問が1つでも取れていない                                               ⇒ incomplete
  (11) どれでもない（入力の誤り）                                                      ⇒ unknown
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from salescall.core import call_report, calling_hours, spoken_time
from salescall.core.business_rules import Rules
from salescall.core.call_report import CallReport

# 判定の決まりの版（変えたら上げる）。かけ直しの時刻の読み方の版も入れる（読み方を直すと、判定待ちの行が判定し直される＝settle.rejudge）
JUDGE_VERSION = f"judge-2026-10-30.1+time-{spoken_time.VERSION}"   # 2026-10-30.1（管理者の指摘・決定）：切れ方の「断った」は営業拒否の言葉のときだけ（AI が話していた・だれも話していなかっただけでは断ったにしない）・声が片方に届いていなかった証拠があれば「声が届かず切れた」（audio＝途中で切れた電話と同じ）・決められない切れ方は確かめのかけ直しで決める（鳴ったのに出なければ営業拒否）。2026-10-24.1（見直し18b）：「お客様と話したか」を人の証の表で読む（人の言葉の締め・お客様からの質問・受け答えなど）・「誰でも」の業務は話した＝繋がった（業務設定から判定の入力に）・判定できない結果に、断られたかは分かっているか（refusal_known）を残す。2026-10-22.8：判定できなかった通話にも、会話の報告のかけ直しの申し出（約束）を残す（promise_offer。途中で切れたとき、すぐにかけず約束を守る＝見直し15）・約束の幅に、お客様が言った端とその日を言ったか（said・day_said）を残す・理由の無い「わからない」の確かめ（(6)）は、お客様と話した通話だけ（前の通話の答えで、出なかった電話まで判定できないにしない）。.7：目的のお客様に繋がらなかった結果に again（同じ日にまたかけてよいと言われたか）・在籍していないは業務の応じ方で（終える／後任の方を追う＝successor の印）。.6：CL-1 の 4-28（長期の不在は不在の仲間・ほかの手段と在籍していないは電話でかけ直さず終える）。.5：取り次げなかった通話に、あとでまたかけてよいと言われたか（again）を残す。.4：不在で戻る時刻が取れなかった申し出は「不在」（約束にしない）。.3：CL-1 の 10-22 の報告を受ける（目的のお客様のある通話は繋いだ事実だけで繋がった・繋がらなかった理由／かけ直しの申し出があれば話した・繋がって要らなくなったかけ直しは読まない。読み方は call_report の1か所）（.2：確かめ直しで目的のお客様だと確かめられなかった通話は「取り次いでもらえなかった」。.1：繋いだ事実で話した・繋がったと読む）
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
# 原因ごとに、断られたか（発信の拒否）は分かっているか（判定できない結果の中身の refusal_known。同じお客様のほかの行を止めるかは、
# データベースの salescall.refusal_undecided がこの印を読む＝決めるのはこの表だけ）。分からないのは、拒否を決める前に止まった原因
# （報告が無い・切れ方の材料が無い）だけ。ほかの原因は、判定の (2) までの拒否の確かめを通ったあと（拒否は無かった）に起きる。
# 途中で切れた通話は、原因によらず、通話のあとの確かめで断りの言葉が見つからなければ断られていない（データベースの側。0118）。
# ⛔ 見直し18b の (5)：前は、どの原因の判定できないも同じお客様のほかの行を止め、時刻・理由の抜けで、断られていないお客様の
# ほかの行まで、直して判定し直すまでかからなかった
REFUSAL_KNOWN: dict[str, bool] = {"report_missing": False, "cut_missing": False, "reason_missing": True,
                                  "not_reached_reason_missing": True, "callback_time_unreadable": True,
                                  "callback_time_unconfirmed": True}
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
    # ⛔ 2026-10-30 に外した入力：refused_after（通話後の検証で見つけた断り。書く入口がどこにも無く、使われない道だった。断りを
    # あとから見つける道は「録音から作り直した報告」の1本＝call_report.save_rebuilt）・unclear_is_refusal（設定
    # judge.unclear_disconnect_is_refusal。決め方が管理者の決定の1つ＝確かめのかけ直しになったので、設定ごと外した）
    # 通話の途中の「もうかけないで」が、その場で台帳に入っている（dnc.register_declared）。⚠️ 会話の処理が報告を書く前に
    # 落ちても、申告は台帳にある：報告だけを見ると、断りを見落として、行のほかの番号へ広げず、お詫びしてかけ直そうとした
    # （2026-10-19 の見直し）
    declared_in_call: bool = False
    # 目的のお客様に判定なしで繋いだ通話を確かめ直して、目的のお客様だと確かめられなかった（別の方・確かめられなかった。
    # core/target_recheck.py）。その通話の答えは、もう戻されている・「-」になっている。結果の中身に残す
    target_unconfirmed: bool = False
    # お客様の時間の設定（営業時間＝午前・午後・お客様指定の歯止め時間・1回分の長さ・言葉ごとの幅。読むのは gate.callee_settings の
    # 1か所。初めの値の写しを持たない＝必ず渡す。⛔ 見直し17b：前は渡し忘れても黙って 7〜22時・1時間になった。⛔ 見直し23 の (2)：
    # 言葉ごとの幅は別の引数 periods で、既定に初めの値の写し spoken_time.PERIODS を持っていた）
    callee: calling_hours.CalleeSettings = field(kw_only=True)
    # 業務に目的のお客様がいないか（「誰でも」。業務設定の版から＝business_settings.anyone_target の1か所。初めの値を持たない＝必ず
    # 渡す）。⛔ 見直し18b の (3)：前は判定の入力に無く、報告の印だけで「目的のお客様のある通話か」を推していた
    anyone_target: bool = field(kw_only=True)
    # この通話が、切れ方の分からない通話のあとの確かめのかけ直しで、お客様の電話まで鳴ったか（next_action.check_outcome の1か所が
    # "rang" と答えた。確かめのかけ直しでない・鳴らなかった・こちらの不具合で終えた＝False。初めの値を持たない＝必ず渡す）。
    # 2026-10-30 管理者の決定：鳴ったのに出なかった・出ても話さなかった ⇒ 両方の回線は元気＝営業拒否（`refusal`）
    check_rang: bool = field(kw_only=True)

    @property
    def reached_target(self) -> bool:
        """目的のお客様と話せたか（判定の (8)「目的のお客様に繋がらなかった」と、締めの約束の日のお詫びの印を外す所が読む1か所）：
        会話の報告で繋がっていて（call_report.CallReport.reached＝「誰でも」の業務はお客様と話した、目的のお客様のいる業務は繋いだ
        事実がある）、確かめ直しで目的のお客様だと確かめられなかった通話でない（2026-10-22 の見直し：前は判定が「繋がった」、締めが
        「繋がらなかった」と別々に読み、別の方だった約束の電話を「一部だけ取れた」にして、約束の幅を外していた）。"""
        return self.report is not None and self.report.reached(self.anyone_target) and not self.target_unconfirmed


@dataclass(frozen=True)
class Judgement:
    state: str                   # 業務の状態（4-1）
    complaint: bool
    disconnect: str | None       # 試行に記録する切れ方（normal / refused / interrupted。決められないものは設定で決めたほう）
    dnc_reason: str | None       # 発信禁止に登録する理由（refused_in_call / refused_unclear_checked / wrong_party / fax_line）
    detail: dict[str, Any]       # 判定の中身（依頼者への報告・次の動きが読む。policy_key＝再発信の方針の鍵）


def classify_cut(facts: Facts, report: CallReport | None) -> str | None:
    """切れ方の判定（7-1c。2026-10-14 ユーザー承認）。お客様と話した通話で、ふつうに終えたのでないときだけ決める（`cut_of` の1か所）。"""
    return cut_of(reached_answer=facts.reached_answer, end_state=facts.end_state, hangup_side=facts.hangup_side, report=report)


def cut_of(*, reached_answer: bool, end_state: str, hangup_side: str | None, report: CallReport | None) -> str | None:
    """切れ方を決める1か所（締め＝`classify_cut` と、状態の表の入口＝core/health が、声が届かず切れた電話を E4 にするのに読む）。
    返す値：normal / refused / interrupted / audio（声が届かず切れた）／unclear（材料はあるが決められない）／missing（材料が報告されて
    いない＝入力の誤り）。対象でなければ None。⚠️ missing を unclear と同じに扱わない（報告の書き損じで、ふつうに終えたお客様を営業拒否
    にしない）。
    refused は営業拒否の言葉があったときだけ（お客様の最後の発話が断りの言葉だった。通話の途中の「もうかけないで」の申告は
    `refusal` が先に見る）。2026-10-30 管理者の指摘：「声が届かないお客様が黙って切った通話を、営業拒否にしない」。⛔ 前は、切れた瞬間に
    AI が話していた・だれも話していなかった（speaker が ai / none）だけで refused にしていた（声が届かず「もしもし？」のあと切ったお客様も
    営業拒否＝発信禁止になった）。声が片方に届いていなかった証拠（cut.callee_cannot_hear・cut.callee_silent）があれば audio＝「声が
    聞き取れない」と同じ扱い（こちらの不具合に数えない・途中で切れた電話と同じく、お詫びとともにかけ直す・状態の表の E4＝声が通ると
    分かっている回線から）。拒否の言葉も声の証拠も無いもの（speaker が ai / none / 分からない）は unclear＝確かめのかけ直し
    （next_action.after_call。2026-10-30 管理者の決定：初めの値は「かけ直す」。決め方がその1つになったので、営業拒否にする設定
    judge.unclear_disconnect_is_refusal は外した）。"""
    if not reached_answer or end_state not in ("ended", "dropped") or report is None:
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
    if cut.callee_cannot_hear or cut.callee_silent:
        return "audio"                           # 声が片方に届いていなかった（お客様が「聞こえない」と言っていた・お客様の声が届かない）
    if cut.speaker == "callee":
        return "interrupted"                     # お客様の言葉が途中で途切れた
    if hangup_side == "line":
        return "interrupted"                     # 回線の信号で、回線の側で切れたと分かる
    return "unclear"                             # AI が話している途中・話し終えたあとの間に、お客様が何も言わずに切った など


def cut_off(*, reached_answer: bool, end_state: str, raw_cut: str | None, hangup_side: str | None,
            closing_started: bool) -> bool:
    """途中で切れた＝お客様が出たあと、お客様の意思と関係なく、話の途中で切れた（こちらの仕組みが止まった／回線・言葉の途中で
    切れた）。復旧次第、お詫びとともにすぐかけ直す（2026-10-17 ユーザー：「仕組みが途中で止まった通話や、回線が途中で切れた通話は
    復旧次第、お詫びとともにすぐかけ直さないといけないでしょ！」）。**決めるのはこの1か所**（締めの処理が試行に1回だけ残す＝
    attempts.record_cut_off。続けて何回切れたかも、それを数える）。入れないもの：回線の信号が「お客様が切った」もの（お客様が自分で
    切った）・切れ方を決められなかったもの（2026-10-18 の見直し）・締めに入っていた通話（closing_started＝会話の報告の
    `CallReport.closing_started`。こちらの仕組みが締めの言葉のあとで止まっても、話の途中ではない。⛔ 2026-10-22 の見直し14）。
    raw_cut＝依頼者の設定で写す前の切れ方（`classify_cut`。締めに入っていれば normal）。"""
    if not reached_answer:
        return False
    # 声が届かず切れた（audio）は、お客様が自分で切ったときも入れる（お客様の意思ではなく、声が届かなかった＝「声が聞き取れない」と同じ。
    # 2026-10-30 管理者の指摘）
    return ((end_state == "technical_error" and not closing_started) or raw_cut == "audio"
            or (raw_cut == "interrupted" and hangup_side != "callee"))


def judge(inp: JudgeInput) -> Judgement:
    return refusal(inp) or _business(inp)


def _cut(inp: JudgeInput) -> tuple[str | None, str | None]:
    """（依頼者の設定で写す前の切れ方, 試行に記録する切れ方）。声が届かず切れた（audio）は、記録では「途中で切れた」（interrupted＝
    お客様の意思と関係なく切れた）。決められない（unclear）は、まだ記録しない（確かめのかけ直しで決まる＝next_action.after_call。鳴っても
    出なければ、そのかけ直しの締めが営業拒否にする＝`refusal`。2026-10-30 管理者の決定）。"""
    raw_cut = classify_cut(inp.facts, inp.report)
    return raw_cut, {"unclear": None, "audio": "interrupted", "missing": None}.get(raw_cut, raw_cut)


def refusal(inp: JudgeInput) -> Judgement | None:
    """発信の拒否と番号違いの決め方（判定の (1)(2)。この1か所。お詫びの電話の締めも、これを使う＝2026-10-16 の見直し J6）。
    決まれば結果、拒否も番号違いも無ければ None（業務の中身の判定へ進む）。断ったかを決められない（会話の報告が無い・
    切れ方の材料が無い）ときは unknown。⚠️ はっきりした拒否（通話中の申告）は、切れ方の材料が
    無くても拒否（前は材料の抜けを先に見て unknown にし、発信禁止を登録しなかった＝2026-10-16 の見直し J9）。"""
    r = inp.report
    raw_cut, cut = _cut(inp)
    complaint = bool(r and r.complaint)
    detail: dict[str, Any] = _detail(inp, raw_cut, cut)

    def done(state: str, *, dnc: str | None = None, **more: Any) -> Judgement:
        return Judgement(state, complaint, cut, dnc, {**detail, **more})

    def unknown(cause: str, error: str) -> Judgement:
        return _unknown(inp, complaint, detail, cause, error)

    # お客様が出たのに会話の報告が無い：結果不明（処理が落ちた）なら、何も分からないまま扱う（途中で切れた通話として、復旧次第
    # お詫びとともにすぐかけ直す＝`cut_off`。2026-10-17 ユーザー）。
    # それ以外は入力の誤り（報告を書き損ねた）。決めつけない
    if inp.facts.reached_answer and r is None and inp.facts.end_state != "technical_error":
        if inp.declared_in_call:   # 報告は無くても、通話の途中の申告が台帳にある：はっきりした拒否が勝つ（2026-10-19 の見直し）
            return done("do_not_call", dnc="refused_in_call", refusal="in_call")
        return unknown("report_missing", "お客様が出た通話なのに、会話の報告がありません")
    # (0) FAX の回線：お客様に届かない番号。その番号だけ止める（番号違いと同じ扱い。行のお客様が断ったのではない）
    if r and "fax" in r.parties and not r.talked:
        if inp.facts.dialed_callee_number:   # 言われた番号が FAX だった：その番号だけ止め、番号を確かめて聞き直す（次の動き）
            return done("not_reached", dnc="fax_line", not_reached="wrong_alternate", fax_line=True)
        return done("out_of_scope", dnc="fax_line", out_of_scope="fax_line")
    # (1) 番号違い（その番号の持ち主の断りも、その番号だけを止める）
    if r and "wrong_party" in r.parties:
        refused = {"wrong_party_refused": True} if r.do_not_call or inp.declared_in_call or cut == "refused" else {}
        if inp.facts.dialed_callee_number:   # 言われた番号が違った：その番号だけ止め、番号を確かめて聞き直す（次の動き）
            return done("not_reached", dnc="wrong_party", not_reached="wrong_alternate", **refused)
        return done("out_of_scope", dnc="wrong_party", out_of_scope="wrong_party", **refused)
    # (2) 発信の拒否（原則14）：はっきりした拒否が先、切れ方はそのあと
    if (r and r.do_not_call) or inp.declared_in_call:   # 申告の台帳も見る（報告が無い・報告から抜けた＝結果不明の通話も）
        return done("do_not_call", dnc="refused_in_call", refusal="in_call")
    # 切れ方の分からない通話のあとの確かめのかけ直しが、お客様の電話まで鳴ったのに、お客様が出て話さなかった（出なかった・留守電）：
    # 両方の回線は元気なので、前の通話はお客様が切った＝営業拒否とみなす（2026-10-30 管理者の決定。発信禁止の登録は、ほかの営業拒否と
    # 同じこの1か所・広げ方も同じ＝dnc.CUSTOMER_WIDE。台帳には、はっきり断られた refused_in_call と別の理由 refused_unclear_checked で残す）。お客様が出て話を続けたら、その話の結果どおり（下へ進む）
    if inp.check_rang and not (inp.facts.reached_answer and r is not None and r.talked):
        return done("do_not_call", dnc="refused_unclear_checked", refusal="unclear_checked")
    if raw_cut == "missing":
        return unknown("cut_missing", "お客様と話した通話なのに、切れ方の材料（切れた瞬間に話していた側など）が報告されていません")
    if cut == "refused":
        return done("do_not_call", dnc="refused_in_call", refusal="cut")
    return None


def unreached_today(inp: JudgeInput) -> tuple[str | None, bool]:
    """目的のお客様に繋がらなかった理由と、同じ日にまたかけてよいと言われたか（同じ日にかけ直さないかの材料。1か所：業務の判定の
    (7) の不在・(8) と、お詫びの電話の締め＝業務の判定を書かない電話が、同じものを読む。⛔ 見直し16b：前はお詫びの電話の締めだけ
    会話の報告から直に読み、確かめ直しで目的のお客様だと確かめられなかった通話を「また掛けてよい」と読みえた。業務の判定を通すと、
    お詫びの電話は行の答えがもうそろっているので「完了」になり、材料が消えた）。
    - 不在で戻る時刻が取れなかった（かけ直しの申し出の理由が不在の仲間で、時刻が無い）⇒（不在, 会話の報告の may_call_again）
    - お客様と話したが目的のお客様に繋がらなかった ⇒ 確かめ直しで目的のお客様だと確かめられなかった通話は（取り次げなかった, いいえ）
      ＝聞いていない。それ以外は（会話の報告の理由, may_call_again）
    - どれでもない ⇒（None, いいえ）"""
    r = inp.report
    if r is None:
        return None, False
    cb = r.callback
    if (cb is not None and cb.reason in call_report.ABSENT_REASONS
            and _when(inp, cb).kind == spoken_time.UNSPECIFIED):
        return "absent", r.may_call_again
    if inp.facts.reached_answer and r.talked and not inp.reached_target:
        if inp.target_unconfirmed:
            return "not_transferred", False
        return r.not_reached, r.may_call_again
    return None, False


def _when(inp: JudgeInput, cb: Any) -> spoken_time.Reading:
    """かけ直しの申し出の時刻の言葉の読み（1か所。core/spoken_time.read）。"""
    # beyond_ok＝お客様指定の歯止め時間の外の時刻（「23時以降なら」）を、通話の中で確かめて「はい」と言われた（会話の報告の
    # callback.beyond_limit_ok。無い＝確かめていない＝今までどおり判定できない）。until＝「〇時まで」の言い方のときの、かけてよい
    # 時間の決め方（いま電話を続けられなかった理由＝Callback.until_reason から、お客様の時間の設定の1か所が決める）
    c = inp.callee
    return spoken_time.read(cb.time_said, inp.facts.called_at, business_hours=c.business_hours, periods=c.period_table(),
                            monthly=cb.cycle == "month", limit=c.designated_limit, beyond_ok=cb.beyond_limit_ok,
                            slot=c.exact_slot, until=c.until_rule(cb.until_reason))


def _callback(cb: Any, when: spoken_time.Reading) -> dict[str, Any]:
    """約束したかけ直しの中身（判定の中身の callback・promise_offer。1か所）。at＝幅の始まり（ちょうどの時刻ならその時刻。読めない・
    時刻を任された約束は None）・until＝幅の終わり（無ければ終わり無し）・daily＝1日の中の幅（秒）・exact＝お客様が時刻を指定した・
    since＝その回の本当の始まり・repeat＝話せなかったときの次の回（お客様がその日を選んだ理由。spoken_time.Reading.repeat）・
    said＝1日の中の幅の始まり・終わりをお客様が言ったか・day_said＝お客様がその日を言ったか（どちらも、依頼者の時間帯の外で
    かけてよいかを決める材料＝calling_hours.callee_hours）・until_why＝「〇時まで」の言い方のときの、いま電話を続けられなかった理由
    （Callback.until_reason。記録だけ＝幅はもう決まっている）・beyond_ok＝お客様指定の歯止め時間の外の時刻を通話の中で確かめた
    （時刻ちょうどの約束の回を、歯止めの外で1回分使ってよいか＝next_action._exact_window が読む）。"""
    iso = lambda t: None if t is None else t.isoformat()   # noqa: E731
    return {"reason": cb.reason, "at": iso(when.start), "until": iso(when.end), "since": iso(when.since), "repeat": when.repeat,
            "cycle": cb.cycle, "daily": None if when.daily is None else list(when.daily),
            "days": None if when.days is None else list(when.days), "exact": when.exact, "said": list(when.said),
            "day_said": when.day_said, "time_said": cb.time_said, "number_said": cb.number_said,
            "number_e164": cb.number_e164, "person": cb.person, "until_why": cb.until_reason,
            "beyond_ok": cb.beyond_limit_ok}


def _promise_offer(inp: JudgeInput) -> dict[str, Any] | None:
    """会話の報告にある、かけ直しの約束になる申し出（判定できなかった通話に残す＝promise_offer。1か所）。途中で切れて判定できなかった
    通話でも、約束の申し出があれば、すぐにはかけず、約束を守る（次の動き。2026-10-22 ユーザー決定7と同じ扱い。見直し15）。約束に
    ならない申し出（在籍していない・ほかの手段・不在で戻る時刻が取れなかった）は None。時刻の言葉が読めない・1つに決まらないときは
    時刻の決まっていない約束（at が None）。"""
    r = inp.report
    cb = None if r is None else r.callback
    if cb is None or cb.reason == "departed" or cb.reason in call_report.ENDING_CALLBACKS:
        return None
    when = _when(inp, cb)
    if when.kind in (spoken_time.UNREADABLE, spoken_time.CONFIRM):
        return _callback(cb, spoken_time.Reading(spoken_time.UNSPECIFIED))
    if cb.reason in call_report.ABSENT_REASONS and when.kind == spoken_time.UNSPECIFIED:
        return None
    return _callback(cb, when)


def _unknown(inp: JudgeInput, complaint: bool, detail: dict[str, Any], cause: str, error: str) -> Judgement:
    """判定できない（1か所）。会話の報告に約束の申し出があれば、中身に残す（`_promise_offer`）。断られたかは分かっているか
    （refusal_known＝`REFUSAL_KNOWN` の1か所）も残す（データベースの salescall.refusal_undecided が読む）。"""
    offer = _promise_offer(inp)
    return Judgement("unknown", complaint, None, None,
                     {**detail, "unknown_cause": cause, "error": error, "refusal_known": REFUSAL_KNOWN[cause],
                      **({"promise_offer": offer} if offer else {})})


def _unreached_key(r: CallReport) -> str:
    """目的のお客様に繋がらなかった通話の再発信の方針の鍵（1か所）。在籍していないで後任の方を追う通話は、不在と同じ方針
    （回数・間隔。同じ日はまたかけてよいと言われたときだけ＝次の動き）。在籍していないで終える通話・ほかの手段は、システムが
    終える方針。"""
    if r.not_reached == "other_means":
        return "other_means"
    if r.not_reached == "departed":
        return "not_reached_departed" if r.departed_end else "not_reached_absent"
    return "not_reached_" + r.not_reached


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
        return _unknown(inp, complaint, detail, cause, error)

    # この通話で目的のお客様が後任の方に替わった（お呼びした方が在籍しておらず、業務の応じ方が「後任の方をお呼びする」）：これからの
    # 電話は後任のご担当者様をお呼びする（次の動きが、この印で予定と予約証に印を付ける。call_report.turned_to_successor の1か所。
    # 2026-10-22 ユーザー：「基本は後任へつないでもらう」）
    if r and r.turned_to_successor:
        detail["successor"] = True
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
    #     見るのは、お客様と話した通話だけ（項目は行のこれまでの通話の答え。前の通話の「理由の無いわからない」は、その通話の判定で
    #     結果不明にした。⛔ 2026-10-22 の見直し15：前は、そのあとの出なかった・話せなかった電話まで、毎回「判定できない」にしていた）
    human = bool(r and r.talked)
    conversation = inp.facts.reached_answer and human
    for q in (missing if conversation else []):
        it = inp.items.get(q)
        if it is None or it.state not in ("dont_know", "refused"):
            continue
        h = rules.handling.get((q, it.state, it.reason)) if it.reason is not None else None
        if h is None:
            return unknown("reason_missing", f"質問 {q} の {it.state} に、決まった理由が無い（理由 {it.reason!r}）")
        if h.kind == "conclude":
            return done("concluded", conclusion=h.conclusion, concluded_by=q)
    # お呼びした方が在籍しておらず、行を終える（業務で「お礼を言って終える」を選んだ・後任の方も在籍していない＝
    # call_report.departed_end の1か所）：約束やかけ直しより先に終える（お呼びできる方がもういない）
    if r and r.departed_end and not inp.reached_target:
        return done("not_reached", not_reached="departed", policy_key="not_reached_departed")
    # (7) かけ直しの約束（「在籍していない」の申し出は約束にしない：後任の方を追うのは (8)＝上の印と _unreached_key）
    if r and r.callback is not None and r.callback.reason != "departed":
        cb = r.callback
        if cb.reason in call_report.ENDING_CALLBACKS:
            # ほかの手段を望まれた・在籍していない（電話ではかけ直さない。2026-10-22 ユーザー決定。CL-1 の連絡帳 4-28）：約束にしない。
            # ほかの手段は、目的のお客様に繋がっていても同じ（希望と宛先を記録して締めた）。方針はシステムが決める（業務では変えない）
            if cb.reason == "other_means":
                return done("not_reached" if not inp.reached_target else "incomplete",
                            **({} if inp.reached_target else {"not_reached": "other_means"}),
                            **({"missing": missing} if inp.reached_target else {}), policy_key="other_means")
        when = _when(inp, cb)
        if when.kind == spoken_time.UNREADABLE:
            return unknown("callback_time_unreadable", f"かけ直しの時刻「{cb.time_said}」を読めません（{when.why}）")
        if when.kind == spoken_time.CONFIRM:
            return unknown("callback_time_unconfirmed", f"かけ直しの時刻「{cb.time_said}」が1つに決まりません（{when.why}）")
        if cb.reason in call_report.ABSENT_REASONS and when.kind == spoken_time.UNSPECIFIED:
            # 不在で、戻る時刻が取れなかった：約束ではなく「不在」（再発信の方針 not_reached_absent＝かけ直しの回数の上限まで）。
            # 2026-10-13 ユーザー：「戻る時刻が聞けたときは『折り返し予定』…⇒はい」「『不在』のときは、かけ直しの回数の上限までかけ直す
            # 案です。⇒OK」。CL-1 は不在のとき、戻る時刻を聞けなくても、かけ直しの申し出（理由 absent）を残す。⛔ 2026-10-22 の見直し13：
            # 前は時刻の決まっていない約束（callback_unspecified_time）にして、回数の上限の外でかけ続けえた
            nr, again = unreached_today(inp)
            return done("not_reached", not_reached=nr, policy_key="not_reached_absent", again=again)
        return done("callback_scheduled", callback=_callback(cb, when),
                    policy_key=None if when.start else "callback_unspecified_time")
    # (8) お客様と話したが、目的のお客様に繋がらなかった。確かめ直しで目的のお客様だと確かめられなかった通話（別の方が「はい」と
    #     答えた・確かめられなかった）は、目的のお客様に取り次いでもらえなかったのと同じに扱う（その方から目的のお客様へは届いていない）
    if conversation and not inp.reached_target:
        # again＝あとでまたかけてよいと言われたか（取り次げなかったときだけ。読むのは call_report の1か所。確かめ直しで目的の
        #   お客様だと確かめられなかった通話は、聞いていないので False）。約束の電話で当日の幅の中でかけ直すかを、次の動きが決める
        nr, again = unreached_today(inp)
        if nr is None:
            return unknown("not_reached_reason_missing", "目的のお客様に繋がらなかった理由がありません")
        key = "not_reached_not_transferred" if inp.target_unconfirmed else _unreached_key(r)
        return done("not_reached", not_reached=nr, policy_key=key, again=again)
    # (9) 会話に至らなかった
    if not conversation:
        return done("open", policy_key="open")
    # (10) 必須の質問が1つでも取れていない
    return done("incomplete", missing=missing, policy_key="incomplete")
