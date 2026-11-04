"""録音の版と承認の記録（定型の音声の台帳。DESIGN.md 6-13）と、話してよい声。持ち主 CL-2。表の作り 0047。

- 録音の持ち主は2つ（DESIGN.md 3-8-3。✅ 2026-11-02 管理者：「システムで共通する名前や対応などは、契約者や依頼者ごとではなくて、
  システムで共通で録音を持っておくということになります。業務で持つのは明らかにその業務特有のトークスクリプトや問答だけになります。」）：
  **システム**（契約者の無い行。管理者が作って承認し、すべての契約者が読む）＝共通の一言（単位 contractor＝相づち・締め・つなぎ・
  システムが足す質問など、どの依頼者にも同じもの。0171）と名前の録音、**依頼者**＝その業務特有の録音（単位 business＝業務設定の版と
  声の名前ごと。トークスクリプト）とお詫びの電話の文。声の名前ごとに1組（声の型ごとではない）。⛔ 0171 まで：共通の一言を契約者ごとに
  録音していた（2026-10-15 ユーザーの「契約者単位でシステム的に録音」をもとにした形。契約者が増えるたびに同じ録音を作り直して聞き直す
  ことになる）。単位の名前 contractor は、CL-1 と合わせて替えるまで、このまま（CL-2 の連絡帳 4-194）。
- 組は変えられない（作り直しは新しい組）。部分（part）の一覧は会話の側（CL-1）の1か所が決める。ここは、渡された部分をそのまま残す。
- 承認の操作は組ごとに1回。管理者か契約者が聞いて承認する（事前の録音チェック。データベースが守る）。承認した組には部分を足せない。
- **話してよい声は `ready_voices` の1か所で決める**：使う声（使わない印の無い声）のうち、承認した録音が、システムの共通の一言と、その版の
  依頼者の単位の両方にあり、初めの声の型を持つもの（2026-10-17 ユーザー：「事前の録音チェックになる方向性」「新しい声が増える
  かもしれません」＝声を足しても処理は変えない）。門（core/gate.py の `choose_voice`）だけが使う。共通の一言の組には、その部分
  （`CONTRACTOR_PARTS`。名前の正本は CL-1 の phrases.RECORDED）がすべて要る（CL-1 の連絡帳 4-39）。
- **名前の録音の台帳**（0132・0134・0137）：お名前の入る一言は、役職の部分（「営業部部長の」）・名前の部分（「「おかだ」様」）・続く言葉
  （「はいらっしゃいますでしょうか？」）を組み合わせて流す（切れ目は 2026-10-25 夕のご決定＝CL-1 の連絡帳 4-55 の (2)）。どれも組の単位
  （name_role・name・name_tail）で、承認の仕組みは同じ。持ち主は2つ（0173。✅ 2026-11-04 管理者：「契約者が作った段階では、業務ごとの
  録音設定と同じ扱いです。管理者が認めて初めてシステムの共通録音に登録されます。」）：**業務の録音**（契約者がその業務のために作って
  承認したもの。その業務の電話で使う）と、**システムの共通の録音**（管理者が作って承認したもの・管理者が業務の録音を聞いて認めて
  写したもの。すべての契約者が読む）。その行で要るものは `name_needs`、その声で足りないものは `name_missing` の1か所（門の
  `choose_voice` が読む）、流す録音は `name_recording`。管理者が聞いて決めるのは `name_reviews`・`review_names`（まとめて）。管理者の
  チェックを要らないとした契約者（`set_name_review_skip`）の録音は、決まった間隔の仕事が入れる（`admit_unreviewed_names`）。
- **承認は録音の中身に付く**（0172。設計書 3-8-3 の (2)。✅ 2026-11-02 管理者 伺い2「案1でOK」）：承認するという操作は組ごとに
  記録する（`approve`＝だれが・いつ、その組を聞いて承認したか）。部分が承認済みかは中身で決める＝同じ持ち主（契約者と依頼者。
  システムは両方とも無い）・同じ声で、文・録音で選んだ名前の読み・音声の指紋がまったく同じ部分が、承認した組のどれかにあれば
  承認済み（データベースの見方 salescall.part_states の1か所。中身の鍵は content_of の1か所で作り、部分と読みを書いたときに part_contents に残す。部分の名前と置き場は見ない）。組がそろったかも1か所（salescall.set_ready：
  部分が1つ以上・すべて承認済み・その版のいまの読みと合う）⇒ 全部が承認済みの部分の組は、承認を待たずにそろう。版が変わっても、
  聞くのは変わった部分だけ（`unapproved_parts`＝画面が聞かせる部分）。
- 流すときに使う組は `current_sets`（それぞれの単位で、そろった組のうち、いちばん新しく作った組＝見方 current_recording_sets。
  作った時刻はデータベースが決める）。
- **測り終わるまで消さない録音**（`hold`・`release`。表の作り 0056。2026-10-17 ユーザー 案A。Intent.md 11-4 第2版）：検証モードの通話
  （締めのときに付ける）と、誤判定率を測るために抜き取った通話（抜き取る処理が付ける）は、測り終わって外すまで、古い録音を消す処理
  （core/backup.py の `prune`）が消さない。外すのは測定を記録する処理（16-3）が、使い終えた通話について1回だけ。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from salescall.core import business_settings, readings as readings_mod
from salescall.core.access import Tx


@dataclass(frozen=True)
class Part:
    """録音の1つの部分（流す単位）。"""
    part: str            # 流す単位の名前（会話の側の名前。例 greeting・closing・Q2）
    spoken_text: str     # 録音の文
    audio_ref: str       # 音声のファイルの置き場の参照
    audio_sha256: str    # 中身の指紋
    # この録音が話す名前の読み（名前の表記 → ひらがな）。入口の読みの無い名前を話す部分は、必ず書く（依頼者の単位の組だけ）。
    # その版のいまの読み（版の読み・録音を聞いて選んだ読み。Intent.md 6-2）と同じでないと、組を作れない・承認できない・流さない
    readings: dict[str, str] = field(default_factory=dict)


HOLD_REASONS = ("verification", "sample")   # 検証モードの通話・誤判定率を測るために抜き取った通話


def hold(tx: Tx, attempt_id: UUID, reason: str) -> None:
    """その通話の録音を、測り終わるまで消さない印を付ける（同じ理由は1つ）。"""
    if reason not in HOLD_REASONS:
        raise ValueError(f"録音を残す理由は {HOLD_REASONS} のどれかです（{reason!r}）")
    tx.execute("INSERT INTO salescall.recording_holds (tenant_id, client_id, attempt_id, reason)"
               " SELECT tenant_id, client_id, attempt_id, %s FROM salescall.attempts WHERE attempt_id = %s"
               " ON CONFLICT (attempt_id, reason) DO NOTHING", (reason, attempt_id))


def release(tx: Tx, attempt_ids: list[UUID], reason: str, *, measurement: str) -> int:
    """測り終わった通話の印を外す（どの測定で使い終えたかを書く。1回だけ）。外した数を返す。ほかの理由の印が残っていれば消さない。"""
    if not measurement.strip():
        raise ValueError("使い終えた測定の名前が要ります")
    return len(tx.all("UPDATE salescall.recording_holds SET released_at = now(), released_for = %s"
                      " WHERE attempt_id = ANY(%s) AND reason = %s AND released_at IS NULL RETURNING attempt_id",
                      (measurement, list(attempt_ids), reason)))


def add_voice(tx: Tx, voice_name: str, *, note: str, display_gender: str | None) -> None:
    """声の一覧に声を足す（会話AIのモデルが進化して声が増えたとき。管理者だけ＝データベースが守る）。話し方（声の型）は CL-1 が足す。
    display_gender：画面に出す性別の表示名（「男性」「女性」。分からなければ None。あとから変えない。Intent.md 9-8）。門は、ほかの
    依頼者がこの番号へかけた声と替えるとき、できれば別の性別の声にする（core/gate.choose_voice）。"""
    tx.execute("INSERT INTO salescall.voices (voice_name, note, display_gender) VALUES (%s, %s, %s)",
               (voice_name, note, display_gender))


def add_voice_type(tx: Tx, code: str, voice_name: str, *, default_for_voice: bool, note: str) -> None:
    """声の型（声の名前＋話し方）を足す（管理者だけ＝データベースが守る。2026-10-18 の見直し：前は移行のファイルでしか足せず、
    足した声がいつまでも使えなかった）。話し方の中身は CL-1 の持ち物（連絡帳で知らせてもらう）。新しい声には、初めの声の型
    （default_for_voice）を1つ足す（声を替えるとき、替えた先に使う。話してよい声は、初めの声の型のある声だけ＝`ready_voices`）。"""
    tx.execute("INSERT INTO salescall.voice_types (code, voice_name, default_for_voice, note) VALUES (%s, %s, %s, %s)",
               (code, voice_name, default_for_voice, note))


def retire_voice(tx: Tx, voice_name: str, *, reason: str) -> None:
    """声に「使わない」の印を付ける（1回だけ。管理者だけ）。門は、その声で話さない。"""
    row = tx.one("UPDATE salescall.voices SET retired_at = now(), retired_reason = %s"
                 " WHERE voice_name = %s AND retired_at IS NULL RETURNING voice_name", (reason, voice_name))
    if row is None:
        raise LookupError(f"使っている声ではありません：{voice_name}")


def create_set(tx: Tx, *, unit: Literal["contractor", "business", "apology", "name_role", "name", "name_tail", "standard"],
               voice_name: str, parts: list[Part], version_id: UUID | None = None, order_id: int | None = None,
               name_role: str | None = None, name_key: NameKey | None = None, tail: str | None = None,
               business_id: UUID | None = None, standard_id: UUID | None = None) -> UUID:
    """録音の組を作る（承認の前）。共通の一言（contractor）はシステムの持ち物（管理者がシステムの取引で作る＝データベースが守る。0171）、
    依頼者の単位は取引の依頼者の、その版。お詫びの単位（apology）は
    取引の依頼者の、そのお詫びの案（order_id。部分は apology.PARTS の2つで、文は案の文と同じ・声は誤った通話の声＝データベースが
    守る。0131）。名前の録音の台帳（name_role＝役職の部分・name＝名前の部分・name_tail＝続く言葉）は、鍵（name_role・name_key・tail）を
    渡す（`add_name_role`・`add_name_part`・`add_name_tail` が呼ぶ）。持ち主は、契約者が依頼者のタブの取引で作れば business_id の業務
    （業務の録音と同じ扱い）、管理者がシステムの取引で作ればシステムの共通の録音（business_id は渡さない。データベースが守る＝0173）。
    標準の録音（standard）は、その標準の版（standard_id）の想定問答の答えの録音。システムの持ち物（管理者がシステムの取引で作る。0174）。
    部分は、その標準の版の想定問答の答え（名前は ANSWER_PART_PREFIX＋問いの名前・文は答えの文）だけ（`_standard_parts` の1か所）。"""
    if not parts:
        raise ValueError("録音の部分がありません")
    if len({p.part for p in parts}) != len(parts):
        raise ValueError("同じ名前の部分が2つあります")
    if (unit == "business") != (version_id is not None) or (unit == "apology") != (order_id is not None) \
            or (unit == "name_role") != (name_role is not None) or (unit == "name") != (name_key is not None) \
            or (unit == "name_tail") != (tail is not None) or (unit == "standard") != (standard_id is not None) \
            or (business_id is not None and unit not in NAME_UNITS):
        raise ValueError("業務の録音の組には業務設定の版を、お詫びの単位の組にはお詫びの案を、名前の録音の台帳には鍵（業務の録音なら業務も）を、"
                         "標準の録音の組には標準の版を渡します（共通の一言の組には、どれも渡しません）")
    if unit == "standard":
        _standard_parts(tx, standard_id, parts)
    chosen = _chosen_readings(tx, parts, version_id)
    role = name_role if unit == "name_role" else name_key.role if name_key is not None else None
    set_id = tx.one(
        "INSERT INTO salescall.recording_sets (tenant_id, unit, voice_name, client_id, version_id, order_id,"
        " name_role, name_reading, tail, business_id, standard_id)"
        " VALUES (salescall.cur_tenant(), %s, %s, CASE WHEN %s IN ('business', 'apology') OR %s::uuid IS NOT NULL"
        " THEN salescall.cur_client() END, %s, %s, %s, %s, %s, %s, %s) RETURNING set_id",
        (unit, voice_name, unit, business_id, version_id, order_id, role, name_key.reading if name_key else None, tail,
         business_id, standard_id))["set_id"]
    for p in parts:
        tx.execute("INSERT INTO salescall.recordings (tenant_id, client_id, set_id, part, spoken_text, audio_ref, audio_sha256)"
                   " SELECT tenant_id, client_id, set_id, %s, %s, %s, %s FROM salescall.recording_sets WHERE set_id = %s",
                   (p.part, p.spoken_text, p.audio_ref, p.audio_sha256, set_id))
    for part, name, reading in chosen:
        tx.execute("INSERT INTO salescall.recording_readings (tenant_id, client_id, set_id, part, name_text, reading)"
                   " SELECT tenant_id, client_id, set_id, %s, %s, %s FROM salescall.recording_sets WHERE set_id = %s",
                   (part, name, reading, set_id))
    return set_id


def _standard_parts(tx: Tx, standard_id: UUID, parts: list[Part]) -> None:
    """標準の録音の組の部分の確かめ（1か所）：その標準の版の想定問答の答えの部分と、名前も文も同じであること（足りない・余る・文が
    違うは作らない。標準の録音の組は、その標準の版の答えを、その声で全部録ったもの）。"""
    want = {business_settings.ANSWER_PART_PREFIX + a: t for a, t in business_settings.standard(tx, standard_id).answers}
    got = {p.part: p.spoken_text for p in parts}
    if not want:
        raise ValueError("その標準の版には、想定問答がありません（標準の録音は要りません）")
    if set(got) != set(want):
        raise ValueError(f"標準の録音の部分は、その標準の版の想定問答の答え {sorted(want)} です（足りない：{sorted(set(want) - set(got))}・"
                         f"余る：{sorted(set(got) - set(want))}）")
    wrong = sorted(k for k in want if got[k] != want[k])
    if wrong:
        raise ValueError(f"標準の録音の文が、その標準の版の答えと違います：{wrong}")


def choose_reading(tx: Tx, version_id: UUID, name_text: str, reading: str) -> None:
    """その版の名前の読みを、契約者が録音を聞いて選ぶ（1か所。Intent.md 6-2）。選び直しは記録を足すだけ（いまの読みは見方
    current_name_readings）。選び直したら、合わない声の録音は使われず（recording_set_fits）、その読みで録り直した声から話す。
    入口の読み（担当者名など）のある名前は選べない（入口の読みがいちばん強い）。選べるのは管理者か契約者だけ（データベースが守る）。"""
    names = {n.text: n for n in business_settings.stored_names(tx, version_id)}
    if name_text not in names:
        raise ValueError(f"名前「{name_text}」は、この版の業務設定の名前にありません")
    if names[name_text].reading_by == "given":
        raise ValueError(f"名前「{name_text}」の読みは入っています（「{names[name_text].reading}」。録音では選びません）")
    r = readings_mod.required(reading, f"名前「{name_text}」の読み「{reading}」")
    tx.execute("INSERT INTO salescall.version_name_readings (tenant_id, client_id, version_id, name_text, reading)"   # 番号は
               " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s)", (version_id, name_text, r))   # next_seq（0109）


def carry_readings(tx: Tx, from_version: UUID, to_version: UUID) -> None:
    """ある版で契約者が録音を聞いて選んだ名前の読みを、その版の中身から作った新しい版へ引き継ぐ（以前の版へ戻す・業務を複製する＝
    business_settings.save_from だけが呼ぶ。中身が同じなので、名前も入口の読みも同じ）。引き継ぐのも選ぶのと同じく、戻す・複製する
    管理者か契約者の名で記録する（データベースが守る。選んだ読みが無ければ何も書かない）。"""
    tx.execute(
        "INSERT INTO salescall.version_name_readings (tenant_id, client_id, version_id, name_text, seq, reading)"
        " SELECT v.tenant_id, v.client_id, v.version_id, c.name_text, 1, c.reading"
        " FROM salescall.current_name_readings c, salescall.business_versions v"
        " WHERE c.version_id = %s AND c.reading_by = 'recording' AND v.version_id = %s", (from_version, to_version))


def _chosen_readings(tx: Tx, parts: list[Part], version_id: UUID | None) -> list[tuple[str, str, str]]:
    """録音の名前の読みの確かめ（1か所）：(部分, 名前, 読み) の並び。依頼者の単位の組だけ・その版の業務設定にある名前だけ・その版の
    いまの読み（見方 current_name_readings の1か所：入口の読み・契約者が確かめた読み・AI の読み・録音を聞いて選んだ読み）と同じものだけ
    （データベースも守る＝0101）。読みが決まらない名前は、先に録音を聞いて選ぶ（choose_reading）。
    ⚠️ 入口の読みの無い名前（読みを選び直せる名前）を話す部分は、その名前の読みを必ず書く（書かない組は、読みを選び直したあとも
    「合う」として流れてしまう＝見直し6・7）。話しているかは、録音の文の「」の中（ひらがなにそろえて）が、その名前の読み・考えられる
    読み・まず言ってみる読みのどれかで、その部分に書いた読みで説明できないとき、または名前の表記がそのまま文にあるとき。録音の文は、
    読みをひらがなで「」にくくった形（0047）。"""
    out = [(p.part, name, readings_mod.required(r, f"録音「{p.part}」の名前「{name}」の読み「{r}」"))
           for p in parts for name, r in p.readings.items()]
    if version_id is None:
        if out:
            raise ValueError("録音の名前の読みは、依頼者の単位（業務設定の版）の録音だけに付けます")
        return out
    stored = {n.text: n for n in business_settings.stored_names(tx, version_id)}
    now = {r["name_text"]: r for r in tx.all(
        "SELECT name_text, reading, reading_by FROM salescall.current_name_readings WHERE version_id = %s", (version_id,))}
    for part, name, reading in out:
        if name not in stored:
            raise ValueError(f"録音「{part}」の名前「{name}」は、この版の業務設定の名前にありません")
        want = now[name]["reading"] if name in now else None
        if want != reading:
            raise ValueError(f"録音「{part}」の名前「{name}」の読み「{reading}」は、この版のいまの読み（"
                             + (f"「{want}」" if want else "まだ決まっていない。先に録音を聞いて選ぶ＝choose_reading") + "）と違います")
    for p in parts:
        quoted = {readings_mod.normalize(q) for q in re.findall(r"「([^「」]+)」", p.spoken_text)} - {None} - set(p.readings.values())
        for n in stored.values():
            if n.text in p.readings or (n.text in now and now[n.text]["reading_by"] == "given"):
                continue
            heard = {x for x in (*n.candidates, n.likely, n.reading, now[n.text]["reading"] if n.text in now else None) if x}
            if quoted & heard or n.text in p.spoken_text:
                raise ValueError(f"録音「{p.part}」は名前「{n.text}」を話しています。その読みを書いてください（Part.readings）")
    return out


def approve(tx: Tx, set_id: UUID) -> None:
    """録音の組を承認する（管理者か契約者が聞いて。データベースが守る）。承認されるのは、その組の部分の中身（同じ持ち主・同じ声の、
    同じ中身の部分は、どの組の中でも承認済みになる＝0172）。聞いていただくのは `unapproved_parts` の部分だけでよい。"""
    row = tx.one("INSERT INTO salescall.recording_approvals (tenant_id, client_id, set_id, approved_by)"
                 " SELECT tenant_id, client_id, set_id, salescall.cur_actor() FROM salescall.recording_sets WHERE set_id = %s"
                 " RETURNING set_id", (set_id,))
    if row is None:
        raise LookupError(f"録音の組がありません（または、取引の範囲の外）：{set_id}")


def unapproved_parts(tx: Tx, set_id: UUID) -> list[str]:
    """その組で、まだ承認されていない部分の名前（名前の順）。画面が承認の前に聞かせるのは、これだけ（ほかの部分は、同じ中身をもう
    聞いて承認している＝データベースの見方 salescall.part_states の1か所。0172）。空なら、その組は承認を待たずにそろう（読みが合えば）。"""
    return [r["part"] for r in tx.all(
        "SELECT part FROM salescall.part_states WHERE set_id = %s AND NOT approved ORDER BY part",
        (set_id,))]


_CURRENT = ("EXISTS (SELECT 1 FROM salescall.current_recording_sets s"
            " WHERE s.voice_name = v.voice_name AND {where})")
# 共通の一言のいまの組（システムの行）と、その版の業務の録音のいまの組（取引の契約者の行）。話してよい声・足りない部分・流す組が読む1か所
_COMMON = "s.unit = 'contractor' AND s.tenant_id IS NULL"
_BUSINESS = "s.unit = 'business' AND s.tenant_id = salescall.cur_tenant() AND s.version_id = %s"
# その版が指す標準の版の、標準の録音のいまの組（システムの行。0174）。その版が標準から流す部分は、業務設定の1か所
# （`business_settings.standard_parts_of`）が決める。無い版は、標準の録音を見ない
_STANDARD = ("s.unit = 'standard' AND s.tenant_id IS NULL"
             " AND s.standard_id = (SELECT b.standard_id FROM salescall.business_versions b WHERE b.version_id = %s)")


# 契約者の単位の録音の部分の名前（判断はここだけ。話してよい声＝`ready_voices` が、契約者の組にこれが全部そろっているかを見る）。
# 名前の正本は CL-1 の phrases.RECORDED（origin/main 6364c51 の58。CL-1 の連絡帳 4-34 の 4-83 (a)・4-37・4-41、4-42・4-43 の
# hold_timeout＝保留で待ちきったときの締め、4-45 の moshimoshi＝電話に出た方が黙っているときの「もしもし？」、4-49 の
# apology_purpose_same_day・apology_purpose_later＝お詫びの電話で受付の方に用件を聞かれたときの一言、4-55 の
# callback_next_workday＝お休みのお客様に「次のご出社はいつか」を伺う一言、4-63 (a) の callback_refusal＝ご本人へのご遠慮の確かめ
# 「今後このようなお電話はご遠慮したほうがよろしいでしょうか？」。頭の「失礼いたしました。」は相づちの shitsurei）、4-64 の (a)(g) の
# callback_hurry_again＝急いでいる方への「それではまた改めてお掛けしてもよろしいでしょうか？」・callback_hurry_when＝「ご都合のいい日時は
# いつ頃でしょうか？」・dounasaimasu＝考えている方に黙られたときの「どうなさいますか？」）、4-66 の (d) の callback_not_agreed＝説明した
# 内容をご承認いただけなかったときの「ご納得いただけなかった点はどの部分になりますでしょうか？」（システムの質問 N1）、4-66 のあとの
# (f)(i) の callback_other_point・callback_other_point_2＝N2「ほかにご納得いただけない点はございますでしょうか？」（2回目からの言い方）・
# callback_agreed_now＝N3「ご説明した内容でご納得いただけましたでしょうか？」・own_fallback_point＝資料でご説明できない点の一言・
# closing_after_decline＝お断りで最終目的が果たせなくなったときの締め、4-66 のあとの (k)（11-02 7時12分のご判断）の kashikomari＝「承知しました。」が続くときの相づち「かしこまりました。」、
# 4-72 の (k)（11-03 日本時間20時48分のご承認）の callback_line＝「それでは恐れ入りますがLINEのIDをお伺いしてもよろしいでしょうか？」（システムの質問 C10）・
# wish_told_line・wish_told_email・wish_told_fax・wish_told_post・wish_told_phone・wish_told_other＝ほかのご希望を担当の者に申し伝える一言、4-75 の ai_answer＝
# 「AIですか」への標準の答え「何か問題でもございましたでしょうか？」（業務が答えを書いたときは業務の録音の部分 ai）。4-80 の
# callback_not_agreed_again＝ご承認の手順の途中で切れた電話の続きの「前回お伺いした件でご納得いただけなかった点はどの部分になりますでしょうか？」（✅ 11-04
# 日本時間18時16分）、4-81 の callback_line_how（LINE の連絡先の伺い方＝C11）・_2（伺い直し）・_again（続きの電話）・callback_mobile（SMS でご招待する携帯電話の
# 番号＝C12）・callback_email_again・callback_fax_again・callback_address_again（連絡先を伺う前に切れた電話の続き）（✅ 11-04 日本時間19時26分）、4-82 の
# callback_email_chosen（伺い方でメールを選んでいただいたあとのメールアドレスの一言。✅ 11-04 日本時間20時27分）、4-84 の callback_line_how_without_（id・sms・
# email）とその _2（選んでいただいた方法の連絡先が無いときに、ほかの方法を伺う一言とその伺い直し。✅ 11-05 日本時間0時48分・1時19分47秒・1時29分06秒）、4-85 の
# resumed_fallback_lead（続きの電話の始めのご説明が受け皿の文で流れるときに、その前に流す「前回お伺いした件ですが、」）・own_fallback_point_again（同じく答えを
# 持っていない受け皿の「恐れ入ります。前回お伺いした点は私では…」。⏳ どちらも言い方は管理者に伺い中＝文が変わっても名前は変わらない）。CL-1 の試験 test_offline が
# この一覧と同じかを確かめる（CL-2 の連絡帳で頼む）。CL-1 が部分を足したら、ここにも足す（足すと、足した部分の無い前の組の声では
# 話さない＝足りない録音を契約者に知らせ、録音を入れた組の承認を待つ）。
# ⛔ 2026-10-24（CL-1 の連絡帳 4-39）：前は、予約証の印ごとに一部の部分（お詫び・不在の伺い）だけを見て、約束の電話には業務の組の
# promise_again を求めた。お名前の入る一言（target・target_confirm・target_reconfirm・promise_again・target_successor）は業務の組に入らず、
# 名前の録音の台帳（CL-1 の DESIGN.md 4-3）から流すので、約束の電話が1本もかからなかった。契約者の組の部分はどの電話でも流れうる（相づち・
# 締め・つなぎ・通話を途中で切るときの2つ・番号を伺い直す一言）ので、電話の種類で分けずに全部を見る。
# お名前の入る一言の確かめ（その行の名前の部分と、その声の続く言葉が承認されているか）は、名前の録音の台帳の1か所（`name_needs`・
# `name_missing`。0132）。
# 業務の組の部分（名乗り・質問など）は、組を作るときに CL-1 の一覧（make_ack_clips.script_table）から作られるので、ここでは見ない。
CONTRACTOR_PARTS: tuple[str, ...] = (
    "ai_again", "apology_cut_off", "apology_missed_promise", "apology_purpose_later", "apology_purpose_same_day", "arigatou", "back", "callback_address", "callback_ask_when",
    "callback_ask_who", "callback_authority", "callback_back_date", "callback_back_time", "callback_back_today",
    "callback_call_again", "callback_email", "callback_hurry_again", "callback_hurry_when", "callback_next_workday", "callback_fax", "callback_later_time", "callback_not_agreed", "callback_number",
    "callback_agreed_now", "callback_other_point", "callback_other_point_2", "closing_after_decline", "own_fallback_point",
    "callback_reachable_time", "callback_refusal", "callback_transfer_time", "closing", "closing_declined", "departed", "dou", "dounasaimasu", "greeting", "hai", "hold_timeout", "ikaga", "kashikomari",
    "line_trouble_ask", "line_trouble_bye", "moshimoshi", "moushiwake", "moushiwake_heavy", "number_again_unreachable",
    "number_again_wrong_party", "offer_thanks", "own_fallback", "shitsurei", "shouchi", "successor_call",
    "successor_confirm", "successor_reconfirm", "successor_unavailable", "talk_agree_deep", "talk_agree_high", "talk_agree_light", "talk_cared",
    "talk_hard_bright", "talk_hard_sad", "talk_heard_glad", "talk_heard_nod", "talk_heard_sad", "talk_osewa",
    "talk_praised", "talk_welcome", "talk_yoroshiku", "wait", "wait_heavy",
    "ai_answer", "callback_line", "wish_told_email", "wish_told_fax", "wish_told_line", "wish_told_other", "wish_told_phone", "wish_told_post",
    "callback_not_agreed_again",
    "callback_line_how", "callback_line_how_2", "callback_line_how_again", "callback_mobile",
    "callback_email_again", "callback_fax_again", "callback_address_again", "callback_email_chosen",
    "callback_line_how_without_id", "callback_line_how_without_id_2", "callback_line_how_without_sms",
    "callback_line_how_without_sms_2", "callback_line_how_without_email", "callback_line_how_without_email_2",
    "resumed_fallback_lead", "own_fallback_point_again")

# その組に、共通の一言の部分がすべてあるか（SQL。ready_voices の中で、組 s ごとに見る）
_HAS_PARTS = ("NOT EXISTS (SELECT 1 FROM unnest(%s::text[]) p(part) WHERE NOT EXISTS (SELECT 1 FROM salescall.recordings r"
              " WHERE r.set_id = s.set_id AND r.part = p.part))")


def ready_voices(tx: Tx, version_id: UUID) -> list[str]:
    """その版で話してよい声の名前（名前の順）。決まりはここだけ（上の説明）。使う組は、流す組と同じ見方 current_recording_sets
    （そろった組＝部分がすべて承認済みで、その版のいまの名前の読みと合うもの。0172）。共通の一言の組（システム）には、その部分（`CONTRACTOR_PARTS`）がすべて要る。
    その版が標準から流す部分（標準の想定問答の答え。0174）があれば、その版が指す標準の版の、標準の録音の組（システム）に、その部分がすべて要る。"""
    needs = list(business_settings.standard_parts_of(tx, version_id))
    return [r["voice_name"] for r in tx.all(
        "SELECT v.voice_name FROM salescall.voices v"
        " WHERE v.retired_at IS NULL"
        "   AND EXISTS (SELECT 1 FROM salescall.voice_types t WHERE t.voice_name = v.voice_name AND t.default_for_voice)"
        "  AND " + _CURRENT.format(where=_COMMON + " AND " + _HAS_PARTS) +
        "  AND " + _CURRENT.format(where=_BUSINESS) +
        "  AND (cardinality(%s::text[]) = 0 OR " + _CURRENT.format(where=_STANDARD + " AND " + _HAS_PARTS) + ")"
        " ORDER BY v.voice_name", (list(CONTRACTOR_PARTS), version_id, needs, version_id, needs))]


def missing_parts(tx: Tx, version_id: UUID) -> list[str]:
    """その版で業務の録音（承認済み）はあるのに、システムの組（共通の一言の組・その版が指す標準の版の標準の録音の組）に足りない部分が
    ある声の、足りない部分の名前（名前の順。知らせに使う。直すのは管理者＝どちらもシステムの持ち物）。組が無い声の部分は、全部が足りない。
    ⛔ 0171 まで：取引の契約者の組の足りない部分を、版を問わず並べた。"""
    return [r["part"] for r in tx.all(
        "SELECT DISTINCT p.part FROM unnest(%s::text[]) p(part), salescall.voices v"
        " WHERE v.retired_at IS NULL AND " + _CURRENT.format(where=_BUSINESS) +
        "   AND NOT EXISTS (SELECT 1 FROM salescall.current_recording_sets s JOIN salescall.recordings r ON r.set_id = s.set_id"
        "                   WHERE s.voice_name = v.voice_name AND " + _COMMON + " AND r.part = p.part)"
        " UNION "
        "SELECT DISTINCT p.part FROM unnest(%s::text[]) p(part), salescall.voices v"
        " WHERE v.retired_at IS NULL AND " + _CURRENT.format(where=_BUSINESS) +
        "   AND NOT EXISTS (SELECT 1 FROM salescall.current_recording_sets s JOIN salescall.recordings r ON r.set_id = s.set_id"
        "                   WHERE s.voice_name = v.voice_name AND " + _STANDARD + " AND r.part = p.part)"
        " ORDER BY part", (list(CONTRACTOR_PARTS), version_id, list(business_settings.standard_parts_of(tx, version_id)),
                           version_id, version_id))]


# ═════════ 名前の録音の台帳（0132・0134。CL-1 の DESIGN.md 4-3 の「台帳の形」・CL-1 の連絡帳 4-34 の 4-122・4-39） ═════════
# お名前の入る一言（呼ぶ一言 target／個人は target_individual・確かめる一言 target_confirm・聞き返し target_reconfirm・約束の一言
# promise_again・後任の方をお呼びする一言 target_successor）は、3つの部分を組み合わせて流す（2026-10-24 ユーザー：「『営業部部長の〇〇様は』
# も『営業部部長』『〇〇様』『は』に分けて録音しておいた方が汎用性があるんじゃないですか？…永遠に録音が必要ですよ。」）：
#   - 役職の部分「営業部部長の」（鍵＝部署と役職・声）。法人の名前の行で部署・役職があるときの呼ぶ一言・後任の方の一言の頭
#   - 名前の部分「「おかだ」様」（鍵＝読み・声。同じ読みなら漢字が違っても同じ録音）。役職のみの目的のお客様は、役職の呼び方（「社長様」
#     「院長先生」。CL-1 の phrases.ROLE_HONORIFIC）が名前と同じ所に入るので、名前の部分（鍵＝役職・声）
#   - 続く言葉（鍵＝続く言葉の名前・声。例「はいらっしゃいますでしょうか？」）。
#   どこで切るかは 2026-10-25 夕のユーザーのご決定（CL-1 の連絡帳 4-55 の (2)）：役職の部分・「〇〇様」・続く言葉（助詞から）の3つ。
#   つなぎ目の録り方は CL-1 が測って決める（G-69）ので、データベースは切れ目を決め打ちせず、文が鍵の言葉を含むかだけを見る（0137。
#   ⛔ 0134 は未承認の案を守りにしていた）
# どれも人が聞いて承認したものだけを使い、その行の分が選んだ声で全部そろうまで、その行はその声ではかけない（門の `gate.choose_voice`
# の1か所。どの声でもそろわなければ待たせて、契約者に知らせる）。持ち主は2つ（0173。✅ 2026-11-02 管理者 伺い1「案1」・2026-11-04
# 管理者：「契約者が作ったものをそのままシステムに移行させるのではなく、管理者がチェックしないとシステムに入れられない」「契約者が作った
# 段階では、業務ごとの録音設定と同じ扱いです。管理者が認めて初めてシステムの共通録音に登録されます。」）：
#   - 業務の録音：契約者が、依頼者のタブの取引で、その業務のために録音して承認したもの（`add_name_*` に business_id を渡す）。その業務の
#     電話で使う（承認してすぐ・管理者が決める前から・管理者が除外したあとも＝2026-11-04 管理者「その契約者だけ使う」「その契約者は
#     使い続ける」）。
#   - システムの共通の録音：管理者がシステムの取引で作って承認したもの（代表的な名前）と、管理者が業務の録音を聞いて認めたもの
#     （`review_names`＝まとめて決める。管理者のチェックを要らないとした契約者の録音は、決まった間隔の仕事が `admit_unreviewed_names` で
#     入れる。どちらもデータベースの仕掛けが、聞いた中身をそのまま写したシステムの組を作り、その中身を承認した中身の台帳に入れる）。
#     すべての契約者が読む。
#     書けて承認できるのは管理者だけ（データベースが守る）。
#   流す録音は1か所（データベースの関数 salescall.current_name_set）：その業務の録音を先に（契約者が聞いて選んだもの）、無ければシステムの
#   共通の録音。同じ持ち主の中では、いちばん新しく作ったそろった組。
# 続く言葉の名前（データベースの守り 0134 の recording_sets_name_key と同じ。名前の正本は CL-1 の Business.replay_parts の部分の名前。呼ぶ一言は法人と個人で
# 続く言葉が違うので、個人は target_individual）
NAME_TAILS: tuple[str, ...] = ("target", "target_individual", "target_confirm", "target_reconfirm", "promise_again",
                               "target_successor", "unavailable_confirm")
NAME_UNITS: tuple[str, ...] = ("name_role", "name", "name_tail")   # 名前の録音の台帳の単位（データベースの recording_sets_unit と同じ）


@dataclass(frozen=True)
class NameKey:
    """名前の部分の鍵（声の名前は別に渡す）。reading＝名前の読み（ひらがな）か、role＝役職のみの目的のお客様の役職（呼び方「社長様」など
    で呼ぶ）の、どちらか1つ。"""
    reading: str | None = None
    role: str | None = None

    def __post_init__(self) -> None:
        if (self.reading is None) == (self.role is None):
            raise ValueError("名前の部分の鍵は、読みか（役職のみの）役職の、どちらか1つです")

    def label(self) -> str:
        """知らせに出す形（録音の文と同じ形。役職のみは役職の名前のまま）。"""
        return f"「{self.reading}」様" if self.reading is not None else f"{self.role}（役職の呼び方）"


@dataclass(frozen=True)
class NameNeeds:
    """その行で使う役職の部分・名前の部分・続く言葉（`name_needs` の答え）。no_reading＝呼ぶ材料が無い（名前を呼ぶ業務なのに読みも
    言ってみる読みも無い・役職で呼ぶ業務なのに役職が無い）。business_id＝その行の業務（その業務の録音を先に流す）。"""
    business_id: UUID
    roles: tuple[str, ...]
    names: tuple[NameKey, ...]
    tails: tuple[str, ...]
    no_reading: bool = False


def name_needs(tx: Tx, task_id: UUID) -> NameNeeds | None:
    """その行で使う、お名前の入る一言の部分（決まりはここだけ。CL-1 の 4-3 の表と phrases.target_who・target_call・target_confirm・
    target_reconfirm・Target.promise_again・successor_call の呼び方）。目的のお客様のいない業務（誰でも）は None。
    約束の一言・後任の方の一言は、約束・後任の方があとの通話で決まるので、目的のお客様のいる業務ならどの行も初めから要る（CL-1 の 4-3。
    承認を待つと約束の時刻に遅れる）。後任の方の一言は法人だけ。役職の部分は、法人の名前の行で部署・役職があるとき（呼ぶ一言・後任の方の
    一言の頭）。名前・読み・部署と役職は、会話が読む1か所（`business_settings.target_of`。読みが決まらない名前は、まず言ってみる読み＝
    会話がそれで呼ぶ。会話で確かめた読みがあればそれ）。"""
    target = business_settings.target_of(tx, task_id)["target"]
    if target is None:
        return None
    business = tx.one("SELECT r.business_id FROM salescall.tasks t JOIN salescall.runs r ON r.run_id = t.run_id"
                      " WHERE t.task_id = %s", (task_id,))["business_id"]
    corporate = target["kind"] == "法人"
    role = target["role"] or None
    named = bool(target["name"])
    tails = ("target" if corporate or not named else "target_individual", "target_confirm", "target_reconfirm",
             "promise_again", "unavailable_confirm") + (("target_successor",) if corporate else ())
    if named:
        reading = target["reading"] or None
        if reading is None:
            return NameNeeds(business, (), (), tails, no_reading=True)
        # お名前だけではどの方か分からないと言われたときに、部署とフルネームでお呼びし直す一言（会話の側の Target.full_call・後任の方の形
        # successor_full_call＝名乗りの部分 target_full・target_successor_full。CL-1 の連絡帳 4-65 の (d)）も、同じ3つの部分を組み合わせて
        # 流す：役職の部分「（部署）（役職）の」・名前の部分「（フルネームの読み）様」・続く言葉（呼ぶ一言・後任の方の一言と同じ）。
        # お伝えすることが増える行（フルネームがお名字と違う・部署がある＝target_of が役職に入っていない部署だけを渡す）だけ要る
        full = target["full_reading"] if target["full_name"] and target["full_name"] != target["name"] else reading
        full_role = (target["department"] + (role or "")) if target["department"] else role
        roles = tuple(dict.fromkeys(r for r in (role, full_role) if corporate and r))
        names = tuple(dict.fromkeys((NameKey(reading=reading), NameKey(reading=full))))
        return NameNeeds(business, roles, names, tails)
    if role is None:   # 呼ぶ名前も役職も無い（門が target_name_missing・target_role_missing で先に断る）
        return NameNeeds(business, (), (), tails, no_reading=True)
    return NameNeeds(business, (), (NameKey(role=role),), tails)   # 役職のみ：役職の呼び方が名前の部分。呼ぶ一言は法人の形（phrases.target_call）


def _missing(tx: Tx, needs: NameNeeds, voice_name: str) -> list[tuple[str, str]]:
    """その声で、承認した台帳の録音が無いもの（種類 role・name・tail と知らせの形）。見るのは流す録音を選ぶ1か所（salescall.current_name_set
    ＝その行の業務の録音か、システムの共通の録音）。"""
    if needs.no_reading:
        return [("name", "お名前の読み・役職（呼ぶ材料がありません）")]
    rows = tx.all(
        "SELECT 'role' AS what, k.i FROM unnest(%(roles)s::text[]) WITH ORDINALITY k(role, i)"
        " WHERE salescall.current_name_set(%(b)s, %(v)s, 'name_role', k.role, NULL, NULL) IS NULL"
        " UNION ALL SELECT 'name', n.i FROM unnest(%(readings)s::text[], %(nroles)s::text[]) WITH ORDINALITY n(reading, role, i)"
        " WHERE salescall.current_name_set(%(b)s, %(v)s, 'name', n.role, n.reading, NULL) IS NULL"
        " UNION ALL SELECT 'tail', t.i FROM unnest(%(tails)s::text[]) WITH ORDINALITY t(tail, i)"
        " WHERE salescall.current_name_set(%(b)s, %(v)s, 'name_tail', NULL, NULL, t.tail) IS NULL"
        " ORDER BY 1, 2",
        {"roles": list(needs.roles), "readings": [k.reading for k in needs.names], "nroles": [k.role for k in needs.names],
         "tails": list(needs.tails), "b": needs.business_id, "v": voice_name})
    label = {"role": lambda i: f"{needs.roles[i]}の（役職の部分）", "name": lambda i: needs.names[i].label(),
             "tail": lambda i: f"続く言葉 {needs.tails[i]}"}
    order = {"role": 0, "name": 1, "tail": 2}
    return sorted(((r["what"], label[r["what"]](r["i"] - 1)) for r in rows), key=lambda x: order[x[0]])


def name_missing(tx: Tx, needs: NameNeeds, voice_name: str) -> list[str]:
    """その声で、承認した台帳の録音が無い役職の部分・名前の部分・続く言葉（知らせに出す形。無ければ空＝その声で呼べる）。"""
    return [what for _, what in _missing(tx, needs, voice_name)]


def name_recording(tx: Tx, business_id: UUID, voice_name: str, *, role: str | None = None, key: NameKey | None = None,
                   tail: str | None = None) -> Part | None:
    """流す台帳の録音（役職の部分は role、名前の部分は key、続く言葉は tail のどれか1つ。会話の処理が読む。選ぶのはデータベースの
    1か所 salescall.current_name_set：その業務 business_id の録音を先に、無ければシステムの共通の録音）。無ければ None（門が、そろった
    声でしかかけないので、かける電話では起きない）。"""
    if sum(x is not None for x in (role, key, tail)) != 1:
        raise ValueError("役職の部分（role）・名前の部分（key）・続く言葉（tail）の、どれか1つを渡します")
    unit, k_role, k_reading = (("name_role", role, None) if role is not None
                               else ("name", key.role, key.reading) if key is not None else ("name_tail", None, None))
    row = tx.one(
        "SELECT r.part, r.spoken_text, r.audio_ref, r.audio_sha256 FROM salescall.recordings r"
        " WHERE r.set_id = salescall.current_name_set(%s, %s, %s, %s, %s, %s)",
        (business_id, voice_name, unit, k_role, k_reading, tail))
    return None if row is None else Part(row["part"], row["spoken_text"], row["audio_ref"], row["audio_sha256"])


def add_name_role(tx: Tx, *, voice_name: str, role: str, part: Part, business_id: UUID | None = None) -> UUID:
    """台帳に役職の部分を1つ入れる（承認の前。部分の名前は role、文はその部署と役職を含む＝データベースが守る。「の」の置き場は未決）。
    承認は `approve`。"""
    if part.part != "role":
        raise ValueError("役職の部分の録音の部分の名前は role です")
    return create_set(tx, unit="name_role", voice_name=voice_name, parts=[part], name_role=role, business_id=business_id)


def add_name_part(tx: Tx, *, voice_name: str, key: NameKey, part: Part, business_id: UUID | None = None) -> UUID:
    """台帳に名前の部分を1つ入れる（承認の前。部分の名前は name。文は「「読み」」か役職を含む＝データベースが守る。切れ目は未決）。
    承認は `approve`。"""
    if part.part != "name":
        raise ValueError("名前の部分の録音の部分の名前は name です")
    return create_set(tx, unit="name", voice_name=voice_name, parts=[part], name_key=key, business_id=business_id)


def add_name_tail(tx: Tx, *, voice_name: str, tail: str, part: Part, business_id: UUID | None = None) -> UUID:
    """台帳に続く言葉を1つ入れる（承認の前。部分の名前は続く言葉の名前と同じ。文は空でない＝データベースが守る。助詞から始まるかは
    未決）。承認は `approve`。"""
    if tail not in NAME_TAILS:
        raise ValueError(f"続く言葉の名前は {NAME_TAILS} のどれかです（{tail!r}）")
    return create_set(tx, unit="name_tail", voice_name=voice_name, parts=[part], tail=tail, business_id=business_id)


@dataclass(frozen=True)
class NameReview:
    """管理者がまだ聞いて決めていない、業務の名前の録音（`name_reviews` の答え）。どの契約者（tenant_id・tenant_name）・どの依頼者の
    タブ（client_name）・どの業務（business_name）のものかと、その契約者が管理者のチェックを要らないとされているか（skipped）を持つ
    （✅ 2026-11-04 管理者：「信頼度の高い契約者のものはそのまま通す可能性がありますし、信頼度の低い契約者はじっくりとチェックをして、
    確認する必要があるからです。」）。"""
    set_id: UUID
    tenant_id: UUID
    tenant_name: str
    client_name: str
    business_name: str
    skipped: bool
    unit: str
    voice_name: str
    key: str          # 知らせに出す形（役職の部分・名前の部分・続く言葉）
    part: Part
    created_at: object


def name_reviews(tx: Tx) -> list[NameReview]:
    """管理者がまだ聞いて決めていない、業務の名前の録音（契約者が承認して、その業務の電話で使えるようになったもの。契約者ごとに、作った
    順）。読めるのは管理者だけ（システムの取引で）＝データベースの1か所 salescall.name_recordings_to_review が守る。"""
    out = []
    for r in tx.all("SELECT * FROM salescall.name_recordings_to_review()"):
        key = (f"{r['name_role']}の（役職の部分）" if r["unit"] == "name_role"
               else NameKey(reading=r["name_reading"], role=r["name_role"] if r["name_reading"] is None else None).label()
               if r["unit"] == "name" else f"続く言葉 {r['tail']}")
        out.append(NameReview(r["set_id"], r["tenant_id"], r["tenant_name"], r["client_name"], r["business_name"], r["skipped"],
                              r["unit"], r["voice_name"], key,
                              Part(r["part"], r["spoken_text"], r["audio_ref"], r["audio_sha256"]), r["created_at"]))
    return out


REVIEW_MARKS = ("admit", "exclude")   # 管理者のレ点の意味：入れるものに付けた／除外するものに付けた


def review_names(tx: Tx, shown: list[UUID], marked: list[UUID], *, marks: str) -> dict[UUID, UUID | None]:
    """管理者が、まとめて流して聞いた業務の名前の録音を、まとめて決める（1回の操作・1つの取引で全部。✅ 2026-11-04 管理者：「まとめて
    流して、私がレ点を入れたものをのみ登録、もしくは除外を入れたもののみ、登録されないという登録方法にしてください。」）。
    shown＝聞いていただいた組（一覧に出したもの。あとから増えた組は決めない）・marked＝レ点を入れた組・marks＝レ点の意味（admit＝
    レ点を入れたものだけ入れる／exclude＝レ点を入れたものだけ除外し、残りを入れる）。入れる＝聞いた中身をそのまま写したシステムの
    共通の録音にする／除外する＝システムに入れない（その業務では使い続ける）。答え＝組ごとの、入れたシステムの組（除外は None）。
    決められるのは管理者だけ（システムの取引で）・1つの組は1回だけ＝データベースの1か所 salescall.review_name_recordings が守る。"""
    if marks not in REVIEW_MARKS:
        raise ValueError(f"レ点の意味は {REVIEW_MARKS} のどちらかです（{marks!r}）")
    if not set(marked) <= set(shown):
        raise ValueError("レ点は、聞いていただいた組にだけ付けます")
    admit = [x for x in shown if (x in marked) == (marks == "admit")]
    return {r["set_id"]: r["system_set_id"] for r in tx.all(
        "SELECT * FROM salescall.review_name_recordings(%s::uuid[], %s::uuid[])", (list(shown), admit))}


def set_name_review_skip(tx: Tx, tenant_id: UUID, skip: bool) -> None:
    """その契約者の名前の録音を、管理者のチェックなしでシステムの共通の録音に入れるか（✅ 2026-11-04 管理者：「信頼や熟練した契約者や
    スタッフが契約者の場合は、私がチェック不要にするという意味合いです。」）。決めるのは管理者だけ（システムの取引で）＝データベースが
    守る。変えた記録は残る（いまの値は、いちばん新しい行）。"""
    tx.execute("SELECT salescall.set_name_review_skip(%s, %s)", (tenant_id, skip))


def admit_unreviewed_names(tx: Tx) -> int:
    """管理者のチェックを要らないとした契約者の、まだ決めていない業務の名前の録音を、システムの共通の録音に入れる（決まった間隔の仕事
    が、システムの取引で呼ぶ＝supervisor.jobs の trusted_names）。入れ方は管理者が入れるときと同じ1か所（データベースの
    name_review_decide）。入れた組の数を返す。"""
    return tx.one("SELECT salescall.admit_unreviewed_name_recordings() AS n")["n"]


def name_coverage(tx: Tx, run_id: UUID, voice_name: str) -> dict:
    """その実施回のまだ終えていない行のうち、その声で台帳の録音が全部そろう行の数と、足りないものごとの行の数（CL-1 が前もって作る
    名字・役職の数を決めるため。DESIGN.md 4-3「台帳で足りる行が何割か」）。rows＝目的のお客様を呼ぶ行の数・covered＝そろう行の数・
    roles・names・tails＝足りない役職の部分・名前の部分・続く言葉ごとの {知らせの形: 行の数}（多い順。役職と名前は別々に数える）。"""
    tasks = tx.all("SELECT t.task_id FROM salescall.tasks t WHERE t.run_id = %s"
                   " AND coalesce((SELECT n.kind FROM salescall.current_next_actions n WHERE n.task_id = t.task_id) <> 'end', true)"
                   " ORDER BY t.task_id", (run_id,))
    rows = covered = 0
    missing: dict[str, dict[str, int]] = {"role": {}, "name": {}, "tail": {}}
    for t in tasks:
        needs = name_needs(tx, t["task_id"])
        if needs is None:
            continue
        rows += 1
        gone = _missing(tx, needs, voice_name)
        covered += not gone
        for what, m in gone:
            missing[what][m] = missing[what].get(m, 0) + 1
    ranked = {k: dict(sorted(v.items(), key=lambda x: (-x[1], x[0]))) for k, v in missing.items()}
    return {"rows": rows, "covered": covered, "roles": ranked["role"], "names": ranked["name"], "tails": ranked["tail"]}


def current_sets(tx: Tx, version_id: UUID, voice_name: str) -> dict[str, UUID | None]:
    """流すときに使う組（それぞれの単位で、そろった組のうち、いちばん新しく作った組＝データベースの見方 current_recording_sets の1か所。
    そろったかは salescall.set_ready＝部分がすべて承認済みで、その版のいまの読みと合う。0172）。共通の一言はシステムの組、業務の録音は取引の契約者のその版の組、
    標準の録音はその版が指す標準の版のシステムの組（話してよい声と同じ見方 _COMMON・_BUSINESS・_STANDARD）。標準の想定問答の答え
    （その版の business_versions.standard_parts）は標準の組から、ほかの業務の部分は業務の組から流す（0174）。
    無ければ None。"""
    rows = {r["unit"]: r["set_id"] for r in tx.all(
        "SELECT unit, set_id FROM salescall.current_recording_sets s WHERE voice_name = %s"
        " AND ((" + _COMMON + ") OR (" + _BUSINESS + ") OR (" + _STANDARD + "))", (voice_name, version_id, version_id))}
    return {"contractor": rows.get("contractor"), "business": rows.get("business"), "standard": rows.get("standard")}


def apology_voice(tx: Tx, task_id: UUID) -> str | None:
    """その行の、承認されたいちばん新しいお詫びの案に結んだ録音の声（承認が無ければ None。どの承認にも録音の組がある＝0131・0137）。
    お詫びの電話は、この声で話す（録音と同じ声。決めるのは門の gate.choose_voice）。"""
    row = tx.one("SELECT s.voice_name FROM salescall.apology_orders o JOIN salescall.apology_approvals p ON p.order_id = o.order_id"
                 " JOIN salescall.recording_sets s ON s.set_id = p.set_id"
                 " WHERE o.task_id = %s ORDER BY o.order_id DESC LIMIT 1", (task_id,))
    return None if row is None else row["voice_name"]


def parts(tx: Tx, set_id: UUID) -> dict[str, Part]:
    """組の部分（名前 → 部分）。"""
    return {r["part"]: Part(r["part"], r["spoken_text"], r["audio_ref"], r["audio_sha256"]) for r in tx.all(
        "SELECT part, spoken_text, audio_ref, audio_sha256 FROM salescall.recordings WHERE set_id = %s ORDER BY part",
        (set_id,))}
