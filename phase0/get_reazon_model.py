"""ReazonSpeech（PC の中で動く日本語専門の耳。ears.ReazonEar）のモデルを取る。置き場＝models/reazonspeech/（GitHub には上げない）。

取り先：sherpa-onnx（ReazonSpeech を動かす部品）が公開している、同じモデル（reazon-research/reazonspeech-k2-v2）を sherpa-onnx で動く形に
まとめたファイル（GitHub の公開ファイル）。中身の説明書きに「reazon-research/reazonspeech-k2-v2 から」とある。
✅ 2026-10-17：この置き場は、いまの作業環境の通信の決まりのまま取れた（Hugging Face は止められている ⇒ ユーザーの設定が1つ減った）。
作業環境は使うたびに作り直されるので、モデルが無ければ ears_try.py がこれを呼んで取り直す。
- 取るのは、エンコーダ・デコーダ・ジョイナ（ふつうの形と int8 の形）と言葉の一覧だけ。まとめたファイルの中の、それ以外のもの（試しの声など）は取り出さない。
- まとめたファイルは外から来たものなので、中の名前を確かめてから取り出す（置き場の外を指す名前・ふつうのファイルでないものは取り出さない）。
"""
from __future__ import annotations

import argparse
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from ears import REAZON_DIR

URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01.tar.bz2"
KINDS = ("encoder", "decoder", "joiner")


def wanted(name: str) -> str | None:
    """まとめたファイルの中の1つの名前から、取り出すなら置き場での名前を返す（取り出さないなら None）。"""
    parts = Path(name).parts
    if len(parts) != 2 or ".." in parts or parts[0].startswith("/"):
        return None
    base = parts[1]
    if base == "tokens.txt" or (base.endswith(".onnx") and base.startswith(KINDS)):
        return base
    return None


def have(d: Path = REAZON_DIR) -> bool:
    return (d / "tokens.txt").exists() and all(any(d.glob(f"{k}*.onnx")) for k in KINDS)


def fetch(url: str = URL, d: Path = REAZON_DIR) -> list[str]:
    """取り出したファイルの名前の一覧。"""
    d.mkdir(parents=True, exist_ok=True)
    got = []
    with tempfile.TemporaryDirectory() as tmp:
        arc = Path(tmp) / "model.tar.bz2"
        print(f"取っています：{url}（約700MB）")
        urllib.request.urlretrieve(url, arc)
        with tarfile.open(arc, "r:bz2") as t:
            for m in t.getmembers():
                base = wanted(m.name)
                if not base or not m.isfile():
                    continue
                with t.extractfile(m) as src, open(d / base, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                got.append(base)
    if not have(d):
        raise SystemExit(f"モデルのファイルがそろいませんでした（取り出したもの：{got}）")
    return got


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default=URL)
    p.add_argument("--again", action="store_true", help="あっても取り直す")
    args = p.parse_args()
    if have() and not args.again:
        print(f"もうあります：{REAZON_DIR}")
        return
    print("取り出したもの：", fetch(args.url))
    print(f"置き場：{REAZON_DIR}")


if __name__ == "__main__":
    main()
