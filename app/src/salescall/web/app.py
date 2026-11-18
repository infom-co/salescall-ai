"""ダッシュボードの入口（画面のルートの1か所）。設計は docs/DASHBOARD.md。

作り方：`create_app(access, identify, secret)`。
- access：core の読み書きの層（データベースへは、これだけを通す。取引の始め方は context.Ctx の1か所）。
- identify：ログインを確かめる関数（要求 → ログインした人か None）。⏳ 利用者の表は CL-2 に頼んでいる。無い間は、画面は動かさない
  （確かめずに通す既定の形を作らない）。試験は、架空の人を返す関数を渡す。
- secret：画面の札（なりすましの送信の確かめ）に使う秘密。値を git・画面・記録に出さない。

住所の決まり（docs/DASHBOARD.md 2-2）：全体は /t/...、依頼者の中は /c/{依頼者ID}/...、管理者の場所は /admin/...。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from salescall.core import access as access_layer
from salescall.core import calling_hours, customer_rows, phone, stops
from salescall.core.access import Access
from salescall.output import notices, words
from salescall.web import actions, counts, inbox
from salescall.web.context import AdminOnly, Ctx, Identify, NotFound, Person, acting

HERE = Path(__file__).resolve().parent
ACTING_COOKIE = "acting_tenant"   # 管理者が見ている契約者（署名つき）


def _time(t: datetime | None) -> str:
    """時刻の書き方（1か所＝calling_hours.label。日本時間。年は出さない）。"""
    return calling_hours.label(t) if t else ""


def _phone(e164: str | None) -> str:
    """番号の書き方（1か所＝phone.normalize。国内の番号は 0 から。そろえられなければ、そのまま）。"""
    if not e164:
        return ""
    n = phone.normalize(e164)
    return n.national or e164


def _back(path: str, out: actions.Outcome) -> RedirectResponse:
    """操作のあと、元の画面へ戻し、結果の1行を出す（送り直しで二度効かないよう、必ず GET の画面へ戻す）。"""
    return RedirectResponse(f"{path}?done={quote(out.message)}&ok={int(out.ok)}", status_code=303)


def _waiting(ctx: Ctx) -> list[tuple[dict[str, Any], inbox.Item]]:
    """全部の依頼者の承認待ちを、受け箱の並べ方（inbox.ordered の1か所）で並べる。"""
    pairs = [(c, i) for c in ctx.clients() for i in ctx.client(c["client_id"], inbox.collect)]
    rank = {id(i): n for n, i in enumerate(inbox.ordered([i for _, i in pairs]))}
    return sorted(pairs, key=lambda ci: rank[id(ci[1])])


def _templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(HERE / "templates"))   # 文字は必ず「文字として」出す（自動のエスケープ）
    t.env.globals.update(words=words, time=_time, phone=_phone, token_field=actions.TOKEN_FIELD)
    return t


def create_app(access: Access, identify: Identify, secret: bytes) -> FastAPI:
    if not secret:
        raise ValueError("画面の秘密（secret）が空です")
    app = FastAPI(title="SalesCall ダッシュボード", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = _templates()

    def page(request: Request, name: str, person: Person, ctx: Ctx, **values: Any) -> HTMLResponse:
        values.setdefault("client", None)
        return templates.TemplateResponse(request, name, {
            "person": person, "ctx": ctx, "clients": ctx.clients(), "token": actions.form_token(secret, person.login_id), **values})

    def chosen_tenant(request: Request, person: Person) -> UUID | None:
        """管理者が選んだ契約者（署名つきの札から読む。札が合わなければ選んでいないとみなす）。"""
        return actions.read_choice(secret, person.login_id, request.cookies.get(ACTING_COOKIE))

    def problem(request: Request, status: int, title: str, body: str) -> HTMLResponse:
        return templates.TemplateResponse(request, "problem.html", {"title": title, "body": body}, status_code=status)

    def handle(request: Request, render: Any) -> HTMLResponse:
        """ログインの確かめと、見つからない・届かないの返事の1か所。"""
        person = identify(request)
        if person is None:
            return RedirectResponse("/login", status_code=303)
        ctx = acting(access, person, chosen_tenant(request, person))
        try:
            return render(person, ctx)
        except (NotFound, AdminOnly):
            return problem(request, 404, "見つかりません", "住所を確かめてください。")
        except access_layer.TimedOut:
            return problem(request, 503, "読めませんでした", "データベースの返事が遅れています。少し待ってから開き直してください。")
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
            resp.set_cookie(ACTING_COOKIE, actions.sign_choice(secret, person.login_id, UUID(raw)), httponly=True, samesite="strict")
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
    def home(request: Request) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            waiting = _waiting(ctx)
            per_client = [{**c, **ctx.client(c["client_id"], counts.client_today, read_only=True),
                           "waiting": sum(1 for wc, _ in waiting if wc["client_id"] == c["client_id"])} for c in ctx.clients()]
            return page(request, "home.html", person, ctx, stops=counts.open_stops(ctx), per_client=per_client,
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
    def inbox_page(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            return page(request, "inbox.html", person, ctx, rows=_waiting(ctx), done=done, ok=bool(ok))
        return handle(request, render)

    @app.post("/c/{client_id}/inbox/{kind}/{action}")
    async def inbox_act(request: Request, client_id: UUID, kind: str, action: str) -> HTMLResponse:
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            k = inbox.KINDS.get(kind)
            if k is None or action not in k.perform:
                raise NotFound()
            do, done = k.perform[action]
            key = {f: str(form.get(f, "")) for f in ("attempt_id", "seq", "warning_id")}
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, lambda tx: do(tx, key)), done=done)
            return _back("/t/inbox", out)
        return handle(request, render)

    @app.get("/t/stops")
    def stops_page(request: Request) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            return page(request, "stops.html", person, ctx, stops=counts.open_stops(ctx))
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

    @app.post("/c/{client_id}/stops/{event_id}/resume")
    async def client_stop_resume(request: Request, client_id: UUID, event_id: int) -> HTMLResponse:
        return _stop_resume(request, event_id, client_id, dict(await request.form()))

    @app.get("/t/notices")
    def notices_page(request: Request) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            rows = ctx.tenant(lambda tx: tx.all(
                "SELECT e.event_id, e.client_id, e.kind, e.summary, e.occurred_at, e.urgent, e.for_admin,"
                "       ARRAY(SELECT d.status FROM salescall.notification_deliveries d WHERE d.event_id = e.event_id) AS statuses"
                " FROM salescall.notification_events e ORDER BY e.urgent DESC, e.occurred_at DESC, e.event_id DESC LIMIT 200"))
            rows = [{**r, "state": notices.delivery_state(r["statuses"])} for r in rows]
            return page(request, "notices.html", person, ctx, rows=rows, state_words=notices.STATE_WORDS)
        return handle(request, render)

    # ── 依頼者 ─────────────────────────────────────────────
    @app.get("/c/{client_id}")
    def client_home(request: Request, client_id: UUID) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                c = tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,))
                runs = tx.all("SELECT r.run_id, r.name, b.name AS business FROM salescall.runs r"
                              " JOIN salescall.businesses b ON b.business_id = r.business_id ORDER BY r.created_at DESC, r.run_id")
                return {"client": c, "today": counts.client_today(tx), "runs": runs, "waiting": inbox.collect(tx)}
            return page(request, "client.html", person, ctx, **ctx.client(client_id, read))
        return handle(request, render)

    @app.get("/c/{client_id}/runs/{run_id}")
    def rows_page(request: Request, client_id: UUID, run_id: UUID) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                c = tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,))
                run = tx.one("SELECT r.run_id, r.name, b.name AS business FROM salescall.runs r"
                             " JOIN salescall.businesses b ON b.business_id = r.business_id WHERE r.run_id = %s", (run_id,))
                if run is None:
                    raise NotFound()
                return {"client": c, "run": run, "rows": customer_rows.progress(tx, run_id)}
            return page(request, "rows.html", person, ctx, **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    return app
