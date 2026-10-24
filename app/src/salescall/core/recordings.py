"""録音の版と承認の記録（定型の音声の台帳。DESIGN.md 6-13）と、話してよい声。持ち主 CL-2。表の作り 0047。

- 録音は2つの単位（2026-10-15 ユーザー）：契約者の単位（契約者と声の名前ごと。相づち・締めなど、どの依頼者にも同じもの）と、
  依頼者の単位（業務設定の版と声の名前ごと。トークスクリプト）。声の名前ごとに1組（声の型ごとではない）。
- 組は変えられない（作り直しは新しい組）。部分（part）の一覧は会話の側（CL-1）の1か所が決める。ここは、渡された部分をそのまま残す。
- 承認は組ごとに1回。管理者か契約者が聞いて承認する（事前の録音チェック。データベースが守る）。承認した組には部分を足せない。
- **話してよい声は `ready_voices` の1か所で決める**：使う声（使わない印の無い声）のうち、承認した録音が、契約者の単位と、その版の
  依頼者の単位の両方にあり、初めの声の型を持つもの（2026-10-17 ユーザー：「事前の録音チェックになる方向性」「新しい声が増える
  かもしれません」＝声を足しても処理は変えない）。門（core/gate.py の `choose_voice`）だけが使う。契約者の組には、契約者の単位の
  部分（`CONTRACTOR_PARTS`。名前の正本は CL-1 の phrases.RECORDED）がすべて要る（CL-1 の連絡帳 4-39）。
- 流すときに使う組は `current_sets`（それぞれの単位で、いちばん新しく承認した組のうち、名前の読みがその版のいまの読みと合うもの＝
  見方 current_recording_sets）。
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


def create_set(tx: Tx, *, unit: Literal["contractor", "business", "apology"], voice_name: str, parts: list[Part],
               version_id: UUID | None = None, order_id: int | None = None) -> UUID:
    """録音の組を作る（承認の前）。契約者の単位は取引の契約者、依頼者の単位は取引の依頼者の、その版。お詫びの単位（apology）は
    取引の依頼者の、そのお詫びの案（order_id。部分は apology.PARTS の2つで、文は案の文と同じ・声は誤った通話の声＝データベースが
    守る。0131）。"""
    if not parts:
        raise ValueError("録音の部分がありません")
    if len({p.part for p in parts}) != len(parts):
        raise ValueError("同じ名前の部分が2つあります")
    if (unit == "business") != (version_id is not None) or (unit == "apology") != (order_id is not None):
        raise ValueError("依頼者の単位の組には業務設定の版を、お詫びの単位の組にはお詫びの案を渡します（契約者の単位の組には、どちらも"
                         "渡しません）")
    chosen = _chosen_readings(tx, parts, version_id)
    set_id = tx.one(
        "INSERT INTO salescall.recording_sets (tenant_id, unit, voice_name, client_id, version_id, order_id)"
        " VALUES (salescall.cur_tenant(), %s, %s, CASE WHEN %s <> 'contractor' THEN salescall.cur_client() END, %s, %s)"
        " RETURNING set_id", (unit, voice_name, unit, version_id, order_id))["set_id"]
    for p in parts:
        tx.execute("INSERT INTO salescall.recordings (tenant_id, client_id, set_id, part, spoken_text, audio_ref, audio_sha256)"
                   " SELECT tenant_id, client_id, set_id, %s, %s, %s, %s FROM salescall.recording_sets WHERE set_id = %s",
                   (p.part, p.spoken_text, p.audio_ref, p.audio_sha256, set_id))
    for part, name, reading in chosen:
        tx.execute("INSERT INTO salescall.recording_readings (tenant_id, client_id, set_id, part, name_text, reading)"
                   " SELECT tenant_id, client_id, set_id, %s, %s, %s FROM salescall.recording_sets WHERE set_id = %s",
                   (part, name, reading, set_id))
    return set_id


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
    """録音の組を承認する（管理者か契約者が聞いて。データベースが守る）。"""
    row = tx.one("INSERT INTO salescall.recording_approvals (tenant_id, client_id, set_id, approved_by)"
                 " SELECT tenant_id, client_id, set_id, salescall.cur_actor() FROM salescall.recording_sets WHERE set_id = %s"
                 " RETURNING set_id", (set_id,))
    if row is None:
        raise LookupError(f"録音の組がありません（または、取引の範囲の外）：{set_id}")


_CURRENT = ("EXISTS (SELECT 1 FROM salescall.current_recording_sets s"
            " WHERE s.voice_name = v.voice_name AND s.tenant_id = salescall.cur_tenant() AND {where})")


# 契約者の単位の録音の部分の名前（判断はここだけ。話してよい声＝`ready_voices` が、契約者の組にこれが全部そろっているかを見る）。
# 名前の正本は CL-1 の phrases.RECORDED（origin/main 10f2e22 の53。CL-1 の連絡帳 4-34 の 4-83 (a)・4-37・4-41）。CL-1 の試験 test_offline が
# この一覧と同じかを確かめる（CL-2 の連絡帳で頼む）。CL-1 が部分を足したら、ここにも足す（足すと、足した部分の無い前の組の声では
# 話さない＝足りない録音を契約者に知らせ、録音を入れた組の承認を待つ）。
# ⛔ 2026-10-24（CL-1 の連絡帳 4-39）：前は、予約証の印ごとに一部の部分（お詫び・不在の伺い）だけを見て、約束の電話には業務の組の
# promise_again を求めた。お名前の入る一言（target・target_confirm・target_reconfirm・promise_again・target_successor）は業務の組に入らず、
# 名前の録音の台帳（CL-1 の DESIGN.md 4-3）から流すので、約束の電話が1本もかからなかった。契約者の組の部分はどの電話でも流れうる（相づち・
# 締め・つなぎ・通話を途中で切るときの2つ・番号を伺い直す一言）ので、電話の種類で分けずに全部を見る。
# ⏳ お名前の入る一言の確かめ（その行の名前の部分と、その声の続く言葉が承認されているか）は、名前の録音の台帳を作るときに入れる。
# 業務の組の部分（名乗り・質問など）は、組を作るときに CL-1 の一覧（make_ack_clips.script_table）から作られるので、ここでは見ない。
CONTRACTOR_PARTS: tuple[str, ...] = (
    "ai_again", "apology_cut_off", "apology_missed_promise", "arigatou", "back", "callback_address", "callback_ask_when",
    "callback_ask_who", "callback_authority", "callback_back_date", "callback_back_time", "callback_back_today",
    "callback_call_again", "callback_email", "callback_fax", "callback_later_time", "callback_number",
    "callback_reachable_time", "closing", "closing_declined", "departed", "dou", "greeting", "hai", "ikaga",
    "line_trouble_ask", "line_trouble_bye", "moushiwake", "moushiwake_heavy", "number_again_unreachable",
    "number_again_wrong_party", "offer_thanks", "own_fallback", "shitsurei", "shouchi", "successor_call",
    "successor_confirm", "successor_reconfirm", "talk_agree_deep", "talk_agree_high", "talk_agree_light", "talk_cared",
    "talk_hard_bright", "talk_hard_sad", "talk_heard_glad", "talk_heard_nod", "talk_heard_sad", "talk_osewa",
    "talk_praised", "talk_welcome", "talk_yoroshiku", "wait", "wait_heavy")

# その組に、契約者の単位の部分がすべてあるか（SQL。ready_voices の中で、組 s ごとに見る）
_HAS_PARTS = ("NOT EXISTS (SELECT 1 FROM unnest(%s::text[]) p(part) WHERE NOT EXISTS (SELECT 1 FROM salescall.recordings r"
              " WHERE r.set_id = s.set_id AND r.part = p.part))")


def ready_voices(tx: Tx, version_id: UUID) -> list[str]:
    """その版で話してよい声の名前（名前の順）。決まりはここだけ（上の説明）。使う組は、流す組と同じ見方 current_recording_sets
    （承認した組のうち、その版のいまの名前の読みと合うもの）。契約者の組には、契約者の単位の部分（`CONTRACTOR_PARTS`）がすべて要る。"""
    return [r["voice_name"] for r in tx.all(
        "SELECT v.voice_name FROM salescall.voices v"
        " WHERE v.retired_at IS NULL"
        "   AND EXISTS (SELECT 1 FROM salescall.voice_types t WHERE t.voice_name = v.voice_name AND t.default_for_voice)"
        "  AND " + _CURRENT.format(where="s.unit = 'contractor' AND " + _HAS_PARTS) +
        "  AND " + _CURRENT.format(where="s.unit = 'business' AND s.version_id = %s") +
        " ORDER BY v.voice_name", (list(CONTRACTOR_PARTS), version_id))]


def missing_parts(tx: Tx) -> list[str]:
    """承認した契約者の組のある声のうち、契約者の単位の部分が足りない声の、足りない部分の名前（名前の順。知らせに使う）。"""
    return [r["part"] for r in tx.all(
        "SELECT DISTINCT p.part FROM unnest(%s::text[]) p(part), salescall.current_recording_sets s"
        " WHERE s.tenant_id = salescall.cur_tenant() AND s.unit = 'contractor'"
        "   AND NOT EXISTS (SELECT 1 FROM salescall.recordings r WHERE r.set_id = s.set_id AND r.part = p.part)"
        " ORDER BY p.part", (list(CONTRACTOR_PARTS),))]


def current_sets(tx: Tx, version_id: UUID, voice_name: str) -> dict[str, UUID | None]:
    """流すときに使う組（それぞれの単位で、いちばん新しく承認した組＝データベースの見方 current_recording_sets の1か所。承認のときの
    読みの確かめも同じ見方）。無ければ None。"""
    rows = {r["unit"]: r["set_id"] for r in tx.all(
        "SELECT unit, set_id FROM salescall.current_recording_sets WHERE tenant_id = salescall.cur_tenant() AND voice_name = %s"
        " AND (unit = 'contractor' OR version_id = %s)", (voice_name, version_id))}
    return {"contractor": rows.get("contractor"), "business": rows.get("business")}


def apology_voice(tx: Tx, task_id: UUID) -> str | None:
    """その行の、承認されたいちばん新しいお詫びの案に結んだ録音の声（無ければ None。0131 より前の録音の無い承認も None）。
    お詫びの電話は、この声で話す（録音と同じ声。決めるのは門の gate.choose_voice）。"""
    row = tx.one("SELECT s.voice_name FROM salescall.apology_orders o JOIN salescall.apology_approvals p ON p.order_id = o.order_id"
                 " LEFT JOIN salescall.recording_sets s ON s.set_id = p.set_id"
                 " WHERE o.task_id = %s ORDER BY o.order_id DESC LIMIT 1", (task_id,))
    return None if row is None else row["voice_name"]


def parts(tx: Tx, set_id: UUID) -> dict[str, Part]:
    """組の部分（名前 → 部分）。"""
    return {r["part"]: Part(r["part"], r["spoken_text"], r["audio_ref"], r["audio_sha256"]) for r in tx.all(
        "SELECT part, spoken_text, audio_ref, audio_sha256 FROM salescall.recordings WHERE set_id = %s ORDER BY part",
        (set_id,))}
