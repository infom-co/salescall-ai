"""AIと同じ声で、プログラムが流す録音を作る（DESIGN.md 6-13・6-15）。
- 共通の一言（声の型ごとに1回。どの依頼主にも同じもの）：相づち・つなぎの言葉・締め・待ってもらう一言（phrases.RECORDED）⇒ results/ack/
- 依頼主の単位（--script。トークスクリプトだけ）：質問の文（業務設定の質問。つなぎの言葉は含めない）⇒ results/script/
  ✅ 2026-10-15 ユーザー：「頻繁に使うワードは、契約者単位でシステム的に録音しておいたらいい。依頼者ごとに録音するのは、トークスクリプトのみ」

✅ 2026-10-14 ユーザー：聞き比べで「明らかにこちらの方が自然な間。話の流れもいい感じ」。基本は「はい、」。違和感のある場面や
   「はい」が続くときは、別の相づち。⇒ 言葉は決まった言い方の表（phrases.ACK）の1か所から読み、どの場面でどれを流すかは
   流れの制御（flowctl.Flow._ack）が決める。
作った録音は、別の耳の2通りの聞き方の**両方**で、読みが合うことを確かめる（毎回の通話で同じ録音を流すので、ほかの試験より厳しくする）。
長すぎる録音も落とす（間を埋めるための相づちが、AIの声を待たせないように）。長さは、声の前後の無音を除いた、声だけの長さで比べる
（⛔ 2026-10-14 の1回目：無音まで含めた長さで比べ、読みの合った S4 の「承知しました、」を長すぎとして落とした）。
作ってある録音は作り直さない（--redo で名指ししたものだけ作り直す）。
--candidates N：確かめに通った録り方を N 本、候補として candidates/ に残す（使っている録音は置き換えない）。人が聞いて選ぶ。
--adopt：人が選んだ候補（例 S4_shouchi_2）を、使う録音にする。
  ✅ 2026-10-15 ユーザー：「S4 の『承知しました、』02＞01＞04＞03（03のみ最後が途切れている）」
  ⚠️ 2026-10-15 ユーザー：「S4 の最初の『承知しました。』が外国人が話すようなイントネーション」＝読みは合っていて、耳の確かめでは落とせない
⚠️ 本番で使うなら、決まった発話の録音（6-13）と同じく、人（運営者）が聞いて承認する。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
import time

from barge import voice_runs, voiced
from common import HERE, base_parser, describe_error, make_client, pick_key, require_python, save_result, say, write_wav
from instruction import VOICE_TYPES, voice_style
from jtext import compare_reading
from listen import hear_feeds
from live_util import build_config, collect_turn, send_text
from phrases import POINT_FALLBACK
from phrases import _CONTACT_RESUMED, APOLOGY_FIRST, CALLBACK_ASK_ANSWERING, APOLOGY_PURPOSE, CALLBACK_ASK, CALLBACK_ASK_AGAIN, CALLBACK_ASK_CHOSEN, CALLBACK_ASK_WHICH, CALLBACK_ASK_OTHER_WAYS, CALLBACK_ASK_OTHER_WAYS_AGAIN, CALLBACK_ASK_RESUMED, LINE_TROUBLE, NUMBER_AGAIN, RECORDED, STANDARD_PARTS, SUCCESSOR_IDS, TALK_FALLBACK, WISH_TOLD
from voice_features import read_wav

OUT = HERE / "results" / "ack"
SCRIPT_OUT = HERE / "results" / "script"
def speech_seconds(pcm: bytes, rate: int) -> float:
    """声の前後の無音を除いた、声だけの長さ（秒）。"""
    runs = voice_runs(voiced(pcm, rate), 0.15)
    return runs[-1][1] - runs[0][0] if runs else 0.0


# 読み方（相づちとお詫びで、声の調子を変える）と、長さの上限（声だけの長さ・秒）
HOW = {"moushiwake": "電話で、相手に聞き取れなかったことを申し訳なく思いながら、丁寧に",
       # ✅ 2026-10-16 ユーザー：重大なお詫び（2回目以降の聞き返し・最後の1回・こちらの誤り・クレーム・かけ直し）
       "moushiwake_heavy": "電話で、お客様にご迷惑をおかけしたことを、心から深くお詫びするように、誠実に、丁寧に",
       "shitsurei": "電話で、こちらの聞き違いを丁寧にお詫びするように",
       # ✅ 2026-10-14 ユーザー：「Sadachbia の『ありがとうございます。』の1つが、明るいのは良いが、ハイテンション過ぎて、ちょっとお客が
       #    引きそう。『前向き』なくらいでいい」「Leda の『ありがとうございます。』は逆に少し、感謝が足りない暗い感じが多い」
       "arigatou": "電話で、相手に感謝を伝えるように、前向きに、心を込めて。ただし、はしゃがず、声を張り上げず、落ち着いた調子で、短く",
       "lead_soredeha": "電話で、次のお話に移るつなぎの言葉として、自然に、柔らかく、短く",
       "lead_tsuzukimashite": "電話で、次のお話に移るつなぎの言葉として、自然に、柔らかく、短く",
       "lead_deha": "電話で、次のお話に移るつなぎの言葉として、自然に、柔らかく、短く",
       # ⚠️ 2026-10-15 ユーザー：「『お忙しいところ、お時間をいただきまして、ありがとうございました。それでは、失礼いたします。』に
       #    なって、少しテンポが悪い」（文の途中に 0.2〜0.4秒の無音が入っていた）⇒ 区切らずに読ませ、文の途中の長い無音は確かめで落とす
       "closing": "電話の最後に、お客様に感謝を伝えるように、前向きに、心を込めて、落ち着いた調子で。言葉の途中で区切らずに、なめらかに、ひと続きで",
       "closing_declined": "電話の最後に、お忙しいところお電話したことを申し訳なく思いながら、丁寧に、落ち着いた調子で。"
                           "言葉の途中で区切らずに、なめらかに、ひと続きで",
       "wait": "電話で、お客様に少し待っていただくことを、申し訳なく思いながら、柔らかく丁寧にお願いするように",
       "wait_heavy": "電話で、ご迷惑をおかけしているお客様に、さらにお待ちいただくことを、心から申し訳なく思いながら、誠実に、丁寧にお願いするように",
       "question": "電話で、お客様に丁寧に、柔らかくお聞きするように",
       "greeting": "電話に出てくださったお客様に、最初のごあいさつをするように、丁寧に、落ち着いた調子で",
       "intro": "電話で、お客様に会社名と自分の名前を名乗るように、丁寧に、落ち着いた調子で。名前ははっきりと",
       "purpose": "電話で、お客様にお電話の用件を伝えるように、丁寧に、柔らかく",
       # 「AIですか？」への決まった言い方（✅ 2026-10-19 ユーザー「何か問題でもございましたでしょうか？」）：問い詰める・身構える調子にしない
       "ai": "電話で、お客様に、何か気になる点があったかを、心配するように、丁寧に、柔らかくお聞きするように",
       "voicemail": "留守番電話に伝言を残すように、丁寧に、落ち着いた調子で、はっきりと",
       # 重ねて「人ですか」と聞かれたとき（phrases.AI_ASKED_AGAIN）：身構えず、誠実に、落ち着いて（言い訳のような早口にしない）
       "ai_again": "電話で、不安にさせてしまったお客様に、心から申し訳なく思いながら、誠実に、落ち着いた調子で、言葉の途中で区切らずに",
       # 相手の質問に答える（✅ 2026-10-22 ユーザー：「必ずどんな営業トークにも出てくるパターンはシステム側に録音しておいた方がよくない？」）：
       # 想定問答の答え（依頼主の単位）
       "answer": "電話で、お客様のご質問に、丁寧に、落ち着いた調子でお答えするように",
       # 相手の申し出に応じる一言（⏳ 2026-10-22 の案。phrases.OFFER_THANKS・DEPARTED_ACK）と、伝言を残す業務の伝言の文（依頼主の単位）
       "offer_thanks": "電話で、お客様のお気遣いに心から感謝を伝えるように、前向きに、丁寧に、落ち着いた調子で。言葉の途中で区切らずに",
       "departed": "電話で、お呼びした方がもういらっしゃらないと知って、失礼をお詫びするように、丁寧に、落ち着いた調子で",
       "message": "電話で、お客様にご伝言をお願いするように、丁寧に、柔らかく、はっきりと",
       "authority": "電話で、お客様に丁寧に、柔らかくお聞きするように。失礼のないように、へりくだって",
       # 答えの文が確かめに通らなかったときの一言（phrases.OWN_FALLBACK）：断らない（✅ 2026-10-22 ユーザー（4回目）。⛔ 前は「お答えできないことを、
       # 申し訳なく思いながら」と読ませていた＝断りの調子）。ご質問を受け止めて、確かにお伝えすることを
       "own_fallback": "電話で、お客様のご質問をしっかり受け止めて、確かにお伝えすることを、前向きに、丁寧に、落ち着いた調子で。言葉の途中で区切らずに",
       # 受け答えの受け皿の一言（phrases.TALK_FALLBACK）：お客様のお話に、気持ちをこめて応える（決まり文句を読み上げる調子にしない）
       "talk": "電話で、お客様のお話に、気持ちをこめて自然に応えるように、丁寧に、柔らかく、落ち着いた調子で",
       # 前の電話の事情で、まずお詫びする一言（phrases.APOLOGY_FIRST）：重大なお詫びと同じ心で
       "apology_first": "電話で、前回の電話でお客様にご迷惑をおかけしたことを、心から深くお詫びするように、誠実に、丁寧に、落ち着いた調子で。言葉の途中で区切らずに",
       # 通話をどうしても途中で切るときの2つ（phrases.LINE_TROUBLE）：こちらの都合でご迷惑をおかけしていることを、慌てずに誠実に
       "line_trouble": "電話で、こちらの都合でお客様にご迷惑をおかけしていることを、申し訳なく思いながら、慌てずに、誠実に、丁寧に、落ち着いた調子で。言葉の途中で区切らずに",
       # 番号を伺い直す一言（phrases.NUMBER_AGAIN）：前回伺った番号のことを、お客様を責めずに、申し訳なさそうに丁寧にお伺いする
       "number_again": "電話で、前回伺った番号のことを、お客様を責めずに、申し訳なさそうに、丁寧に、柔らかくお伺いするように。言葉の途中で区切らずに",
       # 同じ言葉の調子ごとの録音（phrases.TALK_FALLBACK の調子。✅ 2026-10-23 ユーザーが S2 の聞き比べで調子を聞き分けた）
       "talk_agree_high": "電話で、弾んだ調子で楽しそうに話すお客様に、こちらも元気よく、気持ちを込めて同意するように",
       "talk_agree_deep": "電話で、お客様のお話に、深くうなずきながら、心から同意するように、気持ちを込めて、落ち着いた調子で",
       "talk_agree_light": "電話で、軽い世間話に、さらっと軽く同意するように、柔らかく、自然に",
       "talk_heard_sad": "電話で、お客様の残念なお話を伺って、しんみりと、思いやりを込めて応えるように、静かに、丁寧に",
       "talk_heard_glad": "電話で、お客様のお話を伺って、少し嬉しそうに、柔らかく応えるように",
       "talk_heard_nod": "電話で、お客様のご事情やご説明を伺って、なるほどと納得したように、落ち着いて、柔らかく応えるように",
       "talk_hard_sad": "電話で、お客様のつらいお話を伺って、しんみりと、心から気づかうように、静かに、丁寧に",
       "talk_hard_bright": "電話で、大変だったけれど今は落ち着いたというお客様のお話に、前向きに、ねぎらうように共感して応えるように",
       # もう伺った質問へ戻る前の一言（phrases.BACK_TO）：お客様のお話を受けたあと、柔らかく本題へ戻る
       "back": "電話で、お客様のお話を受けたあとに、柔らかく自然に本題へ戻るように、丁寧に、落ち着いた調子で",
       # 同じ所へ2回目に戻るときの一言（phrases.BACK_LINES。✅ 2026-11-07 日本時間3時14分01秒 ユーザー「(4) H7」）：同じ読み方。3回目からの一言（BACK_TO_MIDWAY）は
       # 続く質問と1本の録音（質問の読み方＝script_table・BACK_FIT）
       "back_again": "電話で、お客様のお話を受けたあとに、柔らかく自然に本題へ戻るように、丁寧に、落ち着いた調子で",
       # まだ答えを考えている途中のお客様への相づち（phrases.ACK の aizuchi）：答えを期待するような言い方にしない（✅ 2026-10-14 ユーザー：S2・S4 の「はい」が物欲しそう・
       # 期待しているように聞こえる＝instruction.THINKING_RULE と同じ読み方）。✅ 2026-11-06 日本時間11時19分55秒 ユーザー「(1) ア」
       "aizuchi": "電話で、考えているお客様に、落ち着いた声で、語尾を上げずに短く相づちを打つように（相手の答えを期待するような言い方にしない）"}
DEFAULT_HOW = "電話で、相手の話に相づちを打つように、短く"
MAX_S = {"hai": 1.0, "shouchi": 1.4, "kashikomari": 1.6, "arigatou": 1.6, "shitsurei": 1.6, "moushiwake": 1.6, "moushiwake_heavy": 1.8, "lead_soredeha": 1.0,
         "lead_tsuzukimashite": 1.2, "lead_deha": 0.8, "closing": 8.0, "closing_declined": 7.0, "wait": 3.5, "wait_heavy": 3.8, "question": 6.0,
         "greeting": 2.5, "intro": 4.5, "purpose": 4.5, "ai": 3.0, "voicemail": 12.0, "ai_again": 8.0, "answer": 8.0, "offer_thanks": 7.5,
         "departed": 4.0, "message": 12.0, "authority": 13.0, "own_fallback": 9.0, "back": 3.0, "back_again": 4.0, "aizuchi": 1.0,
         "talk": 3.5, "apology_first": 6.5, "line_trouble": 9.5, "number_again": 12.0}
MAX_S.update({k: MAX_S["talk"] for k, (_, tone, _) in TALK_FALLBACK.items() if tone})
# 長い文の上限は、文の長さの見込み（L4 の締めの実測：42字・5.92秒＝1字0.141秒）の1.15倍より短くしない（ClipTablesTest が見る）。
# ✅ 2026-10-22 ユーザー（4回目）の言い方で、ほかの手段の質問（「それでは恐れ入りますが…」）・お立場を伺う一言（2つから選んでいただく形）・
# 申し出へのお礼・断らない一言が長くなった ⇒ 質問 5.0→6.0・お立場 7.0→13.0・お礼 6.0→7.5・一言 4.0→5.5（10-23 のユーザーの言い方で 5.5→8.5）。「AIですか」と重ねて聞かれた
# ときの一言（46字）も、この見込みに届いていなかった（7.0→8.0）。✅ 2026-11-05 日本時間3時33分11秒 ユーザーのご修正の言い方（続きの電話のご説明できないときの
# 一言・54字）で、断らない一言 8.5→9.0
CHAR_S = 0.141
# 録音の中の名前ごとの読み方の鍵（無ければ中の名前のまま）。かけ直しで足す質問は、質問の読み方（⛔ 2026-10-19 の見直しで見つけた漏れ：
# 足す質問を共通の一言の録音に入れたとき、読み方も長さの上限も無く、作ると止まった＝ClipTablesTest で、表の漏れを見る）
HOW_OF = {f"callback_{k}": "question" for k in CALLBACK_ASK}
HOW_OF.update({f"callback_{k}_{i}": "question" for k, vs in CALLBACK_ASK_AGAIN.items() for i, _ in enumerate(vs, 2)})   # 2回目からの言い方も質問の読み方
HOW_OF.update({f"callback_{k}_which{t}": "question" for k in CALLBACK_ASK_WHICH for t in ("", "_recap")})   # どの点かを伺う一言も質問の読み方（2026-11-06）
HOW_OF.update({f"callback_{k}_again": "question" for k in CALLBACK_ASK_RESUMED})   # 続きの電話で切れた所から伺う一言も質問の読み方（2026-11-04）
HOW_OF.update({f"callback_{k}_chosen": "question" for k in CALLBACK_ASK_CHOSEN})
HOW_OF.update({f"callback_{k}_answering": "question" for k in CALLBACK_ASK_ANSWERING})   # 答えの段のかけ直しの一言も質問の読み方（2026-11-06）   # LINE の伺い方を選んでいただいたあとの一言も質問の読み方（2026-11-04 20時27分）
# LINE の伺い方で選んでいただいた方法の連絡先が無いときに、ほかの方法を伺う一言と、その伺い直しも質問の読み方（2026-11-05 0時48分。phrases.CALLBACK_ASK_OTHER_WAYS）。
# ほかの方法を伺う一言は、LINE の伺い方の一言（callback_line_how）と同じく長い質問の上限（1時19分47秒のメールの一言は40字＝見込み 6.5秒）
HOW_OF.update({f"callback_line_how_without_{k}": "question" for k in CALLBACK_ASK_OTHER_WAYS})
# 伺い直しの一言の名前は、伺い直しの言い方の表（CALLBACK_ASK_OTHER_WAYS_AGAIN）から作る＝phrases.RECORDED と同じ元（⛔ 2026-11-05 の読み直しで見つけた：ほかの方法を伺う
# 一言の表の鍵から作っていて、2つの表の鍵がずれても気づけなかった）
HOW_OF.update({f"callback_line_how_without_{k}_2": "question" for k in CALLBACK_ASK_OTHER_WAYS_AGAIN})
# 資料でご説明できない点の一言は、断らない一言と同じ読み方（2026-11-02）
HOW_OF.update({**{n: "own_fallback" for n in POINT_FALLBACK}, "resumed_fallback_lead": "own_fallback"})
# 点を言っていただけないまま手順を終えるときに挟む一言（phrases.POINT_ANYTIME・その後半だけの POINT_ANYTIME_PLAIN。✅ 2026-11-05 日本時間5時20分56秒・5時45分 ユーザー）：
# お客様のお申し出をいつでもお受けすることを
# 伝える一言＝断らない一言と同じ読み方（34字＝見込み 4.8秒）
HOW_OF["point_anytime"] = HOW_OF["point_anytime_plain"] = "own_fallback"
# 今のところ分からない点は無いと言われたときに添える一言（phrases.UNCLEAR_ANYTIME。✅ 2026-11-07 日本時間13時05分06秒 ユーザー）：いつでもお尋ねを
# お受けすることを伝える一言＝上と同じ読み方
HOW_OF["unclear_anytime"] = "own_fallback"
# ほかのご希望を担当の者に申し伝える一言（phrases.WISH_TOLD。2026-11-03）：確かにお伝えすることを言う一言＝断らない一言と同じ読み方
HOW_OF.update({f"wish_told_{k}": "own_fallback" for k in WISH_TOLD})
# お断りのあとの締め：お断りを受け止めてからお礼（長さの見込み 約8.3秒＝締めの上限 8.0 に届かない）ので、読み方と上限は別（2026-11-02）
HOW["closing_after_decline"] = ("電話の最後に、お客様のお断りを丁寧に受け止めてから、お時間をいただいたことへの感謝を伝えるように、前向きに、心を込めて、"
                                "落ち着いた調子で。言葉の途中で区切らずに、なめらかに、ひと続きで")
MAX_S["closing_after_decline"] = 9.0
# 業務で「引き止める」を選んだときの一言（業務が書く文＝依頼主の単位の busy。script_table）：読み方は質問と同じ。業務の文は質問より長くなりうる
# （例：phrases.BUSY_ASK は42字＝見込み 6.6秒）ので、長さの上限だけ別にする（業務が書く伝言・留守番電話の伝言と同じ考え）
HOW["busy"] = HOW["question"]
MAX_S["busy"] = 9.0
# LINE の連絡先の伺い方（phrases.CALLBACK_ASK の line_how）と、連絡先を伺う前に切れた電話の続きの一言（CALLBACK_ASK_RESUMED の連絡先の役目）：読み方は質問と
# 同じ。✅ 2026-11-04 日本時間19時26分のお言葉の一言は104字（見込み 16.9秒）と長いので、長さの上限だけ別にする（busy・お立場の一言と同じ考え）
HOW["question_long"] = HOW["question"]
MAX_S["question_long"] = 17.0
# 3回目から戻るときの一言（phrases.BACK_TO_MIDWAY・27字）を、上の104字の一言と1本にした形（131字＝見込み 21.2秒）の上限（2026-11-07 日本時間4時40分35秒のご指摘）
HOW["question_xl"] = HOW["question"]
MAX_S["question_xl"] = 24.0


def question_how(text: str) -> str:
    """業務の質問の録音の読み方（唯一の決まり＝script_table・phrases.BACK_FIT）：質問の読み方で、文の長さの見込み（CHAR_S）が収まるいちばん小さい上限の鍵
    （question・question_long・question_xl）。⛔ 2026-11-06 日本時間15時台：続きの電話で最初に伺う形＝頭が「確認させていただきたい内容なのですが」で18字長い形を
    足して見つけた。文の長さにかかわらず question＝6秒だった）。"""
    need = len(text) * CHAR_S * 1.15
    return next((k for k in ("question", "question_long") if need <= MAX_S[k]), "question_xl")
# システムの質問を2回目・3回目からの戻る一言に続けて流す形（phrases.BACK_FIT＝back_fit の1か所。方法をお伝えしたあとの連絡先の一言も同じ形）：質問の読み方で、
# 長さの上限は文の長さで選ぶ（3回目からの形は頭が27字長い）
from phrases import BACK_FIT  # noqa: E402
HOW_OF.update({k: question_how(t) for k, t in BACK_FIT.items()})
# 連絡先の一言の鍵は言い方の表の1か所（phrases._CONTACT_RESUMED＝flowctl.contact_again_key。⛔ 2026-11-05 まで役目を手で並べていた＝オンラインの一言を足すと漏れる）
HOW_OF.update({k: "question_long" for k in ("callback_line_how", *(f"callback_{r}_again" for r in _CONTACT_RESUMED))})
HOW_OF.update({f"callback_line_how_without_{k}": "question_long" for k in CALLBACK_ASK_OTHER_WAYS})
HOW_OF.update({v: "question" for v in SUCCESSOR_IDS.values()})
HOW_OF["callback_authority"] = "authority"
# 待つか、かけ直すかを1回伺う一言（phrases.CALLBACK_ASK["wait_or_later"]・約59字）：判断いただけるお立場かの一言と同じ「2つから選んでいただく問い」の形なので、
# 同じ読み方と長さの上限（✅ 2026-11-05 日本時間23時56分48秒 ユーザー「(ア)」）
HOW_OF["callback_wait_or_later"] = "authority"
# 確かめて戻るお客様を待つ一言（phrases.WAIT_REPLY。頭の「はい、」は相づちの録音）：こちらはこのまま待っていると、落ち着いて伝える
HOW["wait_reply"] = "電話で、お客様が確かめてくるあいだ、こちらはこのまま電話口で待っていると、落ち着いて丁寧に伝えるように"
MAX_S["wait_reply"] = 4.0  # 判断いただけるお立場か・お取次ぎのみかを聞く一言は長い（76字）ので、読み方と長さの上限は別  # 後任の方をお呼びする・確かめる一言（phrases.SUCCESSOR）も質問の読み方
# 「いかがなさいましたか？」「どうなさいましたか？」（phrases.SILENCE_PROMPT）：考えている相手にも、「AIですか」と3回目から聞かれたときにも
# 流す。どちらも、何か気になる点があったかを心配して聞く一言なので、「AIですか」への言い方と同じ読み方
HOW_OF.update({"ikaga": "ai", "dou": "ai"})
# 「AIですか？」への標準の答え（phrases.STANDARD_PARTS。業務が書かないときに流す共通の一言）：業務の答え（依頼主の単位の ai）と同じ読み方
HOW_OF[STANDARD_PARTS["ai"]] = "ai"
# 「どうなさいますか？」（phrases.THINKING_PROMPT の1回目）：悩んで黙った方に、どうなさるかを伺う一言＝質問の読み方（✅ 2026-11-01 日本時間23時04分 ユーザー）
HOW_OF["dounasaimasu"] = "question"
HOW_OF.update({k: "talk" for k, (_, tone, _) in TALK_FALLBACK.items() if tone is None})
HOW_OF.update({f"apology_{k}": "apology_first" for k in APOLOGY_FIRST})  # 調子ごとの録音は、名前そのものが読み方の鍵
# 申し伝える一言まで言って切れた続きの電話のお詫びと申し伝える一言（phrases.APOLOGY_CUT_END・TOLD_AGAIN・TOLD_FOLLOW_UP）：お詫びは前の電話のお詫びと同じ
# 読み方・申し伝える一言はご説明できないときの一言と同じ読み方
HOW_OF.update({**{f"apology_cut_end_{k}": "apology_first" for k in ("same_day", "later")},
               **{f"told_again_{k}_{w}": "own_fallback" for k in ("question", "request", "opinion") for w in ("same_day", "later")},
               "told_follow_up": "own_fallback"})
HOW_OF.update({f"line_trouble_{k}": "line_trouble" for k in LINE_TROUBLE})
HOW_OF.update({f"number_again_{k}": "number_again" for k in NUMBER_AGAIN})
HOW_OF["hold_timeout"] = "closing"   # 保留で待ちきったときの締め：締めの読み方
HOW_OF.update({f"apology_purpose_{k}": "purpose" for k in APOLOGY_PURPOSE})  # お詫びの電話の用件の一言：用件の一文と同じ読み方
# ✅ 2026-10-24 ユーザー：「『もしもし』は誠実な感じを出してください」「誠実+謙虚、誠実+ビジネス、誠実＋謙虚＋ビジネスを試してください」⇒
#    S2 で「誠実に、謙虚に、ビジネスの電話らしく、丁寧に」の4本目が ◎（S4 も同じ録音）。⛔ 前の読み方は S2 で通らなかった：「穏やかに確かめる・
#    問いかけるように柔らかく」＝○「少し怪しむような」・○△「ちょっとイラついた感じ」／「心配して気づかうように」＝3本とも「威圧的」／
#    「誠実に、丁寧に、落ち着いた声で」＝6本とも合格に届かない（L4 の ◎ は前の読み方で録ったもの＝そのまま使う）
HOW["moshimoshi"] = "電話で、相手の声が聞こえないときに、繋がっているかを確かめるように。誠実に、謙虚に、ビジネスの電話らしく、丁寧に"
# 続きの電話の始めに「覚えていない」と言われたときの頭の一言（phrases.ACK の gomottomo）と、振り返りの一言（phrases.recap_line＝依頼主の単位の録音 （番号）_recap。
# script_table）。✅ 2026-11-05 日本時間4時16分52秒 ユーザー：「こちら「ごもっともです。前回は、『（前回、説明した内容）』。とお話したところでお電話が切れて
# しまいました。」などと伝える方が自然な流れですよね。」。振り返りの一言はご承認いただく内容の説明の文が入るので、長い質問と同じ上限
HOW["gomottomo"] = "電話で、お客様のお話をもっともだと受け止めるように、穏やかに、丁寧に、短く"
MAX_S["gomottomo"] = 1.6
HOW["recap"] = ("電話で、前回のお電話でお話しした内容を、お客様に思い出していただけるように、穏やかに、丁寧に、落ち着いた調子で。"
                "言葉の途中で区切らずに、なめらかに、ひと続きで")
MAX_S["recap"] = MAX_S["question_long"]
MAX_S["moshimoshi"] = 1.5


END_SILENCE_S = 0.1  # 声のあとに、これだけの無音が無ければ、声の途中で切れた録音として落とす
# ⛔ 2026-10-15：S4 の「承知しました、」の候補3は、声が鳴っている途中で終わっていた（声のあとの無音 0秒・最後の20ミリ秒が -18dB）。
#    耳の確かめは「承知しました。」と聞いて通した。ほかの録音は、どれも声のあとに 0.2秒以上の無音がある。


def ends_in_silence(pcm: bytes, rate: int) -> bool:
    """声のあとに無音があるか（声の途中で切れていないか）。"""
    runs = voice_runs(voiced(pcm, rate), 0.05)
    return bool(runs) and len(pcm) / 2 / rate - runs[-1][1] >= END_SILENCE_S


LONG_PAUSE_S = 0.11  # 文の途中に入れてよい無音の長さの上限（これより長い無音は、文の切れ目「。」「？」・頭のつなぎの言葉のあとだけ）
# ⛔ 2026-10-15：0.25秒にしていた。ユーザーは締めの候補4本を「『お忙しいところ、お時間をいただきまして、ありがとうございます。』と
#    また『、』が入った言い方になっているので全部だめ」と聞いた。4本とも「ところ｜お時間」「まして｜ありがとう」に 0.12〜0.24秒の無音が
#    あった（区切りのご指摘がなかった昼の締めは、同じ場所が 0.08〜0.1秒）。⇒ 0.11秒にした。選んでいただいた録音（承知しました・はい・
#    ありがとうございます・失礼いたしました・最初の質問・「続きまして…」）は、この基準でも通る。⚠️「それではご希望の日時を…」だけは、
#    つなぎの言葉のあと（0.38秒）のほかに 0.12秒の無音が1つあり、この基準では落ちる（ユーザーは「前半はどれも良い」と聞いた＝0.12秒は境目）
#    ⇒ ✅ 2026-10-16 ユーザー（通話まるごとを聞いて）：「『それでは、ご希望の日時を、お聞かせいただけますでしょうか？』になってしまっている。
#    それではの後も少しだけ間が長い」＝0.12秒も「、」に聞こえる。作り直して選び直す
LEAD_PAUSE_MAX_S = 0.32  # 頭のつなぎの言葉（「それでは」「続きまして」）のあとの間の上限
# ✅ 2026-10-16 ユーザー：「それではの後も少しだけ間が長い」（0.38秒）。「続きまして」のあと（0.30秒。G-38 の05）は指摘なし
LEAD_SPAN_S = 1.0  # 声の始まりからこの長さの中で切れていれば、つなぎの言葉のあとの間とみなす（「それでは」約0.8秒・「続きまして」約0.9秒）


def long_pauses_ok(text: str, pcm: bytes, rate: int) -> bool:
    """文の途中に長い無音が無いか（✅ 2026-10-15 ユーザー：句読点のような間・テンポの悪さ）。文の切れ目と、頭のつなぎの言葉のあとの間だけは許す。
    頭のつなぎの言葉のあとの間も、長すぎれば落とす（LEAD_PAUSE_MAX_S）。"""
    from phrases import LEAD_IN
    runs = voice_runs(voiced(pcm, rate), 0.05)
    if not runs:
        return False
    gaps = [(a[1] - runs[0][0], b[0] - a[1]) for a, b in zip(runs, runs[1:]) if b[0] - a[1] >= LONG_PAUSE_S]
    if any(text.startswith(w) for forms in LEAD_IN.values() for w in forms) and gaps and gaps[0][0] <= LEAD_SPAN_S:
        if gaps[0][1] > LEAD_PAUSE_MAX_S:
            return False
        gaps = gaps[1:]
    return len(gaps) <= len(re.findall(r"[。？?！!]", text.rstrip("。？?！!、")))


_STAND_IN = "山田"  # 名前の後ろの言葉を、名前に続く形で読むための仮の名前（読みは「やまだ」）


def _part_reading(part: str, after_name: bool) -> str:
    """名前の前後の言葉の読み。名前の後ろの言葉は、仮の名前に続けて読む（⛔ 2026-10-19：「様はいらっしゃいますでしょうか？」だけを読むと
    「様」が「よう」になり、名前の入る文（目的の人を呼ぶ一言）の録音が、正しく読めていても全部落ちた）。"""
    if not after_name:
        return compare_reading(part)
    stand_in = compare_reading(_STAND_IN)
    r = compare_reading(_STAND_IN + part)
    return r[len(stand_in):] if r.startswith(stand_in) else compare_reading(part)


def heard_ok(text: str, heard: dict) -> bool:
    """2通りの聞き方の両方で、読みが合うか（句読点・言い終わりの違いは比べない）。
    名前（「」でくくった所。くくるのは instruction.quote_in_text の1か所）は比べず、名前の前後の言葉だけを比べる。
    ⛔ 見直しで見つけた穴：別の耳は名前を、聞いた音に合う別の漢字で書く（「しょうじ」⇒「庄司」・「アオバ物流」⇒「青葉物流」）か、
       ふつうの読みの漢字で書く（「しんがき」⇒「新垣」＝あらがき）。名前まで比べると、読みのめずらしい名前は正しく読めた録音ほど落ち、
       ふつうの読みで読み違えた録音が通っていた。名前の読みは、人が聞いて選ぶときに確かめる（✅ 2026-10-16 ユーザー：「こちら側の
       担当者名は、依頼者ごとの録音時に録音しておくといいでしょう」）"""
    parts = re.split(r"「[^「」]+」", text)
    if len(parts) == 1:
        want = compare_reading(text)
        return bool(heard) and all(v is not None and compare_reading(v) == want for v in heard.values())
    pattern = ".+?".join(re.escape(_part_reading(x, after_name=k > 0)) for k, x in enumerate(parts))
    return bool(heard) and all(v is not None and re.fullmatch(pattern, compare_reading(v)) for v in heard.values())


def standard_table(answers) -> dict:
    """システムの標準の想定問答の答えの録音の表 {部分の名前: (言葉, 読み方の鍵)}（answers＝(問いの名前, 答えの文) の並び＝CL-2 の save_standard の answers）。
    部分の名前は業務の録音と同じ answer_問いの名前（flowctl.ANSWER_PREFIX）・読み方も同じ（answer）。標準の組（CL-2 の create_set の unit="standard"）は、
    管理者がこの表で作る。業務の録音の表（script_table）は、標準から来た答えを入れない（CL-2 の連絡帳 4-211）。"""
    from flowctl import ANSWER_PREFIX
    return {f"{ANSWER_PREFIX}{name}": (text, "answer") for name, text in answers}


def script_table(business) -> tuple[dict, dict]:
    """依頼主の単位の録音の表 {中の名前: (言葉, 読み方と長さの上限の鍵)} と、{中の名前: その録音で読んだ名前の読み}。
    ✅ 2026-10-16 ユーザー：「こちら側の担当者名は、依頼者ごとの録音時に録音しておくといいでしょう」⇒ 名乗り（自社名・担当者名）と
    用件の一文も、依頼主の単位で録音する（前は質問の文だけ）。名前の読みは、人が聞いて選ぶときに確かめる（別の耳では確かめられない）。
    読みが決まらない名前が入る文は、読みの候補ごとに録音を作る（instruction.reading_variants）。選ばれた録音の読みが、その名前の読みになる。"""
    from flowctl import (ANSWER_PREFIX, DAY_SLOT, DAY_VARIANTS, approval_explanation, callback_question, day_recording_id, holds_pending, may_wish, other_wish_recording_id, pending_line, pending_recording_id, question_forms, recap_recording_id, way_any_line, way_offer,
                         tell_recording_id)
    from instruction import reading_variants
    from phrases import BACK_TO_AGAIN, BACK_TO_MIDWAY, back_fit, question_clip

    items = [("intro", business.opening_parts()[1], "intro")]
    if business.purpose_line:
        items.append(("purpose", business.purpose_line, "purpose"))
    # 相手に聞かれたことへの決まった言い方（「AIですか？」）・留守番電話の伝言・伝言・引き止めの一言（業務が書いたときだけ＝replay_parts）も、依頼主の単位
    # （DESIGN.md 6-13 の3）。業務が書かなかった「AIですか？」の答えは標準＝共通の一言の録音（phrases.STANDARD_PARTS）なので、ここには入らない。
    # 目的の人を呼ぶ一言・確かめる一言は、電話をかける相手ごとに名前が変わるので、ここではなく名前の録音の台帳（DESIGN.md 4-3）
    parts = business.replay_parts()
    items += [(k, parts[k], k) for k in ("ai", "voicemail", "message", "busy") if parts.get(k)]
    # 想定問答の答え（Business.answers）。システムの標準から来た答え（Business.standard_answers）は標準の録音の組（standard_table）から流すので、業務の録音には
    # 入れない（CL-2 の連絡帳 4-211。⛔ それまで、標準の答えまで業務の録音に入れていた）
    items += [(k, text, "answer") for k, text in parts.items()
              if k.startswith(ANSWER_PREFIX) and k[len(ANSWER_PREFIX):] not in business.standard_answers]
    for q in business.questions:  # つなぎの言葉と質問を1本で録音する（つなぎの言葉を決めるのは flowctl.spoken_question の1か所）
        if q.reading_of:
            continue  # 名前の読みの確かめは、まず言ってみる読みが相手ごとに変わるので、AIが言う（録音にしない）
        # 流れの制御が流しうる形ごとに1本（flowctl.question_forms の1か所：次の項目として初めて伺う「続きまして」の形・直前の一言の頭と重なってつなぎの言葉を
        # 替えた形・もう伺った質問を伺い直す「それでは」の形・続きの電話で最初に伺う「確認させていただきたい内容なのですが」の形（✅ 11-06 15時20分30秒）。✅ 2026-11-06 日本時間11時55分13秒 ユーザー「(1) ウ、(2) イ」・12時54分30秒「次の項目の質問に
        # 入る前は、すべて「続きまして」でいいんじゃないですか。」）
        for p in question_forms(business.questions, q.id):
            text, lead, tail = p["text"], p["lead"], p["id"][len(q.id):]
            if DAY_SLOT in text:
                # 日によって変わる言い方（最短の日）の入った質問：言い方ごとに文を丸ごと録音する（✅ 2026-11-03 ユーザー (ア)「OK」。名前は flowctl.day_recording_id）。
                # 替わるのは質問の文の録音だけ（下の伝える一言・ほかのご希望を伺う一言は、ほかの質問と同じ。⛔ 見直しで見つけた：ここで次の質問へ進み、作っていなかった）
                for w in DAY_VARIANTS:
                    items.append((question_clip(day_recording_id(q.id, w) + tail, lead), text.replace(DAY_SLOT, w), question_how(text.replace(DAY_SLOT, w))))
            else:
                items.append((question_clip(p["id"], lead), text, question_how(text)))
        # その答えのあとに伝える一言（業務設定。名前は flowctl.tell_recording_id）
        items += [(tell_recording_id(q, c), line, "question") for c, line in q.tell_words.items()]
        if holds_pending(q):
            # 決められないのがお客様の最終の決定のとき、その質問を締める一言（文は flowctl.pending_line・名前は flowctl.pending_recording_id。✅ 2026-11-06
            # 日本時間11時19分55秒 ユーザー「(2) …「承知しました。それではパンフレットは保留とさせていただきますね。」とパンフレットは締めて、次へ」）
            items.append((pending_recording_id(q), pending_line(q), "question"))
        if may_wish(q):
            # 「どれも要らない」・最終目的への言い切っていないお断りのあと、ほかのご希望を伺う一言（文は flowctl._wish_text の1か所＝W1 の質問の文。
            # 名前は flowctl.other_wish_recording_id）。伺っている途中で切れた電話の続きで、切れた所から伺う一言（again＝「前回お伺いした…の件で…」）も
            wishes = [(other_wish_recording_id(q, again), callback_question("other_wish", of=q, again=again).text) for again in (False, True)]
            if len(way_offer(q) or ()) > 1:
                # ご案内の方法の申し出に、どの方法かは言わずに受けていただいたときの1回の伺い直し（「それではメールとLINEとZOOMのどちらが…」＝flowctl の Flow._wish_step）
                wishes.append((other_wish_recording_id(q, which=True), callback_question("other_wish", of=q, which=True).text))
            # 2回目・3回目からの戻る一言に続けて流す形（phrases.back_fit の1か所＝流す flowctl.question_play と同じ。システムの質問は phrases.BACK_FIT）
            fitted = [back_fit(rid, text, b) for rid, text in wishes for b in (BACK_TO_AGAIN, BACK_TO_MIDWAY)]
            items += [(rid, text, question_how(text)) for rid, text in dict(wishes + fitted).items()]
            if len(way_offer(q) or ()) > 1:
                # 並べて伺ったあとの「どれでも」に、こちらで方法をお伝えする一言（flowctl.way_any_line の1か所。2026-11-07 3時14分01秒 H3 イ）
                items.append((*way_any_line(q), "own_fallback"))
        if approval_explanation(q):
            # 続きの電話の始めに、ご納得いただけなかった点を言っていただけなかったときの振り返りの一言（文は callback_question の recap＝phrases.recap_line の1か所。
            # 名前は flowctl.recap_recording_id）。説明の文の無いご承認の質問には作らない（振り返らずにご納得を伺う＝Flow._point_step）
            items.append((recap_recording_id(q), callback_question("other_point", recap=q).text, "recap"))
    table, readings = {}, {}
    for base, text, how in items:
        variants = reading_variants(text, business.names)
        for k, (read, t) in enumerate(variants, 1):
            aid = base if len(variants) == 1 else f"{base}_v{k}"
            table[aid] = (t, how)
            if read:
                readings[aid] = read
    return table, readings


def words(script: bool) -> tuple[dict, Path]:
    """作る録音の表 {中の名前: (言葉, 読み方と長さの上限の鍵)} と、置き場所。script＝依頼主の単位（試験の業務設定）。"""
    if script:
        from g22_opening import BUSINESS
        return script_table(BUSINESS)[0], SCRIPT_OUT
    return {aid: (text, HOW_OF.get(aid, aid)) for aid, text in RECORDED.items()}, OUT


def usable(m: dict, table: dict | None = None) -> bool:
    """使う録音か（唯一の決まり＝作るとき・試験の通話で流すとき）：音声があり、確かめに通ったか、人が聞いて選んだもの（人が選んだ録音は、
    間の確かめに通っていないものも多い＝ok は偽のまま）。table を渡せば、文がいまの言い方と同じものだけ（⛔ 2026-10-15：文が変わった録音を
    作り済みとして飛ばしていた）。⛔ 2026-10-24 の見直しで見つけた穴：ok だけで見ていたので、人が選んだ録音を、作る道具は「まだ無い」として
    作り直し（台帳の行を、確かめに落ちた行で置き換えうる）、試験の通話（convo.recording_seconds）は「無い」として止めていた。"""
    return (bool(m.get("wav")) and (bool(m.get("ok")) or m.get("chosen_by") == "人")
            and (table is None or m.get("text") == table.get(m.get("id"), (None,))[0]))


def adopt(names: list[str], script: bool, as_type: str | None = None, as_id: str | None = None) -> dict:
    """人が選んだ候補を、使う録音にする（候補の確かめの結果も、そのまま台帳に移す）。
    as_type：別の声の型の録り方で作った候補を、この声の型の録音として使う（同じ声の名前のときだけ）。
    ✅ 2026-10-15 ユーザー：「録音は私がチェックする場合、S4とS2を分ける必要はありません。私が最高に良いと思えるものを選んでいるからです」
    as_id：同じ言葉の別の録音（例：「申し訳ございません、」の重いお詫び＝moushiwake_heavy）として使う（言葉が同じときだけ）。
    ✅ 2026-10-16 ユーザー：「申し訳ございません、02軽いお詫びの時、04重大なお詫びの時」"""
    table, out = words(script)
    index_path = out / "index.json"
    made = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    # 候補の置き場所：この単位の candidates/ と、G-38 の録音（results/voice_match/。✅ 2026-10-15 ユーザーが 06＝A5 を選んだ）
    cands = {r["wav"][:-4]: (out / "candidates", r) for r in json.loads((out / "candidates" / "index.json").read_text(encoding="utf-8"))
             if r.get("wav")} if (out / "candidates" / "index.json").exists() else {}
    # 使っている録音も選べる（✅ 2026-10-23：S2 の「本当にそうですよね！」を、ユーザーが聞き分けた調子の録音＝talk_agree_high として使う）
    cands.update({m["wav"][:-4]: (out, m) for m in made if m.get("wav") and m["wav"][:-4] not in cands})
    for g in sorted(HERE.glob("results/G-38_*.json")):
        cands.update({r["wav"][:-4]: (HERE / "results" / "voice_match", r) for r in json.loads(g.read_text(encoding="utf-8"))["rows"]
                      if r.get("wav") and r.get("ok") and r["id"] in table})
    done = []
    for name in names:
        where, row = cands[name]
        row = {k: v for k, v in row.items() if k not in ("features", "st_from_ref", "cond", "take")}
        target = as_id or row["id"]
        assert target in table and table[target][0] == row["text"], (name, target, row["text"])  # 言葉が違う録音・文が変わったあとの古い候補は使わない
        if target != row["id"]:
            row.update({"made_as_id": row["id"], "id": target})
        if as_type and as_type != row["type"]:
            assert voice_style(as_type)[0] == voice_style(row["type"])[0], (name, as_type)  # 声の名前が違う録音は使わない
            row.update({"made_as": row["type"], "type": as_type})
        pcm, rate = read_wav(where / row["wav"])
        assert ends_in_silence(pcm, rate), name  # 終わりの切れた候補は使わない（候補3）
        path = out / f"{row['type']}_{row['id']}.wav"
        write_wav(path, pcm, rate)
        row.update({"wav": path.name, "chosen_from": name, "chosen_by": "人"})
        made = [m for m in made if (m["type"], m["id"]) != (row["type"], row["id"])] + [row]
        done.append(row)
    index_path.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
    chosen = adopted_readings(done, script)
    return {"adopted": done, "name_readings": chosen}


def adopted_readings(done: list[dict], script: bool) -> dict:
    """選ばれた録音で読んだ名前の読みを、その名前の読みとして残す（依頼主の単位のときだけ。台帳は SCRIPT_OUT/name_readings.json）。"""
    if not script:
        return {}
    from g22_opening import BUSINESS
    return record_readings(done, script_table(BUSINESS)[1], SCRIPT_OUT / "name_readings.json")


def record_readings(done: list[dict], readings: dict, path: Path) -> dict:
    """読みの出どころ＝「録音で選んだ」。⚠️ 同じ名前を、別の録音で別の読みに選んだら止める（読みは名前ごとに1つ。どちらが正しいかを
    人に選び直していただく）。"""
    book = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for row in done:
        for surface, reading in readings.get(row["id"], {}).items():
            old = book.get(surface, {}).get("reading")
            assert old in (None, reading), f"「{surface}」の読みを、前に「{old}」で選んでいます（今回は「{reading}」）"
            book[surface] = {"reading": reading, "source": "録音で選んだ", "chosen_in": row.get("chosen_from")}
    if book:
        path.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    return book


async def run(args) -> dict:
    table, out = words(args.script)
    ids = args.id or list(table)
    assert set(ids) <= set(table), set(ids) - set(table)
    index_path = out / "index.json"
    made = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    # 作り済みでも、文が変わった録音は作り直す（⛔ 2026-10-15：読点を外した質問の文が、作り済みとして飛ばされていた）
    have = {(m["type"], m["id"]) for m in made if usable(m, table)}
    todo = [(vt, aid) for vt in (args.type or VOICE_TYPES) for aid in ids if (vt, aid) not in have or aid in (args.redo or [])]
    if args.candidates:  # 候補は、作り済みかどうかに関わらず、名指しした録音を作る
        todo = [(vt, aid) for vt in (args.type or VOICE_TYPES) for aid in ids]
    if args.dry_run:
        return {"dry_run": True, "todo": todo, "candidates": args.candidates, "words": {a: table[a][0] for a in ids}}
    client = make_client(pick_key(args))
    out.mkdir(parents=True, exist_ok=True)
    if args.candidates:
        return await make_candidates(client, args, table, todo, out / "candidates")
    for vt, aid in todo:
        row = None
        for n in range(args.tries):
            row, pcm, rate = await take(client, args, vt, aid, *table[aid])
            row["tries"] = n + 1
            if row["ok"]:
                path = out / f"{vt}_{aid}.wav"
                write_wav(path, pcm, rate)
                row["wav"] = path.name
                break
        made = [m for m in made if (m["type"], m["id"]) != (vt, aid)] + [row]
        index_path.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")  # 1本ごとに残す（途中で止まっても失わない）
        await asyncio.sleep(args.pause)
    return {"model": args.model, "made": made, "missing": [(m["type"], m["id"]) for m in made if not m.get("ok")]}


# 前置きの一言を付けて読ませ、文の切れ目の無音のところで後ろの文だけを残す録音（声の高さ・速さには手を加えない。切るのは無音のところだけ）。
# ✅ 2026-10-16 ユーザー：「少々お待ち、どれもダメ　理由は同じ　少々とお待ちに区切りがあるのと、いただいても、よろしいでしょうかと区切りがある」
#    （10本とも。区切りは無音ではなく、確かめでは見分けられない）⇒「前に一言付けて自然な流れで読ませ、文の切れ目の無音のところで後ろの文だけを
#    残す」案に「(2)で進めて」。⚠️ 区切りが少なくなるかは、まだ確かめていない（人の耳で選ぶ）
CONTEXT = {"wait": "かしこまりました。", "wait_heavy": "かしこまりました。"}
CUT_GAP_S = 0.15   # 前置きの文と、残す文の間の無音の長さの下限（文の切れ目とみなす）
CUT_KEEP_S = 0.03  # 残す文の声の始まりの前に残す無音（声の頭を切らないため）


def cut_after_context(pcm: bytes, rate: int) -> bytes | None:
    """前置きの文のあとの、最初の文の切れ目（CUT_GAP_S 以上の無音）から後ろだけを返す。切れ目が無ければ None。"""
    runs = voice_runs(voiced(pcm, rate), 0.05)
    for a, b in zip(runs, runs[1:]):
        if b[0] - a[1] >= CUT_GAP_S:
            start = max(0, int((b[0] - CUT_KEEP_S) * rate)) * 2
            return pcm[start:]
    return None


# 声のAIが読み違える言葉の読み（✅ 2026-10-24 ユーザー：番号違いの伺い直しの録音で「『前回』を『せんかい』と言っている」）。声のAIに渡す
# 言葉そのものを、読みのかなに置き換えて渡す（どの録音でも同じ1か所＝take。録音の文と耳の確かめは元の文のまま）。⛔ 2026-10-24 の1回目：
# 指示に「『前回』は『ぜんかい』と読みます」と添えただけでは、作り直しの半分ほどが、まだ「せんかい」と読んだ（ユーザーが聞いて見つけた）。
# ⚠️ 耳の確かめでは見分けられない（聞き取りは、どちらの読みでも「前回」と書いた）ので、人が聞いて選ぶ
# 「LINE」「ID」（LINE の ID を伺う一言・LINE でのご連絡を申し伝える一言＝2026-11-03）：英字のまま渡すと、読みが決まらない
READ_AS = {"前回": "ぜんかい", "LINE": "ライン", "ID": "アイディー"}


def spoken_input(text: str) -> str:
    """声のAIに読ませる言葉（READ_AS の言葉を読みのかなに置き換えたもの。含まなければ、そのまま）。"""
    for w, r in READ_AS.items():
        text = text.replace(w, r)
    return text


async def take(client, args, vt: str, aid: str, text: str, how: str, voice_note: str = "") -> tuple[dict, bytes, int]:
    """1回読ませて、確かめる（両方の耳で読みが合う・声だけの長さが上限以内・文の途中に長い無音が無い・声の途中で切れていない）。
    voice_note＝声そのものの指示（G-38 で比べる。空なら足さない）。
    前置きのある言葉（CONTEXT）は、前置きと一緒に読ませて後ろだけを残し、残した声を確かめる（頭が欠けていないかも、耳で確かめる）。"""
    voice, style = voice_style(vt)
    context = CONTEXT.get(aid, "")
    try:
        inst = (f"{style}{voice_note}\nあなたは音声の材料を作る係です。渡された言葉だけを、{HOW.get(how, DEFAULT_HOW)}、そのまま読み上げます。"
                "言葉を足しません。")
        async with client.aio.live.connect(model=args.model, config=build_config(voice=voice, system_instruction=inst)) as s:
            t = time.monotonic()
            await send_text(s, f"読み上げる言葉：{context}{spoken_input(text)}")
            turn = await collect_turn(s, started_at=t, timeout_s=60)
        pcm, rate = bytes(turn.audio), turn.audio_rate or 24000
        if context and pcm:
            pcm = cut_after_context(pcm, rate) or b""  # 文の切れ目が無ければ、使えない録り方
        seconds = speech_seconds(pcm, rate) if pcm else 0.0
        heard = await hear_feeds(client, pcm, rate) if pcm else {}
        ok = (bool(pcm) and heard_ok(text, heard) and seconds <= MAX_S[how] and long_pauses_ok(text, pcm, rate)
              and ends_in_silence(pcm, rate))
        say(f"{vt} {context}{text}：聞こえた {heard} 長さ {seconds:.2f}秒 合格={ok}")
        row = {"type": vt, "voice": voice, "id": aid, "text": text, "heard": heard, "ok": ok, "seconds": round(seconds, 2)}
        if context:
            row["context"] = context
        return row, pcm, rate
    except Exception as exc:
        return {"type": vt, "id": aid, "text": text, "ok": False, "error": describe_error(exc)}, b"", 0


async def make_candidates(client, args, table: dict, todo: list, out: Path) -> dict:
    """確かめに通った録り方を args.candidates 本ずつ残す（{声の型}_{中の名前}_{番号}.wav）。録る回数は 本数×tries まで。"""
    out.mkdir(parents=True, exist_ok=True)
    index_path = out / "index.json"
    # 台帳は、ほかの声の型・ほかの言葉の候補を残したまま、作り直す分だけ入れ替える
    # （⛔ 2026-10-15：作るたびに台帳を丸ごと書き直していた＝S2 の候補を作ったあと S4 の候補を作ると、S2 の記録が消えた）
    kept_rows = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    kept_rows = [r for r in kept_rows if (r["type"], r["id"]) not in set(todo)]
    rows = []
    for vt, aid in todo:
        for old in out.glob(f"{vt}_{aid}_*.wav"):  # 前の候補は残さない（どれを聞いて選んだか、取り違えないため）
            old.unlink()
        kept, n = 0, 0
        while kept < args.candidates and n < args.candidates * args.tries:
            n += 1
            row, pcm, rate = await take(client, args, vt, aid, *table[aid])
            row["try"] = n
            if row["ok"]:
                kept += 1
                path = out / f"{vt}_{aid}_{kept}.wav"
                write_wav(path, pcm, rate)
                row["wav"] = path.name
            rows.append(row)
            index_path.write_text(json.dumps(kept_rows + rows, ensure_ascii=False, indent=2), encoding="utf-8")
            await asyncio.sleep(args.pause)
    return {"model": args.model, "candidates": rows,
            "kept": {f"{vt}_{aid}": sum(1 for r in rows if r.get("wav") and (r["type"], r["id"]) == (vt, aid)) for vt, aid in todo}}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--tries", type=int, default=3)
    p.add_argument("--pause", type=float, default=2.0)
    p.add_argument("--type", choices=sorted(VOICE_TYPES), action="append")
    p.add_argument("--script", action="store_true", help="依頼主の単位の録音（試験の業務設定の質問の文）を作る")
    p.add_argument("--id", action="append", help="作る録音（中の名前。質問なら Q2 など）。無ければ全部")
    p.add_argument("--redo", action="append", help="作ってある録音でも作り直す（名指ししたものだけ）")
    p.add_argument("--candidates", type=int, default=0, help="確かめに通った録り方を、この本数だけ候補として残す（人が聞いて選ぶ）")
    p.add_argument("--adopt", action="append", help="人が選んだ候補（例 S4_shouchi_2）を使う録音にする（キーを使わない）")
    p.add_argument("--as", dest="as_type", choices=sorted(VOICE_TYPES), help="--adopt：この声の型の録音として使う（同じ声の名前のときだけ）")
    p.add_argument("--as-id", help="--adopt：同じ言葉の別の録音として使う（例 moushiwake_heavy）")
    args = p.parse_args()
    if args.adopt:
        print(json.dumps(adopt(args.adopt, args.script, args.as_type, args.as_id), ensure_ascii=False))
        return
    if args.dry_run:  # 下見は保存しない（G-36 と同じ）
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
        return
    save_result("ACK-CLIPS", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
