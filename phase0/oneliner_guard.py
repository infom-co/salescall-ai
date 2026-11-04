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
- python3.13 oneliner_guard.py --after-compact [作業場所のフォルダ]：0-1 で読むもの（区分けの (A)＝ALWAYS）を、読む道具で切れない大きさのファイルに出す
  （0-1 の頭の手順を機械でする1か所）＝区切りの時刻（日本時間）・seg（前の区切りから今の区切りまでのご発言と返事）・after（区切りのあとのご発言）・
  english（前の区切りからの英語の一言）・book（帳面の 0-1・0-2・1）・intent（本書の 1章・2章・9章）。(B)＝BEFORE（その作業の前に読む）の表と、
  (C)（毎回は読まない）も出す。どの記録の区切りかは、止めたときに見張りが覚えておく（COMPACT_STATE）。
- ✅ 7時台のご質問「intent.mdをしっかり読み込んで、そこまでのやりとりをしっかりと理解できるようになっていますか？」⇒ 命令を呼んだだけでは止めるのを
  やめない：出したファイルを全部、読む道具で切らずに（offset・limit を付けずに）読み終えるまで、読む道具（READ_TOOLS）と `--status`（残りを出す）の
  ほかは止める（⛔ 帳面 0-1 の17回目＝探して読んだ所だけで書き換えた、を作りで止める）。読み終えたかは、会話の記録の1か所（読む道具の呼び出しと、
  止められずに返った答え）で数える。区切りは、記録の末尾で見つけたら覚え、そのあとは覚えた所から見る（記録の末尾から外れても止め続ける）。
- ⛔ 2026-11-05 日本時間7時台（入れた日の最初の本物の区切り）に見つけて直した3つ：(1) 印を読み込むときに「読む途中」を捨てていて、命令を呼んでも
  止め続けた（私は、命令の名前を入れた別の命令で見張りを直した＝下の (3) の穴を使った・帳面 0-1） (2) 区切りを毎回、記録の末尾2MBから探していた
  ので、本書を読み進めて記録が伸びると区切りが見えなくなり、29本のうち後半12本を数えず、書き換えも止めなかった (3) 通す命令を「名前が入っているか」
  だけで見ていて、ほかの命令をつなげても通った（形まで見る＝_guard_command）。
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
STATUS = "--status"
READ_TOOLS = ("Read", "Grep", "Glob", "ToolSearch", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet")   # 読むあいだも通す道具（何も書き換えない）
INTENT = HERE.parent / "Intent.md"
BOOK = HERE.parent / "docs" / "team" / "CL-1.md"
# 要約のあとに読むものの区分けの正本（✅ 2026-11-05 日本時間7時31分29秒 ユーザー「(1)(2)ともその形でOK、進めてください」。7時25分09秒のお言葉：
# 「毎回、絶対に読むべき部分、このような作業の前は絶対に読むべき部分、すでに終わったなどで読まなくて良い部分などに区分けしておくと、無駄なコンテキストの浪費を
# 抑えられませんか？」。⛔ それまでは本書の全部とご発言の全部を毎回読み、読むだけでコンテキストの半分以上を使っていた）
ALWAYS = (   # (A) 要約のあと毎回必ず読む（見張りが出し、読み終えるまで書き換えを止める）：(出す名前, ファイル, 見出しの頭)
    ("book", BOOK, ("### 0-1.", "### 0-2.", "## 1.")),       # 帳面：手順・ご決定の台帳・いまの状態
    ("intent", INTENT, ("## 1.", "## 2.", "## 9.")),        # 本書：目的と人の呼び方・原則・会話（CL-1 の持ち場）
)
BEFORE = (   # (B) その作業の前に必ず読む（見張りは表を出すだけ。読むのは CL-1 が守る）：(いつ, 何を)
    ("取込・名前の読みの作業の前", "本書 6章"),
    ("電話の部品・止め方・かけ直しに触る前", "本書 8章・10章"),
    ("判定AIの顔ぶれ・料金・キー・お客様の言葉の送り先・測りの前", "本書 12章・13章と、測りの記録（docs/PHASE0_VERIFICATION.md）のその番号の所"),
    ("判定への説明・試しの言葉の正解を直す前", "測りの記録の G-59・G-53（試しの言葉の正解へのご判断の正本）"),
    ("ご判断を伺う前（決まっていることを伺い直さない）", "本書 18章と、帳面 0-2・本書をその言葉で探した所"),
    ("CL-2 の頼みに答える前", "帳面 4章のその番号の所と、CL-2 の連絡帳"),
    ("ほかの担当の決まりに触る前", "本書 3〜5・7・11・14〜17章"),
)
NOT_ROUTINE = ("(C) 毎回は読まない（要るときに探して読む）：本書 19章（直した記録）・ご発言の生の全文（ご決定は帳面 0-2 の台帳）・"
               "経過の記録（docs/team/CL-1_HISTORY.md・docs/INTENT_HISTORY.md）")
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
    return {"done": list(st.get("done") or []), "pending": st.get("pending"), "reading": st.get("reading")}


def _save_compact(st: dict) -> None:
    COMPACT_STATE.parent.mkdir(parents=True, exist_ok=True)
    COMPACT_STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


class _locked:
    """印を読んで書き直すあいだ、ほかの見張り（同時に呼んだ道具の分）を待たせる（書き直しの取り合いで、読んだ印を落とさない）。"""
    def __enter__(self):
        COMPACT_STATE.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(COMPACT_STATE.with_name(COMPACT_STATE.name + ".lock"), "a")
        try:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, *exc):
        self.f.close()


def _guard_command(hook: dict, *flags: str) -> bool:
    """この見張りの命令（flags のどれか）だけを呼ぶ Bash か。⛔ 2026-11-05 日本時間7時台：名前が入っているかだけで見ていて、ほかの命令を
    つなげても通っていた（見張りの不具合で止まったとき、私がそれを使って見張りを直した＝帳面 0-1）＝形まで見る：python・この見張りのファイル・
    flags の1つ（--after-compact なら作業場所のフォルダを1つまで）だけで、つなぎの記号を含まない。"""
    import shlex
    if hook.get("hook_event_name") == "Stop" or hook.get("tool_name") != "Bash":
        return False
    command = str((hook.get("tool_input") or {}).get("command", ""))
    if any(c in command for c in ";&|`$<>()\n\\"):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) < 3 or not Path(parts[0]).name.startswith("python") or parts[2] not in flags:
        return False
    where = Path(parts[1]) if Path(parts[1]).is_absolute() else Path(str(hook.get("cwd") or ".")) / parts[1]
    if where.resolve() != Path(__file__).resolve():
        return False
    return len(parts) == 3 or (len(parts) == 4 and parts[2] == AFTER)


def _scan_reads(reading: dict) -> None:
    """会話の記録の、前に見た所から先を読み、出したファイルを読む道具で切らずに（offset・limit を付けずに）開いて答えが返った（止められて
    いない・読めた）ものを、読んだと数える（読んだかの事実は会話の記録の1か所。呼ぶ時の知らせでは数えない）。"""
    files, done = set(reading.get("files") or []), set(reading.get("read") or [])
    uses = dict(reading.get("uses") or {})
    with open(reading["transcript"], "rb") as f:
        f.seek(int(reading.get("scanned") or 0))
        data = f.read()
    end = data.rfind(b"\n") + 1   # 書きかけの最後の行は次に読む
    for line in data[:end].splitlines():
        if b'"tool_use"' not in line and b'"tool_result"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        for block in (row.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Read":
                args = block.get("input") or {}
                path = str(Path(str(args.get("file_path", ""))).resolve())
                if path in files and not args.get("offset") and not args.get("limit"):
                    uses[str(block.get("id"))] = path
            elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in uses:
                path = uses.pop(str(block.get("tool_use_id")))
                if not block.get("is_error"):
                    done.add(path)
    reading["scanned"] = int(reading.get("scanned") or 0) + end
    reading["read"], reading["uses"] = sorted(done), uses


def compact_verdict(hook: dict, rows: list[dict] | None = None) -> str | None:
    """まだ済んでいない要約の区切りがあれば止める理由（この見張りの命令・止めたあとの書き直し・手伝いの係は止めない）。区切りは、記録の末尾に
    あれば読み取り、そのあとは覚えた所（COMPACT_STATE）から見る（⛔ 2026-11-05 日本時間7時台：毎回、記録の末尾2MBから探していたので、読み進めて
    記録が伸びると区切りが見えなくなり、数えるのも止めるのもやめていた）。"""
    if hook.get("agent_id"):
        return None
    stop = hook.get("hook_event_name") == "Stop"
    if stop and hook.get("stop_hook_active"):
        return None
    transcript = str(hook["transcript_path"])
    marks = [r.get("timestamp", "") for r in (rows if rows is not None else _tail_rows(transcript)) if _is_boundary(r)]
    with _locked():
        st = _load_compact()
        mine = lambda x: bool(x) and x.get("transcript") == transcript and x.get("boundary") not in st["done"]   # noqa: E731
        reading, pend = st.get("reading"), st.get("pending")
        if marks and marks[-1] not in st["done"] and not (mine(reading) and reading["boundary"] == marks[-1]):
            pend = st["pending"] = {"transcript": transcript, "boundary": marks[-1]}
            _save_compact(st)
        elif mine(reading) and (not marks or reading["boundary"] >= marks[-1]):
            return _reading_verdict(hook, st, reading, stop)
        elif not (mine(pend) and (not marks or pend["boundary"] >= marks[-1])):
            return None
    if _guard_command(hook, AFTER, STATUS):
        return None
    how = (f"最初に `{sys.executable} {Path(__file__).resolve()} {AFTER} <作業場所のフォルダ>` を呼んでください（区切りの時刻・前の区切りからの"
           "ご発言と返事・英語の一言・本書を切らずに読むファイルを出します）。そのあと帳面 0-1 を上から順に通し、出したファイルを全部読み終えるまで、"
           "作り・帳面・本書を書き換えないでください。")
    head = "返事は終えていません" if stop else "この道具は呼んでいません"
    return f"⛔ 要約の区切り（日本時間{jst(pend['boundary'])}）のあと、まだ帳面 0-1 を始めていません。{head}。{how}"


def _reading_verdict(hook: dict, st: dict, reading: dict, stop: bool) -> str | None:
    """--after-compact のあと、出したファイルを全部読み終えるまで：読む道具と見張りの命令は通し、ほかは止める（全部読んだら、その区切りを済みにする）。
    印を書き直すのは、ロックの中で呼ばれたときだけ（compact_verdict）。"""
    _scan_reads(reading)
    left = [f for f in reading.get("files") or [] if f not in set(reading["read"])]
    if not left:
        st["done"].append(reading["boundary"])
        st["reading"] = None
    _save_compact(st)
    if not left or _guard_command(hook, AFTER, STATUS) or (not stop and hook.get("tool_name") in READ_TOOLS):
        return None
    head = "返事は終えていません" if stop else "この道具は呼んでいません"
    return (f"⛔ 要約の区切り（日本時間{jst(reading['boundary'])}）のあとの 0-1 で、まだ読み終えていないファイルがあります（残り {len(left)}：{left[0]} ほか）。"
            f"{head}。残りを読む道具で切らずに（offset・limit を付けずに）全部読み終えるまで、作り・帳面・本書を書き換えないでください"
            f"（残りの一覧は `{sys.executable} {Path(__file__).resolve()} {STATUS}`）。")


def _sections(path: Path, heads: tuple[str, ...]) -> list[tuple[int, str]]:
    """ファイルの、見出しの頭が heads の節（次の同じか上の段の見出しまで）を、もとの行の番号つきで（本書・帳面を直すときに、行の番号で探せる）。
    見出しが見つからなければ止める（黙って読まずに済ませない）。"""
    lines = path.read_text(encoding="utf-8").split("\n")
    level = lambda l: len(l) - len(l.lstrip("#")) if l.startswith("#") and l.lstrip("#").startswith(" ") else 0   # noqa: E731
    picked = []
    for head in heads:
        start = next((i for i, l in enumerate(lines) if l.startswith(head)), None)
        if start is None:
            raise ValueError(f"{path.name} に見出し「{head}」がありません（区分けの表 ALWAYS を直す）")
        end = next((j for j in range(start + 1, len(lines)) if 0 < level(lines[j]) <= level(lines[start])), len(lines))
        picked += [(i + 1, lines[i]) for i in range(start, end)]
    return picked


def _split_lines(lines: list, out: Path, size: int = READ_SIZE, fold: int = READ_FOLD) -> list[Path]:
    """行の並びを、読む道具で切れない大きさのファイル（out/p01.txt…）に分ける唯一の分け方（行の頭に行の番号。長い行は fold 字ずつ折り返し、
    折り返しの行は「番号+:」。⛔ 2026-11-05：行ごとに分けていたので、3万字を超える1行が1つのファイルに入り、読む道具が途中で切った＝折り返した
    切れごとに分ける）。"""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("p*.txt"):
        old.unlink()
    files, cur, used = [], [], 0
    for i, line in (x if isinstance(x, tuple) else (n, x) for n, x in enumerate(lines, 1)):
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
    return paths


def split_for_reading(src: Path, out: Path, size: int = READ_SIZE, fold: int = READ_FOLD) -> tuple[list[Path], int]:
    """本書を読むファイルに分ける（分け方は _split_lines の1か所）。"""
    lines = src.read_text(encoding="utf-8").split("\n")
    return _split_lines(lines, out, size, fold), len(lines)


def after_compact(out_dir: Path | None = None) -> str:
    """止めた要約の区切りについて、0-1 の頭の手順を機械でして（区切りの時刻・ご発言と返事・英語の一言・本書を読むファイル）、その区切りを済みにする。"""
    from read_user_messages import ME, _rows, english as is_english
    with _locked():
        pend = _load_compact().get("pending")
    if not pend:
        return "止められている要約の区切りはありません（何もしていません）。"
    path, b = Path(pend["transcript"]), pend["boundary"]
    scanned = path.stat().st_size   # 読んだかは、ここ（ファイルを出したあと）から先の記録で数える
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

    def write(name: str, rows: list) -> tuple[list[Path], int]:
        lines = "\n\n------\n".join(f"[日本時間{jst(ts)}] {who}\n{text}" for ts, who, text in rows).split("\n") if rows else ["（無し）"]
        return _split_lines(lines, out / name), len(rows)
    seg = write("seg", _rows(path, prev, b, with_assistant=True))
    after = write("after", _rows(path, b, ""))
    eng = write("english", [r for r in _rows(path, prev, "", with_assistant=True) if r[1] == ME and is_english(r[2])])
    books = {name: _split_lines(_sections(src, heads), out / name) for name, src, heads in ALWAYS}
    for old in out.glob("users"):   # 前の形の出し物（ご発言の全部）は残さない
        for x in old.glob("p*.txt"):
            x.unlink()
    files = [str(x.resolve()) for x in seg[0] + after[0] + eng[0] + [p for ps in books.values() for p in ps]]
    with _locked():
        st = _load_compact()
        st["reading"] = {"transcript": str(path), "boundary": b, "files": files, "read": [], "uses": {}, "scanned": scanned}
        st["pending"] = None
        _save_compact(st)
    def names(ps: list[Path]) -> str:
        return f"{ps[0].parent}/p01.txt〜p{len(ps):02d}.txt（{len(ps)}本）"
    return "\n".join([
        f"要約の区切り：日本時間{jst(b)}（前の区切り：{('日本時間' + jst(prev)) if prev else '無し'}）。",
        f"見張りは、下の (A) の {len(files)} 本を全部、読む道具で切らずに（offset・limit を付けずに）読み終えるまで、読む道具のほかを止めます。帳面 0-1 の手順：",
        f"(A1) 前の区切りから今の区切りまでのご発言と私の返事（{seg[1]}件）：{names(seg[0])}",
        f"(A2) 今の区切りのあとのご発言（{after[1]}件）：{names(after[0])}",
        f"(A3) 前の区切りからの英語の一言（{eng[1]}件）：{names(eng[0])} ＝帳面に記録が無いものを記録する（読み終えてから）",
        *(f"(A{n}) {src.relative_to(HERE.parent)} の {'・'.join(h.split(' ')[1].rstrip('.') for h in heads)}（行の番号はもとの番号）：{names(books[name])}"
          for n, (name, src, heads) in enumerate(ALWAYS, 4)),
        "読み終えたら、要約の前後のご決定・約束・待っていることが本書・設計書・帳面に入っているかを1つずつ突き合わせる（見張りでは見られない＝CL-1 が守る）",
        "(B) その作業の前に必ず読む：" + "／".join(f"{when}＝{what}" for when, what in BEFORE),
        NOT_ROUTINE,
        f"読むファイル：{files[0]} ほか（全部の一覧は {STATUS}）",
    ])


def status() -> str:
    """読むあいだの残り（--status）。"""
    with _locked():
        st = _load_compact()
        reading = st.get("reading")
        if not reading:
            return "読んでいる途中の要約の区切りはありません。"
        _scan_reads(reading)
        _save_compact(st)
    done = set(reading.get("read") or [])
    left = [f for f in reading.get("files") or [] if f not in done]
    return "\n".join([f"要約の区切り（日本時間{jst(reading['boundary'])}）の 0-1：残り {len(left)} 本（読んだ {len(done)} 本）", *left])


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
    if STATUS in sys.argv[1:]:
        print(status())
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
