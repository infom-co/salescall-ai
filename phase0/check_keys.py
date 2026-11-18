"""コミットの前に、送るファイルにキーの値が紛れていないかを確かめる（値は出さない。件数と、見つかったファイルの名前だけ）。
使い方：python3.13 phase0/check_keys.py（docs/team/CL-1.md 0-1 の「送る前の確かめ」。2026-11-03 に作業場所から移した＝会話ごとに作業場所が変わるため）。
キーの値＝環境変数のうち、名前に KEY・TOKEN・SECRET を含み、16字以上のもの。
確かめるファイル＝作業中の変更・追加したファイル・まだ送っていないコミットに入っているファイル（wav などの中身もバイトで見る）。"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent   # リポジトリの頭（このファイルは phase0/ にある）
values = [v for k, v in os.environ.items() if any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET")) and len(v) >= 16]


def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True).stdout.split("\n")


names = set(git("diff", "--name-only", "HEAD")) | set(git("diff", "--cached", "--name-only")) | set(git("ls-files", "--others", "--exclude-standard"))
upstream = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--abbrev-ref", "@{u}"], capture_output=True, text=True).stdout.strip()
if upstream:
    names |= set(git("diff", "--name-only", f"{upstream}..HEAD"))
names = sorted(n for n in names if n and (REPO / n).is_file())
found = []
for n in names:
    data = (REPO / n).read_bytes()
    if any(v.encode() in data for v in values):
        found.append(n)
msgs = "\n".join(git("log", "--format=%B", f"{upstream}..HEAD")) if upstream else ""
if any(v in msgs for v in values):
    found.append("（コミットの説明文）")
print(f"確かめたファイル {len(names)}・キーの値 {len(values)} 個・見つかった {len(found)}")
for n in found:
    print("  ⛔ キーの値が入っている：", n)
