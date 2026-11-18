"""自分の言葉の答えの声（会社の所在地を聞かれる場面）を、独立した耳（Soniox・AmiVoice）でも聞く。声が崩れているのか、試験の耳の聞き違いかを
分ける（G-57 tc36〜tc38：AIの文字は「弊社の本社は東京都港区にございます。」なのに、試験の耳には「東京マーク」「墨田区」「丸の内」「都外」「茂木区」、
「弊社」は「A社」「ヘイセン」「名社」と聞こえた）。判定は付けない（声の確かめだけ）。結果＝results/OWN-ANSWER-EARS_<時刻>.json"""
import asyncio
import json
import time

import scenes
from common import KeyRing, base_parser, read_wav_pcm16_mono
from convo import AUDIO_DIR, run_call
from ears import open_ear

SCENE = "目的の人_会社の所在地を聞かれる"


async def main():
    p = base_parser(__doc__)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--info", action="append", help="業務の情報を、この文に置き換える（地名の渡し方を比べる。例：読みをひらがなにして「」でくくる）")
    p.add_argument("--tag", default="")
    args = p.parse_args()
    ring = KeyRing(args)
    import dataclasses
    sc = scenes.SCENES[SCENE]
    if args.info:
        sc = dataclasses.replace(sc, business=dataclasses.replace(sc.business, info=list(args.info)))
    files = []
    for rep in range(args.repeat):
        for vt in ("S2", "S4", "L4"):
            call, _ = await ring.acall(args.model, lambda client, vt=vt, rep=rep: run_call(
                client, args.model, sc.business, vt, sc.lines, opening_mode="recorded", use_ear=True, save_audio="all",
                audio_label=f"OWNEARS{args.tag}_{vt}_{rep}_", followup_wait_s=scenes.FOLLOWUP_WAIT_S, play_recordings=True,
                callee_voice="swap", **scenes.FAST, **sc.kw))
            for t in call["turns"]:
                if any(e["name"] == "replay_opening" and e["reply"] == "own_answer" for e in t.get("tool_events") or []) and t.get("audio_file"):
                    files.append((vt, rep, t["audio_file"], t.get("intended"), t.get("ear")))
            print("通話", vt, rep, "答えの声", sum(1 for f in files if f[:2] == (vt, rep)))
    out = []
    for kind in ("soniox", "amivoice"):
        async with open_ear(kind) as ear:
            for vt, rep, f, intended, gem in files:
                pcm, rate = read_wav_pcm16_mono(AUDIO_DIR / f)
                loop = asyncio.get_running_loop()
                end = loop.time() + len(pcm) / 2 / rate
                await ear.feed(pcm, rate, 0.3)
                text = await ear.wait(end + 8.0 - loop.time())
                out.append({"ear": kind, "type": vt, "rep": rep, "file": f, "ai": intended, "test_ear": gem, "heard": text, "info": sc.business.info})
                print(kind, vt, rep, "| AI:", intended, "| 試験の耳:", gem, "|", kind, ":", text)
    path = scenes.HERE / "results" / f"OWN-ANSWER-EARS{args.tag}_{time.strftime('%Y%m%d_%H%M%S')}.json" if hasattr(scenes, "HERE") else None
    from pathlib import Path
    path = path or Path(__file__).parent / "results" / f"OWN-ANSWER-EARS{args.tag}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("結果を保存しました：", path)


asyncio.run(main())
