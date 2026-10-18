"""外れた言葉を止めたあとの言い直しの声を、独立した耳（Soniox・AmiVoice）でも聞く（声の誤りか、試験の耳の誤りかを分ける。G-57 の言い間違えの直し・2026-10-19）。"""
import asyncio, json, sys
sys.path.insert(0, "/home/user/salescall-ai/phase0")
from pathlib import Path
import scenes
from common import KeyRing, read_wav_pcm16_mono
from convo import run_call, AUDIO_DIR
from ears import open_ear
from listen import differences

class A: pass

async def main():
    from common import base_parser
    args = base_parser("").parse_args([])
    ring = KeyRing(args)
    sc = scenes.SCENES["言い間違えの直し"]
    files = []
    for rep in range(2):
        for vt in ("S2", "S4", "L4"):
            call, _ = await ring.acall(args.model, lambda client: run_call(
                client, args.model, sc.business, vt, sc.lines, opening_mode="recorded", use_ear=True, save_audio="all",
                audio_label=f"ARATAME3_{vt}_{rep}_", followup_wait_s=scenes.FOLLOWUP_WAIT_S, play_recordings=True,
                callee_voice="swap", **scenes.FAST, **sc.kw))
            m = scenes.measures(call, "言い間違えの直し")
            print("通話", vt, rep, "合格" if m["pass"] else "不合格", m["failed"], m["restate_gap_s"])
            for t in call["turns"]:
                if isinstance(t.get("callee"), dict) and "restate" in t["callee"] and not t.get("audio_file"):
                    print("  ⚠️ 言い直しの声が保存されていない", vt, rep, t.get("intended"), t.get("audio_seconds"))
                if isinstance(t.get("callee"), dict) and "restate" in t["callee"] and t.get("audio_file"):
                    files.append((vt, rep, t["audio_file"], t.get("intended"), t.get("ear"), t.get("gap_s"), t.get("gap_parts_s")))
    out = []
    for kind in ("soniox", "amivoice"):
        async with open_ear(kind) as ear:
            for vt, rep, f, intended, gem, gap, parts in files:
                pcm, rate = read_wav_pcm16_mono(AUDIO_DIR / f)
                loop = asyncio.get_running_loop()
                end = loop.time() + len(pcm) / 2 / rate
                await ear.feed(pcm, rate, 0.3)
                text = await ear.wait(end + 8.0 - loop.time())
                out.append({"ear": kind, "type": vt, "rep": rep, "file": f, "ai": intended, "gemini_ear": gem, "heard": text, "gap_s": gap, "parts": parts})
                print(kind, vt, rep, "| AI:", intended, "| Gemini耳:", gem, "|", kind, ":", text, "| 間", gap, parts)
    Path("/home/user/salescall-ai/phase0/results").joinpath("ARATAME-EARS-3.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

asyncio.run(main())
