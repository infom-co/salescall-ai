"""K-3：認証エラーとクォータ超過を見分けられるか（のうち、認証エラー側）。

やること：わざと無効なキーで1回だけ接続し、返ってくるエラーの種類・番号・文面を記録する。
⇒ 公式のエラー一覧と突き合わせ、DESIGN.md 9章の「キー固有」に当たる見分け方を決める。
※ クォータ超過は、わざと起こすと料金や利用制限がかかるので起こさない。公式の資料で確かめる。
"""
from __future__ import annotations

import asyncio

from common import ApiKey, base_parser, describe_error, make_client, require_python, save_result, say
from live_util import build_config, collect_turn, send_text

FAKE = ApiKey("INVALID_FOR_TEST", "AIza" + "0" * 35)


async def run(args) -> dict:
    cfg = build_config(voice=None, system_instruction="接続の試験です。")
    if args.dry_run:
        return {"dry_run": True, "config_ok": True}
    client = make_client(FAKE)
    try:
        async with client.aio.live.connect(model=args.model, config=cfg) as session:
            await send_text(session, "試験です。")
            await collect_turn(session, started_at=0.0, timeout_s=20)
        return {"unexpected": "無効なキーで接続できてしまった"}
    except Exception as exc:
        return {"error": describe_error(exc)}


def main() -> None:
    require_python()
    args = base_parser(__doc__).parse_args()
    result = asyncio.run(run(args))
    say("記録したエラー：", result.get("error", result))
    save_result("K-3", {"model": args.model, **result})


if __name__ == "__main__":
    main()
