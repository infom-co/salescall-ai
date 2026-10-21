"""控えのファイルの置き方の決まり（DESIGN.md 3-10・5-5）。持ち主 CL-2。

サーバー（名前を付ける・片付けの決まりを渡す）と PC の部品（名前とフォルダを確かめて書く・片付ける）の両方が使う。**名前・フォルダ・
名前の日時の決まりは、この1か所**（両方で別々に書くと、片方だけ直したときに、サーバーが付けた名前を PC が断る・消さない）。

ハードディスクの中の形：<ハードディスク>\\salescall-backup\\<依頼者のフォルダ>\\AI・submission・recording ／ データベース全体は
<ハードディスク>\\salescall-backup\\system\\AI（2026-10-17 ユーザー：「E:\\salescall-backup\\client1\\AI」）。
"""

from __future__ import annotations

import re
from datetime import datetime

from salescall.link.jst import JST

BASE = "salescall-backup"
DRIVE_MARK = "salescall-backup-drive.txt"   # ハードディスクの一番上に置く印（無ければ「つながっていない」として書かない）
AI, SUBMISSION, RECORDING = "AI", "submission", "recording"
KINDS = (AI, SUBMISSION, RECORDING)
WHOLE = "system"                            # データベース全体の控えのフォルダ（依頼者のフォルダには使えない名前）
PART = ".part"                              # 書きかけの名前の終わり（書き終えてから名前を付ける）

_NAME = re.compile(r"^salescall-[0-9A-Za-z._-]{1,200}\.enc$")          # サーバーが付ける名前（ほかの名前は書かない）
_FOLDER = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f.][^\\/:*?\"<>|\x00-\x1f]{0,59}$")   # Windows で使える・上へ出ない
_DATED = re.compile(r"^salescall-(\d{8})-(\d{6})[.-].*\.enc$")          # 片付けは、名前の日時だけを見る
_HELD = re.compile(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-")


def file_name(started: datetime, rest: str) -> str:
    """控えのファイルの名前（日本時間の日時つき。PC の片付けが、名前の日時で決める）。rest＝その後ろ（例「.ai.enc」）。"""
    return f"salescall-{started.astimezone(JST):%Y%m%d-%H%M%S}{rest}"


def good_name(name: str) -> bool:
    return bool(_NAME.match(name))


def good_folder(folder: list[str]) -> bool:
    """書いてよいフォルダか（依頼者のフォルダ・データベース全体のフォルダ ＋ 決まった種類）。"""
    return len(folder) == 2 and bool(_FOLDER.match(folder[0])) and folder[1] in KINDS


def taken_at(name: str) -> datetime | None:
    """名前の日時（日本時間）。名前に日時の無いファイルは None（片付けで消さない）。"""
    m = _DATED.match(name)
    return None if m is None else datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=JST)


def held_call(name: str) -> str | None:
    """録音の名前の通話の番号（測り終わるまで消さない通話か、を見る）。"""
    m = _HELD.search(name)
    return None if m is None else m.group(1)
