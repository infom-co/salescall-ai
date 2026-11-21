"""数える部品の1か所（docs/DASHBOARD.md 6-5）。ホーム・いつも見える帯・依頼者の概要は、ここだけを読む。

数えるだけ（割合は計算しない）。何を「済み」「判定待ち」とするかは、答える場所（次の予定の最新＝見方 current_next_actions、
業務の結果の最新＝見方 latest_task_results、発信まで進んだか＝salescall.is_dialed）の答えを数える。ここで決め直さない。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from salescall.core import agents, approval, business_settings, gate, health, health_rules, metrics, reward, ring_test, stops, usage
from salescall.core.access import Tx
from salescall.output import notices, words
from salescall.web.context import Ctx

_OPEN = ("SELECT event_id, scope::text AS scope, cause::text AS cause, reason, at, client_id, business_id, line_id"
         " FROM salescall.open_stop_events")


def stopped_clients(stops: list[dict[str, Any]]) -> set[str]:
    """依頼者の範囲（依頼者・業務）の停止がある依頼者（タブの印とホームの依頼者ごとの表が同じ答えを使う。契約者・全体の範囲の停止は
    帯とホームの上だけに出す＝全部の依頼者に付けない）。stops＝open_stops の答え。"""
    return {str(r["client_id"]) for r in stops if r.get("client_id")}


def open_stops(ctx: Ctx) -> list[dict[str, Any]]:
    """その契約者で、まだ解かれていない停止のすべて（新しい順）。

    データベースの決まりで、依頼者・業務の範囲の停止は、その依頼者の取引でしか見えない（0007 の stop_scope）。⇒ 契約者の取引で見える
    停止（全体・契約者・回線の範囲）と、依頼者ごとの取引で見える停止を合わせ、同じ停止は1つにする。⛔ 2026-11-18 の見本の画面で、
    契約者の取引だけを読み、依頼者の範囲の停止を数え漏らしていたのを見つけた。各行の `client_id`＝開く・解くときに使う取引の依頼者
    （依頼者・業務の範囲だけ。ほかは None＝契約者の取引）。"""
    found: dict[int, dict[str, Any]] = {}
    def tenant_side(tx: Tx) -> list[Any]:   # 契約者の取引で見える停止と、依頼者・業務の範囲の停止の一覧（core の stops.client_stops＝CL-2 4-452）
        return [tx.all(_OPEN), stops.client_stops(tx)]
    own, scoped = ctx.tenant(tenant_side)
    found.update({r["event_id"]: {**r, "client_id": None} for r in own})
    # 依頼者・業務の範囲の停止の理由の文は、その依頼者の取引でしか読めないので、停止のある依頼者だけを開く（前は、すべての依頼者を開いていた）
    names = {c["client_id"]: c["name"] for c in ctx.clients()}
    by_client: dict[Any, list[int]] = {}
    for r in scoped:
        by_client.setdefault(r["client_id"], []).append(r["event_id"])
    for cid, ids in by_client.items():
        for r in ctx.client(cid, lambda tx, ids=ids: tx.all(_OPEN + " WHERE event_id = ANY(%s)", (ids,)), read_only=True):
            found[r["event_id"]] = {**r, "client_name": names.get(cid, "")}
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


def client_home(tx: Tx) -> dict[str, Any]:
    """ホームの依頼者ごとの数（依頼者の取引）：今日の電話と行の予定（`client_today`）・今月の費用と報酬（core の reward.month の1か所）・
    赤字のお知らせ中か（core の usage.deficit_notice の答え＝画面で割合を計算し直さない）。"""
    m = reward.month(tx)
    d = usage.deficit_notice(tx)
    return {**client_today(tx), "cost": m["cost"], "revenue": m["revenue"], "deficit": d is not None and not d["confirmed"]}


def tenant_system(tx: Tx) -> dict[str, Any]:
    """ホームの「回線と PC」（契約者の取引）：使っている回線の数・休んでいる回線の数（段は core の health.line_of の1か所、休みかは
    health_rules.RESTS）・ログインを外していない PC の数と、いちばん新しい合図の時刻（core の agents.pcs。止まったかは core の
    agents.watch_down が知らせで決める＝ここでは決めない）。"""
    lines = tx.all("SELECT line_id FROM salescall.lines WHERE retired_at IS NULL")
    resting = sum(1 for ln in lines if health.line_of(tx, ln["line_id"]).stage in health_rules.RESTS)
    pcs = [p for p in agents.pcs(tx) if p["revoked_at"] is None]
    seen = [p["last_seen_at"] for p in pcs if p["last_seen_at"]]
    return {"lines": len(lines), "resting": resting, "pcs": len(pcs), "last_seen": max(seen) if seen else None}


NOTIFY_KEYS = tuple(notices.DESTINATIONS[False].values())   # 契約者あての知らせの受け先の設定（正本は output/notices の1か所）


def readiness(tx: Tx) -> list[dict[str, Any]]:
    """電話をかける・知らせを届けるための土台の抜け（契約者の取引。答える1か所＝ホームの「いま見ること」・気をつけること・回線と PC の画面が
    同じものを出す。2026-11-21 の見直し：PC が0台で電話がかからないのに、判断がホームの1つの札の中にしか無く、上の段は「何もない」と言っていた）。
    kind＝stop（このままでは電話がかからない）／wait（電話はかかるが、止まったことなどが届かない）。keys＝決めれば直る設定の名前
    （設定の画面が、その行に「決めてください」を出す。同じ答えを使う）。PC が止まったか（合図が途切れたか）は
    core の agents.watch_down が知らせで決める（ここでは決め直さない）。"""
    sysinfo = tenant_system(tx)
    out: list[dict[str, str]] = []
    if not sysinfo["lines"]:
        out.append({"kind": "stop", "title": "使っている回線がありません", "how": "回線を足すと、電話をかけられます。", "link": "/t/lines"})
    elif not sysinfo["pcs"]:
        out.append({"kind": "stop", "title": "電話をつなぐ PC がありません", "how": "PC が無いと電話はかかりません。PC で「PC のログイン」をしてください。",
                    "link": "/t/lines"})
    elif sysinfo["resting"] >= sysinfo["lines"]:
        out.append({"kind": "stop", "title": "すべての回線が休んでいます", "how": "休んでいる理由を確かめてください（決まった確かめが通れば、自動で戻ります）。",
                    "link": "/t/lines"})
    # テスト番号（計画書 8-8）：鳴らなかった電話が回線のせいか番号のせいかを、お客様ではない電話へかけて決める。無ければ決めずに待つ
    # （あるかは core の ring_test.usable の1か所＝鳴らす側と同じ答え）
    if not ring_test.usable(tx):
        out.append({"kind": "wait", "title": "テスト番号が決まっていません",
                    "how": "お客様のところで鳴らなかった電話が、回線のせいか番号のせいかを、お客様ではない電話（テスト番号）へかけて確かめます。"
                           "決めるまでは、その確かめを待ち、休んだ回線も再開しません（そのあいだも、ほかのお客様へは電話します）。",
                    "link": "/t/settings#s-line-test_number", "keys": ["line.test_number"]})
    if not notify_destination_set(tx):
        out.append({"kind": "wait", "title": "知らせの受け先が決まっていません",
                    "how": "止まったときや承認待ちを、ショートメールかチャットワークでお知らせする受け先です（決めるまでは、ダッシュボードの中だけでお知らせします）。",
                    "link": "/t/settings#g-notify", "keys": list(NOTIFY_KEYS)})
    return out


def notify_destination_set(tx: Tx) -> bool:
    """契約者あての知らせの受け先（ショートメールかチャットワークのどちらか）が決まっているか（土台の抜けと、はじめかたの手順が同じ答えを使う）。
    受け先の設定の名前は output/notices.DESTINATIONS の1か所。"""
    from salescall.core import settings
    tenant = tx.one("SELECT salescall.cur_tenant() AS t")["t"]
    return any(settings.resolve(tx, key, tenant_id=tenant, client_id=None, business_id=None) for key in NOTIFY_KEYS)


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
    - denied_now：門がいま見送っているお客様の数（理由ごと・多い順。数えるのは core の gate.denied_now の1か所＝業務で絞れる。CL-2 の 4-468 の 63 の (1)。
      ⛔ 2026-11-21 まで：門の記録を「今日、止め始めた数」として数えていた＝昨日から見送っているお客様が出ず、計画書 5-2 の「門で止めた件数（理由別）」
      （かけ直し待ち・判定待ちと並ぶ、いまの状態の数）と合っていなかった）。"""
    b = (business_id, business_id)
    results = {r["state"]: r["n"] for r in tx.all(
        "SELECT coalesce(l.business_state::text, 'open') AS state, count(*) AS n FROM salescall.tasks t"
        " LEFT JOIN salescall.latest_task_results l ON l.task_id = t.task_id WHERE " + _IN_BUSINESS + " GROUP BY 1", b)}
    u = approval.unconfirmed_counts(tx, business_id=business_id)
    denied = sorted(gate.denied_now(tx, business_id=business_id).items(), key=lambda rn: (-rn[1], rn[0]))
    return {"results": results, "unconfirmed": dict(u), "denied_now": denied}


# ── はじめかた（業務のタブ。docs/DASHBOARD.md 5-8）：まだ動き出していない業務で、次にすることを並べる。どの手順も「データにあるか」を
#    読むだけ（版・実施回・行・回線・録音の答え）。かけられるかは門だけが決める（ここでは言い切らない）。
def script_versions(tx: Tx, business_id: UUID) -> list[dict[str, Any]]:
    """業務の電話の台本の版（新しい順）と、それぞれ中身を読めるか（ready。読むのは core の business_settings.load の1か所＝通話と同じ確かめ）。
    「台本ができているか」（はじめかた）と「お客様リストに選べる版」（版を選ぶ欄）は、この1つの答えを使う（2026-11-20 の見直し：
    版を選ぶ欄は版が1つでもあれば出し、中身の無い版を選べた）。"""
    return [{**v, "ready": version_ready(tx, v["version_id"])}
            for v in tx.all("SELECT version_id, version_no, created_at FROM salescall.business_versions WHERE business_id = %s"
                            " ORDER BY version_no DESC", (business_id,))]


# 版を使えないとみなす断り（core の取り込み importer が「版を直してから取り込んでください」と断るのと同じ4つ。2026-11-21：前は2つだけを
# 数え、業務の決まりが無い・誤っている版を「使える」としていた＝はじめかたは済みなのに、取り込みで断られえた）
_VERSION_UNUSABLE = (LookupError, business_settings.SettingsInvalid, business_settings.business_rules.RulesMissing,
                     business_settings.business_rules.RulesInvalid)


def run_version(tx: Tx, run_id: UUID) -> UUID | None:
    """お客様リストがいま使う台本の版（データベースの salescall.run_current_version の1か所を読むだけ。無ければ None）。"""
    return tx.one("SELECT salescall.run_current_version(%s) AS v", (run_id,))["v"]


def version_ready(tx: Tx, version_id: UUID | None) -> bool:
    """その台本の版の中身を読めるか（版を選ぶ欄・はじめかた・取り込みの1番目の手順が同じ答えを使う。読むのは core の business_settings.load）。"""
    if version_id is None:
        return False
    try:
        business_settings.load(tx, version_id)
        return True
    except _VERSION_UNUSABLE:
        return False


def _script_ready(tx: Tx, business_id: UUID) -> bool:
    return any(v["ready"] for v in script_versions(tx, business_id))


def setup_steps(tx: Tx, client_id: UUID, business_id: UUID, recording_needs: list[Any]) -> list[dict[str, Any]]:
    """手順の一覧（name・done・how＝どこでするか・link）。recording_needs＝業務のタブが core の recordings.version_needs から作った
    録音の要るもの（同じ答えを2回読まない）。"""
    f = tx.one(
        "SELECT (SELECT count(*) FROM salescall.business_versions WHERE business_id = %s) AS versions,"
        " (SELECT count(*) FROM salescall.runs WHERE business_id = %s) AS runs,"
        " (SELECT r.run_id FROM salescall.runs r WHERE r.business_id = %s ORDER BY r.created_at DESC, r.run_id LIMIT 1) AS last_run,"
        " (SELECT count(*) FROM salescall.tasks t JOIN salescall.runs r ON r.run_id = t.run_id WHERE r.business_id = %s) AS rows,"
        " (SELECT count(*) FROM salescall.lines l WHERE l.retired_at IS NULL AND (l.client_id IS NULL OR l.client_id = %s)) AS lines,"
        " (SELECT count(*) FROM salescall.pc_agents p WHERE p.revoked_at IS NULL) AS pcs",
        (business_id, business_id, business_id, business_id, client_id))
    base = f"/c/{client_id}/b/{business_id}"
    script = _script_ready(tx, business_id)
    return [
        {"name": "電話の台本を入れる", "done": script, "how": words.SCRIPT_NOT_READY, "link": ""},
        {"name": "お客様リストを作る", "done": f["runs"] > 0, "how": "下の「＋ お客様リストを作る」から（電話の台本の版を選びます）", "link": ""},
        {"name": "お客様を取り込む", "done": f["rows"] > 0, "how": "お客様リストの画面の「お客様を取り込む」から",
         "link": f"{base}/runs/{f['last_run']}/import" if f["last_run"] else ""},
        {"name": "回線と PC を用意する（この依頼者で使える回線と、電話をつなぐ PC）", "done": f["lines"] > 0 and f["pcs"] > 0,
         "how": "「回線と PC」で回線を足すか使い先を決め、PC で「PC のログイン」をします（PC が無いと電話はかかりません）",
         "link": "/t/lines"},
        {"name": "知らせの受け先を決める（止まったときや承認待ちを受け取る）", "done": notify_destination_set(tx),
         "how": "「契約者の設定」の「知らせ」で、ショートメールの番号かチャットワークの部屋を決めます", "link": "/t/settings#g-notify"},
        {"name": "録音をそろえる", "done": script and not recording_needs,
         "how": "録音の要るものは、この画面の上の警告に出ます" if script else "電話の台本ができてから、録音の要るものが分かります",
         "link": ""},
    ]

