"""見守り役（PC で常に動き、システムの処理をまとめて動かす役。Intent.md 14-1・DESIGN.md 5-4）。持ち主 CL-2。

- `main`：本体（起動のときの後始末・回線の借用と延長・回線ごとの処理の立ち上げと見張り・決まった間隔の仕事）。
- `line`：回線ごとの処理（別の処理＝プロセスで動く。候補 → 門 → 発信の出口 → 通話 を繰り返す）。
- `jobs`：決まった間隔の仕事の表（仕事の一覧は、この表の1か所）。
- `config`：PC ごとの設定。`plugins`：ほかの担当の部品の形。
"""

from __future__ import annotations

from salescall.core.access import Access, Actor
from salescall.core.errors import ErrorReport

ACTOR = Actor("見守り役", "system")


def report(access: Access, tenant_id: object, what: str, error: BaseException, *, line_id: object = None) -> None:
    """見守り役の処理の誤りを、エラーの入口へ渡す（止めずに CL-2 へ修正依頼。errors の「supervisor」）。**誤りの文は渡さない**
    （お客様の情報・接続先が入りうる。渡すのは、どの仕事か と誤りの種類の名前だけ＝控えと同じ決まり）。記録できなくても止めない。"""
    try:
        from salescall.core import watch   # 見守り役の部品を読むときに、watch の読み込みを先にしない
        watch.after_error(access, ACTOR, ErrorReport("supervisor", None, f"{what}：{type(error).__name__}"),
                          tenant_id=tenant_id, line_id=line_id)
    except Exception:  # noqa: BLE001 データベースに届かないときも、見守り役は止めない（PC の記録には残る）
        pass
