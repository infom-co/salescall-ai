"""PC のプログラムの更新（DESIGN.md 5-4-1。表の作り 0080・0083）。持ち主 CL-2。

2026-10-18 ユーザー：「インストーラーは作ります。入れるだけで済む形にします。⇒はい、ただし、プログラム更新やバージョンアップが自動もしくは
手動で反映されるようにしてください。」

- **プログラムの版は、読み込んだときに1回だけ決める**（`PROGRAM`。動いている途中でファイルが入れ替わっても、動いているコードの版の
  まま＝本体と回線ごとの処理は、それぞれ自分の版で比べる）。版は、いちばん新しい表の作りの名前と、すべての表の作りの指紋
  （core/migrate.fingerprint。データベースの側は salescall.schema_fingerprint＝同じ決まり）。
- **版が合うかの判断は `gap` の1か所**。合わない（SAME 以外）あいだは：
  - 回線ごとの処理は新しい電話をかけない（自分で確かめる＝`line`。本体も、その回線の処理を止めて立ち上げない。いまの通話は最後まで）。
  - 本体は、決まった間隔の仕事（締め・控え など）もしない（古いプログラムが新しいデータベースの決まりを知らずに書くと、守りが外れうる）。
  - 版を比べられない（データベースに届かない・表の作りのファイルが無い）ときも、合わないとみなす（かけない側に倒す）。
- 知らせ：プログラムが古い（PROGRAM_OLD）＝PC の更新が要る：その契約者と管理者へ（1日1回）。自動で入れ替える部品（設定 pc.auto_update が
  自動で、その PC に入れ替えの部品があるとき）なら「自動で入れ替えます」、無ければ「更新してください」。プログラムが新しい・表の作りが
  食い違う・分からない＝流し忘れか入れ方の誤り：CL-2 へ修正依頼（変わったときだけ＝呼ぶ側）。
- 通話の途中では入れ替えない（入れ替えるのは、回線ごとの処理がいまの通話を終えて止まったあと。⏳ 入れ替えの部品＝インストーラーと一緒）。
"""

from __future__ import annotations

from uuid import UUID

from salescall.core import migrate, notify, settings
from salescall.core.access import Tx

SAME, PROGRAM_OLD, PROGRAM_NEW, MISMATCH, UNKNOWN = "same", "program_old", "program_new", "mismatch", "unknown"
PROGRAM: tuple[str, str] | None = migrate.fingerprint()   # 読み込んだときの版（いちばん新しい名前・指紋）


_OWN = object()   # 呼ぶ側が版を渡さなければ、このプログラムの版（PROGRAM）


def gap(db: tuple[str | None, str | None] | None, program: object = _OWN) -> str:
    """プログラムとデータベースの版がずれているか（判断はここだけ）。db＝データベースの（いちばん新しい名前・指紋）。"""
    program = PROGRAM if program is _OWN else program
    if program is None or db is None or db[0] is None:
        return UNKNOWN
    if db[1] == program[1]:
        return SAME
    if db[0] > program[0]:
        return PROGRAM_OLD
    if db[0] < program[0]:
        return PROGRAM_NEW
    return MISMATCH                      # いちばん新しい名前は同じなのに、途中が違う（流し忘れ・同じ番号の別の表の作り）


def current(tx: Tx, program: object = _OWN) -> str:
    """いまのずれ（どの取引でも読める）。"""
    row = tx.one("SELECT salescall.schema_version() AS v, salescall.schema_fingerprint() AS f")
    return gap((row["v"], row["f"]), program)


def check(tx: Tx, tenant_id: UUID, *, can_replace: bool, program: object = _OWN) -> str:
    """その契約者の取引で、版を比べる。プログラムが古ければ、その契約者と管理者へ知らせる（1日1回）。can_replace＝この PC に、自動で
    入れ替える部品があるか（⏳ インストーラーと一緒に作る）。結果（SAME など）を返す。"""
    got = current(tx, program)
    if got == PROGRAM_OLD:
        auto = can_replace and bool(settings.resolve(tx, "pc.auto_update", tenant_id=tenant_id, client_id=None, business_id=None))
        day = tx.one("SELECT salescall.jst(now())::date AS d")["d"]
        notify.emit(tx, "program_update", key=f"program_update:{day}",
                    summary=("PC のプログラムの更新があります。空いているときに自動で入れ替えます（いまの通話は最後まで）。"
                             if auto else "PC のプログラムの更新があります。プログラムを更新してください。")
                            + "更新が済むまで、この PC からは新しい電話をかけません。",
                    refs={"auto": auto}, admin=True, contractor=True)
    return got


def describe(result: str) -> str:
    """記録に残す、ずれの説明（値は入れない）。"""
    return {PROGRAM_OLD: "PC のプログラムが、データベースより古い（更新を待っている）",
            PROGRAM_NEW: "PC のプログラムが、データベースより新しい（データベースへの表の作りの流し忘れ）",
            MISMATCH: "PC のプログラムとデータベースの表の作りが食い違う（流し忘れ・同じ番号の別の表の作り）",
            UNKNOWN: "版を比べられない（データベースに届かない・表の作りのファイルが無い）"}.get(result, "")
