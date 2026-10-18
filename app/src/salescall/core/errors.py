"""エラーの分類と処置の表（Q9「このエラーは何で、どうするか」）。持ち主 CL-2。DESIGN.md 9章・Intent.md 10-3。

データベースを使わない部品。**どこで起きたエラーも `classify` が種類に分け、処置は `ACTIONS` の表だけが決める**。
呼び出し元（会話の処理・キーの選択 Q15・回線の部品・通話後の検証・連携）は、表の処置を実行するだけにする
（その場で再試行やキーの切り替えを書かない）。止める・解くの判断は core/watch.py（この表を読む）。

- 種類は、**会話AI（Google）のエラーの一覧 `CATALOG`** で見分ける（2026-10-15 ユーザー：「エラーコード一覧を作っておいたら」）。
  一覧の1行ごとに出どころを持つ：公式（Google の公式の一覧）／実測（私たちが実際に受けた）／報告（ほかの利用者の報告。公式ではない）。
  **自動で種類を決めるのは、公式と実測の行だけ**。報告の行は参考として載せるだけ（種類は決めない＝不明のまま）。
  閉じる番号だけでは決めず、番号と文面（またはステータスの名前）の組で見る（同じ 1007 が「キーの誤り」と「設定の誤り」の両方で
  返った。2026-10-12 の実測）。一覧に無いものは「不明」（推測で種類を決めない）。
- 不明のときは、キーを切り替えない（原因が分からないので、キーを変えても直るとは限らない。切り替えるのは、キーのせいと
  分かったときだけ＝Intent.md 12-3 の切り替えのルール1）。続いたら新しい発信を待ち、直す担当に修正依頼を出す（人は確かめ・承認だけ）。
- 上限（2026-10-17 ユーザー：「本番はできるだけ分制限などの制限を回避し、1日制限や回避できなかった他の制限が来たら次のキー」）：
  1分の上限が近づいたら、キーの選択（Q15）が、きりのいいところ（新しいセッションを開くとき）で次のキーを併せて使う
  （門で待たせない＝2026-10-17 ユーザー：「作業を止めるなって」。DESIGN.md 5-2 の9）。それでも当たったもの・1日の上限は、
  次のキーで続ける（`next_key`）。
- 表の「知らせる」は、知らせの台帳に入れるだけ（管理者・契約者の返事を待たない。2026-10-13 ユーザー：管理者も契約者も介在しない仕組み）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

SOURCES = ("ai", "ear", "line", "storage", "delivery", "recording")   # 会話AI／別の耳（通話後の検証）／回線／保存／連携／
#                                                                       録音（通話の録音が残っていない・壊れている）

@dataclass(frozen=True)
class CatalogEntry:
    """会話AI（Google）のエラーの一覧の1行。"""
    code: int | None       # 閉じる番号（1007 など）か HTTP の番号（429 など）。None＝番号を問わない
    text: str | None       # 文面に含まれる言葉（ステータスの名前を含む）。None＝文面を問わない
    kind: str | None       # 種類（ACTIONS の鍵）。None＝参考だけ（自動では決めない）
    origin: str            # official（Google の公式の一覧）／observed（実測）／reported（ほかの利用者の報告。公式ではない）
    meaning: str           # 日本語の意味と、受けたときの原因
    ref: str               # 出どころ（公式の文書の場所・実測の日付と記録）


_OFFICIAL = "https://ai.google.dev/gemini-api/docs/troubleshooting（2026-10-15 に確かめた公式の表）"
# 上から順に見て、最初に当たった行の種類（番号と文面の組の行を、番号だけ・ステータスだけの行より上に置く）
CATALOG: tuple[CatalogEntry, ...] = (
    # ── 実測した組（番号と文面）
    CatalogEntry(1007, "API key not valid", "key_specific", "observed", "キーが無効（認証の失敗）", "2026-10-12 実測（フェーズ0）"),
    CatalogEntry(1007, "is not supported for this model", "config_error", "observed",
                 "設定の誤り（そのモデルが受け付けない設定。例：考えるレベル）", "2026-10-12 実測（フェーズ0）"),
    CatalogEntry(1008, "has been denied access", "project_denied", "observed",
                 "プロジェクトの利用拒否（ユーザーが管理画面で確かめた理由：1分あたりの入力トークン数の上限）", "2026-10-12 実測（10個中4個のキー）"),
    CatalogEntry(1011, "exceeded your current quota", "project_quota", "observed", "プロジェクトの枠の超過", "2026-10-12 実測（フェーズ0）"),
    CatalogEntry(1011, "Internal error", "session_drop", "observed",
                 "Google の側の内部の誤りで、会話の途中で切られた",
                 "実測（フェーズ0 G-25 2026-10-13・G-36 2026-10-14。PHASE0_VERIFICATION.md）"),
    CatalogEntry(1006, None, "session_drop", "observed",
                 "接続が途中で切れた（閉じる合図の無い切れ方。会話AIでも別の耳でも起きた）",
                 "実測（フェーズ0 G-25・G-38 の会話AI、別の耳。PHASE0_VERIFICATION.md）"),
    # ── 公式の一覧（ステータスの名前。閉じる理由の文面や HTTP の応答に入る）
    CatalogEntry(None, "API key not valid", "key_specific", "official", "キーが無効（400 INVALID_ARGUMENT の1つ）", _OFFICIAL),
    CatalogEntry(None, "PERMISSION_DENIED", "key_specific", "official", "キーに権限が無い・違うキー（403）", _OFFICIAL),
    CatalogEntry(None, "RESOURCE_EXHAUSTED", "project_denied", "official",
                 "利用の上限に当たった（1分あたりの回数・トークン数・1日の回数など。429）。待ってから試す", _OFFICIAL),
    CatalogEntry(None, "INVALID_ARGUMENT", "config_error", "official", "要求の形が違う（設定の誤り。400）", _OFFICIAL),
    CatalogEntry(None, "FAILED_PRECONDITION", "config_error", "official",
                 "無料枠が使えない地域で、支払いを設定していない など（400）", _OFFICIAL),
    CatalogEntry(None, "NOT_FOUND", "config_error", "official", "指定したもの（モデルなど）が無い（404）", _OFFICIAL),
    CatalogEntry(None, "INTERNAL", "ai_server_error", "official", "Google の側の思いがけない誤り（500）。少し待って試す", _OFFICIAL),
    CatalogEntry(None, "UNAVAILABLE", "ai_server_error", "official", "一時的に使えない・混んでいる（503）。間隔を伸ばして試す", _OFFICIAL),
    CatalogEntry(None, "DEADLINE_EXCEEDED", "ai_server_error", "official", "時間内に処理を終えられなかった（504）", _OFFICIAL),
    CatalogEntry(429, None, "project_denied", "official", "利用の上限（429 RESOURCE_EXHAUSTED）", _OFFICIAL),
    CatalogEntry(403, None, "key_specific", "official", "キーに権限が無い（403 PERMISSION_DENIED）", _OFFICIAL),
    CatalogEntry(404, None, "config_error", "official", "指定したものが無い（404 NOT_FOUND）", _OFFICIAL),
    CatalogEntry(500, None, "ai_server_error", "official", "Google の側の誤り（500 INTERNAL）", _OFFICIAL),
    CatalogEntry(503, None, "ai_server_error", "official", "一時的に使えない（503 UNAVAILABLE）", _OFFICIAL),
    CatalogEntry(504, None, "ai_server_error", "official", "時間切れ（504 DEADLINE_EXCEEDED）", _OFFICIAL),
    # ── ほかの利用者の報告（公式ではない。参考だけ：自動では種類を決めない＝不明として扱い、続いたら止める）
    CatalogEntry(1008, "Operation is not implemented, or supported, or enabled", None, "reported",
                 "道具の返事を待つ間に音声を送り続けると切られる、という報告", "https://discuss.ai.google.dev/t/114644"),
    CatalogEntry(1007, None, None, "reported", "送った内容の形が違う（接続の始めの設定の誤り）という報告",
                 "https://discuss.ai.google.dev/t/96680"),
)


@dataclass(frozen=True)
class ErrorReport:
    """起きたエラー（呼び出し元が、分かったことをそのまま渡す。推測で埋めない）。"""
    source: str                    # SOURCES のどれか
    code: int | None               # 閉じる番号など（無ければ None）
    message: str | None            # 文面（お客様の発話・番号を入れない。エラーの文面だけ）
    unreachable: bool = False      # つながらなかった（接続を開けない・応答が無い・時間切れ）と、呼び出し元が確かに言える


@dataclass(frozen=True)
class Action:
    """処置（表の1行）。"""
    key: str                       # キー：keep（変えない）／mark_key_unusable（そのキーを使わない印）／
                                   #       next_key（そのプロジェクトを休ませ、次のキーで続ける。同じプロジェクトと分かっているキーは
                                   #       飛ばす。2026-10-17 ユーザー：「1日制限や回避できなかった他の制限が来たら次のキー」）
    retry: str                     # やり直し：none／backoff（間隔を伸ばしながら上限まで）／reconnect_once（6-7）／
                                   #           project_backoff（そのプロジェクトを待ってから1回だけ。設定 stop.ai_rate_limit.backoff）
    stop_cause: str | None         # 止めるときの原因（DESIGN.md 9-1 の名前）。None＝止めない
    stop_scope: str | None         # 止める範囲：tenant（その契約者。会話AIの枠は契約者ごと＝キー・プロジェクトは契約者のもの。
                                   #   2026-10-16 ユーザー）／line（その回線）
    stop_after: str | None         # 何回続いたら止めるか（設定の名前。None＝1回で止める）
    notify: bool                   # 契約者に知らせる（知らせの台帳に入れるだけ。管理者には、どの種類も知らせる）
    meaning: str                   # 日本語（画面・知らせに出す）
    fixer: str | None = None       # 根源的（設定の誤り・原因の分からない誤り・録音が残らない など、待っても直らない）なら、直す担当
    #                                （CL-1＝会話・別の耳／CL-2＝業務の土台・保存・回線の部品・録音（電話接続の担当が決まるまで））。止めずに、その担当へ修正依頼を
    #                                出す（core/fix_requests.py。人は確かめ・承認だけ＝2026-10-17 ユーザー：「止めずに CL1 か CL2 に
    #                                修正依頼を出さないといけないだろ。人はそのプロセスをチェックもしくは承認するだけだよ」）。
    #                                None＝一時的（混雑・つながらない・時間切れ）＝間隔を伸ばして待ち、直ったら自動で続ける
    #                                （2026-10-16 ユーザー：「一時的なものなのか、根源的なものなのかで対応が違う」）
    lasting_fixer: str | None = None  # 一時的なもので、止めずに待つもの：LASTING 以上続いたら、待っても直らないとみなして、この担当へ
    #                                修正依頼を出す（2026-10-18 の見直し：前は何日続いても修正依頼にならず、その間お客様の行が待ち続けた）

    @property
    def fundamental(self) -> bool:
        return self.fixer is not None


LASTING = timedelta(hours=1)   # 一時的なものが「続いている」とみなす長さ（1時間より長くあかずに、1時間以上起き続けている）


# 知らせに出す短い名前（注意は短く。詳しい意味は Action.meaning＝画面の「止まった理由」・i マークで。2026-10-17 ユーザー）
LABELS: dict[str, str] = {
    "key_specific": "キーが使えません", "project_denied": "会話AIの1分の上限に当たりました",
    "config_error": "会話AIの設定の誤りです", "project_quota": "会話AIの利用枠を超えました",
    "ai_unreachable": "会話AIにつながりません", "ear_unreachable": "通話後の確かめ（別の耳）につながりません",
    "ear_error": "通話後の確かめ（別の耳）の誤りです",
    "ai_server_error": "会話AIの側の誤り・混雑です", "session_drop": "通話中に会話AIが切れました",
    "line": "回線の異常です", "storage": "保存に失敗しました", "delivery": "結果の書き戻しに失敗しました",
    "recording": "通話の録音が残っていません",
    "unknown_ai": "原因の分からない会話AIの誤りです", "unknown_line": "原因の分からない回線の誤りです",
}

ACTIONS: dict[str, Action] = {
    "key_specific": Action("mark_key_unusable", "none", None, None, None, False,
                           "キー固有（認証失敗・キーの無効化）：そのキーを使わない印を付け、次のセッションから別のキー"),
    "project_denied": Action("next_key", "project_backoff", None, None, None, True,
                             "プロジェクトの利用拒否（1分あたりの上限）：そのプロジェクトをしばらく休ませ"
                             "（待ちは 10秒→30秒→2分→10分→30分）、次のキーで続ける（2026-10-17 ユーザー：門では待たせない）"),
    "config_error": Action("keep", "none", "ai_connection", "tenant", None, True,
                           "設定の誤り：キーを切り替えず、新しい発信を待つ（設定を直すまで、どのキーでも同じ結果＝会話AIにつなげない"
                           "ので、かけられない）。CL-1 に修正依頼を出す", fixer="CL-1"),
    "project_quota": Action("next_key", "none", None, None, None, True,
                            "プロジェクトの枠の超過（1日の上限など）：次のキーで続ける（同じプロジェクトと分かっているキーは飛ばす。"
                            "使えるキーが1つも無ければ、キーの選択が「会話AIにつながらない」（ai_unreachable）として知らせる）"),
    "ai_unreachable": Action("keep", "backoff", "ai_connection", "tenant", "stop.ai.stop_after_failures", False,
                             "会話AIにつながらない（一時的。使えるキーが1つも無いときも、これ）：間隔を伸ばしてやり直す。続いたら、"
                             "つながるまで、その契約者の新しい発信を待ち、直ったら自動で続ける"),
    "ear_unreachable": Action("keep", "backoff", None, None, None, True,
                              "別の耳（通話後の検証）につながらない（一時的）：発信は止めない。検証は順番待ちのまま、キーを併用して"
                              "やり直す（つながるまで、その行は締めずに待つ＝同じお客様には、断られたかが決まるまでかけない）。"
                              "管理者に知らせる（2026-10-17：止めるのはお客様に影響が出るときだけ。検証が遅れても、門が"
                              "その行と同じお客様を守るので、お客様には影響が出ない）。1時間以上続いたら CL-1 に修正依頼を出す",
                              lasting_fixer="CL-1"),
    "ear_error": Action("keep", "none", None, None, None, True,
                        "別の耳（通話後の検証）の設定の誤り・原因の分からない誤り：発信は止めない（止めるのはお客様に影響が出るときだけ。"
                        "検証が済むまで、門がその行と同じお客様にかけないので、お客様には影響が出ない）。CL-1 に修正依頼を出す。"
                        "検証は順番待ちのまま、直ったらやり直す", fixer="CL-1"),
    "ai_server_error": Action("keep", "backoff", "ai_connection", "tenant", "stop.ai.stop_after_failures", False,
                              "Google の側の誤り・混雑・時間切れ（公式の一覧の 500・503・504。一時的）：間隔を伸ばしてやり直す。続いたら、直るまで新しい発信を待ち、直ったら自動で続ける"),
    "session_drop": Action("keep", "reconnect_once", None, None, None, False,
                           "通話中のセッションの切断：会話AIは1回だけつなぎ直し、駄目ならおわびして終える（6-7。その行は、"
                           "あとでかけ直す）。別の耳は1回だけ聞き直し、駄目なら検証は順番待ちのまま（あとでやり直す）。止める判断は、品質の段の"
                           "「技術的な失敗の割合」で見る（切断は会話に入ったあとに起きるので、続けた回数では数えられない）"),
    "line": Action("keep", "none", "line_failure", "line", "stop.line.stop_after_failures", False,
                   "回線（発信失敗・回線の上限・端末やアプリの異常。一時的）：続いたら、その回線の発信を待ち、直ったら自動で続ける"),
    "storage": Action("keep", "none", None, None, None, True,
                      "保存の失敗：止めない。CL-2 に修正依頼を出し、ほかの行は続ける（2026-10-16 ユーザー：「理由を記載と"
                      "あなたへの修正依頼をして、次の行に進んだらいい」）。保存できなかった通話は締めが済まないまま残るので、門が"
                      "その行と同じお客様にはかけない（断られたかが決まっていない）。直れば、締めの処理が締め直す。"
                      "データベースそのものに届かないときは、門が判断できないので、発信は自然に待つ", fixer="CL-2"),
    "recording": Action("keep", "none", None, None, None, False,
                        "通話の録音が残っていない・壊れている（電話接続の録音の不具合）：止めない。CL-2 に修正依頼を出し、ほかの行は"
                        "続ける（2026-10-17 ユーザー：「止めずに CL1 か CL2 に修正依頼を出さないといけないだろ」）。その通話の"
                        "確かめは待ちのまま（門が、その行と同じお客様にはかけない）、録音が見つかればやり直す。⏳ 電話接続の担当が"
                        "決まったら、その担当へ", fixer="CL-2"),
    "delivery": Action("keep", "backoff", None, None, None, False,
                       "連携（書き戻し）の失敗：連携だけ再送する（通話の結果は失わない。4-5）。1時間以上続いたら CL-3 に修正依頼を出す",
                       lasting_fixer="CL-3"),
    "unknown_ai": Action("keep", "none", "ai_connection", "tenant", "stop.ai.stop_after_failures", True,
                         "不明（会話AI）：キーを切り替えない。続いたら（会話AIで話せない）新しい発信を待つ。CL-1 に修正依頼を出す",
                         fixer="CL-1"),
    "unknown_line": Action("keep", "none", "line_failure", "line", "stop.line.stop_after_failures", True,
                           "不明（回線）：続いたら（その回線でかけられない）、その回線の発信を待つ。CL-2 に修正依頼を出す",
                           fixer="CL-2"),
}

# 止める原因ごとに、続けて数えるエラーの種類（同じ原因で止める種類を合わせて数える。1か所）
COUNTED_FOR: dict[str, tuple[str, ...]] = {
    cause: tuple(k for k, a in ACTIONS.items() if a.stop_cause == cause)
    for cause in {a.stop_cause for a in ACTIONS.values() if a.stop_cause is not None}
}

# 止める種類ごとに、「直った」とみなす証拠（続いた回数を、この証拠の時刻から数え直す。1か所。データベースの
# salescall.consecutive_errors がこの名前で読む）。2026-10-16 見直し：前は会話AIと別の耳を「どこかの通話が会話に入った」で
# まとめて数え直していたので、別の耳だけつながらないとき、ほかの通話が会話に入るたびに数え直し、止まらなかった。
#   in_conversation＝どこかの通話が会話に入った（会話AIが動いた）／verified＝どこかの通話の検証が済んだ（別の耳が動いた）／
#   ringing＝その回線でお客様を呼び出せた（回線が動いた）／none＝証拠を持たない（1回で止める種類）
EVIDENCE: dict[str, str] = {
    "config_error": "in_conversation",
    "ai_unreachable": "in_conversation",
    "ai_server_error": "in_conversation",
    "unknown_ai": "in_conversation",
    "line": "ringing",
    "unknown_line": "ringing",
}


def counted_with(kind: str) -> tuple[str, ...]:
    """その種類で止めるか決めるとき、合わせて数える種類（同じ止める原因で、同じ証拠で数え直す種類。1か所）。"""
    a = ACTIONS[kind]
    return tuple(k for k in COUNTED_FOR[a.stop_cause] if EVIDENCE[k] == EVIDENCE[kind])


def lookup(e: ErrorReport) -> CatalogEntry | None:
    """一覧で、そのエラーに当たる最初の行（無ければ None）。報告の行にも当たる（意味を画面に出すため）。"""
    for entry in CATALOG:
        if entry.code is not None and e.code != entry.code:
            continue
        if entry.text is not None and (e.message is None or entry.text not in e.message):
            continue
        return entry
    return None


def classify(e: ErrorReport) -> str:
    """エラーの種類（ACTIONS の鍵）。会話AI・別の耳は一覧の公式と実測の行だけで決める。ほかの出どころは出どころで決まる。"""
    if e.source not in SOURCES:
        raise ValueError(f"出どころは {SOURCES} のどれかです（{e.source!r}）")
    if e.source in ("ai", "ear"):
        entry = lookup(e)
        # 切断は、開いたセッションが途中で切れたこと。開けなかった（つながらなかった）なら、つながらないの種類にする
        kind = (entry.kind if entry is not None and entry.kind is not None
                and not (entry.kind == "session_drop" and e.unreachable) else None)
        if kind is None:
            kind = "ai_unreachable" if e.unreachable else "unknown_ai"
        if e.source == "ear" and ACTIONS[kind].stop_cause is not None:
            # 別の耳のエラーで、発信は止めない（2026-10-17：止めるのはお客様に影響が出るときだけ＝検証が済むまで門が同じお客様を
            # 守る）。一覧の種類のうち止める種類は、一時的なら「つながらない」、根源的（設定の誤り・不明）なら「別の耳の誤り」にする
            # （2026-10-17 の計画書との突き合わせ：前は別の耳の 503 が3回・1007 が1回で、その契約者の発信を止めていた）
            kind = "ear_error" if ACTIONS[kind].fundamental else "ear_unreachable"
        return kind
    if e.source == "line":
        return "line"
    if e.source == "storage":
        return "storage"
    if e.source == "recording":
        return "recording"
    return "delivery"
