"""共通の一言の録音の名前と文の一覧（core/common_phrases.py の TEXTS）と、名前の録音の台帳の続く言葉の名前と文（同じファイルの NAME_TAILS）を、会話の担当
（CL-1）の文の正本 phrases.RECORDED・phrases.NAME_TAILS から作り直す道具（続く言葉は CL-2 の連絡帳 4-405 の (3)・CL-1 の連絡帳 4-199。CL-2 の
連絡帳 4-403。✅ 2026-11-15 ユーザー：共通の一言の録音も、業務の録音・標準の録音と「同じ決まりにする」＝文の違う承認済みの録音は使わない）。
手で写さない（名前の写し漏れが続いた＝CL-2 の連絡帳 4-402 の (2)）。CL-1 が文や名前を変えたら、これで作り直す。合っているかは CL-1 の比べる
試験（phase0/test_offline.py）が見る。
使い方（app で）：.venv/bin/python tools/common_phrases.py ../phase0/phrases.py"""
import importlib.util
import pathlib
import sys

OUT = pathlib.Path(__file__).resolve().parents[1] / "src" / "salescall" / "core" / "common_phrases.py"
HEAD = '''"""共通の一言の録音の名前と文（判断はここだけ。生成したファイル＝手で直さない。作り直しは app/tools/common_phrases.py）。持ち主 CL-2。

- 名前と文の正本は、会話の担当（CL-1）の phrases.RECORDED（流れの制御と判定が読む文）。ここはその写しで、CL-1 の比べる試験
  （会話の担当の試験 test_offline）が、名前も文も同じかを確かめる（CL-2 の連絡帳 4-403）。
- ✅ 2026-11-15 ユーザー（共通の一言の文が変わったとき）：「同じ決まりにする（お勧め）」＝業務の録音・標準の録音と同じく、文の違う承認済みの録音は
  使わない。話してよい声（core/recordings.ready_voices）は、共通の一言の組（システムの持ち物）に、ここの部分がすべて、ここと同じ文である声だけ。
  ⛔ それまでは名前だけを見ていた＝文が替わっても、前の文で録った録音が流れ、お客様に聞こえる文と、判定がお返事を読む文が食い違いえた
  （CL-1 の連絡帳 4-164 の例：announce_recheck を前の文で録った録音）。
- ⏳ 会話の担当の本番のコード（app/src/salescall/conversation/。フェーズ1）ができたら、ここは写しをやめて、そこの1か所を読む。
"""

TEXTS: dict[str, str] = {
'''
TAIL_HEAD = '''}

# 名前の録音の台帳の続く言葉（名前 → 文。名乗りの部分の名前＝recordings.NAME_TAILS はこの鍵）。文は、お名前の呼び方 {who} より後ろ（助詞から）。
# 正本は CL-1 の phrases.NAME_TAILS（形は phrases.NAME_FORMS）。続く言葉の録音は、この文と同じ文の録音だけを流す（共通の一言と同じ決まり＝データベースの
# salescall.current_name_set が文で選ぶ。0259）。お名前の前に言葉のある形（「恐れ入りますが」＝phrases.NAME_HEADS）の頭は、続く言葉に入らない。
NAME_TAILS: dict[str, str] = {
'''


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    spec = importlib.util.spec_from_file_location("cl1_phrases", argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    texts = mod.RECORDED
    bad = sorted(k for k, v in texts.items() if not (isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()))
    if bad:
        print(f"名前か文が空・文字でない部分があります：{bad}")
        return 1
    tails = mod.NAME_TAILS
    bad = sorted(k for k, v in tails.items() if not (isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()))
    if bad:
        print(f"続く言葉の名前か文が空・文字でないものがあります：{bad}")
        return 1
    body = "".join(f"    {k!r}: {texts[k]!r},\n" for k in sorted(texts))
    tail_body = "".join(f"    {k!r}: {tails[k]!r},\n" for k in tails)   # 並びは正本のまま（名乗りの部分の順）
    OUT.write_text(HEAD + body + TAIL_HEAD + tail_body + "}\n", encoding="utf-8")
    print(f"{OUT} に {len(texts)} 部分と、続く言葉 {len(tails)} を書きました")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
