"""記録のすべての書き出し＝基本の形（docs/DESIGN.md 7-3-5b。持ち主 CL-3）。

✅ 2026-11-21 管理者「依頼者への出力はエクセルの事もあれば、システムに登録する形もあるので業務ごとに設定します。基本の形はデータベースを
出力することもできるようにします。」・伺い3「イ」＝契約者が手元で使う控え（依頼者へは渡さない）・伺い4「データベースを見て決める」。

- お客様リスト（実施回）1つ分の記録を、データベースの記録の単位のまま、記録の種類ごとに1枚のシートで返す（`sheets`）。Excel に書くのは
  output/files.book の1か所（Excel を書く部品はそこだけ）。
- 読むだけ。どの値も、それを決める1か所の答えを読む（ここで決め直さない）：いまの状態・次の予定・かけない印・かけた回数＝一覧と同じ
  core の customer_rows.progress／質問の文＝core の business_settings.load／電話口の種別＝core の call_report.machine_party。ほかは記録の表を
  そのまま並べる（足していくだけの表なので、並べるだけで記録のすべてになる）。
- 言葉は画面と同じ言葉の表（output/words）から引く。表に無い値は、記録の値のまま（契約者の手元の控え。依頼者へは渡さない＝依頼者へ出さない
  言葉の確かめはしない）。
- どのシートにも「ファイルの行」「管理番号」を置く（シートどうしを突き合わせる鍵）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from salescall.core import business_settings, call_report, customer_rows, phone, row_fixes
from salescall.core.access import Tx
from salescall.output import words

TOKYO = ZoneInfo("Asia/Tokyo")
KEY_HEADER = ["ファイルの行", "管理番号"]


def _at(t: datetime | None) -> str:
    """日時（日本時間・年から秒まで。手元の控えなので、画面の短い書き方ではなく、並べ替えられる形）。"""
    return t.astimezone(TOKYO).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def _tel(e164: str | None) -> str:
    """電話番号（国内の形。そろえ方は core の phone の1か所）。"""
    if not e164:
        return ""
    try:
        return phone.normalize(e164).national
    except Exception:   # 記録の値が番号として読めなくても、控えには記録の値のまま出す（書き出しを止めない）
        return e164


def _word(table: dict[str, Any], name: Any) -> str:
    """言葉の表から引く（Word なら画面の言葉）。表に無ければ記録の値のまま。"""
    if name is None:
        return ""
    w = table.get(name)
    if w is None:
        return str(name)
    return w.screen if hasattr(w, "screen") else str(w)


def _value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def sheets(tx: Tx, run_id: UUID) -> list[tuple[str, list[str], list[list[Any]]]]:
    """その実施回の記録のすべて（シートの名前・見出し・行の並び。シートは DESIGN.md 7-3-5b の順）。取引の依頼者の中だけ（データベースが守る）。"""
    rows = customer_rows.progress(tx, run_id)
    keys: dict[Any, list[Any]] = {r["task_id"]: [r["source_row"] or "", r["client_ref"] or ""] for r in rows}
    task_ids = list(keys)
    now = {r["task_id"]: r for r in tx.all(
        "SELECT * FROM salescall.tasks_now WHERE run_id = %s", (run_id,))}
    out: list[tuple[str, list[str], list[list[Any]]]] = []

    # (1) お客様
    out.append(("お客様", KEY_HEADER + [
        "会社名", "会社名の読み", "お呼びする方", "お呼びする方の読み", "お呼びする方のフルネーム", "お呼びする方の部署", "お呼びする方の役職",
        "取り込んだ番号の種類", "かける番号", "いまの状態", "次の予定", "次にかける時刻", "終えた理由", "かけない印", "かけた回数", "最後の電話", "取り込んだ日時"],
        [keys[r["task_id"]] + [
            r["source_name"], (now.get(r["task_id"]) or {}).get("source_name_reading"), r["target_name"],
            (now.get(r["task_id"]) or {}).get("target_name_reading"), (now.get(r["task_id"]) or {}).get("target_full_name"),
            r["target_department"], r["target_role"], _word(words.NUMBER_LABEL_WORDS, (now.get(r["task_id"]) or {}).get("list_label")), _tel(r["dial_phone"]),
            _word(words.BUSINESS_STATES, r["business_state"]), _word(words.NEXT_KINDS, r["next_kind"]), _at(r["next_call_at"]),
            _word(words.END_REASONS, r["next_end_reason"]), "1本もかけない" if r["excluded"] else "", r["attempts"],
            (_word(words.ATTEMPT_STATES, r["last_state"]) + "・" + _at(r["last_at"])) if r["last_state"] else "",
            _at((now.get(r["task_id"]) or {}).get("created_at"))]
         for r in rows]))

    # (2) 番号と連絡先
    nums = tx.all("SELECT task_id, phone_e164, label, source, record_origin, recorded_by, added_at FROM salescall.task_numbers"
                  " WHERE task_id = ANY(%s) ORDER BY task_id, added_at", (task_ids,))
    contacts = tx.all("SELECT task_id, kind, value, service, origin, recorded_by, recorded_at FROM salescall.task_contacts"
                      " WHERE task_id = ANY(%s) ORDER BY task_id, seq", (task_ids,))
    out.append(("番号と連絡先", KEY_HEADER + ["種類", "値", "サービス", "出どころ", "記録した方", "日時"],
                [keys[n["task_id"]] + [_word(words.NUMBER_LABEL_WORDS, n["label"]), _tel(n["phone_e164"]), "",
                                       _word(words.ORIGIN_WORDS, n["record_origin"] or n["source"]), n["recorded_by"], _at(n["added_at"])]
                 for n in nums] +
                [keys[c["task_id"]] + [_word(words.IMPORT_FIELD_WORDS, c["kind"]), c["value"], c["service"],
                                       _word(words.ORIGIN_WORDS, c["origin"]), c["recorded_by"], _at(c["recorded_at"])]
                 for c in contacts]))

    # (3) 電話
    calls = tx.all("SELECT a.attempt_id, a.task_id, a.claimed_at, a.person_since, a.ended_at, a.phone_e164, l.name AS line_name, a.purpose,"
                   " a.state::text AS state, a.hangup_side::text AS hangup_side, a.notice, a.talked, a.reached_target, a.trouble, a.trouble_why"
                   " FROM salescall.attempts a LEFT JOIN salescall.lines l ON l.line_id = a.line_id"
                   " WHERE a.task_id = ANY(%s) AND a.state <> 'released' ORDER BY a.claimed_at", (task_ids,))
    when = {c["attempt_id"]: _at(c["claimed_at"]) for c in calls}
    out.append(("電話", KEY_HEADER + ["かけた日時", "お客様が出た日時", "終わった日時", "かけた番号", "回線", "電話の目的", "電話の状態",
                                      "電話口", "お客様と話せた", "目的のお客様と話せた", "こちらの不具合", "不具合の中身"],
                [keys[c["task_id"]] + [
                    _at(c["claimed_at"]), _at(c["person_since"]), _at(c["ended_at"]), _tel(c["phone_e164"]), c["line_name"], _word(words.CALL_PURPOSE_WORDS, c["purpose"]),
                    _word(words.ATTEMPT_STATES, c["state"]), _word(words.CALL_END_CLIENT, call_report.machine_party(tx, c["attempt_id"])),
                    "はい" if c["talked"] else "", "はい" if c["reached_target"] else "", words.call_end(c["trouble"]), c["trouble_why"] or ""]
                 for c in calls]))

    # (4) やり取り
    task_of = {c["attempt_id"]: c["task_id"] for c in calls}
    said = tx.all("SELECT attempt_id, seq, speaker, turn, said, at_s FROM salescall.call_utterances WHERE attempt_id = ANY(%s)"
                  " ORDER BY attempt_id, seq", (list(task_of),)) if task_of else []
    out.append(("やり取り", KEY_HEADER + ["電話（かけた日時）", "順", "話した側", "通話の始めからの秒", "言葉"],
                [keys[task_of[u["attempt_id"]]] + [when[u["attempt_id"]], u["seq"], _word(words.SPEAKER_WORDS, u["speaker"]),
                                                   _value(u["at_s"]), u["said"]] for u in said]))

    # (5) 答え（質問の文は、その答えの版の業務設定から＝core の business_settings.load の1か所）
    items = tx.all("SELECT task_id, version_id, question_id, state::text AS state, value, condition, hedged, reason, updated_at"
                   " FROM salescall.item_values WHERE task_id = ANY(%s) ORDER BY task_id, question_id", (task_ids,))
    texts: dict[Any, dict[str, str]] = {}
    for v in {i["version_id"] for i in items}:
        try:
            texts[v] = {q.id: q.text for q in business_settings.load(tx, v).questions}
        except Exception:   # 版が読めなくても、答えの記録は出す（質問の文は空）
            texts[v] = {}
    out.append(("答え", KEY_HEADER + ["質問", "答えの状態", "答え", "条件", "お客様の言葉", "理由", "日時"],
                [keys[i["task_id"]] + [texts.get(i["version_id"], {}).get(i["question_id"], i["question_id"]),
                                       _word(words.ITEM_STATES, i["state"]), _value(i["value"]), i["condition"], i["hedged"], i["reason"],
                                       _at(i["updated_at"])] for i in items]))

    # (6) 結果の版
    results = tx.all("SELECT r.task_id, r.version_no, r.kind::text AS kind, r.business_state::text AS business_state, r.complaint, r.decided_at,"
                     " r.decided_by, r.attempt_id, r.detail, x.stage AS released_stage, x.released_at"
                     " FROM salescall.task_results r LEFT JOIN salescall.result_releases x ON x.task_id = r.task_id AND x.version_no = r.version_no"
                     " WHERE r.task_id = ANY(%s) ORDER BY r.task_id, r.version_no", (task_ids,))
    out.append(("結果の版", KEY_HEADER + ["版", "決め方", "業務の結果", "苦情", "決めた日時", "決めた方", "もとの電話（かけた日時）", "確定した段", "確定した日時"],
                [keys[r["task_id"]] + [r["version_no"], _word(words.RESULT_KIND_WORDS, r["kind"]), _word(words.BUSINESS_STATES, r["business_state"]),
                                       "あり" if r["complaint"] else "", _at(r["decided_at"]), r["decided_by"], when.get(r["attempt_id"], ""),
                                       r["released_stage"] or "", _at(r["released_at"])] for r in results]))

    # (7) お客様のお話（いちばん新しい版の判定の中身を、1件1行に。種類の言葉は words.DETAIL_KIND_WORDS）
    latest: dict[Any, dict[str, Any]] = {}
    for r in results:
        latest[r["task_id"]] = r
    talk: list[list[Any]] = []
    for tid, r in latest.items():
        for kind, v in (r["detail"] or {}).items():
            entries = v if isinstance(v, list) else [v]
            for e in entries:
                said_ = e.get("said") if isinstance(e, dict) else None
                question = e.get("question") if isinstance(e, dict) else None
                rest = {k: x for k, x in e.items() if k not in ("said", "question")} if isinstance(e, dict) else e
                talk.append(keys[tid] + [r["version_no"], _word(words.DETAIL_KIND_WORDS, kind), question or "", said_ or "", _value(rest)])
    out.append(("お客様のお話", KEY_HEADER + ["結果の版", "種類", "質問", "お客様の言葉", "記録のまま"], talk))

    # (8) 操作の記録（かけない印・直し・発信前の承認。どれも足していくだけの表）
    ops: list[tuple[Any, list[Any]]] = []
    for e in tx.all("SELECT task_id, excluded, forced, reason, decided_by, decided_at FROM salescall.task_exclusions WHERE task_id = ANY(%s)", (task_ids,)):
        level = ("force" if e["forced"] else "exclude") if e["excluded"] else "call"
        ops.append((e["decided_at"], keys[e["task_id"]] + [_at(e["decided_at"]), "かけるかを変えた", words.exclusion_level(level), e["reason"], e["decided_by"]]))
    for f in tx.all("SELECT task_id, field, value, reading, recorded_by, recorded_at, same_person FROM salescall.task_fixes WHERE task_id = ANY(%s)", (task_ids,)):
        what = row_fixes.FIELD_NAMES.get(f["field"], f["field"]) + "：" + (f["value"] or "（消す）") + (f"（読み {f['reading']}）" if f["reading"] else "")
        ops.append((f["recorded_at"], keys[f["task_id"]] + [_at(f["recorded_at"]), "お名前・連絡先を直した", what,
                                                            "" if f["same_person"] is None else ("同じ方" if f["same_person"] else "違う方"), f["recorded_by"]]))
    for d in tx.all("SELECT f.task_id, f.recorded_by, f.recorded_at, n.phone_e164 FROM salescall.task_dial_fixes f"
                    " JOIN salescall.task_numbers n ON n.task_id = f.task_id AND n.contact_id = f.contact_id WHERE f.task_id = ANY(%s)", (task_ids,)):
        ops.append((d["recorded_at"], keys[d["task_id"]] + [_at(d["recorded_at"]), "かける番号を直した", _tel(d["phone_e164"]), "", d["recorded_by"]]))
    for a in tx.all("SELECT task_id, approved_at, approved_by FROM salescall.task_approvals WHERE task_id = ANY(%s)", (task_ids,)):
        ops.append((a["approved_at"], keys[a["task_id"]] + [_at(a["approved_at"]), "発信の前に承認した", "", "", a["approved_by"]]))
    ops.sort(key=lambda o: o[0] or datetime.min.replace(tzinfo=TOKYO))
    out.append(("操作の記録", KEY_HEADER + ["日時", "操作", "中身", "理由・添えたこと", "した方"], [row for _, row in ops]))
    return out
