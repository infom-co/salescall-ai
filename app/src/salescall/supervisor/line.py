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
   （`_unrung` の1か所。だれのせいかはデータベースの salescall.unrung_blame が、同じ回線のあとの証拠で決める＝0129・0143：あとで
   この回線がお客様の電話を鳴らせた・テスト番号が鳴った＝番号のせい／テスト番号が鳴らなかった・回線を使わなくした＝回線のせい／
   それまでは、まだ分からない）。数えたら、その回線を休ませる（一時中止。続けば、エラーの処置の表のとおり、その回線を止めて管理者と
   契約者に知らせる）。番号のせい・まだ分からないときは、回線を休ませず、回線の異常にも数えない。まだ分からない電話の行は、決まるまで
   締めずに休み（settle.settle。休むのはその電話をかけた回線でだけ＝ほかの回線はかけてよい。0144）、回線は次の行へ進む（2026-10-24 管理者：「回線か番号かどちらかわからない場合は、その行に固執するのでなく、
   1回目から、次の行に進むようにしてください。次の行に進んで、次の行がつながればすぐに回線の問題か番号の問題か判断できますよね」）。
   ⛔ 0129〜0141：決定に無い「違う番号が2つ続けて鳴らなかったら回線のせい」と「テスト番号で決める」印の2つの道があり、元気な回線を
   止め（見直し21a の場面 a3・a7・a2）、テスト番号を余分に鳴らした（場面 a9）。回線がまだ前の通話の途中・PC の部品がかけようとしなかった
   ときは、その回線を休ませる（数えない）。声が聞き取れずに終えた電話が違うお客様で続いたときも、回線の異常として数える（記録の理由の文は
   理由ごと＝`_unrung`）。
4. **回線が「通話中」のまま（電話の部品が「通話中」と答え続ける・答えられない）で、かけられない状態が `STUCK_AFTER` 続いたら、
   回線の異常として数える**（`Stuck` の1か所。数える入口は、かけられなかったときと同じ `_line_fault`＝続けば、処置の表のとおり、
   その回線を止めて知らせる。⛔ 2026-10-22 の見直し15：前は、通話中を理由に休み続けるだけで、だれにも知らされなかった）。
5. **通話の長さでは切らない**（2026-10-24 ユーザー：時間で業務を打ち切らない）。通話ごとの口（`_Call`＝plugins.CallControl）を通話を
   進める部品へ渡し、その「動いています」の知らせ（`beat`）が設定 call.frozen_after（初め3秒。2026-10-24 ユーザー：「20秒でも長すぎる
   でしょ」）途切れたら、こちらのプログラムが固まったとみなし、通話を途中で切るときの2つの一言で終え、結果不明にして理由（frozen）を
   残す。通話を進める部品が見つけた甚大な問題（`end_for_trouble`）・通話を進める部品が落ちた／終えずに戻った（runner_error）も同じ道
   （2026-10-24 ユーザー決定）。どれも、こちらのせい（番号にも回線にも行の回数にも数えない＝attempts.blame の ours。0136・0139）。
   おわびして切り終えたら、こちらのプログラムの不具合として伝える（core/troubles.after_trouble の1か所：記録・契約者と管理者への
   知らせ・直す担当への修正依頼。同じ行で2回続けばその行を休ませ、違う3つの行で同じ理由なら契約者の新しい電話を止める＝2026-10-25
   ユーザー。⛔ 0139 の前は「見守り役の誤り」として CL-2 へだけ伝え、同じ行で3回目からはふつうの鳴らなかった電話として数えた）。
   見張るのは通話が進行中のあいだだけ・おわびして切り終えるまで「話している」・通話の途中の書き込みは通話ごとの書き手（`_Call`）。
   見張りはデータベースを通さない（⛔ 0130：前はデータベースの「進んだ時刻」を 90 秒見ていた）。**「動いています」が続いているあいだは、
   書き込みが遅くても終えない**（見直し22 の 1）。書き手の止まり（書き込み1つが call.frozen_after より長く終わらない）でデータベースが
   止まったとしておわびしてすぐ終える（storage_stuck。直す担当 CL-2。0143）のは、通話の部品が書き手を待っているあいだ（flush・終わりの
   移りの書き切り）と、通話の部品が戻ったあとだけ。待っているあいだは、書き手が進んでいれば途切れに数えない。
6. **番号のせいか回線のせいかがまだ分からない続き**があり、その回線がまもなく（テスト番号をあける長さの並びの1つ目＝初め15分。
   core/ring_test.decide_within。0141）かけられる行が無いときだけ、テスト番号で試して決める（`_decide_if_stuck` の1か所→core/ring_test。
   違う番号の数にはよらない＝0143）。まもなくかけられる行があれば、その電話の結果で決まるので、テスト番号は鳴らさない。テスト番号で
   決められないとき（無い・鳴らしてよい時間の外・見分けられない）は決めずに待つ（無い・見分けられないは、すぐ1回知らせる。PC の部品が
   試さないまま、あける長さの最初を過ぎたときも同じ＝core/ring_test。見直し22 の 9）。
   **回線の異常で止めた回線**は、毎回の頭で、テスト番号で再開してよいかを確かめる
   （`_check_stop`。1回鳴れば解く＝core/watch.record_check。2026-10-25 夜 管理者「鳴ったら、テストＯＫなだけ」）。テスト番号を鳴らすのは、
   その回線の電話の部品を持つ、ここだけ。
締め（判定・次の動き）は、見守り役の決まった間隔の仕事がする（jobs の settle）。
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from salescall.core import (attempts, conversation_io, dial_exit, gate, health, next_action, ring_test, scheduler, settings,
                            troubles, watch)
from salescall.core.access import Access, Actor, Tx
from salescall.core.errors import ErrorReport
from salescall.link.protocol import APOLOGY_WAIT
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
WATCHER_TROUBLES = ("frozen", "runner_error", "storage_stuck")   # 通話ごとの口だけが決める、終えた理由（通話を進める部品は使わない）
# 通話の部品が頼んだ移りのうち、通話が終わりに向かっているもの（話し終えた ending・ended、出なかった・話し中・つながらなかった など。
# conversation_io.move_work で頼まれたもの）。頼まれていれば、`_Call` は通話を終えると決める前に、書き手がそれを書き切るのを（書き手が
# 進むあいだ）待ってから、データベースで進行中かを見る（見直し22 の 3。⛔ 0143：頼んだ時点で「終わりに向かっている」とし、書き手が
# 断った移り・書く前に固まった電話でも、お詫びも終わりの記録もしなかった＝場面 w22b・w23）
FINISHING = frozenset({"ending", "ended", "busy", "no_answer", "call_failed", "dropped", "technical_error"})


@dataclass(frozen=True)
class Round:
    """1回分の結果。called＝かけた試行、pause＝回線の理由（その回線をしばらく休む。かけたあとでも、回線のせいで鳴らなかったら
    休む）。free＝電話の部品が「通話中ではない」と答えた（発信の出口が、通話中かを聞いて先へ進んだ）。"""
    called: UUID | None = None
    pause: str | None = None
    free: bool = False


@dataclass
class Stuck:
    """回線が「通話中」のまま、かけられない状態の見張り（判断はこの1か所）。since＝続けて「通話中」と分かった始まり（数えるたびに
    数え直す）。began＝かけられなくなった始まり（数えても数え直さない。状態の欄の影へ渡す E17 の時刻＝core/health.stuck）。"""
    since: float | None = None
    began: float | None = None

    def seen(self, got: Round, now: float) -> bool:
        """1回分の結果を見る。「通話中」が STUCK_AFTER 続いたら True（数えたら、また初めから数える＝続けば、また数える）。
        「通話中ではない」と分かったら数え直す。どちらとも分からない回（かける行が無い・門が断った）は、そのまま。"""
        if got.pause == dial_exit.Outcome.LINE_IN_CALL.value:
            if self.since is None:
                self.since = now
                self.began = now if self.began is None else self.began
            elif now - self.since >= STUCK_AFTER:
                self.since = now
                return True
        elif got.free:
            self.since = None
            self.began = None
        return False


class _Call:
    """通話ごとの口（plugins.CallControl。判断はこの1か所）：通話を進める部品の「動いています」の知らせ（`beat`）を WATCH_EVERY ごとに
    見て、frozen_after 途切れたら、こちらのプログラムが固まったとみなして終える（frozen）。通話を進める部品が見つけた甚大な問題
    （`end_for_trouble`）・通話を進める部品が落ちた／終えずに戻った（runner_error。`after_runner`）・データベースへの書き込みが進まない
    （storage_stuck）も同じ道（`_end`）：試行がまだ進行中なら、結果不明にして理由と根拠の文を残し（attempts.end_for_trouble）、通話を
    途中で切るときの2つの一言で終える（dial_exit.abandon）。1回だけ。
    - **見張るのは通話が進行中のあいだだけ**（0136）：知らせが途切れても、試行がもう終わっていれば何もしない（お客様が切ったあとの
      片付けの途中で、終わった通話におわびを流さない・こちらのせいの印を付けない）。通話を進める部品が戻れば、見張りをやめる。
      **進行中かは、データベースで決める**：通話の部品が終わりの移り（`FINISHING`＝話し終えた ending・ended など）を
      conversation_io.move_work で頼んでいれば、書き手がそれを書き切るのを（書き手が進むあいだ）待ってから見る（見直し22 の 3。決まった
      秒数は待たない。頼んでいなければ、すぐ見る＝本当に固まった通話には、すぐおわびする。⛔ 0141：仮の設定 call.end_write_wait＝5秒まで
      書き手を待った。⛔ 0143：頼んだ時点で終わりに向かっているとし、断られた移り・書く前に固まった電話を、進行中のまま黙って残した）。
      まだ進行中なら（お別れの言葉の途中＝ending も）、終わりの記録を書き、おわびして切る。
    - **「動いています」が続いているあいだは、書き込みが遅くても終えない**（見直し22 の 1。⛔ 0143：書き込み1つが frozen_after より
      長いだけで、元気な通話をおわびで切り、3行で契約者の電話を止めた＝場面 w20・w25）。
    - **通話の部品が書き手を待っているあいだ**（`flush`）は、書き手が進んでいれば（書き込みが1つずつ終わっていれば）知らせの途切れに
      数えない（0143。⛔ 0141：flush で待つあいだ知らせを出せず、データベースが遅いと元気な通話を固まったとして切った＝見直し21a の 7）。
    - **書き手が進まない**（`_stalled`＝いま書いている書き込み1つが frozen_after より長く終わらない）のが、通話の部品が書き手を待って
      いるあいだ（`flush`・終わりの移りの書き切り）か、通話の部品が戻ったあとなら、データベースが止まったとして、記録を待たずにすぐ
      おわびして終え（storage_stuck。出どころ「保存」＝直す担当 CL-2。0143）、そのあとで記録する（書き手が試行を終わった状態へ移せて
      いれば＝`finishing`、おわびは流さない）。
    - **おわびして切り終えるまで「話している」**（supervisor.call_live の1か所。`live`）：止めるとき・PC のログインを外したときに、
      おわびの途中で切らない。切り終えたら外れる（固まった部品が戻らなくても、止める処理が待ち続けない）。
    - **戻らない**（`abandoned`。0141・0143）：(1) 終えると決めたあと、おわびを流し終える長さ（protocol.APOLOGY_WAIT）と frozen_after を
      過ぎても通話を進める部品が戻らない (2) 通話を進める部品が戻ったあと（か、おわびして切り終えたあと）、書き手が進まない。中央の
      見守り役がその回線の流れを捨てて立て直し、契約者と管理者に知らせ、直す担当（(1) 会話＝CL-1・(2) 保存＝CL-2。`stuck_source`）へ
      修正依頼を出す（supervisor/main.py）。⛔ 0141：仮の設定 call.stuck_after（60秒）で決め、戻ったあとの書き手の待ちは上限なしで、
      書き込みがデータベースで止まると回線が黙って止まった（見直し21a の 5・21b の 1）。
    - **データベースへの書き込みは、通話ごとの書き手**（`write`。頼んだ順に、別の流れで書く）：通話を進める流れがデータベースの
      書き込みを待って止まり、知らせが途切れて、元気な通話をおわびして切ることが無いように（0136）。**書けなかった分は書き手が持ち、
      `flush` で通話の部品へ返す**（plugins.WriteFailed。0141）。データベースの誤りは、そのときに見守り役の誤りとしても記録する
      （受け口の決まりに合わない誤り＝conversation_io.REFUSED は、頼んだ側が直すもの）。通話を進める部品が戻ったら、書き手が進むあいだ
      書き終えるまで待ってから締め、返せなかった分を記録する。
    かける前から終わるまで、supervisor の1か所（call_live）に載る（かけた直後に閉じない）。進んでいる通話は、どれだけ長くても切らない
    （2026-10-24 ユーザー：時間で業務を打ち切らない）。"""

    def __init__(self, access: Access, actor: Actor, tenant_id: UUID, client_id: UUID, line_id: UUID, attempt_id: UUID,
                 driver: CallDriver, limits: CallLimits) -> None:
        self.access, self.actor, self.tenant_id, self.client_id = access, actor, tenant_id, client_id
        self.line_id, self.attempt_id, self.driver = line_id, attempt_id, driver
        self.frozen_after = limits.frozen_after
        self.last = time.monotonic()
        self.ended = False                 # 終えると決めた（1回だけ）
        self.end_at: float | None = None   # 終えると決めた時刻
        self.finishing = False             # 書き手が、試行を終わった状態（ended_at のある状態）へ移せた（書けたあとに立てる）
        self.troubled = False              # こちらのプログラムの不具合として終え、伝えた（同じ出来事を見守り役の誤りに重ねない）
        self.over_at: float | None = None  # おわびして切り終えた時刻（終えると決めて、進行中でなかったときも）
        self._over = threading.Event()    # おわびして切り終えた
        self._closed = False               # 片付けた（このあとは終えない）
        self._lock = threading.Lock()
        self._done = threading.Event()    # 見張りをやめる（通話を進める部品が戻った・片付けた）
        self.thread = threading.Thread(target=self._run, name=f"通話の見張り {attempt_id}", daemon=True)
        self._writes: queue.Queue[Callable[[Tx], Any] | None] = queue.Queue()
        self._writer: threading.Thread | None = None
        self._progress = threading.Condition()   # 書き手の進み（頼んだ数・書き終えた数・書けなかった分）
        self._asked = 0                          # 頼まれた書き込みの数（通し）
        self._written = 0                        # 書き終えた（書けなかったも）数
        self._writing_since: float | None = None   # いま書いている書き込みを始めた時刻（書いていなければ None）
        self._waiting = 0                        # 通話の部品が書き手を待っている（flush の途中）
        self._finish_ticket = 0                  # 終わりの移り（FINISHING）を頼んだ書き込みの番号（通し。0＝頼んでいない）
        self._failed: list[tuple[Callable[[Tx], Any], BaseException]] = []   # 書けなかった分（まだ通話の部品へ返していない）

    @property
    def live(self) -> bool:
        """「話している」か（おわびして切り終えるまでは話している）。"""
        return not (self.ended and self._over.is_set())

    @property
    def abandoned(self) -> bool:
        """戻らない（1か所。中央の見守り役が読む＝supervisor.stuck_call）：(1) 終えると決めたあと、おわびを流し終える長さ（APOLOGY_WAIT）と
        frozen_after を過ぎても、通話を進める部品が戻らない (2) 通話を進める部品が戻ったあと（か、おわびして切り終えたあと）、書き手が
        進まない（`_stalled`）。"""
        now = time.monotonic()
        end = self.end_at
        if end is not None and not self._done.is_set() and now - end >= APOLOGY_WAIT + self.frozen_after:
            return True
        return (self._done.is_set() or self._over.is_set()) and self._stalled(now)

    @property
    def stuck_source(self) -> str:
        """戻らない理由の出どころ（直す担当を決める＝core/errors の表）：書き手が進まない＝保存（storage）・ほかは通話の部品（runner）。"""
        return "storage" if self._stalled() else "runner"

    def beat(self) -> None:
        self.last = time.monotonic()

    def end_for_trouble(self, reason: str, why: str | None = None) -> None:
        if reason not in attempts.TROUBLES or reason in WATCHER_TROUBLES:   # 見張りの理由を決めるのは、この口だけ
            raise ValueError(f"通話を終える理由は {tuple(t for t in attempts.TROUBLES if t not in WATCHER_TROUBLES)} のどれかです："
                             f"{reason!r}")
        if why is not None and not isinstance(why, str):
            raise TypeError("根拠の文（why）は短い文字で渡します（お客様の言葉・名前は入れない）")
        self._end(reason, "通話を進める部品が、その電話の甚大な問題を知らせました", why)

    def write(self, work: Callable[[Tx], Any]) -> None:
        """データベースへの書き込み（その依頼者の取引で work(tx)）を、通話ごとの書き手に頼む（待たずに戻る。頼んだ順に書く。書けなかった
        分は `flush` で返す）。頼めない形（呼べないもの）は、その場で TypeError。状態の移り（conversation_io.move_work）のうち、通話が
        終わりに向かう移り（`FINISHING`）は、その番号を覚える（`_end` が、書き手がそれを書き切るまで待ってから決める）。"""
        if not callable(work):
            raise TypeError("書き込みは、取引を受け取る関数（work(tx)）で頼みます")
        with self._lock:
            if self._writer is None:
                self._writer = threading.Thread(target=self._write_loop, name=f"通話の書き手 {self.attempt_id}", daemon=True)
                self._writer.start()
        with self._progress:
            self._asked += 1
            if isinstance(work, conversation_io.MoveWork) and work.to_state in FINISHING:
                self._finish_ticket = self._asked
        self._writes.put(work)

    def flush(self) -> None:
        """頼んだ書き込みを、すべて書き終えるまで待つ。待つあいだは、書き手が進んでいれば知らせの途切れに数えない（見張りが読む）。
        書けなかった分があれば plugins.WriteFailed で返す（返した分は手放す）。"""
        with self._progress:
            self._waiting += 1
        try:
            self._writes.join()
        finally:
            with self._progress:
                self._waiting -= 1
            self.last = max(self.last, time.monotonic())   # 待っていたあいだは、途切れに数えない
        failed = self._take_failed()
        if failed:
            raise plugins.WriteFailed(failed)

    def _take_failed(self) -> tuple[tuple[Callable[[Tx], Any], BaseException], ...]:
        with self._progress:
            failed, self._failed = tuple(self._failed), []
        return failed

    def _stalled(self, now: float | None = None) -> bool:
        """書き手が進まないか（1か所）：いま書いている書き込み1つが frozen_after より長く終わらない（データベースが止まった）。"""
        since = self._writing_since
        return since is not None and (time.monotonic() if now is None else now) - since > self.frozen_after

    def _drain(self, ticket: int | None = None) -> bool:
        """いま頼まれている書き込み（ticket＝その番号の書き込みまで）を書き終えるまで待つ（書き手が進んでいるあいだは待ち続ける。決まった
        秒数で打ち切らない）。書き手が進まなくなったら（`_stalled`）、待つのをやめて False。"""
        with self._progress:
            ticket = self._asked if ticket is None else ticket
            while self._written < ticket:
                if self._stalled():
                    return False
                self._progress.wait(WATCH_EVERY)
        return True

    def _write_loop(self) -> None:
        while True:
            work = self._writes.get()
            try:
                if work is None:
                    return
                self._writing_since = time.monotonic()
                try:
                    done = self.access.in_client(self.actor, self.tenant_id, self.client_id, work)
                except Exception as e:  # noqa: BLE001 書けなくても通話は止めない（持って、flush で返す）
                    with self._progress:
                        self._failed.append((work, e))
                    if not isinstance(e, conversation_io.REFUSED):   # データベースの誤り：見守り役の誤りとしても記録する
                        report(self.access, self.tenant_id, "通話の書き込み", e, line_id=self.line_id)
                else:
                    # 試行を終わった状態へ移せた（終わったかはデータベースの1か所＝終わった時刻 ended_at）：データベースを読めなくても、
                    # おわびを流さない（`_end`。書けたあとに立てる＝見直し22 の 3）
                    if isinstance(work, conversation_io.MoveWork) and done["ended_at"] is not None:
                        self.finishing = True
            finally:
                with self._progress:
                    self._writing_since = None
                    if work is not None:
                        self._written += 1
                    self._progress.notify_all()
                self._writes.task_done()

    def _end(self, reason: str, what: str, why: str | None = None, *, drained: bool = False) -> bool:
        """通話を終える（1か所）。試行がまだ進行中なら、理由と根拠の文（why）を残して結果不明にし、おわびして切り（True）、切り終えて
        から、こちらのプログラムの不具合として伝える（core/troubles.after_trouble の1か所＝記録・契約者と管理者への知らせ・直す担当への
        修正依頼・同じ行で続けば休ませる・違う行で続けば止める。2026-10-25 ユーザー）。もう終わっていれば何もしない。
        進行中かはデータベースで見る。通話の部品が終わりの移り（`FINISHING`）を頼んでいれば、書き手がそれを書き切るまで（書き手が進む
        あいだ）待ってから見る（drained＝頼まれた書き込みを、もう書き終えた）。終わりの移りを頼んだあとでも、試行がまだ進行中なら（断られた
        移り・お別れの言葉の途中で固まった）、終わりの記録を書き、おわびして切る（見直し22 の 3。⛔ 0143：頼んだ時点の手元の印だけで
        終わりに向かっているとし、何も記録せず、試行が進行中のまま残って回線が黙って止まった＝場面 w22b・w23）。データベースが止まった
        （storage_stuck。書き切りを待つうちに書き手が進まなくなった・いま書き手が止まっている、も）ときは、データベースを読まずに、記録を
        待たずに先におわびして切る（書き手が試行を終わった状態へ移せていれば＝`finishing`、おわびは流さない）。what＝何が起きたか（決まった
        言葉と誤りの種類の名前だけ）。"""
        with self._lock:
            if self.ended or self._closed:
                return False
            self.ended = True
            self.end_at = time.monotonic()
        if reason != "storage_stuck" and not drained and (not self._drain(self._finish_ticket) or self._stalled()):
            reason, what, why = "storage_stuck", f"データベースへの書き込みが進みません（{what}）", None
        recorded = False
        stuck = reason == "storage_stuck"

        def record(tx: Tx) -> tuple[bool, bool]:   # 終えた・こちらの不具合か（決まりはデータベースの trouble_ours の1か所。0141）
            ended = attempts.end_for_trouble(tx, self.attempt_id, reason, why)
            return ended, ended and attempts.is_ours(tx, self.attempt_id)

        def record_or_report() -> tuple[bool, bool]:
            try:
                return self.access.in_client(self.actor, self.tenant_id, self.client_id, record)
            except Exception as e:  # noqa: BLE001 記録できない：進行中とみなす（お客様を無言で放置しない）
                report(self.access, self.tenant_id, "通話を終える記録", e, line_id=self.line_id)
                return True, False
        try:
            if stuck:   # データベースが止まった：記録を待たずに、すぐおわびして切る（書き手が終わった状態へ移せていれば、流さない）
                live = not self.finishing
                if live:
                    dial_exit.abandon(self.driver)
            else:
                live, recorded = record_or_report()
                if live:
                    dial_exit.abandon(self.driver)   # 通話を途中で切るときの2つの一言を流して切る
        finally:
            self.over_at = time.monotonic()
            self._over.set()
        if stuck:   # おわびのあとで記録する（まだ進行中なら。データベースが戻るまで、この流れが待つ）
            _, recorded = record_or_report()
        # お客様へのおわびを待たせないよう、切り終えてから伝える（伝えられなくても、通話の処理は止めない）。こちらの不具合でない理由
        # （声が聞き取れない）は、ここでは伝えない（途中で切れた電話と同じ。違うお客様で続けば、回線ごとの処理が回線の異常として数える＝`_unrung`）
        if recorded:
            self.troubled = True
            try:
                troubles.after_trouble(self.access, self.actor, tenant_id=self.tenant_id, client_id=self.client_id,
                                       line_id=self.line_id, attempt_id=self.attempt_id, reason=reason, what=what)
            except Exception as e:  # noqa: BLE001
                report(self.access, self.tenant_id, "こちらの不具合の記録", e, line_id=self.line_id)
        return live

    def start(self) -> None:
        """「話している」に載せる（かける前から。supervisor.call_live）。"""
        call_began(self.line_id, self)

    def watch(self) -> None:
        """かけたあと、知らせの見張りを始める（途切れの長さは、ここから数える）。"""
        self.last = time.monotonic()
        self.thread.start()

    def after_runner(self, error: BaseException | None) -> None:
        """通話を進める部品が戻った（error＝外へ出た誤り）：見張りをやめ、まだ終えると決めていなければ、頼まれた書き込みを書き手が進む
        あいだ書き終えてから、試行がまだ進行中なら（落ちた・終えずに戻った）こちらのせい（runner_error）で終える（1か所。⛔ 0136 の前は
        理由を残さず、回線のせい・番号のせいに数えうる形で終えた）。書き手が進まなくなったら、データベースが止まったとして、すぐおわびして
        終える（storage_stuck。書き手が試行を終わった状態へ移せていれば、おわびは流さない＝`_end`）。そのあと書き終えるまで待つ（進まない
        あいだは `abandoned`＝中央の見守り役が立て直す）。通話の部品へ返せなかった書けなかった分は、記録する（データベースの誤りは、
        書けなかったときに記録済み）。"""
        self._done.set()
        with self._lock:
            ending = self.ended
        if ending:
            self._over.wait()   # 見張り・通話を進める部品が終えている途中：切り終えるまで待つ
        elif self._drain():
            self._end("runner_error", "通話を終えないまま戻りました" if error is None else f"落ちました（{type(error).__name__}）",
                      None if error is None else type(error).__name__, drained=True)
        else:   # 書き手が進まない：データベースが止まった
            self._end("storage_stuck", "データベースへの書き込みが進みません（通話の部品が戻ったあと）")
        self._writes.join()
        for _, e in self._take_failed():
            if isinstance(e, conversation_io.REFUSED):   # 受け口の決まりに合わず、通話の部品が受け取らずに戻った
                report(self.access, self.tenant_id, "通話の書き込み（受け口が断ったまま、通話の部品が受け取らなかった）", e,
                       line_id=self.line_id)
        # こちらのプログラムの不具合として伝えた電話（固まったあとに落ちた も）は、同じ出来事なので見守り役の誤りに重ねない（記録・知らせ・
        # 修正依頼は1つの入口で）。通話はもう終わっていた（こちらの不具合で終えた電話ではない）ときだけ、見守り役の誤りとして記録する
        if error is not None and not self.troubled:
            report(self.access, self.tenant_id, "通話を進める部品", error, line_id=self.line_id)

    def stop(self) -> None:
        """見張りと書き手を止め、「話している」から外す（何度呼んでもよい。おわびして切っている途中なら、切り終えるまで待つ。書き手が
        進まないあいだは待ち続け、中央の見守り役が `abandoned` で立て直す）。"""
        with self._lock:
            self._closed = True
            ending = self.ended
        self._done.set()
        if ending:
            self._over.wait()
        self._writes.join()
        if self._writer is not None:
            self._writes.put(None)
        call_over(self.line_id, self)

    def _run(self) -> None:
        while not self._done.wait(WATCH_EVERY):
            now = time.monotonic()
            if self._waiting:   # 通話の部品が書き手を待っている（flush）：書き手が進んでいれば、途切れに数えない
                if self._stalled(now):   # 書き込み1つが frozen_after より長く終わらない：データベースが止まった
                    self._end("storage_stuck", f"データベースへの書き込みが {self.frozen_after:g} 秒より長く終わりません")
                    return
                continue
            # 「動いています」が続いているあいだは、書き込みが遅くても終えない（見直し22 の 1。⛔ 0143：書き込み1つが遅いだけで、元気な
            # 通話をおわびで切った＝場面 w20・w25）。途切れたら固まった（書き手が止まっていれば、`_end` がデータベースの止まりとして終える）
            if now - self.last > self.frozen_after:
                self._end("frozen", f"「動いています」の知らせが {self.frozen_after:g} 秒途切れました",
                          f"「動いています」の知らせが {self.frozen_after:g} 秒途切れた")
                return


@dataclass(frozen=True)
class CallLimits:
    """通話ごとの口の長さ（秒。システムの設定）。frozen_after＝「動いています」が途切れたら固まったとみなす長さ（call.frozen_after）。
    書き手が進まないとみなす長さも同じ値。戻らないとみなす長さは、これと protocol.APOLOGY_WAIT から決める（0143。仮の設定
    call.end_write_wait・call.stuck_after は外した）。"""
    frozen_after: float


def frozen_after(tx: Any) -> float:
    """通話を進める部品が固まったとみなす長さ（秒。設定 call.frozen_after の1か所。2026-10-24 ユーザー：3秒）。"""
    return float(settings.system_value(tx, "call.frozen_after"))


def call_limits(tx: Any) -> CallLimits:
    """通話ごとの口の長さ（1か所）。"""
    return CallLimits(frozen_after(tx))


def _line_fault(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, what: str, *,
                client_id: UUID | None = None, attempt_id: UUID | None = None) -> None:
    """回線の異常として数える（入口はこの1か所。続けば、エラーの処置の表のとおり、その回線を止めて知らせる）。"""
    watch.after_error(access, actor, ErrorReport("line", None, what), tenant_id=tenant_id, line_id=line_id,
                      client_id=client_id, attempt_id=attempt_id)


# 回線の異常として数えるときの理由の文（`_unrung` が理由ごとに選ぶ。0143。⛔ 0141：声が聞き取れずに止めた回線も「鳴りませんでした」と記録した）
UNRUNG_LINE = "この回線で、お客様のところで電話が鳴りませんでした（テスト番号も鳴らなかった・回線を使わなくした＝回線のせい）"
POOR_AUDIO_LINE = "この回線で、違うお客様との電話が続けて、声が聞き取れずに終わりました（回線の声の受け渡しのせい）"


def _unrung(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID) -> bool:
    """その回線の、回線のせいと分かったのに、まだ回線の異常として記録していない電話を、1本ずつ回線の異常として数える（1か所。
    attempts.line_faults_unrecorded。続きが回線のせいと決まったときは、その続きの分をまとめて数える）。記録の理由の文は理由ごと（鳴らな
    かった＝`UNRUNG_LINE`・声が聞き取れない電話が違うお客様で続いた＝`POOR_AUDIO_LINE`）。数えたら True（その回線を休ませる）。番号の
    せい・まだ分からないときは数えない（False）。⛔ 0141：「テスト番号で決める」印もここで置いていた（0143 で外した）。"""
    faults = access.in_tenant(actor, tenant_id, lambda tx: attempts.line_faults_unrecorded(tx, line_id))
    for f in faults:
        _line_fault(access, actor, tenant_id, line_id, POOR_AUDIO_LINE if f["by_audio"] else UNRUNG_LINE,
                    client_id=f["client_id"], attempt_id=f["attempt_id"])
    return bool(faults)


def _claim(tx: Any, task: dict[str, Any], line_id: UUID, holder: str, tenant_id: UUID, client_id: UUID) -> Any:
    got = gate.claim(tx, task_id=task["task_id"], line_id=line_id, lease_holder=holder)
    if isinstance(got, gate.Denied) and got.reason not in gate.PLAN_KEPT:   # 行の予定を書き直さない理由は gate の1か所
        next_action.after_denial(tx, task["task_id"], got, tenant_id=tenant_id, client_id=client_id,
                                 business_id=task["business_id"])
    return got


def one_round(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, holder: str, driver: CallDriver,
              runner: plugins.CallRunner) -> Round:
    # 毎回の頭：状態の欄の「次に動く時刻」（E10）・声が通ると分かっている回線が空いた（E18）を、状態の欄へ（DESIGN.md 17章の3段目＝影。
    # 記録だけ。この回の動きは変えない。影が失敗しても止めない）
    health.in_tenant(access, actor, tenant_id, health.tick, line_id)
    clients = access.in_tenant(actor, tenant_id, lambda tx: scheduler.clients_for_line(tx, line_id))
    held: list[str] = []   # 依頼者の理由（同時の上限・無効）で断られた
    for promised_only in (True, False):   # 約束したかけ直しを、すべての依頼者について先に（目印で分けるのは due_tasks の1か所）
        for client_id in clients:
            got = _client_round(access, actor, tenant_id, client_id, line_id, holder, driver, runner, promised_only, held)
            if got is not None:
                if got.pause in gate.LINE_REASONS:   # この回線がいまかけられない：まもなくかけられる行も無い（決めにいく）
                    _decide_if_stuck(access, actor, tenant_id, line_id, clients, driver, line_blocked=True)
                return got
    # どこもかけられず、依頼者の理由で断られていた：回線の理由と同じく休む（⛔ 2026-10-22 の見直し：休まずに数秒ごとに門へ渡し
    # 直し、同時の上限のあいだ、断られた記録を増やし続けた）
    if held:
        return Round(pause=held[0])
    return _decide_if_stuck(access, actor, tenant_id, line_id, clients, driver)


def _decide_if_stuck(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, clients: list[UUID], driver: CallDriver,
                     *, line_blocked: bool = False) -> Round:
    """番号のせいか回線のせいかがまだ分からない続きを、止まり続けさせない（決めにいくかの判断はこの1か所。0136・0143）。2026-10-24 管理者：
    「回線か番号かどちらかわからない場合は、その行に固執するのでなく、1回目から、次の行に進むようにしてください。次の行に進んで、次の行が
    つながればすぐに回線の問題か番号の問題か判断できますよね。一つの行で止まり続けることがないようにしてください。」「その回線にかける行が
    残っていないときは、『テスト番号』で決めます。」
    決めにいくのは、その回線に、まだ分からない続き（attempts.line_pending）があり、その回線が「次の電話で決まるのを待つ長さ」
    （core/ring_test.decide_within＝テスト番号をあける長さの並びの1つ目。初め15分）のうちにかけられる行が無いときだけ
    （scheduler.soon_left の1か所。続きの番号の行・決まるのを待つ行・候補に出さない行・判定待ち・止めた範囲の行・時刻が先の行は数えない。
    line_blocked＝この回線が門の回線の理由でいまかけられない＝かけられる行は無い）。違う番号の数にはよらない（⛔ 0129〜0141：違う番号が
    2つ続けば回線のせい・テスト番号を先に鳴らす、の別の道があった＝0143 で外した）。まもなくかけられる行があれば、その電話の結果で決まる
    ので待つ（テスト番号は鳴らさない）。
    - テスト番号で試す（core/ring_test.run の1か所）：鳴った＝まだ分からなかった続きは番号のせい（鳴った電話と同じ区切り）・鳴らなかった＝
      回線のせい（回線の異常として数えて休む）。
    - 決まらない（テスト番号が無い・鳴らしてよい時間の外・見分けられない・鳴らす間がまだ・話している途中・PC の部品が試さなかった）ときは、
      決めずに待つ（休んでから試し直す）。テスト番号が無い・見分けられないは、core/ring_test.run が契約者と管理者にすぐ1回知らせる。
      ⛔ 0131〜0141：テスト番号で決められないとき、前の区切りで決めた（前に鳴った電話があれば番号のせい）＝壊れた回線のお客様の行を
      失いえた。"""
    pending = access.in_tenant(actor, tenant_id, lambda tx: attempts.line_pending(tx, line_id))
    if pending is None:
        return Round()
    if not line_blocked:
        if any(access.in_client(actor, tenant_id, c, lambda tx: scheduler.soon_left(
                tx, line_id, not_phones=pending["phones"])) for c in clients):
            return Round()
    tested = ring_test.run(access, actor, tenant_id, line_id, driver, "no_rows")
    if tested is ring_test.Result.SILENT:
        _unrung(access, actor, tenant_id, line_id)       # 回線のせいと決まった分を、回線の異常として数える
        return Round(pause=UNRUNG_PAUSE)
    if tested is ring_test.Result.RANG:
        return Round()
    return Round(pause=RING_TEST_PAUSE)


def _check_stop(access: Access, actor: Actor, tenant_id: UUID, line_id: UUID, driver: CallDriver) -> float | None:
    """回線の異常で止めた、この回線の停止の確かめ（2026-10-24 ユーザー：再開の確かめもテスト番号で「OK」）：テスト番号で確かめる停止
    （core/ring_test.checks_stop の1か所。テスト番号の無い契約者は確かめの部品＝supervisor.jobs）で、確かめの時が来ていれば、テスト番号で
    試し、鳴れば成功・鳴らなければ失敗として記録する（core/watch.record_check。1回鳴れば解く＝2026-10-25 夜 管理者「鳴ったら、テストＯＫ
    なだけ」。0143）。声が聞き取れずに止め直した回線は、確かめの時を、止めた時から並びの長さだけ遅らせる（core/watch.checkable の1か所。
    2026-10-29 管理者。0144）。見分けられない（core/ring_test.run が、その停止について1回知らせる）・話している途中・PC の部品が試さなかった・
    鳴らしてよい時間の外（設定 line.test_ring_hours。2026-10-24 ユーザー：テスト番号はいまユーザー自身の電話）なら記録しない
    （いつもの休みのあとで試し直す。時間の外は、時間の始まりまで鳴らさず、アプリが応答するかだけの確かめでも解かない＝朝まで待つ。
    記録しないので、確かめの間隔も伸びない）。返すのは、次に確かめてよいまでの秒（その停止が無い・決まらなかったときは None＝
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
                limits = access.in_tenant(actor, tenant_id, call_limits)
                call = _Call(access, actor, tenant_id, client_id, line_id, got.attempt_id, driver, limits)
                call.start()   # かける前から「話している」に載せる（かけた直後に、止める・ログインを外す処理が閉じない）
                try:
                    done = dial_exit.dial(access, actor, tenant_id, client_id, got.attempt_id, driver)
                    if done is dial_exit.Outcome.DIALED:
                        call.watch()
                        error: BaseException | None = None
                        try:
                            runner.run(access, actor, got, tenant_id=tenant_id, client_id=client_id, call=call)
                        except Exception as e:  # noqa: BLE001 その通話だけを、こちらのせいで終えて、次へ進む（止めない）
                            error = e
                        # 落ちた・終えずに戻った（試行がまだ進行中）：こちらのせいで、おわびして終える（`_Call.after_runner` の1か所。
                        # ⛔ 2026-10-22 の見直し：電話はつながったまま無言になり、回線は「通話中」で止まり続け、だれにも知らされなかった）
                        call.after_runner(error)
                finally:
                    call.stop()   # どの道でも「話している」から外す（残ると、止める・ログインを外す処理が待ち続ける）
                if done is dial_exit.Outcome.DIALED:
                    paused = access.in_client(actor, tenant_id, client_id, lambda tx: attempts.line_counted_after(
                        tx, got.attempt_id)) and _unrung(access, actor, tenant_id, line_id)
                    # お客様のところで鳴らずに終わった（せいがある＝attempts.blame。お客様が出る前に終わった結果不明＝数え方の
                    # line_failure など。どの回数にも数えない）・声が聞き取れずに終えた（0141。違うお客様で続けば回線の異常）：回線のせいと
                    # 分かれば回線の
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
            # 止めた回線の再開の確かめは、かける行の有る無しと分けて、毎回の頭で（⛔ 見直し19a：前は門が「止まっている」で断った
            # ときだけ見て、その回線にかける行が無いあいだ、止めた回線をテスト番号で確かめなかった）
            checked = _check_stop(access, ACTOR, tenant_id, line, driver)
            got = one_round(access, ACTOR, tenant_id, line, holder, driver, runner)
            wait = pause
            if checked is not None:   # 確かめを待つ停止がある：次に確かめてよいときより長く休まない
                wait = max(1.0, min(pause, checked))
            now = time.monotonic()
            if stuck.seen(got, now):
                _line_fault(access, ACTOR, tenant_id, line,
                            f"回線が「通話中」のまま、かけられません（{int(STUCK_AFTER)}秒以上。電話の部品が答えない場合も）")
                # かけられない（E17）を状態の欄へ（DESIGN.md 17章の3段目＝影。記録だけ）。時刻は、かけられなくなった始まり
                health.in_tenant(access, ACTOR, tenant_id, health.stuck, line, now - (stuck.began or now))
            if got.called is None or got.pause:
                stop.wait(wait if got.pause else min(wait, IDLE_SECONDS))
        except Exception as e:  # noqa: BLE001 記録して、少し待ってから続ける（止めない）
            report(access, tenant_id, "回線ごとの処理", e, line_id=line)
            stop.wait(IDLE_SECONDS)
