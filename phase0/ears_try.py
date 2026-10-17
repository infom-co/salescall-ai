"""独立した別の耳（AmiVoice・Soniox・ReazonSpeech）を、1つの命令で確かめて比べる（DESIGN.md 6-10 の (5)・G-15 の続き）。
2026-10-17 ユーザー：「3つとも試す準備を進めて」「3つの耳の準備はコピペするだけの状態にしてください」。ユーザーが作業環境の設定に貼る文字は
docs/EARS_SETUP.md（ここで確かめる接続先の名前・キーの名前と、同じものだけを載せる＝test_offline.EarsTryTest が見張る）。

使い方：python ears_try.py            （4つの耳を順に。Gemini の書き起こしの耳は、同じ測り方で比べるための物差し）
       python ears_try.py --check    （足りないものの確かめだけ）
       python ears_try.py --ear soniox --ear amivoice
耳ごとの流れ（どこかで止まった耳は、そこで止めて理由を出し、次の耳へ進む）：
 (1) 足りないものの確かめ：キー（AmiVoice・Soniox）・接続先に届くか（作業環境の通信の決まり）・モデル（ReazonSpeech。無ければ取る）
 (2) 1本だけつないで確かめる（試験集の「はい、予約しています。」）：つなぎ方（命令の形）が正しく、文字が届くか
 (3) G-15：答えの声すべてを、この耳だけで聞く（g15_ear.py --ear。どの耳も同じ形＝通話ごとに1本つなぐ）
 (4) 見込み：G-14 の記録にある通話（会話AIの聞き取りの文字・AIが選んだ答え）に、この耳の文字を、通話の中と同じ比べ方で当てる
     （flowctl.heard_alike・待つのは Limits.ear_wait_s 秒まで）。AIが選んだ答えで、拾える誤り・要らない確かめを数える（問い合わせはしない）
 (5) 通話の中で使う形（g14_listen.py --ear）：(4) で拾える誤りが1つも無い耳は流さない（足しても数字が上がらない仕組みは外す）
 (6) 比べた表（results/EARS-TRY_*.json）
キーの値は画面にも記録にも出さない（キーは common.load_secret だけが読む。ここでは、あるかだけを見る）。
"""
from __future__ import annotations

import asyncio
import glob
import http.client
import json
import os
import socket
import subprocess
import sys
import urllib.parse
from collections import Counter

from common import HERE, RESULTS_DIR, base_parser, has_secret, load_keys, make_client, pick_key, read_wav_pcm16_mono, require_python, save_result, say
from ears import AMIVOICE_URL, EAR_KINDS, SECRETS, SONIOX_MODEL, SONIOX_URL, open_ear
from flowctl import Limits, heard_alike
from g15_ear import live_calls, ok
from make_g14_set import OUT, load_all
from stitch_gap import callee_speech
import get_reazon_model

TRY_ORDER = ("transcribe35", "amivoice", "soniox", "reazon")
# 耳ごとに、つなぐ接続先（作業環境の通信の決まりで許されている要がある）。ReazonSpeech はモデルを取るときだけ
SONIOX_API = "https://api.soniox.com/v1/models"   # 使えるモデルの名前の一覧（SONIOX_MODEL が古くなっていないかを見る）
HOSTS = {"amivoice": (urllib.parse.urlsplit(AMIVOICE_URL).hostname,),
         "soniox": (urllib.parse.urlsplit(SONIOX_URL).hostname, urllib.parse.urlsplit(SONIOX_API).hostname),
         "reazon": (urllib.parse.urlsplit(get_reazon_model.URL).hostname,)}
NAMES = {"transcribe35": "Gemini の書き起こしの耳（物差し）", "amivoice": "AmiVoice", "soniox": "Soniox", "reazon": "ReazonSpeech（PC の中）"}
SMOKE = ("Aoede", "y1")                 # 1本だけの確かめに使う声（「はい、予約しています。」）
JOBS = {"reazon": 2}                    # 通話の中の試験を、声ごとに同時に流す数（PC の中の耳は、同時に流すと文字にする時間が延びる）
VOICES = ("Aoede", "Charon", "Fenrir", "Kore", "Leda", "Puck")


def reachable(host: str, timeout: float = 10.0) -> str | None:
    """接続先に届くか（届けば None、届かなければ理由）。作業環境では通信の中継所を通るので、中継所に「この接続先へ」と頼んで確かめる。"""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    try:
        if proxy:
            u = urllib.parse.urlsplit(proxy)
            c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
            c.set_tunnel(host, 443)
            c.connect()
            c.close()
        else:
            socket.create_connection((host, 443), timeout).close()
        return None
    except OSError as e:
        if "403" in str(e):
            return f"接続先 {host} が、作業環境の通信の決まりで止められています"
        return f"接続先 {host} につながりません（{type(e).__name__}）"


def missing(kind: str, env_path: str | None) -> list[str]:
    """足りないもの（無ければ空）。ReazonSpeech のモデルは、ここでは確かめるだけ（取るのは prepare）。"""
    out = []
    if kind == "transcribe35":
        try:
            load_keys(env_path)
        except SystemExit as e:
            out.append(str(e))
        return out
    if kind in SECRETS and not has_secret(SECRETS[kind], env_path):
        out.append(f"キー {SECRETS[kind]} が入っていません（作業環境の設定の「環境変数」の欄。docs/EARS_SETUP.md の (2)）")
    if kind == "reazon":
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            out.append("sherpa-onnx が入っていません（pip install -r requirements.txt）")
        if get_reazon_model.have():
            return out
    out += [r + "（docs/EARS_SETUP.md の (1)）" for h in HOSTS.get(kind, ()) if (r := reachable(h))]
    return out


def prepare(kind: str) -> None:
    if kind == "reazon" and not get_reazon_model.have():
        get_reazon_model.fetch()


async def smoke(kind: str, client, env_path: str | None) -> dict:
    """1本だけつないで、文字が届くかを確かめる。"""
    m = next(m for m in load_all() if (m["voice"], m["id"]) == SMOKE and m.get("wav"))
    pcm, rate = read_wav_pcm16_mono(OUT.parent / m["wav"])
    async with open_ear(kind, client, env_path) as ear:
        loop = asyncio.get_running_loop()
        speech_end = loop.time() + len(pcm) / 2 / rate - callee_speech(m["wav"])[2]
        await ear.feed(pcm, rate)
        text = await ear.wait(speech_end + 8.0 - loop.time())
        return {"script": m["text"], "text": text, "ok": ok(m["text"], text or ""), "first_s": round(ear.first_at - speech_end, 3) if ear.first_at else None,
                "errors": list(ear.errors)}


def soniox_models(env_path: str | None) -> list[str] | None:
    """Soniox で使えるモデルの名前（ears.SONIOX_MODEL が新しい名前に替わっていないかを見るため。取れなければ None）。"""
    import urllib.request

    from common import load_secret

    req = urllib.request.Request(SONIOX_API, headers={"Authorization": f"Bearer {load_secret(SECRETS['soniox'], env_path)}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except Exception as e:
        say(f"Soniox のモデルの一覧が取れませんでした（{type(e).__name__}）")
        return None
    return sorted({m.get("id") or m.get("name") for m in (d.get("models") if isinstance(d, dict) else d) or [] if isinstance(m, dict)} - {None})


def run_step(cmd: list[str]) -> int:
    say("流します：", " ".join(cmd))
    return subprocess.call([sys.executable, *cmd], cwd=HERE)


def latest(pattern: str) -> list[str]:
    """結果のファイル（途中の保存・数え直しを除く）。"""
    return sorted(p for p in glob.glob(str(RESULTS_DIR / pattern)) if "_partial" not in p and "TALLY" not in p)


def predict(rows: list[dict], calls: list[dict], wait_s: float = Limits.ear_wait_s) -> dict:
    """G-15 のこの耳の文字を、G-14 の記録の通話（g15_ear.live_calls：会話AIの聞き取りの文字・AIの最初の報告・正解）に当て、通話の中と同じ決まりで数える。
    ・食い違えば確かめる（flowctl.Flow._op_record_answer の ears と同じ：別の耳の文字が wait_s 秒までに届いて、heard_alike でない）
    ・数えるのは、in_call と同じく AI が選んだ答えで：AIが正しく選んだ通話で確かめれば「要らない確かめ」、誤って選んだ通話で確かめれば「拾える誤り」。
      曖昧な答え（もともとプログラムが確かめる）と、正解の無い記録は数えない。
    ⛔ 2026-10-17 の1回目は、会話AIの文字が台本と違うかで数えた ⇒ 物差しの耳（Gemini）で「要らない確かめ 1.5%・拾える誤り 37組」と出たが、
       実際の通話の中では 8.5%・0回だった（文字が違っても、AIの選ぶ答えは正しいことが多い＝「清掃しました」でも「はい」）。"""
    ears = {r["wav"]: (r["ear"] if r.get("first_s") is not None and r["first_s"] <= wait_s else None) for r in rows if "ear" in r}
    c = Counter()
    for call in calls:
        if call["wav"] not in ears or call.get("want") in (None, "ambiguous", "follow"):
            continue
        ear, right = ears[call["wav"]], call.get("condition") == call["want"]
        c["通話"] += 1
        c["AIが正しく選んだ"] += right
        if ear is None:
            c["間に合わない"] += 1
        elif not heard_alike(call["heard"], ear):
            c["要らない確かめ" if right else "拾える誤り"] += 1
        elif not right:
            c["拾えない誤り"] += 1
    wrong = c["通話"] - c["AIが正しく選んだ"]
    return {**dict(c), "要らない確かめの割合": round(c["要らない確かめ"] / c["AIが正しく選んだ"], 3) if c["AIが正しく選んだ"] else None,
            "拾える誤りの割合": round(c["拾える誤り"] / wrong, 3) if wrong else None}


def in_call(rows: list[dict]) -> dict:
    """通話の中で使った試験（G-14 --ear）の数え。AIの最初の報告が正しかったか（はっきりした答え・わからないの答えだけ。曖昧な答えは
    もともとプログラムが確かめるので数えない）と、別の耳との食い違いで確かめたか（g14_listen.measures の ear_confirm）。"""
    c = Counter(r.get("verdict") for r in rows if "verdict" in r)
    clear = [r for r in rows if "verdict" in r and r.get("want") != "ambiguous"]
    late = [r for r in rows if "verdict" in r and not (r.get("ear_check") or {}).get("text")]
    return {"通話": sum(c.values()), "判定": dict(c),
            "要らない確かめ（AIは正しかった）": sum(1 for r in clear if r.get("ear_confirm") and r.get("condition") == r.get("want")),
            "拾えた（AIが誤っていた）": sum(1 for r in clear if r.get("ear_confirm") and r.get("condition") != r.get("want")),
            "拾えなかった誤り": c.get("⛔違う条件", 0), "別の耳の文字が届かなかった": len(late)}


def try_ear(kind: str, args, calls: list[dict]) -> dict:
    out = {"耳": NAMES[kind]}
    lack = missing(kind, args.env)
    if lack:
        return {**out, "止まった所": "(1) 足りないもの", "足りないもの": lack}
    if args.check:
        return {**out, "止まった所": "（確かめだけ）", "足りないもの": []}
    prepare(kind)
    if kind == "soniox":
        out["Sonioxのモデル"] = {"使う名前": SONIOX_MODEL, "使える名前": soniox_models(args.env)}
        say("Soniox のモデル：", json.dumps(out["Sonioxのモデル"], ensure_ascii=False))
    client = make_client(pick_key(args)) if kind == "transcribe35" else None
    s = asyncio.run(smoke(kind, client, args.env))
    out["1本だけの確かめ"] = s
    say(f"{NAMES[kind]}：1本だけの確かめ「{s['script']}」⇒ {s['text']!r}（{s['first_s']}秒） {s['errors']}")
    if not s["text"]:
        return {**out, "止まった所": "(2) 1本だけの確かめ（文字が届かない）"}
    found = latest(f"G-15-{kind}_*.json")
    if not (args.reuse and found):
        if run_step(["g15_ear.py", "--ear", kind, f"--tag=-{kind}"]) != 0:
            return {**out, "止まった所": "(3) G-15"}
        found = latest(f"G-15-{kind}_*.json")
    g15 = json.loads(open(found[-1], encoding="utf-8").read())
    out.update({"G-15": found[-1].rsplit("/", 1)[-1], "G-15の数え": g15["summary"], "見込み": predict(g15["rows"], calls)})
    say(f"{NAMES[kind]}：見込み", json.dumps(out["見込み"], ensure_ascii=False))
    if args.skip_in_call:
        return {**out, "止まった所": "（通話の中の試験は流さない指定）"}
    if not out["見込み"].get("拾える誤り"):
        return {**out, "止まった所": "(5) 拾える誤りが無いので、通話の中の試験は流さない"}
    tag = f"-ear-{kind}"
    if not (args.reuse and latest(f"G-14{tag}-*_*.json")):
        procs, jobs = [], args.jobs or JOBS.get(kind, len(VOICES))
        for v in VOICES:
            procs.append(subprocess.Popen([sys.executable, "g14_listen.py", "--ear", kind, "--voice", v, f"--tag={tag}-{v}"], cwd=HERE))
            while sum(p.poll() is None for p in procs) >= jobs:
                procs[[p.poll() is None for p in procs].index(True)].wait()
        for p in procs:
            p.wait()
    rows = [r for f in latest(f"G-14{tag}-*_*.json") for r in json.loads(open(f, encoding="utf-8").read())["rows"]]
    out["通話の中"] = in_call(rows)
    return {**out, "止まった所": "（最後まで）"}


def table(results: list[dict]) -> None:
    say("── 比べた表 ──")
    for r in results:
        line = [r["耳"], r["止まった所"]]
        if r.get("足りないもの"):
            line.append("足りないもの：" + "／".join(r["足りないもの"]))
        if "G-15の数え" in r:
            g = r["G-15の数え"]
            line.append(f"台本どおり {g['別の耳が台本どおり']}/{g['声']}・最初の文字まで（中央・遅いほうから1割・最大）{g['最初の文字が届くまで（中央・遅いほうから1割・最大）']}")
        if "見込み" in r:
            p = r["見込み"]
            line.append(f"見込み：拾える誤り {p.get('拾える誤り', 0)}・拾えない誤り {p.get('拾えない誤り', 0)}・要らない確かめ {p.get('要らない確かめ', 0)}（{p.get('要らない確かめの割合')}）・間に合わない {p.get('間に合わない', 0)}")
        if "通話の中" in r:
            line.append("通話の中：" + json.dumps(r["通話の中"], ensure_ascii=False))
        say(" ｜ ".join(line))


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--ear", action="append", choices=EAR_KINDS, help="試す耳（省略すると4つとも）")
    p.add_argument("--check", action="store_true", help="足りないものの確かめだけ")
    p.add_argument("--reuse", action="store_true", help="前に流した G-15・G-14 の結果があれば使う（流し直さない）")
    p.add_argument("--skip-in-call", action="store_true", help="通話の中の試験（G-14 --ear）を流さない")
    p.add_argument("--jobs", type=int, default=0, help="通話の中の試験を、声ごとに同時に流す数（0＝耳ごとの既定）")
    args = p.parse_args()
    calls = live_calls()
    results = []
    for kind in [k for k in TRY_ORDER if k in (args.ear or TRY_ORDER)]:
        try:
            results.append(try_ear(kind, args, calls))
        except SystemExit as e:  # 耳1つの失敗で、ほかの耳を止めない
            results.append({"耳": NAMES[kind], "止まった所": "途中", "足りないもの": [str(e)]})
        except Exception as e:
            results.append({"耳": NAMES[kind], "止まった所": f"途中（{type(e).__name__}）", "足りないもの": [str(e)[:300]]})
    table(results)
    if not args.check:
        save_result("EARS-TRY", {"results": results})


if __name__ == "__main__":
    main()
