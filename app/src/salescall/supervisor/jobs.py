"""中央の見守り役の決まった間隔の仕事（DESIGN.md 5-4・5-5）。**仕事の一覧は、この表 `JOBS` の1か所**。持ち主 CL-2。

- 中央の見守り役（サーバー）が、すべての契約者に共通の仕事と、契約者ごとの仕事を**すべての契約者の分**する（2026-10-21 ユーザー承認：
  契約者の PC には電話をつなぐ部分だけを置く。PC が止まっていても、ほかの仕事・控えの見張りは止まらない）。契約者ごとの仕事は、
  その契約者の取引で、契約者ごとに別に回す（1つの契約者の誤りで、ほかの契約者を止めない）。
- どの仕事も、始める前に「仕事の借用」（leases.take_job。期限つき）を取る：同じ仕事を同時にしない。落ちれば期限で次が
  引き継ぐ。契約者ごとの仕事は「名前:契約者」、すべての契約者に共通の仕事は名前で借りる。
- どの仕事も、中で落ちても見守り役を止めない（「見守り役の誤り」として CL-2 へ修正依頼＝`report`。次の回にまたする）。
  行ごと・依頼者ごとに分けられる仕事は、1つが落ちても残りを続ける（1つの通話の締めの誤りで、ほかの締めを止めない）。
- 間隔の秒数は、仕事が受け持つものの速さから決めた（下の表の「なぜ」）。設定の値にしないのは、契約者が変えて得るものが無いため。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from salescall.core import agents, answer_rate, attempts, backup, calendar, health, leases, measure, next_action, settle, usage, watch
from salescall.core.access import Access
from salescall.supervisor import ACTOR, config, plugins, report
from salescall.supervisor.links import Links


@dataclass
class Context:
    """仕事の1回分の材料。tenant_id＝契約者ごとの仕事なら、その契約者（すべての契約者に共通の仕事は None）。"""
    access: Access
    cfg: config.Config
    holder: str
    links: Links
    tenant_id: UUID | None = None
    parts: dict[str, Any] = field(default_factory=dict)   # ほかの担当の部品（契約者をまたいで1回だけ作る）

    def part(self, spec: str | None) -> Any:
        """ほかの担当の部品（無ければ None。作るのは1回だけ）。"""
        if spec is None:
            return None
        if spec not in self.parts:
            self.parts[spec] = plugins.load(spec)
        return self.parts[spec]

    def for_tenant(self, tenant_id: UUID) -> "Context":
        return Context(self.access, self.cfg, self.holder, self.links, tenant_id, self.parts)


@dataclass(frozen=True)
class Job:
    name: str
    every: int                      # 何秒ごとか
    scope: str                      # tenant＝契約者ごとの仕事（すべての契約者について1つずつ）／system＝すべての契約者に共通の仕事。
                                    # 費用の見張りと控えは契約者ごと（2026-10-18 ユーザー）。共通は祝日の暦・全体の停止・データベース全体の控え
    lease: int                      # 借りる期限（秒）。仕事が終わるより長く（終われば返す）
    work: Callable[[Context], Any]
    wanted: Callable[[config.Config], bool] = lambda cfg: True   # する仕事か（ほかの担当の部品の設定があるか など）
    heavy: bool = False             # 長い仕事（控えの書き込み＝PC を待つ）。別の流れの組で回す（締め・後始末の順番を塞がない）

    def key(self, tenant_id: UUID | None) -> str:
        return self.name if self.scope == "system" else f"{self.name}:{tenant_id}"


def _clients(ctx: Context) -> list[UUID]:
    return backup.client_ids(ctx.access, ctx.tenant_id)


def _each(ctx: Context, what: str, items: list[Any], work: Callable[[Any], Any]) -> None:
    """1つずつ。1つが落ちても、記録して残りを続ける。"""
    for item in items:
        try:
            work(item)
        except Exception as e:  # noqa: BLE001
            report(ctx.access, ctx.tenant_id, what, e)


def recover_expired(ctx: Context) -> None:
    """借用の期限が切れた回線（前に動いていた中央の見守り役が落ちた）の、進行中の試行の後始末（DESIGN.md 4-2）。自分が借りている
    回線は除く（データベースに届かないあいだに期限が切れても、まだ話している自分の通話を終わらせない）。"""
    ctx.access.in_tenant(ACTOR, ctx.tenant_id, lambda tx: leases.recover(tx, except_holder=ctx.holder))


def settle_ready(ctx: Context) -> None:
    """締めを待っている試行（終わって、通話後の検証が済んだもの）を締める（判定 → 結果 → 次の動き。1本ずつ別の取引）。
    締めの処理は、締める前に、その電話の出来事（E1〜E5）をまだ状態の表へ渡していなければ渡す（core/settle.settle）。締めのあと、声が
    聞き取れずに終えた電話の行への分（E4）を渡す（`_settle_one`。DESIGN.md 17章）。"""
    def per_client(client_id: UUID) -> None:
        ready = ctx.access.in_client(ACTOR, ctx.tenant_id, client_id, settle.unsettled)
        _each(ctx, "締め", [r["attempt_id"] for r in ready],
              lambda a: ctx.access.in_client(ACTOR, ctx.tenant_id, client_id, lambda tx: _settle_one(tx, a)))
    _each(ctx, "締め（依頼者）", _clients(ctx), per_client)


def _settle_one(tx: Any, attempt_id: UUID) -> dict[str, Any]:
    """1本を締め、そのあとで、声が聞き取れずに終えた電話（E4）を、その行へ渡す（core/health.after_settle。「すぐのかけ直しが続けて2回の
    うちか」が、締めが残す「途中で切れたか」を読むため）。"""
    out = settle.settle(tx, attempt_id)
    health.after_settle(tx, attempt_id)
    return out


def rejudge_held(ctx: Context) -> None:
    """判定できずに待っている行を、原因が直っていれば判定し直す（DESIGN.md 7-2-2）。"""
    _each(ctx, "判定し直し", _clients(ctx),
          lambda c: ctx.access.in_client(ACTOR, ctx.tenant_id, c, settle.rejudge_held))


def reopen_revoked(ctx: Context) -> None:
    """発信禁止で門が終えた行のうち、そのあと登録が取り消されたものを、終える前の予定に戻す（依頼者ごと。契約者の共通の台帳の取り消しは、
    すべての依頼者の行に効くので、ここで戻す。どの行を戻すかは core/next_action.reopen_revoked の1か所。2026-10-30 管理者のご決定 (7)）。"""
    _each(ctx, "発信禁止の取り消しで行を戻す", _clients(ctx),
          lambda c: ctx.access.in_client(ACTOR, ctx.tenant_id, c, lambda tx: next_action.reopen_revoked(
              tx, cause="発信禁止の取り消し：終える前の予定に戻す")))


def draw_samples(ctx: Context) -> None:
    """誤判定率を測るための抜き取り（業務ごと・週ごとに1回。するかは measure の1か所が決める。Intent.md 16-3）と、測らない通話の
    検証の印を外すこと（話していない通話・かけずに取り消した予約の録音を、ふつうの決まりに戻す）。"""
    def work(tx: Any) -> None:
        measure.release_unmeasured(tx)
        measure.draw(tx)
    _each(ctx, "抜き取り", _clients(ctx), lambda c: ctx.access.in_client(ACTOR, ctx.tenant_id, c, work))


def answer_rates(ctx: Context) -> None:
    """番号ごとの応答率の見張り（その契約者の回線すべて。落ちたら知らせるだけ。Intent.md 8-6）。"""
    ctx.access.in_tenant(ACTOR, ctx.tenant_id, lambda tx: answer_rate.watch(tx, ctx.tenant_id))


def verify_pending(ctx: Context) -> None:
    """通話後の検証（CL-1 の部品）：待ちの通話を確かめる。録音が届かないまま待つ時間を過ぎた通話は「録音なし」で済ませる
    （attempts.finish_without_recording。CL-1 の部品が呼ぶ）。"""
    ctx.part(ctx.cfg.verifier).run_pending(ctx.access, ACTOR, tenant_id=ctx.tenant_id)


def verify_watch(ctx: Context) -> None:
    """通話後の検証が止まっている通話に気づく（検証の部品の有無に関係なく動く＝検証の仕事とは別の仕事。CL-1 へ修正依頼）。"""
    _each(ctx, "通話後の検証の見張り", _clients(ctx),
          lambda c: ctx.access.in_client(ACTOR, ctx.tenant_id, c, attempts.watch_stuck_verifications))


def _probe(ctx: Context, run: Callable[[Callable[[Any], Any]], Any]) -> None:
    """確かめの部品で確かめる停止（会話AIの異常で止めた停止。回線の休みの再開の確かめは、回線の状態の表が置いたテスト番号を、その回線の
    処理が鳴らす＝DESIGN.md 17章。⛔ 4段目まで：テスト番号で確かめる回線の停止を ring_test.checks_stop で除いていた）。"""
    prober = ctx.part(ctx.cfg.prober)
    def one(stop: dict[str, Any]) -> None:
        ok, evidence = prober.probe(stop)
        run(lambda tx: watch.record_check(tx, stop["event_id"], ok, evidence=evidence))
    _each(ctx, "停止を解く確かめ",
          run(watch.due_checks), one)


def stop_checks(ctx: Context) -> None:
    """システムが止めた停止（会話AI）の確かめ（その契約者の分）。決めた回数続けて通れば、確かめの部品が解く（9-1）。"""
    _probe(ctx, lambda work: ctx.access.in_tenant(ACTOR, ctx.tenant_id, work))


def system_stop_checks(ctx: Context) -> None:
    """全体の範囲の、システムが止めた停止の確かめ。"""
    _probe(ctx, lambda work: ctx.access.in_system(ACTOR, work))


def lasting_stops(ctx: Context) -> None:
    """止まったまま1時間以上続いている停止の修正依頼（その契約者の分。確かめの部品が無くても動く）。"""
    ctx.access.in_tenant(ACTOR, ctx.tenant_id, watch.lasting_stops)


def system_lasting_stops(ctx: Context) -> None:
    """全体の範囲の、止まったまま1時間以上続いている停止の修正依頼。"""
    ctx.access.in_system(ACTOR, watch.lasting_stops)


def fixed_stops(ctx: Context) -> None:
    """その種類の不具合を直したと記録され、そのあとプログラムが替わった（E11）を、状態の表を引く入口へ渡す（その契約者の分。core/health.fixed。
    DESIGN.md 17章）。表が、止めていた種類なら契約者の止めを解き（C-5）、その種類で休んだ行・復旧待ちの行を戻す（R-6・R-10）。
    ⛔ 4段目の前：止めた種類を停止の根拠の文の頭から読み（watch.resume_fixed・stopped_kind）、行は別のデータベースの関数で戻した。"""
    ctx.access.in_tenant(ACTOR, ctx.tenant_id, health.fixed)


def budget(ctx: Context) -> None:
    """費用の見張り（その契約者の分）：今日の費用の注意・赤字の警告と、期限までに確認されなければもう一度の知らせ（赤字では止めない＝
    2026-10-22 深夜 ユーザー。12-1）。"""
    usage.tick(ctx.access, ACTOR, ctx.tenant_id)


def holidays(ctx: Context) -> None:
    """祝日の暦（要るときだけ公式の一覧を取りに行く。取りに行くかは calendar の1か所が決める）。"""
    calendar.sync_official(ctx.access, ACTOR)


def backups(ctx: Context) -> None:
    """その契約者の控え（依頼者ごとの AI用・提出用を写す・古いものを消す。やるかは種類ごとに backup の1か所が決める）。書くのは、
    その契約者の、つないでいる PC の部品（無ければ、しない＝控えの見張りが、取れていないことに気づく）。"""
    writer = ctx.links.writer(ctx.tenant_id)
    if writer is not None:
        backup.run_contractor(ctx.access, ctx.tenant_id, writer)


def backup_watch(ctx: Context) -> None:
    """この契約者の控えが取れているか（その契約者の PC が止まっていても動く＝取れないことに気づく）。"""
    _each(ctx, "控えの見張り", _clients(ctx), lambda c: backup.watch_client(ctx.access, ctx.tenant_id, c))


def whole_backup_watch(ctx: Context) -> None:
    """データベース全体の控えが取れているか（接続の設定が欠ける・管理者の PC が止まっていて取れないことに気づく）。"""
    backup.watch_whole(ctx.access)


def whole_backup(ctx: Context) -> None:
    """データベース全体の控え（1日1回、戻せるかまで確かめる）。書くのは、管理者の PC の部品だけ（すべての契約者のデータが入る）。"""
    writer = ctx.links.whole_writer()
    if writer is not None:
        backup.run_whole(ctx.access, ctx.cfg.owner_dsn, ctx.cfg.restore_dsn, writer)


def pc_watch(ctx: Context) -> None:
    """その契約者の PC（PC の部品）からの合図が途切れたら、その契約者と管理者へ知らせる（DESIGN.md 5-5。止めない）。"""
    alive, listening_for = ctx.links.connected()
    ctx.access.in_tenant(ACTOR, ctx.tenant_id, lambda tx: agents.watch_down(tx, alive=alive, listening_for=listening_for))


# 仕事の表（1か所）。なぜその間隔か：
# - recover：前の中央の見守り役が落ちてから、その回線の通話を結果不明にするまでの遅れ（回線の借用の期限 60秒の上に、最大 60秒）
# - settle：締めは次の動き（お詫びしてすぐかけ直す電話を含む）を決めるので、待たせない
# - verify：通話後の検証は締めの前に要る（同じお客様への次の電話を待たせる）
# - stop_checks：確かめてよい時刻は停止の記録が決める（ここは、その時刻を見に行く間隔）
# - rejudge：判定できない原因が直るのは、修正が入ったとき（数分の遅れは問題にならない）
# - reopen_revoked：契約者の共通の発信禁止を取り消したあと、その登録で終えた行を予定に戻すまでの遅れ（5分。依頼者の台帳の取り消しは、その場で戻す）
# - budget：赤字の警告の期限（1時間）を見る。数分の遅れは問題にならない
# - holidays：取りに行くかは calendar が決める（1日1回で足りる）
# - samples：週に1回でよい（週の終わりから1日あけて選ぶ。するかは measure が決める）
# - answer_rates：応答率の低下は、その日のうちに気づけばよい（1回線1日1回知らせる）。30分の遅れは問題にならない
# - backups：提出用を数分ごとに写す（2026-10-17 ユーザー：Fly.io に残さない）。AI用は backup が1日1回にする
# - pc_watch：PC の合図の途切れ（pc.down_after＝初め3分）に、1分の遅れで気づく
# - verify_watch：通話後の検証が止まっている（録音を待つ時間＋1時間）ことに、10分の遅れで気づく（修正依頼は同じ不具合で1つ）
# - whole_backup：数分ごとに見て、backup が1日1回にする（失敗したら1時間あけてやり直す）
# - fixed_stops：直したと記録され、そのあとプログラムが替わってから、こちらの不具合の停止を解くまでの遅れ（1分。版を書くのは本体の毎回の段）
# - lasting_stops・system_lasting_stops：止まったまま1時間続いた停止の修正依頼（確かめの部品が無くても動く。依頼は同じ不具合で1つ）
# - backup_watch・whole_backup_watch：控えが1日以上取れていないことに気づく（取る仕事は設定が無いと動かないので、別の仕事にする）。
#   1時間に1回で足りる（知らせは1日1回）
JOBS: tuple[Job, ...] = (
    Job("recover", 60, "tenant", 300, recover_expired),
    Job("settle", 15, "tenant", 600, settle_ready),
    Job("verify", 15, "tenant", 600, verify_pending, wanted=lambda cfg: cfg.verifier is not None),
    Job("stop_checks", 30, "tenant", 600, stop_checks, wanted=lambda cfg: cfg.prober is not None),
    Job("rejudge", 600, "tenant", 1800, rejudge_held),
    Job("reopen_revoked", 300, "tenant", 600, reopen_revoked),
    Job("samples", 3600, "tenant", 1800, draw_samples),
    Job("answer_rates", 1800, "tenant", 600, answer_rates),
    Job("budget", 60, "tenant", 600, budget),
    Job("system_stop_checks", 30, "system", 600, system_stop_checks, wanted=lambda cfg: cfg.prober is not None),
    Job("holidays", 86400, "system", 600, holidays),   # 1日1回（⛔ 2026-10-22 の見直し：1時間ごとで、翌年の分が載るまで毎時間取りに行った）
    Job("backups", 300, "tenant", 4 * 3600, backups, heavy=True),
    Job("whole_backup", 300, "system", 4 * 3600, whole_backup, wanted=lambda cfg: cfg.whole_backup, heavy=True),
    Job("backup_watch", 3600, "tenant", 600, backup_watch),
    Job("lasting_stops", 300, "tenant", 600, lasting_stops),
    Job("fixed_stops", 60, "tenant", 300, fixed_stops),
    Job("system_lasting_stops", 300, "system", 600, system_lasting_stops),
    Job("whole_backup_watch", 3600, "system", 600, whole_backup_watch),
    Job("pc_watch", 60, "tenant", 300, pc_watch),
    Job("verify_watch", 600, "tenant", 300, verify_watch),
)


def _in_scope(ctx: Context, job: Job, work: Callable[[Any], Any]) -> Any:
    """仕事の範囲の取引（契約者ごとの仕事＝その契約者の取引・共通の仕事＝全体の取引）。"""
    if job.scope == "system":
        return ctx.access.in_system(ACTOR, work)
    return ctx.access.in_tenant(ACTOR, ctx.tenant_id, work)


def run_job(ctx: Context, job: Job) -> bool:
    """1つの仕事を1回する（契約者ごとの仕事は ctx の契約者の分。借りられなければ、しない）。したら True。"""
    key = job.key(ctx.tenant_id)
    try:   # 借りる所の誤りも記録する（⛔ 2026-10-22 の見直し：外へ出て、だれも読まずに消えていた）
        if not _in_scope(ctx, job, lambda tx: leases.take_job(tx, key, ctx.holder, job.lease)):
            return False
    except Exception as e:  # noqa: BLE001
        report(ctx.access, ctx.tenant_id, job.name, e)
        return False
    try:
        job.work(ctx)
    except Exception as e:  # noqa: BLE001 記録して、次の回にまたする（止めない）
        report(ctx.access, ctx.tenant_id, job.name, e)
    finally:
        try:
            _in_scope(ctx, job, lambda tx: leases.return_job(tx, key, ctx.holder))
        except Exception:  # noqa: BLE001 返せなくても、期限で次が引き継ぐ
            pass
    return True
