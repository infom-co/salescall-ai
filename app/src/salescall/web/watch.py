"""見張り（計画書 12-1 の「監視」に挙がっているもの。docs/DASHBOARD.md 5-14）。項目ごとの違いは、この表 `WATCHES` の1か所に書き、
画面の部品は1つ（承認待ちの受け箱と同じ作り）。

画面は、答える場所の事実を読んで並べるだけ（決め直さない）：
- 回線の段・待っていること＝core の health.line_of（言葉は health_rules.LineStage・Wait）。
- 行の段＝行の欄の表 task_states（CL-2 の health の1か所が書く。読むだけ）。行の待ちの言葉＝core の gate.REASONS（段 → 理由は
  gate.TASK_STAGE_REASONS の1か所）。
- 電話の終わり方＝試行の trouble（こちらの不具合か＝データベースの salescall.trouble_ours の1か所。言葉は output/words.call_end）。
- テスト番号の結果＝line_ring_tests（rang：鳴った／鳴らない／見分けられない）。
⏳ 名前の録音がそろわずかけられない行：行ごとの事実を読む部品が core に無い（門がその場で決める）＝CL-2 へのお願い 37。いまは業務のタブ
  の録音の警告（実施回ごと）に出している。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from salescall.core import errors, gate, health, health_rules
from salescall.core.access import Tx
from salescall.output import words
from salescall.web import links

DAYS = 7   # 終えた電話の一覧を見る日数（直近。一覧の長さを決めるだけ。数え方・決め方には使わない）


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
    where: str                                # tenant＝契約者の取引で読む／client＝依頼者ごとの取引で読む
    collect: Callable[[Tx], list[Entry]]
    attention: bool = True                    # ホームの「気をつけること」に数えるか（終えた電話の記録などは数えない）


def _company(tx: Tx, task_id: Any) -> str:
    t = tx.one("SELECT source_name, target_name FROM salescall.tasks WHERE task_id = %s", (task_id,))
    return " ／ ".join(x for x in ((t or {}).get("source_name"), (t or {}).get("target_name")) if x) or "（会社名なし）"


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
                  " WHERE t.tested_at > now() - make_interval(days => %s) ORDER BY t.tested_at DESC LIMIT 50", (DAYS,))
    return [Entry(r["name"], f"{words.RING_PURPOSE_WORDS.get(r['purpose'], words.NO_WORD)}：{words.RING_WORDS[r['rang']]}", r["tested_at"], "/t/lines")
            for r in rows]


def _runner_stuck(tx: Tx) -> list[Entry]:
    rows = tx.all("SELECT e.occurred_at, e.message, l.name FROM salescall.error_events e LEFT JOIN salescall.lines l ON l.line_id = e.line_id"
                  " WHERE e.kind = 'runner_stuck' AND e.occurred_at > now() - make_interval(days => %s)"
                  " ORDER BY e.occurred_at DESC LIMIT 50", (DAYS,))
    return [Entry(r["name"] or "回線", errors.LABELS["runner_stuck"], r["occurred_at"], "/t/lines", r["message"] or "") for r in rows]


def _calls(where_sql: str) -> Callable[[Tx], list[Entry]]:
    def collect(tx: Tx) -> list[Entry]:
        rows = tx.all("SELECT attempt_id, task_id, trouble, trouble_why, ended_at FROM salescall.attempts"
                      f" WHERE {where_sql} AND ended_at > now() - make_interval(days => %s) ORDER BY ended_at DESC LIMIT 50", (DAYS,))
        return [Entry(_company(tx, r["task_id"]), words.call_end(r["trouble"]), r["ended_at"], links.call(tx, r["task_id"], r["attempt_id"]),
                      r["trouble_why"] or "") for r in rows]
    return collect


def _rows(stage: str, detail: Callable[[Any], str]) -> Callable[[Tx], list[Entry]]:
    def collect(tx: Tx) -> list[Entry]:
        out = []
        for r in tx.all("SELECT task_id, state, moved_at FROM salescall.task_states WHERE stage = %s ORDER BY moved_at, task_id", (stage,)):
            out.append(Entry(_company(tx, r["task_id"]), detail(health.decode(r["state"])), r["moved_at"], links.row(tx, r["task_id"])))
        return out
    return collect


def _stage_reason(stage: str) -> Callable[[Any], str]:
    """行の段の待ちの言葉（段 → 理由は core の gate.TASK_STAGE_REASONS の1か所、文は gate.REASONS）。"""
    return lambda st: gate.REASONS[gate.TASK_STAGE_REASONS[stage]].message


def _resting(st: Any) -> str:
    kinds = "・".join(errors.LABELS.get(k, words.NO_WORD) for k in sorted(st.kinds))
    return _stage_reason("resting")(st) + (f"（続いた不具合：{kinds}）" if kinds else "")


WATCHES: dict[str, Watch] = {
    "lines": Watch("確かめ中・休みの回線", "回線のせいか番号のせいかを確かめている回線と、休ませている回線です。直ったと確かめられると自動で戻ります"
                   "（手で再開するときは「回線と PC」から）。", "tenant", _lines),
    "ring_tests": Watch("テスト番号の結果（直近7日）", "回線が電話網につながるかを、テスト番号に鳴らして確かめた結果です。「見分けられなかった」ときは、"
                        "すぐお知らせしています。", "tenant", _ring_tests, attention=False),
    "undecided_rows": Watch("番号か回線か、決まらず休んでいる行", "お客様のところで鳴らなかった電話が、番号のせいか回線のせいか、同じ回線の次の電話で"
                            "決まるのを待っています。その行にこだわらず、回線は次の行へ進んでいます（計画書 10-3）。", "client",
                            _rows("await_decision", lambda st: health_rules.TaskStage.AWAIT_DECISION.value)),
    "fault_calls": Watch("こちらの不具合で終えた電話（直近7日）", "こちらのプログラムの不具合で終えた電話です。番号・回線・行の回数には数えません。"
                         "直す担当へ修正依頼が出ています。", "client", _calls("salescall.trouble_ours(trouble)"), attention=False),
    "runner_stuck": Watch("お詫びのあとも通話の部品が戻らない（直近7日）", "通話の部品が、お詫びして終えたあとも戻らなかった記録です。",
                          "tenant", _runner_stuck, attention=False),
    "resting_rows": Watch("こちらの不具合で休ませている行", "同じ行で、こちらの不具合が続けて起きたので休ませています。", "client",
                          _rows("resting", _resting)),
    "recovery_rows": Watch("復旧を待っている行", "こちらの不具合で終えた電話の続きです。復旧を確かめたら、すぐ（お客様が出たあとならお詫びとともに）"
                           "かけ直します。", "client", _rows("await_recovery", _stage_reason("await_recovery"))),
    "voice_rows": Watch("声が聞き取れず、かけ直しを待っている行", "声が聞き取れずに終えた電話（こちらの不具合には数えません）のあと、声が通ると"
                        "分かっている回線を待っています。", "client", _rows("voice_redial", _stage_reason("voice_redial"))),
    "voice_calls": Watch("声が聞き取れずに終えた電話（直近7日）", "お客様の電波などで声が聞き取れず、お詫びして終えた電話です。同じ回線で違うお客様に"
                         "続けば、回線の異常として扱います。", "client", _calls("trouble = 'poor_audio'"), attention=False),
}


def collect(ctx: Any) -> list[tuple[str, Watch, list[tuple[dict[str, Any] | None, Entry]]]]:
    """すべての項目を集める（契約者の取引の項目は1回、依頼者の項目は依頼者ごとの読むだけの取引で）。(鍵, 項目, [(依頼者, 中身)])。"""
    clients = ctx.clients()
    out = []
    for key, w in WATCHES.items():
        if w.where == "tenant":
            got = [(None, e) for e in ctx.tenant(w.collect)]
        else:
            got = [(c, e) for c in clients for e in ctx.client(c["client_id"], w.collect, read_only=True)]
        out.append((key, w, got))
    return out


def attention_count(found: list[tuple[str, Watch, list[Any]]]) -> int:
    """ホームの「気をつけること」の数（attention の項目の件数の合計。数えるのはここ1か所）。"""
    return sum(len(got) for _, w, got in found if w.attention)
