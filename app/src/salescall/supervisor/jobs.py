"""見守り役の決まった間隔の仕事（DESIGN.md 5-4）。**仕事の一覧は、この表 `JOBS` の1か所**。持ち主 CL-2。

- どの仕事も、始める前に「仕事の借用」（leases.take_job。期限つき）を取る：2台の PC が同じ仕事を同時にしない。落ちれば期限で次が
  引き継ぐ。契約者ごとの仕事は「名前:契約者」、すべての契約者に共通の仕事は名前で借りる。
- どの仕事も、中で落ちても見守り役を止めない（「見守り役の誤り」として CL-2 へ修正依頼＝`report`。次の回にまたする）。
  行ごと・依頼者ごとに分けられる仕事は、1つが落ちても残りを続ける（1つの通話の締めの誤りで、ほかの締めを止めない）。
- 間隔の秒数は、仕事が受け持つものの速さから決めた（下の表の「なぜ」）。設定の値にしないのは、契約者が変えて得るものが無いため。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable
from uuid import UUID

from salescall.core import answer_rate, backup, calendar, leases, measure, settle, usage, watch
from salescall.core.access import Access
from salescall.supervisor import ACTOR, config, plugins, report


@dataclass
class Context:
    access: Access
    cfg: config.Config
    holder: str
    _parts: dict[str, Any] | None = None

    def part(self, spec: str | None) -> Any:
        """ほかの担当の部品（無ければ None。作るのは1回だけ）。"""
        if spec is None:
            return None
        self._parts = self._parts or {}
        if spec not in self._parts:
            self._parts[spec] = plugins.load(spec)
        return self._parts[spec]


@dataclass(frozen=True)
class Job:
    name: str
    every: int                      # 何秒ごとか
    scope: str                      # tenant＝この PC の契約者の仕事／system＝すべての契約者に共通の仕事（管理者の PC だけ）。
                                    # 費用の見張りと控えは契約者ごと（2026-10-18 ユーザー）。共通は祝日の暦・全体の停止・データベース全体の控え
    lease: int                      # 借りる期限（秒）。仕事が終わるより長く（終われば返す）
    work: Callable[[Context], Any]
    wanted: Callable[[config.Config], bool] = lambda cfg: True   # この PC でする仕事か

    def key(self, cfg: config.Config) -> str:
        return self.name if self.scope == "system" else f"{self.name}:{cfg.tenant_id}"

    def runs_here(self, cfg: config.Config) -> bool:
        return (self.scope == "tenant" or cfg.system_jobs) and self.wanted(cfg)


def _clients(ctx: Context) -> list[UUID]:
    return ctx.access.in_tenant(ACTOR, ctx.cfg.tenant_id, lambda tx: [r["client_id"] for r in tx.all(
        "SELECT client_id FROM salescall.clients WHERE tenant_id = %s ORDER BY created_at", (ctx.cfg.tenant_id,))])


def _each(ctx: Context, what: str, items: list[Any], work: Callable[[Any], Any]) -> None:
    """1つずつ。1つが落ちても、記録して残りを続ける。"""
    for item in items:
        try:
            work(item)
        except Exception as e:  # noqa: BLE001
            report(ctx.access, ctx.cfg.tenant_id, what, e)


# 管理者の PC が、ほかの契約者の回線を後始末するまでの、借用の期限が切れてからの長さ。ふつうの通話の長さ（会話AIの1回の音声の
# セッションの上限 約15分＝Intent.md の Live API の上限）より十分長く＝その PC がデータベースに届かないだけで、まだ話している通話を
# 終わらせない。その契約者のほかの PC は、期限が切れてすぐ後始末する（recover）
STALE_AFTER = timedelta(minutes=30)


def recover_expired(ctx: Context) -> None:
    """借用の期限が切れた回線（同じ契約者のほかの PC が落ちた）の、進行中の試行の後始末（DESIGN.md 4-2）。自分が借りている回線は
    除く（データベースに届かないあいだに期限が切れても、まだ話している自分の通話を終わらせない）。"""
    ctx.access.in_tenant(ACTOR, ctx.cfg.tenant_id, lambda tx: leases.recover(tx, except_holder=ctx.holder))


def recover_stale(ctx: Context) -> None:
    """管理者の PC：契約者の PC が落ちたまま戻らない回線の後始末（すべての契約者に共通の仕事。残すと、その番号にすべての契約者が
    かけられない・出たお客様へのお詫びが遅れる）。契約者ごとに別の取引。"""
    tenants = ctx.access.in_system(ACTOR, lambda tx: leases.tenants_with_stale_lines(tx, STALE_AFTER))
    _each(ctx, "ほかの PC の回線の後始末", tenants, lambda t: ctx.access.in_tenant(
        ACTOR, t, lambda tx: leases.recover(tx, except_holder=ctx.holder, expired_for=STALE_AFTER)))


def settle_ready(ctx: Context) -> None:
    """締めを待っている試行（終わって、通話後の検証が済んだもの）を締める（判定 → 結果 → 次の動き。1本ずつ別の取引）。"""
    def per_client(client_id: UUID) -> None:
        ready = ctx.access.in_client(ACTOR, ctx.cfg.tenant_id, client_id, settle.unsettled)
        _each(ctx, "締め", [r["attempt_id"] for r in ready],
              lambda a: ctx.access.in_client(ACTOR, ctx.cfg.tenant_id, client_id, lambda tx: settle.settle(tx, a)))
    _each(ctx, "締め（依頼者）", _clients(ctx), per_client)


def rejudge_held(ctx: Context) -> None:
    """判定できずに待っている行を、原因が直っていれば判定し直す（DESIGN.md 7-2-2）。"""
    _each(ctx, "判定し直し", _clients(ctx),
          lambda c: ctx.access.in_client(ACTOR, ctx.cfg.tenant_id, c, settle.rejudge_held))


def draw_samples(ctx: Context) -> None:
    """誤判定率を測るための抜き取り（業務ごと・週ごとに1回。するかは measure の1か所が決める。Intent.md 16-3）と、測らない通話の
    検証の印を外すこと（話していない通話・かけずに取り消した予約の録音を、ふつうの決まりに戻す）。"""
    def work(tx: Any) -> None:
        measure.release_unmeasured(tx)
        measure.draw(tx)
    _each(ctx, "抜き取り", _clients(ctx), lambda c: ctx.access.in_client(ACTOR, ctx.cfg.tenant_id, c, work))


def answer_rates(ctx: Context) -> None:
    """番号ごとの応答率の見張り（この PC の契約者の回線すべて。落ちたら知らせるだけ。Intent.md 8-6）。"""
    ctx.access.in_tenant(ACTOR, ctx.cfg.tenant_id, lambda tx: answer_rate.watch(tx, ctx.cfg.tenant_id))


def verify_pending(ctx: Context) -> None:
    """通話後の検証（CL-1 の部品）：待ちの通話を確かめる。録音が届かないまま待つ時間を過ぎた通話は「録音なし」で済ませる
    （attempts.finish_without_recording。CL-1 の部品が呼ぶ）。"""
    ctx.part(ctx.cfg.verifier).run_pending(ctx.access, ACTOR, tenant_id=ctx.cfg.tenant_id)


def _probe(ctx: Context, run: Callable[[Callable[[Any], Any]], Any]) -> None:
    prober = ctx.part(ctx.cfg.prober)
    def one(stop: dict[str, Any]) -> None:
        ok, evidence = prober.probe(stop)
        run(lambda tx: watch.record_check(tx, stop["event_id"], ok, evidence=evidence))
    _each(ctx, "停止を解く確かめ", run(watch.due_checks), one)


def stop_checks(ctx: Context) -> None:
    """システムが止めた停止（会話AI・回線）の確かめ（この PC の契約者の分）。決めた回数続けて通れば、確かめの部品が解く（9-1）。"""
    _probe(ctx, lambda work: ctx.access.in_tenant(ACTOR, ctx.cfg.tenant_id, work))


def system_stop_checks(ctx: Context) -> None:
    """全体の範囲の、システムが止めた停止の確かめ。"""
    _probe(ctx, lambda work: ctx.access.in_system(ACTOR, work))


def budget(ctx: Context) -> None:
    """費用の見張り（この PC の契約者の分）：今日の費用の注意・赤字の警告の期限・赤字でなくなった停止を解く（12-1）。"""
    usage.tick(ctx.access, ACTOR, ctx.cfg.tenant_id)


def holidays(ctx: Context) -> None:
    """祝日の暦（要るときだけ公式の一覧を取りに行く。取りに行くかは calendar の1か所が決める）。"""
    calendar.sync_official(ctx.access, ACTOR)


def backups(ctx: Context) -> None:
    """この PC の契約者の控え（依頼者ごとの AI用・提出用を写す・古いものを消す。やるかは種類ごとに backup の1か所が決める）。"""
    backup.run_contractor(ctx.access, ctx.cfg.tenant_id, ctx.cfg.backup_drive, ctx.cfg.environ)


def backup_watch(ctx: Context) -> None:
    """この契約者の控えが取れているか（控え用のハードディスクの設定が無い PC でも動く＝取る仕事が動かないことに気づく）。"""
    backup.watch_contractor(ctx.access, ctx.cfg.tenant_id)


def whole_backup_watch(ctx: Context) -> None:
    """データベース全体の控えが取れているか（管理者の PC。接続の設定が欠けて取る仕事が動かないことに気づく）。"""
    backup.watch_whole(ctx.access)


def whole_backup(ctx: Context) -> None:
    """データベース全体の控え（管理者の PC。1日1回、戻せるかまで確かめる）。"""
    cfg = ctx.cfg
    backup.run_whole(ctx.access, cfg.owner_dsn, cfg.restore_dsn, cfg.backup_drive, cfg.environ)


# 仕事の表（1か所）。なぜその間隔か：
# - recover：PC が落ちてから、その回線の通話を結果不明にするまでの遅れ（回線の借用の期限 60秒の上に、最大 60秒）
# - recover_stale：契約者の PC が落ちたまま戻らないとき（STALE_AFTER の上に、最大 5分）
# - settle：締めは次の動き（お詫びしてすぐかけ直す電話を含む）を決めるので、待たせない
# - verify：通話後の検証は締めの前に要る（同じお客様への次の電話を待たせる）
# - stop_checks：確かめてよい時刻は停止の記録が決める（ここは、その時刻を見に行く間隔）
# - rejudge：判定できない原因が直るのは、修正が入ったとき（数分の遅れは問題にならない）
# - budget：赤字の警告の期限（1時間）を見る。数分の遅れは問題にならない
# - holidays：取りに行くかは calendar が決める（1日1回で足りる）
# - samples：週に1回でよい（週の終わりから1日あけて選ぶ。するかは measure が決める）
# - answer_rates：応答率の低下は、その日のうちに気づけばよい（1回線1日1回知らせる）。30分の遅れは問題にならない
# - backups：提出用を数分ごとに写す（2026-10-17 ユーザー：Fly.io に残さない）。AI用は backup が1日1回にする
# - whole_backup：数分ごとに見て、backup が1日1回にする（失敗したら1時間あけてやり直す）
# - backup_watch・whole_backup_watch：控えが1日以上取れていないことに気づく（取る仕事は設定が無いと動かないので、別の仕事にする）。
#   1時間に1回で足りる（知らせは1日1回）
JOBS: tuple[Job, ...] = (
    Job("recover", 60, "tenant", 300, recover_expired),
    Job("recover_stale", 300, "system", 600, recover_stale),
    Job("settle", 15, "tenant", 600, settle_ready),
    Job("verify", 15, "tenant", 600, verify_pending, wanted=lambda cfg: cfg.verifier is not None),
    Job("stop_checks", 30, "tenant", 600, stop_checks, wanted=lambda cfg: cfg.prober is not None),
    Job("rejudge", 600, "tenant", 1800, rejudge_held),
    Job("samples", 3600, "tenant", 1800, draw_samples),
    Job("answer_rates", 1800, "tenant", 600, answer_rates),
    Job("budget", 60, "tenant", 600, budget),
    Job("system_stop_checks", 30, "system", 600, system_stop_checks, wanted=lambda cfg: cfg.prober is not None),
    Job("holidays", 3600, "system", 600, holidays),
    Job("backups", 300, "tenant", 4 * 3600, backups, wanted=lambda cfg: cfg.backup_drive is not None),
    Job("whole_backup", 300, "system", 4 * 3600, whole_backup, wanted=lambda cfg: cfg.whole_backup),
    Job("backup_watch", 3600, "tenant", 600, backup_watch),
    Job("whole_backup_watch", 3600, "system", 600, whole_backup_watch),
)


def _in_scope(ctx: Context, job: Job, work: Callable[[Any], Any]) -> Any:
    """仕事の範囲の取引（契約者の仕事＝その契約者の取引。契約者の PC の合言葉は全体の取引を開けない＝0077）。"""
    if job.scope == "system":
        return ctx.access.in_system(ACTOR, work)
    return ctx.access.in_tenant(ACTOR, ctx.cfg.tenant_id, work)


def run_job(ctx: Context, job: Job) -> bool:
    """1つの仕事を1回する（借りられなければ、しない）。したら True。"""
    key = job.key(ctx.cfg)
    if not _in_scope(ctx, job, lambda tx: leases.take_job(tx, key, ctx.holder, job.lease)):
        return False
    try:
        job.work(ctx)
    except Exception as e:  # noqa: BLE001 記録して、次の回にまたする（止めない）
        report(ctx.access, ctx.cfg.tenant_id, job.name, e)
    finally:
        try:
            _in_scope(ctx, job, lambda tx: leases.return_job(tx, key, ctx.holder))
        except Exception:  # noqa: BLE001 返せなくても、期限で次が引き継ぐ
            pass
    return True
