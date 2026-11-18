"""ダッシュボードの入口（画面のルートの1か所）。設計は docs/DASHBOARD.md。

作り方：`create_app(access, identify, secret)`。
- access：core の読み書きの層（データベースへは、これだけを通す。取引の始め方は context.Ctx の1か所）。
- identify：ログインを確かめる関数（要求 → ログインした人か None）。⏳ 利用者の表は CL-2 に頼んでいる。無い間は、画面は動かさない
  （確かめずに通す既定の形を作らない）。試験は、架空の人を返す関数を渡す。
- secret：画面の札（なりすましの送信の確かめ）に使う秘密。値を git・画面・記録に出さない。

住所の決まり（docs/DASHBOARD.md 2-1・2-2）：全体は /t/...、依頼者の中は /c/{依頼者ID}/...、業務の中は /c/{依頼者ID}/b/{業務ID}/...、
管理者の場所は /admin/...。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from salescall.core import access as access_layer
from salescall.core import (agents, calling_hours, customer_contacts, customer_rows, dnc, exclusions, health, health_rules,
                            import_files, importer, phone, recordings, registry, settings, stops)
from salescall.core.access import Access
from salescall.output import notices, words
from salescall.web import actions, context, counts, importing, inbox, pc_login, rowfix, setting_forms, uploads
from salescall.web.context import AdminOnly, Ctx, Identify, NotFound, Person, acting

HERE = Path(__file__).resolve().parent
ACTING_COOKIE = "acting_tenant"   # 管理者が見ている契約者（署名つき）
SYSTEM_PLACE: dict[str, Any] = {"client_id": None, "name": "全体（管理者だけ）"}   # 全体の範囲の承認待ちの「どこ」


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


def _waiting(ctx: Ctx, client_id: UUID | None = None, business_id: UUID | None = None,
             kind: str | None = None) -> list[tuple[dict[str, Any], inbox.Item]]:
    """承認待ちを、受け箱の並べ方（inbox.ordered の1か所）で並べる。絞り方もここ1か所：依頼者（依頼者のタブ・受け箱の絞り）・
    業務（業務のタブ）・種類（受け箱の絞り）。どの画面の承認待ちの数も、これを数える（ホーム・帯・タブで数が食い違わない）。"""
    chosen = [c for c in ctx.clients() if client_id is None or c["client_id"] == client_id]
    pairs = [(c, i) for c in chosen for i in ctx.client(c["client_id"], inbox.collect)
             if (business_id is None or i.business_id == business_id) and (kind is None or i.kind == kind)]
    if ctx.person.is_admin and client_id is None and business_id is None:   # 全体の範囲の種類（管理者だけ）は、依頼者で絞らないときだけ
        pairs += [(SYSTEM_PLACE, i) for i in ctx.system(lambda tx: inbox.collect(tx, "system")) if kind is None or i.kind == kind]
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
            rows = every if pick_kind is None and pick_client is None else _waiting(ctx, pick_client, kind=pick_kind)
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
            do, done = k.perform[action]
            key = {f: str(form.get(f, "")) for f in inbox.KEY_FIELDS}
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, lambda tx: do(tx, key)), done=done)
            back = str(form.get("back") or "")   # 1行の画面などから決めたときは、その画面へ戻る（その依頼者の中の住所だけ）
            return _back(back if back.startswith(f"/c/{client_id}/") and "//" not in back else "/t/inbox", out)
        return handle(request, render)

    @app.post("/admin/inbox/{kind}/{action}")
    async def admin_inbox_act(request: Request, kind: str, action: str) -> HTMLResponse:
        """全体の範囲の承認待ち（管理者だけ。管理者でなければ「見つかりません」＝ctx.system が断る。データベースも守る）。"""
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            k = inbox.KINDS.get(kind)
            if k is None or k.where != "system" or action not in k.perform:
                raise NotFound()
            do, done = k.perform[action]
            key = {f: str(form.get(f, "")) for f in inbox.KEY_FIELDS}
            out = actions.act(secret, person.login_id, form, lambda: ctx.system(lambda tx: do(tx, key)), done=done)
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
                    cid = UUID(str(form.get("client_id")))
                    return ctx.client(cid, lambda tx: stop_in(tx, tenant_id=ctx.tenant_id, client_id=cid))
                if scope == "system":
                    return ctx.system(lambda tx: stop_in(tx))
                raise NotFound()
            out = actions.act(secret, person.login_id, form, do, done="", reason_field="reason")
            return _back("/t/stops", actions.Outcome(out.ok, out.value if out.ok else out.message))
        return handle(request, render)

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
            rows.append({**d, "got": got, "shown": None if got is None else setting_forms.show(d["value_type"], got["value"]),
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

    # ── PC のログイン（RFC 8628 の機器のログイン。web/pc_login.py。PC の側は agent/pc_login.py） ──────────────
    @app.post("/pc-login/start")
    async def pc_login_start(request: Request) -> JSONResponse:
        """PC がログインを始める（ログイン不要。確かめのコードと、契約者が開くページを返す）。"""
        try:
            body = await request.json()
        except ValueError:
            body = {}
        p = pc_login.start(str(body.get("pc_label") or ""), str(body.get("replaces") or "") or None)
        page_url = str(request.base_url).rstrip("/") + "/pc-login"
        return JSONResponse({"device_code": p.device_code, "user_code": p.user_code, "verification_uri": page_url,
                             "verification_uri_complete": f"{page_url}?code={p.user_code}", "interval": pc_login.INTERVAL,
                             "expires_in": pc_login.EXPIRES})

    @app.post("/pc-login/token")
    async def pc_login_token(request: Request) -> JSONResponse:
        """PC が通行証を受け取る。許可済みなら、core の agents.issue で1回だけ払い出す（前の通行証があれば止める）。"""
        try:
            body = await request.json()
        except ValueError:
            body = {}
        state, p = pc_login.poll(str(body.get("device_code") or ""))
        if p is None:
            return JSONResponse({"error": state}, status_code=400)
        try:
            replaces = UUID(str(body.get("replaces") or p.replaces or "")) if (body.get("replaces") or p.replaces) else None
        except ValueError:
            replaces = None
        # 払い出すのは「ログインを確かめるシステムの処理」（データベースの決まり）＝この入口。誰が許可したかは、処理の名前に残す
        issuer = access_layer.Actor(f"PC のログインの入口（許可：{p.actor.name}）", "system")
        got = agents.issue(access, issuer, p.tenant_id, label=p.pc_label, replaces=replaces)
        return JSONResponse({"tenant_id": str(got.tenant_id), "pc": str(got.pc_id), "token": got.token},
                            headers={"Cache-Control": "no-store"})

    @app.get("/pc-login")
    def pc_login_page(request: Request, code: str = "", done: str | None = None, ok: int = 1) -> HTMLResponse:
        """契約者が PC を許可するページ（ログインが要る）。コードが PC の画面と同じかを確かめてもらう。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            p = pc_login.by_user_code(code) if code else None
            return page(request, "pc_login.html", person, ctx, code=pc_login.normalize(code) if code else "", pending=p,
                        done=done, ok=bool(ok), active="lines")
        return handle(request, render)

    @app.post("/pc-login")
    async def pc_login_decide(request: Request) -> HTMLResponse:
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            approve = str(form.get("decision") or "") == "approve"
            def do() -> Any:
                p = pc_login.decide(str(form.get("code") or ""), approve=approve, tenant_id=ctx.tenant_id, actor=person.actor)
                if p is None:
                    raise actions.Refused("そのコードの PC は見つかりません（期限の10分を過ぎたか、もう決めたコードです。PC の画面の新しいコードで、もう一度開いてください）")
                return p
            out = actions.act(secret, person.login_id, form, do,
                              done=(f"この PC を「{ctx.tenant_name}」で使えるようにしました。PC の画面に戻ると、自動で動き出します。" if approve
                                    else "断りました。この PC はログインしていません。"))
            return _back("/pc-login", out)
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

    def _client_or_shared(ctx: Ctx, raw: Any) -> UUID | None:
        if not raw:
            return None
        ids = {str(c["client_id"]): c["client_id"] for c in ctx.clients()}
        if str(raw) not in ids:
            raise NotFound()
        return ids[str(raw)]

    @app.post("/t/lines")
    async def line_create(request: Request) -> HTMLResponse:
        form = dict(await request.form())
        return _line_act(request, form, lambda tx, ctx: registry.create_line(
            tx, actions.name_of(form), method=str(form.get("method") or ""), caller=str(form.get("caller") or ""),
            client_id=_client_or_shared(ctx, form.get("client_id"))), "回線を作りました。")

    @app.post("/t/lines/{line_id}/assign")
    async def line_assign(request: Request, line_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        return _line_act(request, form, lambda tx, ctx: registry.assign_line(tx, line_id, _client_or_shared(ctx, form.get("client_id"))),
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
                needs = []   # 録音の要るもの（業務のタブだけ。実施回のいまの版ごと。判断は core の recordings.version_needs の1か所）
                for r in (runs if business_id is not None else []):
                    v = tx.one("SELECT salescall.run_current_version(%s) AS v", (r["run_id"],))["v"]
                    if v is not None:
                        got = recordings.version_needs(tx, v)
                        if not got.listed or got.parts:
                            needs.append({"run": r["name"], "listed": got.listed, "parts": got.parts})
                return {"client": c, "business": b, "businesses": context.businesses(tx),
                        "today": counts.client_today(tx, business_id), "runs": runs, "versions": versions, "needs": needs}
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
        form = await request.form()
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
                values.update(extra(person))
            def read(tx: Any) -> dict[str, Any]:
                return {"client": tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,)),
                        "business": context.business(tx, business_id), "businesses": context.businesses(tx),
                        "run": _run_in(tx, business_id, run_id)}
            return page(request, name, person, ctx, field_words=words.IMPORT_FIELD_WORDS, encodings=import_files.ENCODINGS,
                        **values, **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    def _sheet(u: uploads.Upload) -> import_files.Sheet:
        return import_files.read(u.file_name, u.data, sheet_name=u.sheet_name, encoding=u.encoding)

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}/import")
    def import_start(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, done: str | None = None,
                     ok: int = 1) -> HTMLResponse:
        return _import_page(request, "import.html", client_id, business_id, run_id, done=done, ok=bool(ok))

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/import")
    async def import_upload(request: Request, client_id: UUID, business_id: UUID, run_id: UUID) -> HTMLResponse:
        """ファイルを受け、読めるかを確かめて一時置き場に置く（書くのは手元の記憶だけ）。"""
        form = await request.form()
        f = form.get("file")
        data = await f.read() if hasattr(f, "read") else b""
        file_name = getattr(f, "filename", "") or ""
        base = f"/c/{client_id}/b/{business_id}/runs/{run_id}/import"
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            ctx.client(client_id, lambda tx: _run_in(tx, business_id, run_id), read_only=True)
            def do() -> str:
                if not data:
                    raise actions.Refused("ファイルを選んでください")
                u = uploads.Upload(person.login_id, client_id, run_id, file_name, data,
                                   sheet_name=str(form.get("sheet_name") or "").strip() or None,
                                   encoding=str(form.get("encoding") or "") or None)
                _sheet(u)   # 読めるか（読めなければ理由の文つきで断る）
                try:
                    return uploads.put(u)
                except uploads.TooLarge as e:
                    raise actions.Refused(str(e)) from None
            out = actions.act(secret, person.login_id, dict(form), do, done="")
            if not out.ok:
                return _back(base, out)
            return RedirectResponse(f"{base}/{out.value}", status_code=303)
        return handle(request, render)

    def _upload_or_missing(request: Request, person: Person, client_id: UUID, run_id: UUID, key: str) -> uploads.Upload:
        u = uploads.get(key, person.login_id, client_id, run_id)
        if u is None:
            raise NotFound()
        return u

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}/import/{key}")
    def import_map(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, key: str) -> HTMLResponse:
        """列と欄の対応付け（はじめの案つき）と、ファイルの最初の数行。"""
        def extra(person: Person) -> dict[str, Any]:
            u = _upload_or_missing(request, person, client_id, run_id, key)
            sheet = _sheet(u)
            return {"sheet": sheet, "sample": sheet.rows[:3], "mapping": u.mapping or importing.guess(sheet)}
        return _import_page(request, "import_map.html", client_id, business_id, run_id, key=key, extra=extra)

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/import/{key}/preview")
    async def import_preview(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, key: str) -> HTMLResponse:
        """行ごとの合否（何も書かない）。指紋・AI の読み・確かめた読みは置き場に残し、登録はそれと同じときだけ受ける。"""
        form = await request.form()
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            u = _upload_or_missing(request, person, client_id, run_id, key)
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
            u = _upload_or_missing(request, person, client_id, run_id, key)
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
                row = next(r for r in customer_rows.progress(tx, run_id) if r["task_id"] == task_id)
                calls = tx.all("SELECT state::text AS state, claimed_at, ended_at, trouble FROM salescall.attempts"
                               " WHERE task_id = %s AND state <> 'released' ORDER BY claimed_at DESC LIMIT 30", (task_id,))
                step: dict[str, Any] = {}
                if draft is not None:
                    try:
                        q = rowfix.next_question(tx, task_id, draft)
                        step = {"question": q} if q else {"confirm": rowfix.summary(tx, task_id, draft)}
                    except actions.REFUSALS as e:
                        step = {"error": str(e)}
                return {"level": exclusions.level(tx, task_id),
                        "correction": tx.one("SELECT salescall.task_correction_unconfirmed(%s) AS v", (task_id,))["v"],
                        "client": tx.one("SELECT client_id, name FROM salescall.clients WHERE client_id = %s", (client_id,)),
                        "business": context.business(tx, business_id), "businesses": context.businesses(tx),
                        "run": tx.one("SELECT run_id, name FROM salescall.runs WHERE run_id = %s", (run_id,)),
                        "row": row, "calls": calls, **step}
            values = ctx.client(client_id, read, read_only=True)
            return page(request, "row.html", person, ctx, draft=draft, level_names=exclusions.LEVEL_NAMES, fields=[(f, rowfix.field_name(f)) for f in rowfix.FIELDS],
                        name_fields=list(rowfix.NAME_FIELDS), kind_names=customer_contacts.KIND_NAMES, done=done, ok=bool(ok), **values)
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
