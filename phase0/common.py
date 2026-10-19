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


def _secret_value(name: str, env_path: str | os.PathLike | None) -> str | None:
    env_path = env_path or os.environ.get("SALESCALL_ENV", DEFAULT_ENV_PATH)
    pairs = read_env_file(env_path) if Path(env_path).is_file() else list(os.environ.items())
    return next((v for n, v in pairs if n == name and v), None)


def has_secret(name: str, env_path: str | os.PathLike | None = None) -> bool:
    """キーが入っているか（値は伏せる一覧にも画面にも出さず、あるかだけ。足りないものの確かめ用）。"""
    return _secret_value(name, env_path) is not None


def load_secret(name: str, env_path: str | os.PathLike | None = None) -> str:
    """Gemini 以外のキー（別の耳など）を1つ読む唯一の場所。`.env` があればそこから、無ければ環境変数から。値は伏せる一覧に入れ、画面には名前だけ出す。
    無ければ、入れ方を書いて止める（⛔ キーの値をチャットで聞かない。クラウドの作業環境では「環境変数」の欄に入れていただく＝docs/EARS_SETUP.md）。"""
    value = _secret_value(name, env_path)
    if not value:
        raise SystemExit(f"キー {name} が見つかりません。作業環境の設定（セッションの上のクラウド環境のメニュー →「編集」）の「環境変数」の欄に、"
                         f"{name}=（キー） の1行で入れてください（手順：docs/EARS_SETUP.md）。")
    _LOADED_SECRETS.append(value)
    say(f"キー {name} を読みました")
    return value


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


def is_unavailable(exc: BaseException) -> bool:
    """相手の側が一時的に混んでいる（503 UNAVAILABLE）か。⛔ 1日・1分の上限（429 RESOURCE_EXHAUSTED）は含めない（上限は待っても
    その日のうちは戻らない。上限をやり直しで回り込まない）。"""
    return str(getattr(exc, "code", "")) == "503" or getattr(exc, "status", "") == "UNAVAILABLE"


def is_quota(exc: BaseException) -> bool:
    """1日・1分の上限（429 RESOURCE_EXHAUSTED）に当たったか。会話のモデル（つなぎっぱなしの接続）では、閉じる番号 1011 と
    「You exceeded your current quota」の文面で返る（2026-10-17：6つの声を同じキーで同時に流した G-14 --ear amivoice）。"""
    return (str(getattr(exc, "code", "")) == "429" or getattr(exc, "status", "") == "RESOURCE_EXHAUSTED" or "RESOURCE_EXHAUSTED" in str(exc)
            or "exceeded your current quota" in str(exc))


def is_denied(exc: BaseException) -> bool:
    """キーのプロジェクトそのものが止められているか（403 PERMISSION_DENIED。「Your project has been denied access」）。
    2026-10-16：GEMINI_API_KEY_10 で、文字のモデルがこの答えだった（上限ではない＝待っても戻らない）。"""
    # 会話のモデル（つなぎっぱなしの接続）では、閉じる番号 1008 と同じ文面で返る（2026-10-17：GEMINI_API_KEY_15）。
    # ⛔ 番号 1008 だけでは見分けない：つなぎの時間切れ（「…GoAway signal once the session duration…」）も 1008 で返る（2026-10-17 G-15）
    return (str(getattr(exc, "code", "")) == "403" or getattr(exc, "status", "") == "PERMISSION_DENIED"
            or "denied access" in str(exc))


def is_daily(exc: BaseException) -> bool:
    """1日の上限か（上限の知らせの中の上限の名前に「PerDay」が入る。1分の上限は「PerMinute」）。
    ⛔ 2026-10-17 G-53：待つ秒の書かれていない上限の知らせを1日の上限と決めつけ、同時に流した判定で、使えるキー11本をすべて
       「今日はもう使えない」と記録した（実際は1分の上限＝35秒ほどで戻る。そのあと同じキーで続けて41回通った）。"""
    return "PerDay" in str(exc)


def retry_after_s(exc: BaseException) -> float | None:
    """上限の誤りに書いてある「あと何秒で使えるか」（無ければ None）。1分の上限（数十秒）と1日の上限（数時間）を見分けるのに使う。"""
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", str(exc))
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))


class KeyUnusable(RuntimeError):
    """KeyRing.call(rotate=False) の、そのキーが使えない（止められている・1日の上限・1分の上限が続いた）。"""


class KeyRing:
    """キーを順に使う（文字のモデルの問い合わせ）。
    ✅ 2026-10-12 ユーザー：「どのキーを使っても大丈夫です」。⛔ 2026-10-16 ユーザー：「キーはどれを使ってもいいと言ってるよ。制限はあなたが
       勝手に決めただけじゃないですか」＝私（CL-1）が「1つ目のキーだけ」と決めて、上限に当たると確かめを止めていた。
    - 1分の上限（429。あと数十秒で使える）：同じキーで、言われた秒数だけ待ってから続ける。そもそも当たらないように、同じキー・同じモデルの
      問い合わせは min_interval_s 秒あける（無料枠の文字のモデルは1分に5回まで＝ユーザーが管理画面で確かめた表）。
      ⛔ 2026-10-16：3秒おきに聞いて1分の上限に当たり、1日の上限と取り違えて次のキーへ進んでいた
    - 1日の上限（429。あと数時間）：そのモデルでは、そのキーを飛ばして次のキーで続ける
    - プロジェクトが止められている（403）：そのキーを、どのモデルでも飛ばす
    ⚠️ 上限はプロジェクトごとに数えられる。同じプロジェクトのキーなら、次のキーもすぐ上限に当たる。"""

    def __init__(self, args, min_interval_s: float = 13.0, state_path: Path | None = None):
        self.keys = [PROXY_PLACEHOLDER] if getattr(args, "proxy_key", False) else load_keys(args.env, args.key_name)
        self.start = getattr(args, "key_index", 0)
        self.min_interval_s = min_interval_s
        self.spent: dict[str, set[int]] = {}   # モデル ⇒ 1日の上限に当たったキーの番号
        self.denied: set[int] = set()          # プロジェクトが止められているキーの番号
        self._clients: dict[int, object] = {}
        self._last: dict[tuple[int, str], float] = {}
        self._cool: dict[tuple[int, str], float] = {}  # 会話のモデルで上限に当たったキーの、休み明けの時刻（acall）
        # その日の状態を覚えておく（上限に当たったキーに、次の実行でまた問い合わせない）。キーの値は書かない（指紋だけ）。
        # 1日の上限は、太平洋時間の0時に戻る（Google の決まり）ので、その日付で分ける
        self.state_path = state_path if state_path is not None else (None if getattr(args, "dry_run", False) else RESULTS_DIR / "keyring_state.json")
        self._load()

    @staticmethod
    def _today() -> str:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()

    def _load(self) -> None:
        if not self.state_path or not Path(self.state_path).exists():
            return
        try:
            st = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
        except Exception:
            return
        fp = {k.fingerprint: i for i, k in enumerate(self.keys)}
        if st.get("day") == self._today():  # 止められたキーも、日が替われば確かめ直す（ユーザーが管理画面で直すことがある）
            self.denied = {fp[f] for f in st.get("denied", []) if f in fp}
            self.spent = {m: {fp[f] for f in fs if f in fp} for m, fs in st.get("spent", {}).items()}

    def _save(self) -> None:
        """その日の状態を書く。同じファイルに書くほかの KeyRing（同じ実行の会話のモデルと意味の判定・同時に流すほかの試験）の記録とは、
        合わせて書く（⛔ 2026-10-19 の見直し：後から書いたほうが、先に書いたほうの「上限」「止められた」を消していた）。"""
        if not self.state_path:
            return
        RESULTS_DIR.mkdir(exist_ok=True)
        day, denied = self._today(), {self.keys[i].fingerprint for i in self.denied}
        spent = {m: {self.keys[i].fingerprint for i in v} for m, v in self.spent.items()}
        try:
            old = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
        except Exception:
            old = {}
        if old.get("day") == day:
            denied |= set(old.get("denied", []))
            for m, fs in old.get("spent", {}).items():
                spent.setdefault(m, set()).update(fs)
        Path(self.state_path).write_text(json.dumps({"day": day, "denied": sorted(denied), "spent": {m: sorted(v) for m, v in spent.items()}},
                                                    ensure_ascii=False, indent=2), encoding="utf-8")

    def client(self, i: int | None = None):
        i = self.start if i is None else i
        if i not in self._clients:
            self._clients[i] = make_client(self.keys[i])
        return self._clients[i]

    def call(self, model: str, fn, rotate: bool = True):
        """fn(client) を、そのモデルで使えるキーで呼ぶ（混んでいる 503 は call_with_retry が待ってやり直す）。戻り値＝(結果, キーの番号)。
        rotate=False：始めのキー（key_index）だけを使う（キーごとに1本の流れで、同じ仕事の山から取って流すとき。そのキーが使えなくなったら
        ＝止められている・1日の上限、KeyUnusable を投げ、流れのほうがやめる＝ほかの流れのキーに重ならない）。1分の上限は待って続ける。"""
        import time
        for k in range(len(self.keys) if rotate else 1):
            i = (self.start + k) % len(self.keys)
            if i in self.denied or i in self.spent.get(model, set()):
                continue
            # 1分の上限で待つのは3回まで（次のキーへ進めるとき）。自分のキーだけで流すとき（rotate=False）は、1分の上限なら待ち続ける
            # （⛔ 2026-10-18：3回続いたらその流れを抜けさせていて、全部の流れが抜け、判定が1,105件残った。1日の上限ではなかった）
            for _ in range(3 if rotate else 100):
                wait = self._last.get((i, model), -1e9) + self.min_interval_s - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last[(i, model)] = time.monotonic()
                try:
                    return call_with_retry(lambda: fn(self.client(i)))[0], i
                except Exception as exc:
                    if is_denied(exc):
                        self.denied.add(i)
                        self._save()
                        say(f"{self.keys[i].label} はプロジェクトが止められています ⇒ このキーは使いません")
                        break
                    if not is_quota(exc):
                        raise
                    after = retry_after_s(exc)
                    if not is_daily(exc):  # 1日の上限と書かれていなければ、待って同じキーで続ける（秒が無ければ30秒）
                        wait = after if after is not None else 30.0
                        say(f"{self.keys[i].label} は {model} の1分の上限に当たりました ⇒ {wait:.0f}秒待って続けます")
                        time.sleep(min(wait, 90.0) + 1)
                        continue
                    self.spent.setdefault(model, set()).add(i)
                    self._save()
                    say(f"{self.keys[i].label} は {model} の1日の上限に当たりました ⇒ 次のキーで続けます")
                    break
        if not rotate:
            raise KeyUnusable(f"{self.keys[self.start].label} では {model} を使えません")
        raise RuntimeError(f"どのキーでも {model} を使えません（1日の上限・プロジェクトが止められている・1分の上限が続いた）")

    LIVE_COOL_S = 600.0     # 会話のモデルで上限に当たったキーを休ませる秒（この実行の中だけ）
    LIVE_MAX_WAIT_S = 1800.0

    async def acall(self, model: str, fn, max_wait_s: float | None = None):
        """会話のモデル（つなぎっぱなしの接続。通話の試験）の call：await fn(client) を、使えるキーで呼ぶ。戻り値＝(結果, キーの番号)。
        ⛔ 2026-10-17：通話の試験（g14_listen）は1つのキーだけでつないでいて、6つの声を同時に流すと上限に当たり、153本のうち104本が止まった。
        - プロジェクトが止められている ⇒ call と同じく、そのキーは使わない（記録する）
        - 上限 ⇒ そのキーを、この実行の中で LIVE_COOL_S 秒休ませて、次のキーで fn を初めからやり直す（会話のモデルの上限の答えには、
          あと何秒かが書かれておらず、1分の上限か1日の上限かを見分けられないので、1日ぶん止める記録はしない）。1日の上限と書いてある
          （文字のモデル）ときだけ、call と同じく、その日はそのキーを使わない
        - どのキーも休み中なら、いちばん早く空くまで待つ（合わせて max_wait_s 秒まで。既定 LIVE_MAX_WAIT_S。通話の中の判定は待たない＝0）"""
        import asyncio
        import time
        cool = self._cool
        waited = 0.0
        while True:
            for k in range(len(self.keys)):
                i = (self.start + k) % len(self.keys)
                if i in self.denied or i in self.spent.get(model, set()) or cool.get((i, model), 0) > time.monotonic():
                    continue
                try:
                    return await fn(self.client(i)), i
                except Exception as exc:
                    if is_denied(exc):
                        self.denied.add(i)
                        self._save()
                        say(f"{self.keys[i].label} はプロジェクトが止められています ⇒ このキーは使いません")
                        continue
                    if not is_quota(exc):
                        raise
                    # 待つ秒が書いてあれば、その秒だけ休ませる（文字のモデルの1分の上限。⛔ 2026-10-17：意味の判定の1分の上限にも10分休ませ、
                    # 通話の試験の153本で判定が1つも間に合わなかった）。書いていなければ、会話のモデルと同じく LIVE_COOL_S
                    # 待つ秒が無くても、1分の上限と書いてあれば（上限の名前に PerMinute）、call と同じく30秒（⛔ 2026-10-18：判定の測り直しと
                    # 同時に流した通話の中の判定が、1分の上限のたびにキーを10分休ませ、判定が通話の中に届かなかった）
                    if is_daily(exc):
                        # 1日の上限と書いてある（文字のモデル＝意味の判定）：call と同じく、その日はそのキーを使わない（記録する）。
                        # ⛔ 2026-10-19：10分休ませるだけだったので、休み明けのたびに上限のキーへ順に問い合わせ直し、1回の判定に7.9秒かかった
                        self.spent.setdefault(model, set()).add(i)
                        self._save()
                        say(f"{self.keys[i].label} は {model} の1日の上限に当たりました ⇒ 次のキーで初めからやり直します")
                        continue
                    after = retry_after_s(exc)
                    if after is None and "PerMinute" in str(exc):
                        after = 30.0
                    rest = after + 1 if after is not None else self.LIVE_COOL_S
                    cool[(i, model)] = time.monotonic() + rest
                    say(f"{self.keys[i].label} は {model} の上限に当たりました ⇒ {rest:.0f}秒休ませて、次のキーで初めからやり直します")
            ends = [t for (i, m), t in cool.items() if m == model and i not in self.denied]
            if not ends or waited >= (self.LIVE_MAX_WAIT_S if max_wait_s is None else max_wait_s):
                raise RuntimeError(f"どのキーでも {model} を使えません（上限・プロジェクトが止められている）")
            pause = max(1.0, min(ends) - time.monotonic() + 1)
            say(f"どのキーも {model} の上限で休み中 ⇒ {pause:.0f}秒待ちます")
            await asyncio.sleep(pause)
            waited += pause


def call_with_retry(fn, waits=(10, 30, 90)):
    """混んでいる（503）ときだけ、間を空けて、やり直す。戻り値＝(結果, やり直した回数)。ほかの誤りと、やり直しを使い切った誤りは、そのまま上げる。
    ⛔ 2026-10-13 G-23：やり直しが無く、3.6〜3.8 Flash の 42回中38回が 503 で「結果なし」になり、比べが成り立たなかった。"""
    import time
    for n, wait in enumerate((*waits, None)):
        try:
            return fn(), n
        except Exception as exc:
            if wait is None or not is_unavailable(exc):
                raise
            time.sleep(wait)


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
