"""数える部品の1か所（docs/DASHBOARD.md 6-5）。ホーム・いつも見える帯・依頼者の概要は、ここだけを読む。

数えるだけ（割合は計算しない）。何を「済み」「判定待ち」とするかは、答える場所（次の予定の最新＝見方 current_next_actions、
業務の結果の最新＝見方 latest_task_results、発信まで進んだか＝salescall.is_dialed）の答えを数える。ここで決め直さない。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import approval, business_settings, metrics
from salescall.core.access import Tx
from salescall.output import words
from salescall.web.context import Ctx

_OPEN = ("SELECT event_id, scope::text AS scope, cause::text AS cause, reason, at, client_id, business_id, line_id"
         " FROM salescall.open_stop_events")


def open_stops(ctx: Ctx) -> list[dict[str, Any]]:
    """その契約者で、まだ解かれていない停止のすべて（新しい順）。

    データベースの決まりで、依頼者・業務の範囲の停止は、その依頼者の取引でしか見えない（0007 の stop_scope）。⇒ 契約者の取引で見える
    停止（全体・契約者・回線の範囲）と、依頼者ごとの取引で見える停止を合わせ、同じ停止は1つにする。⛔ 2026-11-18 の見本の画面で、
    契約者の取引だけを読み、依頼者の範囲の停止を数え漏らしていたのを見つけた。各行の `client_id`＝開く・解くときに使う取引の依頼者
    （依頼者・業務の範囲だけ。ほかは None＝契約者の取引）。"""
    found: dict[int, dict[str, Any]] = {r["event_id"]: {**r, "client_id": None} for r in ctx.tenant(lambda tx: tx.all(_OPEN))}
    for c in ctx.clients():
        for r in ctx.client(c["client_id"], lambda tx: tx.all(_OPEN + " WHERE scope IN ('client', 'business')"), read_only=True):
            found[r["event_id"]] = {**r, "client_name": c["name"]}
    return sorted(found.values(), key=lambda r: (r["at"], r["event_id"]), reverse=True)



def stops_for(stops: list[dict[str, Any]], client_id: UUID, business_id: UUID | None = None) -> list[dict[str, Any]]:
    """`open_stops` のうち、その依頼者（業務を渡せば、その業務）の新しい電話に効く停止（依頼者・業務の画面の赤い帯）。
    効く範囲の決まりは1か所：全体・契約者の範囲＝どの依頼者にも効く／依頼者の範囲＝その依頼者／業務の範囲＝その業務（依頼者の
    概要では、その依頼者のどの業務の停止も出す）。回線の範囲は、その回線を使う行だけに効くので、ここには出さない（「止まっているもの」に出る）。"""
    def hits(s: dict[str, Any]) -> bool:
        return SCOPE_HOLDS[s["scope"]](s, client_id, business_id)   # 知らない範囲は KeyError（黙って出さない形にしない）
    return [s for s in stops if hits(s)]


# 停止の範囲ごとに、その依頼者・業務に効くか（stops.SCOPE_NAMES のすべての範囲を書く。試験が突き合わせる）
SCOPE_HOLDS: dict[str, Any] = {
    "system": lambda s, c, b: True,
    "tenant": lambda s, c, b: True,
    "client": lambda s, c, b: s["client_id"] == c,
    "business": lambda s, c, b: s["client_id"] == c and (b is None or s["business_id"] == b),
    "line": lambda s, c, b: False,   # 回線の範囲は、その回線を使う行だけに効く（「止まっているもの」に出る）
}


def client_today(tx: Tx, business_id: UUID | None = None) -> dict[str, int]:
    """依頼者の取引で、今日（日本時間）の電話の数と、行の次の予定ごとの数。business_id を渡せば、その業務の行だけ（業務のタブ）。
    今日の数（かけた dialed・電話に出た answered＝留守番電話・自動音声も入る・お客様と話した talked・目的のお客様と話せた reached）は
    core の `metrics.day` の1か所（品質の段・業務の指標と同じ数え方。CL-2 4-455。⛔ 前は、かけた数をここで別の式で数えていた）。"""
    only = "" if business_id is None else " AND t.run_id IN (SELECT run_id FROM salescall.runs WHERE business_id = %s)"
    extra = () if business_id is None else (business_id,)
    kinds = {r["kind"]: r["n"] for r in tx.all(
        "SELECT coalesce(n.kind::text, 'none') AS kind, count(*) AS n FROM salescall.tasks t"
        " LEFT JOIN salescall.current_next_actions n ON n.task_id = t.task_id WHERE true" + only + " GROUP BY 1", extra)}
    return {**metrics.day(tx, tx.today(), business_id=business_id), "call": kinds.get("call", 0), "hold": kinds.get("hold", 0),
            "end": kinds.get("end", 0), "none": kinds.get("none", 0)}


def required_answers(tx: Tx, business_id: UUID) -> list[dict[str, Any]]:
    """業務の必須の質問ごとの「取れた」「-」「✕」などの数（計画書 5-2「必須項目の『取れた』『-』『✕』の数（別々に）」）。数えるのは
    core の `metrics.required_items` の1か所（行ごとのいちばん新しい判定に使った答え）。期間は、これまでのすべての電話。見出しは、
    いちばん新しい版の質問の「聞くこと」（依頼者へ渡すファイルの見出しと同じ1か所＝result_rows.question_headers の元）。"""
    from datetime import datetime, timedelta, timezone
    got = metrics.required_items(tx, business_id, since=datetime(2000, 1, 1, tzinfo=timezone.utc),
                                 until=datetime.now(timezone.utc) + timedelta(days=1))
    names: dict[str, str] = {}
    v = tx.one("SELECT version_id FROM salescall.business_versions WHERE business_id = %s ORDER BY version_no DESC LIMIT 1", (business_id,))
    if v is not None:
        try:
            names = {q.id: (q.ask_about or q.text) for q in business_settings.load(tx, v["version_id"]).questions}
        except LookupError:
            names = {}
    return [{"question": names.get(q, q), "tally": t} for q, t in got.items()]


# ── 業務の成果（計画書 5-2 の「業務成果」と「門で止めた件数（理由別）」。docs/DASHBOARD.md 5-7・5-8）──────────────────
# 確定していない結果の数（確かめ待ち・決定待ち・判定できない）は core の approval.unconfirmed_counts の1か所（受け箱の「結果の確かめ」の
# approval.results_to_check と同じ式。CL-2 4-439 の 29。⛔ 前は、同じ式をここに書き写していた）。
_IN_BUSINESS = "(%s::uuid IS NULL OR t.run_id IN (SELECT run_id FROM salescall.runs WHERE business_id = %s))"


def business_figures(tx: Tx, business_id: UUID | None = None) -> dict[str, Any]:
    """依頼者の取引で、業務（None なら依頼者のすべての業務）の成果の数。数えるだけ（決め直さない）：
    - results：行ごとの最新の結果の状態 → 行の数（結果の無い行は open＝まだ結果なし。正本は latest_task_results）。
    - unconfirmed：確定していない結果を3つに分けた数（確かめ待ち＝検証モードで契約者の確かめを待つ・決定待ち＝取り消せない決定や
      あとの通話を待つ・判定できない）。3つは重ならない。
    - denied_today：今日（日本時間）門が止め始めた行の数（理由ごと。門は理由が変わったときだけ記録する＝その日に新しく止めた数）。"""
    b = (business_id, business_id)
    results = {r["state"]: r["n"] for r in tx.all(
        "SELECT coalesce(l.business_state::text, 'open') AS state, count(*) AS n FROM salescall.tasks t"
        " LEFT JOIN salescall.latest_task_results l ON l.task_id = t.task_id WHERE " + _IN_BUSINESS + " GROUP BY 1", b)}
    u = approval.unconfirmed_counts(tx, business_id=business_id)
    denied = tx.all("SELECT d.reason, count(DISTINCT d.task_id) AS n FROM salescall.gate_denials d JOIN salescall.tasks t ON t.task_id = d.task_id"
                    " WHERE " + _IN_BUSINESS + " AND salescall.jst(d.denied_at)::date = %s GROUP BY 1 ORDER BY 2 DESC, 1", (*b, tx.today()))
    return {"results": results, "unconfirmed": dict(u), "denied_today": [(r["reason"], r["n"]) for r in denied]}


# ── はじめかた（業務のタブ。docs/DASHBOARD.md 5-8）：まだ動き出していない業務で、次にすることを並べる。どの手順も「データにあるか」を
#    読むだけ（版・実施回・行・回線・録音の答え）。かけられるかは門だけが決める（ここでは言い切らない）。
def _script_ready(tx: Tx, business_id: UUID) -> bool:
    """業務に、中身を読める電話の台本の版があるか（版があるだけでは済みにしない＝中身を置いていない版もある。読むのは core の
    business_settings.load の1か所＝通話と同じ確かめ）。"""
    v = tx.one("SELECT version_id FROM salescall.business_versions WHERE business_id = %s ORDER BY version_no DESC LIMIT 1", (business_id,))
    if v is None:
        return False
    try:
        business_settings.load(tx, v["version_id"])
        return True
    except (LookupError, business_settings.SettingsInvalid):
        return False


def setup_steps(tx: Tx, client_id: UUID, business_id: UUID, recording_needs: list[Any]) -> list[dict[str, Any]]:
    """手順の一覧（name・done・how＝どこでするか・link）。recording_needs＝業務のタブが core の recordings.version_needs から作った
    録音の要るもの（同じ答えを2回読まない）。"""
    f = tx.one(
        "SELECT (SELECT count(*) FROM salescall.business_versions WHERE business_id = %s) AS versions,"
        " (SELECT count(*) FROM salescall.runs WHERE business_id = %s) AS runs,"
        " (SELECT r.run_id FROM salescall.runs r WHERE r.business_id = %s ORDER BY r.created_at DESC, r.run_id LIMIT 1) AS last_run,"
        " (SELECT count(*) FROM salescall.tasks t JOIN salescall.runs r ON r.run_id = t.run_id WHERE r.business_id = %s) AS rows,"
        " (SELECT count(*) FROM salescall.lines l WHERE l.retired_at IS NULL AND (l.client_id IS NULL OR l.client_id = %s)) AS lines",
        (business_id, business_id, business_id, business_id, client_id))
    base = f"/c/{client_id}/b/{business_id}"
    script = _script_ready(tx, business_id)
    return [
        {"name": "電話の台本を入れる", "done": script, "how": words.SCRIPT_NOT_READY, "link": ""},
        {"name": "お客様リストを作る", "done": f["runs"] > 0, "how": "下の「＋ お客様リストを作る」から（電話の台本の版を選びます）", "link": ""},
        {"name": "お客様を取り込む", "done": f["rows"] > 0, "how": "お客様リストの画面の「お客様を取り込む」から",
         "link": f"{base}/runs/{f['last_run']}/import" if f["last_run"] else ""},
        {"name": "回線を用意する（この依頼者で使える回線）", "done": f["lines"] > 0, "how": "「回線と PC」で足すか、使い先を決めます",
         "link": "/t/lines"},
        {"name": "録音をそろえる", "done": script and not recording_needs, "how": "録音の要るものは、この画面の上の警告に出ます",
         "link": ""},
    ]

