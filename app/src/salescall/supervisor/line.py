"""回線ごとの処理（DESIGN.md 5-4・5-5）。持ち主 CL-2。

中央の見守り役（サーバー）が、PC の部品がつないでいる回線1本ごとに、別の流れ（スレッド）で `loop` を動かす。電話の操作と通話の
声は、その回線の電話の部品（`links.Links.driver`＝PC の部品へ頼む形）を通る。1本の通話の処理の誤りは、その通話だけを結果不明にして
次へ進む（ほかの回線を巻き込まない＝CL-1 の 2026-10-13 の (c)。スマホのアプリ・音声の機器を扱う部分は PC の部品の側で回線ごとに分ける）。
1回線（スマホ1台）で同時に1本なので、この処理は1本ずつ順にかける（Intent.md 8-3）。

1回分（`one_round`）：
1. その回線でかけてよい依頼者を並べ（scheduler.clients_for_line）、依頼者ごとに、いまかけてよさそうな行を門に渡す順に出す
   （scheduler.due_tasks。判定はしない）。**お客様と約束したかけ直しを、すべての依頼者について先に出し、それが済んでから
   ふつうの電話を始める**（2026-10-19 ユーザー：「その日の約束の電話には目印が付き、それを処理してから、通常の順序の電話を
   始めた方がいい」。約束の時刻・幅が来ていない約束は、来るまで出ない＝その時間の中で最優先になる）。
2. 門（gate.claim）に渡す。通れば予約証が出る。断られたときの扱いは、門の理由の表の扱いの列の1か所（gate.REASONS）：
   - 扱いが plan・plan_if_known の理由だけ、次の動き（next_action.after_denial）へ渡す（次にいつ候補に出すかを決めるのは次の動き
     だけ＝Q4。終える・分かっている時刻へ1回書く）。ほかの理由では、行の予定を書き直さない（gate.PLAN_KEPT）。
   - 回線の理由（gate.LINE_REASONS）なら、この回線はしばらく休む（ほかの回線は、その行をかけられる）。
   - 依頼者の理由（gate.CLIENT_REASONS）なら、その依頼者の行は、この回だけ飛ばす。業務の理由（gate.BUSINESS_REASONS：その業務の
     今日の上限・時刻を指定された約束が、その業務の平均の長さのうちに来る）なら、その業務の行は、この回だけ飛ばす（同じ依頼者の
     ほかの業務の行はかける）。実施回の理由・品質の段の理由も、この回だけ飛ばす。すぐに変わる行の理由（gate.TASK_REASONS：同じ行・
     同じお客様・同じ番号の発信が進行中）は、この回だけその行を飛ばす。
   - 候補に出さない理由（gate.UNOFFERED_REASONS）は、候補を出す処理が初めから出さない（ここへ来るのは、出したあとで変わったとき）。
3. 予約証で発信の出口（dial_exit.dial）がかけ、通話を進める部品（CL-1）が最後まで進める。外へ出た誤りは、その試行を結果不明に
   して「見守り役の誤り」として記録する（止めない）。お客様のところで鳴らなかった電話（かけられなかった・お客様が出る前に終わった
   ＝結果不明。通話を進める部品が落ちた・終えずに戻ったときも）のあとは、**回線のせいと分かった電話だけを回線の異常として数える**
   （`_unrung` の1か所。だれのせいかはデータベースの salescall.unrung_blame が、同じ回線の前後の電話の証拠で決める＝0129：鳴った
   電話を挟まずに違う番号も続けて鳴らなければ回線のせい）。数えたら、その回線を休ませる（一時中止。続けば、エラーの処置の表の
   とおり、その回線を止めて管理者と契約者に知らせる）。番号のせい・まだ分からないときは、回線を休ませず、回線の異常にも数えない
   （番号の続きで数える）。2026-10-24 ユーザー：「明らかに番号に問題がある場合は、その行は取りやめですが、回線の問題が明らかな場合は
   行は取りやめでなく一時中止だけ」。⛔ 0129：前は、アプリの言い分（番号のせい・回線のせいか分からない）で分け、お客様が出る前に
   終わった電話はいつも回線の異常に数えた（見直し18a の問題1〜3）。回線がまだ前の通話の途中・PC の部品がかけようとしなかった
   ときは、その回線を休ませる（数えない）。
4. **回線が「通話中」のまま（電話の部品が「通話中」と答え続ける・答えられない）で、かけられない状態が `STUCK_AFTER` 続いたら、
   回線の異常として数える**（`Stuck` の1か所。数える入口は、かけられなかったときと同じ `_line_fault`＝続けば、処置の表のとおり、
   その回線を止めて知らせる。⛔ 2026-10-22 の見直し15：前は、通話中を理由に休み続けるだけで、だれにも知らされなかった）。
5. **通話の長さでは切らない**（2026-10-24 ユーザー：時間で業務を打ち切らない）。通話ごとの口（`_Call`＝plugins.CallControl）を通話を
   進める部品へ渡し、その「動いています」の知らせ（`beat`）が設定 call.frozen_after（初め3秒。2026-10-24 ユーザー：「20秒でも長すぎる
   でしょ」）途切れたら、こちらのプログラムが固まったとみなし、通話を途中で切るときの2つの一言で終え、結果不明にして理由（frozen）を
   残す。通話を進める部品が見つけた甚大な問題（`end_for_trouble`）も同じ道（2026-10-24 ユーザー決定）。見張りはデータベースを通さない
   （⛔ 0130：前はデータベースの「進んだ時刻」を 90 秒見ていた）。
6. **その回線にかける行が残っていない**のに、番号のせいか回線のせいかがまだ分からない続きがあれば、テスト番号で試して決める
   （`_close_if_nothing_left`→core/ring_test。テスト番号の無い契約者は前の区切りで決める）。**回線の異常で止めた回線**は、テスト番号で
   再開してよいかを確かめる（`_check_stop`。鳴れば成功＝core/watch.record_check。2026-10-24 ユーザー「OK」）。テスト番号を鳴らすのは、
   その回線の電話の部品を持つ、ここだけ。
締め（判定・次の動き）は、見守り役の決まった間隔の仕事がする（jobs の settle）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import attempts, dial_exit, gate, next_action, ring_test, scheduler, settings, watch
from salescall.core.access import Access, Actor, Tx
from salescall.core.errors import ErrorReport
from salescall.supervisor import ACTOR, call_began, call_over, plugins, report, updates
from salescall.supervisor.links import CallDriver

CANDIDATES_PER_CLIENT = 20   # 1回に門へ渡す行の数の上限（依頼者ごと）。通れば1本かけて、また初めから並べ直す
IDLE_SECONDS = 5             # かける行が無いとき・版が合わないときの待ち
# 回線が「通話中」のまま、かけられない状態がこれだけ続いたら、回線の異常として数える（秒）。こちらの通話は、かける前に必ず終わって
# いる（話している通話は回線ごとの処理が最後まで進めて戻り、固まった通話は call.frozen_after でお詫びして終える）ので、そのあとも
# 「通話中」なら、通話ではなく、電話の部品か回線が動いていない（1分＝電話の部品が切れたことに気づくのに足りる長さ）
STUCK_AFTER = 60.0
WATCH_EVERY = 0.25   # 通話の途中に、「動いています」の知らせが途切れていないかを見る間（秒。固まったとみなす長さより十分短く）
UNRUNG_PAUSE = "unrung_line"   # 回線のせいで鳴らなかった電話を数えて、回線を休ませた（Round.pause の名前）
RING_TEST_PAUSE = "ring_test"  # テスト番号を試したが決まらなかった（分からない・話している途中）：休んでから試し直す（鳴らし続けない）


@dataclass(frozen=True)
class Round:
    """1回分の結果。called＝かけた試行、pause＝回線の理由（その回線をしばらく休む。かけたあとでも、回線のせいで鳴らなかったら
    休む）。free＝電話の部品が「通話中ではない」と答えた（発信の出口が、通話中かを聞いて先へ進んだ）。"""
    called: UUID | None = None
    pause: str | None = None
    free: bool = False


@dataclass
class Stuck:
    """回線が「通話中」のまま、かけられない状態の見張り（判断はこの1か所）。since＝続けて「通話中」と分かった始まり。"""
    since: float | None = None

    def seen(self, got: Round, now: float) -> bool:
        """1回分の結果を見る。「通話中」が STUCK_AFTER 続いたら True（数えたら、また初めから数える＝続けば、また数える）。
        「通話中ではない」と分かったら数え直す。どちらとも分からない回（かける行が無い・門が断った）は、そのまま。"""
        if got.pause == dial_exit.Outcome.LINE_IN_CALL.value:
            if self.since is None:
                self.since = now
            elif now - self.since >= STUCK_AFTER:
                self.since = now
                return True
        elif got.free:
            self.since = None
        return False


class _Call:
    """通話ごとの口（plugins.CallControl。判断はこの1か所）：通話を進める部品の「動いています」の知らせ（`beat`）を WATCH_EVERY ごとに
    見て、frozen_after 途切れたら、こちらのプログラムが固まったとみなして終える（frozen）。通話を進める部品が見つけた甚大な問題
    （`end_for_trouble`）も同じ道（`_end`）：通話を途中で切るときの2つの一言で終え（dial_exit.abandon）、試行を結果不明にして理由を
    残す（attempts.end_for_trouble）。1回だけ。かける前から終わるまで、supervisor の1か所（call_live）に載る（止めるとき・PC のログインを
    外したときに待つ。かけた直後に閉じない）。進んでいる通話は、どれだけ長くても切らない（2026-10-24 ユーザー：時間で業務を打ち切らない）。"""

    def __init__(self, access: Access, actor: Actor, tenant_id: UUID, client_id: UUID, line_id: UUID, attempt_id: UUID,
                 driver: CallDriver, frozen_after: float) -> None:
        self.access, self.actor, self.tenant_id, self.client_id = access, actor, tenant_id, client_id
        self.line_id, self.attempt_id, self.driver, self.frozen_after = line_id, attempt_id, driver, frozen_after
        self.last = time.monotonic()
        self.ended = False
        self._lock = threading.Lock()
        self._done = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"通話の見張り {attempt_id}", daemon=True)

    def beat(self) -> None:
        self.last = time.monotonic()

    def end_for_trouble(self, reason: str) -> None:
        if reason not in attempts.TROUBLES or reason == "frozen":   # frozen を決めるのは、この見張りだけ
            raise ValueError(f"通話を終える理由は {tuple(t for t in attempts.TROUBLES if t != 'frozen')} のどれかです：{reason!r}")
        self._end(reason)

    def _end(self, reason: str) -> bool:
        with self._lock:
            if self.ended:
                return False
            self.ended = True
        dial_exit.abandon(self.driver)   # お客様を無言で放置しない（通話を途中で切るときの2つの一言を流して切る）
        try:
            self.access.in_client(self.actor, self.tenant_id, self.client_id,
                                  lambda tx: attempts.end_for_trouble(tx, self.attempt_id, reason))
        except Exception as e:  # noqa: BLE001 記録できなくても止めない（通話を進める部品が戻れば `_end_unknown` が結果不明にする）
            report(self.access, self.tenant_id, "通話を終える記録", e, line_id=self.line_id)
        return True

    def start(self) -> None:
        """「話している」に載せる（かける前から。supervisor.call_live）。"""
        call_began(self.line_id, self)

    def watch(self) -> None:
        """かけたあと、知らせの見張りを始める（途切れの長さは、ここから数える）。"""
        self.last = time.monotonic()
        self.thread.start()

    def stop(self) -> None:
        """見張りを止め、「話している」から外す（何度呼んでもよい）。"""
        self._done.set()
        call_over(self.line_id, self)

    def _run(self) -> None:
        while not self._done.wait(WATCH_EVERY):
            if time.monotonic() - self.last > self.frozen_after:
                if self._end("frozen"):
                    report(self.access, self.tenant_id, "通話を進める部品",
                           RuntimeError(f"「動いています」の知らせが {self.frozen_after:g} 秒途切れました（おわびして終えました）"),
                           line_id=self.line_id)
                return


def frozen_after(tx: Any) -> float:
    """通話を進める部品が固まったとみなす長さ（秒。設定 call.frozen_after の1か所。2026-10-24 ユーザー：3秒）。"""
    return float(settings.system_value(tx, "call.frozen_after"))


def _line_fault(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, what: str, *,
                client_id: UUID | None = None, attempt_id: UUID | None = None) -> None:
    """回線の異常として数える（入口はこの1か所。続けば、エラーの処置の表のとおり、その回線を止めて知らせる）。"""
    watch.after_error(access, actor, ErrorReport("line", None, what), tenant_id=tenant_id, line_id=line_id,
                      client_id=client_id, attempt_id=attempt_id)


UNRUNG_LINE = "鳴った電話を挟まずに、この回線で違う番号へかけた電話も続けて、お客様のところで鳴りませんでした（回線のせい）"


def _unrung(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID) -> bool:
    """お客様のところで鳴らなかった電話のあと（1か所）：その回線の、回線のせいと分かったのに、まだ回線の異常として記録していない
    電話を、1本ずつ回線の異常として数える（attempts.line_faults_unrecorded。続きが初めて回線のせいになったときは、前のまだ分から
    なかった分もまとめて数える＝鳴った電話を挟まずに違う番号が続けて鳴らなかった数が、回線の異常の数）。数えたら True（その回線を
    休ませる）。番号のせい・まだ分からないときは数えない（False）。"""
    faults = access.in_tenant(actor, tenant_id, lambda tx: attempts.line_faults_unrecorded(tx, line_id))
    for f in faults:
        _line_fault(access, actor, tenant_id, line_id, UNRUNG_LINE, client_id=f["client_id"], attempt_id=f["attempt_id"])
    return bool(faults)


def _claim(tx: Any, task: dict[str, Any], line_id: UUID, holder: str, tenant_id: UUID, client_id: UUID) -> Any:
    got = gate.claim(tx, task_id=task["task_id"], line_id=line_id, lease_holder=holder)
    if isinstance(got, gate.Denied) and got.reason not in gate.PLAN_KEPT:   # 行の予定を書き直さない理由は gate の1か所
        next_action.after_denial(tx, task["task_id"], got, tenant_id=tenant_id, client_id=client_id,
                                 business_id=task["business_id"])
    return got


def _end_unknown(access: Access, actor: Actor, tenant_id: UUID, client_id: UUID, attempt_id: UUID) -> bool:
    """通話を進める部品から誤りが外へ出た・通話を終えずに戻った：まだ進行中なら結果不明にする（お客様が出ていれば、締めがお詫び
    してかけ直す）。結果不明にしたら True。"""
    def work(tx: Any) -> bool:
        if tx.one("SELECT salescall.is_active(state) AND salescall.is_dialed(state) AS live FROM salescall.attempts"
                  " WHERE attempt_id = %s", (attempt_id,))["live"]:   # 発信まで進み、まだ進行中
            attempts.move(tx, attempt_id, "technical_error")
            return True
        return False
    return access.in_client(actor, tenant_id, client_id, work)


def one_round(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, holder: str, driver: CallDriver,
              runner: plugins.CallRunner) -> Round:
    clients = access.in_tenant(actor, tenant_id, lambda tx: scheduler.clients_for_line(tx, line_id))
    held: list[str] = []   # 依頼者の理由（同時の上限・無効）で断られた
    for promised_only in (True, False):   # 約束したかけ直しを、すべての依頼者について先に（目印で分けるのは due_tasks の1か所）
        for client_id in clients:
            got = _client_round(access, actor, tenant_id, client_id, line_id, holder, driver, runner, promised_only, held)
            if got is not None:
                return got
    # どこもかけられず、依頼者の理由で断られていた：回線の理由と同じく休む（⛔ 2026-10-22 の見直し：休まずに数秒ごとに門へ渡し
    # 直し、同時の上限のあいだ、断られた記録を増やし続けた）
    if held:
        return Round(pause=held[0])
    return _close_if_nothing_left(access, actor, tenant_id, line_id, clients, driver)


def _close_if_nothing_left(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, clients: list[UUID],
                           driver: CallDriver) -> Round:
    """かける行が無い回（1か所）：その回線に、番号のせいか回線のせいかがまだ分からない続きがあり、その回線にかける行が、分からないまま
    待つ行のほかに残っていなければ（scheduler.rows_left）、決着させる。2026-10-24 ユーザー：「回線か番号かどちらかわからない場合は、
    その行に固執するのでなく、1回目から、次の行に進むようにしてください。…一つの行で止まり続けることがないようにしてください。」
    「その回線にかける行が残っていないときは、『テスト番号』で決めます。」
    - テスト番号があれば、試して決める（core/ring_test の1か所。鳴れば、まだ分からなかった続きは番号のせい＝鳴った電話と同じ区切り。
      鳴らなければ回線のせい＝回線の異常として数える）。決まらなかった（分からない・話している途中）ときは、休んでから試し直す
      （テスト番号を鳴らし続けない）。
    - テスト番号が無ければ（⏳ フェーズ1でユーザーが入れるまで）、続きを閉じて前の区切りで決める（attempts.close_unrung_run の no_rows。
      前に鳴った電話があれば番号のせい、無ければ回線のせい）。
    次の行があれば、次の電話の結果で決まるので試さない（夜・時間帯の外で「今はかけられない」だけの行も、残っている行）。"""
    if not access.in_tenant(actor, tenant_id, lambda tx: attempts.line_run_pending(tx, line_id)):
        return Round()
    if any(access.in_client(actor, tenant_id, c, scheduler.rows_left) for c in clients):
        return Round()
    tested = ring_test.run(access, actor, tenant_id, line_id, driver, "no_rows")
    if tested is ring_test.Result.NO_NUMBER:
        if access.in_tenant(actor, tenant_id, lambda tx: attempts.close_unrung_run(tx, line_id, "no_rows")):
            _unrung(access, actor, tenant_id, line_id)   # 回線のせいと決まった分は、回線の異常として数える（同じ1か所）
        return Round()
    if tested is ring_test.Result.SILENT:
        _unrung(access, actor, tenant_id, line_id)       # 回線のせいと決まった分を、回線の異常として数える
        return Round(pause=UNRUNG_PAUSE)
    if tested is ring_test.Result.RANG:
        return Round()
    return Round(pause=RING_TEST_PAUSE)


def _check_stop(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, driver: CallDriver) -> float | None:
    """回線の異常で止めた、この回線の停止の確かめ（2026-10-24 ユーザー：再開の確かめもテスト番号で「OK」）：テスト番号で確かめる停止
    （core/ring_test.checks_stop の1か所。テスト番号の無い契約者は確かめの部品＝supervisor.jobs）で、確かめの時が来ていれば、テスト番号で
    試し、鳴れば成功・鳴らなければ失敗として記録する（core/watch.record_check。決めた回数続けて成功したら解く）。分からない・話している
    途中なら記録しない（いつもの休みのあとで試し直す）。返すのは、次に確かめてよいまでの秒（その停止が無い・決まらなかったときは None＝
    いつもの休み）：回線ごとの処理は、それより長く休まない（確かめの間隔＝stop.line.probe_interval のとおりに試す）。"""
    def mine(tx: Tx) -> list[dict[str, Any]]:
        return [s for s in watch.checkable(tx) if s["line_id"] == line_id and ring_test.checks_stop(tx, s)]
    stopped = access.in_tenant(actor, tenant_id, mine)
    due = [s for s in stopped if s["wait"] <= 0]
    if due:
        tested = ring_test.run(access, actor, tenant_id, line_id, driver, "resume_check")
        if tested not in (ring_test.Result.RANG, ring_test.Result.SILENT):
            return None   # 決まらなかった：いつもの休みのあとで試し直す（確かめの時刻は来たままなので、すぐ試し直すと鳴らし続ける）
        ok = tested is ring_test.Result.RANG
        evidence = "テスト番号の電話が鳴った" if ok else "テスト番号の電話が鳴らなかった"
        for s in due:
            access.in_tenant(actor, tenant_id, lambda tx, s=s: watch.record_check(tx, s["event_id"], ok, evidence=evidence))
        stopped = access.in_tenant(actor, tenant_id, mine)
    return min((s["wait"] for s in stopped), default=None)


def _client_round(access: Access, actor: Actor, tenant_id: UUID, client_id: UUID, line_id: UUID, holder: str,
                  driver: CallDriver, runner: plugins.CallRunner, promised_only: bool = False,
                  held: list[str] | None = None) -> Round | None:
    """1つの依頼者の行を、門に渡す。かけたか回線を休むなら Round、この依頼者でかけられる行が無ければ None。
    業務の理由で断られた業務は、この回だけ飛ばし、その業務を除いて候補を読み直す（前は読み直さなかったので、上限に達した業務の
    行が候補の先頭を埋めると、同じ依頼者のほかの業務の行が1日じゅう候補に入らなかった＝2026-10-18 の見直し）。"""
    capped: set[UUID] = set()
    runs: set[UUID] = set()      # 実施回の版の理由で断られた実施回（gate.RUN_REASONS）
    staged: set[UUID] = set()    # 品質の段の理由で断られた実施回（gate.STAGE_REASONS。決まった文面だけの電話は出す）
    held = [] if held is None else held
    while True:
        tasks = access.in_client(actor, tenant_id, client_id,
                                 lambda tx: scheduler.due_tasks(tx, CANDIDATES_PER_CLIENT, skip_businesses=capped,
                                                                skip_runs=runs, skip_stage=staged,
                                                                promised_only=promised_only, line_id=line_id))
        newly = False
        for task in tasks:
            if task["business_id"] in capped or task["run_id"] in runs:
                continue
            got = access.in_client(actor, tenant_id, client_id,
                                   lambda tx, task=task: _claim(tx, task, line_id, holder, tenant_id, client_id))
            if isinstance(got, gate.Claim):
                driver.prepare_call(got.voice_type)   # この通話の声の型を電話の部品へ（おわびの録音も同じ声で）
                frozen = access.in_tenant(actor, tenant_id, frozen_after)
                call = _Call(access, actor, tenant_id, client_id, line_id, got.attempt_id, driver, frozen)
                call.start()   # かける前から「話している」に載せる（かけた直後に、止める・ログインを外す処理が閉じない）
                try:
                    done = dial_exit.dial(access, actor, tenant_id, client_id, got.attempt_id, driver)
                    if done is dial_exit.Outcome.DIALED:
                        call.watch()
                        try:
                            runner.run(access, actor, got, tenant_id=tenant_id, client_id=client_id, call=call)
                        except Exception as e:  # noqa: BLE001 その通話だけを結果不明にして、次へ進む（止めない）
                            call.stop()
                            if not call.ended:
                                dial_exit.abandon(driver)   # 通話が残っていれば、おわびして切る（お客様を無言で放置しない）
                                _end_unknown(access, actor, tenant_id, client_id, got.attempt_id)
                            report(access, tenant_id, "通話を進める部品", e, line_id=line_id)
                        else:
                            call.stop()
                            # 通話を終えずに戻った（試行がまだ進行中）：誤りのときと同じに扱う（⛔ 2026-10-22 の見直し：電話は
                            # つながったまま無言になり、回線は「通話中」で止まり続け、だれにも知らされなかった）
                            if not call.ended and _end_unknown(access, actor, tenant_id, client_id, got.attempt_id):
                                dial_exit.abandon(driver)
                                report(access, tenant_id, "通話を進める部品",
                                       RuntimeError("通話を終えないまま戻りました"), line_id=line_id)
                finally:
                    call.stop()   # どの道でも「話している」から外す（残ると、止める・ログインを外す処理が待ち続ける）
                if done is dial_exit.Outcome.DIALED:
                    paused = access.in_client(actor, tenant_id, client_id,
                                              lambda tx: attempts.blame(tx, got.attempt_id)) is not None \
                        and _unrung(access, actor, tenant_id, line_id)
                    # お客様のところで鳴らずに終わった（せいがある＝attempts.blame。お客様が出る前に終わった結果不明＝数え方の
                    # line_failure など。どの回数にも数えない）：回線のせいと分かれば回線の
                    # 異常として数えて休む（`_unrung` の1か所。数えない電話の歯止め＝2026-10-22 深夜 ユーザー「回線の異常が続いたら
                    # 回線を止める仕組み：OK」。⛔ 見直し17a：前は、通話を進める部品が呼び出しのあとで落ちた・終えずに戻ったときは
                    # 数えなかった。⛔ 0129：前は、番号のせいでも回線の異常に数えた）
                    return Round(called=got.attempt_id, pause=UNRUNG_PAUSE if paused else None, free=True)
                if done is dial_exit.Outcome.NOT_PLACED:   # かけられなかった（「発信に失敗」）：回線のせいと分かれば数えて休む
                    return Round(pause=UNRUNG_PAUSE if _unrung(access, actor, tenant_id, line_id) else None, free=True)
                if done in (dial_exit.Outcome.LINE_IN_CALL, dial_exit.Outcome.WITHHELD):
                    # PC がかけようとしなかった（緊急停止・つながっていない・答えの期限を過ぎた）も休む（⛔ 2026-10-22 の見直し：
                    # 休まずに数秒ごとに次の行を予約し、どの行も「発信に失敗」にしていった）。まだ前の通話の途中：その回線を休ませる
                    # （休まず次の行へ進むと、行の回数と回線の1日の上限を数分で使い切りうる＝2026-10-18 の見直し）
                    return Round(pause=done.value, free=done is not dial_exit.Outcome.LINE_IN_CALL)
                # 予約証が使えなかった（緊急停止など）：次の回で、門から考え直す
                return Round(free=True)
            if got.reason in gate.LINE_REASONS:
                return Round(pause=got.reason)
            if got.reason in gate.CLIENT_REASONS:
                held.append(got.reason)   # この依頼者は、いまかけられない（ほかの依頼者を見る。どこもかけなければ休む）
                return None
            if got.reason in gate.BUSINESS_REASONS:
                capped.add(task["business_id"])
                newly = True
            if got.reason in gate.RUN_REASONS:
                runs.add(task["run_id"])
                newly = True
            if got.reason in gate.STAGE_REASONS and task["run_id"] not in staged:
                staged.add(task["run_id"])
                newly = True
        if not newly:
            return None


def loop(access: Access, tenant_id: UUID, line: UUID, holder: str, driver: CallDriver, runner: plugins.CallRunner,
         stop: Any) -> None:
    """回線ごとの処理（中央の見守り役が、別の流れで動かす）。stop が立つまで繰り返す（いまの通話は最後まで進めてから止まる）。"""
    pause = int(access.in_tenant(ACTOR, tenant_id, lambda tx: settings.system_value(tx, "gate.denied_retry_wait")))
    stuck = Stuck()
    while not stop.is_set():
        try:
            # プログラムとデータベースの版が合わなければ、新しい電話をかけない（中央の見守り役が立ち上げたあとでデータベースが
            # 新しくなった、でも、かける処理が自分の版で確かめる。supervisor.updates）
            if access.in_tenant(ACTOR, tenant_id, updates.current) != updates.SAME:
                stop.wait(IDLE_SECONDS)
                continue
            got = one_round(access, ACTOR, tenant_id, line, holder, driver, runner)
            wait = pause
            if got.pause == "line_stopped":   # この回線が止まっている：テスト番号で再開してよいかを確かめる（時が来ていれば）
                checked = _check_stop(access, ACTOR, tenant_id, line, driver)
                if checked is not None:
                    wait = max(1.0, min(pause, checked))
            if stuck.seen(got, time.monotonic()):
                _line_fault(access, ACTOR, tenant_id, line,
                            f"回線が「通話中」のまま、かけられません（{int(STUCK_AFTER)}秒以上。電話の部品が答えない場合も）")
            if got.called is None or got.pause:
                stop.wait(wait if got.pause else IDLE_SECONDS)
        except Exception as e:  # noqa: BLE001 記録して、少し待ってから続ける（止めない）
            report(access, tenant_id, "回線ごとの処理", e, line_id=line)
            stop.wait(IDLE_SECONDS)
