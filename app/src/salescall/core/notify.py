"""知らせの台帳へ出来事を入れる（Q23。2026-10-14 ユーザー承認）。持ち主 CL-2（入れる側とデータの形）。送るのは CL-3（output/）。

- 出来事を台帳に入れるのは、この部品の `emit` だけ（I-12。app/checks が見張る）。停止・品質の段・締め・暦の取り込みなどは、
  出来事を入れるだけで、自分で送らない（同じ知らせが二重に届く・漏れるのを防ぐ）。
- 同じ出来事は1回だけ（出来事の鍵 `key`。同じ鍵で2回呼んでも1つ）。鍵は出来事そのものを指す名前にする（例 "stop:123"）。
- 契約者・依頼者は取引のものをそのまま使う（全体の取引なら全体の出来事）。
- **管理者あて**（`admin=True`。2026-10-16 ユーザー承認）：プログラムの誤り・エラーなど、管理者が確かめ・承認する出来事（直すのは担当＝修正依頼。core/fix_requests.py）は、
  契約者の台帳に入れるのと同じ呼び出しで、管理者あての写しも入れる（契約者を持たない行。読めるのは管理者と送る処理だけ。0027）。
  どの出来事を管理者に知らせるかは、呼ぶ側の決まった1か所（判定できない原因＝`judge.PROGRAM_CAUSES`、エラー＝すべて）が決める。
  エラーのうち、契約者にも知らせるのは、処置の表（core/errors.py）が「知らせる」とした種類だけ（ほかは管理者あてだけ。
  `contractor=False`）。2026-10-16 見直し：前は「知らせる」の種類だけを管理者にも送り、キーの無効化など、直せるのが管理者
  だけのエラーが、管理者に届いていなかった。
- 要約に、お客様の電話番号・名前・発話の中身を入れない（画面と知らせに出す。中身は refs の ID で指す。CL-3 の 4-1e の5）。
  要約は、この部品を呼ぶ処理が、決まった言葉と ID・数から作る（会話の文字をそのまま入れない）。
"""

from __future__ import annotations

import json
from typing import Any

from salescall.core.access import Tx

# 出来事の種類（名前 → 日本語）。一覧の正本はここだけ（画面・送る処理は、これを読む）。
KINDS: dict[str, str] = {
    "stopped": "発信を止めた",
    "resumed": "止めていた発信を解いた（自動で・契約者の判断で）",
    "quality_stage": "品質の段が進んだ",
    "judge_unknown": "判定できない通話があった（判定の処理か入力の誤り）",
    "error_notice": "知らせるべきエラーがあった（9章の表）",
    "calendar_sync_failed": "祝日の暦を取りに行けなかった",
    "calendar_next_year_missing": "年末なのに、祝日の公式の一覧に翌年の分がまだ無い（年が替わると電話をかけない。毎日取りに行く）",
    "budget": "費用の上限に達した・報酬に対する費用の割合が上限を超えた",
    "forbidden_speech": "言ってはいけないことを言った通話があった（その行はひとまず終えた）",
    "complaint": "苦情があった通話があった（その行は終えた。契約者が対応する特例＝Intent.md 18-3）",
    "apology": "お詫びの電話の結果（伝えた・決めた回数かけても届かなかった）",
    "deficit_warning": "赤字の警告（止めない。期限までに確認ボタンが押されなければ、契約者と管理者へもう一度知らせる＝2026-10-22 深夜 ユーザー「止めない。知らせるだけでいい」）",
    "program_update": "PC のプログラムの更新が要る（データベースと版が合わないあいだ、その PC は新しい電話をかけない）",
    "backup_failed": "控えが取れなかった・戻せなかった・写せなかった（止めない。プログラムの誤りなら CL-2 に修正依頼を出した。ハードディスクが外れていれば「つないでください」）",
    "answer_rate_drop": "回線（番号）の、お客様に出てもらえた割合が大きく下がった（迷惑電話として扱われているおそれ。発信は止めない。Intent.md 8-6）",
    "sample_ready": "誤判定率を測るために通話を抜き取った（管理者か契約者が録音を聞いて正解を付ける。Intent.md 16-3）",
    "setting_missing": "設定がされていません（注意。止めない：費用の単価・報酬の決まり など、お客様に影響しない設定の抜け）",
    "pc_down": "PC（電話をつなぐ部品）からの動いている合図が途切れた（その PC の回線ではかけない。止めない。DESIGN.md 5-5）",
    "line_undecided": "テスト番号で、回線のせいか番号のせいかを決められない・止めた回線を再開してよいか確かめられない（テスト番号の答えが見分けられない＝続き・停止ごとに1回。電話をつなぐ部品の担当へ修正依頼も出した／テスト番号が無い＝続きごとに1回。その続きの行は待ったまま。0141・0143）",
    "task_resting": "こちらのプログラムの不具合で、同じ行の電話が続けて終わったので、その行を休ませた（次の行へ進む。その不具合を直したと記録され、そのあとプログラムが替わると自動で戻る。管理者・契約者も戻せる。0139・0141）",
    "pc_line_conflict": "同じ回線を2台の PC がつないでいる（先につないだ PC でかける。もう一方の PC の設定を直す。DESIGN.md 5-5）",
    "decision_approval": "検証モードの業務で、お客様の断り・番号違い・怒っていたを、契約者が録音とやり取りを確かめて承認するのを待っている"
                         "（承認か差し戻しを押すまで、そのお客様にかけない。2026-10-30 管理者のご決定 (6)）",
}


class UnknownKind(Exception):
    """一覧に無い種類。"""


def emit(tx: Tx, kind: str, *, key: str, summary: str, refs: dict[str, Any] | None = None, admin: bool = False,
         contractor: bool = True) -> bool:
    """出来事を台帳に入れる（取引の契約者・依頼者の出来事）。admin＝管理者あての写しも入れる。contractor=False＝契約者の台帳には
    入れない（管理者あてだけ。admin と組で使う）。新しく入れたら True、同じ出来事がもうあれば False。同じ出来事かは、鍵と契約者で
    決まる（管理者あての写しは、鍵と出どころの契約者。データベースの一意の約束の1か所＝0064。鍵に契約者を入れなくてよい）。"""
    if kind not in KINDS:
        raise UnknownKind(kind)
    if not (admin or contractor):
        raise ValueError("あて先がありません（管理者か契約者のどちらか）")
    if admin:
        got = tx.one("SELECT salescall.notify_admin(%s, %s, %s, %s) AS new",
                     (kind, key, summary, json.dumps(refs or {}, ensure_ascii=False, default=str)))["new"]
        if not contractor:
            return got
    row = tx.one(
        "INSERT INTO salescall.notification_events (tenant_id, client_id, kind, event_key, summary, refs)"
        " VALUES (salescall.cur_tenant(), salescall.cur_client(), %s, %s, %s, %s)"
        " ON CONFLICT (event_key, tenant_id, origin_tenant) DO NOTHING RETURNING event_id",   # 同じ出来事かは契約者ごと（0064）
        (kind, key, summary, json.dumps(refs or {}, ensure_ascii=False, default=str)))
    return row is not None
