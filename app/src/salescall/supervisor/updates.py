"""PC のプログラムの更新（DESIGN.md 5-4-1。表の作り 0080）。持ち主 CL-2。

2026-10-18 ユーザー：「インストーラーは作ります。入れるだけで済む形にします。⇒はい、ただし、プログラム更新やバージョンアップが自動もしくは
手動で反映されるようにしてください。」

- **版が合うかの判断は `gap` の1か所**：プログラムが持つ表の作りの版（そのプログラムと一緒に入っている、いちばん新しい表の作りの名前）と、
  データベースの版（`salescall.schema_version`）を比べる。ずれていれば、見守り役は新しい電話をかけない（いまの通話は最後まで。版が
  合ったら、またかける）。古いプログラムが新しいデータベースの決まりを知らずにかけると、守りが外れうるため（お客様に影響しうる）。
  - プログラムが古い（データベースが先に新しくなった）＝PC の更新が要る：自動の契約者（設定 pc.auto_update）なら、更新の部品が
    空いているときに入れ替える（⏳ 入れ替えの部品＝インストーラーと一緒に作る。署名を確かめてから入れ替える）。手動の契約者には
    「更新してください」と知らせる（1日1回）。
  - プログラムが新しい（データベースがまだ古い）＝データベースへの表の作りの流し忘れ：管理者へ（CL-2 に修正依頼）。
- 通話の途中では入れ替えない（入れ替えるのは、回線ごとの処理がいまの通話を終えて止まったあと）。
"""

from __future__ import annotations

from uuid import UUID

from salescall.core import migrate, notify, settings
from salescall.core.access import Tx

SAME, PROGRAM_OLD, PROGRAM_NEW = "same", "program_old", "program_new"


def program_schema() -> str:
    """このプログラムが持つ、いちばん新しい表の作りの名前。"""
    return migrate.migration_files()[-1].name


def gap(db_schema: str | None, program: str | None = None) -> str:
    """プログラムとデータベースの版がずれているか（判断はここだけ）。表の作りの名前は番号で始まるので、名前の順が版の順。"""
    program = program or program_schema()
    if db_schema is None or db_schema < program:
        return PROGRAM_NEW
    if db_schema > program:
        return PROGRAM_OLD
    return SAME


def check(tx: Tx, tenant_id: UUID, *, program: str | None = None) -> str:
    """その契約者の取引で、版を比べる。ずれていれば知らせる（1日1回）。結果（SAME など）を返す。"""
    got = gap(tx.one("SELECT salescall.schema_version() AS v")["v"], program)
    if got == PROGRAM_OLD:
        auto = bool(settings.resolve(tx, "pc.auto_update", tenant_id=tenant_id, client_id=None, business_id=None))
        day = tx.one("SELECT salescall.jst(now())::date AS d")["d"]
        notify.emit(tx, "program_update", key=f"program_update:{day}",
                    summary=("PC のプログラムの更新があります。空いているときに自動で入れ替えます（いまの通話は最後まで）。"
                             if auto else "PC のプログラムの更新があります。画面の「更新」を押してください。")
                            + "更新が済むまで、この PC からは新しい電話をかけません。",
                    refs={"auto": auto})
    return got


def describe(result: str) -> str:
    """記録に残す、ずれの説明（値は入れない）。"""
    return {PROGRAM_OLD: "PC のプログラムが、データベースより古い（更新を待っている）",
            PROGRAM_NEW: "PC のプログラムが、データベースより新しい（データベースへの表の作りの流し忘れ）"}.get(result, "")

