"""フェーズ0の確認スクリプトが共通で使う部品（ここ1か所にまとめる）。

ここにあるもの：
- APIキーの読み込み（値は外に出さない。識別名と指紋だけ使う）
- 画面・ファイルに出す文字から、キーの値を伏せる処理（DESIGN.md I-10）
- 結果の保存（phase0/results/ に JSON で残す）
- 音声ファイル（WAV）の読み書き

PHASE0_VERIFICATION.md の各項目の結果は、results/ の JSON を根拠にして書き込む。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

# 計画書（Intent.md 9-1）の値。公式ページで確かめるまでは仮の値（G-1）。
DEFAULT_MODEL = "gemini-3.8-live"
DEFAULT_ENV_PATH = r"C:\Users\User\SalesCall\.env"
VOICES = ("Sadachbia", "Leda")

# 入力に送る音声の形式。これまでの Live API の値（3.8 では G-2 で確かめる）。
INPUT_RATE = 16000
INPUT_MIME = f"audio/pcm;rate={INPUT_RATE}"

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"


# ---------------------------------------------------------------- キー
@dataclass(frozen=True)
class ApiKey:
    """キー1つ。値は `value` だけが持ち、表示には `label` を使う。"""

    name: str  # .env の変数名
    value: str

    @property
    def fingerprint(self) -> str:
        # 値そのものではなく、値から作った短い指紋（同じキーかどうかを見分けるだけ）
        return hashlib.sha256(self.value.encode("utf-8")).hexdigest()[:8]

    @property
    def label(self) -> str:
        return f"{self.name}#{self.fingerprint}"

    def __repr__(self) -> str:  # 誤って print しても値が出ないようにする
        return f"ApiKey({self.label})"


_KEY_NAME_PATTERN = re.compile(r"(GEMINI|GOOGLE).*KEY|KEY.*(GEMINI|GOOGLE)", re.IGNORECASE)
_LOADED_SECRETS: list[str] = []


def read_env_file(path: str | os.PathLike) -> list[tuple[str, str]]:
    """`.env` を、書き方を変えずに読む（KEY=VALUE、引用符あり・なし、export つき、# のコメント）。"""
    pairs: list[tuple[str, str]] = []
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if value[:1] in ("'", '"'):
            # 引用符で囲まれた値：閉じる引用符までが値（後ろの「# メモ」は捨てる）
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        pairs.append((name, value))
    return pairs


def load_keys(env_path: str | os.PathLike, key_names: list[str] | None = None) -> list[ApiKey]:
    """APIキーを読み込む唯一の場所。`key_names` を指定しなければ、名前に GEMINI/GOOGLE と KEY を含む変数を使う。

    `.env` のファイルがあればそこから、無ければ環境変数から読む（クラウドの作業環境では環境変数に入れる）。
    """
    if Path(env_path).is_file():
        pairs = read_env_file(env_path)
        source = str(env_path)
    else:
        pairs = sorted(os.environ.items())
        source = "環境変数"
    if key_names:
        wanted = set(key_names)
        keys = [ApiKey(n, v) for n, v in pairs if n in wanted and v]
        missing = wanted - {k.name for k in keys}
        if missing:
            raise SystemExit(f"指定したキーが .env にありません：{sorted(missing)}")
    else:
        keys = [ApiKey(n, v) for n, v in pairs if _KEY_NAME_PATTERN.search(n) and v]
    for k in keys:
        _LOADED_SECRETS.append(k.value)
    if not keys:
        raise SystemExit(
            f"APIキーが見つかりません（読んだ場所：{source}）。k01_env_shape.py で形を確かめ、--key-name で変数名を指定してください。"
        )
    say(f"キーを読んだ場所：{source}（{len(keys)} 個）")
    return keys


def redact(text: object) -> str:
    """画面・ファイルに出す前に、読み込んだキーの値をすべて伏せる（ここを通さずに出さない）。"""
    s = str(text)
    for secret in _LOADED_SECRETS:
        if secret:
            s = s.replace(secret, "***")
    return s


def say(*parts: object) -> None:
    """画面に出す唯一の口（必ず伏せてから出す）。"""
    print(redact(" ".join(str(p) for p in parts)), flush=True)


def make_client(key: ApiKey):
    from google import genai  # 使うときだけ読み込む（dry-run では不要）

    return genai.Client(api_key=key.value)


# ---------------------------------------------------------------- 結果
def save_result(item: str, data: dict) -> Path:
    """結果を results/<項目>_<日時>.json に保存する。中身も必ず伏せる。"""
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"{item}_{stamp}.json"
    payload = {"item": item, "saved_at": _dt.datetime.now().isoformat(timespec="seconds"), **data}
    path.write_text(redact(json.dumps(payload, ensure_ascii=False, indent=2, default=str)), encoding="utf-8")
    say(f"結果を保存しました：{path}")
    return path


def save_partial(item: str, data: dict) -> Path:
    """長い試験の途中の結果を results/<項目>_partial.json に上書きで残す（途中で止まっても、そこまでの結果が残るように）。
    最後に save_result で本の結果を保存したら、drop_partial で消す。中身は save_result と同じく必ず伏せる。"""
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{item}_partial.json"
    payload = {"item": item, "partial": True, "saved_at": _dt.datetime.now().isoformat(timespec="seconds"), **data}
    path.write_text(redact(json.dumps(payload, ensure_ascii=False, indent=2, default=str)), encoding="utf-8")
    return path


def drop_partial(item: str) -> None:
    (RESULTS_DIR / f"{item}_partial.json").unlink(missing_ok=True)


def describe_error(exc: BaseException) -> dict:
    """エラーを、種類を見分けられる形で記録する（K-3・DESIGN.md 9章の分類の材料）。"""
    info = {"type": type(exc).__name__, "module": type(exc).__module__, "message": redact(exc)[:2000]}
    for attr in ("code", "status", "details"):
        if hasattr(exc, attr):
            info[attr] = redact(getattr(exc, attr))
    return info


# ---------------------------------------------------------------- 音声
def parse_rate(mime_type: str | None, default: int) -> int:
    m = re.search(r"rate=(\d+)", mime_type or "")
    return int(m.group(1)) if m else default


def write_wav(path: Path, pcm16: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)


def read_wav_pcm16_mono(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path} は 16bit・モノラルの WAV にしてください")
        return w.readframes(w.getnframes()), w.getframerate()


def silence(ms: int, rate: int = INPUT_RATE) -> bytes:
    return b"\x00\x00" * (rate * ms // 1000)


# ---------------------------------------------------------------- 引数
def base_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--env", default=os.environ.get("SALESCALL_ENV", DEFAULT_ENV_PATH), help=".env の場所")
    p.add_argument("--key-name", action="append", help="使うキーの変数名（複数指定可）。省略時は名前で探す")
    p.add_argument("--key-index", type=int, default=0, help="何番目のキーを使うか（0 から）")
    p.add_argument("--model", default=DEFAULT_MODEL, help="モデルID（G-1 で公式の値を確かめる）")
    p.add_argument("--dry-run", action="store_true", help="接続せず、設定の組み立てだけ確かめる")
    p.add_argument(
        "--proxy-key",
        action="store_true",
        help="クラウドの作業環境用：キーの値を持たず、通信の中継所が付ける本物のキーを使う（ネットワークのシークレット）",
    )
    return p


# 中継所が本物のキーに置き換えるための、形だけの値（本物のキーではない）
PROXY_PLACEHOLDER = ApiKey("PROXY_INJECTED", "AIza" + "0" * 35)


def pick_key(args) -> ApiKey | None:
    """使うキーを決める唯一の場所。"""
    if args.dry_run:
        return None
    if args.proxy_key:
        say("使うキー：通信の中継所が付けるキー（作業環境の中にキーの値はありません）")
        return PROXY_PLACEHOLDER
    keys = load_keys(args.env, args.key_name)
    if not 0 <= args.key_index < len(keys):
        raise SystemExit(f"--key-index は 0〜{len(keys) - 1} です")
    key = keys[args.key_index]
    say(f"使うキー：{key.label}（{len(keys)} 個のうち {args.key_index} 番目）")
    return key


def require_python() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 以上で動かしてください")
