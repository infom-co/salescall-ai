"""G-14 の試験集を作る：その場で確かめない答え（はい／いいえ・選択肢）を、6つの声で、電話の音質で読ませる（DESIGN.md 6-10・PHASE0 G-14）。

✅ 2026-10-12 ユーザー：「確かめない種類の項目（はい／いいえ等）は、聞き取れていれば渡します。チャットGPTでの実験では、聞き取り精度が
   悪かったので、聞き取り精度の良い仕組みを入れてください」⇒ 仕組みを1つ足すたびに、この試験集で正しさを測る（足しても上がらない仕組みは外す）。
- 質問：Q1「ご予約はされていますでしょうか？」（はい／いいえ）・Q2「お支払いはカードと振込のどちらがよろしいでしょうか？」（カード／振込）
- 答えの種類：はっきり・遠回し・わからない・曖昧（「大丈夫です」「結構です」など、受け入れにも断りにも取れる＝6-10 の (3)）
- 話し手：6つの声（会話のAI＝Sadachbia とは別の声）。お客の話し方（make_callee_samples.CUSTOMER_INSTRUCTION）。電話の音質（8kHz）
- 雑音：はっきりした答えだけ、2つの声で、白い雑音を混ぜた分も作る（声との強さの比 15dB）
- 作った声は、別の耳（transcribe35）で、台本どおりの言葉かを確かめる（make_callee_samples.sample_ok）。通らなければ3回まで作り直し、
  それでも通らない声は試験集から外す（台本と違う声で、AIの聞き取りを数えないため）
⚠️ 限界：作った声であって、人の声ではない。人の声での確かめは、本物の電話（R-1〜）のあとで行う。
"""
from __future__ import annotations

import asyncio
import json
import time

import numpy as np

from audio import duration_s, phone_quality
from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from listen import hear
from live_util import build_config, collect_turn, send_text
from make_callee_samples import CUSTOMER_INSTRUCTION, sample_ok

OUT = HERE / "results" / "callee" / "g14"
VOICES = ("Puck", "Kore", "Leda", "Charon", "Aoede", "Fenrir")
NOISE_VOICES = ("Kore", "Charon")
NOISE_SNR_DB = 15.0
# (中の名前, 質問, 正解, 種類, 言葉)。正解＝そのまま渡す条件（はい・いいえ・カード・振込）／unknown＝わからない／ambiguous＝曖昧（聞き返す）
ITEMS = [
    ("y1", "Q1", "はい", "はっきり", "はい、予約しています。"),
    ("y2", "Q1", "はい", "はっきり", "はい。"),
    ("y3", "Q1", "はい", "はっきり", "はい、先週しました。"),
    ("y4", "Q1", "はい", "遠回し", "ええ、してます。"),
    ("y5", "Q1", "はい", "遠回し", "予約済みです。"),
    ("y6", "Q1", "はい", "遠回し", "一応、予約はしてあります。"),
    ("n1", "Q1", "いいえ", "はっきり", "いいえ、まだです。"),
    ("n2", "Q1", "いいえ", "はっきり", "いえ、まだしていません。"),
    ("n3", "Q1", "いいえ", "遠回し", "してないです。"),
    ("n4", "Q1", "いいえ", "遠回し", "いや、これからなんです。"),
    ("n5", "Q1", "いいえ", "遠回し", "予約はしてないですね。"),
    ("u1", "Q1", "unknown", "わからない", "ちょっとわからないです。"),
    ("u2", "Q1", "unknown", "わからない", "家族がしたかもしれないです。"),
    ("a1", "Q1", "ambiguous", "曖昧", "大丈夫です。"),
    ("a2", "Q1", "ambiguous", "曖昧", "結構です。"),
    ("c1", "Q2", "カード", "はっきり", "カードでお願いします。"),
    ("c2", "Q2", "カード", "遠回し", "クレジットカードで。"),
    ("c3", "Q2", "カード", "遠回し", "カード払いにします。"),
    ("t1", "Q2", "振込", "はっきり", "振込でお願いします。"),
    ("t2", "Q2", "振込", "遠回し", "銀行振込で。"),
    ("t3", "Q2", "振込", "遠回し", "振り込みにします。"),
    ("u3", "Q2", "unknown", "わからない", "まだ決めてないです。"),
    ("a3", "Q2", "ambiguous", "曖昧", "どちらでも大丈夫です。"),
    ("a4", "Q1", "ambiguous", "曖昧", "いいです。"),
    # 続きの返事（こちらの確かめ「念のため…でよろしいでしょうか？」への返事。g14_listen --followup だけが使う。1言の試験には入れない）
    ("f1", "Q1", "follow", "続きの返事", "はい、そうです。"),
    ("f2", "Q1", "follow", "続きの返事", "いいえ、予約はしています。"),  # 「いえ、」は6つの声のうち5つで頭が落ちた・「いいえ」「ええ」に聞こえた
]


def add_noise(pcm16: bytes, snr_db: float, seed: int) -> bytes:
    """白い雑音を混ぜる（声の部分の強さとの比が snr_db）。声そのものの高さ・速さは変えない（試験の材料。会話のAIの声・録音には使わない）。"""
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
    voiced = x[np.abs(x) > 300]
    p = float(np.mean(voiced ** 2)) if voiced.size else float(np.mean(x ** 2))
    n = np.random.default_rng(seed).normal(0.0, np.sqrt(p / (10 ** (snr_db / 10))), x.size)
    return np.clip(x + n, -32768, 32767).astype(np.int16).tobytes()


def load_all() -> list[dict]:
    """声ごとの台帳（index_{声}.json）と、前の1つの台帳（index.json）を合わせる。"""
    made = []
    for f in sorted(OUT.glob("index_*.json")):
        made += json.loads(f.read_text(encoding="utf-8"))
    old = OUT / "index.json"
    if old.exists():
        seen = {(m["voice"], m["id"]) for m in made}
        made += [m for m in json.loads(old.read_text(encoding="utf-8")) if (m["voice"], m["id"]) not in seen]
    return made


async def make(args) -> list[dict]:
    """声ごとに作る（声ごとに台帳を分けるので、声ごとに別のキーで同時に流せる）。雑音を混ぜる分は --noise で、全部の声が済んでから。"""
    client = make_client(pick_key(args))
    OUT.mkdir(parents=True, exist_ok=True)
    for voice in args.voice or VOICES:
        index = OUT / f"index_{voice}.json"
        made = [m for m in load_all() if m["voice"] == voice and not m.get("noise")]
        have = {(m["voice"], m["id"]) for m in made if m.get("ok")}
        for iid, q, want, kind, text in ITEMS:
            if (voice, iid) in have:
                continue
            made = [m for m in made if (m["voice"], m["id"]) != (voice, iid)]
            row = {"voice": voice, "id": iid, "question": q, "want": want, "kind": kind, "text": text, "ok": False}
            for n in range(args.tries):
                try:
                    async with client.aio.live.connect(model=args.model,
                                                       config=build_config(voice=voice, system_instruction=CUSTOMER_INSTRUCTION)) as s:
                        t = time.monotonic()
                        await send_text(s, f"読み上げる言葉：{text}")
                        turn = await collect_turn(s, started_at=t, timeout_s=60)
                    pcm = phone_quality(bytes(turn.audio), turn.audio_rate or 24000, 16000)
                    check = sample_ok(text, await hear(client, pcm, 16000, "transcribe35"))
                    row.update({"heard": check["heard"], "tries": n + 1, "seconds": round(duration_s(pcm, 16000), 2)})
                    if check["ok"]:
                        row["ok"] = True
                        write_wav(OUT / f"{voice}_{iid}.wav", pcm, 16000)
                        row["wav"] = f"g14/{voice}_{iid}.wav"
                        break
                except Exception as exc:
                    row["error"] = describe_error(exc)
            made.append(row)
            say(f"{voice} {iid}「{text}」：{'合格' if row['ok'] else '不合格'} 聞こえた「{row.get('heard', '')}」")
            index.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_all()


def add_noise_rows() -> list[dict]:
    """雑音を混ぜた分（はっきりした答えだけ）。作った声に混ぜるので、作り直しはしない。全部の声の台帳を1つにまとめて index.json に書く。"""
    made = [m for m in load_all() if not m.get("noise")]
    for m in [m for m in made if m.get("ok") and m["kind"] == "はっきり" and m["voice"] in NOISE_VOICES]:
        from common import read_wav_pcm16_mono
        pcm, rate = read_wav_pcm16_mono(HERE / "results" / "callee" / m["wav"])
        noisy = add_noise(pcm, NOISE_SNR_DB, seed=hash((m["voice"], m["id"])) & 0xFFFF)
        name = f"{m['voice']}_{m['id']}_noise.wav"
        write_wav(OUT / name, noisy, rate)
        nid = f"{m['id']}_noise"
        made = [x for x in made if (x["voice"], x["id"]) != (m["voice"], nid)]
        made.append({**m, "id": nid, "noise": f"白い雑音 {NOISE_SNR_DB:.0f}dB", "wav": f"g14/{name}"})
    (OUT / "index.json").write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
    return made


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--tries", type=int, default=3)
    p.add_argument("--voice", action="append", choices=VOICES)
    p.add_argument("--noise", action="store_true", help="全部の声が済んだあとに：雑音を混ぜた分を作り、台帳を1つにまとめる")
    args = p.parse_args()
    made = add_noise_rows() if args.noise else asyncio.run(make(args))
    ok = [m for m in made if m.get("ok")]
    say(f"試験集：{len(ok)} 本（作れなかった {len(made) - len(ok)} 本）")
    save_result("G-14-SET", {"rows": made})


if __name__ == "__main__":
    main()
