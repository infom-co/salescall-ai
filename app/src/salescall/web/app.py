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
from datetime import date, datetime, timedelta, timezone
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
from salescall.core import (agents, ai_plans, calling_hours, help_texts, conversation_io, customer_contacts, customer_rows, destinations, dnc, dnc_import, exclusions,
                            gate, health, health_rules, import_files, importer, measure, phone, quality_stage, recordings, registry, results,
                            reward, settings, stops, troubles, usage)
from salescall.core import approval as approval_core
from salescall.core import google_login, once, passkeys, people
from salescall.core.access import Access
from salescall.output import destinations as result_destinations
from salescall.output import files as result_files
from salescall.output import notices, record_book, result_rows, words
from salescall.web import actions, context, counts, importing, inbox, links, names, rowfix, setting_forms, uploads, watch
from salescall.web import login as web_login
from salescall.web.context import AdminOnly, Ctx, Identify, NotFound, Person, acting

HERE = Path(__file__).resolve().parent
ACTING_COOKIE = "acting_tenant"   # 管理者が見ている契約者（署名つき）
GROUP_COOKIE = "client_group"     # タブに並べる依頼者のグループ（並べ方だけ。権限には使わない＝署名は要らない）
TENANT_BACKS = ("/t/costs",)   # 受け箱の操作のあと、依頼者の中の住所のほかに戻ってよい全体の画面（赤字の確認ボタン）


def _is_uuid(v: Any) -> bool:
    try:
        UUID(str(v))
        return True
    except ValueError:
        return False


def safe_back(back: str, client_id: UUID, fallback: str) -> str:
    """操作のあとに戻ってよい住所の決まり（1か所）：その依頼者の住所そのもの（/c/{依頼者}）とその下・決めた全体の画面だけ。
    ほかのサイト・ほかの依頼者の住所へは戻さない。"""
    own = f"/c/{client_id}"
    if "//" not in back and (back == own or back.startswith(own + "/") or back.startswith(own + "?") or back in TENANT_BACKS):
        return back
    return fallback
SYSTEM_PLACE: dict[str, Any] = {"client_id": None, "name": "全体（管理者だけ）"}   # 全体の範囲の承認待ちの「どこ」


def _time(t: datetime | None) -> str:
    """時刻の書き方（1か所＝calling_hours.label。日本時間。年は出さない）。"""
    return calling_hours.label(t) if t else ""


def _yen_input(raw: Any, what: str = "円") -> Decimal:
    """画面で入れた円を数にする（1か所。3桁ごとの「,」は外す。空は 0。数でない・負・無限は理由つきで断る）。"""
    text = str(raw or "").strip().replace(",", "").replace("，", "")
    if not text:
        return Decimal(0)
    try:
        yen = Decimal(text)
    except Exception:
        raise actions.Refused(f"{what}は数で入れてください（例：1200）") from None
    if not yen.is_finite() or yen < 0:
        raise actions.Refused(f"{what}は 0 以上の数で入れてください")
    return yen


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


def _explain(key: str) -> str:
    """i マークの説明（設定でない登録の項目。説明の正本は core の help_texts.explain の1か所＝画面に説明を写さない。設計書 6-7）。
    設定の説明は設定の画面が設定の定義から読むので、ここでは設定でない項目（help_texts.FIELDS）だけを受ける（取引の要らない側）。
    無い名前は LookupError（書き漏れを黙って空にしない）。"""
    if key not in help_texts.FIELDS:
        raise LookupError(f"i マークの説明がありません：{key}")
    return help_texts.explain(None, key)   # type: ignore[arg-type]  FIELDS の項目は取引を使わない


_phone = names.phone_text   # 番号の書き方（1か所＝web/names。そろえるのは core の phone.normalize）


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
    t.env.globals.update(words=words, time=_time, phone=_phone, yen=_yen, token_field=actions.TOKEN_FIELD, explain=_explain)
    return t


class Limiter:
    """回数の絞りの1か所：鍵（相手の住所・ログインした方）ごとに、決めた秒数の中で決めた回数まで。画面のサーバーの手元の記憶だけ
    （⏳ 画面のサーバーを2台以上にするときは、データベースの数え方へ移す＝CL-2 と相談）。覚えておく鍵の数も絞る。"""

    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.seen: dict[str, list[float]] = {}

    def hit(self, key: str) -> bool:
        """1回数える。もう決めた回数に達していれば True（断る）。"""
        now = time.monotonic()
        recent = [t for t in self.seen.get(key, []) if now - t < self.window]
        self.seen[key] = recent + [now]
        if len(self.seen) > 10_000:   # 古いものから捨てる
            for k in list(self.seen)[:5_000]:
                del self.seen[k]
        return len(recent) >= self.limit


def create_app(access: Access, identify: Identify, secret: bytes, *, client_ip_header: str | None = None,
               secure_cookies: bool = True, site: Any = None, google_client_id: str | None = None,
               google_keys: Callable[[], dict[str, Any]] | None = None) -> FastAPI:
    """site＝パスキーを結び付けるサイト（core の passkeys.Site。無ければパスキーの画面を出さない）・google_client_id＝Google のログインの
    クライアント ID（無ければ Google のボタンを出さない）・google_keys＝Google の署名の鍵を返す関数（無ければ core が取りに行く＝試験は渡す）。"""
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
            "person": person, "ctx": ctx, "clients": ctx.clients(), "groups": ctx.groups(), "no_group": context.NO_GROUP,
            # なりすましの札に、この画面の二度押しの札をつなぐ（札は core の once の1か所。画面を開くたびに1枚。web/actions の決まり）
            "token": actions.with_once(actions.form_token(secret, person.login_id),
                                       ctx.tenant(lambda tx: once.issue(tx, actions.ONCE_ACTION))), **values})

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

    @app.exception_handler(NotFound)
    @app.exception_handler(AdminOnly)
    async def not_found(request: Request, e: Exception) -> HTMLResponse:
        """「見つかりません」は、どこで起きても同じ返事（handle の外＝住所の読み方の段で起きても）。"""
        return problem(request, 404, "見つかりません", "住所を確かめてください。")

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
            ctx = acting(access, person, chosen_tenant(request, person),   # 契約者の名前を読む（データベースに届かないときも、下の返事にする）
                         request.cookies.get(GROUP_COOKIE) or None)
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

    # ── ログイン（web/login の1か所。ログインの表・パスワード・印は core の people＝CL-2 4-433）──────────
    login_limit = Limiter(10, 60.0)   # 同じ住所からのログインの試し（続けて誤ったときの止め方は、ログインごとに core が持つ）

    def _login_page(request: Request, error: str = "", status: int = 200) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {"error": error, "note": web_login.NOTE_2FA, "google_client_id": google_client_id},
                                          status_code=status)

    def _signed_in(session: Any) -> Response:
        """ログインが通った（1段目）。パスキーで確かめる方は、2段目の画面へ（印は、2段目が通るまで使えない＝core の people.by_session）。"""
        resp = RedirectResponse("/login/second" if session.needs_passkey else "/t", status_code=303)
        resp.set_cookie(web_login.COOKIE, session.token, httponly=True, samesite="strict", secure=secure_cookies)
        return resp

    def _pending(request: Request) -> str | None:
        """2段目を待つ印（ブラウザにある印が、もう使えるなら None）。"""
        token = request.cookies.get(web_login.COOKIE)
        if not token or people.by_session(access, token) is not None:
            return None
        return token

    @app.get("/login")
    def login(request: Request) -> Response:
        if identify(request) is not None:
            return RedirectResponse("/t", status_code=303)
        if _pending(request) is not None:   # パスワードは通って、2段目を待っている
            return RedirectResponse("/login/second", status_code=303)
        return _login_page(request)

    @app.post("/login")
    async def login_post(request: Request) -> Response:
        if login_limit.hit(_address(request)):
            return _login_page(request, "試す回数が多すぎます。1分ほど待ってから、もう一度入れてください。", 429)
        form = dict(await request.form())
        session = people.login(access, str(form.get("login_id") or "").strip(), str(form.get("password") or ""))
        if session is None:
            return _login_page(request, web_login.WRONG, 401)
        return _signed_in(session)

    # ── ログインの2段目（パスキー・予備の合言葉。core の passkeys の1か所＝CL-2 4-456）と Google でのログイン（google_login＝4-457）──
    second_limit = Limiter(10, 60.0)

    def _second_page(request: Request, error: str = "", status: int = 200) -> HTMLResponse:
        return templates.TemplateResponse(request, "login_second.html", {"error": error, "passkey": site is not None}, status_code=status)

    @app.get("/login/second")
    def login_second(request: Request) -> Response:
        if _pending(request) is None:
            return RedirectResponse("/t" if identify(request) is not None else "/login", status_code=303)
        return _second_page(request)

    @app.post("/login/second/options")
    def login_second_options(request: Request) -> Response:
        token = _pending(request)
        if token is None or site is None:
            return JSONResponse({"error": "ログインし直してください。"}, status_code=400)
        try:
            return Response(passkeys.login_options(access, token, site), media_type="application/json")
        except passkeys.PasskeyRefused as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/login/second/verify")
    async def login_second_verify(request: Request) -> Response:
        token = _pending(request)
        if token is None or site is None or second_limit.hit(_address(request)):
            return JSONResponse({"error": "確かめられませんでした。ログインし直してください。"}, status_code=400)
        ok = passkeys.verify_login(access, token, site, (await request.body()).decode("utf-8", "replace"))
        return JSONResponse({"next": "/t"} if ok else {"error": "パスキーで確かめられませんでした。もう一度お試しください。"}, status_code=200 if ok else 400)

    @app.post("/login/second/code")
    async def login_second_code(request: Request) -> Response:
        token = _pending(request)
        if token is None:
            return RedirectResponse("/login", status_code=303)
        if second_limit.hit(_address(request)):
            return _second_page(request, "試す回数が多すぎます。1分ほど待ってから、もう一度入れてください。", 429)
        form = dict(await request.form())
        if passkeys.use_recovery_code(access, token, str(form.get("code") or "")):
            return RedirectResponse("/t", status_code=303)
        return _second_page(request, "予備の合言葉が違うか、もう使ったものです。", 401)

    def _google_keys() -> dict[str, Any] | None:
        return google_keys() if google_keys is not None else None   # None＝core が取りに行く

    @app.post("/login/google")
    async def login_google(request: Request) -> Response:
        if not google_client_id or login_limit.hit(_address(request)):
            return JSONResponse({"error": "いまは Google でログインできません。ログインIDとパスワードでログインしてください。"}, status_code=400)
        form = dict(await request.form())
        try:
            session = google_login.login(access, str(form.get("credential") or ""), client_id=google_client_id, keys=_google_keys())
        except google_login.GoogleRefused as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if session is None:   # 結び付いていない（理由は記録に残す。ここでは、だれのアカウントかを教えない）
            return JSONResponse({"error": "この Google のアカウントは、まだ結び付いていません。ログインIDとパスワードでログインし、"
                                          "「ログインと安全」で結び付けてください。"}, status_code=400)
        resp = JSONResponse({"next": "/login/second" if session.needs_passkey else "/t"})
        resp.set_cookie(web_login.COOKIE, session.token, httponly=True, samesite="strict", secure=secure_cookies)
        return resp

    # ── ログインと安全（その方のパスキー・予備の合言葉・Google のアカウント。持ち主は、ログインした方ご本人）──────────
    def _me(request: Request) -> Any:
        """ログインした方と、画面の札（ヘッダー X-Form-Token か、フォームの欄）が合うか。合わなければ None。"""
        person = identify(request)
        if person is None or not actions.token_ok(secret, person.login_id, request.headers.get("x-form-token")):
            return None
        return person

    def _account(request: Request, done: str | None = None, ok: int = 1, codes: list[str] | None = None) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            return page(request, "account.html", person, ctx, active="account", done=done, ok=bool(ok), codes=codes,
                        sec=context.security(access, person.person_id), passkey=site is not None, google_client_id=google_client_id)
        return handle(request, render)

    @app.get("/t/account")
    def account(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _account(request, done, ok)

    @app.post("/t/account/passkey/options")
    def account_passkey_options(request: Request) -> Response:
        person = _me(request)
        if person is None or site is None:
            return JSONResponse({"error": "画面を開き直してから、もう一度押してください。"}, status_code=400)
        return Response(passkeys.register_options(access, person, site), media_type="application/json")

    @app.post("/t/account/passkey")
    async def account_passkey(request: Request) -> Response:
        person = _me(request)
        if person is None or site is None:
            return JSONResponse({"error": "画面を開き直してから、もう一度押してください。"}, status_code=400)
        body = await request.json()
        try:
            _, codes = passkeys.register(access, person, site, body.get("credential") or {}, str(body.get("label") or ""))
        except passkeys.PasskeyRefused as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"codes": codes or []})

    async def _account_form(request: Request) -> tuple[Any, dict[str, Any]]:
        form = dict(await request.form())
        person = identify(request)
        if person is None or not actions.token_ok(secret, person.login_id, form.get(actions.TOKEN_FIELD)):
            return None, form
        return person, form

    @app.post("/t/account/passkey/remove")
    async def account_passkey_remove(request: Request) -> Response:
        person, form = await _account_form(request)
        if person is None:
            return RedirectResponse("/t/account?ok=0&done=" + quote("画面を開き直してから、もう一度押してください。"), status_code=303)
        try:
            passkeys.remove(access, person, UUID(str(form.get("passkey_id") or "")))
        except (ValueError, passkeys.PasskeyRefused) as e:
            return RedirectResponse("/t/account?ok=0&done=" + quote(str(e) if isinstance(e, passkeys.PasskeyRefused) else "そのパスキーはありません"), status_code=303)
        return RedirectResponse("/t/account?done=" + quote("パスキーを外しました。"), status_code=303)

    @app.post("/t/account/codes")
    async def account_codes(request: Request) -> Response:
        person, _ = await _account_form(request)
        if person is None:
            return RedirectResponse("/t/account?ok=0&done=" + quote("画面を開き直してから、もう一度押してください。"), status_code=303)
        try:
            codes = passkeys.new_recovery_codes(access, person)
        except passkeys.PasskeyRefused as e:
            return RedirectResponse("/t/account?ok=0&done=" + quote(str(e)), status_code=303)
        return _account(request, "予備の合言葉を作り直しました。前の合言葉は、もう使えません。", 1, codes)   # 合言葉は、このときだけ見せる（残さない）

    @app.post("/t/account/google")
    async def account_google(request: Request) -> Response:
        person = _me(request)
        if person is None or not google_client_id:
            return JSONResponse({"error": "画面を開き直してから、もう一度押してください。"}, status_code=400)
        form = dict(await request.form())
        try:
            email = google_login.link(access, person, str(form.get("credential") or ""), client_id=google_client_id, keys=_google_keys())
        except google_login.GoogleRefused as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"next": "/t/account?done=" + quote(f"Google のアカウント（{email}）を結び付けました。")})

    @app.post("/t/account/google/unlink")
    async def account_google_unlink(request: Request) -> Response:
        person, _ = await _account_form(request)
        if person is None:
            return RedirectResponse("/t/account?ok=0&done=" + quote("画面を開き直してから、もう一度押してください。"), status_code=303)
        google_login.unlink(access, person)
        return RedirectResponse("/t/account?done=" + quote("Google のアカウントを外しました。"), status_code=303)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        form = dict(await request.form())
        person = identify(request)
        if person is not None and not actions.token_ok(secret, person.login_id, form.get(actions.TOKEN_FIELD)):
            return problem(request, 404, "見つかりません", "住所を確かめてください。")
        token = request.cookies.get(web_login.COOKIE)
        if token:
            people.logout(access, token)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(web_login.COOKIE)
        resp.delete_cookie(ACTING_COOKIE)
        return resp

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/t", status_code=303)

    # ── 全体（契約者） ─────────────────────────────────────────
    @app.get("/t")
    def home(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            waiting, stops_now = _waiting(ctx), counts.open_stops(ctx)
            stopped = counts.stopped_clients(stops_now)
            per_client = [{**c, **ctx.client(c["client_id"], counts.client_home, read_only=True), "stopped": str(c["client_id"]) in stopped,
                           "waiting": sum(1 for wc, _ in waiting if wc["client_id"] == c["client_id"])} for c in ctx.clients()]
            found = watch.collect(ctx)
            noticed = [(w, got) for _, w, got in found if w.attention and got]
            return page(request, "home.html", person, ctx, ready=ctx.tenant(counts.readiness), stops=stops_now, per_client=per_client, done=done, ok=bool(ok),
                        waiting_total=len(waiting), first=waiting[:3], watch_total=watch.attention_count(found), watch_first=noticed[:1],
                        system=ctx.tenant(counts.tenant_system))
        return handle(request, render)

    @app.get("/t/watch")
    def watch_page(request: Request) -> HTMLResponse:
        """見張り（計画書 12-1 の監視。項目は web/watch.WATCHES の1か所。画面は答える場所の事実を並べるだけ）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            found = watch.collect(ctx)
            return page(request, "watch.html", person, ctx, ready=ctx.tenant(counts.readiness), active="watch", found=found, days=watch.DAYS)
        return handle(request, render)

    @app.get("/t/badges")
    def badges(request: Request) -> HTMLResponse:
        """いつも見える帯の数（画面を開いたあとに読み、30秒ごとに読み直す）。数える部品はホームと同じ。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            stops_now, waiting = counts.open_stops(ctx), _waiting(ctx)
            # 依頼者のタブの印（同じ数え方の内訳。依頼者の範囲の停止・その依頼者の承認待ち。契約者・全体の範囲の停止は帯の印だけ＝全部のタブに付けない）
            stop_clients = sorted(counts.stopped_clients(stops_now))
            wait_clients = sorted({str(c["client_id"]) for c, _ in waiting if c.get("client_id")})
            return templates.TemplateResponse(request, "badges.html", {"n_stops": len(stops_now), "n_wait": len(waiting),
                                                                       "stop_clients": stop_clients, "wait_clients": wait_clients})
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
            return _back(safe_back(back, client_id, "/t/inbox"), out)
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
            client = None if client_id is None else ctx.client(client_id, lambda tx: context.client(tx, client_id), read_only=True)
            return page(request, "stop.html", person, ctx, x=x, base=base, done=done, ok=bool(ok), client=client,
                        active="client" if client else "stops")
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
        """緊急停止（人が押す。docs/DASHBOARD.md 5-3・7-4）。範囲：この契約者の全部／1つの依頼者／1つの業務／全体（管理者だけ＝データベースも守る）。
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
                if scope == "business":   # その業務だけ（業務の画面から。2026-11-20 の見直しで足した＝前は依頼者より細かく止められなかった）
                    cid, bid = actions.uuid_of(form, "client_id"), actions.uuid_of(form, "business_id")
                    def biz(tx: Any) -> str:
                        context.business(tx, bid)
                        return stop_in(tx, tenant_id=ctx.tenant_id, client_id=cid, business_id=bid)
                    return ctx.client(cid, biz)
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
                    "SELECT e.event_id, e.client_id, e.kind, e.summary, e.occurred_at, e.urgent, e.refs ->> 'task_id' AS task_id,"
                    "       e.refs ->> 'business_id' AS business_id,"
                    "       ARRAY(SELECT d.status FROM salescall.notification_deliveries d WHERE d.event_id = e.event_id) AS statuses"
                    " FROM salescall.notification_events e WHERE e.for_admin = %s"
                    " ORDER BY e.urgent DESC, e.occurred_at DESC, e.event_id DESC LIMIT 200", (only_admin,))
                # まだ送っていない知らせのうち、まとめて送るもの（送ってよい時間の外・毎朝の1通）は「いつ送るか」を添える（決まりは
                # output/notices.send_time の1か所＝送る処理と同じ。画面には、どの知らせも起きたときにすぐ出す＝2026-11-19 管理者）
                when = notices.schedule(tx, None if only_admin else tx.one("SELECT salescall.cur_tenant() AS t")["t"])
                out = []
                for r in rows:
                    state = notices.delivery_state(r["statuses"])
                    st = when(r) if state == "none" else None
                    out.append({**r, "state": state, "later": calling_hours.from_local(st.at) if st is not None and st.bundled else None})
                return out
            return run
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            rows = ctx.tenant(read(False), read_only=True)
            admin_rows = ctx.system(read(True)) if person.is_admin else []
            # 知らせ1件ずつの「どこの」と「見る」：依頼者の知らせは依頼者の名前、お客様の知らせはその行へ（住所は links の1か所。
            # 行は依頼者の取引でしか見えないので、依頼者ごとに1回で引く）
            names = {c["client_id"]: c for c in ctx.clients()}
            tasks: dict[Any, list[str]] = {}
            for r in rows:
                if r["client_id"] in names and r["task_id"]:
                    tasks.setdefault(r["client_id"], []).append(r["task_id"])
            found: dict[str, dict[str, str]] = {}
            for cid, ids in tasks.items():
                valid = [t for t in ids if _is_uuid(t)]
                found.update({str(k): v for k, v in ctx.client(cid, lambda tx: links.rows(tx, valid), read_only=True).items()})
            for r in rows:
                c = names.get(r["client_id"])
                r["where"] = c["name"] if c else ""
                row = found.get(str(r["task_id"])) if r["task_id"] else None
                biz = links.business_notice(c["client_id"], r["business_id"], r["kind"]) if c and _is_uuid(r["business_id"] or "") else ""
                r["link"] = row["url"] if row else (biz or (f"/c/{c['client_id']}" if c else ""))
                r["who"] = row["who"] if row else ""
            return page(request, "notices.html", person, ctx, rows=rows, admin_rows=admin_rows, state_words=notices.STATE_WORDS)
        return handle(request, render)

    # ── 設定（docs/DASHBOARD.md 5-9。Intent.md 5-3・5-4）：契約者・依頼者・業務の段で、同じ1つの画面 ──────────
    SCOPES = ("system", "tenant", "client", "business")
    # 段の重なりの説明。{chain} は、その設定を決められる段だけ（finest_scope まで）を細かい順に（契約者までしか決められない設定に
    # 「業務 ＞ 依頼者」を書かない）。決められる段が1つだけなら、重なりの説明は出さない
    SCOPE_SHORT = {"system": "初めの値", "tenant": "契約者", "client": "依頼者", "business": "業務"}
    MERGE_WORDS = {"override": "細かい段（{chain}）の値が勝ちます",
                   "min": "どの段の値より小さくなりません（いちばん小さい値を使います）",
                   "max": "どの段の値より大きくなりません（いちばん大きい値を使います）",
                   "intersect": "どの段の時間帯にも入る時間だけを使います", "union": "どの段の値もすべて合わせて使います"}

    def _merge_words(kind: str, finest: str) -> str:
        levels = SCOPES[:SCOPES.index(finest) + 1]
        if len(levels) < 2:
            return ""
        return MERGE_WORDS.get(kind, "").format(chain=" ＞ ".join(SCOPE_SHORT[x] for x in reversed(levels)))

    def _settings_rows(tx: Any, level: str, tenant_id: UUID, client_id: UUID | None, business_id: UUID | None) -> list[dict[str, Any]]:
        """その段で決められる設定（いちばん細かく決められる段＝finest_scope がその段より細かいか同じ）と、その段で結局いくつか
        （合成は core の settings.resolve_detail の1か所）。"""
        rows = []
        for d in tx.all("SELECT key, meaning, help, choice_words, value_type::text AS value_type, merge_kind::text AS merge_kind,"
                        "       finest_scope::text AS finest_scope, choices, bound_key FROM salescall.setting_definitions ORDER BY key"):
            if SCOPES.index(d["finest_scope"]) < SCOPES.index(level):
                continue
            # 会話AI・業務を取り込む AI の枠は、契約者が自分で API を契約するプランのときだけ出す（AI料金込みならシステムのキーなので、
            # 契約者の枠は無い。決めるのは core の ai_plans.tier の1か所＝None なら出さない。CL-2 4-438・4-446 の 55）
            if level != "system" and d["key"] in ai_plans.TIER_SETTINGS.values():
                ai = next(k for k, v in ai_plans.TIER_SETTINGS.items() if v == d["key"])
                try:
                    if ai_plans.tier(tx, ai, tenant_id=tenant_id) is None:
                        continue
                except settings.SettingMissing:
                    continue
            try:
                got = settings.resolve_detail(tx, d["key"], tenant_id=tenant_id, client_id=client_id, business_id=business_id)
            except settings.SettingMissing:
                got = None
            # 選び肢の言葉・契約者向けの説明は、設定の定義の1か所（choice_words・help＝CL-2 4-450。開発の根拠を含まない日本語だけ）
            shown = (None if got is None else words.choice_word(d["choice_words"], got["value"]) if d["value_type"] == "choice"
                     else words.yes_no(d["key"], got["value"]) if d["value_type"] == "boolean"
                     else setting_forms.show(d["value_type"], got["value"], words.SETTING_UNITS.get(d["key"], "")))
            rows.append({**d, "label": words.SETTING_LABELS.get(d["key"], words.NO_WORD), "meaning": words.plain(d["help"]),
                         "got": got, "shown": shown,
                         "own": got is not None and got["from"] == level, "group": d["key"].split(".")[0],
                         "merge": _merge_words(d["merge_kind"], d["finest_scope"]), "unit": words.SETTING_UNITS.get(d["key"], "")})
        return rows

    def _settings_page(request: Request, level: str, client_id: UUID | None, business_id: UUID | None,
                       done: str | None, ok: int) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                out: dict[str, Any] = {"rows": _settings_rows(tx, level, ctx.tenant_id, client_id, business_id)}
                if client_id is None:   # 契約者の設定：決めれば土台の抜けが直る設定（counts.readiness の keys の1か所）に「決めてください」
                    out["need"] = {k: r for r in counts.readiness(tx) for k in r.get("keys", [])}
                if client_id is not None:
                    out.update(client=context.client(tx, client_id),
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

    @app.get("/admin/tenants")
    def admin_tenants(request: Request) -> HTMLResponse:
        """管理者が、見る契約者を選ぶ画面（管理者だけ。全体の取引で、core の registry.tenants の1か所を読む＝CL-2 4-430 の 9。
        ✅ 2026-11-18 管理者「管理者は全てを見れて操作できる」）。選ぶ操作は /admin/acting の1か所。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            rows = ctx.system(registry.tenants)
            return page(request, "admin_tenants.html", person, ctx, tenants=rows, active="admin_tenants")
        return handle(request, render)

    # ── 名前の録音の審査（管理者だけ。✅ 2026-11-04 管理者「まとめて流して、私がレ点を入れたものをのみ登録、もしくは除外を入れたもののみ、
    #    登録されないという登録方法に」・「どの契約者・どの業務のものかは出るようにしとかないとダメ」。一覧＝core の recordings.name_reviews、
    #    聞く＝recording_audio.review_listen、決める＝recordings.review_names、チェック不要の契約者＝set_name_review_skip。docs/DASHBOARD.md 5-15）
    @app.get("/admin/name-reviews")
    def name_reviews(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        from salescall.core import recordings
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                items = recordings.name_reviews(tx)
                heard = {i.set_id for i in items if not inbox.unheard_of(tx, i.set_id, [i.part.part])}   # 聞いた記録は core の review_listen が残す
                groups: dict[UUID, list[Any]] = {}
                for i in items:
                    groups.setdefault(i.tenant_id, []).append(i)
                return {"groups": list(groups.values()), "heard": heard, "count": len(items)}
            return page(request, "admin_name_reviews.html", person, ctx, active="name_reviews", done=done, ok=bool(ok),
                        marks=recordings.REVIEW_MARKS, **ctx.system(read))
        return handle(request, render)

    @app.get("/admin/name-reviews/{set_id}/{part}.wav")
    def name_review_listen(request: Request, set_id: UUID, part: str) -> Response:
        """審査で名前の録音の1つの部分を聞く（音を読むのも聞いた記録を残すのも core の recording_audio.review_listen の1か所）。"""
        from salescall.core import recording_audio
        def render(person: Person, ctx: Ctx) -> Response:
            try:
                data = ctx.system(lambda tx: recording_audio.review_listen(tx, set_id, part))
            except recording_audio.AudioMissing:
                return problem(request, 404, "見つかりません", "その録音は、いま聞けません（もう決めた録音かもしれません）。")
            return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})
        return handle(request, render)

    @app.post("/admin/name-reviews")
    async def name_reviews_decide(request: Request) -> HTMLResponse:
        """一覧に出した録音を、1回の操作でまとめて決める（決めるのは core の recordings.review_names の1か所。1つの組は1回だけ＝データベース）。
        まだ聞いていない録音があれば決めない（聞かずに決める形を作らない＝受け箱の録音の承認と同じ決まり）。"""
        from salescall.core import recordings
        form = await request.form(max_fields=20_000)
        def ids(name: str) -> list[UUID]:
            try:
                return [UUID(str(v)) for v in form.getlist(name)]
            except ValueError:
                raise NotFound() from None
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            shown, marked, marks = ids("shown"), ids("marked"), str(form.get("marks") or "")
            def do(tx: Any) -> dict[UUID, UUID | None]:
                if marks not in recordings.REVIEW_MARKS:
                    raise actions.Refused("レ点の意味を選んでください")
                if not shown:
                    raise actions.Refused("決める録音がありません")
                parts = {i.set_id: i.part.part for i in recordings.name_reviews(tx)}   # 審査を待っている組の部分（決めたあとの組は core が断る）
                left = [x for x in shown if x in parts and inbox.unheard_of(tx, x, [parts[x]])]
                if left:
                    raise actions.Refused(f"録音をすべて聞いてから決めてください（まだ聞いていない録音が {len(left)} つあります）")
                return recordings.review_names(tx, shown, marked, marks=marks)
            out = actions.act(secret, person.login_id, dict(form), lambda: ctx.system(do), done="")
            if out.ok:
                admitted = sum(1 for v in out.value.values() if v is not None)
                out = actions.Outcome(True, f"{len(out.value)}件を決めました：システムの共通の録音に入れた {admitted}件・入れなかった "
                                            f"{len(out.value) - admitted}件（入れなかった録音も、その業務では使い続けます）。")
            return _back("/admin/name-reviews", out)
        return handle(request, render)

    @app.post("/admin/name-reviews/skip")
    async def name_reviews_skip(request: Request) -> HTMLResponse:
        """その契約者の名前の録音を、管理者のチェックなしでシステムの共通の録音に入れるか（core の recordings.set_name_review_skip。変えた記録は残る）。"""
        from salescall.core import recordings
        form = dict(await request.form())
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            try:
                tenant = UUID(str(form.get("tenant_id") or ""))
            except ValueError:
                raise NotFound() from None
            skip = str(form.get("skip") or "") == "1"
            out = actions.act(secret, person.login_id, form, lambda: ctx.system(lambda tx: recordings.set_name_review_skip(tx, tenant, skip)),
                              done=("この契約者の名前の録音は、これから管理者のチェックなしで入ります（決まった間隔の仕事が入れます）。" if skip
                                    else "この契約者の名前の録音は、これから管理者が聞いて決めます。"))
            return _back("/admin/name-reviews", out)
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
    # 回数の絞りは Limiter の1か所（どの入口も同じ数え方）：始める入口＝ログイン不要なので、同じ所（相手の住所）から1分に10回まで
    # （CL-2 4-423）。確かめのコードを試す＝ログインした方ごとに10分に20回まで（打ち試しでほかの PC のコードを当てさせない＝RFC 8628 の
    # 5.1・CL-2 4-425）。
    start_limit = Limiter(10, 60.0)
    code_limit = Limiter(20, 600.0)

    def _address(request: Request) -> str:
        return (request.headers.get(client_ip_header) if client_ip_header else None) or (request.client.host if request.client else "?")

    TOO_MANY_CODES = "コードを試す回数が多すぎます。10分ほど待ってから、PC の画面のコードで、もう一度開いてください。"

    async def _json(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @app.post("/pc-login/start")
    async def pc_login_start(request: Request) -> JSONResponse:
        """PC がログインを始める（ログイン不要）。確かめのコードと、契約者が開くページを返す。"""
        if start_limit.hit(_address(request)):
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
            if code and code_limit.hit(person.login_id):
                return page(request, "pc_login.html", person, ctx, code="", pending=None, done=TOO_MANY_CODES, ok=False, active="lines")
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
                if code_limit.hit(person.login_id):
                    raise actions.Refused(TOO_MANY_CODES)
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
            def read_month(tx: Any) -> dict[str, Any]:
                m = reward.month(tx)
                names = {b["business_id"]: b["name"] for b in context.businesses(tx)}
                # 今月かけたのに、いま効いている報酬の決まりの無い業務（決めるのは reward.month の1か所。報酬の決まりの画面へつなぐ）
                return {**m, "deficit": usage.deficit_notice(tx),
                        "without_terms": [(bid, names.get(bid, words.NO_WORD)) for bid in m["businesses_without_terms"]]}
            month = [{**c, **ctx.client(c["client_id"], read_month, read_only=True)} for c in ctx.clients()]
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
                if not str(form.get("yen") or "").strip():
                    raise actions.Refused("円を入れてください")
                yen = _yen_input(form.get("yen"))
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
                                 " ORDER BY retired_at NULLS FIRST, name, caller_e164, line_id"):   # 同じ名前の回線は番号の順（line_id の順＝ばらばらに見えた）
                    st = health.line_of(tx, ln["line_id"])
                    lines.append({**ln, "stage": st.stage, "wait": st.wait, "next_at": st.next_at,
                                  "resting": st.stage in health_rules.RESTS})
                return {"lines": lines, "methods": registry.line_methods(tx), "pcs": agents.pcs(tx)}
            return page(request, "lines.html", person, ctx, ready=ctx.tenant(counts.readiness), active="lines", done=done, ok=bool(ok),
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
                      f"       to_jsonb(d) ->> 'client_ref' AS client_ref, to_jsonb(d) ->> 'note' AS note FROM salescall.{table} d"
                      f" ORDER BY revoked_at NULLS FIRST, registered_at DESC LIMIT 500")

    def _dnc_page(request: Request, client_id: UUID | None, done: str | None, ok: int) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            if client_id is None:
                values = ctx.tenant(lambda tx: {"rows": _dnc_rows(tx, "contractor_dnc_entries")})
            else:
                values = ctx.client(client_id, lambda tx: {
                    "rows": _dnc_rows(tx, "dnc_entries"), "businesses": context.businesses(tx),
                    "client": context.client(tx, client_id)}, read_only=True)
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
                    "client": context.client(tx, client_id),
                    "businesses": context.businesses(tx)}, read_only=True))
            fields = [f for f in dnc_import.FIELDS if client_id is not None or f == "phone"]   # 契約者の共通の台帳は番号だけ（core と同じ）
            values.setdefault("stage", stage)   # 行ごとの結果を出せなかったときは、列の段に戻す（extra が決める）
            return page(request, "dnc_import.html", person, ctx, active="dnc" if client_id is None else "client", key=key,
                        fields=fields, encodings=[(e, import_files.ENCODING_NAMES.get(e, e)) for e in import_files.ENCODINGS], **values)
        return handle(request, render)

    def _dnc_base(client_id: UUID | None) -> str:
        return "/t/dnc/import" if client_id is None else f"/c/{client_id}/dnc/import"

    def _dnc_import_routes(prefix: str, scoped: bool) -> None:
        """依頼者の台帳（/c/{依頼者}/dnc/import）と契約者の共通の台帳（/t/dnc/import）の入口を、同じ部品で作る。"""
        def cid(request: Request) -> UUID | None:
            if not scoped:
                return None
            try:   # 住所の ID の形が違えば「見つかりません」（ほかの /c/{依頼者} の住所と同じ返事。500 にしない）
                return UUID(str(request.path_params.get("client_id")))
            except ValueError:
                raise NotFound() from None

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

    def _dnc_act(request: Request, form: dict[str, Any], client_id: UUID | None, do: Any, done: str) -> HTMLResponse:
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
            return _back(back, actions.act(secret, person.login_id, form, run, done=done, reason_field="reason"))
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
                        "取り消しました。この登録で終えたお客様は、決まった間隔の仕事が予定に戻します。")

    @app.get("/c/{client_id}/dnc")
    def client_dnc(request: Request, client_id: UUID, done: str | None = None, ok: int = 1) -> HTMLResponse:
        return _dnc_page(request, client_id, done, ok)

    @app.post("/c/{client_id}/dnc")
    async def client_dnc_add(request: Request, client_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        # 手で入れるときは、なぜ入れたかの文が必ず要る（台帳の note に残る。無ければデータベースが断る＝CL-2 4-430 の 25）。理由の欄は共通の発信禁止と同じ
        return _dnc_act(request, form, client_id, lambda tx: dnc.register(tx, str(form.get("phone") or ""), "manual",
                                                                          note=str(form.get("reason") or "").strip()),
                        "登録しました。この番号のお客様へは、この依頼者のどの一覧からもかけません。")

    @app.post("/c/{client_id}/dnc/revoke")
    async def client_dnc_revoke(request: Request, client_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        return _dnc_act(request, form, client_id, lambda tx: dnc.revoke(tx, _entry(form), str(form.get("reason") or "").strip()),
                        "取り消しました。この登録で終えたお客様は予定に戻り、依頼者へ渡す結果の「営業拒否」も外れます。")

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
                return registry.create_run(tx, business_id, actions.name_of(form), version, reason="ダッシュボードでお客様リストを作った")
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, do), done="お客様リストを作りました。")
            base = f"/c/{client_id}/b/{business_id}"
            return _back(f"{base}/runs/{out.value}/import" if out.ok else base, out)
        return handle(request, render)

    # ── 名前を直す・依頼者を無効にする（計画書 4章 A「登録・編集・無効化」。直すのは core の registry だけ。前と後の名前・誰が・いつは
    #    データベースが残す。同じ名前があれば core が断り、その文のまま出す。依頼者・業務・お客様リスト・回線で同じ1つの流れ） ──────────
    def _rename(request: Request, form: dict[str, Any], write: Callable[[Ctx, str], Any], back: str) -> HTMLResponse:
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            out = actions.act(secret, person.login_id, form, lambda: write(ctx, actions.name_of(form)),
                              done="名前を直しました（これまでの記録・設定・電話はそのままです）。")
            return _back(back, out)
        return handle(request, render)

    @app.post("/c/{client_id}/name")
    async def rename_client(request: Request, client_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        return _rename(request, form, lambda ctx, name: ctx.client(client_id, lambda tx: registry.rename_client(tx, client_id, name)),
                       f"/c/{client_id}")

    @app.post("/c/{client_id}/b/{business_id}/name")
    async def rename_business(request: Request, client_id: UUID, business_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        def write(ctx: Ctx, name: str) -> None:
            ctx.client(client_id, lambda tx: (context.business(tx, business_id), registry.rename_business(tx, business_id, name)))
        return _rename(request, form, write, f"/c/{client_id}/b/{business_id}")

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/name")
    async def rename_run(request: Request, client_id: UUID, business_id: UUID, run_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        def write(ctx: Ctx, name: str) -> None:
            ctx.client(client_id, lambda tx: (_run_in(tx, business_id, run_id), registry.rename_run(tx, run_id, name)))
        return _rename(request, form, write, f"/c/{client_id}/b/{business_id}/runs/{run_id}")

    @app.post("/t/lines/{line_id}/name")
    async def rename_line(request: Request, line_id: UUID) -> HTMLResponse:
        form = dict(await request.form())
        return _rename(request, form, lambda ctx, name: ctx.tenant(lambda tx: registry.rename_line(tx, line_id, name)), "/t/lines")

    @app.post("/c/{client_id}/active")
    async def client_active(request: Request, client_id: UUID) -> HTMLResponse:
        """依頼者を無効にする（取引を終えた など）・有効に戻す。無効にすると、まだ発信していない予約を取り消し、門が新しい電話を通さない
        （core の registry.set_client_active の1か所。理由は記録に残る）。"""
        form = dict(await request.form())
        active = form.get("active") == "1"
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            out = actions.act(secret, person.login_id, form,
                              lambda: ctx.client(client_id, lambda tx: registry.set_client_active(tx, active, reason=str(form.get("reason") or ""))),
                              done=("有効に戻しました。この依頼者に、また電話をかけます（ほかに止めているものがあれば、そちらが先です）。" if active
                                    else "無効にしました。この依頼者には、新しい電話をかけません（まだかけていない予約は取り消しました）。"),
                              reason_field="reason")
            return _back(f"/c/{client_id}/settings", out)
        return handle(request, render)

    # ── 依頼者と業務（タブは2段：依頼者 → 業務。✅ 2026-11-18 管理者） ──────────────
    def _client_or_business(request: Request, client_id: UUID, business_id: UUID | None, done: str | None = None,
                            ok: int = 1) -> HTMLResponse:
        """依頼者のタブ（business_id が None＝概要・全業務）と業務のタブは、同じこの1つの画面の部品で出す（絞り方が違うだけ）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                c = context.client(tx, client_id)
                b = None if business_id is None else context.business(tx, business_id)
                runs = tx.all("SELECT r.run_id, r.name, b.business_id, b.name AS business FROM salescall.runs r"
                              " JOIN salescall.businesses b ON b.business_id = r.business_id"
                              " WHERE %s::uuid IS NULL OR r.business_id = %s ORDER BY r.created_at DESC, r.run_id",
                              (business_id, business_id))
                versions = [] if business_id is None else counts.script_versions(tx, business_id)   # 中身を読めるか（ready）も1か所で
                needs, stages = [], []   # 業務のタブだけ：録音の要るもの（recordings.version_needs）と品質の段（quality_stage）。実施回のいまの版ごと
                verification = business_id is not None and approval_core.human_for_business(tx, business_id)
                for r in (runs if business_id is not None else []):
                    v = counts.run_version(tx, r["run_id"])
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
                        "deficit": usage.deficit_notice(tx),   # 赤字か＝core の1か所の答え（依頼者全体のもの。業務のタブにも出す）
                        "figures": counts.business_figures(tx, business_id),   # 業務の成果（数えるのは counts の1か所）
                        "required": [] if business_id is None else counts.required_answers(tx, business_id),
                        "steps": [] if business_id is None else counts.setup_steps(tx, client_id, business_id, needs)}
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
                return {"client": context.client(tx, client_id),
                        "businesses": context.businesses(tx), "found": found}
            return page(request, "search.html", person, ctx, q=term, **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    # ── 依頼者のグループ（docs/DASHBOARD.md 3章のタブ。並べ方だけ。core の registry のグループの部品＝CL-2 4-436）──────────
    @app.get("/t/group")
    def choose_group(request: Request, g: str = "", back: str = "/t") -> Response:
        """タブに並べるグループを選ぶ（ブラウザに覚えさせる。空＝すべて）。戻る先は、この画面の中の住所だけ。"""
        if identify(request) is None:
            return RedirectResponse("/login", status_code=303)
        resp = RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/t", status_code=303)
        if g:
            resp.set_cookie(GROUP_COOKIE, g[:64], httponly=True, samesite="strict", secure=secure_cookies)
        else:
            resp.delete_cookie(GROUP_COOKIE)
        return resp

    @app.get("/t/groups")
    def groups_page(request: Request, done: str | None = None, ok: int = 1) -> HTMLResponse:
        """グループを整える（作る・名前を変える・並べ替える・やめる・依頼者を入れる／外す）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            every = ctx.tenant(lambda tx: tx.all(f"SELECT {context.CLIENT_COLUMNS} FROM salescall.clients c ORDER BY c.name, c.client_id"), read_only=True)
            return page(request, "groups.html", person, ctx, all_clients=every, done=done, ok=bool(ok), active="groups")
        return handle(request, render)

    @app.post("/t/groups")
    async def groups_change(request: Request) -> HTMLResponse:
        """グループの操作の1か所（form の op で分ける。どれも core の registry のグループの部品だけを呼ぶ）。"""
        form = dict(await request.form())
        members = (await request.form()).getlist("client_id")
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def do(tx: Any) -> Any:
                op = str(form.get("op") or "")
                if op == "create":
                    return registry.create_group(tx, actions.name_of(form))
                gid = actions.uuid_of(form, "group_id")
                if op == "rename":
                    return registry.rename_group(tx, gid, actions.name_of(form))
                if op == "move":
                    pos = str(form.get("position") or "")
                    if not pos.isdigit() or int(pos) < 1:
                        raise actions.Refused("並びの位置は1以上の数です")
                    return registry.move_group(tx, gid, int(pos))
                if op == "remove":
                    return registry.remove_group(tx, gid)
                if op == "members":
                    now = next((g for g in registry.groups(tx) if g["group_id"] == gid), None)
                    if now is None:
                        raise LookupError("group")
                    want = {UUID(m) for m in members if _is_uuid(m)}
                    for cid in want - set(now["clients"]):
                        registry.set_group_member(tx, gid, cid, True)
                    for cid in set(now["clients"]) - want:
                        registry.set_group_member(tx, gid, cid, False)
                    return None
                raise NotFound()
            out = actions.act(secret, person.login_id, form, lambda: ctx.tenant(do), done="グループを変えました。")
            return _back("/t/groups", out)
        return handle(request, render)

    # ── 報酬の決まり（業務ごと。docs/DASHBOARD.md 5-11。✅ 2026-11-18 管理者「報酬の決まりは『業務』ごと」。core の reward.set_terms の1か所＝CL-2 4-437）
    REWARD_FIELDS = (("per_completed_yen", "成果1件"), ("per_dial_yen", "かけた1本"), ("per_talk_yen", "話せた1本"),
                     ("monthly_yen", "月額"), ("hourly_yen", "時給（1時間）"))

    @app.get("/c/{client_id}/b/{business_id}/reward")
    def reward_page(request: Request, client_id: UUID, business_id: UUID, done: str | None = None, ok: int = 1) -> HTMLResponse:
        """その業務の報酬の決まり（いま効いている決まりと、これまでの決まり。足すだけ＝前の決まりは残る）。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                b = context.business(tx, business_id)
                rows = tx.all("SELECT terms_id, valid_from, per_completed_yen, per_dial_yen, per_talk_yen, monthly_yen, hourly_yen,"
                              " hourly_gap_seconds, deficit_caution, note, set_by FROM salescall.reward_terms WHERE business_id = %s"
                              " ORDER BY valid_from DESC, terms_id DESC", (business_id,))
                now = tx.one("SELECT now() AS t")["t"]
                current = next((r for r in rows if r["valid_from"] <= now), None)
                return {"client": context.client(tx, client_id),
                        "business": b, "businesses": context.businesses(tx), "rows": rows, "current": current, "now": now}
            return page(request, "reward.html", person, ctx, fields=REWARD_FIELDS, cautions=reward.CAUTIONS, done=done, ok=bool(ok),
                        **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    @app.post("/c/{client_id}/b/{business_id}/reward")
    async def reward_set(request: Request, client_id: UUID, business_id: UUID) -> HTMLResponse:
        """報酬の決まりを足す（core の reward.set_terms だけ。理由は必ず＝決まりの記録に残る。いつからは、空なら今から）。"""
        form = dict(await request.form())
        back = f"/c/{client_id}/b/{business_id}/reward"
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def do(tx: Any) -> int:
                context.business(tx, business_id)
                yen = {k: _yen_input(form.get(k), label) for k, label in REWARD_FIELDS}
                gap = str(form.get("hourly_gap_seconds") or "").strip()
                if gap and not gap.isdigit():
                    raise actions.Refused("電話と電話の間の秒数は、数で入れてください")
                day = str(form.get("valid_from") or "").strip()
                if day:
                    try:
                        start = calling_hours.from_local(datetime.combine(date.fromisoformat(day), datetime.min.time()))
                    except ValueError:
                        raise actions.Refused("いつからは、日付で入れてください") from None
                else:
                    start = tx.one("SELECT now() AS t")["t"]
                try:
                    return reward.set_terms(tx, business_id, reward.Terms(deficit_caution=str(form.get("deficit_caution") or "") or None,
                                                                          hourly_gap_seconds=int(gap) if gap else None, **yen),
                                            valid_from=start, note=str(form.get("reason") or "").strip())
                except ValueError as e:
                    if isinstance(e, actions.Refused):
                        raise
                    raise actions.Refused(str(e)) from None
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, do), reason_field="reason",
                              done="報酬の決まりを足しました（前の決まりは残ります）。")
            return _back(back, out)
        return handle(request, render)

    # ── 誤判定率と抜き取り（計画書 16-3。docs/DASHBOARD.md 5-8b。割合は core の measure.rates、抜き取りは measure.draw の記録を読むだけ）
    METRIC_DAYS = (7, 30, 90)   # 指標の期間の選び（日。初めは30日）

    @app.get("/c/{client_id}/b/{business_id}/metrics")
    def metrics_page(request: Request, client_id: UUID, business_id: UUID, days: int = 30) -> HTMLResponse:
        """業務の指標（計画書 9-8：声の型ごとに記録・比較する）。数えるのは core の metrics.business の1か所（業務全体と、その期間に使った
        声の型ごと）。画面は並べるだけ（割合を計算し直さない）。"""
        from salescall.core import metrics
        span = days if days in METRIC_DAYS else 30
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                b = context.business(tx, business_id)
                until = datetime.now(timezone.utc)
                since = until - timedelta(days=span)
                voices = tx.all(
                    "SELECT v.code, v.voice_name, v.note FROM salescall.voice_types v WHERE v.code IN ("
                    " SELECT DISTINCT a.voice_type FROM salescall.attempts a JOIN salescall.tasks t ON t.task_id = a.task_id"
                    " JOIN salescall.runs r ON r.run_id = t.run_id WHERE r.business_id = %s AND a.claimed_at >= %s AND a.claimed_at < %s)"
                    " ORDER BY v.code", (business_id, since, until))
                columns = [("すべて", metrics.business(tx, business_id, since=since, until=until))]
                columns += [(f"{v['voice_name']}・{words.plain(v['note'])}", metrics.business(tx, business_id, since=since, until=until,
                                                                                          voice_type=v["code"])) for v in voices]
                return {"client": context.client(tx, client_id),
                        "business": b, "businesses": context.businesses(tx), "columns": columns}
            return page(request, "metrics.html", person, ctx, days=span, day_choices=METRIC_DAYS,
                        **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    @app.get("/c/{client_id}/b/{business_id}/samples")
    def samples_page(request: Request, client_id: UUID, business_id: UUID) -> HTMLResponse:
        """その業務の誤判定率（出どころごと＝検証モード・抜き取り。本番で気づいて直したものは数だけ）と、抜き取った電話の一覧。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                b = context.business(tx, business_id)
                rates = {s: measure.rates(tx, business_id, source=s) for s in measure.SOURCES}
                draws = tx.all("SELECT draw_id, week_start, pool_size, wanted, drawn, method, drawn_at FROM salescall.sample_draws"
                               " WHERE business_id = %s ORDER BY week_start DESC LIMIT 12", (business_id,))
                calls = tx.all(
                    "SELECT c.draw_id, a.attempt_id, a.task_id, a.claimed_at,"
                    "       EXISTS (SELECT 1 FROM salescall.judgement_checks k WHERE k.attempt_id = a.attempt_id AND k.source = 'sample') AS checked"
                    " FROM salescall.sample_draw_calls c JOIN salescall.attempts a ON a.attempt_id = c.attempt_id"
                    " WHERE c.draw_id = ANY(%s::uuid[]) ORDER BY a.claimed_at", ([str(d["draw_id"]) for d in draws],))
                rows = links.rows(tx, list({c["task_id"] for c in calls}))
                for c in calls:
                    r = rows.get(c["task_id"])
                    c["who"] = r["who"] if r else ""
                    c["url"] = f"{r['url']}/calls/{c['attempt_id']}" if r else ""
                for d in draws:
                    d["calls"] = [c for c in calls if c["draw_id"] == d["draw_id"]]
                return {"client": context.client(tx, client_id),
                        "business": b, "businesses": context.businesses(tx), "rates": rates, "draws": draws}
            return page(request, "samples.html", person, ctx, **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    # ── 結果の送り先（依頼者ごと。docs/DESIGN.md 4-5・docs/DASHBOARD.md 5-10。送り先を作る・直すのは output/destinations の1か所。
    #    どの業務の結果をどこへ渡すかを決めるのは core の destinations.for_business、選ぶのは destinations.choose の1か所＝CL-2 4-435）
    @app.get("/c/{client_id}/destinations")
    def destinations_page(request: Request, client_id: UUID, done: str | None = None, ok: int = 1) -> HTMLResponse:
        """依頼者の送り先の一覧・作る・直す、業務ごとに渡す送り先を選ぶ。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                every = result_destinations.of_client(tx)
                # 業務ごとの渡し方（決めるのは core の destinations.chosen の1か所）：None＝依頼者の送り先すべて／空＝ファイルだけ／
                # 送り先のそろい＝選んだ送り先だけ
                per = []
                for b in context.businesses(tx):
                    picked = destinations.chosen(tx, b["business_id"])
                    per.append(dict(b, mode="all" if picked is None else ("file" if not picked else "pick"),
                                    sends={d["destination_id"] for d in destinations.for_business(tx, b["business_id"])}))
                return {"client": context.client(tx, client_id),
                        "dests": every, "per_business": per}
            return page(request, "destinations.html", person, ctx, methods=result_destinations.METHODS, done=done, ok=bool(ok),
                        **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    @app.post("/c/{client_id}/destinations")
    async def destinations_change(request: Request, client_id: UUID) -> HTMLResponse:
        """送り先の操作の1か所（form の op で分ける）：create・update は output/destinations、business は core の destinations.choose。"""
        raw = await request.form()
        form = dict(raw)
        picked = {UUID(v) for v in raw.getlist("destination_id") if _is_uuid(v)}
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def do(tx: Any) -> Any:
                op = str(form.get("op") or "")
                auto = form.get("auto_writeback") == "1"
                if op == "create":
                    return result_destinations.create(tx, str(form.get("name") or ""), str(form.get("method") or ""), auto)
                if op == "update":
                    return result_destinations.update(tx, actions.uuid_of(form, "id"), str(form.get("name") or ""), auto)
                if op == "business":
                    bid = actions.uuid_of(form, "business_id")
                    context.business(tx, bid)
                    mode = str(form.get("mode") or "")
                    if mode == "all":                       # 依頼者の送り先すべて（あとで足した送り先にも渡る）
                        destinations.set_choosing(tx, bid, False)
                        return None
                    if mode not in ("pick", "file"):
                        raise actions.Refused("渡し方を選んでください")
                    every = {d["destination_id"] for d in result_destinations.of_client(tx)}
                    want = set() if mode == "file" else picked & every
                    if mode == "pick" and not want:
                        raise actions.Refused("「選んだ送り先だけ」のときは、送り先を1つ以上選んでください（どこへも書き戻さないときは「ファイルだけ」）")
                    destinations.set_choosing(tx, bid, True)   # 選ぶ業務にする（1つも選ばなければファイルだけ＝core の決まり）
                    for d in every:
                        destinations.choose(tx, bid, d, d in want)
                    return None
                raise NotFound()
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, do), done="送り先を変えました。")
            return _back(f"/c/{client_id}/destinations", out)
        return handle(request, render)

    @app.get("/c/{client_id}/b/{business_id}/versions/{version_id}")
    def version_page(request: Request, client_id: UUID, business_id: UUID, version_id: UUID) -> HTMLResponse:
        """業務設定の版の中身（読むだけ。読むのは core の business_settings.load の1か所＝置いたときと同じ確かめで読む）。"""
        from salescall.core import business_rules, business_settings
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                b = context.business(tx, business_id)
                v = tx.one("SELECT version_id, version_no, created_at FROM salescall.business_versions WHERE version_id = %s"
                           " AND business_id = %s", (version_id, business_id))
                if v is None:
                    raise NotFound()
                missing = False
                try:
                    s, problem = business_settings.load(tx, version_id), None
                except business_settings.SettingsInvalid as e:
                    s, problem = None, f"この版は、いまの確かめに通りません：{e}"
                except LookupError:   # 中身をまだ置いていない版（業務の取り込みで置く）。誤りではないので、赤い帯ではなく案内で出す
                    s, problem, missing = None, None, True
                try:   # 結果ごとのかけ直し（再発信の方針。読むのは core の business_rules.load の1か所＝通話のあとの次の動きと同じ）
                    rules = business_rules.load(tx, version_id)
                except (business_rules.RulesMissing, business_rules.RulesInvalid):
                    rules = None
                latest = business_settings.successor_tip(tx, version_id) == version_id   # 変える欄は、つながりのいちばん後の版にだけ出す
                return {"client": context.client(tx, client_id),
                        "business": b, "businesses": context.businesses(tx), "version": v, "s": s, "problem": problem, "missing": missing, "latest": latest,
                        "policies": [(k, rules.retry[k]) for k in business_rules.POLICY_KEYS] if rules else [],
                        "system_policies": [(k, rules.retry[k]) for k in business_rules.SYSTEM_POLICIES] if rules else [],
                        # 言ってはいけないこと・「わからない」「答えたくない」の扱い（計画書 6-2・6-3。読むのは同じ business_rules.load の1か所）
                        "forbidden": sorted(rules.forbidden.values(), key=lambda f: f.say_not) if rules else [],
                        "handling": sorted(rules.handling.items()) if rules else []}
            return page(request, "version.html", person, ctx, done=request.query_params.get("done"), ok=request.query_params.get("ok", "1") == "1",
                        units=business_rules.INTERVAL_UNITS, **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    @app.post("/c/{client_id}/b/{business_id}/versions/{version_id}/retry")
    async def version_retry(request: Request, client_id: UUID, business_id: UUID, version_id: UUID) -> HTMLResponse:
        """結果ごとのかけ直し（再発信の方針）を、まとめて変える（計画書 6-2「ダッシュボードでは一括登録できるように」）。方針だけを変えた新しい版を
        足すのは core の business_settings.change_retry の1か所（CL-2 4-440 の 49。確かめ・登録は取込と同じ道。前の版は残る）。実施回への切り替えは、
        決まった間隔の仕事（adopt_ready）が、録音などがそろってから行う。理由は必ず。"""
        from salescall.core import business_rules, business_settings
        form = dict(await request.form())
        base = f"/c/{client_id}/b/{business_id}/versions"
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def do(tx: Any) -> UUID:
                if tx.one("SELECT 1 FROM salescall.business_versions WHERE version_id = %s AND business_id = %s",
                          (version_id, business_id)) is None:
                    raise NotFound()
                policies = {}
                for key in business_rules.POLICY_KEYS:
                    action = str(form.get(f"{key}_action") or "")
                    if action == "end":
                        policies[key] = business_rules.Policy("end", None, None)
                    elif action == "call":
                        times, n, unit = (str(form.get(f"{key}_{x}") or "").strip() for x in ("times", "n", "unit"))
                        if not (times.isdigit() and n.isdigit() and int(times) >= 1 and int(n) >= 1) or unit not in business_rules.INTERVAL_UNITS:
                            raise actions.Refused(f"「{words.RETRY_POLICY_WORDS.get(key, key)}」の回数と間隔を、1以上の数で入れてください")
                        policies[key] = business_rules.Policy("call", int(times), (unit, int(n)))
                    else:
                        raise actions.Refused("かけ直すか、終えるかを選んでください")
                try:
                    return business_settings.change_retry(tx, version_id, policies, reason=str(form.get("reason") or "").strip())
                except business_settings.SettingsInvalid as e:
                    raise actions.Refused(str(e)) from None
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, do), reason_field="reason",
                              done="結果ごとのかけ直しを変えた、新しい版を作りました（前の版は残ります）。録音などがそろえば、お客様リストは自動でこの版に替わります。")
            return _back(f"{base}/{out.value}" if out.ok else f"{base}/{version_id}", out)
        return handle(request, render)

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}")
    def rows_page(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, done: str | None = None,
                  ok: int = 1) -> HTMLResponse:
        """お客様リスト（実施回ごと）。住所の依頼者の取引で、業務がその依頼者のもの・実施回がその業務のものかを確かめる。"""
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def read(tx: Any) -> dict[str, Any]:
                c = context.client(tx, client_id)
                b = context.business(tx, business_id)
                run = tx.one("SELECT run_id, name FROM salescall.runs WHERE run_id = %s AND business_id = %s", (run_id, business_id))
                if run is None:
                    raise NotFound()
                return {"client": c, "business": b, "businesses": context.businesses(tx), "run": run,
                        "rows": customer_rows.progress(tx, run_id), "levels": _levels(tx, run_id),
                        # 「さらにかける」の承認待ち（受け箱の種類 more_calls の1か所。行のボタンは、受け箱と同じ鍵・同じ受け口で送る）
                        "more": {UUID(i.key["task_id"]): i for i in inbox.KINDS["more_calls"].collect(tx, None, run_id)}}
            return page(request, "rows.html", person, ctx, done=done, ok=bool(ok), level_names=words.EXCLUSION_WORDS,
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
                    raise actions.Refused("印を付けるお客様を選んでください")
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
            msg = f"お客様 {out.value}件を「{words.exclusion_level(level)}」にしました（同じ印だったお客様は、そのままです）。" if out.ok else out.message
            target = f"{base}/rows/{ids[0]}" if back == "row" and len(ids) == 1 else base
            return _back(target, actions.Outcome(out.ok, msg))
        return handle(request, render)

    def _levels(tx: Any, run_id: UUID) -> dict[UUID, str]:
        """実施回の行ごとの「かけない」の印（どの印かはデータベースの1か所 task_exclusion_level を読むだけ）。"""
        return {r["task_id"]: r["level"] for r in tx.all(
            "SELECT task_id, salescall.task_exclusion_level(task_id) AS level FROM salescall.tasks WHERE run_id = %s", (run_id,))}

    # ── 結果の書き出し（docs/DESIGN.md 7-3。行は output/result_rows、ファイルは output/files の1か所） ──────────
    def _results_file(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, *, submit: bool,
                      form: dict[str, Any] | None = None, fmt: str = "csv") -> Response:
        """その実施回の結果の CSV。submit＝依頼者へ渡すファイルとして書き出す（提出用の控えに置く。同じ取引で）。依頼者へ出さない言葉が
        入っていれば、書き出さずに止める（output/files.check）。"""
        def render(person: Person, ctx: Ctx) -> Response:
            base = f"/c/{client_id}/b/{business_id}/runs/{run_id}"
            def work(tx: Any) -> tuple[str, bytes, int | None]:
                run = _run_in(tx, business_id, run_id)
                c = context.client(tx, client_id)
                got = result_rows.rows(tx, run_id)
                name = result_files.file_name(c["name"], run["name"], tx.today(), fmt)
                data = result_files.write(fmt, got, run["name"])
                return name, data, (result_files.submit(tx, got, name, data) if submit else None)
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
            return Response(data, media_type=result_files.FORMATS[fmt][2],
                            headers={"Content-Disposition": f"attachment; filename=\"results{result_files.FORMATS[fmt][1]}\"; filename*=UTF-8''{quote(name)}",
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
                return {"client": context.client(tx, client_id),
                        "business": context.business(tx, business_id), "businesses": context.businesses(tx), "run": run,
                        "rows": got, "leak": leak}
            return page(request, "results.html", person, ctx, columns=result_rows.COLUMNS, please_fill=result_rows.PLEASE_FILL, zip=zip,
                        deliverable=result_files.deliverable,
                        **ctx.client(client_id, read, read_only=True))
        return handle(request, render)

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}/results.{fmt}")
    def results_check(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, fmt: str) -> Response:
        """確かめるために書き出す（控えには置かない）。形は output/files.FORMATS（CSV・Excel）。"""
        if fmt not in result_files.FORMATS:
            return problem(request, 404, "見つかりません", "住所を確かめてください。")
        return _results_file(request, client_id, business_id, run_id, submit=False, fmt=fmt)

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/results.{fmt}")
    async def results_submit(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, fmt: str) -> Response:
        """依頼者へ渡すファイルとして書き出す（提出用の控えに置く）。"""
        if fmt not in result_files.FORMATS:
            return problem(request, 404, "見つかりません", "住所を確かめてください。")
        return _results_file(request, client_id, business_id, run_id, submit=True, form=dict(await request.form()), fmt=fmt)

    @app.get("/c/{client_id}/b/{business_id}/runs/{run_id}/records.xlsx")
    def records_book(request: Request, client_id: UUID, business_id: UUID, run_id: UUID) -> Response:
        """記録のすべての書き出し＝基本の形（設計書 7-3-5b。契約者の手元の控え＝依頼者へは渡さない・提出用の控えに置かない）。
        集めるのは output/record_book、Excel に書くのは output/files.book の1か所。読むだけの取引。"""
        def render(person: Person, ctx: Ctx) -> Response:
            def work(tx: Any) -> tuple[str, bytes]:
                run = _run_in(tx, business_id, run_id)
                c = context.client(tx, client_id)
                name = result_files.file_name(c["name"], run["name"] + "_記録のすべて", tx.today(), "xlsx")
                return name, result_files.book(record_book.sheets(tx, run_id))
            name, data = ctx.client(client_id, work, read_only=True)
            return Response(data, media_type=result_files.FORMATS["xlsx"][2],
                            headers={"Content-Disposition": f"attachment; filename=\"records.xlsx\"; filename*=UTF-8''{quote(name)}",
                                     "Cache-Control": "no-store"})
        return handle(request, render)

    @app.get("/c/{client_id}/recordings/{set_id}/{part}.wav")
    def recording_listen(request: Request, client_id: UUID, set_id: UUID, part: str) -> Response:
        """承認・審査の画面で録音の1つの部分を聞く（音を読むのも聞いた記録を残すのも core の recording_audio.listen の1か所＝CL-2 の 4-460）。
        見えない組・置き場に無い音は「見つかりません」（依頼者の取引の守りで、ほかの依頼者・契約者の録音は読めない）。"""
        from salescall.core import recording_audio
        def render(person: Person, ctx: Ctx) -> Response:
            try:
                data = ctx.client(client_id, lambda tx: recording_audio.listen(tx, set_id, part))
            except recording_audio.AudioMissing:
                return problem(request, 404, "見つかりません", "その録音は、いま聞けません。")
            return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})
        return handle(request, render)

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
                run = _run_in(tx, business_id, run_id)
                return {"client": context.client(tx, client_id),
                        "business": context.business(tx, business_id), "businesses": context.businesses(tx),
                        "run": run, "script_ready": counts.version_ready(tx, counts.run_version(tx, run_id))}
            return page(request, name, person, ctx, field_words=words.IMPORT_FIELD_WORDS,
                        encodings=[(e, import_files.ENCODING_NAMES.get(e, e)) for e in import_files.ENCODINGS],   # 名前の正本は import_files の組
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
                row = next(iter(customer_rows.progress(tx, run_id, task_id=task_id)), None)   # 1行だけ（同じ1つの問い合わせ＝CL-2 4-431 の 47）
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
                # こちらの不具合で休ませている・復旧を待っている行（戻せる段は core の troubles.RESTORABLE、待ちの言葉は gate.REASONS の1か所）
                stage = health.task_of(tx, task_id).stage
                rest = (gate.REASONS[gate.TASK_STAGE_REASONS[stage.name.lower()]].message
                        if stage in troubles.RESTORABLE else "")
                return {"level": exclusions.level(tx, task_id), "rest": rest,
                        "latest": latest, "can_correct": approval_core.correctable(tx, task_id) is not None,   # 直せるかは core の1か所（29 の (c)）
                        "answers": result_rows.answers_for_check(tx, task_id),   # 依頼者へ渡す書き方のままの答え（結果のファイルと同じ1か所）
                        "pending": inbox.collect(tx, task=task_id),   # この行の承認待ち（受け箱と同じ1か所・同じ形で出す）
                        "client": context.client(tx, client_id),
                        "business": context.business(tx, business_id), "businesses": context.businesses(tx),
                        "run": tx.one("SELECT run_id, name FROM salescall.runs WHERE run_id = %s", (run_id,)),
                        "row": row, "calls": calls, **step}
            values = ctx.client(client_id, read, read_only=True)
            return page(request, "row.html", person, ctx, draft=draft, level_names=words.EXCLUSION_WORDS, fields=[(f, rowfix.field_name(f)) for f in rowfix.FIELDS],
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
                return {"client": context.client(tx, client_id),
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

    @app.post("/c/{client_id}/b/{business_id}/runs/{run_id}/rows/{task_id}/restore")
    async def row_restore(request: Request, client_id: UUID, business_id: UUID, run_id: UUID, task_id: UUID) -> HTMLResponse:
        """こちらの不具合で休ませた行・復旧を待っている行を、管理者か契約者が手で戻す（core の troubles.restore だけ。計画書 10-3 の 2026-10-25
        夜 (2)「管理者・契約者が手で戻すこともできる」）。理由は必ず（記録に残る）。いま戻せる段でなければ断る（何も変えない）。"""
        form = dict(await request.form())
        base = f"/c/{client_id}/b/{business_id}/runs/{run_id}/rows/{task_id}"
        def render(person: Person, ctx: Ctx) -> HTMLResponse:
            def do(tx: Any) -> None:
                _row_in(tx, business_id, run_id, task_id)
                if not troubles.restore(tx, task_id, reason=str(form.get("reason") or "")):
                    raise actions.Refused("このお客様は、いま休んでいません（もう戻ったか、待っている理由が変わりました）。何も変えていません。")
            out = actions.act(secret, person.login_id, form, lambda: ctx.client(client_id, do),
                              done="戻しました。このお客様に、またおかけします（同じ不具合で続けて終われば、また休みます）。", reason_field="reason")
            return _back(base, out)
        return handle(request, render)

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
