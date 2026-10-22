"""移る間の形の、この機械の PC の部品の通行証（DESIGN.md 5-5）。持ち主 CL-2。

サーバーができるまでは、管理者の PC で中央の見守り役と PC の部品を両方動かす（つなぎ口は、この機械の中だけで待つ＝
`config.Config.local_only`）。そのあいだ、PC の部品の通行証は、**見守り役が自分で払い出して、この機械にしまう**（管理者は命令を
打たない。2026-10-22 ユーザー：「自動にすべきなんじゃないの？人が介在しない仕組みと、面倒がかからない形にするように」）。
- 契約者は、管理者自身の契約者（`SALESCALL_ADMIN_TENANT`）。
- しまった通行証が無い・止められている・別の契約者のもの なら、払い出し直す（前の通行証は止める）。見守り役は動いているあいだ、
  決まった間隔でこれを確かめる（PC の部品が、止められた通行証を消したときも、すぐ入れ直す）。
- PC の部品は、ログインの入口（https。CL-3）が無いあいだ、通行証がしまわれるのを待つ（`agent.pc_login.until_logged_in`）。
- サーバーへ移ったあと（ほかの機械の PC がつなぐ形）は、何もしない（契約者の PC は、ブラウザでログインして通行証を受け取る）。
- 通行証の値は、画面にも記録にも出さない。しまう場所と形は `link.pass_file` の1か所（PC の部品と同じ）。
"""

from __future__ import annotations

import socket

from salescall.core import agents
from salescall.core.access import Access
from salescall.link import pass_file
from salescall.supervisor import ACTOR
from salescall.supervisor.config import Config

CHECK_EVERY = 30.0   # 見守り役が、この機械の通行証を確かめる間隔（秒）


def ensure(app: Access, cfg: Config) -> bool:
    """移る間の形なら、この機械の PC の部品に、使える通行証があるようにする。新しく入れたら True。"""
    if not cfg.local_only or cfg.admin_tenant is None:
        return False
    try:
        kept = pass_file.load()
    except Exception:  # noqa: BLE001 読めない通行証（壊れた・Windows の利用者が違う）は、無いものとして入れ直す
        kept = None
    mine = kept is not None and kept.tenant_id == cfg.admin_tenant
    if mine and app.in_system(ACTOR, lambda tx: agents.identify(tx, kept.token)) is not None:
        return False
    got = agents.issue(app, ACTOR, cfg.admin_tenant, label=socket.gethostname(), replaces=kept.pc_id if mine else None)
    pass_file.save(got)
    return True
