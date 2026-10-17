"""ReazonSpeech（PC の中で動く日本語専門の耳。ears.ReazonEar）のモデルを取る（1回だけ）。置き場＝models/reazonspeech/（GitHub には上げない）。

取り先：Hugging Face の reazon-research/reazonspeech-k2-v2（sherpa-onnx で動く形。エンコーダ・デコーダ・ジョイナ・言葉の一覧）。
⚠️ 2026-10-17：この作業環境では huggingface.co が通信の決まりで止められている ⇒ ユーザーが作業環境の設定で許可を足してから流す。
ファイルの名前は決め打ちにせず、Hugging Face のファイル一覧から選ぶ（名前が変わっても取れるように）。int8（小さく速い形）は --int8 のときだけ。
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from ears import REAZON_DIR

REPO = "reazon-research/reazonspeech-k2-v2"


def files(repo: str) -> list[str]:
    with urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}", timeout=60) as r:
        return [s["rfilename"] for s in json.load(r).get("siblings", [])]


def pick(names: list[str], int8: bool) -> list[str]:
    out = [n for n in names if n.endswith("tokens.txt")]
    for kind in ("encoder", "decoder", "joiner"):
        cands = [n for n in names if Path(n).name.startswith(kind) and n.endswith(".onnx") and (("int8" in n) == int8)]
        if not cands:
            raise SystemExit(f"{kind} のファイルが見つかりません（一覧：{names[:20]}）")
        out.append(sorted(cands)[0])
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=REPO)
    p.add_argument("--int8", action="store_true")
    args = p.parse_args()
    REAZON_DIR.mkdir(parents=True, exist_ok=True)
    for name in pick(files(args.repo), args.int8):
        dst = REAZON_DIR / Path(name).name
        if dst.exists():
            print(f"あります：{dst.name}")
            continue
        print(f"取っています：{name}")
        urllib.request.urlretrieve(f"https://huggingface.co/{args.repo}/resolve/main/{name}", dst)
    print(f"置き場：{REAZON_DIR}")


if __name__ == "__main__":
    main()
