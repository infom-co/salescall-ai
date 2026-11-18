"""コミットの前に、作業中の変更で消した文（句点で区切った8字より長いもの）のうち、新しい中身に無いものを出す（意図しない消しの見張り）。
使い方：リポジトリの頭で python3.13 phase0/check_removed.py（docs/team/CL-1.md 0-1 の「送る前の確かめ」。出た文が、どれも意図した直し・置き換えかを
確かめてから git add する。2026-11-03 に作業場所から移した＝会話ごとに作業場所が変わるため）。"""
import subprocess, re
files = subprocess.run(["git","diff","--name-only"],capture_output=True,text=True).stdout.split()
for f in files:
    d = subprocess.run(["git","diff","-U0","--",f],capture_output=True,text=True).stdout
    removed = "\n".join(l[1:] for l in d.split("\n") if l.startswith("-") and not l.startswith("---"))
    new = open(f,encoding="utf-8").read()
    norm = lambda t: re.sub(r"[\s~#\"+()]", "", t)
    nn = norm(new)
    miss = [x.strip() for x in re.split("。", removed) if len(x.strip()) > 8 and norm(x) not in nn]
    print(f, len(miss))
    for m in miss: print("   ", m[:160])
