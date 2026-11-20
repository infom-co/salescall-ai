"""確かめの道具：本物の電話の部品（rakuten_link.make＝画面の口 screen_uia・音の装置 sound_vac）で、1本の電話を通しで動かす。持ち主 CL-3。

かける → 呼び出しの音を聞き分ける（tones）→ つながったと読む → お客様の声がサーバーへ送る形で届く → こちらの音を流す → 切る、を
1回で確かめる（docs/PHONE.md 3章の R-2〜R-6 を、本番と同じ部品で）。管理者の PC で、このリポジトリの中から動かす：
    cd C:\\Users\\User\\SalesCall\\salescall-ai
    git pull
    py app\\tools\\phone_probe\\probe_call.py

- かける相手は、ご家族の携帯だけ。番号はその場で打ち、ファイルにも結果にも残さない（結果には下4けただけ）。
- 結果は phone_call_result.json（このファイルと同じフォルダ。番号・お名前・声は入らない＝出来事の名前と秒数だけ）。
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "src"))   # app/src（この道具は本番の外＝app/tools/phone_probe に置く）

from salescall.agent.phones import audio, rakuten_link  # noqa: E402

RESULT = HERE.parent / "phone_call_result.json"


def beep(hz: float, seconds: float, rate: int) -> bytes:
    n = int(seconds * rate)
    return audio.pcm_of([7000 * math.sin(2 * math.pi * hz * i / rate) for i in range(n)])


def main() -> None:
    number = input("ご家族の携帯の番号を、0から始まる数字だけで打って Enter（ファイルには残しません）\n> ").strip()
    print("電話の部品を用意しています（数秒）…")
    phone = rakuten_link.make("probe")
    t0 = time.monotonic()
    events: list[dict] = []
    voiced = {"frames": 0, "after_answer": 0}
    answered_at: list[float] = []

    def on_event(e: dict) -> None:
        at = round(time.monotonic() - t0, 1)
        events.append({"at_s": at, "kind": e.get("kind")})
        print(f"  {at:5.1f}秒：出来事＝{e.get('kind')}")
        if e.get("kind") == "answered":
            answered_at.append(time.monotonic())

    def on_audio(frame: bytes) -> None:
        if audio.rms_of(frame) > 300:
            voiced["frames"] += 1
            if answered_at:
                voiced["after_answer"] += 1

    phone.on_event(on_event)
    phone.on_audio(on_audio)
    input("ご家族には「出たら『もしもし』と言って、そのあと『ピッ』が3回聞こえたら『聞こえた』と言ってください」とお伝えください。\n"
          "用意ができたら Enter。プログラムが発信します（終わるまでマウスとキーボードにさわらないでください）。")
    result: dict = {"at": datetime.now().isoformat(timespec="seconds"), "to_last4": number[-4:]}
    t0 = time.monotonic()
    try:
        phone.start_call(number)
        result["placed"] = True
        result["dial_timings"] = dict(getattr(phone.screen, "timings", {}))
        print("  発信の手順ごとの秒数：", result["dial_timings"])
    except Exception as e:
        result["placed"] = False
        result["error"] = f"{type(e).__name__}: {e}"[:200]
        print("発信できませんでした：", result["error"])
    if result["placed"]:
        end = time.monotonic() + 40
        while time.monotonic() < end and not answered_at:
            time.sleep(0.2)
        if answered_at:
            time.sleep(3)
            print("  こちらの音（ピッ×3）を流します")
            pcm = b"".join(beep(1000, 0.3, rakuten_link.FROM_SERVER_RATE) + b"\x00\x00" * int(0.5 * rakuten_link.FROM_SERVER_RATE)
                           for _ in range(3))
            phone.play(pcm)
            time.sleep(6)
        try:
            phone.hang_up()
        except Exception as e:
            result["hang_up_error"] = f"{type(e).__name__}: {e}"[:200]
        time.sleep(2)
    result["events"] = events
    result["ringback_heard"] = any(e["kind"] == "ringback" for e in events)
    result["answered_seen"] = bool(answered_at)
    result["ended_seen"] = any(e["kind"] == "ended" for e in events)
    result["voice_seconds_to_server"] = round(voiced["frames"] * 0.02, 1)
    result["voice_seconds_after_answer"] = round(voiced["after_answer"] * 0.02, 1)
    result["family_heard_beeps"] = input("\nご家族に「ピッ」が3回聞こえたかを聞いて、y／n／s を打ってください\n> ").strip().lower()[:1]
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果を {RESULT} に書きました。このファイルを送ってください。")
    try:
        phone.sound.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
