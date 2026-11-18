"""使っている AI に新しい版が出ていないかを確かめる1か所（✅ 2026-11-15 日本時間12時43分22秒 ユーザー：「AIは全て、最新版が大きく進化するため、
テストをする場合は必ず、最新版が出ていないかを確認してください。本番に入っても10日に一回は最新版が出ていないかを確認して、最新版が出れば
置き換えてください。」＝帳面 0-2 の「AIの最新版」・Intent.md 9-5 の判定の顔ぶれ・12-1）。

- 使っている名前は、版まで決めて持つ所から読む（ここに書き写さない）：判定＝meaning_judge の組 RACE・その場の判定 STOP_ONLY・緊急の道
  EMERGENCY（名前と版は OTHER_JUDGES）／会話AI＝common.DEFAULT_MODEL／別の耳＝ears.SONIOX_MODEL・AMIVOICE_ENGINE／取込の文字のAI 2つ・
  そろわないときの考える判定＝import_ai.ASKERS・REFEREE（文字のAI とその場の判定の Gemini の表示名は g23_names.MODELS）。試しの候補は
  --also で足す（例 --also openrouter:nace-ai/drex-max）。
- 新しい版を探すのは、作り手の公開のモデルの一覧だけ（問い合わせではない＝料金はかからない）：OpenRouter（判断のモデルとふつうのモデル。
  鍵は要らない）・SubQ（鍵は要らない）・Anthropic（ANTHROPIC_JUDGE_KEY）・Gemini（common.KeyRing）・Soniox（SONIOX_API_KEY）。
  AmiVoice は一覧の口が無い＝作り手の頁で確かめる（結果にそう書く）。キーの値は画面にも結果にも出さない（common.load_secret）。
- 同じ系統か（family）：名前から作り手の前置き・「:free」・日付の8けたを外し、数の部分を # にした形が同じもの。版（version_key）は
  数の並び（日付の8けたは後ろ）で比べる。「最新」の別名（latest を含む名前）は版ではないので比べない（測ったあとに中身が黙って替わる）。
  系統の違う新しいもの（同じ作り手で、使っているものより後に出た・番号が大きい）は「別の系統」として並べる＝替えるかは測りで決める。
- 置き換えるときは、線（sure_min など）を新しい版で、決まった決め方で決め直す（線は版ごとの測りの値）。
- 新しい版で測って替えないと決めた版は DECLINED の1か所に覚え（版の名前 → 決めた日・理由・測りの記録の名前）、次の確かめでは「新しい版」
  にも「別の系統」にも並べない（測った結果がもうある＝並べると、同じ測りと同じ修正依頼を繰り返す。CL-2 の連絡帳 4-400 の (2)）。黙って
  捨てずに、行の「見送った版」に並べる。
"""
from __future__ import annotations

import datetime as dt
import json
import re

DATE = re.compile(r"(?<!\d)20\d{6}(?!\d)")
NUM = re.compile(r"\d+(?:\.\d+)*")
OPENROUTER_LISTS = ("https://openrouter.ai/api/v1/models", "https://openrouter.ai/api/v1/models?output_modalities=decisions")
SUBQ_LIST = "https://api.subq.ai/v1/models"
ANTHROPIC_LIST = "https://api.anthropic.com/v1/models?limit=1000"
SONIOX_LIST = "https://api.soniox.com/v1/models"
NO_LIST = "一覧の口が無い＝作り手の頁で確かめる"
# 新しい版で測って替えないと決めた版（作り手の一覧の id か、版まで決めた名前 slug → {"日", "理由", "記録"（測りの結果のファイルの名前）}）。
# 今は無い。決めたら、ここに1行足す（替えると決めた版は、版を持つ1か所を直すので、ここには書かない）
DECLINED: dict[str, dict] = {}


def bare(model_id: str) -> str:
    """作り手の前置き・「models/」・「:free」などの後ろ書き・「~」を外した名前（小文字）。"""
    return model_id.lower().split("/")[-1].split(":")[0].lstrip("~")


def family(model_id: str) -> str:
    """同じ系統かを決める名前の形：日付の8けたを外し、数を # にし、続く # を1つにまとめる（例 jev-1.14＝jev-#・d1-ultra-20261015＝d#-ultra・
    claude-sonnet-5-5＝claude-sonnet-#・gemini-3.8-live＝gemini-#-live）。"""
    s = re.sub(r"[-.]?" + DATE.pattern, "", bare(model_id))
    return re.sub(r"#(?:[-.]#)+", "#", NUM.sub("#", s))


def version_key(model_id: str) -> tuple:
    """版の比べ方：日付でない数の並び（3.8＝(3, 8)・5-5＝(5, 5)）と、日付の8けた（無ければ0）。"""
    s = bare(model_id)
    dates = [int(d) for d in DATE.findall(s)]
    nums = tuple(int(x) for part in NUM.findall(DATE.sub("", s)) for x in part.split("."))
    return nums, max(dates, default=0)


def is_alias(model_id: str) -> bool:
    return "latest" in bare(model_id)


def compare(current: str, listed: list[dict]) -> dict:
    """listed＝作り手の一覧（{"id", "slug"（版まで決めた名前・無ければ id）, "created"（秒・無ければ0）}）。返すのは、同じ系統の新しい版
    （newer）と、同じ作り手の別の系統で後から出たもの（others）と、測って替えないと決めた版（DECLINED にあるもの＝newer・others には入れない）。
    作り手は一覧を作り手ごとに渡す側が分ける。"""
    cur = next((m for m in listed if current in (m["id"], m["slug"])), None)
    if cur is None:  # 一覧に無い名前（もう無い・名前が変わった）＝何と比べるかが決まらないので、比べずにそうと返す
        return {"使っている版": current, "一覧にある": False, "新しい版": [], "別の系統で後から出たもの": [], "見送った版": []}
    cur_slug = cur["slug"]
    fam, key, words = family(cur_slug), version_key(cur_slug), set(re.findall(r"[a-z]+", family(cur_slug)))
    newer, others, declined = [], [], []
    for m in listed:
        if is_alias(m["id"]) or m["slug"] == cur_slug or m["id"] == current:
            continue
        if m["id"] in DECLINED or m["slug"] in DECLINED:   # 測って替えないと決めた版（DECLINED）
            declined.append(m["slug"])
            continue
        if family(m["slug"]) == fam:
            if version_key(m["slug"]) > key:
                newer.append(m["slug"])
        elif cur and cur["created"] and m["created"] > cur["created"]:
            others.append(m["slug"])
        elif not (cur and cur["created"]) and words <= set(re.findall(r"[a-z]+", family(m["slug"]))) and version_key(m["slug"])[0] > key[0]:
            others.append(m["slug"])
    return {"使っている版": cur_slug, "一覧にある": bool(cur), "新しい版": sorted(set(newer), key=version_key),
            "別の系統で後から出たもの": sorted(set(others)), "見送った版": sorted(set(declined))}


def in_use(also: tuple = ()) -> list[dict]:
    """使っている AI（役目・名前・作り手の一覧の種類・名前）を、版まで決めて持つ所から並べる。"""
    import common, ears, g23_names, import_ai, meaning_judge as mj
    rows = []
    for name in dict.fromkeys(mj.RACE + tuple(mj.STOP_ONLY) + (import_ai.REFEREE,)):
        j = mj.OTHER_JUDGES.get(name)
        if j:
            where = {"openrouter.ai": "openrouter", "api.anthropic.com": "anthropic", "api.subq.ai": "subq"}.get(j.get("host", ""), j.get("host", ""))
            rows.append({"役目": "判定", "名前": name, "一覧": where, "id": j["model"], "maker": j["maker"]})
        elif name in g23_names.MODELS:
            rows.append({"役目": "判定（その場）", "名前": name, "一覧": "gemini", "display": g23_names.MODELS[name]})
    rows.append({"役目": "判定（緊急の道）", "名前": "EMERGENCY", "一覧": "openrouter", "id": mj.EMERGENCY["model"],
                 "maker": mj.EMERGENCY["model"].split("/")[0]})
    rows.append({"役目": "会話AI", "名前": "DEFAULT_MODEL", "一覧": "gemini", "id": common.DEFAULT_MODEL})
    for key in import_ai.ASKERS:
        rows.append({"役目": "取込の文字のAI", "名前": key, "一覧": "gemini", "display": g23_names.MODELS[key]})
    rows.append({"役目": "別の耳", "名前": "SONIOX_MODEL", "一覧": "soniox", "id": ears.SONIOX_MODEL})
    rows.append({"役目": "別の耳", "名前": "AMIVOICE_ENGINE", "一覧": "amivoice", "id": ears.AMIVOICE_ENGINE})
    for a in also:
        where, _, model = a.partition(":")
        rows.append({"役目": "試しの候補", "名前": model, "一覧": where, "id": model, "maker": model.split("/")[0] if "/" in model else where})
    return rows


def _get(url: str, headers: dict | None = None) -> dict:
    import httpx
    r = httpx.get(url, headers=headers or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch(kind: str, args) -> list[dict]:
    """作り手の一覧を {"id", "slug", "created", "display"} の形で返す（問い合わせではない）。"""
    if kind == "openrouter":
        seen = {}
        for url in OPENROUTER_LISTS:
            for m in _get(url).get("data", []):
                seen[m["id"]] = {"id": m["id"], "slug": m.get("canonical_slug") or m["id"], "created": m.get("created") or 0, "display": m.get("name", "")}
        return list(seen.values())
    if kind == "subq":
        return [{"id": m["id"], "slug": m["id"], "created": m.get("created") or 0, "display": m.get("name", "")} for m in _get(SUBQ_LIST).get("data", [])]
    if kind == "anthropic":
        from common import load_secret
        key = load_secret("ANTHROPIC_JUDGE_KEY", args.env)
        data = _get(ANTHROPIC_LIST, {"x-api-key": key, "anthropic-version": "2023-06-01"}).get("data", [])
        return [{"id": m["id"], "slug": m["id"], "display": m.get("display_name", ""),
                 "created": int(dt.datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).timestamp()) if m.get("created_at") else 0} for m in data]
    if kind == "soniox":
        from common import load_secret
        key = load_secret("SONIOX_API_KEY", args.env)
        data = _get(SONIOX_LIST, {"authorization": f"Bearer {key}"})
        return [{"id": m["id"], "slug": m["id"], "created": 0, "display": m.get("name", "")} for m in data.get("models", data.get("data", []))]
    if kind == "gemini":
        from common import KeyRing
        ring = KeyRing(args)
        models = ring.call("models.list", lambda client: list(client.models.list()))[0]
        return [{"id": m.name.split("/")[-1], "slug": m.name.split("/")[-1], "created": 0, "display": (getattr(m, "display_name", "") or "").strip()}
                for m in models]
    raise ValueError(kind)


def check(rows: list[dict], lists: dict) -> list[dict]:
    """使っている AI ごとに、作り手の一覧と比べた結果を返す（一覧が読めなかった作り手は、その理由）。"""
    out = []
    for r in rows:
        got = lists.get(r["一覧"])
        if r["一覧"] == "amivoice":
            out.append({**r, "結果": NO_LIST})
            continue
        if isinstance(got, str) or got is None:
            out.append({**r, "結果": f"一覧を読めない（{got or '一覧の種類が無い'}）"})
            continue
        listed = [m for m in got if r.get("maker") is None or m["id"].split("/")[0] == r["maker"] or "/" not in m["id"]]
        current = r.get("id") or next((m["id"] for m in got if m["display"] == r["display"]), r.get("display", ""))
        out.append({**r, "id": current, **compare(current, listed)})
    return out


def main(argv=None) -> int:
    from common import base_parser, say
    p = base_parser(__doc__.split("\n")[0])  # --env・--key-name・--key-index・--dry-run は、ほかの道具と同じ1か所
    p.add_argument("--also", action="append", default=[], help="試しの候補（一覧の種類:名前。例 openrouter:nace-ai/drex-max）")
    p.add_argument("--out", default=None, help="結果を書く JSON（キーの値は入らない）")
    args = p.parse_args(argv)
    rows = in_use(tuple(args.also))
    lists = {}
    for kind in sorted({r["一覧"] for r in rows} - {"amivoice"}):
        try:
            lists[kind] = fetch(kind, args)
        except (Exception, SystemExit) as exc:  # 1つの作り手が読めなくても、ほかは確かめる
            lists[kind] = f"{type(exc).__name__}: {str(exc)[:120]}"
    result = check(rows, lists)
    for r in result:
        gone = "" if r.get("一覧にある", True) else "（作り手の一覧に無い）"
        say(f"{r['役目']}｜{r['名前']}｜{r.get('使っている版', r.get('id', r.get('display', '')))}{gone}｜"
            f"新しい版：{r.get('新しい版', r.get('結果'))}｜別の系統：{r.get('別の系統で後から出たもの', '')}"
            + (f"｜見送った版：{r['見送った版']}" if r.get("見送った版") else ""))
    newer = [r for r in result if r.get("新しい版")]
    say(f"新しい版が出ているもの：{len(newer)} 個" + ("（替える前に、新しい版で線を決め直す測りをする）" if newer else ""))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"at": dt.datetime.now(dt.timezone.utc).isoformat(), "rows": result}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
