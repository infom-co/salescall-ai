"""確かめの道具：Rakuten Link の声の受け渡し（docs/PHONE.md 3章の R-2・R-3・R-4）。持ち主 CL-3。

管理者の PC で、このファイルだけを動かす（ほかの部品を読まない＝リポジトリが無くても動く。確かめの道具なので、本物の部品の
「声の形を変える1か所」とは別に、測るための小さな計算だけを持つ）。

使い方（Windows。Python 3.13 と sounddevice が要る）：
    py -m pip install sounddevice==0.5.1
    py probe_audio.py

前の準備：Virtual Audio Cable の Line 1・Line 2 があり、Rakuten Link の出力＝Line 1・入力＝Line 2 にしてある。
電話をかける相手は、管理者の手元の電話かご家族の電話だけ（管理者の携帯は発信の元なので受けられない）。結果は同じフォルダの phone_probe_result.json（番号・お客様の情報は入れない）と、
録った声の wav（管理者の声。送らなくてよい）。
"""

from __future__ import annotations

import array
import json
import math
import platform
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

RATE = 48000
FRAME = RATE // 50          # 20ms
OUT = Path(__file__).resolve().parent
RESULT = OUT / "phone_probe_result.json"


def _sd():
    try:
        import sounddevice
    except ImportError:
        sys.exit("sounddevice が入っていません。次を打ってから、もう一度動かしてください：\n  py -m pip install sounddevice==0.5.1")
    return sounddevice


def find(sd, name: str, kind: str) -> int | None:
    """名前に name を含む装置（kind＝input／output）。同じ装置がいくつかの形で出るので、最初に見つかったものを使う。"""
    for i, d in enumerate(sd.query_devices()):
        ch = d["max_input_channels"] if kind == "input" else d["max_output_channels"]
        if name.lower() in d["name"].lower() and ch > 0:
            return i
    return None


def beep(hz: float, seconds: float, amp: float = 9000.0) -> bytes:
    n = int(seconds * RATE)
    fade = int(0.01 * RATE)
    vals = array.array("h", (int(amp * math.sin(2 * math.pi * hz * i / RATE) * min(1.0, i / fade, (n - i) / fade)) for i in range(n)))
    return vals.tobytes()


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * RATE)


def frames(pcm: bytes) -> list[array.array]:
    a = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    return [a[i:i + FRAME] for i in range(0, len(a) - FRAME + 1, FRAME)]


def rms(f: array.array) -> float:
    return math.sqrt(sum(v * v for v in f) / len(f)) if len(f) else 0.0


def share(f: array.array, hz: float) -> float:
    """その切れ目で、hz の音が全体の音のどれだけを占めるか（0〜1）。"""
    energy = sum(v * v for v in f)
    if energy == 0:
        return 0.0
    c = 2 * math.cos(2 * math.pi * hz / RATE)
    s1 = s2 = 0.0
    for v in f:
        s0 = v + c * s1 - s2
        s2, s1 = s1, s0
    return min(1.0, 2 * (s1 * s1 + s2 * s2 - c * s1 * s2) / (len(f) * energy))


def record(sd, dev: int, seconds: float) -> bytes:
    chunks: list[bytes] = []
    with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", device=dev, blocksize=FRAME) as s:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            data, _ = s.read(FRAME)
            chunks.append(bytes(data))
    return b"".join(chunks)


def play(sd, dev: int, pcm: bytes) -> None:
    with sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", device=dev, blocksize=FRAME) as s:
        for i in range(0, len(pcm), FRAME * 2):
            s.write(pcm[i:i + FRAME * 2])


def save(name: str, pcm: bytes) -> str:
    p = OUT / name
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return p.name


def ask(text: str) -> str:
    return input(f"\n{text}\n> ").strip().lower()


def r2(sd, line1: int) -> dict:
    """お客様の声（相手の携帯の声）が、Line 1 から途切れずに録れるか。管理者の携帯は発信の元なので、受けるのは別の電話（ご家族など）。"""
    ask("【R-2】Rakuten Link から、受ける方の電話へかけてください（管理者の携帯ではなく、ご家族の携帯など）。受ける方が出たら Enter を押してください。")
    print("これから20秒、録ります。受ける方に、1から順に数をゆっくり数えてもらいます（前もって伝えておいてください）。")
    pcm = record(sd, line1, 20.0)
    fs = frames(pcm)
    loud = [rms(f) for f in fs]
    voiced = sum(1 for v in loud if v > 300) * 0.02
    # 声のあいだに、0 だけが続く所（音が抜けた所）が 0.1秒以上あるか
    gaps, run = 0, 0
    for f in fs:
        if all(v == 0 for v in f):
            run += 1
        else:
            if run * 0.02 >= 0.1:
                gaps += 1
            run = 0
    ok = voiced >= 5.0 and gaps == 0
    print(f"声が入っていた長さ：{voiced:.1f}秒／音が抜けた所：{gaps}か所 ⇒ {'合格' if ok else '不合格'}")
    return {"voiced_s": round(voiced, 1), "dropouts": gaps, "peak_rms": round(max(loud or [0]), 1), "wav": save("r2_line1.wav", pcm), "pass": ok}


def r3(sd, line1: int, line2: int) -> dict:
    """Line 2 へ流した音が、相手の携帯で聞こえるか（AI の声の代わりの短い音）。確かめのあいだ、管理者は通話の声を聞けないので、
    受けた方の返事（「聞こえた」など）を5秒録って残す（あとで再生して聞く）。"""
    ask("【R-3】電話はつないだままです。Enter で、短い音を3回流します。受けた方には、音が3回聞こえたら「聞こえた」と言ってもらいます。")
    play(sd, line2, b"".join(beep(1000, 0.3) + silence(0.5) for _ in range(3)))
    print("受けた方の返事を5秒録ります。")
    reply = record(sd, line1, 5.0)
    name = save("r3_reply.wav", reply)
    spoke = sum(1 for f in frames(reply) if rms(f) > 300) * 0.02
    print(f"返事の声：{spoke:.1f}秒。{name} を再生して、受けた方が何と答えたかを聞いてください（音を出すのはパソコンのふだんのスピーカー）。")
    a = ask("受けた方は「聞こえた」と答えていましたか？（y＝はい／n＝いいえ／s＝分からない）")
    ok = a.startswith("y")
    print("⇒ 合格" if ok else "⇒ 不合格" if a.startswith("n") else "⇒ 分からない")
    return {"heard": ok, "answer": a[:1], "reply_voiced_s": round(spoke, 1), "wav": name, "pass": ok}


def r4(sd, line1: int, line2: int) -> dict:
    """Line 2 へ流した音が、Line 1（録る側）へ戻ってこないか（エコー）。"""
    ask("【R-4】電話はつないだままです。受ける方には、黙っていてもらいます（前もって伝えておいてください）。Enter を押すと、音を流しながら録ります。")
    beeps = b"".join(beep(1000, 0.4) + silence(0.6) for _ in range(5))
    got: dict[str, bytes] = {}
    t = threading.Thread(target=lambda: got.update(pcm=record(sd, line1, len(beeps) / 2 / RATE + 1.0)))
    t.start()
    time.sleep(0.3)
    play(sd, line2, beeps)
    t.join()
    pcm = got.get("pcm", b"")
    fs = frames(pcm)
    tone = [share(f, 1000) for f in fs if rms(f) > 150]
    echo = sum(1 for s in tone if s > 0.3) * 0.02
    ok = echo < 0.2
    print(f"録る側に戻ってきた音：{echo:.2f}秒 ⇒ {'合格（戻ってこない）' if ok else '不合格（戻ってくる）'}")
    return {"echo_s": round(echo, 2), "wav": save("r4_line1.wav", pcm), "pass": ok}


def main() -> None:
    sd = _sd()
    line1_in, line2_out = find(sd, "Line 1", "input"), find(sd, "Line 2", "output")
    print("見つけた装置：")
    print(f"  お客様の声を録る（Line 1）：{sd.query_devices(line1_in)['name'] if line1_in is not None else '見つかりません'}")
    print(f"  AI の声を流す（Line 2）　：{sd.query_devices(line2_out)['name'] if line2_out is not None else '見つかりません'}")
    if line1_in is None or line2_out is None:
        sys.exit("Virtual Audio Cable の Line 1・Line 2 が見つかりません。コントロールパネルで線の数が2本かを確かめてください。")
    result = {"at": datetime.now().isoformat(timespec="seconds"), "python": platform.python_version(), "windows": platform.platform(),
              "devices": {"line1_in": sd.query_devices(line1_in)["name"], "line2_out": sd.query_devices(line2_out)["name"]}}
    for key, run in (("R-2", lambda: r2(sd, line1_in)), ("R-3", lambda: r3(sd, line1_in, line2_out)), ("R-4", lambda: r4(sd, line1_in, line2_out))):
        try:
            result[key] = run()
        except Exception as e:   # 1つ落ちても、ほかの確かめと記録は続ける
            result[key] = {"error": f"{type(e).__name__}: {e}"[:300], "pass": False}
            print(f"{key} で誤りが出ました：{result[key]['error']}")
    ask("確かめは終わりです。Rakuten Link の電話を切ってから、Enter を押してください。")
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果を {RESULT} に書きました。このファイル（phone_probe_result.json）だけを送ってください（wav は送らなくて構いません）。")


if __name__ == "__main__":
    main()
