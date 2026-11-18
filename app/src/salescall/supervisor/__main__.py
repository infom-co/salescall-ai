"""中央の見守り役の命令（サーバーで使う。手順は app/README.md）。持ち主 CL-2。

- `python -m salescall.supervisor run`：中央の見守り役を動かす（PC の部品のつなぎ口も開く＝SALESCALL_LISTEN）。
- `python -m salescall.supervisor status`：どの設定が入っているか（値は出さない）。
- `python -m salescall.supervisor drain`：同じ機械で動いている見守り役に「新しい電話をかけずに、いまの通話が終わったら止まって」と
  頼み、止まるまで待って知らせる（入れ替えの前に使う。`main.ask_drain`）。
契約者の PC で動かすのは、PC の部品（`python -m salescall.agent`）。
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

from salescall.supervisor import config, main


RESTART_WAIT = 30   # 思いがけない誤りのあと、動き直すまでの長さ（秒）
# 見守り役の流れの名前の頭（決まった間隔の仕事・控え・回線ごとの流れ・つなぎ口。main.py・channel.py の名前と同じ）。
# データベースのつなぎの流れ（salescall-worker など）は数えない（処理の終わりを止めないため）
FLOW_PREFIXES = ("salescall-job", "salescall-backup", "salescall-line-", "salescall-channel")


def left_over() -> list[str]:
    """前の回の見守り役の流れのうち、まだ生きているもの（固まった仕事・止まらない回線の流れなど）の名前。"""
    return sorted(t.name for t in threading.enumerate() if t.is_alive() and t.name.startswith(FLOW_PREFIXES))


def _end(code: int) -> int:
    """終わるとき：前の回の流れが残っていれば、待たずに処理を終える（固まった仕事を Python が待ち続け、処理が終わらないため。
    ⛔ 2026-10-22 の見直し13）。残っていなければ、ふつうに終わる。"""
    if left_over():
        sys.stdout.flush()
        os._exit(code)
    return code


def _stop_on_term() -> threading.Event:
    """終わりの合図（SIGTERM。Fly.io の入れ替え・OS の終わり）を、Ctrl+C と同じ止め方にする（1か所）：新しい発信を止め、いまの
    通話の終わりを待ってから、回線を返して止まる（`main.serve` の止める印）。⛔ 2026-10-22 の見直し14：前は受け取らず、入れ替えの
    たびに、話している通話がいきなり切れうった（本番でいきなり止めない＝2026-10-22 ユーザー）。"""
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    return stop


def run(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    if command == "status":
        print("\n".join(config.status()))
        return 0
    if command == "drain":
        print("新しい電話を止めて、いまの通話の終わりを待っています（話している通話は、終わるまで待ちます。固まった通話は、"
              "おわびして終えます）…", flush=True)
        done = main.ask_drain()
        if done is None:
            print("この機械では、見守り役は動いていません")
            return 0
        if done:
            print("見守り役が止まりました。入れ替えてかまいません")
            return 0
        print("待っても止まりません（通話が終わらない・固まっている）。入れ替えると、まだ話している通話はおわびの録音で切れます")
        return 1
    if command == "run":
        stop = _stop_on_term()
        while True:
            try:
                main.serve(config.load(), stop)
                return _end(0)
            except (config.ConfigMissing, main.AlreadyRunning) as e:
                print(str(e))
                return _end(1)
            except KeyboardInterrupt:
                return _end(0)
            except Exception as e:  # noqa: BLE001 思いがけない誤りでも、見守り役を終わらせない（間をあけて動き直す＝PC の部品と同じ）
                if left_over():   # 前の回の流れが残っている：同じ処理の中で動き直すと、まだ通話中の流れの回線を後始末して二重に
                    #               立ち上げうる（⛔ 2026-10-22 の見直し13）⇒ 処理を終え、動かす所の仕組み（タスク スケジューラ・
                    #               Fly.io）に立ち上げ直させる
                    print(f"思いがけない誤り（{type(e).__name__}）。前の回の流れが残っているので終わります（自動で立ち上げ直されます）")
                    return _end(1)
                print(f"思いがけない誤り（{type(e).__name__}）。しばらくして動き直します")
                time.sleep(RESTART_WAIT)
    print("使い方：python -m salescall.supervisor [run | status | drain]")
    return 2


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
