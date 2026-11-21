"""見張り（計画書 12-1 の「監視」に挙がっているもの。docs/DASHBOARD.md 5-14）。項目ごとの違いは、この表 `WATCHES` の1か所に書き、
画面の部品は1つ（承認待ちの受け箱と同じ作り）。

画面は、答える場所の事実を読んで並べるだけ（決め直さない）：
- 回線の段・待っていること＝core の health.line_of（言葉は health_rules.LineStage・Wait）。
- 行の段＝行の欄の表 task_states（CL-2 の health の1か所が書く。読むだけ）。行の待ちの言葉＝core の gate.REASONS（段 → 理由は
  gate.TASK_STAGE_REASONS の1か所）。
- 電話の終わり方＝試行の trouble（こちらの不具合か＝データベースの salescall.trouble_ours の1か所。言葉は output/words.call_end）。
- テスト番号の結果＝line_ring_tests（rang：鳴った／鳴らない／見分けられない）。
- 名前の録音がそろわずかけられない行＝core の gate.name_recording_missing（門と同じ1か所＝voice_candidates。CL-2 4-440 の 37）。これからかける
  行（次の予定がかける・まだ予定の無い行）だけを、行ごとに聞く（終えた行は門が見ない）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from salescall.core import attempts, errors, gate, health, health_rules
from salescall.core.access import Tx
from salescall.output import notices, words
from salescall.web import links

DAYS = 7      # 終えた電話の一覧を見る日数（直近。一覧の長さを決めるだけ。数え方・決め方には使わない）
LIMIT = 500   # 1つの項目・1つの依頼者で読む数の上限（画面を重くしない。ふだんは届かない数。届いたら、上限までを出す）


@dataclass(frozen=True)
class Entry:
    what: str                 # 何が（回線の名前・お客様の会社名 など）
    detail: str               # どうなっているか（答える場所の言葉）
    at: datetime | None = None
    link: str = ""            # 詳しく見る画面
    note: str = ""            # 添える短い文（なぜ終えたか など）


@dataclass(frozen=True)
class Watch:
    title: str
    meaning: str                              # この項目が何か・システムが何をしているか（契約者が読む）
    where: str                                # tenant＝契約者の取引で読む／client＝依頼者ごとの取引で読む／system＝管理者の取引で読む（管理者にだけ見せる）
    collect: Callable[[Tx], list[Entry]]
    attention: bool = True                    # ホームの「気をつけること」に数えるか（終えた電話の記録などは数えない）


def _lines(tx: Tx) -> list[Entry]:
    out = []
    for ln in tx.all("SELECT line_id, name FROM salescall.lines WHERE retired_at IS NULL ORDER BY name, line_id"):
        st = health.line_of(tx, ln["line_id"])
        if st.stage is health_rules.LineStage.HEALTHY:
            continue
        detail = st.stage.value + (f"・待っていること：{st.wait.value}" if st.wait else "")
        out.append(Entry(ln["name"], detail, st.next_at, "/t/lines"))
    return out


def _ring_tests(tx: Tx) -> list[Entry]:
    rows = tx.all("SELECT t.tested_at, t.purpose, t.rang, l.name FROM salescall.line_ring_tests t"
                  " JOIN salescall.lines l ON l.line_id = t.line_id"
                  " WHERE t.tested_at > now() - make_interval(days => %s) ORDER BY t.tested_at DESC LIMIT %s", (DAYS, LIMIT))
    return [Entry(r["name"], f"{words.RING_PURPOSE_WORDS.get(r['purpose'], words.NO_WORD)}：{words.RING_WORDS[r['rang']]}", r["tested_at"], "/t/lines")
            for r in rows]


def _runner_stuck(tx: Tx) -> list[Entry]:
    rows = tx.all("SELECT e.occurred_at, e.message, l.name FROM salescall.error_events e LEFT JOIN salescall.lines l ON l.line_id = e.line_id"
                  " WHERE e.kind = 'runner_stuck' AND e.occurred_at > now() - make_interval(days => %s)"
                  " ORDER BY e.occurred_at DESC LIMIT %s", (DAYS, LIMIT))
    return [Entry(r["name"] or "回線", errors.LABELS["runner_stuck"], r["occurred_at"], "/t/lines", r["message"] or "") for r in rows]


def _calls(kind: str) -> Callable[[Tx], list[Entry]]:
    """お詫びして終えた電話（直近 DAYS 日）。読むのは core の attempts.ended_with_trouble の1か所（絞り方は attempts.ENDED_KINDS。CL-2 4-451）。"""
    def collect(tx: Tx) -> list[Entry]:
        rows = attempts.ended_with_trouble(tx, kind, days=DAYS, limit=LIMIT)
        info = links.rows(tx, [r["task_id"] for r in rows])
        return [Entry(info[r["task_id"]]["who"], words.call_end(r["trouble"]), r["ended_at"],
                      f"{info[r['task_id']]['url']}/calls/{r['attempt_id']}", words.plain(r["trouble_why"] or "")) for r in rows if r["task_id"] in info]
    return collect


def _rows(stage: health_rules.TaskStage, detail: Callable[[Any], str]) -> Callable[[Tx], list[Entry]]:
    """その段にいる行。読むのは core の health.rows_in_stage の1か所（CL-2 4-451）。"""
    def collect(tx: Tx) -> list[Entry]:
        rows = health.rows_in_stage(tx, stage, limit=LIMIT)
        info = links.rows(tx, [r["task_id"] for r in rows])
        return [Entry(info[r["task_id"]]["who"], detail(r["state"]), r["moved_at"], info[r["task_id"]]["url"])
                for r in rows if r["task_id"] in info]
    return collect


def _stage_reason(stage: str) -> Callable[[Any], str]:
    """行の段の待ちの言葉（段 → 理由は core の gate.TASK_STAGE_REASONS の1か所、文は gate.REASONS）。"""
    return lambda st: gate.REASONS[gate.TASK_STAGE_REASONS[stage]].message


def _resting(st: Any) -> str:
    kinds = "・".join(errors.LABELS.get(k, words.NO_WORD) for k in sorted(st.kinds))
    return _stage_reason("resting")(st) + (f"（続いた不具合：{kinds}）" if kinds else "")


def _name_recordings(tx: Tx) -> list[Entry]:
    """名前の録音がそろわず、かけられないお客様（計画書 12-1）。足りない録音の部分の名前を見せる（録音を作るのは業務のタブ・録音の画面）。"""
    rows = tx.all("SELECT t.task_id, n.decided_at FROM salescall.tasks t LEFT JOIN salescall.current_next_actions n ON n.task_id = t.task_id"
                  " WHERE coalesce(n.kind::text, 'call') = 'call' ORDER BY n.decided_at NULLS FIRST, t.task_id LIMIT %s", (LIMIT,))
    missing = {}
    for r in rows:
        try:
            got = gate.name_recording_missing(tx, r["task_id"])
        except LookupError:   # 業務設定の中身をまだ置いていない版（業務の取り込みの前）：録音の前に、そもそもかけられない
            continue
        if got:
            missing[r["task_id"]] = (got, r["decided_at"])
    info = links.rows(tx, list(missing))
    return [Entry(info[t]["who"], "名前の録音がそろうまで、おかけしません（足りない録音：" + "・".join(names) + "）", at, info[t]["url"])
            for t, (names, at) in missing.items() if t in info]


def _undelivered(for_admin: bool) -> Callable[[Tx], list[Entry]]:
    """送れていない知らせ（届いたかの決め方は output/notices.delivery_state の1か所。知らせの画面と同じ）。肝心の知らせが届いて
    いないので、ホームの「気をつけること」に数える。契約者あては契約者の取引で、管理者あては管理者の取引（system）で読む。"""
    link = "/t/notices#admin-notices" if for_admin else "/t/notices"

    def collect(tx: Tx) -> list[Entry]:
        rows = tx.all("SELECT e.kind, e.summary, e.occurred_at,"
                      " ARRAY(SELECT d.status FROM salescall.notification_deliveries d WHERE d.event_id = e.event_id) AS statuses"
                      " FROM salescall.notification_events e WHERE e.for_admin = %s AND e.occurred_at > now() - make_interval(days => %s)"
                      " ORDER BY e.occurred_at DESC LIMIT %s", (for_admin, DAYS, LIMIT))
        return [Entry(words.notice_kind(r["kind"]), words.plain(r["summary"]), r["occurred_at"], link) for r in rows
                if notices.delivery_state(r["statuses"]) == "gave_up"]
    return collect


WATCHES: dict[str, Watch] = {
    "undelivered": Watch("送れていない知らせ（直近7日）", "ショートメールもチャットワークも、決めた回数送っても届かなかった知らせです。"
                         "中身を「知らせ」で確かめ、送り先の設定を見直してください。", "tenant", _undelivered(False)),
    "undelivered_admin": Watch("送れていない管理者あての知らせ（直近7日）", "管理者あての知らせが、決めた回数送っても届かなかったものです。"
                               "管理者にだけ見えます。中身を「知らせ」の「管理者あて」で確かめてください。", "system", _undelivered(True)),
    "lines": Watch("確かめ中・休みの回線", "回線のせいか番号のせいかを確かめている回線と、休ませている回線です。直ったと確かめられると自動で戻ります"
                   "（手で再開するときは「回線と PC」から）。", "tenant", _lines),
    "ring_tests": Watch("テスト番号の結果（直近7日）", "回線が電話網につながるかを、テスト番号に鳴らして確かめた結果です。「見分けられなかった」ときは、"
                        "すぐお知らせしています。", "tenant", _ring_tests, attention=False),
    "undecided_rows": Watch("番号か回線か、決まらず休んでいるお客様", "お客様のところで鳴らなかった電話が、番号のせいか回線のせいか、同じ回線の次の電話で"
                            "決まるのを待っています。そのお客様にこだわらず、回線は次のお客様へ進んでいます。", "client",
                            _rows(health_rules.TaskStage.AWAIT_DECISION, lambda st: health_rules.TaskStage.AWAIT_DECISION.value)),
    "fault_calls": Watch("こちらの不具合で終えた電話（直近7日）", "こちらのプログラムの不具合で終えた電話です。番号・回線・お客様の回数には数えません。"
                         "直す担当へ修正依頼が出ています。", "client", _calls("ours"), attention=False),
    "runner_stuck": Watch("お詫びのあとも電話を進めるプログラムが戻らない（直近7日）", "電話を進めるプログラムが、お詫びして終えたあとも戻らなかった記録です。",
                          "tenant", _runner_stuck, attention=False),
    "resting_rows": Watch("こちらの不具合で休ませているお客様", "同じお客様で、こちらの不具合が続けて起きたので休ませています。", "client",
                          _rows(health_rules.TaskStage.RESTING, _resting)),
    "recovery_rows": Watch("復旧を待っているお客様", "こちらの不具合で終えた電話の続きです。復旧を確かめたら、すぐ（お客様が出たあとならお詫びとともに）"
                           "かけ直します。", "client", _rows(health_rules.TaskStage.AWAIT_RECOVERY, _stage_reason("await_recovery"))),
    "voice_rows": Watch("声が聞き取れず、かけ直しを待っているお客様", "声が聞き取れずに終えた電話（こちらの不具合には数えません）のあと、声が通ると"
                        "分かっている回線を待っています。", "client", _rows(health_rules.TaskStage.VOICE_REDIAL, _stage_reason("voice_redial"))),
    "name_recordings": Watch("名前の録音がそろわず、かけられないお客様", "お呼びする方のお名前などの録音がそろうまで、そのお客様にはおかけしません。"
                             "録音がそろうと、自動でかけ始めます。", "client", _name_recordings),
    "voice_calls": Watch("声が聞き取れずに終えた電話（直近7日）", "お客様の電波などで声が聞き取れず、お詫びして終えた電話です。同じ回線で違うお客様に"
                         "続けば、回線の異常として扱います。", "client", _calls("poor_audio"), attention=False),
}


def collect(ctx: Any) -> list[tuple[str, Watch, list[tuple[dict[str, Any] | None, Entry]]]]:
    """すべての項目を集める（契約者の取引の項目は1回、依頼者の項目は依頼者ごとの読むだけの取引で、管理者の項目は管理者のときだけ
    管理者の取引で1回）。(鍵, 項目, [(依頼者, 中身)])。管理者でなければ、管理者の項目は並べない（「なし」とも出さない）。"""
    shown = {k: w for k, w in WATCHES.items() if w.where != "system" or ctx.person.is_admin}
    found: dict[str, list[tuple[dict[str, Any] | None, Entry]]] = {k: [] for k in shown}

    def keys(where: str) -> list[str]:
        return [k for k, w in shown.items() if w.where == where]

    def read(ks: list[str]) -> Callable[[Tx], dict[str, list[Entry]]]:
        return lambda tx: {k: shown[k].collect(tx) for k in ks}
    if keys("system"):
        for k, got in ctx.system(read(keys("system"))).items():
            found[k] = [(None, e) for e in got]
    for k, got in ctx.tenant(read(keys("tenant")), read_only=True).items():   # 契約者の取引は1つ（読むだけ）
        found[k] = [(None, e) for e in got]
    for c in ctx.clients():   # 依頼者ごとに、読むだけの取引を1つ（その中で依頼者の項目をすべて集める）
        for k, got in ctx.client(c["client_id"], read(keys("client")), read_only=True).items():
            found[k] += [(c, e) for e in got]
    return [(k, w, found[k]) for k, w in shown.items()]


def attention_count(found: list[tuple[str, Watch, list[Any]]]) -> int:
    """ホームの「気をつけること」の数（attention の項目の件数の合計。数えるのはここ1か所）。"""
    return sum(len(got) for _, w, got in found if w.attention)
