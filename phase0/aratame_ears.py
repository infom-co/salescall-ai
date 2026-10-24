"""外れた言葉を止めたあとの言い直しの声を、独立した耳（Soniox・AmiVoice）でも聞く（声の誤りか、試験の耳の誤りかを分ける。G-57 の言い間違えの直し・2026-10-19）。
2026-10-25：場面が3つ（聞こえる前・受けの言葉だけ・中身まで）に分かれたので、その3つを回帰と同じ判定で流す（scenes.run の通話の作りと同じ）。
日時の言い直しは「15日の14時、午後2時でよろしいでしょうか？」（flowctl.spoken_clock）。"""
import asyncio, json, sys
sys.path.insert(0, "/home/user/salescall-ai/phase0")
from pathlib import Path
import scenes
from common import KeyRing, base_parser, read_wav_pcm16_mono
from convo import run_call, AUDIO_DIR
from ears import open_ear
from meaning_judge import add_judge_args, make_judges


async def main():
    p = base_parser(__doc__)
    add_judge_args(p)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--out", default="ARATAME-EARS-4", help="results/ に残す名前")
    args = p.parse_args()
    ring, judges = KeyRing(args), make_judges(args)
    files = []
    for rep in range(args.repeat):
        for name in [n for n in scenes.SCENES if n.startswith("言い間違えの直し")]:
            sc = scenes.SCENES[name]
            for vt in ("S2", "S4", "L4"):
                lines, kw = sc.call()
                call, _ = await ring.acall(args.model, lambda client: run_call(
                    client, args.model, sc.business, vt, lines, opening_mode="recorded", use_ear=True, save_audio="all",
                    audio_label=f"{args.out}_{name}_{vt}_{rep}_", followup_wait_s=scenes.FOLLOWUP_WAIT_S, play_recordings=True,
                    callee_voice="swap", **judges, **scenes.FAST, **kw))
                m = scenes.measures(call, name)
                print("通話", name, vt, rep, "合格" if m["pass"] else "不合格", m["failed"], m["restate_gap_s"], flush=True)
                for t in call["turns"]:
                    if isinstance(t.get("callee"), dict) and "restate" in t["callee"]:
                        if not t.get("audio_file"):
                            print("  ⚠️ 言い直しの声が保存されていない", name, vt, rep, t.get("intended"), t.get("audio_seconds"), flush=True)
                        else:
                            files.append((name, vt, rep, t["audio_file"], t.get("intended"), t.get("ear"), t.get("gap_s"), t.get("gap_parts_s")))
    out = []
    for kind in ("soniox", "amivoice"):
        async with open_ear(kind) as ear:
            for name, vt, rep, f, intended, gem, gap, parts in files:
                pcm, rate = read_wav_pcm16_mono(AUDIO_DIR / f)
                loop = asyncio.get_running_loop()
                end = loop.time() + len(pcm) / 2 / rate
                await ear.feed(pcm, rate, 0.3)
                text = await ear.wait(end + 8.0 - loop.time())
                out.append({"ear": kind, "scene": name, "type": vt, "rep": rep, "file": f, "ai": intended, "gemini_ear": gem, "heard": text,
                            "gap_s": gap, "parts": parts})
                print(kind, name, vt, rep, "| AI:", intended, "| Gemini耳:", gem, "|", kind, ":", text, "| 間", gap, parts, flush=True)
    Path("/home/user/salescall-ai/phase0/results").joinpath(f"{args.out}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                                                           encoding="utf-8")

asyncio.run(main())
