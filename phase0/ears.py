"""通話の中の別の耳（DESIGN.md 6-10 の (5)。G-15）：相手の声を、会話AIへ送るのと同時に別の音声認識へも流し、発話ごとの確定した文字を受け取る。

入口は1つ（open_ear）。どの耳も同じ形で使う（通話ごとに1本つなぐ・feed で相手の発話を流す・wait でその発話の確定した文字を待つ。流していない間は
無音を流し続ける＝電話がつながっている間と同じ）。耳を替えても、流れの制御（flowctl.Flow.on_ear・heard_alike）と試験（g14_listen --ear・
g15_ear --ear）は変えない。
- "transcribe35"：Gemini の書き起こし専用のモデル（gemini-3.5-transcribe-live）。⛔ G-15：会話AIと聞き違え方が似ていて、通話の中で使うと
  要らない確かめが8.5%増え、拾えた誤りは0 ⇒ 既定では使わない。
- "amivoice"：AmiVoice（アドバンスト・メディア。日本の会社。日本語専門・電話向けの実績）。WebSocket の s／p／e の命令、確定した文字は A の知らせ。
  記録を残さない接続先（…/v1/nolog/）を使う（相手の声を相手の会社に残さない）。キー＝環境変数 AMIVOICE_APPKEY。
- "soniox"：Soniox（日本語の精度を最も強く打ち出している。自社の測定）。WebSocket。最初に設定の JSON、そのあと声。話し終わり（<end> の印）で、
  その発話の確定した文字を渡す。キー＝環境変数 SONIOX_API_KEY。
- "reazon"：ReazonSpeech（日本語専門のモデル。PC の中で動く＝相手の声を外の会社に出さない）。sherpa-onnx で動かす。モデルは models/reazonspeech/
  （get_reazon_model.py で取る。GitHub には上げない）。試験では、発話を流し終えたところで文字にする（本番では、話し終わりの見張り（VAD）が要る）。
⚠️ 2026-10-17：AmiVoice・Soniox の接続先は、この作業環境の通信の決まりで止められている（ユーザーが許可を足すまで、つないで確かめられない。
   設定に貼る文字は docs/EARS_SETUP.md）。命令の形は公式の資料の要点から書いた ⇒ つないだら、1本目で必ず確かめる（ears_try.py が確かめる）。
   ReazonSpeech のモデルは、いまの通信の決まりのまま取れる置き場から取る（get_reazon_model.py）。
キーの値は記録・画面に出さない（common.load_secret が伏せる一覧に入れる）。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from audio import resample
from common import HERE, load_secret, silence

RATE = 16000                      # どの耳にも 16kHz・16bit・1チャンネルで流す（電話の 8kHz は、本番の音声の変換の1か所で 16kHz にする＝8-3）
CHUNK_S = 0.1                     # 0.1秒ずつ、実際の通話と同じ速さで流す
SECRETS = {"amivoice": "AMIVOICE_APPKEY", "soniox": "SONIOX_API_KEY"}
AMIVOICE_URL = "wss://acp-api.amivoice.com/v1/nolog/"   # ⚠️ 最後の「/」が無いと、記録を残す接続先になる（公式の資料）
AMIVOICE_ENGINE = "-a-general"   # 汎用エンジン
# 言いよどみ（フィラー）として扱う言葉を、結果から消さない。⛔ 2026-10-17 G-15：初めの設定のままでは、答えの頭の「いいえ」「ええ」を
# 言いよどみとして消していた（「いいえ、まだです。」⇒「まだです。」・「ええ、してます。」⇒「してます。」。この設定で「%い%%え%まだです。」
# 「%えー%してます。」と返る＝はい／いいえの答えでは、まさにこの言葉が要る）。
AMIVOICE_KEEP_FILLER = "keepFillerToken=1"
SONIOX_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
SONIOX_MODEL = "stt-rt-v5"        # リアルタイムの最新（2026-10-17 に Soniox のモデルの一覧で確かめた：stt-rt-v3・v4・v5。ears_try が毎回一覧を出す）
REAZON_DIR = HERE / "models" / "reazonspeech"


class Ear:
    """別の耳の共通の形。下の耳は _open・_send・_close・_receive（届いたものを _final へ）だけを書く。"""
    name = ""

    def __init__(self):
        self.errors: list[str] = []
        self.first_at: float | None = None

    async def __aenter__(self):
        self._loop = asyncio.get_running_loop()
        self._got, self._text = self._loop.create_future(), ""
        self._feeding = asyncio.Event()
        await self._open()
        self._tasks = [asyncio.create_task(self._guard(self._receive())), asyncio.create_task(self._guard(self._silence()))]
        return self

    async def __aexit__(self, *exc):
        for t in self._tasks:
            t.cancel()
        try:
            await self._close()
        except Exception as e:  # 閉じるときの誤りで、通話の記録を失わない
            self.errors.append(repr(e)[:200])

    async def _guard(self, coro) -> None:
        """受け取り・無音の作業が落ちても、通話は止めない（文字が届かないだけ＝待つ側が時間で進む）。"""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.errors.append(repr(e)[:200])

    def _final(self, text: str) -> None:
        """いまの発話の確定した文字が届いた（受け取りの処理が呼ぶ）。声を流している間に届いた分（耳が発話の途中の間で区切ったかけら）は
        ためておき、声を流し終えたあとに届いた分で、それまでの分と合わせて1つの発話の文字として渡す。その時刻を残す（待つ時間を決めるための記録）。
        ⛔ 2026-10-17：最初に届いたかけらで渡していたため、Soniox が「はい、先週しました。」を途中の間で区切ると「はい。」だけになった。"""
        if not text:
            return
        self._text += text
        if not self._feeding.is_set() and not self._got.done():
            self.first_at = self._loop.time()
            self._got.set_result(self._text)

    async def _silence(self) -> None:
        chunk = silence(int(CHUNK_S * 1000), RATE)
        while True:
            if not self._feeding.is_set():
                await self._send(chunk, voice=False)
            await asyncio.sleep(CHUNK_S)

    async def feed(self, pcm16: bytes, rate: int) -> None:
        """相手の発話1回ぶんを、実際の通話と同じ速さで流す（この発話の文字を、新しく受け取り始める）。"""
        self._got, self._text, self.first_at = self._loop.create_future(), "", None
        self._feeding.set()
        try:
            pcm = resample(pcm16, rate, RATE)
            step = int(RATE * CHUNK_S) * 2
            for i in range(0, len(pcm), step):
                await self._send(pcm[i:i + step], voice=True)
                await asyncio.sleep(CHUNK_S)
            self._feeding.clear()       # ここから先に届いた確定した文字で渡す（_final）
            await self._fed()
        except Exception as e:
            self.errors.append(repr(e)[:200])
        finally:
            self._feeding.clear()

    async def wait(self, timeout_s: float) -> str | None:
        """いまの発話の確定した文字（声を流し終えたあとに届いた分まで。timeout_s 秒までに届かなければ、それまでにためた分か None）。"""
        try:
            return await asyncio.wait_for(asyncio.shield(self._got), max(0.0, timeout_s))
        except asyncio.TimeoutError:
            return self._text or None

    # ── 耳ごとに書く所
    async def _open(self) -> None: ...
    async def _send(self, pcm: bytes, voice: bool) -> None: ...
    async def _close(self) -> None: ...
    async def _receive(self) -> None: ...
    async def _fed(self) -> None:
        """発話を流し終えた（ふつうは何もしない。耳が自分で話し終わりに気づく）。"""


class GeminiEar(Ear):
    name = "transcribe35"

    def __init__(self, client):
        super().__init__()
        self.client = client

    async def _open(self) -> None:
        from google.genai import types

        self._cm = self.client.aio.live.connect(model="gemini-3.5-transcribe-live", config=types.LiveConnectConfig(
            input_audio_transcription=types.AudioTranscriptionConfig()))
        self.s = await self._cm.__aenter__()

    async def _send(self, pcm: bytes, voice: bool) -> None:
        from google.genai import types

        await self.s.send_realtime_input(audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={RATE}"))

    async def _close(self) -> None:
        await self._cm.__aexit__(None, None, None)

    async def _receive(self) -> None:
        while True:
            async for m in self.s.receive():
                sc = m.server_content
                if sc and sc.input_transcription and sc.input_transcription.text and getattr(sc.input_transcription, "finished", True) is not False:
                    self._final(sc.input_transcription.text)


def amivoice_final(event: str) -> str | None:
    """AmiVoice の知らせ（文字の1行）から、確定した文字を取り出す。確定（A）のときだけ文字、それ以外は None。
    知らせ：S（話し始め）・E（話し終わり）・C（文字にし始め）・U（途中の文字）・A（確定した文字。JSON の text）・G（そのほか）。
    言いよどみの印（「%えー%」の %）は外して、言葉は残す（AMIVOICE_KEEP_FILLER）。"""
    if not event.startswith("A"):
        return None
    try:
        text = json.loads(event[1:].strip() or "{}").get("text") or ""
    except json.JSONDecodeError:
        return None
    return re.sub(r"%([^%]*)%", r"\1", text).strip() or None


class AmiVoiceEar(Ear):
    name = "amivoice"

    def __init__(self, key: str, url: str = AMIVOICE_URL, engine: str = AMIVOICE_ENGINE):
        super().__init__()
        self.key, self.url, self.engine = key, url, engine

    async def _open(self) -> None:
        import websockets

        self.ws = await websockets.connect(self.url, max_size=None)
        await self.ws.send(f"s LSB16K {self.engine} authorization={self.key} {AMIVOICE_KEEP_FILLER}")
        ack = await self.ws.recv()                      # s の返事（誤りなら「s 理由」）
        if not isinstance(ack, str) or ack.strip() != "s":
            raise RuntimeError(f"AmiVoice が始められませんでした：{str(ack)[:120]}")

    async def _send(self, pcm: bytes, voice: bool) -> None:
        await self.ws.send(b"p" + pcm)                  # p の命令：先頭の「p」のあとに声

    async def _close(self) -> None:
        try:
            await self.ws.send("e")
        finally:
            await self.ws.close()

    async def _receive(self) -> None:
        async for msg in self.ws:
            if isinstance(msg, str) and (text := amivoice_final(msg)):
                self._final(text)


def soniox_update(pending: list[str], msg: dict) -> list[str]:
    """Soniox の返事1つを読み、話し終わった発話の文字の一覧を返す（pending は、まだ話し終わっていない確定した文字＝呼ぶ側が持つ）。
    確定した言葉（is_final）だけを足し、確定し終わった印（こちらから頼んだ確定の「<fin>」・Soniox が自分で決めた話し終わりの「<end>」）で
    1つの発話として渡す。誤りの返事は誤りを上げる。"""
    if msg.get("error_code") or msg.get("error_message"):
        raise RuntimeError(f"Soniox の誤り：{msg.get('error_code')} {str(msg.get('error_message'))[:120]}")
    done = []
    for t in msg.get("tokens") or []:
        if not t.get("is_final"):
            continue
        if t.get("text") in ("<end>", "<fin>"):
            if pending:
                done.append("".join(pending).strip())
                pending.clear()
        else:
            pending.append(t.get("text") or "")
    return done


class SonioxEar(Ear):
    """声を流し終えたところで、こちらから確定を頼む（{"type": "finalize"}。Soniox の「手動の確定」）。話し終わりはこちら（通話の側）が
    知っているので、区切りはこちらの1か所で決め、Soniox が自分で話し終わりを決める仕組み（endpoint detection）は使わない。
    ⛔ 2026-10-17：Soniox が自分で決める形では、通話の中の153本のうち17本で、2秒の待ちに文字が間に合わなかった。"""
    name = "soniox"

    def __init__(self, key: str, url: str = SONIOX_URL, model: str = SONIOX_MODEL):
        super().__init__()
        self.key, self.url, self.model = key, url, model
        self._pending: list[str] = []

    async def _open(self) -> None:
        import websockets

        self.ws = await websockets.connect(self.url, max_size=None)
        await self.ws.send(json.dumps({"api_key": self.key, "model": self.model, "audio_format": "pcm_s16le", "sample_rate": RATE,
                                       "num_channels": 1, "language_hints": ["ja"], "enable_endpoint_detection": False}))

    async def _send(self, pcm: bytes, voice: bool) -> None:
        await self.ws.send(pcm)

    async def _fed(self) -> None:
        await self.ws.send(json.dumps({"type": "finalize"}))

    async def _close(self) -> None:
        try:
            await self.ws.send("")                      # 空の知らせ＝声の終わり
        finally:
            await self.ws.close()

    async def _receive(self) -> None:
        async for msg in self.ws:
            for text in soniox_update(self._pending, json.loads(msg)):
                self._final(text)


def reazon_files(d: Path = REAZON_DIR) -> dict:
    """ReazonSpeech のモデルのファイル（エンコーダ・デコーダ・ジョイナ・言葉の一覧）。無ければ、取り方を書いた誤りを上げる。"""
    def one(kind: str) -> str:
        got = sorted(p for p in d.glob(f"{kind}*.onnx") if "int8" not in p.name) or sorted(d.glob(f"{kind}*.onnx"))
        if not got:
            raise SystemExit(f"ReazonSpeech のモデル（{kind}）が {d} にありません。python get_reazon_model.py で取ってください")
        return str(got[0])
    tokens = d / "tokens.txt"
    if not tokens.exists():
        raise SystemExit(f"ReazonSpeech のモデル（tokens.txt）が {d} にありません。python get_reazon_model.py で取ってください")
    return {"encoder": one("encoder"), "decoder": one("decoder"), "joiner": one("joiner"), "tokens": str(tokens)}


class ReazonEar(Ear):
    """PC の中で動く耳。声は外に出ない。発話を流し終えたところで文字にする（文字にする時間がそのまま遅れになる）。
    発話の前の PREROLL_S 秒（その前に流れていた回線の音）も一緒に文字にする。⛔ 2026-10-17：声の頭から切り出して渡すと、頭の言葉を落とした
    （G-14 の Aoede の6本で「はい、予約しています。」⇒「予約しています」・「クレジットカードで。」⇒「カードで」。前に 0.5秒の無音を付けると
    6本とも頭が戻った）。通話では相手が話し始める前にも回線の音が流れているので、話し始めの少し前から切り出すのが正しい形（ほかの耳は、
    つながっている間の音をずっと受け取っているので、この切り出しは要らない）。"""
    name = "reazon"
    PREROLL_S = 0.5
    _recognizer = None            # モデルは1回だけ読む（通話ごとに読み直さない）

    def __init__(self, threads: int = 2):
        super().__init__()
        self.threads = threads
        self._buf = bytearray()
        self._pre = bytes(int(self.PREROLL_S * RATE) * 2)   # 通話の始めは無音から（つないだ直後の発話にも、前の音がある形にする）

    async def _open(self) -> None:
        if ReazonEar._recognizer is None:
            import sherpa_onnx

            ReazonEar._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                **reazon_files(), num_threads=self.threads, sample_rate=RATE, feature_dim=80, decoding_method="greedy_search")

    async def _send(self, pcm: bytes, voice: bool) -> None:
        if voice:
            if not self._buf:
                self._buf.extend(self._pre)          # 発話の頭の前の音から
            self._buf.extend(pcm)
        else:
            self._pre = (self._pre + pcm)[-int(self.PREROLL_S * RATE) * 2:]

    async def _fed(self) -> None:
        import numpy as np

        pcm, self._buf = bytes(self._buf), bytearray()
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        def decode() -> str:
            st = ReazonEar._recognizer.create_stream()
            st.accept_waveform(RATE, samples)
            ReazonEar._recognizer.decode_stream(st)
            return st.result.text
        self._final((await asyncio.to_thread(decode)).strip())

    async def _receive(self) -> None:
        return None


def open_ear(kind: str, client=None, env_path: str | None = None) -> Ear:
    """別の耳を1本作る唯一の入口（つなぐのは async with のとき）。キーは環境（.env か環境変数）から読み、値は伏せる一覧に入れる。"""
    if kind == "transcribe35":
        return GeminiEar(client)
    if kind == "reazon":
        return ReazonEar()
    if kind in SECRETS:
        key = load_secret(SECRETS[kind], env_path)
        return AmiVoiceEar(key) if kind == "amivoice" else SonioxEar(key)
    raise SystemExit(f"別の耳「{kind}」はありません（選べるのは {', '.join(EAR_KINDS)}）")


EAR_KINDS = ("transcribe35", "amivoice", "soniox", "reazon")
# 使う耳の順（2026-10-17 ユーザー：「別の何かで差が付く可能性があるので、Soniox > AmiVoice の順で、最終確定はフェーズ1で決めましょう」）。
# 根拠＝PHASE0_VERIFICATION.md G-15 の続きの4つの耳の表。名前を付けずに別の耳を使うとき（g14_listen.py --ear）は、この先頭の耳。
PREFERRED_EARS = ("soniox", "amivoice")
