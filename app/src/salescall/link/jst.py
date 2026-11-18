"""日本時間（サーバーと PC の部品の両方が使う。**決まりはこの1か所**）。持ち主 CL-2。

夏時間は無い。データベースの salescall.jst・from_jst と同じ答え。サーバーの置き場の時間帯に関係なく、日本時間で決める。
"""

from __future__ import annotations

from datetime import timedelta, timezone

JST = timezone(timedelta(hours=9))
