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

要約の区切りの見張り（同じ hook の1か所。英語の一言より先に見る）：
✅ 2026-11-05 日本時間6時51分20秒 ユーザー：「OK、見張りを入れて進めてください」（お勧め：要約が入ったら、要約の時刻を出す道具を呼ぶまで、ほかの道具を
   止める見張り）。⛔ 要約のあとに帳面 0-1 を通さずに作業へ戻った失敗が18回（docs/team/CL-1.md 0-1）＝決まりを書くだけでは止まらなかった。
- 会話の記録の末尾に、まだ済んでいない要約の区切り（compact_boundary）があれば、`--after-compact` の命令のほかは、どの道具も返事の終わりも止める
  （止めたあとの書き直しの返事・手伝いの係の道具は止めない）。
- python3.13 oneliner_guard.py --after-compact [作業場所のフォルダ]：区切りの時刻（日本時間）・前の区切りから今の区切りまでのご発言と返事・
  区切りのあとのご発言・前の区切りからの英語の一言・本書（Intent.md）を切らずに読めるファイルを出し、その区切りを済みにする（0-1 の頭の手順を
  機械でする1か所。読むのは CL-1）。どの記録の区切りかは、止めたときに見張りが覚えておく（COMPACT_STATE）。
- 英語の一言の見張りは、要約の区切りより前の文を見ない（区切りより前の英語の一言は、--after-compact の英語の数えの1か所で出す）。
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
# 済んだ要約の区切りと、止めたときの記録（--after-compact がどの記録を読むか）。試すときは COMPACT_GUARD_STATE で別の場所にする
COMPACT_STATE = Path(os.environ.get("COMPACT_GUARD_STATE") or Path.home() / ".claude" / "compact_guard.json")
AFTER = "--after-compact"
INTENT = HERE.parent / "Intent.md"
READ_SIZE, READ_FOLD = 16000, 1500   # 本書を読むファイルの大きさ（読む道具で切れない）・長い行を折り返す字数


def _tail_rows(path: str | Path, tail_bytes: int = TAIL_BYTES) -> list[dict]:
    """会話の記録の末尾の行（古い順）。記録は数十MBになる＝いまの返事・最後の要約の区切りは末尾にある。"""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        start = max(0, size - tail_bytes)
        f.seek(start)
        lines = f.read().decode("utf-8", "replace").splitlines()
    if start:
        lines = lines[1:]   # 途中から読んだ最初の行は切れている
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _is_boundary(row: dict) -> bool:
    return row.get("type") == "system" and row.get("subtype") == "compact_boundary"


def recent_texts(path: str | Path, boundaries: int = BOUNDARIES, minutes: float = MINUTES,
                 tail_bytes: int = TAIL_BYTES, rows: list[dict] | None = None) -> list[str]:
    """会話の記録の末尾から、最近の段（直近 boundaries 回の人の発言・道具の結果＝記録の type が user の行まで・minutes 分以内）に、
    こちらが書いた文（古い順）。"""
    import datetime as dt
    texts: list[str] = []
    newest, seen = None, 0
    for row in reversed(rows if rows is not None else _tail_rows(path, tail_bytes)):
        if _is_boundary(row):
            break   # 要約の区切りより前は見ない（--after-compact の英語の数えの1か所で出す）
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


def jst(ts: str) -> str:
    """記録の時刻（世界標準時）を日本時間の「11-05 6時36分06秒」に。"""
    import datetime as dt
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=9)))
    return f"{t.month:02d}-{t.day:02d} {t.hour}時{t.minute:02d}分{t.second:02d}秒"


def _load_compact() -> dict:
    st = json.loads(COMPACT_STATE.read_text(encoding="utf-8")) if COMPACT_STATE.exists() else {}
    return {"done": list(st.get("done") or []), "pending": st.get("pending")}


def _save_compact(st: dict) -> None:
    COMPACT_STATE.parent.mkdir(parents=True, exist_ok=True)
    COMPACT_STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def compact_verdict(hook: dict, rows: list[dict] | None = None) -> str | None:
    """まだ済んでいない要約の区切りがあれば止める理由（--after-compact の命令・止めたあとの書き直し・手伝いの係は止めない）。"""
    if hook.get("agent_id"):
        return None
    stop = hook.get("hook_event_name") == "Stop"
    if stop and hook.get("stop_hook_active"):
        return None
    marks = [r.get("timestamp", "") for r in (rows if rows is not None else _tail_rows(hook["transcript_path"])) if _is_boundary(r)]
    if not marks:
        return None
    st = _load_compact()
    if marks[-1] in st["done"]:
        return None
    st["pending"] = {"transcript": str(hook["transcript_path"]), "boundary": marks[-1]}
    _save_compact(st)
    command = str((hook.get("tool_input") or {}).get("command", ""))
    if not stop and hook.get("tool_name") == "Bash" and AFTER in command and Path(__file__).name in command:
        return None
    how = (f"最初に `{sys.executable} {Path(__file__).resolve()} {AFTER} <作業場所のフォルダ>` を呼んでください（区切りの時刻・前の区切りからの"
           "ご発言と返事・英語の一言・本書を切らずに読むファイルを出します）。そのあと帳面 0-1 を上から順に通し、本書を全部読み終えるまで、"
           "作り・帳面・本書を書き換えないでください。")
    if stop:
        return f"⛔ 要約の区切り（日本時間{jst(marks[-1])}）のあと、まだ帳面 0-1 を始めていません。返事は終えていません。{how}"
    return f"⛔ 要約の区切り（日本時間{jst(marks[-1])}）のあと、まだ帳面 0-1 を始めていません。この道具は呼んでいません。{how}"


def split_for_reading(src: Path, out: Path, size: int = READ_SIZE, fold: int = READ_FOLD) -> tuple[list[Path], int]:
    """本書を、読む道具で切れない大きさのファイルに分ける（行の頭に行の番号。長い行は fold 字ずつ折り返し、折り返しの行は「番号+:」。
    ⛔ 2026-11-05：行ごとに分けていたので、3万字を超える1行が1つのファイルに入り、読む道具が途中で切った＝折り返した切れごとに分ける）。"""
    lines = src.read_text(encoding="utf-8").split("\n")
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("p*.txt"):
        old.unlink()
    files, cur, used = [], [], 0
    for i, line in enumerate(lines, 1):
        for k in range(0, max(len(line), 1), fold):
            piece = f"{i}{'+' if k else ''}: {line[k:k + fold]}"
            if cur and used + len(piece) + 1 > size:
                files.append(cur)
                cur, used = [], 0
            cur.append(piece)
            used += len(piece) + 1
    if cur:
        files.append(cur)
    paths = []
    for n, part in enumerate(files, 1):
        p = out / f"p{n:02d}.txt"
        p.write_text("\n".join(part), encoding="utf-8")
        paths.append(p)
    return paths, len(lines)


def after_compact(out_dir: Path | None = None) -> str:
    """止めた要約の区切りについて、0-1 の頭の手順を機械でして（区切りの時刻・ご発言と返事・英語の一言・本書を読むファイル）、その区切りを済みにする。"""
    from read_user_messages import ME, _rows, english as is_english
    st = _load_compact()
    pend = st.get("pending")
    if not pend:
        return "止められている要約の区切りはありません（何もしていません）。"
    path, b = Path(pend["transcript"]), pend["boundary"]
    marks = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if "compact_boundary" in line:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if _is_boundary(row):
                    marks.append(row.get("timestamp", ""))
    prev = max((m for m in marks if m < b), default="")
    out = out_dir or Path.home() / ".claude" / "after_compact" / b.replace(":", "")
    out.mkdir(parents=True, exist_ok=True)

    def write(name: str, rows: list) -> tuple[Path, int]:
        p = out / name
        p.write_text("\n\n------\n".join(f"[日本時間{jst(ts)}] {who}\n{text}" for ts, who, text in rows), encoding="utf-8")
        return p, len(rows)
    seg = write("seg.txt", _rows(path, prev, b, with_assistant=True))
    after = write("after.txt", _rows(path, b, ""))
    eng = write("english.txt", [r for r in _rows(path, prev, "", with_assistant=True) if r[1] == ME and is_english(r[2])])
    parts, total = split_for_reading(INTENT, out / "intent")
    st["done"].append(b)
    st["pending"] = None
    _save_compact(st)
    return "\n".join([
        f"要約の区切り：日本時間{jst(b)}（前の区切り：{('日本時間' + jst(prev)) if prev else '無し'}）。この区切りを済みにしました。",
        "帳面 0-1 を上から順に：",
        f"(1) 前の区切りから今の区切りまでのご発言と私の返事（{seg[1]}件）：{seg[0]} を全部読む",
        f"(2) 今の区切りのあとのご発言（{after[1]}件）：{after[0]}",
        f"(3) 前の区切りからの英語の一言（{eng[1]}件）：{eng[0]} ＝帳面 0-1 に記録が無いものを記録する",
        f"(4) 本書 Intent.md の1〜{total}行（{len(parts)}のファイル・長い行は{READ_FOLD}字ずつ折り返し）：{parts[0].parent}/p01.txt〜"
        f"p{len(parts):02d}.txt を1つずつ切らずに読む。読み終えるまで、作り・帳面・本書を書き換えない",
        "(5) 読んだら、要約の前後のご決定が本書・設計書・帳面に入っているかを1つずつ突き合わせる",
    ])


def verdict(hook: dict) -> str | None:
    """止める理由（止めないなら None）。要約の区切りを先に見る（記録の末尾は1回だけ読み、2つの見張りで使う）。"""
    rows = _tail_rows(hook["transcript_path"])
    reason = compact_verdict(hook, rows)
    if reason:
        return reason
    if hook.get("agent_id"):
        return None   # 手伝いの係の道具（画面に出る文ではない）
    stop = hook.get("hook_event_name") == "Stop"
    if stop and hook.get("stop_hook_active"):
        return None   # 止めたあとの書き直しの返事（同じ所で止め続けない）
    import hashlib
    done = set(json.loads(STATE.read_text(encoding="utf-8"))) if STATE.exists() else set()
    texts = recent_texts(hook["transcript_path"], rows=rows) + [t for t in [hook.get("last_assistant_message")] if isinstance(t, str) and t]
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
    if AFTER in sys.argv[1:]:
        rest = sys.argv[sys.argv.index(AFTER) + 1:]
        print(after_compact(Path(rest[0]) if rest and not rest[0].startswith("--") else None))
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
