"""エラーの分類と処置の表（Q9「このエラーは何で、どうするか」）。持ち主 CL-2。DESIGN.md 9章・Intent.md 10-3。

データベースを使わない部品。**どこで起きたエラーも `classify` が種類に分け、処置は `ACTIONS` の表だけが決める**。
呼び出し元（会話の処理・キーの選択 Q15・回線の部品・通話後の検証・連携）は、表の処置を実行するだけにする
（その場で再試行やキーの切り替えを書かない）。止める・解くの判断は core/watch.py（この表を読む）。

- 種類は、閉じる番号ではなく「番号と文面の組」で見分ける（同じ 1007 が「キーの誤り」と「設定の誤り」の両方で返った。
  2026-10-12 の実測）。**入れるのは実測した組だけ。見たことのない組は「不明」**（推測で種類を決めない）。
- 不明のときは、キーを切り替えない（原因が分からないまま回すと、無料枠の回避と同じ形になる）。続いたら止める。
- 表の「知らせる」は、知らせの台帳に入れるだけ（人の返事を待たない。2026-10-13 ユーザー：人のいない仕組み）。
"""

from __future__ import annotations

from dataclasses import dataclass

SOURCES = ("ai", "ear", "line", "storage", "delivery")   # 会話AI／別の耳（通話後の検証）／回線／保存／連携

# 実測した「番号と文面の組」（会話AI・別の耳の閉じる番号）。足すのは実測したときだけ（日付と記録を添える）。
_OBSERVED: tuple[tuple[int, str, str], ...] = (
    (1007, "API key not valid", "key_specific"),            # キーの無効化（実測）
    (1008, "has been denied access", "project_denied"),      # 1分あたりの入力トークン数の上限（2026-10-12 実測。9-1）
    (1007, "is not supported for this model", "config_error"),   # 設定の誤り（考えるレベルの設定。実測）
    (1011, "exceeded your current quota", "project_quota"),  # プロジェクトの枠の超過（実測）
)


@dataclass(frozen=True)
class ErrorReport:
    """起きたエラー（呼び出し元が、分かったことをそのまま渡す。推測で埋めない）。"""
    source: str                    # SOURCES のどれか
    code: int | None               # 閉じる番号など（無ければ None）
    message: str | None            # 文面（相手の発話・番号を入れない。エラーの文面だけ）
    unreachable: bool = False      # つながらなかった（接続を開けない・応答が無い・時間切れ）と、呼び出し元が確かに言える


@dataclass(frozen=True)
class Action:
    """処置（表の1行）。"""
    key: str                       # キー：keep（変えない）／mark_key_unusable（そのキーを使わない印）／
                                   #       other_project_only（別のプロジェクトと分かっているキーだけ）
    retry: str                     # やり直し：none／backoff（間隔を伸ばしながら上限まで）／reconnect_once（6-7）／
                                   #           project_backoff（そのプロジェクトを待ってから1回だけ。設定 stop.ai_rate_limit.backoff）
    stop_cause: str | None         # 止めるときの原因（DESIGN.md 9-1 の名前）。None＝止めない
    stop_scope: str | None         # 止める範囲：system／line
    stop_after: str | None         # 何回続いたら止めるか（設定の名前。None＝1回で止める）
    notify: bool                   # 運営者に知らせる（知らせの台帳に入れるだけ）
    meaning: str                   # 日本語（画面・知らせに出す）


ACTIONS: dict[str, Action] = {
    "key_specific": Action("mark_key_unusable", "none", None, None, None, False,
                           "キー固有（認証失敗・キーの無効化）：そのキーを使わない印を付け、次のセッションから別のキー"),
    "project_denied": Action("other_project_only", "project_backoff", None, None, None, True,
                             "プロジェクトの利用拒否（1分あたりの上限）：そのプロジェクトをしばらく使わない（待ちは 10秒→30秒→2分→10分→30分）"),
    "config_error": Action("keep", "none", "ai_connection", "system", None, True,
                           "設定の誤り：キーを切り替えず、新しい発信を止める（設定を直すまで、どのキーでも同じ結果）"),
    "project_quota": Action("other_project_only", "none", None, None, None, True,
                            "プロジェクトの枠の超過：同じプロジェクトのキーへは切り替えない（無ければ門が通さない）"),
    "ai_unreachable": Action("keep", "backoff", "ai_connection", "system", "stop.ai.stop_after_failures", False,
                             "会話AIにつながらない：間隔を伸ばしてやり直す。続いたら全体を止める"),
    "ear_unreachable": Action("keep", "backoff", "ai_connection", "system", "stop.ai.stop_after_failures", False,
                              "別の耳（通話後の検証）につながらない：続いたら全体を止める（検証できないまま、かけた行が"
                              "「検証できなかった」で終わり続けるのを防ぐ）"),
    "session_drop": Action("keep", "reconnect_once", None, None, None, False,
                           "通話中のセッションの切断：1回だけつなぎ直し、駄目ならおわびして終える（6-7）"),
    "line": Action("keep", "none", "line_failure", "line", "stop.line.stop_after_failures", False,
                   "回線（発信失敗・回線の上限・端末やアプリの異常）：続いたら、その回線を止める"),
    "storage": Action("keep", "none", "storage_failure", "system", None, True,
                      "保存の失敗：新しい発信を止める（記録できない状態でかけ続けない）"),
    "delivery": Action("keep", "backoff", None, None, None, False,
                       "連携（書き戻し）の失敗：連携だけ再送する（通話の結果は失わない。4-5）"),
    "unknown_ai": Action("keep", "none", "ai_connection", "system", "stop.ai.stop_after_failures", True,
                         "不明（会話AI）：キーを切り替えない。続いたら全体を止める"),
    "unknown_line": Action("keep", "none", "line_failure", "line", "stop.line.stop_after_failures", True,
                           "不明（回線）：続いたら、その回線を止める"),
    "unknown_other": Action("keep", "none", None, None, None, True, "不明：知らせる（止める範囲を決められない）"),
}

# 止める原因ごとに、続けて数えるエラーの種類（同じ原因で止める種類を合わせて数える。1か所）
COUNTED_FOR: dict[str, tuple[str, ...]] = {
    cause: tuple(k for k, a in ACTIONS.items() if a.stop_cause == cause)
    for cause in {a.stop_cause for a in ACTIONS.values() if a.stop_cause is not None}
}


def classify(e: ErrorReport) -> str:
    """エラーの種類（ACTIONS の鍵）。実測した組と、出どころで決まるものだけ。ほかは不明。"""
    if e.source not in SOURCES:
        raise ValueError(f"出どころは {SOURCES} のどれかです（{e.source!r}）")
    if e.source in ("ai", "ear"):
        for code, text, kind in _OBSERVED:
            if e.code == code and e.message is not None and text in e.message:
                return kind
        if e.unreachable:
            return "ai_unreachable" if e.source == "ai" else "ear_unreachable"
        return "unknown_ai" if e.source == "ai" else "unknown_other"
    if e.source == "line":
        return "line"
    if e.source == "storage":
        return "storage"
    return "delivery"
