"""お詫びの電話（2026-10-16 ユーザー決定：A。DESIGN.md 10-1-2）。持ち主 CL-2。

**契約者は確認と承認だけ**（2026-10-16 ユーザー：「人があくまで確認と承認するだけ」）。
(1) 言ってはいけないことを言った通話を締めるとき、システムがお詫びの電話の案を作る（`prepare`）：誤って案内したこと＝AI が
    実際に言ったこと（通話後の検証の記録 forbidden_findings）／正しい内容＝業務設定の、その言ってはいけないことの正しい案内。
    文面は決まった形（`TEMPLATE_*`。正本はここだけ）にはめる。案は契約者への知らせに入れる。
(2) 契約者が案を見て承認する（`approve`。管理者か契約者だけ。データベースが守る）。承認で、AI がそのお客様（その通話でかけた番号）へかける
    予定が書かれる。承認しなければ、かけない（「場合によっては」の判断は、承認するかどうか）。
- 業務設定に正しい案内が無い言ってはいけないこと（失礼な言葉など）を言ったときは、別の文面（`TEMPLATE_IMPROPER_*`）を使う
  （2026-10-16 ユーザー：「その場合は、別のトークスクリプトを作成しておきましょう」）。**何を言ってしまったかを、文面に入れる**
  （2026-10-16 ユーザー：「何が不適切なのかが相手に伝わらない、お詫びは、お詫びになりません」）。正しい案内のあるものと両方なら、
  両方を伝える。
- お詫びの電話は、業務の結果を変えない。届かなければ、続けてかけず、別の時間帯に決まった回数（retry.apology_max＝3回。2026-10-30 管理者：
  「案A（3回）　出ない場合、連続してかけないようにすること」）までかけ直す（番号の聞き直しと同じ1か所＝next_action._new_band_retry）。
  お客様が「もうかけないで」と言っていれば、門が止める。
- 会話の担当（CL-1）は、目的が apology の予約証を受けたら `for_task` で文面と録音を読み、話し終えたら、伝えられたかを報告に入れる
  （call_report の apology_delivered）。
- **文面は録音で流し、契約者は録音を聞いて承認する**（CL-1 の連絡帳 4-40 の 4。ユーザー「OK」。表の作り 0131）：案を作るとき、伝える
  文を2つ（誤った通話と同じ日＝「先ほど」・あとの日＝「先日」）決めて案に残す。録音は、案ごとの録音の組（単位 apology。部分は
  `PARTS` の2つ。声は誤った通話の声。文が案の文と違えば組に入れられない＝データベースが守る）。承認（`approve`）は、その組の承認と
  同じ取引でし、承認に組を結ぶ（承認の無い録音・録音の無い承認は無い）。流すのは、承認が結んだ組の、その日の文の部分だけ（`for_task`）。
  録音を作るのは CL-1 の録音の道具（`recordings.create_set(unit="apology", order_id=…)`）。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from salescall.core import business_rules, next_action, recordings
from salescall.core.access import Tx

# 2026-10-16 ユーザー決定の文面（「○○」に、誤って案内したことと正しい内容が入る）。誤りが2つ以上なら「また、…」で続ける。
# 誤った通話と、お詫びの電話の日が違えば（お客様が出ず、翌日以降にかけ直したとき）「先ほど」を「先日」にする（2026-10-16 ユーザー承認）
TEMPLATE_FIRST = "{when}のお電話で、誤って「{wrong}」というご案内をしてしまいましたが、正しくは「{correct}」となります。"
WHEN_SAME_DAY, WHEN_LATER = "先ほど", "先日"
TEMPLATE_MORE = "また、誤って「{wrong}」というご案内をしてしまいましたが、正しくは「{correct}」となります。"
TEMPLATE_END = "不正確なご案内をしてしまいまして、誠に申し訳ございませんでした。"
# 正しい案内の無いことを言ったとき（失礼な言葉など）の文面（2026-10-16 ユーザーの文面。{said}＝言ってしまったこと「…」の並び）
# 2026-10-22 ユーザー決定の文（「先ほどのお電話で、『（AIが実際に言った言葉）』といった不適切な発言をしてしまい、大変失礼いたしました。
# 心よりお詫び申し上げます。誠に申し訳ございません！」。言った言葉は「」でくくって {said} に入る）
TEMPLATE_IMPROPER_ONLY = "{when}のお電話で、{said}といった不適切な発言をしてしまい、大変失礼いたしました。心よりお詫び申し上げます。誠に申し訳ございません！"
TEMPLATE_IMPROPER_ALSO = "また、{said}といった不適切な発言もしてしまいました。"
TEMPLATE_END_BOTH = "不正確なご案内と不適切な発言をしてしまいまして、誠に申し訳ございませんでした。"
# お詫びの録音の部分の名前（誤った通話と同じ日に伝えるか → 部分。データベースの守り 0131 と同じ名前）
PARTS = {True: "apology_same_day", False: "apology_later"}


def text(pairs: list[dict[str, str]], *, improper: list[str] = (), same_day: bool = True) -> str:
    """伝える文面（1か所）。pairs＝[{"wrong": 誤って案内したこと, "correct": 正しい内容}, …]。improper＝正しい案内の無いこと
    （失礼な言葉など）として実際に言ってしまったことの並び。same_day＝誤った通話と同じ日（日本時間）に伝えるか（違えば「先日」）。"""
    if not pairs and not improper:
        raise ValueError("お詫びすることが1つもありません")
    when = WHEN_SAME_DAY if same_day else WHEN_LATER
    said = "".join(f"「{x.strip()}」" for x in improper)
    if not pairs:
        return TEMPLATE_IMPROPER_ONLY.format(when=when, said=said)
    parts = [(TEMPLATE_FIRST if n == 0 else TEMPLATE_MORE).format(when=when, wrong=p["wrong"].strip(),
                                                                  correct=p["correct"].strip())
             for n, p in enumerate(pairs)]
    return "".join(parts) + (TEMPLATE_IMPROPER_ALSO.format(said=said) + TEMPLATE_END_BOTH if improper else TEMPLATE_END)


def prepare(tx: Tx, attempt_id: UUID) -> dict[str, Any]:
    """言ってはいけないことを言った通話の、お詫びの電話の案を作る（締めの処理が呼ぶ。同じ通話で2回目からは前の案を返す）。
    答え：order_id と text（案）。"""
    done = tx.one("SELECT order_id, text_same_day FROM salescall.apology_orders WHERE source_attempt_id = %s", (attempt_id,))
    if done is not None:
        return {"order_id": done["order_id"], "text": done["text_same_day"]}
    a = tx.one("SELECT task_id, version_id FROM salescall.attempts WHERE attempt_id = %s", (attempt_id,))
    known = business_rules.load(tx, a["version_id"]).forbidden
    pairs, improper = [], []
    for f in tx.all("SELECT item, said FROM salescall.forbidden_findings WHERE attempt_id = %s ORDER BY seq", (attempt_id,)):
        correct = known[f["item"]].correct
        if correct is None:                          # 正しい案内の無いこと（失礼な言葉など）：別の文面に、言ったことを入れる
            if f["said"] not in improper:
                improper.append(f["said"])
        elif {"wrong": f["said"], "correct": correct} not in pairs:
            pairs.append({"wrong": f["said"], "correct": correct})
    texts = {day: text(pairs, improper=improper, same_day=day) for day in PARTS}   # 伝える文は、ここで1回だけ決めて案に残す
    order_id = tx.one(
        "INSERT INTO salescall.apology_orders (tenant_id, client_id, task_id, source_attempt_id, pairs, improper,"
        "                                      text_same_day, text_later)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s, %s, %s) RETURNING order_id",
        (a["task_id"], attempt_id, json.dumps(pairs, ensure_ascii=False), json.dumps(improper, ensure_ascii=False),
         texts[True], texts[False]))["order_id"]
    return {"order_id": order_id, "text": texts[True]}


def approve(tx: Tx, order_id: int, set_id: UUID) -> None:
    """お詫びの電話の案を、その案の録音（set_id＝お詫びの単位の録音の組）を聞いて承認する（管理者か契約者だけ）。録音の組の承認と
    同じ取引でする（まだ承認していなければ、ここで承認する＝承認の仕組みは録音の1つ）。その通話のお客様へ、いまかける予定を書く。"""
    o = tx.one("SELECT o.task_id, a.contact_id FROM salescall.apology_orders o JOIN salescall.attempts a"
               " ON a.attempt_id = o.source_attempt_id WHERE o.order_id = %s", (order_id,))
    if o is None:
        raise LookupError(f"そのお詫びの案は無い（または、取引の範囲の外）：{order_id}")
    if tx.one("SELECT 1 AS x FROM salescall.recording_approvals WHERE set_id = %s", (set_id,)) is None:
        recordings.approve(tx, set_id)
    tx.execute("INSERT INTO salescall.apology_approvals (tenant_id, client_id, order_id, approved_by, set_id)"
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, salescall.cur_actor(), %s)", (order_id, set_id))
    schedule_if_ready(tx, o["task_id"])


def schedule_if_ready(tx: Tx, task_id: UUID) -> bool:
    """承認されたお詫びの案があれば、お詫びの電話の予定を書く（1回だけ。書いたら True）。予定を書くのはここの1か所：承認したとき
    と、誤った通話の締め・判定し直しのあと（core/settle.py）。書かないのは次のとき（⛔ 2026-10-22 の見直し：前は承認したときに
    すぐ書いていたので、判定待ち＝断ったかが決まっていない行の予定を「いまお詫び」に替え、断ったかもしれないお客様へかけ、
    あとで判定し直す道も消していた）：
    - 誤った通話の判定がまだ無い（締めが済んでいない）・その行が判定待ち（断ったかを決められない）
    - 誤った通話でお客様が発信を断った（もうかけない）"""
    row = tx.one("SELECT o.order_id, o.source_attempt_id, a.contact_id, p.approved_at"
                 " FROM salescall.apology_orders o JOIN salescall.apology_approvals p ON p.order_id = o.order_id"
                 " JOIN salescall.attempts a ON a.attempt_id = o.source_attempt_id"
                 " WHERE o.task_id = %s ORDER BY o.order_id DESC LIMIT 1", (task_id,))
    if row is None:
        return False
    if tx.one("SELECT 1 AS x FROM salescall.task_next_actions WHERE task_id = %s AND purpose = 'apology' AND decided_at >= %s",
              (task_id, row["approved_at"])) is not None:
        return False   # もう書いた（そのあとの動きは、お詫びの電話の締めが決める）
    judged = tx.one("SELECT business_state::text AS state FROM salescall.call_judgements WHERE attempt_id = %s",
                    (row["source_attempt_id"],))
    current = next_action.current(tx, task_id)
    if judged is None or judged["state"] in ("unknown", "do_not_call") or (current is not None and current["kind"] == "hold"):
        return False
    next_action.schedule_apology(tx, task_id, row["contact_id"], cause=f"お詫びの電話の承認（案 {row['order_id']}）")
    return True


def for_task(tx: Tx, task_id: UUID) -> dict[str, Any] | None:
    """その行の、承認されたいちばん新しいお詫びの案と、いま伝える文面・その録音（無ければ None。誤った通話と今日の日付で「先ほど」か
    「先日」かが決まる＝`PARTS`）。録音は、承認が結んだ組の、その日の文の部分（part・audio_ref・audio_sha256・voice_name）。
    どの案にも文が2つ・どの承認にも2つの部分のそろった録音の組がある（データベースの守り 0131・0137）ので、文も録音も空にならない。"""
    row = tx.one("SELECT o.order_id, o.source_attempt_id, o.pairs, o.improper, o.text_same_day, o.text_later, p.set_id,"
                 "       salescall.jst(a.claimed_at)::date = salescall.jst(now())::date AS same_day"
                 " FROM salescall.apology_orders o JOIN salescall.apology_approvals p ON p.order_id = o.order_id"
                 " JOIN salescall.attempts a ON a.attempt_id = o.source_attempt_id"
                 " WHERE o.task_id = %s ORDER BY o.order_id DESC LIMIT 1", (task_id,))
    if row is None:
        return None
    said = row["text_same_day"] if row["same_day"] else row["text_later"]
    rec = tx.one(
        "SELECT r.part, r.audio_ref, r.audio_sha256, s.voice_name FROM salescall.recordings r"
        " JOIN salescall.recording_sets s ON s.set_id = r.set_id WHERE r.set_id = %s AND r.part = %s",
        (row["set_id"], PARTS[row["same_day"]]))
    return dict(row, text=said, recording=dict(rec))
