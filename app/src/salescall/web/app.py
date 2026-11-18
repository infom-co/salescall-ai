"""ダッシュボードの入口（画面のルートの1か所）。設計は docs/DASHBOARD.md。

作り方：`create_app(access, identify, secret)`。
- access：core の読み書きの層（データベースへは、これだけを通す。取引の始め方は context.Ctx の1か所）。
- identify：ログインを確かめる関数（要求 → ログインした人か None）。⏳ 利用者の表は CL-2 に頼んでいる。無い間は、画面は動かさない
  （確かめずに通す既定の形を作らない）。試験は、架空の人を返す関数を渡す。
- secret：画面の札（なりすましの送信の確かめ）に使う秘密。値を git・画面・記録に出さない。
- client_ip_header：前に置いた中継（Fly.io の受け口）が、元の相手の住所を入れる見出しの名前（Fly.io なら "fly-client-ip"）。中継の後ろで
  動かすときだけ渡す（中継が無いのに渡すと、相手が見出しを偽れる）。渡さなければ、つないできた相手の住所。
- secure_cookies：クッキーを https のときだけ送る印を付ける（本番は必ず True。既定も True）。

住所の決まり（docs/DASHBOARD.md 2-1・2-2）：全体は /t/...、依頼者の中は /c/{依頼者ID}/...、業務の中は /c/{依頼者ID}/b/{業務ID}/...、
管理者の場所は /admin/...。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

from salescall.core import access as access_layer
from salescall.core import (agents, calling_hours, conversation_io, customer_contacts, customer_rows, dnc, dnc_import, exclusions, health,
                            health_rules, import_files, importer, measure, phone, quality_stage, recordings, registry, results, reward,
                            settings, stops, usage)
from salescall.core import approval as approval_core
from salescall.core.access import Access
from salescall.output import files as result_files
from salescall.output import notices, result_rows, words
from salescall.web import actions, context, counts, importing, inbox, rowfix, setting_forms, uploads
from salescall.web.context import AdminOnly, Ctx, Identify, NotFound, Person, acting

HERE = Path(__file__).resolve().parent
ACTING_COOKIE = "acting_tenant"   # 管理者が見ている契約者（署名つき）
TENANT_BACKS = ("/t/costs",)   # 受け箱の操作のあと、依頼者の中の住所のほかに戻ってよい全体の画面（赤字の確認ボタン）
SYSTEM_PLACE: dict[str, Any] = {"client_id": None, "name": "全体（管理者だけ）"}   # 全体の範囲の承認待ちの「どこ」


def _time(t: datetime | None) -> str:
    """時刻の書き方（1か所＝calling_hours.label。日本時間。年は出さない）。"""
    return calling_hours.label(t) if t else ""


def _yen(v: Any, *, unit_price: bool = False) -> str:
    """円の書き方（1か所）。金額は1円単位・3桁ごとの区切り。単価（unit_price）は小さい値もあるので有効数字6桁まで（指数の形にしない）。"""
    if v is None:
        return "—"
    d = Decimal(str(v))
    if not d.is_finite():
        return "—"
    if unit_price and d != 0:   # 有効数字6桁まで（0.000012 のような小さい単価も 0 にしない）
        q = d.quantize(Decimal(1).scaleb(min(0, d.adjusted() - 5)))
    else:
        q = d.quantize(Decimal("1"))
    text = f"{q:,f}"
    return (text.rstrip("0").rstrip(".") if "." in text else text) + "円"


def _phone(e164: str | None) -> str:
    """番号の書き方（1か所＝phone.normalize。国内の番号は 0 から。そろえられなければ、そのまま）。"""
    if not e164:
        return ""
    n = phone.normalize(e164)
    return n.national or e164


def _back(path: str, out: actions.Outcome) -> RedirectResponse:
    """操作のあと、元の画面へ戻し、結果の1行を出す（送り直しで二度効かないよう、必ず GET の画面へ戻す）。"""
    return RedirectResponse(f"{path}?done={quote(out.message)}&ok={int(out.ok)}", status_code=303)


def _picked(pair: tuple[dict[str, Any], inbox.Item], client_id: UUID | None, business_id: UUID | None, kind: str | None) -> bool:
    """承認待ちの絞り方の1か所（依頼者・業務・種類）。全体の範囲の種類（管理者だけ）は、依頼者・業務で絞らないときだけ残す。"""
    c, i = pair
    if c is SYSTEM_PLACE:
        return client_id is None and business_id is None and (kind is None or i.kind == kind)
    return ((client_id is None or c["client_id"] == client_id) and (business_id is None or i.business_id == business_id)
            and (kind is None or i.kind == kind))


def _waiting(ctx: Ctx, client_id: UUID | None = None, business_id: UUID | None = None,
             kind: str | None = None) -> list[tuple[dict[str, Any], inbox.Item]]:
    """承認待ちを、受け箱の並べ方（inbox.ordered の1か所）で並べる。絞り方もここ1か所：依頼者（依頼者のタブ・受け箱の絞り）・
    業務（業務のタブ）・種類（受け箱の絞り）。どの画面の承認待ちの数も、これを数える（ホーム・帯・タブで数が食い違わない）。"""
    chosen = [c for c in ctx.clients() if client_id is None or c["client_id"] == client_id]
    pairs = [(c, i) for c in chosen for i in ctx.client(c["client_id"], inbox.collect, read_only=True)]
    if ctx.person.is_admin and client_id is None and business_id is None:   # 全体の範囲の種類（管理者だけ）は、依頼者で絞らないときだけ
        pairs += [(SYSTEM_PLACE, i) for i in ctx.system(lambda tx: inbox.collect(tx, "system"))]
    pairs = [p for p in pairs if _picked(p, client_id, business_id, kind)]
    rank = {id(i): n for n, i in enumerate(inbox.ordered([i for _, i in pairs]))}
    return sorted(pairs, key=lambda ci: rank[id(ci[1])])


def _templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(HERE / "templates"))   # 文字は必ず「文字として」出す（自動のエスケープ）
    t.env.globals.update(words=words, time=_time, phone=_phone, yen=_yen, token_field=actions.TOKEN_FIELD)
    return t


def create_app(access: Access, identify: Identify, secret: bytes, *, client_ip_header: str | None = None,
               secure_cookies: bool = True) -> FastAPI:
    if not secret:
        raise ValueError("画面の秘密（secret）が空です")
    app = FastAPI(title="SalesCall ダッシュボード", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = _templates()

    def page(request: Request, name: str, person: Person, ctx: Ctx, **values: Any) -> HTMLResponse:
        """画面を出す1か所。依頼者の画面なら、業務のタブ（その依頼者の業務の一覧）も、ここで必ず添える。"""
        values.setdefault("client", None)
        values.setdefault("business", None)
        if values["client"] is not None and "businesses" not in values:
            values["businesses"] = ctx.client(values["client"]["client_id"], context.businesses, read_only=True)
        return templates.TemplateResponse(request, name, {
            "person": person, "ctx": ctx, "clients": ctx.clients(), "token": actions.form_token(secret, person.login_id), **values})

    def chosen_tenant(request: Request, person: Person) -> UUID | None:
        """管理者が選んだ契約者（署名つきの札から読む。札が合わなければ選んでいないとみなす）。"""
        return actions.read_choice(secret, person.login_id, request.cookies.get(ACTING_COOKIE))

    def problem(request: Request, status: int, title: str, body: str) -> HTMLResponse:
        return templates.TemplateResponse(request, "problem.html", {"title": title, "body": body}, status_code=status)

    @app.exception_handler(HTTPException)
    async def http_problem(request: Request, e: HTTPException) -> HTMLResponse:
        """枠組みが返す誤り（無い住所・送り方の形の違い など）も、日本語の画面で返す（英語の返事を出さない。1か所）。"""
        if e.status_code == 404:
            return problem(request, 404, "見つかりません", "住所を確かめてください。")
        if e.status_code == 405:
            return problem(request, 405, "この開き方はできません", "画面のボタンから操作してください。")
        return problem(request, e.status_code if 400 <= e.status_code < 600 else 400, "受け付けられませんでした",
                       "送られた中身の形が違います。画面を開き直して、もう一度操作してください。")

    @app.exception_handler(RequestValidationError)
    async def bad_address(request: Request, e: RequestValidationError) -> HTMLResponse:
        """住所や問い合わせの値の形が違う（例：ID の形でない住所）。「無い」と同じ返事にする（英語の返事を出さない）。"""
        return problem(request, 404, "見つかりません", "住所を確かめてください。")

    def handle(request: Request, render: Any) -> HTMLResponse:
        """ログインの確かめと、見つからない・届かないの返事の1か所。"""
        person = identify(request)
        if person is None:
            return RedirectResponse("/login", status_code=303)
        try:
            ctx = acting(access, person, chosen_tenant(request, person))   # 契約者の名前を読む（データベースに届かないときも、下の返事にする）
            return render(person, ctx)
        except (NotFound, AdminOnly):
            return problem(request, 404, "見つかりません", "住所を確かめてください。")
        except access_layer.TimedOut:
            return problem(request, 503, "読めませんでした", "データベースの返事が遅れています。少し待ってから開き直してください。")
        except access_layer.RetryExhausted:
            return problem(request, 503, "読めませんでした", "ほかの操作と重なりました。少し待ってから開き直してください。")
        except access_layer.Rejected as e:   # 読む画面でデータベースが断った（読める範囲の外など）。英語の文は出さない（actions の1か所）
            return problem(request, 403, "開けませんでした", actions.rejected_text(e) + "。")
        except Exception as e:
            if access_layer.unreachable(e) or access_layer.busy(e):
                return problem(request, 503, "データベースに届きません", "古い数を出さないため、何も表示していません。少し待ってから開き直してください。")
            raise

    @app.post("/admin/acting")
    async def choose_tenant(request: Request) -> HTMLResponse:
        """管理者が、見る契約者を選ぶ（管理者だけ。管理者でなければ「見つかりません」と同じ返事）。"""
        form = dict(await request.form())
        person = identify(request)
        if person is None:
            return RedirectResponse("/login", status_code=303)
        if not person.is_admin or not actions.token_ok(secret, person.login_id, form.get(actions.TOKEN_FIELD)):
            return problem(request, 404, "見つかりません", "住所を確かめてください。")
        resp = RedirectResponse("/t", status_code=303)
        raw = str(form.get("tenant_id") or "")
        if raw:
            try:
                chosen = UUID(raw)
            except ValueError:
                return problem(request, 404, "見つかりません", "住所を確かめてください。")
            resp.set_cookie(ACTING_COOKIE, actions.sign_choice(secret, person.login_id, chosen), httponly=True, samesite="strict",
                            secure=secure_cookies)
        else:
            resp.delete_cookie(ACTING_COOKIE)
        return resp

    @app.get("/login")
    def login(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {})

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/t", status_code=303)

    # ── 全体（契約者） ─────────────────────────────────────────
    @app.get("/t")
    def home(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            waiting = _waiting(ctx)
            per_client = [{**c, **ctx.client(c["client_id"], counts.client_today, read_only=True),
                           "waiting": sum(1 for wc, _ in waiting if wc["client_id"] == c["client_id"])} for c in ctx.clients()]
            return page(request, "home.html", person, ctx, stops=counts.open_stops(ctx), per_client=per_client, done=done, ok=bool(ok),
                        waiting_total=len(waiting), first=waiting[:3])
        return handle(request, render)

    @app.get("/t/badges")
    def badges(request: Request) -> HTMLResponse:
        """いつも見える帯の数（画面を開いたあとに読み、30秒ごとに読み直す）。数える部品はホームと同じ。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            n_stops = len(counts.open_stops(ctx))
            n_wait = len(_waiting(ctx))
            return templates.TemplateResponse(request, "badges.html", {"n_stops": n_stops, "n_wait": n_wait})
        return handle(request, render)

    @app.get("/t/inbox")
    def inbox_page(request: Request, done: str | None = None, ok: int = 1, kind: str = "", client: str = "") -> HTMLResponse:
        """承認待ち（全部の依頼者）。上に種類ごとの数を出し、種類・依頼者で絞れる（埋もれさせない＝docs/DASHBOARD.md 4-2）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            every = _waiting(ctx)
            ids = {str(c["client_id"]): c for c in ctx.clients()}
            pick_kind = kind if kind in inbox.KINDS else None
            pick_client = ids[client]["client_id"] if client in ids else None
            rows = [p for p in every if _picked(p, pick_client, None, pick_kind)]   # 集めるのは1回（同じ絞り方の1か所で絞る）
            per_kind = [(k, v.title, sum(1 for _, i in every if i.kind == k)) for k, v in inbox.KINDS.items()]   # 0 の種類は出さない
            return page(request, "inbox.html", person, ctx, rows=rows, per_kind=per_kind, total=len(every),
                        pick_kind=pick_kind or "", pick_client=str(pick_client or ""), done=done, ok=bool(ok))
        return handle(request, render)

    @app.post("/c/{client_id}/inbox/{kind}/{action}")
    async def inbox_act(request: Request, client_id: UUID, kind: str, action: str) -> HTMLResponse:
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            k = inbox.KINDS.get(kind)
            if k is None or k.where != "client" or action not in k.perform:
                raise NotFound()
            sent = {f: str(form.get(f, "")) for f in inbox.KEY_FIELDS}
            out = actions.act(secret, person.login_id, form,
                              lambda: ctx.client(client_id, lambda tx: inbox.perform(tx, kind, action, sent, "client")),
                              done=k.perform[action][1])
            back = str(form.get("back") or "")   # 1行の画面などから決めたときは、その画面へ戻る（その依頼者の中の住所だけ）
            ok_back = (back.startswith(f"/c/{client_id}/") and "//" not in back) or back in TENANT_BACKS   # 戻る先は決めた所だけ
            return _back(back if ok_back else "/t/inbox", out)
        return handle(request, render)

    @app.post("/admin/inbox/{kind}/{action}")
    async def admin_inbox_act(request: Request, kind: str, action: str) -> HTMLResponse:
        """全体の範囲の承認待ち（管理者だけ。管理者でなければ「見つかりません」＝ctx.system が断る。データベースも守る）。"""
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            k = inbox.KINDS.get(kind)
            if k is None or k.where != "system" or action not in k.perform:
                raise NotFound()
            sent = {f: str(form.get(f, "")) for f in inbox.KEY_FIELDS}
            out = actions.act(secret, person.login_id, form,
                              lambda: ctx.system(lambda tx: inbox.perform(tx, kind, action, sent, "system")),
                              done=k.perform[action][1])
            return _back("/t/inbox", out)
        return handle(request, render)

    @app.get("/t/stops")
    def stops_page(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            return page(request, "stops.html", person, ctx, stops=counts.open_stops(ctx), done=done, ok=bool(ok))
        return handle(request, render)

    def _stop_page(request: Request, event_id: int, client_id: UUID | None, done: str | None, ok: int) -> HTMLResponse:
        """何で止まったか（1つの停止）。その停止が見える取引で読む：依頼者・業務の範囲は住所の依頼者の取引、ほかは契約者の取引。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                if tx.one("SELECT 1 FROM salescall.stop_events WHERE event_id = %s AND action = 'stop'", (event_id,)) is None:
                    raise NotFound()
                return stops.explain(tx, event_id)
            x = ctx.tenant(read) if client_id is None else ctx.client(client_id, read)
            base = f"/t/stops/{event_id}" if client_id is None else f"/c/{client_id}/stops/{event_id}"
            return page(request, "stop.html", person, ctx, x=x, base=base, done=done, ok=bool(ok))
        return handle(request, render)

    def _stop_resume(request: Request, event_id: int, client_id: UUID | None, form: dict[str, Any]) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            reason = str(form.get("reason") or "").strip()
            def do(tx: Any) -> int:
                return stops.resume(tx, event_id, reason=reason)
            out = actions.act(secret, person.login_id, form,
                              lambda: ctx.tenant(do) if client_id is None else ctx.client(client_id, do),
                              done="再開しました。", reason_field="reason")
            return _back(f"/t/stops/{event_id}" if client_id is None else f"/c/{client_id}/stops/{event_id}", out)
        return handle(request, render)

    @app.get("/t/stops/{event_id}")
    def stop_page(request: Request, event_id: int, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _stop_page(request, event_id, None, done, ok)

    @app.get("/c/{client_id}/stops/{event_id}")
    def client_stop_page(request: Request, client_id: UUID, event_id: int, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _stop_page(request, event_id, client_id, done, ok)

    @app.post("/t/stops/{event_id}/resume")
    async def stop_resume(request: Request, event_id: int) -> HTMLResponse:
        return _stop_resume(request, event_id, None, dict(await request.form()))

    @app.post("/t/stops/{event_id}/emergency")
    async def stop_emergency(request: Request, event_id: int) -> HTMLResponse:
        """AIの不具合の停止のあいだ、契約者以上が緊急の道で新しい電話を再開する（Intent.md 10-3「ボタンと権限は CL-3・CL-2」。出せるかと
        出すのは core の stops.emergency_possible・emergency_pass の1か所。理由は必ず）。AIの不具合の停止は契約者の範囲（全体の画面）。"""
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            reason = str(form.get("reason") or "").strip()
            out = actions.act(secret, person.login_id, form, lambda: ctx.tenant(lambda tx: stops.emergency_pass(tx, event_id, reason=reason)),
                              done="緊急の道で再開しました。止まった AI が戻ったと確かめられたら、ふだんの道に戻ります。", reason_field="reason")
            return _back(f"/t/stops/{event_id}", out)
        return handle(request, render)

    @app.post("/c/{client_id}/stops/{event_id}/resume")
    async def client_stop_resume(request: Request, client_id: UUID, event_id: int) -> HTMLResponse:
        return _stop_resume(request, event_id, client_id, dict(await request.form()))

    @app.post("/t/emergency")
    async def emergency(request: Request) -> HTMLResponse:
        """緊急停止（人が押す。docs/DASHBOARD.md 5-3・7-4）。範囲：この契約者の全部／1つの依頼者／全体（管理者だけ＝データベースも守る）。
        同じ範囲の人の停止がもう開いていれば重ねない（stops.open_same の1か所）。押したあとは「止まっているもの」へ戻る。"""
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            scope = str(form.get("scope") or "")
            reason = str(form.get("reason") or "").strip()
            def do() -> str:
                def stop_in(tx: Any, **where: Any) -> str:
                    if stops.open_same(tx, scope=scope, cause="manual", **where) is not None:
                        return "もう止まっています（重ねて止めていません）。"
                    stops.stop(tx, scope=scope, cause="manual", reason=reason, **where)
                    return "止めました。進行中の通話は最後まで終えます。新しい電話はかけません。"
                if scope == "tenant":
                    return ctx.tenant(lambda tx: stop_in(tx, tenant_id=ctx.tenant_id))
                if scope == "client":
                    cid = actions.uuid_of(form, "client_id")
                    return ctx.client(cid, lambda tx: stop_in(tx, tenant_id=ctx.tenant_id, client_id=cid))
                if scope == "system":
                    return ctx.system(lambda tx: stop_in(tx))
                raise NotFound()
            out = actions.act(secret, person.login_id, form, do, done="", reason_field="reason")
            return _back("/t/stops", actions.Outcome(out.ok, out.value if out.ok else out.message))
        return handle(request, render)

    @app.get("/t/notices")
    def notices_page(request: Request) -> HTMLResponse:
        """知らせ（契約者の台帳）。管理者には、管理者あての知らせ（全体の取引でだけ見える＝データベースの1か所 0084）も別の欄に出す。"""
        def read(only_admin: bool) -> Callable[[Any], list[dict[str, Any]]]:
            def run(tx: Any) -> list[dict[str, Any]]:
                rows = tx.all(
                    "SELECT e.event_id, e.client_id, e.kind, e.summary, e.occurred_at, e.urgent,"
                    "       ARRAY(SELECT d.status FROM salescall.notification_deliveries d WHERE d.event_id = e.event_id) AS statuses"
                    " FROM salescall.notification_events e WHERE e.for_admin = %s"
                    " ORDER BY e.urgent DESC, e.occurred_at DESC, e.event_id DESC LIMIT 200", (only_admin,))
                return [{**r, "state": notices.delivery_state(r["statuses"])} for r in rows]
            return run
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            rows = ctx.tenant(read(False))
            admin_rows = ctx.system(read(True)) if person.is_admin else []
            return page(request, "notices.html", person, ctx, rows=rows, admin_rows=admin_rows, state_words=notices.STATE_WORDS)
        return handle(request, render)

    # ── 設定（docs/DASHBOARD.md 5-9。Intent.md 5-3・5-4）：契約者・依頼者・業務の段で、同じ1つの画面 ──────────
    SCOPES = ("system", "tenant", "client", "business")
    MERGE_WORDS = {"override": "下の段（業務 ＞ 依頼者 ＞ 契約者 ＞ 初めの値）の値が勝ちます",
                   "min": "どの段の値より小さくなりません（いちばん小さい値を使います）",
                   "max": "どの段の値より大きくなりません（いちばん大きい値を使います）",
                   "intersect": "どの段の時間帯にも入る時間だけを使います", "union": "どの段の値もすべて合わせて使います"}

    def _settings_rows(tx: Any, level: str, tenant_id: UUID, client_id: UUID | None, business_id: UUID | None) -> list[dict[str, Any]]:
        """その段で決められる設定（いちばん細かく決められる段＝finest_scope がその段より細かいか同じ）と、その段で結局いくつか
        （合成は core の settings.resolve_detail の1か所）。"""
        rows = []
        for d in tx.all("SELECT key, meaning, value_type::text AS value_type, merge_kind::text AS merge_kind,"
                        "       finest_scope::text AS finest_scope, choices, bound_key FROM salescall.setting_definitions ORDER BY key"):
            if SCOPES.index(d["finest_scope"]) < SCOPES.index(level):
                continue
            try:
                got = settings.resolve_detail(tx, d["key"], tenant_id=tenant_id, client_id=client_id, business_id=business_id)
            except settings.SettingMissing:
                got = None
            shown = (None if got is None else words.setting_choice(d["key"], got["value"]) if d["value_type"] == "choice"
                     else setting_forms.show(d["value_type"], got["value"]))
            rows.append({**d, "meaning": words.screen_meaning(d["key"], d["meaning"]), "got": got, "shown": shown,
                         "own": got is not None and got["from"] == level, "group": d["key"].split(".")[0],
                         "merge": MERGE_WORDS.get(d["merge_kind"], "")})
        return rows

    def _settings_page(request: Request, level: str, client_id: UUID | None, business_id: UUID | None,
                       done: str | None, ok: int) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                out: dict[str, Any] = {"rows": _settings_rows(tx, level, ctx.tenant_id, client_id, business_id)}
                if client_id is not None:
                    out.update(client=tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,)),
                               businesses=context.businesses(tx),
                               business=None if business_id is None else context.business(tx, business_id))
                return out
            if level == "system":   # 管理者だけ（ctx.system が断る）。仮の値は、見直す時と一緒に見せる（settings.provisional の1か所）
                def read_system(tx: Any) -> dict[str, Any]:
                    prov = {p["key"]: p for p in settings.provisional(tx)}
                    rows = [{**r, "provisional": prov.get(r["key"])} for r in _settings_rows(tx, "system", None, None, None)]
                    return {"rows": rows}
                values = ctx.system(read_system)
            else:
                values = ctx.tenant(read) if client_id is None else ctx.client(client_id, read, read_only=True)
            return page(request, "settings.html", person, ctx, level=level, forms=setting_forms, days=setting_forms.DAYS,
                        done=done, ok=bool(ok), active=("system_settings" if level == "system" else "settings" if client_id is None else "client"),
                        **values)
        return handle(request, render)

    def _settings_save(request: Request, form: dict[str, Any], level: str, client_id: UUID | None,
                       business_id: UUID | None, back: str) -> HTMLResponse:
        """1つの設定の、その段の値を足す（前の値は記録に残る。入れるのは core の settings.set_value の1か所。値の形はデータベースが確かめる）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            key = str(form.get("key") or "")
            def do(tx: Any) -> Any:
                if business_id is not None:
                    context.business(tx, business_id)
                d = tx.one("SELECT key, value_type::text AS value_type, finest_scope::text AS finest_scope, choices"
                           " FROM salescall.setting_definitions WHERE key = %s", (key,))
                if d is None or SCOPES.index(d["finest_scope"]) < SCOPES.index(level):
                    raise NotFound()
                value = setting_forms.read(d["value_type"], form, "f_", d)
                try:
                    if level == "system":
                        status = "provisional" if str(form.get("status") or "") == "provisional" else "decided"
                        return settings.set_system_value(tx, key, value, status=status, rationale=str(form.get("reason") or "").strip(),
                                                         review_when=str(form.get("review_when") or "").strip() or "変える必要が出たとき")
                    return settings.set_value(tx, key, value, scope=level, status="decided", rationale=str(form.get("reason") or "").strip(),
                                              review_when="変える必要が出たとき", tenant_id=ctx.tenant_id, client_id=client_id,
                                              business_id=business_id)
                except ValueError as e:   # set_value の理由つきの断り（テスト番号にお客様の番号 など）
                    raise actions.Refused(str(e)) from None
            work = ((lambda: ctx.system(do)) if level == "system" else
                    (lambda: ctx.tenant(do)) if client_id is None else (lambda: ctx.client(client_id, do)))
            out = actions.act(secret, person.login_id, form, work, done="変えました。前の値は記録に残っています。", reason_field="reason")
            return _back(back, out)
        return handle(request, render)

    @app.get("/admin/settings")
    def system_settings(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        """システムの初めの値（管理者だけ。管理者でなければ「見つかりません」）。"""
        return _settings_page(request, "system", None, None, done, ok)

    @app.post("/admin/settings")
    async def system_settings_save(request: Request) -> HTMLResponse:
        return _settings_save(request, dict(await request.form()), "system", None, None, "/admin/settings")

    @app.get("/t/settings")
    def tenant_settings(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _settings_page(request, "tenant", None, None, done, ok)

    @app.post("/t/settings")
    async def tenant_settings_save(request: Request) -> HTMLResponse:
        return _settings_save(request, dict(await request.form()), "tenant", None, None, "/t/settings")

    @app.get("/c/{client_id}/settings")
    def client_settings(request: Request, client_id: UUID, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _settings_page(request, "client", client_id, None, done, ok)

    @app.post("/c/{client_id}/settings")
    async def client_settings_save(request: Request, client_id: UUID) -> HTMLResponse:
        return _settings_save(request, dict(await request.form()), "client", client_id, None, f"/c/{client_id}/settings")

    @app.get("/c/{client_id}/b/{business_id}/settings")
    def business_settings_page(request: Request, client_id: UUID, business_id: UUID, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _settings_page(request, "business", client_id, business_id, done, ok)

    @app.post("/c/{client_id}/b/{business_id}/settings")
    async def business_settings_save(request: Request, client_id: UUID, business_id: UUID) -> HTMLResponse:
        return _settings_save(request, dict(await request.form()), "business", client_id, business_id,
                              f"/c/{client_id}/b/{business_id}/settings")

    # ── PC のログイン（RFC 8628 の機器のログイン。待ちと払い出しは core の agents.login_* の1か所＝データベース。PC の側は agent/pc_login.py） ──
    starts: dict[str, list[float]] = {}   # 始める入口の数の絞り（ログイン不要の入口なので、同じ所から続けて呼ばれたら断る＝CL-2 4-423）
    START_LIMIT, START_WINDOW = 10, 60.0

    def _too_many(where: str) -> bool:
        now = time.monotonic()
        recent = [t for t in starts.get(where, []) if now - t < START_WINDOW]
        starts[where] = recent + [now]
        if len(starts) > 10_000:   # 覚えておく所の数も絞る（古いものから捨てる）
            for k in list(starts)[:5_000]:
                del starts[k]
        return len(recent) >= START_LIMIT

    async def _json(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @app.post("/pc-login/start")
    async def pc_login_start(request: Request) -> JSONResponse:
        """PC がログインを始める（ログイン不要）。確かめのコードと、契約者が開くページを返す。"""
        where = (request.headers.get(client_ip_header) if client_ip_header else None) or (request.client.host if request.client else "?")
        if _too_many(where):
            return JSONResponse({"error": "slow_down"}, status_code=429)
        body = await _json(request)
        got = agents.login_start(access, label=str(body.get("pc_label") or ""), replaces=body.get("replaces"))
        page_url = str(request.base_url).rstrip("/") + "/pc-login"
        return JSONResponse({"device_code": got.device_code, "user_code": got.user_code, "verification_uri": page_url,
                             "verification_uri_complete": f"{page_url}?code={got.user_code}", "interval": got.interval,
                             "expires_in": got.expires_in}, headers={"Cache-Control": "no-store"})

    @app.post("/pc-login/token")
    async def pc_login_token(request: Request) -> JSONResponse:
        """PC が通行証を受け取る（core の agents.login_take＝払い出しは1回だけ・前の PC の通行証を止める・許可した人を記録）。"""
        body = await _json(request)
        state, got = agents.login_take(access, str(body.get("device_code") or ""))
        if got is None:
            return JSONResponse({"error": state}, status_code=400)
        return JSONResponse({"tenant_id": str(got.tenant_id), "pc": str(got.pc_id), "token": got.token},
                            headers={"Cache-Control": "no-store"})

    @app.get("/pc-login")
    def pc_login_page(request: Request, code: str = "", done: str | None = None, ok: int = 1) -> HTMLResponse:
        """契約者が PC を許可するページ（ログインが要る）。コードが PC の画面と同じかを確かめてもらう。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            pending = ctx.tenant(lambda tx: agents.login_waiting(tx, code)) if code else None
            return page(request, "pc_login.html", person, ctx, code=agents.login_code(code) if code else "", pending=pending,
                        done=done, ok=bool(ok), active="lines")
        return handle(request, render)

    @app.post("/pc-login")
    async def pc_login_decide(request: Request) -> HTMLResponse:
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            approve = str(form.get("decision") or "") == "approve"
            def do() -> Any:
                label = ctx.tenant(lambda tx: agents.login_decide(tx, str(form.get("code") or ""), approve=approve))
                if label is None:
                    raise actions.Refused("そのコードの PC は見つかりません（期限の10分を過ぎたか、もう決めたコードです。PC の画面の新しいコードで、もう一度開いてください）")
                return label
            out = actions.act(secret, person.login_id, form, do,
                              done=(f"この PC を「{ctx.tenant_name}」で使えるようにしました。PC の画面に戻ると、自動で動き出します。" if approve
                                    else "断りました。この PC はログインしていません。"))
            return _back("/pc-login", out)
        return handle(request, render)

    # ── 費用と報酬（docs/DASHBOARD.md 5-11。Intent.md 5-2・5-3 (9)） ──────────────────────────
    @app.get("/t/costs")
    def costs_page(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        """今月の報酬と費用（依頼者ごと＝core の reward.month の1か所）と、単価の表（usage.price_table の1か所。契約者の値が初めの値より強い）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            # 赤字か（Q26）は core の usage.deficit_notice の答えだけを見せる（画面で割合を計算して決め直さない＝Intent.md 5-1）
            month = [{**c, **ctx.client(c["client_id"], lambda tx: {**reward.month(tx), "deficit": usage.deficit_notice(tx)},
                                        read_only=True)} for c in ctx.clients()]
            def read_prices(tx: Any) -> list[dict[str, Any]]:   # 回線の方式の画面の名前は registry.line_methods の1か所
                labels = {m["method"]: m["label"] for m in registry.line_methods(tx)}
                return [{**p, "service_label": labels.get(p["service"], p["service"]) if p["source"] == "line" else p["service"]}
                        for p in usage.price_table(tx)]
            return page(request, "costs.html", person, ctx, active="costs", month=month, prices=ctx.tenant(read_prices),
                        done=done, ok=bool(ok))
        return handle(request, render)

    @app.post("/t/costs/price")
    async def price_set(request: Request) -> HTMLResponse:
        """単価を足す（前の値は残る。この契約者の単価＝初めの値より強い。core の usage.set_price だけ）。画面の量あたりの円を、1単位あたりに直す
        （倍数は output/words.PRICE_UNITS の1か所）。"""
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def do(tx: Any) -> int:
                source, service, unit = (str(form.get(k) or "") for k in ("source", "service", "unit"))
                tier = str(form.get("tier") or "") or None
                if source not in words.PRICE_SOURCE_WORDS or unit not in words.PRICE_UNITS or not service:
                    raise NotFound()
                raw = str(form.get("yen") or "").strip().replace(",", "")
                try:
                    yen = Decimal(raw)
                except Exception:
                    raise actions.Refused("円は数で入れてください（例：12.5）") from None
                if not yen.is_finite() or yen < 0:
                    raise actions.Refused("円は 0 以上の数で入れてください")
                return usage.set_price(tx, source=source, service=service, tier=tier, unit=unit,
                                       yen_per_unit=yen / Decimal(words.PRICE_UNITS[unit][0]), valid_from=datetime.now(timezone.utc),
                                       note=str(form.get("reason") or "").strip())
            out = actions.act(secret, person.login_id, form, lambda: ctx.tenant(do), done="単価を変えました。いまから記録する利用量に使います（前の値は残ります）。",
                              reason_field="reason")
            return _back("/t/costs", out)
        return handle(request, render)

    # ── 回線と PC（docs/DASHBOARD.md 5-5。Intent.md 5-3 (4)） ──────────────────────────
    @app.get("/t/lines")
    def lines_page(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        """回線（状態の段は core の健康の表の1か所＝health.line_of、言葉は health_rules.LineStage）と PC の一覧。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                lines = []
                for ln in tx.all("SELECT line_id, name, method, caller_e164, client_id, retired_at, retired_reason FROM salescall.lines"
                                 " ORDER BY retired_at NULLS FIRST, name, line_id"):
                    st = health.line_of(tx, ln["line_id"])
                    lines.append({**ln, "stage": st.stage, "wait": st.wait, "next_at": st.next_at,
                                  "resting": st.stage in health_rules.RESTS})
                return {"lines": lines, "methods": registry.line_methods(tx), "pcs": agents.pcs(tx)}
            return page(request, "lines.html", person, ctx, active="lines", done=done, ok=bool(ok),
                        client_names={c["client_id"]: c["name"] for c in ctx.clients()}, **ctx.tenant(read))
        return handle(request, render)

    def _line_act(request: Request, form: dict[str, Any], do: Any, done: str, reason: bool = False) -> HTMLResponse:
        """回線と PC の操作の1か所（core の部品の理由つきの断り＝ValueError・PermissionError は、その文のまま出す）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def work() -> Any:
                try:
                    return ctx.tenant(lambda tx: do(tx, ctx))
                except (ValueError, PermissionError) as e:   # registry・health の、人に見せる理由つきの断り（何も書いていない）
                    if isinstance(e, actions.Refused):
                        raise
                    raise actions.Refused(str(e)) from None
            out = actions.act(secret, person.login_id, form, work, done=done, reason_field="reason" if reason else None)
            return _back("/t/lines", out)
        return handle(request, render)

    def _client_or_shared(tx: Any, raw: Any) -> UUID | None:
        """回線の使い先（空＝共有）。同じ契約者の取引の中で、その契約者の依頼者かを確かめる（取引の中で別の取引を開かない）。"""
        if not raw:
            return None
        try:
            cid = UUID(str(raw))
        except ValueError:
            raise NotFound() from None
        if tx.one("SELECT 1 FROM salescall.clients WHERE client_id = %s", (cid,)) is None:
            raise NotFound()
        return cid

    @app.post("/t/lines")
    async def line_create(request: Request) -> HTMLResponse:
        form = dict(await request.form())
        return _line_act(request, form, lambda tx, ctx: registry.create_line(
            tx, actions.name_of(form), method=str(form.get("method") or ""), caller=str(form.get("caller") or ""),
            client_id=_client_or_shared(tx, form.get("client_id"))), "回線を作りました。")

    @app.post("/t/lines/{line_id}/assign")
    async def line_assign(request: Request, line_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        return _line_act(request, form, lambda tx, ctx: registry.assign_line(tx, line_id, _client_or_shared(tx, form.get("client_id"))),
                         "回線の使い先を変えました（まだ発信していない予約は取り消しました）。")

    @app.post("/t/lines/{line_id}/resume")
    async def line_resume(request: Request, line_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        return _line_act(request, form, lambda tx, ctx: health.by_hand_line(tx, line_id, str(form.get("reason") or "")),
                         "回線を再開しました。", reason=True)

    @app.post("/t/lines/{line_id}/retire")
    async def line_retire(request: Request, line_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        return _line_act(request, form, lambda tx, ctx: registry.retire_line(tx, line_id, reason=str(form.get("reason") or "")),
                         "回線を使用停止にしました。", reason=True)

    @app.post("/t/pcs/{pc_id}/revoke")
    async def pc_revoke(request: Request, pc_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        return _line_act(request, form, lambda tx, ctx: agents.revoke(tx, pc_id),
                         "この PC のログインを外しました（話している電話は、終わってから効きます）。")

    # ── 発信禁止（docs/DASHBOARD.md 5-10。Intent.md 原則14・1-4）：依頼者の台帳と、契約者の共通の発信禁止 ──────────
    def _dnc_rows(tx: Any, table: str) -> list[dict[str, Any]]:
        return tx.all(f"SELECT entry_id, phone_e164, reason, registered_at, revoked_at, revoke_reason,"
                      f"       to_jsonb(d) ->> 'client_ref' AS client_ref FROM salescall.{table} d"
                      f" ORDER BY revoked_at NULLS FIRST, registered_at DESC LIMIT 500")

    def _dnc_page(request: Request, client_id: UUID | None, done: str | None, ok: int) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            if client_id is None:
                values = ctx.tenant(lambda tx: {"rows": _dnc_rows(tx, "contractor_dnc_entries")})
            else:
                values = ctx.client(client_id, lambda tx: {
                    "rows": _dnc_rows(tx, "dnc_entries"), "businesses": context.businesses(tx),
                    "client": tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,))}, read_only=True)
            return page(request, "dnc.html", person, ctx, active="dnc" if client_id is None else "client", done=done, ok=bool(ok), **values)
        return handle(request, render)

    # ── 発信禁止の一覧の取り込み（Intent.md 7-2「依頼者から受け取った『かけないで』の一覧も…プレビューで確かめてから台帳に入れる」・
    #    「契約者の共通の台帳にも、同じ入口で入れられる」）。行の結果・登録は core の dnc_import.preview・register の1か所。ファイルの
    #    受け方と一時置き場は、お客様リストの取り込みと同じ1か所（_receive_file・uploads）。依頼者の台帳と契約者の共通の台帳は、
    #    同じこの部品で、取引（依頼者／契約者）と住所だけが違う。
    def _dnc_in(ctx: Ctx, client_id: UUID | None, work: Callable[[Any], Any], *, read_only: bool = False) -> Any:
        return ctx.tenant(work) if client_id is None else ctx.client(client_id, work, read_only=read_only)

    def _dnc_place(ctx: Ctx, client_id: UUID | None) -> tuple[Any, ...]:
        return ("dnc", ctx.tenant_id, client_id)

    def _dnc_import_page(request: Request, client_id: UUID | None, stage: str, *, key: str = "",
                         extra: Callable[[Person, Ctx], dict[str, Any]] | None = None, **values: Any) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            if extra is not None:
                values.update(extra(person, ctx))
            if client_id is not None:
                values.update(ctx.client(client_id, lambda tx: {
                    "client": tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,)),
                    "businesses": context.businesses(tx)}, read_only=True))
            fields = [f for f in dnc_import.FIELDS if client_id is not None or f == "phone"]   # 契約者の共通の台帳は番号だけ（core と同じ）
            values.setdefault("stage", stage)   # 行ごとの結果を出せなかったときは、列の段に戻す（extra が決める）
            return page(request, "dnc_import.html", person, ctx, active="dnc" if client_id is None else "client", key=key,
                        fields=fields, encodings=[(e, import_files._NAMES.get(e, e)) for e in import_files.ENCODINGS], **values)
        return handle(request, render)

    def _dnc_base(client_id: UUID | None) -> str:
        return "/t/dnc/import" if client_id is None else f"/c/{client_id}/dnc/import"

    def _dnc_import_routes(prefix: str, scoped: bool) -> None:
        """依頼者の台帳（/c/{依頼者}/dnc/import）と契約者の共通の台帳（/t/dnc/import）の入口を、同じ部品で作る。"""
        def cid(request: Request) -> UUID | None:
            return request.path_params.get("client_id") if scoped else None

        @app.get(prefix)
        def dnc_import_start(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
            return _dnc_import_page(request, cid(request), "upload", done=done, ok=bool(ok))

        @app.post(prefix)
        async def dnc_import_upload(request: Request) -> HTMLResponse:
            c = cid(request)
            return await _receive_file(request, _dnc_base(c), check=lambda ctx: _dnc_in(ctx, c, lambda tx: None, read_only=True),
                                       place=lambda ctx: _dnc_place(ctx, c))

        @app.get(prefix + "/{key}")
        def dnc_import_map(request: Request, key: str) -> HTMLResponse:
            c = cid(request)
            def extra(person: Person, ctx: Ctx) -> dict[str, Any]:
                u = _upload_or_missing(person, _dnc_place(ctx, c), key)
                sheet = _sheet(u)
                guess = {f: h for f, h in importing.guess(sheet).items() if f in dnc_import.FIELDS and (c is not None or f == "phone")}
                return {"sheet": sheet, "sample": sheet.rows[:3], "mapping": u.mapping or guess, "file_name": u.file_name}
            return _dnc_import_page(request, c, "map", key=key, extra=extra)

        @app.post(prefix + "/{key}/preview")
        async def dnc_import_preview(request: Request, key: str) -> HTMLResponse:
            c = cid(request)
            form = await request.form()
            def extra(person: Person, ctx: Ctx) -> dict[str, Any]:
                u = _upload_or_missing(person, _dnc_place(ctx, c), key)
                sheet = _sheet(u)
                u.mapping = {f: str(form.get(f"map_{f}")) for f in dnc_import.FIELDS if form.get(f"map_{f}")}
                try:
                    plan = _dnc_in(ctx, c, lambda tx: dnc_import.preview(tx, sheet, u.mapping), read_only=True)
                except actions.REFUSALS as e:
                    u.preview = {}
                    return {"stage": "map", "sheet": sheet, "sample": sheet.rows[:3], "mapping": u.mapping, "error": str(e),
                            "file_name": u.file_name}
                u.preview = {"digest": plan.digest}
                return {"stage": "preview", "counts": plan.counts(), "file_name": u.file_name,
                        "stopped": [r for r in plan.rows if r.outcome == "stopped"]}
            return _dnc_import_page(request, c, "preview", key=key, extra=extra)

        @app.post(prefix + "/{key}/register")
        async def dnc_import_register(request: Request, key: str) -> HTMLResponse:
            c = cid(request)
            form = dict(await request.form())
            back = "/t/dnc" if c is None else f"/c/{c}/dnc"
            def render(person: Person, ctx: Ctx) -> HTMLResponse:
                u = _upload_or_missing(person, _dnc_place(ctx, c), key)
                if not u.preview:
                    raise NotFound()
                out = actions.act(secret, person.login_id, form, lambda: _dnc_in(ctx, c, lambda tx: dnc_import.register(
                    tx, _sheet(u), u.mapping, expected_digest=u.preview["digest"])), done="")
                if not out.ok:
                    return _back(_dnc_base(c), out)
                uploads.drop(key)
                n = out.value["counts"]
                return _back(back, actions.Outcome(True, f"取り込みました：新しく止めた行 {n['registered']}行（番号 {n['numbers']}件・"
                                                         f"管理番号 {n['client_refs']}件）・登録済み {n['already']}行・止められなかった行 "
                                                         f"{n['stopped']}行・空の行 {n['empty']}行。"))
            return handle(request, render)

    _dnc_import_routes("/t/dnc/import", scoped=False)
    _dnc_import_routes("/c/{client_id}/dnc/import", scoped=True)

    def _dnc_act(request: Request, form: dict[str, Any], client_id: UUID | None, do: Any, done: str, *,
                 reason: bool = True) -> HTMLResponse:
        """発信禁止の登録・取り消しの1か所（core の dnc だけ。理由は必ず。番号が読めない＝core の理由の文のまま）。"""
        back = "/t/dnc" if client_id is None else f"/c/{client_id}/dnc"
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def work(tx: Any) -> Any:
                try:
                    return do(tx)
                except dnc.DncEntryNotFound:
                    raise LookupError("その登録は無い") from None
                except ValueError as e:   # dnc の理由つきの断り（そろえられない番号 など）
                    if isinstance(e, actions.Refused):
                        raise
                    raise actions.Refused(str(e)) from None
            run = (lambda: ctx.tenant(work)) if client_id is None else (lambda: ctx.client(client_id, work))
            return _back(back, actions.act(secret, person.login_id, form, run, done=done, reason_field="reason" if reason else None))
        return handle(request, render)

    def _entry(form: dict[str, Any]) -> UUID:
        try:
            return UUID(str(form.get("entry_id") or ""))
        except ValueError:
            raise NotFound() from None

    @app.get("/t/dnc")
    def tenant_dnc(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _dnc_page(request, None, done, ok)

    @app.post("/t/dnc")
    async def tenant_dnc_add(request: Request) -> HTMLResponse:
        form = dict(await request.form())
        return _dnc_act(request, form, None, lambda tx: dnc.register_contractor(tx, str(form.get("phone") or ""),
                                                                                str(form.get("reason") or "").strip()),
                        "登録しました。この契約者のすべての依頼者で、この番号へはかけません。")

    @app.post("/t/dnc/revoke")
    async def tenant_dnc_revoke(request: Request) -> HTMLResponse:
        form = dict(await request.form())
        return _dnc_act(request, form, None, lambda tx: dnc.revoke_contractor(tx, _entry(form), str(form.get("reason") or "").strip()),
                        "取り消しました。この登録で終えた行は、決まった間隔の仕事が予定に戻します。")

    @app.get("/c/{client_id}/dnc")
    def client_dnc(request: Request, client_id: UUID, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _dnc_page(request, client_id, done, ok)

    @app.post("/c/{client_id}/dnc")
    async def client_dnc_add(request: Request, client_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        # ⏳ dnc.register は理由の文を受けない（理由の名前 manual だけ）＝理由の欄を出さない（残らない理由を「記録に残ります」と書かない）。CL-2 へのお願い 25
        return _dnc_act(request, form, client_id, lambda tx: dnc.register(tx, str(form.get("phone") or ""), "manual"),
                        "登録しました。この番号のお客様へは、この依頼者のどの一覧からもかけません。", reason=False)

    @app.post("/c/{client_id}/dnc/revoke")
    async def client_dnc_revoke(request: Request, client_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        return _dnc_act(request, form, client_id, lambda tx: dnc.revoke(tx, _entry(form), str(form.get("reason") or "").strip()),
                        "取り消しました。この登録で終えた行は予定に戻り、依頼者へ渡す結果の「営業拒否」も外れます。")

    # ── 依頼者・業務・実施回を作る（作るのは core の registry だけ。docs/DASHBOARD.md 5-6） ──────────
    @app.post("/t/clients")
    async def create_client(request: Request) -> HTMLResponse:
        """見ている契約者に依頼者を作る（契約者の取引）。作ったら、その依頼者のタブを開く。"""
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            out = actions.act(secret, person.login_id, form,
                              lambda: ctx.tenant(lambda tx: registry.create_client(tx, actions.name_of(form))), done="依頼者を作りました。")
            return _back(f"/c/{out.value}" if out.ok else "/t", out)
        return handle(request, render)

    @app.post("/c/{client_id}/businesses")
    async def create_business(request: Request, client_id: UUID) -> HTMLResponse:
        """依頼者に業務を作る（依頼者の取引）。作ったら、その業務のタブを開く（業務設定の版は、業務の取り込みで作る）。"""
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            out = actions.act(secret, person.login_id, form,
                              lambda: ctx.client(client_id, lambda tx: registry.create_business(tx, actions.name_of(form))),
                              done="業務を作りました。")
            return _back(f"/c/{client_id}/b/{out.value}" if out.ok else f"/c/{client_id}", out)
        return handle(request, render)

    @app.post("/c/{client_id}/b/{business_id}/runs")
    async def create_run(request: Request, client_id: UUID, business_id: UUID) -> HTMLResponse:
        """業務に実施回を作り、選んだ業務設定の版を割り当てる（core の registry.create_run＝割り当ての確かめは business_settings の1か所）。
        作ったら、その実施回のお客様リストの取り込みを開く。"""
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def do(tx: Any) -> UUID:
                context.business(tx, business_id)
                try:
                    version = UUID(str(form.get("version_id") or ""))
                except ValueError:
                    raise actions.Refused("業務設定の版を選んでください") from None
                if tx.one("SELECT 1 FROM salescall.business_versions WHERE version_id = %s AND business_id = %s",
                          (version, business_id)) is None:
                    raise NotFound()
                return registry.create_run(tx, business_id, actions.name_of(form), version, reason="ダッシュボードで実施回を作った")
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, do), done="実施回を作りました。")
            base = f"/c/{client_id}/b/{business_id}"
            return _back(f"{base}/runs/{out.value}/import" if out.ok else base, out)
        return handle(request, render)

    # ── 依頼者と業務（タブは2段：依頼者 → 業務。✅ 2026-11-18 管理者） ──────────────
    def _client_or_business(request: Request, client_id: UUID, business_id: UUID | None, done: str | None = None,
                            ok: int = 1) -> HTMLResponse:
        """依頼者のタブ（business_id が None＝概要・全業務）と業務のタブは、同じこの1つの画面の部品で出す（絞り方が違うだけ）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                c = tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,))
                b = None if business_id is None else context.business(tx, business_id)
                runs = tx.all("SELECT r.run_id, r.name, b.business_id, b.name AS business FROM salescall.runs r"
                              " JOIN salescall.businesses b ON b.business_id = r.business_id"
                              " WHERE %s::uuid IS NULL OR r.business_id = %s ORDER BY r.created_at DESC, r.run_id",
                              (business_id, business_id))
                versions = [] if business_id is None else tx.all(
                    "SELECT version_id, version_no, created_at FROM salescall.business_versions WHERE business_id = %s"
                    " ORDER BY version_no DESC", (business_id,))
                needs, stages = [], []   # 業務のタブだけ：録音の要るもの（recordings.version_needs）と品質の段（quality_stage）。実施回のいまの版ごと
                verification = business_id is not None and approval_core.human_for_business(tx, business_id)
                for r in (runs if business_id is not None else []):
                    v = tx.one("SELECT salescall.run_current_version(%s) AS v", (r["run_id"],))["v"]
                    if v is not None and verification:
                        stages.append({"run": r["name"], "stage": quality_stage.current(tx, v), "used": quality_stage.used(tx, v),
                                       "metrics": quality_stage.metrics(tx, v)})
                    if v is not None:
                        got = recordings.version_needs(tx, v)
                        if not got.listed or got.parts:
                            needs.append({"run": r["name"], "listed": got.listed, "parts": got.parts})
                return {"client": c, "business": b, "businesses": context.businesses(tx),
                        "today": counts.client_today(tx, business_id), "runs": runs, "versions": versions, "needs": needs,
                        "verification": verification, "stages": stages,
                        "deficit": usage.deficit_notice(tx)}   # 赤字か＝core の1か所の答え（依頼者全体のもの。業務のタブにも出す）
            values = ctx.client(client_id, read, read_only=True)
            return page(request, "client.html", person, ctx, waiting=_waiting(ctx, client_id, business_id), done=done, ok=bool(ok),
                        stopped=counts.stops_for(counts.open_stops(ctx), client_id, business_id), **values)
        return handle(request, render)

    @app.get("/c/{client_id}")
    def client_home(request: Request, client_id: UUID, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _client_or_business(request, client_id, None, done, ok)

    @app.get("/c/{client_id}/b/{business_id}")
    def business_home(request: Request, client_id: UUID, business_id: UUID, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _client_or_business(request, client_id, business_id, done, ok)

    @app.get("/c/{client_id}/search")
    def client_search(request: Request, client_id: UUID, q: str = "") -> HTMLResponse:
        """その依頼者のお客様の行を探す（会社名・お呼びする方・管理番号の一部、または電話番号＝そろえ方は core の phone.normalize の1か所）。
        住所の依頼者の取引の中だけを探す（ほかの依頼者・契約者の行は出ない＝データベースが見せない）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            term = q.strip()
            def read(tx: Any) -> dict[str, Any]:
                found: list[dict[str, Any]] = []
                if term:
                    n = phone.normalize(term)
                    found = tx.all(
                        "SELECT DISTINCT t.task_id, t.source_name, t.target_name, t.client_ref, r.run_id, r.name AS run, r.business_id,"
                        "       b.name AS business FROM salescall.tasks_now t JOIN salescall.runs r ON r.run_id = t.run_id"
                        " JOIN salescall.businesses b ON b.business_id = r.business_id"
                        " WHERE strpos(coalesce(t.source_name, ''), %s) > 0 OR strpos(coalesce(t.target_name, ''), %s) > 0"
                        "    OR strpos(coalesce(t.client_ref, ''), %s) > 0"
                        "    OR (%s::text IS NOT NULL AND EXISTS (SELECT 1 FROM salescall.task_numbers k WHERE k.task_id = t.task_id"
                        "                                          AND k.phone_e164 = %s))"
                        " ORDER BY t.source_name NULLS LAST, t.task_id LIMIT 200", (term, term, term, n.e164, n.e164))
                return {"client": tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,)),
                        "businesses": context.businesses(tx), "found": found}
            return page(request, "search.html", person, ctx, q=term, **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    @app.get("/c/{client_id}/b/{business_id}/versions/{version_id}")
    def version_page(request: Request, client_id: UUID, business_id: UUID, version_id: UUID) -> HTMLResponse:
        """業務設定の版の中身（読むだけ。読むのは core の business_settings.load の1か所＝置いたときと同じ確かめで読む）。"""
        from salescall.core import business_settings
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                b = context.business(tx, business_id)
                v = tx.one("SELECT version_id, version_no, created_at FROM salescall.business_versions WHERE version_id = %s"
                           " AND business_id = %s", (version_id, business_id))
                if v is None:
                    raise NotFound()
                try:
                    s, problem = business_settings.load(tx, version_id), None
                except business_settings.SettingsInvalid as e:
                    s, problem = None, f"この版は、いまの確かめに通りません：{e}"
                except LookupError:   # 中身をまだ置いていない版（業務の取り込みで置く）
                    s, problem = None, "この版には、まだ業務設定の中身がありません（業務の取り込みで作ります）。"
                return {"client": tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,)),
                        "business": b, "businesses": context.businesses(tx), "version": v, "s": s, "problem": problem}
            return page(request, "version.html", person, ctx, **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}")
    def rows_page(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, done: str | None = None,
                  ok: int = 1) -> HTMLResponse:
        """お客様リスト（実施回ごと）。住所の依頼者の取引で、業務がその依頼者のもの・実施回がその業務のものかを確かめる。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                c = tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,))
                b = context.business(tx, business_id)
                run = tx.one("SELECT run_id, name FROM salescall.runs WHERE run_id = %s AND business_id = %s", (run_id, business_id))
                if run is None:
                    raise NotFound()
                return {"client": c, "business": b, "businesses": context.businesses(tx), "run": run,
                        "rows": customer_rows.progress(tx, run_id), "levels": _levels(tx, run_id)}
            return page(request, "rows.html", person, ctx, done=done, ok=bool(ok), level_names=exclusions.LEVEL_NAMES,
                        **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/exclude")
    async def rows_exclude(request: Request, client_id: UUID, business_id: UUID, run_id: UUID) -> HTMLResponse:
        """「かけない」の印（レ点・一括。1行の画面からも同じ所）。付けるのは core の1か所（exclusions.mark）。選んだ行がすべて、住所の
        実施回の行でなければ、何も書かない（「見つかりません」）。理由は必ず。"""
        form = await request.form(max_fields=200_000)   # 選んだ行の数だけ欄が来る（行の多い実施回でも、まとめて付けられる）
        ids = [str(v) for v in form.getlist("task_id")]
        level = str(form.get("level") or "")
        back = str(form.get("back") or "")
        base = f"/c/{client_id}/b/{business_id}/runs/{run_id}"
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def do(tx: Any) -> int:
                if level not in exclusions.LEVELS:
                    raise NotFound()
                if not ids:
                    raise actions.Refused("印を付ける行を選んでください")
                context.business(tx, business_id)
                try:
                    tasks_ = [UUID(i) for i in ids]
                except ValueError:
                    raise NotFound() from None
                found = tx.one("SELECT count(*) AS n FROM salescall.tasks t JOIN salescall.runs r ON r.run_id = t.run_id"
                               " WHERE t.task_id = ANY(%s) AND r.run_id = %s AND r.business_id = %s", (tasks_, run_id, business_id))["n"]
                if found != len(set(tasks_)):
                    raise NotFound()
                return len(exclusions.mark(tx, tasks_, level, reason=str(form.get("reason") or "").strip()))
            out = actions.act(secret, person.login_id, dict(form), lambda: ctx.client(client_id, do), done="", reason_field="reason")
            msg = f"{out.value}行を「{exclusions.LEVEL_NAMES[level]}」にしました（同じ印だった行は、そのままです）。" if out.ok else out.message
            target = f"{base}/rows/{ids[0]}" if back == "row" and len(ids) == 1 else base
            return _back(target, actions.Outcome(out.ok, msg))
        return handle(request, render)

    def _levels(tx: Any, run_id: UUID) -> dict[UUID, str]:
        """実施回の行ごとの「かけない」の印（どの印かはデータベースの1か所 task_exclusion_level を読むだけ）。"""
        return {r["task_id"]: r["level"] for r in tx.all(
            "SELECT task_id, salescall.task_exclusion_level(task_id) AS level FROM salescall.tasks WHERE run_id = %s", (run_id,))}

    # ── 結果の書き出し（docs/DESIGN.md 7-3。行は output/result_rows、ファイルは output/files の1か所） ──────────
    def _results_file(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, *, submit: bool,
                      form: dict[str, Any] | None = None) -> Response:
        """その実施回の結果の CSV。submit＝依頼者へ渡すファイルとして書き出す（提出用の控えに置く。同じ取引で）。依頼者へ出さない言葉が
        入っていれば、書き出さずに止める（output/files.check）。"""
        def render(person: Person, ctx: Ctx) -> Response:
            base = f"/c/{client_id}/b/{business_id}/runs/{run_id}"
            def work(tx: Any) -> tuple[str, bytes, bool]:
                run = _run_in(tx, business_id, run_id)
                c = tx.one("SELECT name FROM salescall.clients WHERE client_id = %s", (client_id,))
                got = result_rows.rows(tx, run_id)
                name = result_files.file_name(c["name"], run["name"], tx.today())
                data = result_files.to_csv(got, run["name"])
                return name, data, (result_files.submit(tx, got, name, data) if submit else False)
            if submit:   # 依頼者へ渡す：変える操作の1か所（札・断りの言い換え）を通す
                out = actions.act(secret, person.login_id, form or {}, lambda: ctx.client(client_id, work), done="")
                if not out.ok:
                    return _back(base, out)
                name, data, _ = out.value
            else:
                try:
                    name, data, _ = ctx.client(client_id, work, read_only=True)
                except result_files.ClientWordsLeak as e:
                    return _back(base, actions.Outcome(False, str(e)))
            return Response(data, media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f"attachment; filename=\"results.csv\"; filename*=UTF-8''{quote(name)}",
                                     "Cache-Control": "no-store"})
        return handle(request, render)

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}/results")
    def results_page(request: Request, client_id: UUID, business_id: UUID, run_id: UUID) -> HTMLResponse:
        """依頼者へ渡す内容の見本（書き出すファイルと同じ行＝output/result_rows の1か所。依頼者へ出さない言葉の確かめも同じ）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                run = _run_in(tx, business_id, run_id)
                got = result_rows.rows(tx, run_id)
                try:
                    result_files.check(got)
                    leak = None
                except result_files.ClientWordsLeak as e:
                    leak = str(e)
                return {"client": tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,)),
                        "business": context.business(tx, business_id), "businesses": context.businesses(tx), "run": run,
                        "rows": got, "leak": leak}
            return page(request, "results.html", person, ctx, columns=result_rows.COLUMNS, please_fill=result_rows.PLEASE_FILL, zip=zip,
                        **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}/results.csv")
    def results_check(request: Request, client_id: UUID, business_id: UUID, run_id: UUID) -> Response:
        """確かめるために書き出す（控えには置かない）。"""
        return _results_file(request, client_id, business_id, run_id, submit=False)

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/results.csv")
    async def results_submit(request: Request, client_id: UUID, business_id: UUID, run_id: UUID) -> Response:
        """依頼者へ渡すファイルとして書き出す（提出用の控えに置く）。"""
        return _results_file(request, client_id, business_id, run_id, submit=True, form=dict(await request.form()))

    # ── お客様リストの取込（docs/DASHBOARD.md 5-8）：ファイル → 列と欄の対応付け → プレビュー（行ごとの合否）→ 登録 ──────
    def _run_in(tx: Any, business_id: UUID, run_id: UUID) -> dict[str, Any]:
        """住所の業務・実施回が、住所の依頼者の中でつながっているか（違えば NotFound）。実施回を返す。"""
        context.business(tx, business_id)
        run = tx.one("SELECT run_id, name FROM salescall.runs WHERE run_id = %s AND business_id = %s", (run_id, business_id))
        if run is None:
            raise NotFound()
        return run

    def _import_page(request: Request, name: str, client_id: UUID, business_id: UUID, run_id: UUID, *,
                     extra: Any = None, **values: Any) -> HTMLResponse:
        """取込の画面を出す1か所。extra＝ログインした人から足す値（置き場のファイルなど。見つからなければ NotFound）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            if extra is not None:
                values.update(extra(person, ctx))
            def read(tx: Any) -> dict[str, Any]:
                return {"client": tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,)),
                        "business": context.business(tx, business_id), "businesses": context.businesses(tx),
                        "run": _run_in(tx, business_id, run_id)}
            return page(request, name, person, ctx, field_words=words.IMPORT_FIELD_WORDS,
                        encodings=[(e, import_files._NAMES.get(e, e)) for e in import_files.ENCODINGS],   # 名前の正本は import_files の組
                        **values, **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    def _sheet(u: uploads.Upload) -> import_files.Sheet:
        return import_files.read(u.file_name, u.data, sheet_name=u.sheet_name, encoding=u.encoding)

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}/import")
    def import_start(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, done: str | None = None,
                     ok: int = 1) -> HTMLResponse:
        return _import_page(request, "import.html", client_id, business_id, run_id, done=done, ok=bool(ok))

    async def _receive_file(request: Request, base: str, *, check: Callable[[Ctx], Any],
                            place: Callable[[Ctx], tuple[Any, ...]]) -> HTMLResponse:
        """取り込むファイルを受ける1か所（お客様リスト・発信禁止の一覧）。読む順：ログインの確かめ → 送られてくる大きさ（読む前に断る）→
        中身を読む → 中身の大きさ → 読めるか（表として開く）→ 一時置き場に置く（置き場＝place。書くのは手元の記憶だけ）。check＝住所の
        依頼者・実施回などを確かめる（違えば NotFound）。置いたら base/{鍵} へ進む。ログインしていない方の送ったファイルは読まない。"""
        if identify(request) is None:
            return RedirectResponse("/login", status_code=303)
        try:
            size = int(request.headers.get("content-length") or "")
            uploads.check_size(size - 64 * 1024)   # フォームの他の欄と区切りの分を引く
        except ValueError as e:   # 大きさが分からない送り方・大きすぎる（TooLarge も ValueError）
            return _back(base, actions.Outcome(False, str(e) if isinstance(e, uploads.TooLarge) else "ファイルを選び直してください。"))
        try:
            form = await request.form(max_files=1, max_fields=20)
        except HTTPException:   # 送り方の形が違う（ファイルが2つ以上・欄が多すぎる）。英語の返事を出さない
            return _back(base, actions.Outcome(False, "ファイルを1つだけ選んで、もう一度送ってください。"))
        f = form.get("file")
        data = await f.read() if hasattr(f, "read") else b""
        file_name = getattr(f, "filename", "") or ""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            check(ctx)
            def do() -> str:
                if not data:
                    raise actions.Refused("ファイルを選んでください")
                try:
                    uploads.check_size(len(data))   # 開く前に大きさ（大きなファイルを開いて重くしない）
                except uploads.TooLarge as e:
                    raise actions.Refused(str(e)) from None
                u = uploads.Upload(person.login_id, place(ctx), file_name, data,
                                   sheet_name=str(form.get("sheet_name") or "").strip() or None,
                                   encoding=str(form.get("encoding") or "") or None)
                _sheet(u)   # 読めるか（読めなければ理由の文つきで断る）
                return uploads.put(u)
            out = actions.act(secret, person.login_id, dict(form), do, done="")
            if not out.ok:
                return _back(base, out)
            return RedirectResponse(f"{base}/{out.value}", status_code=303)
        return handle(request, render)

    def _list_place(ctx: Ctx, client_id: UUID, run_id: UUID) -> tuple[Any, ...]:
        return ("list", ctx.tenant_id, client_id, run_id)

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/import")
    async def import_upload(request: Request, client_id: UUID, business_id: UUID, run_id: UUID) -> HTMLResponse:
        """お客様リストのファイルを受ける（受け方は _receive_file の1か所）。"""
        return await _receive_file(
            request, f"/c/{client_id}/b/{business_id}/runs/{run_id}/import",
            check=lambda ctx: ctx.client(client_id, lambda tx: _run_in(tx, business_id, run_id), read_only=True),
            place=lambda ctx: _list_place(ctx, client_id, run_id))

    def _upload_or_missing(person: Person, place: tuple[Any, ...], key: str) -> uploads.Upload:
        u = uploads.get(key, person.login_id, place)
        if u is None:
            raise NotFound()
        return u

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}/import/{key}")
    def import_map(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, key: str) -> HTMLResponse:
        """列と欄の対応付け（はじめの案つき）と、ファイルの最初の数行。"""
        def extra(person: Person, ctx: Ctx) -> dict[str, Any]:
            u = _upload_or_missing(person, _list_place(ctx, client_id, run_id), key)
            sheet = _sheet(u)
            return {"sheet": sheet, "sample": sheet.rows[:3], "mapping": u.mapping or importing.guess(sheet)}
        return _import_page(request, "import_map.html", client_id, business_id, run_id, key=key, extra=extra)

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/import/{key}/preview")
    async def import_preview(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, key: str) -> HTMLResponse:
        """行ごとの合否（何も書かない）。指紋・AI の読み・確かめた読みは置き場に残し、登録はそれと同じときだけ受ける。"""
        form = await request.form()
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            u = _upload_or_missing(person, _list_place(ctx, client_id, run_id), key)
            sheet = _sheet(u)
            if any(k.startswith("map_") for k in form.keys()):   # 対応付けの画面から来たときだけ、対応付けを置き直す
                u.mapping = importing.mapping_from_form(form, sheet)
            confirmed = importing.confirmed_from_form(form) or u.preview.get("confirmed", [])
            def read(tx: Any) -> Any:
                _run_in(tx, business_id, run_id)
                return importer.preview(tx, run_id, sheet, u.mapping, confirmed_names=confirmed)
            try:
                plan = ctx.client(client_id, read, read_only=True)
            except actions.REFUSALS as e:
                return _import_page(request, "import_map.html", client_id, business_id, run_id, key=key, sheet=sheet,
                                    sample=sheet.rows[:3], mapping=u.mapping, error=str(e))
            u.preview = {"digest": plan.digest, "ai": plan.ai_readings, "confirmed": confirmed}
            return _import_page(request, "import_preview.html", client_id, business_id, run_id, key=key, plan=plan,
                                counts=plan.counts(), stopped=[r for r in plan.rows if r.outcome == "stopped"],
                                row_names=importing.row_names(sheet, u.mapping),
                                mapping=u.mapping, file_name=u.file_name)
        return handle(request, render)

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/import/{key}/register")
    async def import_register(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, key: str) -> HTMLResponse:
        """登録（core の importer.register。プレビューと同じでなければ core が断る＝何も登録しない）。"""
        form = dict(await request.form())
        rows_url = f"/c/{client_id}/b/{business_id}/runs/{run_id}"
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            u = _upload_or_missing(person, _list_place(ctx, client_id, run_id), key)
            if not u.preview:
                raise NotFound()
            def do(tx: Any) -> dict[str, Any]:
                _run_in(tx, business_id, run_id)
                return importer.register(tx, run_id, _sheet(u), u.mapping, expected_digest=u.preview["digest"],
                                         confirmed_names=u.preview["confirmed"], ai_readings=u.preview["ai"])
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, do), done="")
            if not out.ok:
                return _back(f"{rows_url}/import", out)
            uploads.drop(key)
            c = out.value["counts"]
            return _back(rows_url, actions.Outcome(True, f"取り込みました：登録 {c['registered']}行・止めた行 {c['stopped']}行・"
                                                         f"登録済み {c['already']}行・空の行 {c['empty']}行。"))
        return handle(request, render)

    # ── お客様の行（1行の中身と直し。docs/DASHBOARD.md 5-7） ──────────────────
    def _row_in(tx: Any, business_id: UUID, run_id: UUID, task_id: UUID) -> None:
        """住所の業務・実施回・行が、住所の依頼者の中でつながっているか（依頼者の取引で確かめる。違えば NotFound）。"""
        context.business(tx, business_id)
        if tx.one("SELECT 1 FROM salescall.tasks t JOIN salescall.runs r ON r.run_id = t.run_id"
                  " WHERE t.task_id = %s AND r.run_id = %s AND r.business_id = %s", (task_id, run_id, business_id)) is None:
            raise NotFound()

    def _row_page(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, task_id: UUID, *,
                  draft: rowfix.Draft | None = None, done: str | None = None, ok: int = 1) -> HTMLResponse:
        """1行の画面：行の中身（一覧と同じ1か所＝customer_rows.progress の行）・番号と連絡先・電話の記録・直す所。
        直しの下書きがあれば、次に聞く選び（rowfix.next_question）か、確かめ（何をどう直すか）を出す。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                _row_in(tx, business_id, run_id, task_id)
                # ⏳ 1行だけを読む形（customer_rows の1か所）を CL-2 へお願い（いまは実施回の全行を読んで1行を選ぶ）。
                row = next((r for r in customer_rows.progress(tx, run_id) if r["task_id"] == task_id), None)
                if row is None:
                    raise NotFound()
                calls = tx.all("SELECT attempt_id, state::text AS state, claimed_at, ended_at, trouble FROM salescall.attempts"
                               " WHERE task_id = %s AND state <> 'released' ORDER BY claimed_at DESC LIMIT 30", (task_id,))
                step: dict[str, Any] = {}
                if draft is not None:
                    try:
                        q = rowfix.next_question(tx, task_id, draft)
                        step = {"question": q} if q else {"confirm": rowfix.summary(tx, task_id, draft)}
                    except actions.REFUSALS as e:
                        step = {"error": str(e)}
                latest = results.latest(tx, task_id)
                return {"level": exclusions.level(tx, task_id),
                        "latest": latest, "can_correct": latest is not None and latest["kind"] in measure.SYSTEM_KINDS,
                        "pending": inbox.collect(tx, task=task_id),   # この行の承認待ち（受け箱と同じ1か所・同じ形で出す）
                        "client": tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,)),
                        "business": context.business(tx, business_id), "businesses": context.businesses(tx),
                        "run": tx.one("SELECT run_id, name FROM salescall.runs WHERE run_id = %s", (run_id,)),
                        "row": row, "calls": calls, **step}
            values = ctx.client(client_id, read, read_only=True)
            return page(request, "row.html", person, ctx, draft=draft, level_names=exclusions.LEVEL_NAMES, fields=[(f, rowfix.field_name(f)) for f in rowfix.FIELDS],
                        name_fields=list(rowfix.NAME_FIELDS), kind_names=customer_contacts.KIND_NAMES, done=done, ok=bool(ok), **values)
        return handle(request, render)

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}/rows/{task_id}/calls/{attempt_id}")
    def call_page(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, task_id: UUID, attempt_id: UUID) -> HTMLResponse:
        """1本の電話のやり取りの全文（お客様とこちらの言葉。起きた順＝core の conversation_io.utterances の1か所）。取り消せない決定などを
        承認する前に、その場のやり取りを確かめる（✅ 2026-10-30 管理者「人が録音やその場のやり取りを確認できる状態にする必要があります」）。
        ⏳ 録音を聞く仕組み（CL-1・CL-2）ができたら、同じ画面に録音を置く。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                _row_in(tx, business_id, run_id, task_id)
                call = tx.one("SELECT attempt_id, state::text AS state, claimed_at, ended_at, trouble FROM salescall.attempts"
                              " WHERE attempt_id = %s AND task_id = %s", (attempt_id, task_id))
                if call is None:
                    raise NotFound()
                return {"client": tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,)),
                        "business": context.business(tx, business_id), "businesses": context.businesses(tx),
                        "run": tx.one("SELECT run_id, name FROM salescall.runs WHERE run_id = %s", (run_id,)),
                        "row": tx.one("SELECT task_id, source_name, target_name, target_role FROM salescall.tasks WHERE task_id = %s",
                                      (task_id,)),
                        "call": call, "said": conversation_io.utterances(tx, attempt_id)}
            return page(request, "call.html", person, ctx, **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/rows/{task_id}/result")
    async def row_result(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, task_id: UUID) -> HTMLResponse:
        """契約者が、システムが判定した結果を直す（core の approval.correct_result だけ。直した版が確定し、行の次の動きも直しに従う。
        直しで電話をかけることになれば、「かけてよい」の確かめ待ちになる）。理由は必ず（直した版の記録に残る）。"""
        form = dict(await request.form())
        base = f"/c/{client_id}/b/{business_id}/runs/{run_id}/rows/{task_id}"
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def do(tx: Any) -> int:
                _row_in(tx, business_id, run_id, task_id)
                state = str(form.get("state") or "")
                if state not in words.BUSINESS_STATES or state == "unknown":
                    raise actions.Refused("直す結果を選んでください")
                try:
                    checked = int(str(form.get("version") or ""))
                except ValueError:
                    raise LookupError("version") from None
                try:
                    return approval_core.correct_result(tx, task_id, checked_version=checked,
                                                        business_state=state, wrong_items=[], complaint=False,
                                                        snapshot={"理由": str(form.get("reason") or "").strip()})
                except ValueError as e:   # 確定できない版・直しの形の誤り（core の理由の文）
                    raise actions.Refused(str(e)) from None
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, do),
                              done="結果を直しました（直した結果で確定し、次の予定も直しに従います）。", reason_field="reason")
            return _back(base, out)
        return handle(request, render)

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}/rows/{task_id}")
    def row_page(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, task_id: UUID,
                 done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _row_page(request, client_id, business_id, run_id, task_id, done=done, ok=ok)

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/rows/{task_id}/fix")
    async def row_fix(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, task_id: UUID) -> HTMLResponse:
        """直す。go が無ければ、次の選び・確かめを出すだけ（何も書かない）。go があれば、core の入口で直す（足りない選びは core と rowfix が断る）。"""
        form = dict(await request.form())
        draft = rowfix.from_form(form)
        if str(form.get("go") or "") != "1":
            return _row_page(request, client_id, business_id, run_id, task_id, draft=draft)
        base = f"/c/{client_id}/b/{business_id}/runs/{run_id}/rows/{task_id}"
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def do(tx: Any) -> Any:
                _row_in(tx, business_id, run_id, task_id)
                return rowfix.perform(tx, task_id, draft)
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, do),
                              done=f"{rowfix.field_name(draft.field)}を直しました。")
            return _back(base, out)
        return handle(request, render)

    return app
