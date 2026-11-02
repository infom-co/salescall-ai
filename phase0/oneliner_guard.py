"""道具を呼ぶ前と返事を終える時に、こちらが直前に書いた一言が英語かを見て、英語なら止める見張り（Claude Code の PreToolUse と Stop の hook）。
⛔ 2026-11-03 日本時間1時22分51秒（56回目）：道具を呼ばずに終える返事を英語で書き、道具を呼ぶ前にしか見ない見張りでは止まらなかった
   ⇒ 返事を終える時（Stop）も同じ決まりで見る（止めたら、日本語で書き直すまで返事を終えられない。書き直しのあとは二度は止めない）。

✅ 2026-11-02 日本時間7時12分 ユーザー：「(b) OK」＝英語の一言を仕組みで止める見張りを、この作業場所の設定に入れてよい。
⛔ 作業の途中の一言を英語で書いた失敗が43回になった（docs/team/CL-1.md 0-1）。決まりを書き足すだけでは、8分後に同じ所で破った。
英語の見分け方は read_user_messages.english の1か所（読み直しで数える道具と同じ決まり）。

使い方：
- hook として：標準入力に hook の JSON（transcript_path＝会話の記録）。最近の段（直近 BOUNDARIES 回の人の発言・道具の結果まで・
  MINUTES 分以内）にこちらが書いた文の中に、まだ止めていない英語の一言があれば、終了コード2で理由を返す（その道具は呼ばれない。理由は
  こちらに届く）。止めた文は覚えておき（STATE）、二度は止めない。それ以外は何もしない（終了コード0）。
  ⛔ 2026-11-02 日本時間7時23分に確かめて分かったこと：記録への書き込みは道具の呼び出しより少し遅れる＝見張りが動いた時点では、いまの
  一言がまだ記録に無いことがある（「直前の段だけ」を読む形では、試した2回とも空だった）。⇒ 最近の段まで読み、遅くとも次の道具の前に止める。
- python3.13 oneliner_guard.py --install：このコンテナのユーザー設定（~/.claude/settings.json）に hook を足す（ほかの設定は残す・
  同じ hook が既にあれば足さない）。⚠️ コンテナが替わると消えるので、新しい作業場所では最初に1回流す（CL-1.md 0-1）。
⚠️ 見られるのは会話の記録に残った一言だけ。画面に出した一言が記録に残らず、言い回しの違う要約（考えの要約）が残ることがある
  （2026-11-02 日本時間7時20分ごろ、この続きの一言の一部で見つけた）＝その一言は、この見張りでも読み直しの数えでも見えない。
- 見張りの中で何かが壊れても、作業は止めない（終了コード0）。手伝いの係（サブエージェント）の道具は見ない（画面に出る文ではない）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from read_user_messages import english  # noqa: E402

SETTINGS = Path.home() / ".claude" / "settings.json"
import os
# 止めた文の印（同じ文で二度は止めない）。試すときは ONELINER_GUARD_STATE で別の場所にする（本物の印を汚さない）
STATE = Path(os.environ.get("ONELINER_GUARD_STATE") or Path.home() / ".claude" / "oneliner_guard_seen.json")
TAIL_BYTES = 2_000_000   # 記録の末尾だけを読む（記録は数十MBになる。いまの返事の文は末尾にある）
BOUNDARIES = 3           # さかのぼる段（人の発言・道具の結果の行）の数
MINUTES = 15             # さかのぼる時間（入れる前に書いた古い英語の一言では止めない）


def recent_texts(path: str | Path, boundaries: int = BOUNDARIES, minutes: float = MINUTES,
                 tail_bytes: int = TAIL_BYTES) -> list[str]:
    """会話の記録の末尾から、最近の段（直近 boundaries 回の人の発言・道具の結果＝記録の type が user の行まで・minutes 分以内）に、
    こちらが書いた文（古い順）。"""
    import datetime as dt
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        start = max(0, size - tail_bytes)
        f.seek(start)
        lines = f.read().decode("utf-8", "replace").splitlines()
    if start:
        lines = lines[1:]   # 途中から読んだ最初の行は切れている
    texts: list[str] = []
    newest, seen = None, 0
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        try:
            at = dt.datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
        except ValueError:
            at = None
        if at is not None:
            newest = newest or at
            if (newest - at).total_seconds() > minutes * 60:
                break
        if row.get("type") == "user":
            seen += 1
            if seen > boundaries:
                break
            continue
        if row.get("type") != "assistant":
            continue
        for block in reversed((row.get("message") or {}).get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text") or "")
    return list(reversed(texts))


def verdict(hook: dict) -> str | None:
    """止める理由（止めないなら None）。"""
    if hook.get("agent_id"):
        return None   # 手伝いの係の道具（画面に出る文ではない）
    stop = hook.get("hook_event_name") == "Stop"
    if stop and hook.get("stop_hook_active"):
        return None   # 止めたあとの書き直しの返事（同じ所で止め続けない）
    import hashlib
    done = set(json.loads(STATE.read_text(encoding="utf-8"))) if STATE.exists() else set()
    texts = recent_texts(hook["transcript_path"]) + [t for t in [hook.get("last_assistant_message")] if isinstance(t, str) and t]
    bad = [t for t in dict.fromkeys(texts) if english(t) and hashlib.sha1(t.encode("utf-8")).hexdigest() not in done]
    if not bad:
        return None
    done |= {hashlib.sha1(t.encode("utf-8")).hexdigest() for t in bad}
    STATE.write_text(json.dumps(sorted(done)), encoding="utf-8")
    said = "」「".join(t.strip()[:100] for t in bad)
    if stop:
        return (f"⛔ 最近の返事が英語です（「{said}」）。日本語で書き直し、帳面（docs/team/CL-1.md 0-1）の英語の失敗に記録してから、"
                "返事を終えてください。")
    return (f"⛔ 最近の一言が英語です（「{said}」）。この道具は呼んでいません。日本語の一言で書き直し、"
            "帳面（docs/team/CL-1.md 0-1）の英語の失敗に記録してから、もう一度呼んでください。")


EVENTS = {"PreToolUse": {"matcher": "*"}, "Stop": {}}   # 見張る時（道具を呼ぶ前・返事を終える時）


def install(settings_path: Path | None = None) -> None:
    """ユーザー設定に hook を足す（ほかの設定は残す・同じものがあれば足さない）。"""
    path = settings_path or SETTINGS
    command = f"{sys.executable} {Path(__file__).resolve()}"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    added = []
    for event, extra in EVENTS.items():
        groups = settings.setdefault("hooks", {}).setdefault(event, [])
        if not any(h.get("command") == command for group in groups for h in group.get("hooks") or []):
            groups.append({**extra, "hooks": [{"type": "command", "command": command, "timeout": 10}]})
            added.append(event)
    if not added:
        print(f"もう入っています：{path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"入れました（{'・'.join(added)}）：{path}")


def main() -> None:
    if "--install" in sys.argv[1:]:
        install()
        return
    try:
        reason = verdict(json.load(sys.stdin))
    except Exception:   # noqa: BLE001  見張りの不具合で作業を止めない
        return
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
