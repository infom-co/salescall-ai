"""相手の言葉の2つ目の判定（DESIGN.md 6-10 の (6)。G-53）：相手の言葉が何に当たるかを、会話のAIとは別の文字のAIが決める。
流れの制御（Flow.on_meaning）は、会話のAIの報告と比べて、食い違えばそのまま受け取らずに確かめる。耳の聞き違いを別の耳で確かめる
（6-10 の (5)）のと同じ形で、意味の取り違えを、別の判定で確かめる。1つの判定で、次の3つの場面を受け持つ（場面で選べるものが変わるだけ）：
- 質問への答え：質問の選択肢（選択肢の質問）／「答えた」（値を聞く質問）・どちらでも・わからない・答えたくない
- 言い直しへの返事（asked＝こちらが言い直した言葉）：選択肢の質問は、相手がどの意味を認めたか（「はい」でも、言い直した意味の選択肢）。
  値を聞く質問は「合っている」か「違う値を言われた」か
- 答えでない言葉（どの場面でも）：取り次ぎを待つように言われた・代わった人が話し始めた・決められない

⛔ 2026-10-17 ユーザー：「精度は100％にする仕組みを作らないとだめですよ。間違えるのが当たり前でスルーしてるだろ！」
   ⇒ 記録の通話で、会話のAIが意味を取り違えたまま、流れの制御が確かめずに受け取った答えが12回あった（「まだ決めてないです」⇒
   「どちらでも」5回・「どちらでも大丈夫です」⇒「カード」など5回・「いや、これからなんです」「家まだです」⇒「わからない」2回）。
   どれも聞き取りは正しく、別の耳では拾えない（私は「別の耳では直せません」と書いて、そのまま流していた）。
- 判定に渡すのは、質問の文・こちらが言い直した言葉（言い直しへの返事のとき）・選べるもの・相手の言葉だけ。
  会話のAIの選んだ答えは渡さない（引きずられないように＝独立した判定）。
- 選択肢は、**意味の言葉を名前にして**見せる（取込で決めた Question.meanings。無ければ選択肢の名前）。
  ⛔ 2026-10-18 G-53 の2回目：選択肢の名前（「はい」「いいえ」）で見せていたら、「念のためご予約の日時に変更がおありということでよろしいでしょうか？」
     への「違います」を「いいえ」（＝変更がおあり）と読んだ（2回）＝言い直しを認めていないのに、認めたと読む向き（誤った答えが渡る向き）。
     「違います」と、名前の「いいえ」を、言葉で結びつけた。
"""
from __future__ import annotations

import json

from flowctl import (ABOUT, ANY, ASKS_MEANINGS, BEFORE, CHOICE_TYPES, CONFIRM_LABEL, HANDOVER, HOLD, NO_BEFORE, PHONE, RETURN_MEANINGS, SPEAKER,
                     UNDECIDED, WHO, called_words, facts_reading, label)

# ⛔ 2026-10-31（G-53 の答えの段の外れを1つずつたどった・PHASE0 G-59）：「どちらでも」を言葉で書き、事実を聞く質問の「該当する区分で処理して
#    ください」まで選んだ・「答えたくない」を「拒んだ」だけで書き、選ぶのを避けた「指定はしたくありません」（✅ 2026-10-30 ユーザー：会話が成り立って
#    いない）や、会話になっていない「差し控えさせていただきます」まで選んだ・「おすすめの方で」（任せた）を選ばなかった ⇒ 意味で書いた
SYSTEM_DESC = {ANY: "こちらに任せた・どれでもよい・希望は無い、と言った（選ぶのを頼んだときも。事実を聞く質問には無い。選ぶのを避けただけの言葉は入れない）",
               "unknown": "わからない・覚えていない・確かめていない・まだ決めていない、と言った（選ぶ前の状態。言ったことそのものが選べるものにあれば、そちら）",
               "refused": "この質問に答えることそのものを、はっきり拒んだ（知らないとは言わずに、答えを言わない・言えない、と言ったのも。どの答えを選んでも"
                          "要ることを言うのを拒んだのも。申し出を断ったのは、断る側の答え。選ぶこと・指定することを避けただけの言葉は入れない）"}
# ⛔ 2026-10-31：取り次ぎを待つを「取り次ぐので」と書いていた＝「確認してまいります。少々お待ちください。」を「決められない」と読んだ（待つ側と同じ）
# ⛔ 2026-10-31 早朝（直した文の測り・G-53bf_fix）：上の「確かめてくる など」で、あとで家の人に聞く必要があると言っただけの「ちょっと家の人に
#    聞いてみないと」まで、Jev が待つように言ったと読んだ（10-25 の文では正しく「わからない」）＝分かれ目（この電話の間に確かめてくるので待つ／
#    あとで確かめる）を書かずに例を足した、私の直しの外れ ⇒ 分かれ目を意味で書いた（正解の表の「取り次ぎを待つ」と「決められない」は、この
#    分かれ目ですべて分かれる）
KIND_DESC = {HOLD: "この電話の間に、取り次ぐ・確かめてくるあいだ、こちらに待つように言った（待ってとは言わなくても、いま確かめてくる、と言ったのも。"
                   "答えではない）",
             # ⛔ 2026-10-21 G-59（3.5 Flash Lite・前の言葉つき）：「お電話代わりました。山本です。」を3回中1回「ご本人」と読んだ ⇒ 違う名前は
             #    こちら、と書き足した（書き足したあとは3回とも正しい）。（「担当の者に代わります」のあとの「ご本人」は誤りではなかった＝✅ 2026-10-21 ユーザー：「担当の者に代わりますの後に、はい、お電話変わりましたと出れば、ほぼ『本人』でしょう」（「代わった人」でも正しい）。
             #    「お待たせいたしました。」だけを取り次ぎと読む誤りは、説明を書き足しても直らなかったので外した＝繋がない側の誤り）
             # ⛔ 2026-10-19 G-57・G-59：「お電話代わりました。岡田です。」（お呼びした方の名前を名乗った）を、いまの判定は6回とも、こちらに読んだ
             # ✅ 2026-10-19 ユーザー：「受付が『それでは岡田にお繋ぎします。』のような後に、『はい、お電話代わりました。』と言われれば、
             #    『ご本人』になりますよね」⇒ お呼びした方に取り次いだあとなら、名乗らなくても答えが決まる（その前に相手が言ったこと＝BEFORE）
             # ⚠️ 同じ日のユーザー（2回目）：「取次後は必ずしも、本人とは限らない」⇒ ここは「代わった別の人が話し始めた」ときだけの話。取り次いだ方が
             #    また出て出られないと伝えたときは不在、どちらが出たか分からない言葉はご本人と決めない（決まりの正本＝flowctl.WHO_CALLING）。
             # ⚠️ 2026-10-29 から、目的のお客様の判定はこの説明を使わない（項目ごとに答える＝flowctl.target_facets）。答えの判定（G-53）の文を
             #    変えないために、ここはそのまま残す
             # ⛔ 2026-10-31（G-53・d1-ultra）：言い直しのあとに電話を代わった方の「はい、〇〇でございます。お待たせいたしました。」を、頭の「はい」で
             #    言い直しを認めたと読んだ（確からしさ 0.69）＝話し始めた言葉がどんなものかを書いていなかった ⇒ 意味で足した（言葉の形の例は足さない）
             HANDOVER: "電話を代わった別の人が、話し始めた（「お電話代わりました」など。名乗る・待たせたことを詫びるなど、電話に出たあいさつで、頭の"
                       "「はい」もあいさつの「はい」。答えではない）。お呼びした方がいないので別の者に代わると"
                       "言ったあとに代わった方が出たとき・お呼びした方と違う名前を名乗ったときも、こちら。代わった方が誰かで答えが決まるとき"
                       "（お呼びした方の名前を名乗った・その前に取り次いだ方が、お呼びした方に取り次いだ）は、こちらではなく、その答えを選ぶ",
             # 2026-10-25 G-53：言い直しへの「確認しておきます。」（これから確かめるだけ）を、考えない Sonnet 5.5 が答えと読んだ ⇒「答えになっていない」の例に足した。
             # ⛔ 同じ日に「聞いたことと違う範囲やもの（親会社・ほかの支店など）について言っただけ」も足したが、取り消した（✅ 2026-10-28 ユーザー：
             #    「『グループ全体だと50名超えますが…』を、4つの判定がそろって『50名以上』と読むことに何の問題があるんですか？人が見ても、その部分だけ
             #    見せられたら『50名以上』と読むと思いますよ」）＝言い直した値に寄っていて言い切っていない返事は、その値に曖昧の印（10-18 のご決定）。
             #    私（CL-1）が付けた試験の正解「決められない」が誤りで、それを穴と呼んで決まりを足し、判定を「決められない」（聞き直す側）へ悪くしていた
             # ⛔ 2026-10-31（真因 (A) と同じ）：「『大丈夫です』『結構です』だけなど」と言葉で書いていた＝申し出への「いいえ、結構です」「送付は結構です」
             #    まで「決められない」と読み、聞き直していた（✅ 2026-10-17 ユーザー：「人でも前の質問や会話から判断する言葉です」＝前の質問で決まれば、
             #    その意味）。✅ 2026-10-30 ユーザー：「差し控えさせていただきます」は、本当にそう言ったなら会話になっていない（聞き取りの誤りも考える）
             # ⛔ 2026-10-31（G-53・d1-ultra）：駐車場の質問への「差し控えさせていただきます」を「利用しない」（0.94）と読んだ＝答えるのを控えたのか、
             #    利用を控えるのか決まらない言葉なのに、その決まらなさを書いていなかった ⇒ 意味で足した。✅ 2026-10-30 ユーザー：「お話しすることは
             #    ありません」は「前の会話や…によって、どのパターンもあり得る」（前の会話は「前の会話からは」で読む）
# ⛔ 2026-10-31 未明（最後の文の Jev の外れ・d1-ultra の外れを、なぜを真因までたどった）：足した「控える言葉」が広すぎ、はっきりした断り
#    （「手元にあるかどうかは言えません」）まで「決められない」に入れた・駐車場の「差し控え」は d1-ultra が「利用しない」のまま（決まらないのは、
#    控えたのが答えか利用か、の1点なのに書いていなかった）⇒ その1点で書いた。電話を代わった方の「お待たせいたしました、ご用件を…」を「答えに
#    なっていない」にも当てた（説明が重なっていた）⇒ 待つ・代わった人に当たるときはそちら、と書いた。「言えません」「お答えできません」を
#    「わからない」と読み、「宛先を教えたくありません」から「メール」を推し量った ⇒「答えたくない」に、知らないと言わずに言わない・言えない、
#    どの答えにも要ることを拒んだ、を足した
# ⛔ 2026-10-31 の昼（✅ ユーザー：「『差し控えさせていただきます』だけだと『利用しない』になるのは当然。この部分だけの判断ならAIがあっています。
#    前の分との流れの回答として会話になっていないだけです。それを分かるようにしないと！」）：言葉の意味と、前の流れへの返事として会話になって
#    いるかの2つの軸を、この「決められない」1つに混ぜていた（10-29 の「不在」と「別の方」と同じ形）＝判定は、正しく意味のほうを選んでいた
#    ⇒「意味がつながらない・聞き違い」と「控えたのが答えか利用か」はここから外し、会話になっているかは別の項目で答えさせる（FITS・答えの段だけ）
             UNDECIDED: "この言葉と、前の質問・前の会話からは決められない（受け入れにも断りにも取れる・どの答えも指していない・選ぶのを避けた・"
                        "話すことを終える言葉とも、質問への答えとも取れる・確かめる・調べる・人に聞く、と言っただけ（いま確かめてくるので待つように、"
                        "とは言っていない）。待つように言った・代わった人が話し始めた、に当たるときは、そちら）"}
# 電話のやり取りを読む問い合わせ（尋ねているか・待たせた方が戻ったか＝TALK_KINDS）の「決められない」：お客様の答えの説明（上）は入れない
#    （✅ 2026-10-31 ユーザー：(4) の案「OK」＝答えの段の説明を直すたびに、この2つの問い合わせの文が変わっていた・文の約30%が答え用だった）
TALK_UNDECIDED = ("この言葉と、前の会話からは決められない（聞き違いもありうるので、近い言葉に読み替えない。待つように言った・代わった人が"
                  "話し始めた、に当たるときは、そちら）")
CONFIRM_DESC = {"affirmed": "こちらが言い直した値で合っていると認めた", "corrected": "違う・直した（別の値を言った）"}

# 言い切っているか（✅ 2026-10-18 ユーザー：「『急ぎと言えば急ぎですが』『まあ、できれば早くとは思います』『さらっとは見たんですが』
#    『していないはずですが…』『5万円くらいかなとは思いますが』なんかは、『yes,but 曖昧』のような返答にしておけばいいんじゃないですか？」）。
#    寄っている答えは選び、言い切っていないことは別に答えさせる（それまでは「決められない」に入れていた＝寄っている向きが消えていた）。
#    流れの制御は、最初の答えなら言い直して確かめ、言い直しへの返事なら、その答えに「曖昧」の印を付けて受け取る（Flow._hedged）
FIRM = "firm"
# ⛔ 2026-10-18 の1回目：例に「〜くらい」「〜程度」を入れ、「〜と思うのですが」と「〜と思います」を分けなかった ⇒ 最初の答えの「30名くらいです」
#    「20名ほどおります」「来月以降になると思います」まで言い切っていないと読み、要らない確かめが約80個増えた（Sonnet 5.5・Haiku 5.5 の考える形。
#    「と思います」は言い切りの柔らかい形＝✅ 2026-10-17 ユーザー確定の言い切っていない答えの一覧（flowctl.UNCERTAIN）にも入れていない）
# 寄っているか・言い切っているか（Claude・Gemini の決まり FIRM_RULE・判断のモデルの定義 JEV_FIRM・選択肢の定義 jev_criteria を、ここから作る＝1か所）。
#    ✅ 2026-10-30 ユーザー：「何が問題で間違えたのかを徹底的に洗い出し、どうすれば改善できるのかを考えないといけないんじゃないですか？」
#    ⇒ G-53 の答えの段で判定が外れた言葉を1つずつたどった真因（PHASE0 G-59 の「なぜを真因まで」）：
#    ⛔ (A) 言い切っていない形を言葉の形（「〜とは思うのですが」など）で書いていた＝判定は形で探し、続けて頼む前置きの「が」・言い切った答えの「〜て」
#       まで言い切っていないと読み、言わずに残した逆の事情（「〜ほど助かりますが」）を言い切りと読んだ
#    ⛔ (C)「寄っていればその答えを選ぶ」と書き、何に寄っているかを決めていなかった＝確かめていないと言っただけの言葉（「まだ確認できてないです」）から、
#       事情を推し量って答えを選んだ。✅ 2026-10-31 ユーザー（d1-ultra の読みが正しい）：「手配中と聞いていますが」は予約されているに寄って言い切って
#       いない＝人から聞いたこと・途中のことも、答えを指していれば寄っている（私の案「人から聞いたことからは選ばない」は誤りだった）
#    ⇒ 意味で書き、言葉の形の例は書かない（⚠️ 例を書くと、判定はその形を探す）
#    ⛔ 2026-10-31 未明（最後の文の Jev の外れをたどった）：「推し量り」「まだ途中のこと」を何の推し量りか書かず、自分の予定を柔らかく言った
#       「来月以降になると思います」（10-18 の測り：言い切りの柔らかい形）・途中であることそのものが答えの「候補日を調整中でして」（決まって
#       いない）まで言い切っていないと読んだ。答えの理由を言って止めた「実は都合が悪くなりまして」・もう一方を打ち消した「現金振り込みは
#       使いたくないです」も、言い切りとして書いていなかった ⇒ 推し量りは人や物・出来事の様子のこと・途中のことから推し量った答え、と書き、
#       言い切りに、その4つの意味を足した
#    ⛔ 2026-10-31 の昼（G-70：「複合機は去年買い取りました。」のあとの「そうです。」）：答えを「お客様が言ったこと」と書き、いまの言葉か前に
#       話したことかを決めていなかった＝d1-ultra も Jev も、前に話したことから答え（リースではない）を選んだ ⇒ 答えは、いまの言葉から選び、
#       前に話したことは、いまの言葉がそれを指しているときに中身を補うだけ（食い違いは会話になっているか＝FITS で答える）
LEAN = ("お客様のいまの言葉が答えについて言ったこと（その答えだと言った・そう思っている・そう聞いている・そう望んでいる、または、言ったことから"
        "必ずその答えになること。" + BEFORE + "は、いまの言葉が前に話したことを指している・省いて言ったときに、その中身を補うためだけに使う）")
FIRM_NOT = ("答えに寄っているが、答えそのものを弱める・覆しうることを残している（人や物・出来事の様子を推し量った・人から聞いたこと・まだ途中のことから"
            "推し量った・条件・言わずに残した逆の事情・質問が聞いている時と違う時のこと・質問より狭い範囲だけの答え）")
FIRM_YES = ("質問が聞いていることについて、答えを決めている（柔らかい言い方・自分の望みや予定を柔らかく言った・およその数・まだ途中であることそのものが"
            "答え・答えの理由を言って止めた・続けて頼むことの前置き・答えの外のことへの含みがあっても、言い切っている。言ったことから必ずその答えになる"
            "（選べるもう一方を打ち消した など）ときも、言い切っている）")
FIRM_RULE = ("答えは、" + LEAN + "で選びます。確かめていない・知らない、と言っただけで、どの答えも指していない言葉から、事情を推し量って答えを"
             "選びません。言い切っているか（" + FIRM + "）は、" + FIRM_NOT + "なら false、" + FIRM_YES + "なら true です。")
# 会話になっているか（答えの段だけ。言い切っているかと同じく、選んだ答えとは別に答えさせる＝Claude・Gemini の決まり FITS_RULE・判断のモデルの
#    定義 JEV_FITS を、ここから作る）。✅ 2026-10-31 の昼 ユーザー：「『差し控えさせていただきます』だけだと『利用しない』になるのは当然。この部分
#    だけの判断ならAIがあっています。前の分との流れの回答として会話になっていないだけです。それを分かるようにしないと！」（10-30：「本当にお客様が
#    『差し控えさせていただきます』なら会話になっていない。文字起こしが間違っている可能性も考えるべき」）。流れの制御は、会話になっていないと読めば、
#    受け取らずに念のため確かめるか聞き直す（flowctl.TALK_WHY・Flow._check_again）
#    ⛔ 2026-10-31 の昼（G-70・G-53 で測った）：分かれ目を「流れがつながらない」としか書かなかった ⇒ (1) 前に言ったことと逆の返事（「車で伺うので、
#       駐車場を使わせてください。」のあとの「差し控えさせていただきます」）を、d1-ultra も Jev も6回とも会話になっていると読んだ (2) 答えを拒んだ・
#       理由だけを言った・わからない・迷った・待つように言った返事を、前の会話の無い試し（G-53）で d1-ultra は124・Jev は120、会話になっていないと
#       読んだ（本当に会話になっていないのは、記録の通話の聞き違いの言葉だけ）⇒ 両側を意味で書いた（言葉の形の例は書かない）
FITS = "fits"
FITS_NOT = ("いまの言葉が、前の質問への返事として筋が通らない（質問と関わりのないことを言った・聞き違いや言い違いでなければ出てこない言葉）か、"
            + BEFORE + "と合わない（前に言ったことと逆のことを、考えを変えたとは言わずに言った）。聞き違い・言い違いを疑う")
FITS_YES = ("前の質問への返事として筋が通り、" + BEFORE + "とも合う（答えを言わない返事＝わからない・答えたくない・理由だけを言った・迷った・"
            "聞き返した・待つように言った・代わった人が話し始めた、も、質問への返事として筋が通っていれば、会話になっている。前に言ったことを変えると"
            "言って答えたのも、会話になっている）")
FITS_RULE = ("会話になっているか（" + FITS + "）は、" + FITS_NOT + "なら false、" + FITS_YES + "なら true です（答えは言葉の意味で選び、"
             "会話になっているかは、ここだけで答えます）。")


class Reading(str):
    """判定の答え（中の名前のまま比べられる）に、お客様が言い切っているか（firm）を添えたもの。sure＝判定の確からしさ（返す判定だけ＝Jev）・
    by＝答えた判定の名前・tried＝その前に問い合わせて答えが無かった判定（JudgePanel が付ける）。
    項目ごとに答える質問（Question.facets＝目的のお客様の判定）では、中の名前は組み合わせた読み（flowctl.target_reading）で、away・new＝読みに
    出ない事実の印（不在も伝えた・電話を代わった方）・successor＝はじめの場面で、電話に出ている方は後任の方だと読んだ印（flowctl.facts_reading）・
    facts＝項目ごとの答え（記録・試験の数え方）。weak＝受け持ちの判定がどれも確かな読みを返さず、代わりが答えたときの、受け持ちの確かでない読み
    （流れの制御は、代わりの答えとあわせて数える＝convo.check_handler・Flow.on_unsure）。firm_p＝言い切っているかの確からしさ（0〜1。返す判定
    だけ＝判断のモデル。firm は、これを線 firm_min で分けたもの。線と、どちらとも言えない幅を決める測りが使う）。
    fits＝お客様の言葉が、前の質問（と前の会話）への返事として会話になっているか（答えの段だけ。FITS）・fits_p＝その確からしさ（判断のモデル）。"""
    sure: float | None = None
    firm_p: float | None = None
    fits: bool = True
    fits_p: float | None = None
    by: str | None = None
    tried: tuple = ()
    weak: str | None = None
    away: bool = False
    new: bool = False
    successor: bool = False
    facts: dict | None = None

    def __new__(cls, cond: str, firm: bool = True, sure: float | None = None):
        r = super().__new__(cls, cond)
        r.firm, r.sure = bool(firm), sure
        return r


# 打ち消しの形の言い直しへの「違う」（Claude・Gemini の決まり RULES と、判断のモデルの問い JEV_ASK_REPLY の両方が、ここから作る＝1か所）。
#    ⛔ 2026-10-31（G-53）：「念のため…ではないということでよろしいでしょうか？」への「多分違うとは思うんですが」を、2つの Sonnet が認めた側と
#    読んだ（受け取ると誤った値が渡る）。✅ 2026-10-30 ユーザー：「この質問に『違う』と答えること自体が問題でしょ。『違う』と言われたら、会話に
#    なっていないので、言い回しを変えて、再度、聞き直すのが正しいでしょう」＝打ち消した側でも決められないでも、流れの制御は聞き直す。
#    ⛔ 同じ日：この一文を RULES にだけ書き、判断のモデルの問いに入れていなかった＝d1-ultra が同じ言葉を認めた側と読んだ（決まりが2か所）
NEG_REPLY = "「〜ではない」の言い直しへの「違う」が、どこを打ち消したのか決まらないときは「" + UNDECIDED + "」で、認めた側は選びません。"

# 判定の決まり（どの会社の判定にも同じ文で渡す。Jev のように、決まり・場面・選べるものを別々に受け取る判定には、この部分を問いとして渡す）
ASK_HEAD = "電話でお客様に質問をして、お客様が話しました。お客様の言葉が、次の選べるもののどれに当たるかを、1つだけ選んでください。"
MEANING_RULE = "- 言葉の意味で選びます。言葉の頭の「はい」「いいえ」だけで決めません"
MAYBE_RULE = "- 迷ったら「" + UNDECIDED + "」を選びます。\n"
RULES = (
    ASK_HEAD +
    "あわせて、お客様がその答えを言い切っているか（" + FIRM + "）と、質問への返事として会話になっているか（" + FITS + "）も答えてください。\n"
    "決まり：\n" +
    MEANING_RULE + "（「いいえ、もう済ませてあります」は、済ませた側）。\n"
    "- 打ち消しを重ねた言い方は、意味で読みます（「していないわけではない」は、している側）。\n"
    "- 「〜ございませんでしょうか？」のような打ち消しの問いへの「はい」「いいえ」は、選べるものの名前（意味）で、どちらの意味かを決めます。\n"
    "- こちらが答えを言い直して確かめたあとの返事は、お客様が認めた意味で選びます（言い直した意味を「はい」と認めたら、その意味）。\n"
    # ⚠️ 例は、試験の言葉（G-14・G-52）と同じ言い方にしない（同じだと、試験で測った正しさが高く出る）
    # ⛔ 2026-10-18：言い切っているかを足した形で、Claude Sonnet 5.5（考える）が、打ち消しの言い直し（「〜ではないということで…」）への
    #    打ち消しの返事を、言い直した意味のまま読んだ（2回。受け取ると誤った値が渡る向き）⇒ 打ち消しの返事の決まりを足した。言い直しへの
    #    「違うだけ」「認める」の返事 552個で、危ない読み 2 ⇒ 0・正しい 539 ⇒ 551（⚠️ 同じ答えで決まりを作って測った＝高めに出うる）
    "- 言い直しを打ち消す返事（違う・そうではない、と言う）は、言い直した意味を打ち消した側を選びます（言い直したのが「〜ではない」の意味なら、"
    "「〜である」の側。値を言い直したときは「" + CONFIRM_LABEL["corrected"] + "」）。" + NEG_REPLY + "\n"
    # ⛔ 2026-10-25 に「『いえ』『いや』のあとの『結構です』『大丈夫です』は、その向き」の一文を足したが、取り消した：G-59 で、確かめたあとの
    #    「いえ、受付の者です。少々お待ちください。」を、考えない Sonnet 5.5 が3回とも「別の方」と読むようになった（前は「取り次ぎを待つ」）＝
    #    「いえ」の一文が、結構です・大丈夫です以外にも効いた
    "- " + FIRM_RULE + "\n"
    "- " + FITS_RULE + "\n" +
    MAYBE_RULE
)
# 電話のやり取りを読む問い合わせ（TALK_KINDS＝尋ねているか・待たせた方が戻ったか）の決まり：お客様の答えの決まり（打ち消しの問い・言い直しへの
#    返事・言い切り・会話になっているか）は入れない（✅ 2026-10-31 ユーザー：(4) の案「OK」。rules の1か所で選ぶ）
TALK_RULES = ASK_HEAD + "\n決まり：\n" + MEANING_RULE + "。\n" + MAYBE_RULE
# 項目ごとに答える質問（Question.facets＝目的のお客様の判定）の決まり（RULES の代わり。✅ 2026-10-29 ユーザー：「『不在』と『別の方』というのは、
#    そもそも別の評価軸でしょ。それをどちらかしか選べないというのは設計上の問題じゃないですか？」＝1つの言葉に同時に入る事実を、項目ごとに答えさせる）。
#    ⚠️ 例は、試験の言葉（G-59）と同じ言い方にしない
NOW_ONLY = "お客様の言葉（いまの言葉）で言ったことだけで選びます"


def facet_rules(q) -> str:
    """項目ごとに答える質問の決まり（問い合わせの文の1か所。RULES の代わり）。場面で変わる所（いまお呼びしている方の呼び方）は、項目と同じ
    1か所（flowctl.called_words）から作る。後任の方の場面だけの言い分けは、話している方は誰かの見分け方（flowctl.target_facets）にだけ書く。
    ⚠️ 例は、試験の言葉（G-59）と同じ言い方にしない。決まりの文に、選べるものの説明の条件を写さない（⛔ 2026-10-30：条件を一部だけ写して2回
    行き過ぎた＝帳面 0-2 の21回目の失敗）。
    ✅ 2026-10-29 ユーザー：「『不在』と『別の方』というのは、そもそも別の評価軸でしょ。それをどちらかしか選べないというのは設計上の問題じゃ
       ないですか？」＝1つの言葉に同時に入る事実を、項目ごとに答えさせる
    ⛔ 2026-10-29 G-59：取り次ぎのあとの言葉で、その前の「少々お待ちください。」を、いまの言葉の「取り次ぎを待つ」と読んだ＝どの項目を、いまの
       言葉だけで読むかを書いていなかった ⇒ 下の4行目。後任の方の場面の「田中はただいま席を外しております。」は、田中様が誰かを結ぶのに前の
       言葉が要る ⇒ 5行目（いまの言葉の中の人が誰かも、その前の言葉で結ぶ）
    ⛔ 2026-10-30 G-59：Luna は、取り次ぎのあとの「はい、お電話代わりました。」を、前の言葉を見せても「その前の言葉は相手が言ったことなので、
       ご本人かどうかの判断には使いません」と読んだ＝4行目の「いまの言葉だけ」を話している方にも当てていた ⇒ 4行目と5行目を項目ごとに分けた。
       考える Haiku は、確かめるきっかけの「はい。」を確かめた問いへの答えと取り違えた ⇒ 言葉の決まりの「その前に相手が言ったこと」（確かめる一言への返事の場面だけ＝flowctl.facet_terms。⛔ はじめは5行目の終わりに、どの場面にも書いていた）。
       「迷ったら決められない」と書いていたので、Luna は説明で決まる言葉でも「決められない」に逃げた ⇒ 見分け方の「決められない」を選ぶとき"""
    P, _ = called_words(getattr(q, "seeks_successor", False))
    return ("電話でお呼びする方を、質問の言葉でお呼びして、電話に出ている方が話しました。その言葉について、次の項目ごとに、選べるものを1つずつ"
            "選んでください。\n"
            f"1つの言葉に、いくつもの事実が入ることがあります（{P}ではない方が、{P}は出られないと伝えた・{P}は出られないのでほかの者に代わると"
            "言った など）。項目ごとに、その項目のことだけを読みます（ほかの項目に当たる事実があっても、この項目の答えは変えません）。\n"
            "言葉の決まり（説明・見分け方・決まりの中の言葉）：\n" + "".join(f"- {t}\n" for t in q.terms)
            + "決まり：\n"
            "- 言葉の意味で選びます。言葉の頭の「はい」「いいえ」だけで決めません。\n"
            "- 話している方は誰かは、言葉から言える段まで選び、2つの答えの境目は、その項目の見分け方で決めます。\n"
            f"- 電話のやり取りと、{P}の事情は、{NOW_ONLY}（その前に相手が言った取り次ぎ・不在などの事実は、いまの言葉の答えにしません）。\n"
            "- 前と同じ方かと、話している方は誰かは、その前に相手が言ったことも合わせて決めます（説明と見分け方の中の「その前の言葉で」「取り次ぐと"
            "言われたあとに」などは、その前に相手が言ったことに当てます。いまの言葉の中の人が誰かも、その前の言葉で結びます）。\n")


# いまの言葉だけで読む項目（facet_rules の4行目と、Jev の問い＝jev_request が同じ文を使う。Jev には決まりの文を渡さないため）
NOW_ONLY_FACETS = (PHONE, ABOUT)
TERM_FACETS = (SPEAKER, WHO)   # 説明に言葉の決まり（flowctl.facet_terms）の言葉を使う項目（Jev の問いの指示に、言葉の決まりを入れる）


def facet_choices(q) -> str:
    """項目ごとに答える質問の、項目と選べるもの（問い合わせの文の1か所。見せる名前＝中の名前）。"""
    lines = ["項目と選べるもの（名前：説明）："]
    for i, f in enumerate(q.facets, 1):
        lines.append(f"({i}) {f.heading}（{f.key}）：")
        lines += [f"- {n}：{d}" for n, d in f.choices.items()]
        if f.notes:   # 2つの答えの境目（1つの境目は1回だけ＝flowctl.Facet）
            lines.append("見分け方：")
            lines += [f"・{n}" for n in f.notes]
    return "\n".join(lines) + "\n"


PROMPT = RULES + (   # 並びの見本（文を作るのは prompt_parts の1か所）
    "質問：「{question}」\n"
    "{before}"
    "{asked}"
    "選べるもの（名前：説明）：\n{choices}\n"
    "お客様の言葉：「{text}」\n"
)
# ⛔ 2026-10-29（✅ ユーザー「(1) で進めてOKです」で試した）：毎回同じ部分（決まり・質問・選べるもの）を先に並べ替え、お客様の選択肢の言葉の
#    名前も「答えA」…にした形は、G-53 の 2,817件で危ない向き 40 → 42・言い直しを認めたと誤読 5 → 6（「なるべく早く導入したいと思ってます」
#    を2つの Sonnet とも3回中3回誤読。前の名前に戻すと3回中3回正しい・並べ替えだけでも「50名に近いのですが…」を3回中2回誤読）⇒ 文は前の
#    並びのまま、名前は印のある質問（Question.named）だけ短くする。まとめて送る区切りは、毎回同じ「決まり＋質問」の後ろ（prompt_parts）


# ⛔ 2026-10-18 取り下げた決まりの案：「言い切っていない返事（『〜ですが…』『できれば〜』『〜かな』『〜はず』『〜くらい』）は決められない」。
#    ユーザー：「『メールのほうが助かりますが、電話でも大丈夫です』なら、メールは優先でOK…決まらないではないですよね」＝望みが言えていれば答え。
#    案は逆の向きだった（測った「直った」は、作らせたモデルの誤った正解に合わせただけのものを含む。PHASE0 G-53 の3回目）。


def options(q, asked: str | None = None) -> list[tuple[str, str, str]]:
    """選べるもの（判定に見せる名前、説明、中の名前）。選択肢の質問と、値を聞く質問の最初の答えは Question.all_conditions の並び
    （宣言した選択肢は、意味の言葉を名前にする。無い・ほかの名前とかぶるときは選択肢の名前）。選択肢がこちらが決めた意味の名前の質問
    （Question.named＝目的のお客様か・確かめへの返事・尋ねているか・待たせた方が戻ったか）は、その意味の名前（「ご本人」「別の方」「不在」など）
    を名前にし、意味の定義を説明に1回だけ書く。⛔ 2026-10-29：名前を「答えA」…にした形は、その場の判定（3.5 Flash Lite）が、取り次ぎのあとの
    「お電話代わりました。」を3回とも「代わった人が話し始めた」と読むなど悪くなった（G-59 の両方で読めた128行で 112 対 118＝食い違いが要らない
    確かめになり、お客様の不信になる。✅ ユーザー：「『確かめが1回増える』のは安全でなく、相手からの不信ですよ！」）⇒ 意味の名前に戻した。
    意味の名前で「いいえ、山田と申します。岡田は本日おりません。」を「別の方」と読んだのは、1つの言葉の2つの事実（違う方・お呼びした方は不在）を
    1つの選びに押し込めていたため ⇒ 目的のお客様の判定は、項目ごとに答える形にした（Question.facets・prompt_parts）。
    値を聞く質問の言い直しへの返事は「合っている」「違う値を言われた」（AIの報告 confirm_field と同じ名前）。最後に、答えでない言葉の3つ。
    ⛔ G-53 の2回目：名前の「いいえ」と相手の「違います」を言葉で結びつけ、言い直しを認めていないのに認めたと読んだ ⇒ お客様の選択肢の言葉は名前にしない。
    ⛔ 2026-10-29：印のある質問も意味の定義（長い文）を名前にしていたので、返事の形の指定にも長い文が並び（目的のお客様の判定で送る量の55%）、
       「ご本人」と答えるたびに長い文（約400トークン＝入力の5倍の値段）を書き出していた"""
    if q.type in CHOICE_TYPES or asked is None:
        taken = {label(c) for c in q.all_conditions if c not in q.conditions} | set(KIND_DESC)
        head = []
        for c in q.all_conditions:
            if c in q.conditions and getattr(q, "named", False):
                head.append((label(c), q.meanings.get(c) or f"「{c}」と答えた", c))
            elif c in q.conditions:
                name = q.meanings.get(c)
                if not name or name in taken:
                    name = label(c)
                taken.add(name)
                head.append((name, "この意味の答え" if name != label(c) else f"「{c}」と答えた", c))
            elif not talk_question(q):   # 電話のやり取りを読む問い合わせには、お客様の答えの「わからない」「答えたくない」を並べない
                head.append((label(c), SYSTEM_DESC[c], c))
    else:
        head = [(CONFIRM_LABEL[k], d, k) for k, d in CONFIRM_DESC.items()]
    kinds = KIND_DESC | ({UNDECIDED: TALK_UNDECIDED} if talk_question(q) else {})
    return head + [(k, d, k) for k, d in kinds.items()]


# その前に相手が言ったことの行の見出し（BEFORE）と、無いときの中身（NO_BEFORE）は flowctl（流れの制御が渡すものの説明＝1か所）


CALLED = "はじめにお呼びした言葉"  # 後任の方をお呼びしたあとの目的のお客様の判定だけ（Question.called。「ご本人」は、はじめにお呼びした方）


def scene(q, text: str, asked: str | None = None, before: str | None = None) -> dict:
    """判定に渡す場面（質問の文・はじめにお呼びした言葉・その前に相手が言ったこと・こちらが言い直して確かめた言葉・お客様の言葉）。prompt はこれを文にしたもの。"""
    called, before = getattr(q, "called", ""), before or (NO_BEFORE if getattr(q, "facets", None) else None)
    return ({"質問": q.text} | ({CALLED: called} if called else {}) | ({BEFORE: before} if before else {})
            | ({"こちらが言い直して確かめた言葉": asked} if asked else {}) | {"お客様の言葉": text})


def prompt(q, text: str, asked: str | None = None, before: str | None = None) -> str:
    return "".join(prompt_parts(q, text, asked, before))


def prompt_parts(q, text: str, asked: str | None = None, before: str | None = None) -> tuple[str, str]:
    """問い合わせの文（prompt）の (毎回同じ前の部分, その発話ごとに変わる残り)。Anthropic へは前の部分に印を付けて送る（cached_content）。
    返事の形の指定（選べる名前の決まり）は、文より前に置かれ、前の部分と一緒にまとめて送られる（2026-10-29 に確かめた）。
    並びは PROMPT（質問・その前に相手が言ったこと・言い直した言葉・選べるもの・お客様の言葉）。ただし、こちらが決めた意味の名前の質問
    （Question.named）は、選べるもの（意味の定義＝長くて毎回同じ）を、その前に相手が言ったことより先に置く（定義もまとめて送れる）。
    ⛔ 2026-10-29：お客様の選択肢の言葉の質問で選べるものを先に置くと、言い直しへの返事の読みが悪くなった（G-53。上の PROMPT の注意）。
       目的のお客様の判定（G-59）は、選べるものを先に置いた形で 139 → 144/147"""
    said = f"こちらが言い直して確かめた言葉：「{asked}」\n" if asked else ""
    before = f"{BEFORE}：「{before}」\n" if before else f"{BEFORE}：{NO_BEFORE}\n" if q.facets else ""
    if q.facets:   # 項目ごとに答える質問：決まり・質問（はじめにお呼びした言葉）・項目は毎回同じ（まとめて送る前の部分）
        # ⛔ 2026-10-30 G-59（3つの段の形の1回目）：考える Haiku は、後任の方の「はい、そうです。」の事情を「在籍していない」と読んだ（この行の、
        #    はじめにお呼びした方のことを、いまお呼びしている方の事情と取り違えた）
        # ⛔ 同じ日の2回目：この行の「在籍していないと伺い」が残る間は、考える・考えない Haiku とも、後任の方の「はい、そうです。」の事情を
        #    「在籍していない」と読んだ（流れの制御は、後任の方がいまは話せないと受け取り、繋がない）⇒ この行には事情を書かない
        called = f"{CALLED}：「{q.called}」（いまは、その方の後任のご担当者様をお呼びしています）\n" if getattr(q, "called", "") else ""
        return facet_rules(q) + f"質問：「{q.text}」\n" + called + facet_choices(q), before + said + f"お客様の言葉：「{text}」\n"
    choices = "選べるもの（名前：説明）：\n" + "\n".join(f"- {n}：{d}" for n, d, _ in options(q, asked)) + "\n"
    head, tail = rules(q) + f"質問：「{q.text}」\n", f"お客様の言葉：「{text}」\n"
    if getattr(q, "named", False):
        return head + choices, before + said + tail
    return head, before + said + choices + tail


def cached_content(fixed: str, varying: str, ttl: str | None = None) -> list[dict]:
    """Anthropic へ送る問い合わせの文（唯一の場所。意味の判定・中身の判定）：毎回同じ前の部分に印（cache_control）を付け、2回目からは
    約10分の1の値段で読ませる（5分使わなければ消える。1回目の書き込みは1.25倍）。⚠️ 2026-10-29 に確かめた（約3.4円）：考える Sonnet と
    考えない Sonnet は、本文に置いてもシステムの指示に置いても、互いの印の部分を読めない（考える設定が前の部分に入る）＝形ごとに別に書く。
    同じ形どうしは 1,961トークン中 1,961 を読めた。印の部分は 512トークンより短いと付かない（Sonnet 5.5・Haiku 5.5）。
    ttl＝もつ長さ（"1h"＝1時間。書き込みは2倍。まとめて送る測り＝batch_judge だけ。通話は5分のまま）"""
    mark = {"type": "ephemeral"} | ({"ttl": ttl} if ttl else {})
    return [{"type": "text", "text": fixed, "cache_control": mark}, {"type": "text", "text": varying}]


def _config(q, asked: str | None):
    from google.genai import types

    if q.facets:
        keys = [f.key for f in q.facets]
        schema = types.Schema(type=types.Type.OBJECT, properties={f.key: types.Schema(type=types.Type.STRING, enum=list(f.choices)) for f in q.facets},
                              required=keys, property_ordering=keys)
        return types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0)
    names = [n for n, _, _ in options(q, asked)]
    schema = types.Schema(type=types.Type.OBJECT, properties={"choice": types.Schema(type=types.Type.STRING, enum=names)}
                          | {m: types.Schema(type=types.Type.BOOLEAN) for m in answer_marks(q)}, required=["choice", *answer_marks(q)])
    # ⚠️ 考える量（thinking_budget）は指定しない：3.5 Flash Lite は指定すると断る（400）。3.8 Flash は0にしても約3.3秒（2026-10-17 に測った）
    return types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0)


def read(q, text: str | None, asked: str | None = None) -> Reading | None:
    """判定の返事から、中の名前（選択肢・any・unknown・refused／affirmed・corrected／HOLD・HANDOVER・UNDECIDED）と、言い切っているか。
    読めなければ None。言い切っているかが無い・読めない返事は、言い切っている（印を付けない＝ほかの確かめのとおり）。"""
    try:
        got = json.loads(text or "{}")
        name, firm, sure = got.get("choice"), got.get(FIRM), got.get("sure")
    except (ValueError, AttributeError):
        return None
    sure = sure if isinstance(sure, (int, float)) else None
    if q.facets:
        # 項目ごとの答え（どれか1つでも無い・選べるものに無ければ、読めない返事）⇒ 組み合わせた読みと、読みに出ない事実の印（唯一の決まり）
        facts = {f.key: got.get(f.key) for f in q.facets}
        if any(facts[f.key] not in f.choices for f in q.facets):
            return None
        value, away, new, successor = facts_reading(facts, getattr(q, "seeks_successor", False))
        r = Reading(value, True, sure)
        r.away, r.new, r.successor, r.facts = away, new, successor, facts
        return r
    cond = next((c for n, _, c in options(q, asked) if n == name), None)
    if cond is None:
        return None
    r = Reading(cond, firm is not False, sure)
    r.firm_p = got.get("firm_p") if isinstance(got.get("firm_p"), (int, float)) else None
    r.fits = got.get(FITS) is not False   # 会話になっているかが無い・読めない返事は、なっている（印を付けない＝ほかの確かめのとおり）
    r.fits_p = got.get("fits_p") if isinstance(got.get("fits_p"), (int, float)) else None
    return r


def _schema(q, asked: str | None) -> dict:
    """選べるものの名前だけを返させる形（Gemini は _config・ほかの会社はこの形をそのまま渡す）。項目ごとに答える質問は、項目ごとの名前。"""
    if q.facets:
        return {"type": "object", "properties": {f.key: {"type": "string", "enum": list(f.choices)} for f in q.facets},
                "required": [f.key for f in q.facets], "additionalProperties": False}
    return {"type": "object", "properties": {"choice": {"type": "string", "enum": [n for n, _, _ in options(q, asked)]}}
            | {m: {"type": "boolean"} for m in answer_marks(q)},
            "required": ["choice", *answer_marks(q)], "additionalProperties": False}


def answer_marks(q) -> tuple[str, ...]:
    """選んだ答えとは別に答えさせる印（唯一の場所。返事の形 _config・_schema と判断のモデルの問い jev_request）：お客様の答えを読む問い合わせ
    （答えの段）は、言い切っているか（FIRM）と会話になっているか（FITS）。電話のやり取りを読む問い合わせ（talk_question）には付けない。"""
    return () if talk_question(q) else (FIRM, FITS)


def judge(client, model: str, q, text: str, asked: str | None = None, before: str | None = None) -> str | None:
    """相手の言葉 text が、質問 q（asked＝こちらが言い直した言葉・before＝その前に相手が言ったこと）のどれに当たるか。読めなければ None。"""
    r = client.models.generate_content(model=model, contents=prompt(q, text, asked, before), config=_config(q, asked))
    return read(q, r.text, asked)


async def ajudge(client, model: str, q, text: str, asked: str | None = None, before: str | None = None) -> str | None:
    """judge と同じ（通話の中で、会話のAIの報告を待つ間に走らせる形）。"""
    r = await client.aio.models.generate_content(model=model, contents=prompt(q, text, asked, before), config=_config(q, asked))
    return read(q, r.text, asked)


JUDGE_MODELS = ("3.5 Flash Lite", "3.6 Flash", "3.7 Flash", "3.8 Flash")   # 判定に使える文字のモデル（表示名。g23_names.find_models でIDを引く）
# 質問の種類（question_kind）のうち、目的のお客様をお呼びする段の2つ（目的のお客様の判定・尋ねているか）。中身の判定（grounding）は ground
CALLING_KINDS = ("facets", "asks")
# 電話のやり取りを読む問い合わせの種類（お客様の答えではない＝talk_question）。✅ 2026-10-31 ユーザー：(4) の案「OK」
TALK_KINDS = ("asks", "returned")

# ── Google 以外の判定（DESIGN.md 1-3 の (2)・選んだ根拠は docs/JUDGE_SETUP.md）。同じ問い合わせの文・同じ選べるもの・同じ読み方（read）で、
#    送り方だけが会社ごとに違う。
#    ✅ 2026-10-18 ユーザー：「ジェミニより精度が悪いんだったら意味がないでしょ。ジェミニよりほぼ確実に精度がいいものを抜粋しないと時間の無駄」
#       「最新モデルで評価しないとダメだよ」⇒ 各社の最新の、速くて安いモデルのうち、公開の数字で 3.5 Flash Lite を上回るものだけ
#    ⚠️ キーの名前は ANTHROPIC_API_KEY にしない（この作業の場所そのものが読む名前と同じになる）
# 緊急の道（唯一の場所）：Anthropic の判定（Sonnet 5.5）を、Anthropic へ直接送れないときだけ、OpenRouter の Anthropic と同じ形の窓口
#    （/api/v1/messages）を通して、Google（Vertex）が預かる同じモデルへ送る。問い合わせの文・返す形・読み方は直接と同じ（other_request・other_text）で、
#    替わるのは送り先・鍵・モデルの名前だけ。預け先は Google の米国の受け口1つだけ（ほかへ回さない・お客様の言葉を残す預け先は使わない）。
#    ⛔ 2026-10-30 夜：はじめは「google-vertex」とだけ指定し、Google の受け口3つ（global・us・europe）のどこへでも回っていた＝毎回同じ前の部分の
#    保存が受け口ごとに書かれ（G-59 の1回り：書き込み 114,451 トークン＝直接の約4.6倍・60.9円）、お客様の言葉が欧州へも行きうり、国も決まらなかった
#    ⇒ 米国の受け口（google-vertex/us）だけにした（料金は直接の1.1倍＝OpenRouter の一覧。global は直接と同じ値段だが処理する国が決まらない）。✅ 2026-10-30 夜 ユーザー：「OpenRouter を通して Google が預かる Claudeを使うのは、会話の途中などで
#    アンソロピックが使えなくなった緊急事態の時のみです。基本的にアンソロピックが使えなくなったりその他に使う AI のどれかが止まったら、AI の不具合として
#    回復するまで一時停止してください。ただし、どうしても使わなければいけないシーンが出たときは、契約者以上の権限で…再開することができるようにしておいて
#    ください。」＝使うのは JudgePanel が、通話の中で直接が止まったと分かったとき（_try）と、契約者以上の権限で再開した通話（emergency_first）だけ。
#    新しい電話を一時停止するのは発信の門（CL-2）。止まったことは通話の報告の ai_trouble に残す（Flow.on_ai_trouble）
EMERGENCY = {"base": "https://openrouter.ai", "path": "/api/v1/messages", "key": "OPENROUTER_JUDGE_KEY", "model": "anthropic/claude-sonnet-5.5",
             "provider": {"only": ["google-vertex/us"], "allow_fallbacks": False, "data_collection": "deny"}}
EMERGENCY_ROUTE = "緊急の道（OpenRouter を通して Google が預かる Claude）"
RESUME_EMERGENCY = "emergency"   # 通話の始めに渡す judge_route（convo.run_call）：契約者以上の権限で、緊急の道を使って再開した通話（RouteState.emergency_first）

OTHER_JUDGES = {
    "Claude Haiku 5.5": {"maker": "anthropic", "model": "claude-haiku-5-5", "key": "ANTHROPIC_JUDGE_KEY", "host": "api.anthropic.com"},
    # 考える段を入れた形（2026-10-18：考えない形は「違います」を、言い直した意味を認めたと読む向きに 47 回誤った）
    "Claude Haiku 5.5（考える）": {"maker": "anthropic", "model": "claude-haiku-5-5", "key": "ANTHROPIC_JUDGE_KEY", "host": "api.anthropic.com",
                                 "think": "low"},
    # 同じ考える形を、道具を使わず返事の形を決めて JSON で返させる形（下の Sonnet 5.5（考える）と同じ送り方。2026-10-29 ユーザー：「Haiku（考える）で
    # 早く出させる方法はないのか？」）。道具で返す形は、考えたあとに道具を選んで呼ぶ一手が入り、G-59 の答えまでが中央 2.38秒・遅いほうから1割 4.64秒だった
    "Claude Haiku 5.5（考える・形）": {"maker": "anthropic", "model": "claude-haiku-5-5", "key": "ANTHROPIC_JUDGE_KEY", "host": "api.anthropic.com",
                                    "form": "json", "effort": "low", "thinking": {"type": "adaptive"}},
    # Sonnet 5.5（2026-10-18 ユーザー：「Sonnet5.5でも試した方がいい」）：名前を指定した道具の呼び出しは断る（400）ので、返事の形を決めて
    # JSON で返させる。考えない形は {"type": "between_tools"}（道具が無いので考える段が無い）
    # group_kinds（✅ 2026-10-30 夜 ユーザー：「(1)(2)ともお勧めの形でOKです」）：組の中では、お呼びする段の2つの質問にだけ答える（測った
    #    G-59 で考える形より正しい＝71/71 と 69/71）。答えの意味・中身の判定は考える形だけ（記録で、考えない形だけは中身の判定で危ない文を2つ通し、
    #    2つ同時は考える形だけと正しさが同じで、Sonnet の料金が2倍＝PHASE0 G-59 の「答えの段・中身の判定で、2つの Sonnet…」）
    "Claude Sonnet 5.5": {"maker": "anthropic", "model": "claude-sonnet-5-5", "key": "ANTHROPIC_JUDGE_KEY", "host": "api.anthropic.com",
                          "form": "json", "effort": "low", "thinking": {"type": "between_tools"}, "group_kinds": CALLING_KINDS,
                          "emergency": EMERGENCY},
    # group_kinds＝組の中で答える質問の種類（takes_part）。✅ 2026-10-30 ユーザー：「d1-ultra（線0.6）と Jev（線0.8）を同時に出し、どちらも
    #    確かでないときだけ考えない Sonnet を出す形。OK」＝お呼びする段の2つの質問には、考える形は出さない（答えの意味・中身の判定は今のまま）
    "Claude Sonnet 5.5（考える）": {"maker": "anthropic", "model": "claude-sonnet-5-5", "key": "ANTHROPIC_JUDGE_KEY",
                                  "host": "api.anthropic.com", "form": "json", "effort": "low", "thinking": {"type": "adaptive"},
                                  "group_kinds": ("other", "returned", "ground"), "emergency": EMERGENCY},
    "GPT-6 Luna": {"maker": "openai", "model": "gpt-6-luna", "key": "OPENAI_JUDGE_KEY", "host": "api.openai.com"},
    # 同じ GPT-6 Luna を、設定済みの OpenRouter のキーで（2026-10-23。OpenAI のキーが作業環境に無いため）。送り方は OpenAI と同じ形
    # （OpenRouter の Responses の窓口）。⚠️ 文の行き先は OpenRouter（米国）を通って OpenAI（米国）。考える量は「なし」（中身の判定の
    # 3文で、low は 3.3〜4.4秒・minimal は 3.0〜3.8秒＝2秒の待ちに入らない。none は 1.1〜1.6秒）。⛔ G-63（`G-63-luna_20261023_094345.json`）：
    # 機械の確かめと合わせても危ない文を通した（「当日の担当者につきましては、担当の者よりお話しさせていただきます。」）＝中身の判定に使わない。
    # 新しいアカウントは1分に20回まで（答えが返ったのは168回中40回）
    "GPT-6 Luna（OpenRouter・考えない）": {"maker": "openai", "model": "openai/gpt-6-luna", "key": "OPENROUTER_JUDGE_KEY",
                                       "host": "openrouter.ai", "path": "/api/v1/responses", "effort": "none"},
    # Jev（TypeSafe の、選ぶ・分けるだけの専用モデル。2026-10-18 ユーザー：「判断のみなら、Jevが安くて優秀だと聞きましたが、使えないんですか？」）。
    # 直接の窓口は順番待ちなので OpenRouter を通す。最新の版を名前で決めて測る（「最新」の別名だと、測ったあとに中身が替わる）
    # 線（2026-10-18 G-53 の「曖昧の印と Jev」。線は正解の付いた答えの半分で決め、残りの半分で数えた）：
    # sure_min＝答えにする確からしさ（Jev が受け持って誤った数が 0.95 から0。下は答えにしない＝もう1つの判定で決める）・
    # firm_min＝言い切っているとする確からしさ（0.5 では要らない「曖昧」の印が53個、0.2 では5個）
    # ground_min＝中身の判定（grounding）で読みにする確からしさ（2026-10-23 G-63：Jev が外したのは5回とも 0.48 以下・正しく通したのは 0.51 以上
    # ＝線 0.8 で危ない文 0・Jev が決めるのは 76%。✅ 2026-10-23 の2回目・ユーザー承認で、中身の判定にも毎回入れた＝grounding.GROUNDERS）
    # facets_sure_min＝目的のお客様の判定（項目ごとに答える質問）で答えにする確からしさ（3つの項目の一番低いもの）。✅ 線の決め方は G-53 と同じ
    # （2026-10-18 ユーザーの案：正解の付いた答えの半分で決め、残りの半分で数える）。G-59 の60行×3回り：Jev が外れた回の確からしさは、1回り目で
    # 一番高くて 0.59（2回り目 0.55・3回り目 0.64）⇒ 1回り目で 0.8 と決め、2・3回り目で 120回中49回を Jev だけで決めて外れ0（G-59jev_facets_r2/_r3）。
    # 判定に見せる名前を書いてあるとおりにしたあと（Flow.for_judge）の3回りでも、外れの確からしさは 0.37 まで・0.8 から上は 180回中78回で外れ0
    # （G-59jev_names）。⛔ 2026-10-29：答えの判定で測った 0.95 を、項目ごとの形で測らずに使っていた（Jev が決めるのは 180回中10回だけだった）
    # asks_sure_min＝目的のお客様をお呼びする段の「尋ねているか」（flowctl.ASKS_MEANINGS の質問）で答えにする確からしさ。決め方は上と同じ。G-59 の
    # 71行×3回（G-59jev_layer5・_layer15・_layer17。この質問の説明は3回とも同じ）：尋ねているか・何を聞かれたか・取り次ぎを待つ（電話のやり取りの
    # 正解に当てた）で数えて、1回り目は外れ0・Jev が外れたのは3回の中で確からしさ 0.3 台の1回だけ ⇒ 目的のお客様の判定と同じ 0.8 と決め、2・3回り目で
    # 142回中108回を Jev だけで決めて外れ0（0.95 のときは 64回。尋ねている9行は3回とも 0.8 から上で合った）。⛔ 2026-10-30：この質問も、答えの判定で
    # 測った 0.95 を測らずに使っていた（2026-10-30 ユーザー：「考えない Sonnet の割合を下げながら、100％精度を出す組や仕組みを模索してください。」）
    "Jev 1.14": {"maker": "typesafe", "api": "decision", "model": "typesafe/jev-1.14", "key": "OPENROUTER_JUDGE_KEY", "host": "openrouter.ai",
                 "sure_min": 0.95, "facets_sure_min": 0.8, "asks_sure_min": 0.8, "firm_min": 0.2, "ground_min": 0.8},
    # d1（Liquid の判断のモデル。2026-10-30 ユーザー：「他に、無料や格安で使える優秀なAPIはありますか？」）。Jev と同じ OpenRouter の判断の窓口・
    # 同じ問いの形（項目ごとの選ぶ問い・言い切っているかの 0〜1）で答えることを、1回ずつ確かめた（答えまで約0.5秒・Jev と同じ問いで入力は約3分の1）。
    # 公開の料金は入力100万トークンあたり d1-ultra 約0.042ドル・返す側0。版は名前で決める（「最新」の別名だと、測ったあとに中身が替わる）。
    # 線（決め方は Jev と同じ：初めの3回りで決め、新しい3回りで数えた。G-59d1ultra_layer15・_check）：目的のお客様の判定は、外れの確からしさが
    # 初めの3回りで一番高くて 0.45 ⇒ 0.6。新しい3回りで、Jev（0.8）と並べて Sonnet 46%・外れ0（外れの確からしさは 0.37 まで）。尋ねているかは
    # 6回り426回で外れ0 ⇒ 同じ 0.6。✅ 2026-10-30 ユーザー：「d1-ultra（線0.6）と Jev（線0.8）を同時に出し、どちらも確かでないときだけ考えない
    # Sonnet を出す形。OK」「お客様の言葉の送り先に、Liquid（米国・OpenRouter 経由）が増えます。OK」（Intent.md 13章）。組の中では、測った
    # お呼びする段の2つの質問にだけ答える（group_kinds。中身の判定は測っていない）。
    # 答えの意味の線（2026-10-31・意味で書き直した文＝G-53 の指紋 4fbf72dcb8ee7cbd・`G-53bf_final_20261030_182546.json`）：決まった決め方
    #    （正解の付いた答えの半分で決め、残りの半分で数える）で、答えの確からしさ 0.77・言い切りの確からしさの、どちらとも言えない幅
    #    0.20〜0.92（sure_reading。幅に入る読みは確かでない＝次の判定）。言い切っているのは 0.92 から上（firm_min＝幅の上）。数える半分の 1,430件で
    #    946件を決め、外れは2（「社内にはいると思います」は表の誤りで直した・残る1つは駐車場の質問への「差し控えさせていただきます」＝PHASE0 G-59）。
    #    ⏳ 答えの段の組に入れるのは、Jev も同時に出すか（(ア)）・d1-ultra だけを先に出すか（(a)）のご判断のあと
    "d1 Ultra（Liquid）": {"maker": "liquid", "api": "decision", "model": "liquid/d1-ultra-20261015", "key": "OPENROUTER_JUDGE_KEY",
                          "host": "openrouter.ai", "firm_min": 0.92, "facets_sure_min": 0.6, "asks_sure_min": 0.6,
                          "sure_min": 0.77, "firm_band": (0.20, 0.92), "group_kinds": CALLING_KINDS},
    "d1.1（Liquid）": {"maker": "liquid", "api": "decision", "model": "liquid/d1.1-20261027", "key": "OPENROUTER_JUDGE_KEY",
                      "host": "openrouter.ai", "firm_min": 0.2},
    # 同じ窓口のほかの判断のモデル（2026-10-30 に1回ずつ確かめた。線は測ってから決める・送り先はどれも Intent.md 13章にまだ無い）：
    # GPT-6 Luna Decisions（OpenAI・10-06 版・約0.8秒）・Mercury Decide（Inception・09-30 版・約0.6秒）・Solar Decide（Upstage・09-28 版・
    # 1回目 11秒・2回目 1.05秒＝遅く揺れる。同じ問いの言い切っているかが 0.003 と 0.89 に割れた）
    "GPT-6 Luna Decisions": {"maker": "openai", "api": "decision", "model": "openai/gpt-6-luna-decisions-20261006", "key": "OPENROUTER_JUDGE_KEY",
                             "host": "openrouter.ai", "firm_min": 0.2},
    "Mercury Decide（Inception）": {"maker": "inception", "api": "decision", "model": "inception/mercury-decide-20260930",
                                  "key": "OPENROUTER_JUDGE_KEY", "host": "openrouter.ai", "firm_min": 0.2},
    "Solar Decide（Upstage）": {"maker": "upstage", "api": "decision", "model": "upstage/solar-decide-20260928", "key": "OPENROUTER_JUDGE_KEY",
                              "host": "openrouter.ai", "firm_min": 0.2},
}


def question_kind(q) -> str:
    """質問の種類（唯一の場所。線＝sure_line と、組の中でどの判定が答えるか＝takes_part が使う）：目的のお客様の判定（項目ごとに答える質問＝
    q.facets）は facets・尋ねているか（選ぶものが flowctl.ASKS_MEANINGS の質問）は asks・待たせた方が戻ったか（flowctl.RETURN_MEANINGS）は
    returned・ほか（答えの意味の判定）は other。中身の判定（grounding）は質問を持たないので、呼ぶ側が ground と書く。
    ⛔ 2026-10-31：待たせた方が戻ったかを other（答えの意味と同じ種類）にしていた＝答えの段の線（測っていない）と答え用の文が入っていた"""
    if getattr(q, "facets", ()):
        return "facets"
    conds = set(getattr(q, "conditions", ()) or ())
    return "asks" if conds == set(ASKS_MEANINGS) else "returned" if conds == set(RETURN_MEANINGS) else "other"


def talk_question(q) -> bool:
    """電話のやり取りを読む問い合わせ（TALK_KINDS）か（唯一の場所。決まりの文・選べるもの・返事の形・判断のモデルの問いが、ここで分かれる）：
    お客様の答えを読むのではないので、答え用の文（言い切り・会話になっているか・「わからない」「答えたくない」）を入れない。"""
    return question_kind(q) in TALK_KINDS


def rules(q) -> str:
    """問い合わせの文の決まり（唯一の場所。項目ごとに答える質問は facet_rules）。"""
    return TALK_RULES if talk_question(q) else RULES


def sure_line(name: str, q) -> float | None:
    """確からしさを返す判定（Jev・d1）の、答えにする線を決める唯一の場所：質問の種類（question_kind）ごとの線（facets_sure_min・
    asks_sure_min・returned_sure_min）があればそれ、無ければ sure_min（線は質問の種類ごとに、その種類の正解の付いた答えで決める＝測っていない
    種類は sure_min のまま）。確からしさを返さない判定は None（線なし）。"""
    j = OTHER_JUDGES.get(name, {})
    return j.get(f"{question_kind(q)}_sure_min", j.get("sure_min"))


def firm_matters(q, got) -> bool:
    """読み got で、お客様が言い切っているかが流れの制御の動きを変えるか（唯一の場所。確かな読みの決め方＝sure_reading・測りの道具が使う）：
    選択肢を選んだ読み（最初の答えは、念のため確かめるか受け取るかが変わる・言い直しへの返事は、曖昧の印が付くか）と、値の言い直しを認めた読み。"""
    return got in (getattr(q, "conditions", ()) or ()) or got == "affirmed"


def sure_reading(name: str, q, got) -> bool:
    """確からしさを返す判定（判断のモデル）の読み got が、確かな読みか（唯一の場所。JudgePanel._try・測りの道具）：答えの確からしさが線
    （sure_line）から上で、言い切りが動きを変える読み（firm_matters）なら、言い切りの確からしさが、どちらとも言えない幅（firm_band）の外。
    線の無い判定は、いつも確か。✅ 2026-10-31 ユーザー：d1-ultra の「行けないことはないです」＝中くらい（0.56）の読みが正しい＝言い切りも
    確からしさで扱い、どちらとも言えない読みは次の判定に回す（線と幅は、正解の付いた答えの半分で決め、残りの半分で数える）"""
    line = sure_line(name, q)
    if line is None:
        return True
    if got.sure is None or got.sure < line:
        return False
    band = OTHER_JUDGES.get(name, {}).get("firm_band") if question_kind(q) == "other" else None
    return not (band and got.firm_p is not None and firm_matters(q, got) and band[0] <= got.firm_p < band[1])


def takes_part(name: str, kind: str) -> bool:
    """組（2つ以上の判定の受け持ち）の中で、判定 name が kind（question_kind・ground）の質問に答えるか（唯一の場所。JudgePanel・
    grounding.make_grounder が使う）。group_kinds を書いていない判定は、どの種類にも答える。1つだけの判定で測るときは使わない。"""
    return kind in OTHER_JUDGES.get(name, {}).get("group_kinds", (kind,))


# 質問の種類ごとに、止める側だけに使う判定（唯一の場所。JudgePanel）：その質問には自分で答えず、ほかの受け持ち（組）が確かな読みを返さないと
#    分かった時点で裏で出し、その読みは、組の答えが待つ長さに間に合わない・組が答えられないときに、止める側（黙って待つ・繋ぐのを止める）だけに使う
#    （NoJudgment の tried の stop_only ⇒ convo が Flow.on_unsure(…, answer=False)）。答えにはしない。
#    ✅ 2026-10-30 ユーザー：「Flash Lite は代わりだけにする形でOKです」・同じ夜「(1)(2)ともお勧めの形でOKです」＝お呼びする段の2つの質問（目的の
#    お客様の判定・尋ねているか）では、その場の判定（3.5 Flash Lite）はいつも止める側だけ。⛔ 同じ言葉に同時に出していたので、組が正しく読んでも、
#    Flash Lite の読み1つで流れの制御が動いた（G-59 のいまの文で 213回中30回。組は0）。⛔ 同じ夜まで、組が答えられないときは Flash Lite の読みを
#    答えにしていた＝その言葉（d1-ultra と Jev が2つとも確かでない言葉）96回中17回で流れの制御の動きが外れる（PHASE0 G-59）。組が答えられない
#    ときの代わりは、同じ Sonnet の緊急の道（EMERGENCY）。答えの意味・待たせた方が戻ったかは今のまま同時に出す
STOP_ONLY = {"3.5 Flash Lite": CALLING_KINDS}


PROBE_TEXT = "はい、しています。"   # 回復の確かめで判定に送る言葉（JudgePanel.probe）


def probe_question():
    """回復の確かめで判定に送る、決まった短い質問（はい／いいえ。前の部分が短いので、確かめのたびの料金が小さい＝2つの Sonnet で1回り
    約0.4円・5分を超えて間が空くと前の部分を書き直して約1.1円。2026-10-30 に本物で2回り流して 1.5円）。"""
    from flowctl import Question
    return Question("probe", "ご予約はされていますでしょうか？", "yes_no", conditions=("はい", "いいえ"))


def stop_only(name: str, kind: str) -> bool:
    """判定 name が kind（question_kind）の質問では、止める側だけに使う判定か（STOP_ONLY の1か所）。"""
    return kind in STOP_ONLY.get(name, ())


# 同時に出して、先に届いた確かな読みで決める組（唯一の場所。意味の判定の受け持ち＝JudgePanel・中身の判定＝grounding.make_grounder の両方が使う）。
#    ✅ 2026-10-23 ユーザー（中身の判定）：「W sonnetで暫定としてOK」・2回目「OK」（Jev と2つの Sonnet を同時に出し、早く届いた確かな読みで決める）
#    ✅ 2026-10-25 ユーザー：「以前、JevやSonnet 5.5 の「考えない」形を使うと決めましたよね。同じことを聞くな！」＝意味の2つ目の判定も同じ組
#    ⛔ 2026-10-25 の失敗：意味の2つ目の判定だけ Sonnet 5.5（考える）1つのまま（Jev は止まったときの代わりだけ）にして、答えまで中央 1.69秒・
#       4割が2秒超のまま、「判定の顔ぶれと待つ長さ」を伺い続けた。確かな読み＝Jev は線（OTHER_JUDGES の sure_min・ground_min）から上だけ
#    ✅ 2026-10-30 ユーザー：「d1-ultra（線0.6）と Jev（線0.8）を同時に出し、どちらも確かでないときだけ考えない Sonnet を出す形。OK」＝組の前に
#       d1-ultra を足した。どの判定がどの質問に答えるかは takes_part の1か所（お呼びする段の2つ＝d1-ultra・Jev → 考えない Sonnet／答えの意味・
#       中身の判定＝Jev → 考える・考えない Sonnet で今のまま）。先に出すのは組の中の判断のモデル全部（first_of の leads）
RACE = ("d1 Ultra（Liquid）", "Jev 1.14", "Claude Sonnet 5.5（考える）", "Claude Sonnet 5.5")
RACE_NAME = "d1 Ultra＋Jev 1.14＋Claude Sonnet 5.5（考える・考えない）"
# 契約者が Jev を使わないと選んだとき（✅ 2026-10-18 ユーザー：「契約者設定で、Jev も使うか、Sonnetだけにするかも選択できるようにした方がいいね」。
#    CL-2 の契約者の設定 judge.models の sonnet）：2つの Sonnet の組（Jev は止まった判定の代わりだけ）
SONNETS = ("Claude Sonnet 5.5（考える）", "Claude Sonnet 5.5")
SONNETS_NAME = "Claude Sonnet 5.5（考える・考えない）"
GROUPS = {RACE_NAME: RACE, SONNETS_NAME: SONNETS}
# 2つ目の判定（convo.check_handler の judge2。DESIGN.md 1-3 の (2)）。✅ 2026-10-18 ユーザー：「クレジットの使い残しはもったいないので、当面は、
#    Sonnetだけを使いましょう。Jev検証時を除く」⇒ 10-23・10-25 のご決定で、Jev と2つの Sonnet の組
JUDGE2 = RACE_NAME


def members(name: str) -> tuple[str, ...]:
    """判定の名前（1つの判定か、組の名前）⇒ 同時に出す判定の名前の並び。"""
    return GROUPS.get(name, (name,))
OTHER_PATH = {"openai": "/v1/responses", "decision": "/api/v1/systemone"}  # 部品を使わず httpx で送る窓口（会社の窓口・判断のモデルの窓口）


def decision_model(name: str) -> bool:
    """判断のモデル（選ぶ・分けるだけで、選んだものと確からしさを返す。OpenRouter の判断の窓口＝Jev・d1）か（唯一の場所。問い方・読み方・窓口は
    この形で選ぶ＝会社では選ばない）。"""
    return OTHER_JUDGES.get(name, {}).get("api") == "decision"


def other_path(name: str) -> str:
    """httpx で送る判定の窓口（同じ会社の形を、別の接続先から使うときは、その判定の path）。"""
    j = OTHER_JUDGES[name]
    return j.get("path") or OTHER_PATH[j.get("api") or j["maker"]]
OPENAI_EFFORT = "low"  # 考える量（速さの公開の数字は low で約1.5秒。高くすると十数秒〜）


class OtherJudgeError(RuntimeError):
    """Google 以外の判定の失敗（status＝返事の番号・retry＝待つように言われた秒）。上限（429）・混雑（5xx）は呼ぶ側がやり直す。"""

    def __init__(self, status: int | None, message: str, retry: float | None = None):
        super().__init__(f"{status} {message}")
        self.status, self.retry = status, retry


def routed_model(name: str, route: str | None = None) -> str:
    """判定 name を道 route で送るときのモデルの名前（唯一の場所。意味の判定＝other_request・中身の判定＝grounding.ask_other）：緊急の道なら
    その道の名前（EMERGENCY の model）・直接ならその判定の名前。"""
    j = OTHER_JUDGES[name]
    return j["emergency"]["model"] if route == EMERGENCY_ROUTE else j["model"]


def other_request(name: str, q, text: str, asked: str | None, before: str | None = None, ttl: str | None = None,
                  route: str | None = None) -> dict:
    """Google 以外の判定への問い合わせの中身（会社ごとの形。問い合わせの文と選べるものは Gemini と同じ）。ttl＝Anthropic の、毎回同じ前の部分の
    もつ長さ（まとめて送る測り＝batch_judge だけ。cached_content）。route＝EMERGENCY_ROUTE なら緊急の道（中身は同じで、モデルの名前だけ
    緊急の道の名前＝EMERGENCY。預け先の指定は送る所＝_Routed）。"""
    j, p = OTHER_JUDGES[name], prompt(q, text, asked, before)
    if j["maker"] == "anthropic":
        content = cached_content(*prompt_parts(q, text, asked, before), ttl=ttl)   # 毎回同じ前の部分は、2回目から約10分の1の値段
        # ⚠️ Claude Haiku 5.5 は temperature などを送ると断る（1以外は400＝2026-10-18 に当たった）。名前を指定した道具の呼び出しは、
        #    考える段を飛ばして道具の呼び出しから返す（いちばん速い形）。strict＝選べるもの以外の名前を返させない
        body = {"model": j["model"], "max_tokens": 256, "messages": [{"role": "user", "content": content}],
                "tools": [{"name": "answer", "description": "お客様の言葉に当たるものの名前", "input_schema": _schema(q, asked),
                           "strict": True}],
                "tool_choice": {"type": "tool", "name": "answer"}}
        if j.get("think"):  # 名前を指定した呼び出しは考える段を飛ばすので、道具は任せ、考える量を決め、道具で答えるように言う
            body |= {"max_tokens": 4096, "tool_choice": {"type": "auto"}, "output_config": {"effort": j["think"]},
                     "system": "選んだものは、必ず道具 answer で返してください。"}
        if j.get("form") == "json":  # 道具を使わず、返事の形（選べるものの名前だけ）を決めて返させる
            # ⚠️ 断られたときに別のモデルへ回す仕組み（fallbacks）は入れない：測っているモデルの答えに、別のモデルの答えが混ざるため
            #    （断られたら読めない返事＝None として数える）
            body = {"model": j["model"], "max_tokens": 4096, "messages": [{"role": "user", "content": content}], "thinking": j["thinking"],
                    "output_config": {"effort": j["effort"], "format": {"type": "json_schema", "schema": _schema(q, asked)}}}
        body["model"] = routed_model(name, route)
        return body
    if decision_model(name):
        return jev_request(j["model"], q, text, asked, before)
    return {"model": j["model"], "input": p, "reasoning": {"effort": j.get("effort", OPENAI_EFFORT)}, "max_output_tokens": 2000,
            "text": {"format": {"type": "json_schema", "name": "answer", "schema": _schema(q, asked), "strict": True}}}


# ── Jev（判断だけの専用モデル）への問い方。⛔ 2026-10-18 の1回目：チャットのAI向けの長い決まりの文（RULES）を、そのまま問い（instructions）に
#    渡し、選択肢の定義を「この意味の答え」としか書かず、答えの確からしさを捨てていた（ユーザー：「Jevの使い方は理解できていますか？」）。
#    公式の型（OpenRouter の Jev の手引き）：問いは短く1つ・選択肢の意味は定義（criteria）に書く・確からしさで線を決める（線は正解の付いた答えで）。
#    決まりの中身は RULES・FIRM_RULE と同じ（言い直しへの返事は、認めたら言い直した意味・打ち消したら打ち消した側・寄っているかは LEAN の1か所）
JEV_ASK = "お客様の言葉は、質問へのどの答えに当たりますか？"
JEV_ASK_REPLY = ("お客様の言葉は、こちらが言い直して確かめた言葉への返事として、どの答えに当たりますか？"
                 "（言い直した意味を認めたら、その意味。言い直しを打ち消したら、打ち消した側）")
JEV_FIRM = {"true": FIRM_YES, "false": FIRM_NOT}
JEV_FITS = {"true": FITS_YES, "false": FITS_NOT}
# 判断のモデルの印の問い（答えの段だけ＝answer_marks。定義は上の1か所から）
JEV_MARKS = {FIRM: ("お客様は、その答えを言い切っていますか？", JEV_FIRM),
             FITS: ("お客様のいまの言葉は、前の質問への返事として、また" + BEFORE + "と並べて、会話になっていますか？", JEV_FITS)}
# 会話になっているとする確からしさの線（判断のモデル）。⏳ 2026-10-31：まだ測っていない＝半分で切る仮の線（決まった決め方で決めて、判定ごとの
#    fits_min に入れる）
FITS_LINE = 0.5


def jev_criteria(q, asked: str | None = None) -> dict:
    """Jev に渡す選択肢の定義（名前は options と同じ。答えの意味の選択肢は、意味で読むことを定義に書く。寄っているかは LEAN の1か所）。"""
    out = {}
    for n, d, c in options(q, asked):
        if d == "この意味の答え" or d.startswith("「"):
            d = (f"{LEAN}が「{n}」の意味（打ち消しを重ねた言い方・打ち消しの問いへの「はい」「いいえ」も、言葉の頭ではなく意味で読む。"
                 "言い切っていなくても、この意味を指していればこれ。確かめていない・知らない、と言っただけなら、これではない）")
        elif asked and c == UNDECIDED:   # 言い直しへの返事：打ち消しの形の言い直しへの「違う」（RULES と同じ1か所＝NEG_REPLY。問いは短く1つの型）
            d += "。" + NEG_REPLY
        out[n] = d
    return out


def jev_request(model: str, q, text: str, asked: str | None = None, before: str | None = None) -> dict:
    """Jev への問い合わせ：どの答えか（choice・確からしさ付き）と、言い切っているか（noul）。項目ごとに答える質問は、項目ごとに1つの問い
    （確からしさは項目ごと＝読むときは一番低いもの。言い切っているかは問わない）。"""
    if q.facets:
        return {"model": model, "state": scene(q, text, asked, before),
                "questions": {f.key: {"type": "choice", "instructions": f"電話に出ている方の言葉で、{f.heading}はどれに当たりますか？（"
                                      + (f"{NOW_ONLY}。" if f.key in NOW_ONLY_FACETS else "")
                                      + "ほかの項目に当たる事実があっても、この項目のことだけを読みます）"
                                      + ("見分け方：" + "／".join(f.notes) if f.notes else "")   # 境目は判定の文と同じ1か所（flowctl.Facet）
                                      + ("言葉の決まり：" + "／".join(q.terms) if f.key in TERM_FACETS else ""),
                                      "criteria": dict(f.choices)}
                              for f in q.facets}}
    return {"model": model, "state": scene(q, text, asked, before),
            "questions": {"choice": {"type": "choice", "instructions": JEV_ASK_REPLY if asked else JEV_ASK, "criteria": jev_criteria(q, asked)}}
            | {m: {"type": "noul", "instructions": JEV_MARKS[m][0], "criteria": JEV_MARKS[m][1]} for m in answer_marks(q)}}


def other_text(name: str, data: dict) -> str | None:
    """Google 以外の判定の返事から、read に渡す文字（{"choice": 名前}）。"""
    if OTHER_JUDGES[name]["maker"] == "anthropic":
        if OTHER_JUDGES[name].get("form") == "json":
            return next((c.get("text") for c in data.get("content") or [] if c.get("type") == "text"), None)
        got = next((c.get("input") for c in data.get("content") or [] if c.get("type") == "tool_use"), None)
        return json.dumps(got, ensure_ascii=False) if got else None
    if decision_model(name):  # 言い切っているかは確からしさ（0〜1）で返る：線（firm_min）から上なら言い切っている
        # ⛔ 2026-10-19 の見直し：ここは半分（0.5）で切っていた（測って合った線は0.2）。答えの確からしさ（sure）も捨てていた
        # 選ぶ問いの答えは全部（項目ごとに答える質問は項目ごと）。確からしさは、選ぶ問いのうち一番低いもの（1つでも自信のない項目があれば、
        # その読みは線より下＝JudgePanel が組のほかの判定で読む）
        answers = data.get("answers") or {}
        picked = {k: a for k, a in answers.items() if isinstance(a, dict) and "choice" in a}
        if not picked or any(not a.get("choice") for a in picked.values()):
            return None
        confs = [a.get("confidence") for a in picked.values()]
        firm = (answers.get(FIRM) or {}).get("noul")
        fits = (answers.get(FITS) or {}).get("noul")
        return json.dumps({k: a["choice"] for k, a in picked.items()} | {FIRM: firm is None or firm >= OTHER_JUDGES[name]["firm_min"],
                          FITS: fits is None or fits >= OTHER_JUDGES[name].get("fits_min", FITS_LINE),
                          "sure": min(confs) if all(isinstance(c, (int, float)) for c in confs) else None,
                          "firm_p": firm if isinstance(firm, (int, float)) else None,
                          "fits_p": fits if isinstance(fits, (int, float)) else None}, ensure_ascii=False)
    return next((c.get("text") for o in data.get("output") or [] for c in o.get("content") or [] if c.get("type") == "output_text"), None)


# ── 有料の判定（Anthropic）の料金の帳簿（唯一の場所）。返事ごとに使った量（usage）を数え、結果を保存するときに記録へ入れる（common.save_result）。
#    ⛔ 2026-10-28 ユーザー：「ここまでで3万円以上使用しているということですよ。無駄遣いや、必要ないのに使っているシーンはありませんか？
#       儲けより、API料金の方が高いことになりますよ」＝使った量をどこにも残しておらず、測りの料金が積み上がっても気づけなかった
#       （10-18 の午後だけで、同じ答え 2,259件を Sonnet に約1万1千回送り、同じ測りを2分おいて2回流した回もあった）
#    料金：100万トークンあたりのドル（入力・出力・まとめて送った分の読み・書き。考える量は出力に入る）。1ドル＝150円（Intent.md 12-2）
PRICE = {"claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50), "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125)}
YEN_PER_USD = 150.0


class Spend:
    """送った回数・量・円。limit＝この試しで使ってよい円（common.base_parser の --budget-yen。通話の本番では渡さない＝上限なし）：超えたら、
    それより後は送らずに失敗にする（判定は止まった扱い＝通話は止まらない。測りは、それ以上お金を使わない）。"""
    name = "Anthropic の判定"

    # 返事を待たずに止めた問い合わせ（先に届いた答えで決めたあと、first_of が止めた分）は、使った量が返らないが料金はかかりうる ⇒
    # 同じモデルの返事のあった問い合わせの、まとめて送った分を書いた料金を除いた平均の円で見込む（書くのは1回目だけ。返事が1つも無ければ、
    # 意味の判定の1回＝入力 1,670・出力 40 トークン）。⛔ 2026-10-29：帳簿を作った日の確かめで、止めた Sonnet を数えていなかった・
    # 次に書いた分まで平均に入れ、G-53 の100件で実際の約2倍に見込んだ
    TYPICAL_TOKENS = (1670, 40)

    def __init__(self):
        from collections import Counter, defaultdict
        self.calls, self.stopped, self.tokens, self.yen_by, self.write_by, self.limit = (Counter(), Counter(), defaultdict(Counter), Counter(),
                                                                                       Counter(), None)

    @property
    def yen(self) -> float:
        """返事のあった分の円＋止めた分の見込みの円。"""
        return sum(self.yen_by.values()) + sum(n * self._mean(m) for m, n in self.stopped.items())

    def _mean(self, model: str) -> float:
        if self.calls[model]:
            return (self.yen_by[model] - self.write_by[model]) / self.calls[model]
        p_in, p_out, _, _ = PRICE.get(model, PRICE["claude-sonnet-5-5"])
        return (self.TYPICAL_TOKENS[0] * p_in + self.TYPICAL_TOKENS[1] * p_out) / 1e6 * YEN_PER_USD

    def check(self) -> None:
        if self.limit is not None and self.yen >= self.limit:
            raise OtherJudgeError(None, f"この試しで使ってよい料金（{self.limit:.0f}円）に達したので送りません（--budget-yen）")

    def add(self, model: str, usage, factor: float = 1.0) -> None:
        """返事の使った量を数える。factor＝料金の掛け目（まとめて送る測り＝BATCH_FACTOR。書き込み・読み出しも同じく半分）。1時間もつ形の
        書き込み（cache_creation の ephemeral_1h_input_tokens）は入力の2倍（5分の形は PRICE の書き込み＝1.25倍）。"""
        def field(obj, k):
            return getattr(obj, k, None) or (obj.get(k) if isinstance(obj, dict) else None) or 0
        u = {k: field(usage, k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
        cc = getattr(usage, "cache_creation", None) or (usage.get("cache_creation") if isinstance(usage, dict) else None)
        hour = field(cc, "ephemeral_1h_input_tokens") if cc else 0
        p_in, p_out, p_read, p_write = PRICE.get(model, PRICE["claude-sonnet-5-5"])
        write = (u["cache_creation_input_tokens"] - hour) * p_write + hour * 2 * p_in
        usd = (u["input_tokens"] * p_in + u["output_tokens"] * p_out + u["cache_read_input_tokens"] * p_read + write) / 1e6 * factor
        self.calls[model] += 1
        self.tokens[model].update(u)
        self.yen_by[model] += usd * YEN_PER_USD
        self.write_by[model] += write / 1e6 * factor * YEN_PER_USD

    def summary(self) -> dict:
        return {"円": round(self.yen, 1), "回数": dict(self.calls), "返事を待たずに止めた回数（円は見込み）": dict(self.stopped),
                "トークン": {m: dict(t) for m, t in self.tokens.items()}, **({"上限の円": self.limit} if self.limit is not None else {})}


SPEND = Spend()


class KeySpend:
    """OpenRouter の鍵の使った額で数える帳簿（測りの --budget-yen のときだけ。OpenRouter の判定＝d1-ultra・Jev などは、返事に料金が無く、Spend の
    帳簿を通らない）。鍵の使った額（ドルの累計＝/api/v1/key。読むのは無料）を、はじめと、送る前に EVERY_S 秒おきに読み、はじめからの差が上限に
    達したら、それより後は送らずに失敗にする（Spend.check と同じ扱い）。同じ鍵を同じ時に使った分も入る（多めに数える側）。読めなければ止める側。
    ⛔ 2026-10-31 の昼：--budget-yen 50 を付けて流したが、OpenRouter の分を数える所が無く、上限で止まらなかった（ご承認の約51円に対して、
    G-70 と G-53 で約53円＝2回続けて見込みを超えた）。鍵の使った額は遅れて増える（同じ日の G-53：途中で読んだ額が、あとから見ると約50秒分＝
    約10円少なかった）⇒ 上限には、まだ額に出ていない最近 LAG_S 秒の回数ぶんを、それまでの1回あたりの額（額がまだ増えないうちは PRIOR_YEN）で
    足した見込みを当てる"""
    name = "OpenRouter の鍵"
    EVERY_S = 5.0
    LAG_S = 90.0
    PRIOR_YEN = 0.03   # 1回の見込みの円（額がまだ増えないうち。同じ日の G-53 の d1-ultra と Jev の1回の平均 約0.009円の3倍）

    def __init__(self, read=None):
        import threading
        from collections import Counter
        self.calls, self.limit, self.start, self.now, self.read_at = Counter(), None, None, None, 0.0
        self._read, self._lock, self.marks = read, threading.Lock(), []   # marks＝(送った時刻, それまでの回数)

    def bind(self, key: str, limit: float) -> None:
        """上限と、使った額を読む道具（鍵は持たずに、読む関数だけを持つ）。はじめの額は最初の1回だけ読む。"""
        import time

        import httpx

        def read() -> float:
            r = httpx.get("https://openrouter.ai/api/v1/key", headers={"authorization": f"Bearer {key}"}, timeout=20)
            r.raise_for_status()
            return float(r.json()["data"]["usage"])
        self.limit, self._read = limit, self._read or read
        with self._lock:
            if self.start is None:
                self.start = self.now = self._read()
                self.read_at = time.monotonic()

    @property
    def seen_yen(self) -> float:
        """鍵の使った額の差（遅れて増えるので、少なめに出る）。"""
        return 0.0 if self.start is None else (self.now - self.start) * YEN_PER_USD

    @property
    def yen(self) -> float:
        """見込みの円：鍵の使った額の差＋まだ額に出ていない最近 LAG_S 秒の回数 × 1回あたりの円。"""
        import bisect
        seen = bisect.bisect_right(self.marks, (self.read_at - self.LAG_S, float("inf")))
        seen_calls = self.marks[seen - 1][1] if seen else 0
        total = sum(self.calls.values())
        per = self.seen_yen / seen_calls if seen_calls and self.seen_yen > 0 else self.PRIOR_YEN
        return self.seen_yen + (total - seen_calls) * per

    def refresh(self, force: bool = False) -> None:
        import time
        with self._lock:
            if self._read is None or (not force and time.monotonic() - self.read_at < self.EVERY_S):
                return
            try:
                self.now = self._read()
            except Exception as exc:   # 読めなければ止める側（上限を守れないまま送らない）
                raise OtherJudgeError(None, f"鍵の使った額が読めないので送りません（--budget-yen）：{type(exc).__name__}") from exc
            self.read_at = time.monotonic()

    def check(self, model: str) -> None:
        if self.limit is None:
            return
        self.refresh()
        import time
        with self._lock:
            if self.yen + self.PRIOR_YEN > self.limit:
                raise OtherJudgeError(None, f"この試しで使ってよい料金（{self.limit:.0f}円）に達するので送りません（--budget-yen・鍵の使った額の見込み）")
            self.calls[model] += 1
            self.marks.append((time.monotonic(), sum(self.calls.values())))

    def summary(self) -> dict:
        try:
            self.refresh(force=True)
        except OtherJudgeError:
            pass
        return {"円": round(self.seen_yen, 1), "見込みの円（まだ額に出ていない分を含む）": round(self.yen, 1), "回数": dict(self.calls),
                "数え方": "鍵の使った額の差（同じ鍵を同じ時に使った分も入る・遅れて増えるので、あとで読み直す）",
                **({"上限の円": self.limit} if self.limit is not None else {})}


KEY_SPEND = KeySpend()


class _KeyMetered:
    """OpenRouter へ送る httpx の道具を包む：送る前に KEY_SPEND.check で上限を確かめる（ほかはそのまま渡す）。"""

    def __init__(self, client, aio: bool):
        self._client, self._aio = client, aio

    def __getattr__(self, attr):
        return getattr(self._client, attr)

    def post(self, path, json=None, **kw):
        KEY_SPEND.check((json or {}).get("model", "?"))
        return self._client.post(path, json=json, **kw)


class _Metered:
    """Anthropic の部品の messages.create を包む：送る前に上限を確かめ（SPEND.check）、返事の使った量を帳簿に数えてから返す。
    返事を待たずに止められたら（first_of が先に届いた答えで決めた）、止めた回数に数える。"""

    def __init__(self, client, aio: bool):
        self._client, self._aio, self.messages = client, aio, self

    def create(self, **body):
        SPEND.check()
        if self._aio:
            async def go():
                import asyncio
                try:
                    got = await self._client.messages.create(**body)
                except asyncio.CancelledError:
                    SPEND.stopped[body["model"]] += 1
                    raise
                SPEND.add(body["model"], got.usage)
                return got
            return go()
        got = self._client.messages.create(**body)
        SPEND.add(body["model"], got.usage)
        return got


# 使っていないつながりを保つ秒（判定へ送る道具の1か所）。⛔ 2026-10-29：通信の部品の初めの値は5秒で切る作りで、6秒ずつ間をあけて Jev に
#    問い合わせると、毎回つなぎ直して 0.39〜0.46秒かかった（つないだままなら 0.15〜0.16秒）＝通話の中の判定は数秒おきなので、Jev が待つ長さ
#    （LEAD_WAIT_S 0.3秒）を毎回過ぎて、2つの Sonnet を毎回出すところだった（記録の 0.16秒は、間をあけずに続けて測った値）
KEEPALIVE_S = 90.0


class _Routed:
    """緊急の道へ送る道具：Anthropic の部品と同じ messages.create(**body) の形で、OpenRouter の Anthropic と同じ形の窓口へ送り、返事を同じ形
    （usage・to_dict）で返す＝料金の帳簿（_Metered）・読み方（other_text）は直接と同じ1か所を通る。⚠️ Anthropic の公式の部品は使わない（鍵を
    渡さないと、作業の場所の ANTHROPIC_API_KEY を自分で読む＝別の会社へ送ってしまう）。鍵は OpenRouter の鍵だけを、明示して渡す。"""

    def __init__(self, http, route: dict):
        self.http, self.route, self.messages = http, route, self

    async def create(self, **body):
        r = await self.http.post(self.route["path"], json=body | {"provider": self.route["provider"]})
        r.raise_for_status()
        data = r.json()

        class Got:
            usage = data.get("usage") or {}

            @staticmethod
            def to_dict():
                return data
        return Got()


def other_client(name: str, key: str, aio: bool = False, budget: float | None = None, route: str | None = None):
    """Google 以外の判定へ送る道具（Anthropic は公式の部品を料金の帳簿で包む・OpenAI は httpx）。やり直しは呼ぶ側の1か所でするので、部品では
    繰り返さない。つながりは KEEPALIVE_S 秒保つ。budget：この試しで有料の判定に使ってよい円（試しの引数 --budget-yen。通話の本番は渡さない）。
    route＝EMERGENCY_ROUTE なら緊急の道（通話の中だけ＝aio。key は緊急の道の鍵）。"""
    if route == EMERGENCY_ROUTE:
        import httpx
        from common import LEDGERS

        assert aio, "緊急の道は通話の中だけ"
        if budget is not None:
            SPEND.limit = budget
        if SPEND not in LEDGERS:
            LEDGERS.append(SPEND)
        e = OTHER_JUDGES[name]["emergency"]
        http = httpx.AsyncClient(base_url=e["base"], timeout=60, headers={"authorization": f"Bearer {key}"},
                                 limits=httpx.Limits(keepalive_expiry=KEEPALIVE_S))
        return _Metered(_Routed(http, e), aio=True)
    if OTHER_JUDGES[name]["maker"] == "anthropic":
        import anthropic
        import httpx2
        from common import LEDGERS

        if budget is not None:
            SPEND.limit = budget
        if SPEND not in LEDGERS:
            LEDGERS.append(SPEND)
        http = (anthropic.DefaultAsyncHttpxClient if aio else anthropic.DefaultHttpxClient)(
            limits=httpx2.Limits(max_connections=1000, max_keepalive_connections=100, keepalive_expiry=KEEPALIVE_S))
        return _Metered((anthropic.AsyncAnthropic if aio else anthropic.Anthropic)(api_key=key, max_retries=0, timeout=60, http_client=http), aio)
    import httpx

    http = (httpx.AsyncClient if aio else httpx.Client)(base_url=f"https://{OTHER_JUDGES[name]['host']}", timeout=60,
                                                        headers={"authorization": f"Bearer {key}"},
                                                        limits=httpx.Limits(keepalive_expiry=KEEPALIVE_S))
    if budget is None:
        return http
    if OTHER_JUDGES[name]["host"] != "openrouter.ai":   # 使った額を数える道が無い窓口に、上限つきでは送らない
        raise OtherJudgeError(None, f"{name} は使った額を数えられないので、--budget-yen つきでは送りません")
    from common import LEDGERS

    KEY_SPEND.bind(key, budget)
    if KEY_SPEND not in LEDGERS:
        LEDGERS.append(KEY_SPEND)
    return _KeyMetered(http, aio)


def _other_error(exc: BaseException) -> OtherJudgeError | None:
    """部品ごとの失敗を、1つの形（OtherJudgeError）にそろえる。ほかの失敗は None（そのまま上げる）。"""
    import anthropic
    import httpx

    if isinstance(exc, anthropic.APIStatusError):
        r = exc.response
        return OtherJudgeError(exc.status_code, str(exc.message)[:300], float(r.headers.get("retry-after") or 0) or None)
    if isinstance(exc, httpx.HTTPStatusError):
        r = exc.response
        return OtherJudgeError(r.status_code, r.text[:300], float(r.headers.get("retry-after") or 0) or None)
    return None


def judge_other(client, name: str, q, text: str, asked: str | None = None, before: str | None = None) -> str | None:
    """judge と同じ（Google 以外の判定。client は other_client で作る）。失敗は OtherJudgeError。"""
    body = other_request(name, q, text, asked, before)
    try:
        if OTHER_JUDGES[name]["maker"] == "anthropic":
            data = client.messages.create(**body).to_dict()
        else:
            r = client.post(other_path(name), json=body)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        err = _other_error(exc)
        if err is None:
            raise
        raise err from exc
    return read(q, other_text(name, data), asked)


async def ajudge_other(client, name: str, q, text: str, asked: str | None = None, before: str | None = None,
                       route: str | None = None) -> str | None:
    """judge_other と同じ（通話の中の形。client は other_client(…, aio=True) で作る。route＝緊急の道なら、client もその道のもの）。"""
    body = other_request(name, q, text, asked, before, route=route)
    try:
        if OTHER_JUDGES[name]["maker"] == "anthropic":
            data = (await client.messages.create(**body)).to_dict()
        else:
            r = await client.post(other_path(name), json=body)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        err = _other_error(exc)
        if err is None:
            raise
        raise err from exc
    return read(q, other_text(name, data), asked)


BATCH_FACTOR = 0.5  # まとめて送る測り（Message Batches）の料金の掛け目：送った量すべて（前の部分の書き込み・読み出しも）が半分


def batch_judge(name: str, key: str, items: list, budget: float | None = None, poll_s: float = 30.0, warm: bool = True, client=None) -> dict:
    """正しさだけを見る測り（答えまでの秒は測らない）を、Anthropic の Message Batches でまとめて送る（唯一の場所。✅ 2026-10-30 ユーザー：「Batch
    もOKです」。料金は送った量すべてが半分・答えはたいてい1時間以内・最長24時間）。items＝[(印, 質問, 言葉, 言い直した言葉, 前の言葉)]（印は
    英数字と「_」「-」の64字まで）。問い合わせの文と読み方は通話と同じ1か所（other_request・other_text・read）。⇒ {印: (読み, 読めなかった理由)}。
    - 毎回同じ前の部分は1時間もつ形にし（バッチの中で効くかは「できる限り」＝Anthropic の説明）、warm なら、前の部分ごとに1つだけ先にふつうに
      送って書いておく（その答えも使う）。⚠️ 前の部分が効かないと、まとめて送るほうが、ふつうに送る（前の部分を約10分の1で読む）より高い
    - budget＝この測りで使ってよい円（--budget-yen）：前の部分が1つも効かなかったとき（いちばん高い）の円を数えるだけの窓口で見込み、超えるなら送らない"""
    import time

    from common import LEDGERS, say

    if OTHER_JUDGES[name]["maker"] != "anthropic":
        raise ValueError(f"まとめて送れるのは Anthropic の判定だけです（{name}）")
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=120)
    if budget is not None:
        SPEND.limit = budget
    if SPEND not in LEDGERS:
        LEDGERS.append(SPEND)
    bodies = {cid: (q, asked, other_request(name, q, text, asked, before, ttl="1h")) for cid, q, text, asked, before in items}
    out: dict = {}

    def take(cid, data: dict) -> None:
        q, asked, _ = bodies[cid]
        got = read(q, other_text(name, data), asked)
        out[cid] = (got, None if got is not None else "読めない返事")
    rest, outs = dict(bodies), []   # outs＝先にふつうに送った問い合わせの、返した量（見込みに使う）
    if warm:
        seen = set()
        for cid, (q, asked, body) in bodies.items():
            fixed = body["messages"][0]["content"][0]["text"]
            if fixed not in seen:
                seen.add(fixed)
                SPEND.check()
                msg = client.messages.create(**body)
                SPEND.add(body["model"], msg.usage)
                outs.append(getattr(msg.usage, "output_tokens", 0) or 0)
                take(cid, msg.to_dict())
                del rest[cid]
    if not rest:
        return out
    if SPEND.limit is not None:
        sample = max((b for _, _, b in rest.values()), key=lambda b: len(json.dumps(b, ensure_ascii=False)))
        n_in = client.messages.count_tokens(**{k: sample[k] for k in ("model", "messages", "tools", "tool_choice", "system") if k in sample}).input_tokens
        p_in, p_out, _, _ = PRICE.get(sample["model"], PRICE["claude-sonnet-5-5"])
        # 返す量は、先にふつうに送った問い合わせの多いほうの2倍（無ければ上限の max_tokens）。⛔ 2026-10-30 の1回目：上限の 4,096トークンで
        #    見込み、実際は約30トークンの返事を1回約3円と数えて、上限の10円で止まった（先に送った2回の分は使った）
        n_out = 2 * max(outs) if outs else sample["max_tokens"]
        worst = len(rest) * (n_in * p_in + n_out * p_out) / 1e6 * BATCH_FACTOR * YEN_PER_USD
        if SPEND.yen + worst > SPEND.limit:
            raise OtherJudgeError(None, f"まとめて送る {len(rest)}回は、前の部分が効かないと約{worst:.0f}円かかり、この試しで使ってよい料金"
                                        f"（{SPEND.limit:.0f}円）を超えるので送りません（--budget-yen）")
        say(f"まとめて送ります：{len(rest)}回（前の部分が1つも効かなくても約{worst:.0f}円まで）")
    batch = client.messages.batches.create(requests=[{"custom_id": cid, "params": body} for cid, (_, _, body) in rest.items()])
    while batch.processing_status != "ended":
        time.sleep(poll_s)
        batch = client.messages.batches.retrieve(batch.id)
        say(f"まとめて送った問い合わせ（{batch.id}）：{batch.request_counts}")
    for r in client.messages.batches.results(batch.id):
        if r.result.type == "succeeded":
            SPEND.add(rest[r.custom_id][2]["model"], r.result.message.usage, factor=BATCH_FACTOR)
            take(r.custom_id, r.result.message.to_dict())
        else:
            out[r.custom_id] = (None, r.result.type)
    return out


def add_judge_args(p) -> None:
    """通話の試験が判定を選ぶ引数（どの試験も、ここから足す＝選び方を試験ごとに書かない）。--judge＝その場の判定（既定 3.5 Flash Lite）・
    --judge2＝同時に出すもう1つの判定（既定 JUDGE2）。止まった判定の代わり（STANDIN）は、判定を使えば入る（--no-standin で外す）。"""
    p.add_argument("--judge", nargs="?", const="3.5 Flash Lite", choices=JUDGE_MODELS, help="意味の判定を使う（DESIGN.md 6-10 の (6)）")
    p.add_argument("--judge2", nargs="?", const=JUDGE2, choices=tuple(OTHER_JUDGES) + JUDGE_MODELS + tuple(GROUPS),
                   help=f"もう1つの判定を同時に出す（DESIGN.md 6-10 の (6)。既定 {JUDGE2}）")
    p.add_argument("--no-standin", action="store_true", help=f"止まった判定の代わり（{STANDIN}）と、お呼びする段の止める側だけの判定（STOP_ONLY）を使わない（比べるときだけ）")
    p.add_argument("--judge-down", action="append", choices=tuple(OTHER_JUDGES) + JUDGE_MODELS,
                   help="試験だけ：この判定が止まっているものとして流す（代わりが受け持つかを確かめる。いくつでも）")
    p.add_argument("--judge-route", choices=(RESUME_EMERGENCY,),
                   help="緊急の道で始める（契約者以上の権限で再開した通話と同じ＝RouteState.emergency_first。本番では発信の門が通話ごとに決める）")
    # 中身の判定（grounding）の顔ぶれ（既定 grounding.GROUNDERS）。「なし」＝中身を確かめられない通話（受け皿の道を通話で確かめる）
    p.add_argument("--grounder", action="append", choices=tuple(OTHER_JUDGES) + JUDGE_MODELS + (NO_GROUNDER,),
                   help="試験だけ：AIの自分の言葉の中身の判定の顔ぶれ（同時に出して早いほう。既定 grounding.GROUNDERS。「なし」で判定を使わない）")


# ── 止まった判定の代わり（✅ 2026-10-19 ユーザー：「どれかがダウンしていたら、Jevも使ったらいいんじゃない」）。
#    2つの判定は会社を分けて同時に出す（✅ 2026-10-19 ユーザー：「同じ会社のモデルだけだと、通信の問題が影響するでしょ」。G-61：同じ会社の
#    2本は遅れが一緒に出て、ほとんど縮まなかった）。1社が止まると、残りの1社だけ＝その会社の遅れがそのまま出て、確かめも1つ減る
#    ⇒ 3つ目の会社の判定（Jev）が、止まった判定の受け持ちに入る。ふだんは使わない（✅ 2026-10-18 ユーザー：「当面は、Sonnetだけを使いましょう」）
STANDIN = "Jev 1.14"
NO_GROUNDER = "なし"
DOWN_AFTER_S = 10.0  # 1回の判定をこの秒で打ち切り、その判定は止まっているとみなす（報告を待たせるのは2秒まで。G-61：Sonnet 5.5 の最長 9.4秒）
DOWN_S = 60.0        # 止まっているとみなした判定に、問い合わせずに代わりへ回す秒（毎回の発話で、止まった判定の答えを待たない）


def maker(name: str) -> str:
    """判定の会社（Google の判定は JUDGE_MODELS）。"""
    return OTHER_JUDGES[name]["maker"] if name in OTHER_JUDGES else "google"


def outage(exc: BaseException) -> bool:
    """判定が止まっている側の失敗か（時間切れ・つながらない・上限・キーが止められた・混雑）。問い合わせの中身が断られた（400 など）は、
    その発話だけ代わりに回し、止まっているとはみなさない。"""
    for v in (getattr(exc, "status", None), getattr(exc, "code", None)):
        if isinstance(v, int) and 400 <= v < 500 and v not in (401, 403, 408, 429):
            return False
    return True


class NoJudgment(RuntimeError):
    """受け持ちの判定も代わりも、答えを返さなかった（tried＝問い合わせた判定ごとの、答えが無かった理由）。"""

    def __init__(self, tried: list[dict]):
        def why(t: dict) -> str:
            if t.get("error"):
                return t["error"]
            if "stop_only" in t:
                return f"止める側だけの読み：{t['stop_only']}"
            return "緊急の道で答えた" if t.get("used") else "確からしさが線より下"
        super().__init__("判定が届かなかった：" + "・".join(f"{t['model']}（{why(t)}）" for t in tried))
        self.tried = tried


# 組の中で先に出す判定と、その確かな読みを待つ秒（first_of の lead）。✅ 2026-10-29 ユーザー：「(1) では、Jev が『確かでない』と返したら、
#    その時点で Sonnet を出します。OK」＝⛔ 3つを同時に出すと、Jev が確かな読みで先に答えた発話（意味の判定で6割）でも、2つの Sonnet の料金を
#    払っていた（送った問い合わせは、止めても料金がかかる）。記録の読みと秒で数えると、正しさは1件も変わらず、Sonnet は意味の判定で 58%・
#    中身の判定で 70% 減る（PHASE0 G-59 の 10-29・phase0/judge_cost.py）。Jev の答えまでは中央 0.16秒
#    ✅ 2026-10-30：先に出すのは組の中の判断のモデル全部（decision_model＝Jev・d1-ultra。同時に出す。d1-ultra の答えまでは中央 0.19秒）。
#    G-59 の6回りで、2つがどちらも確かなときに読みが食い違った回は0（目的のお客様の判定21回・尋ねているか243回）＝先に届いた確かな読みで決める
LEAD_WAIT_S = 0.3


def leads_of(names) -> tuple[int, ...]:
    """組の名前の並びのうち、先に出すもの（判断のモデル）の番号（first_of の leads。JudgePanel・grounding.make_grounder が使う）。"""
    return tuple(i for i, n in enumerate(names) if decision_model(n))


async def first_of(starts, usable, leads: tuple[int, ...] = (), wait_s: float = LEAD_WAIT_S, on_rest=None):
    """先に届いた使える答えで決める（唯一の場所。JudgePanel の受け持ち・grounding.make_grounder）：starts＝問い合わせを始める関数の並び
    （引数なし・答えを返す）。usable(答え) が真の、最初に届いた答えを返し、残りの問い合わせは止める。どれも無ければ None。
    失敗した問い合わせ・None の答えは待たない（ほかの答えを待つ）。
    leads＝先に出すもの（starts の番号。安くて速い判断のモデル＝leads_of）：それだけを同時に先に出し、使える答えが1つ届けばそれで決める（ほかは
    出さない）。先に出したものが全部、使えない答え・失敗を返したとき、または wait_s 秒たっても使える答えが無いときに、ほかを同時に出す（まだ
    答えていない先のものは、そのまま待つ）。leads が無いか、全部が先のものなら、全部を同時に出す。
    on_rest＝先に出したもの以外（確からしさを返さない判定）を出す時点で呼ぶ（引数なし。先に出すものが無ければ初めに。全部が先のものなら呼ばない＝
    JudgePanel が、組が確かな読みを返せないと分かった時点で、代わりも裏で出しておく）。"""
    import asyncio

    jobs, rest = [], True
    if leads and len(leads) < len(starts):
        pending = {asyncio.ensure_future(starts[i]()) for i in leads}
        deadline = asyncio.get_running_loop().time() + wait_s
        while pending:
            done, pending = await asyncio.wait(pending, timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
                                               return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for d in done:
                try:
                    got = d.result()
                except Exception:
                    got = None
                if got is not None and usable(got):
                    for p in pending:
                        p.cancel()
                    return got
        jobs += list(pending)
        starts = [s for i, s in enumerate(starts) if i not in leads]
    elif leads:
        rest = False   # 全部が先のもの（判断のモデルだけの組）：ほかを出す時点は無い
    if rest and on_rest is not None and starts:
        on_rest()
    jobs += [asyncio.ensure_future(start()) for start in starts]
    try:
        for done in asyncio.as_completed(jobs):
            try:
                got = await done
            except Exception:
                continue
            if got is not None and usable(got):
                return got
        return None
    finally:
        for j in jobs:
            if not j.done():
                j.cancel()


SAME_MAKER = "ほかの受け持ちが同じ会社の判定で答えた"
FAILED = object()  # routed_call：どの道でも答えが無かった（止まった・断られた。読めない返事＝None とは別）


class RouteState:
    """判定の道（直接・緊急の道）の状態の唯一の場所：止まっているとみなした判定と道（down_until。DOWN_S のあいだ）・契約者以上の権限で再開した通話
    （emergency_first）・止まったことの知らせ先（on_trouble＝通話の流れの制御の Flow.on_ai_trouble。会話の側が通話の始めに入れる）・試験で止めた判定
    （forced_down）。意味の判定（JudgePanel）と中身の判定（grounding.make_grounder）が同じものを使う＝どちらかで Anthropic が止まったと分かれば、
    両方が緊急の道へ回る。"""

    def __init__(self, down=(), clock=None):
        import time

        self.forced_down = set(down)   # 試験だけ（--judge-down）
        self.down_until: dict[str, float] = {}
        self.clock = clock or time.monotonic
        self.emergency_first = False
        self.on_trouble = None

    def _down(self, name: str) -> bool:
        return self.down_until.get(name, 0.0) > self.clock()

    def usable(self, name: str, urgent=None) -> bool:
        """問い合わせてよいか：止まっていない、または止まっていても緊急の道がある（緊急の道そのものも止まっていない）。"""
        return not self._down(name) or (urgent is not None and not self._down(name + EMERGENCY_ROUTE))


async def routed_call(state: RouteState, name: str, direct, urgent, tried: list[dict], timeout: float | None = None):
    """判定 name に1回問い合わせる唯一の場所（道の選び方・止まった印・知らせ）：direct／urgent＝引数なしで問い合わせる関数（urgent＝緊急の道。
    無ければ None）。直接が止まっている（DOWN_S のあいだ）か、契約者以上の権限で再開した通話（emergency_first）なら緊急の道で、直接が止まる
    （outage）と同じ問い合わせを緊急の道で送り直す。✅ 2026-10-30 夜 ユーザー：緊急の道は「会話の途中などでアンソロピックが使えなくなった緊急
    事態の時のみ」・「アンソロピックが使えなくなったりその他に使う AI のどれかが止まったら、AI の不具合として回復するまで一時停止」＝止まった
    （outage）こと・緊急の道で答えたことは、tried に残し、state.on_trouble に知らせる（通話の報告の ai_trouble＝発信の門が新しい電話を止める）。
    返す：答え（読めない返事は None）か FAILED。timeout＝1回の問い合わせの打ち切りの秒（無ければ DOWN_AFTER_S を、呼ぶたびに読む）。"""
    import asyncio

    from common import redact

    timeout = DOWN_AFTER_S if timeout is None else timeout
    def note(entry: dict) -> None:
        tried.append(entry)
        if (entry.get("outage") or entry.get("used")) and state.on_trouble is not None:
            state.on_trouble(entry)
    t0 = state.clock()
    if urgent is None or not (state.emergency_first or state._down(name)):
        try:
            if name in state.forced_down:
                raise OtherJudgeError(503, "試験：止まっているものとして扱う")
            return await asyncio.wait_for(direct(), timeout)
        except Exception as exc:
            down = outage(exc)
            if down:
                state.down_until[name] = state.clock() + DOWN_S
            note({"model": name, "error": redact(repr(exc))[:160], "s": round(state.clock() - t0, 3), "outage": down})
            if not down or urgent is None:
                return FAILED
    if state._down(name + EMERGENCY_ROUTE):
        return FAILED
    t1 = state.clock()
    try:
        got = await asyncio.wait_for(urgent(), timeout)
    except Exception as exc:
        down = outage(exc)
        if down:
            state.down_until[name + EMERGENCY_ROUTE] = state.clock() + DOWN_S
        note({"model": name, "route": EMERGENCY_ROUTE, "error": redact(repr(exc))[:160], "s": round(state.clock() - t1, 3), "outage": down})
        return FAILED
    note({"model": name, "route": EMERGENCY_ROUTE, "used": True, "s": round(state.clock() - t1, 3)})
    return got


class JudgePanel(RouteState):
    """通話の中の意味の判定の受け持ち（唯一の場所。DESIGN.md 6-10 の (6)）：受け持ち（judge・judge2）ごとに、決めた判定の組へ同時に問い合わせ、
    先に届いた確かな読みで決める（組＝members。1つだけの組もある）。受け持ちの組のどれも答えなかった（止まっている・上限・時間切れ・読めない返事・
    確からしさが線より下）ときは、代わり（STANDIN）に問い合わせる。
    - 1つの発話で、2つの受け持ちの答えは違う会社から（同じ会社だと、独立した2つの確かめにならない・止まるのも遅れるのも一緒）：
      ほかの受け持ちが答えた会社の読みは使わない。代わりが入っている間は、ほかの受け持ちの組から代わりと同じ会社を外す
    - 代わりが入るのは1つの受け持ちだけ。代わりは、自分の組に同じ会社の判定が無く、ほかの受け持ちに違う会社の判定が残る受け持ちだけ（serves）
    - 止まっている（outage）とみなした判定は DOWN_S 秒、問い合わせずに組のほか・代わりへ
    - 確からしさを返す判定（Jev）は、線（OTHER_JUDGES の sure_min）より下なら答えにしない＝その判定は届かなかった（組のほかの判定・代わりを
      待つ。どれも無ければ、もう1つの受け持ちで決め、通話後の検証へ。✅ 2026-10-18 ユーザーの案「このスコア以上ならJevで間違いなく正確というライン」）
    - 質問の種類によって止める側だけになる判定（stop_only＝お呼びする段の 3.5 Flash Lite）の受け持ちは、その質問には自分で答えず（answers）、
      ほかの受け持ちの組が確かな読みを返さないと分かった時点で裏で出す（backup＝False なら出さない。試しで比べるときだけ）。その読みは答えに
      しない＝組の答えが待つ長さに間に合わなければ会話の側が（early）、組が答えられなければ NoJudgment の tried の stop_only で、止める側だけに使う
    - 緊急の道（EMERGENCY）を持つ判定（Sonnet 5.5）は、直接が止まっている（outage・DOWN_S のあいだ）か、契約者以上の権限で再開した通話
      （emergency_first）なら、同じ問い合わせを緊急の道で送る（_try の1か所）。✅ 2026-10-30 夜 ユーザー：緊急の道は「会話の途中などで
      アンソロピックが使えなくなった緊急事態の時のみ」
    - どの判定でも、止まった（outage）ことは tried に outage の印で残す（会話の側が Flow.on_ai_trouble で通話の報告の ai_trouble に入れる＝
      新しい電話を一時停止するのは発信の門＝CL-2）。緊急の道で答えたときも tried に残す
    どれを使って答えたかは、答え（Reading）の by・tried に残す。"""

    def __init__(self, judges: dict, standin: tuple | None = None, down=(), clock=None, backup: bool = True):
        from collections import OrderedDict

        super().__init__(down, clock)

        # 受け持ち → ((判定の名前, async (質問, 言葉, こちらが言い直した言葉) -> Reading), …)。1つの (名前, 関数) も受ける
        self.judges = {k: (v,) if isinstance(v[0], str) else tuple(v) for k, v in judges.items()}
        self.standin = standin
        self.serves = set()
        if standin:
            s = maker(standin[0])
            self.serves = {k for k, ms in self.judges.items() if s not in {maker(n) for n, _ in ms}
                           and all(any(maker(n) != s for n, _ in other) for k2, other in self.judges.items() if k2 != k)}
            if not self.serves:
                self.standin = None
        self.holder: str | None = None  # 代わりが入っている受け持ち
        self.answered: OrderedDict = OrderedDict()  # 発話 ⇒ {受け持ち: (答えた会社, 時刻)}（同じ発話で、2つの受け持ちを同じ会社にしない）
        self.backup = backup
        # emergency_first（RouteState）＝契約者以上の権限で、緊急の道を使って再開した通話（✅ 2026-10-30 夜 ユーザー：「どうしても使わなければいけない
        # シーンが出たときは、契約者以上の権限でOpenRouter を通して Google が預かる Claudeを使って再開することができるようにしておいてください。」）：
        # 直接を試さずに緊急の道で送る（1回目の問い合わせで時間切れまで待たない）。決めるのは発信の門（CL-2）で、通話の始めに会話の側が入れる
        # （run_call の judge_route）

    def slot(self, kind: str):
        """受け持ち kind の判定（convo.check_handler に渡す形。panel＝この受け持ちの仕組み・answers＝その質問に自分で答えるか）。"""
        async def run(q, text, asked=None, before=None, early=None):
            return await self.ask(kind, q, text, asked, before, early)
        run.panel = self
        run.answers = lambda q: self.answers(kind, q)
        return run

    def _parts(self, kind: str, qkind: str) -> list:
        """受け持ち kind の判定のうち、組の中の分け（takes_part）で qkind の質問に答えるもの（1つだけの受け持ちは、その判定）。"""
        ms = self.judges[kind]
        return [(n, f) for n, f in ms if len(ms) == 1 or takes_part(n, qkind)]

    def _own(self, kind: str, qkind: str) -> list:
        """受け持ち kind が qkind の質問に自分で出す判定：組の中の分けのうち、ほかの受け持ちに自分で答える判定があれば、止める側だけの判定
        （stop_only）を除く（ほかの受け持ちが無い・ほかも止める側だけなら、自分で答える＝1つの判定で測るとき）。"""
        mine = self._parts(kind, qkind)
        if any(not stop_only(n, qkind) for k in self.judges if k != kind for n, _ in self._parts(k, qkind)):
            mine = [(n, f) for n, f in mine if not stop_only(n, qkind)]
        return mine

    def answers(self, kind: str, q) -> bool:
        """受け持ち kind が、質問 q に自分で答えるか（答えない受け持ちには、convo.check_handler・G-59 が問い合わせない）。"""
        return bool(self._own(kind, question_kind(q)))

    def _backups(self, kind: str, qkind: str) -> list:
        """受け持ち kind の組が確かな読みを返さないときに、裏で出す止める側だけの判定：ほかの受け持ちの、qkind の質問では自分で答えない判定
        （stop_only）。kind の判定と同じ会社のものは入れない（同じ会社だと、止まるのも誤るのも一緒）。"""
        if not self.backup:
            return []
        makers = {maker(n) for n, _ in self._own(kind, qkind)}
        return [(n, f) for k in self.judges if k != kind for n, f in self._parts(k, qkind)
                if (n, f) not in self._own(k, qkind) and maker(n) not in makers]

    def reset(self) -> None:
        """止まっているとみなした印と、代わりが入っている受け持ちを消す（判定の読みを言葉ごとに測る試験だけ：1つの言葉の時間切れで、
        次の言葉からの問い合わせを飛ばさない。⛔ 2026-10-22 G-59：3.5 Flash Lite の時間切れ1回のあと、残りの言葉を全部飛ばして数えた）。"""
        self.down_until.clear()
        self.holder = None
        self.answered.clear()

    async def probe(self) -> list[dict]:
        """回復の確かめの判定の分（唯一の場所。入口は convo.check_ais）：受け持ちのどの判定（止まった判定の代わりも）にも、決まった短い質問
        （probe_question）を直接の道で1回ずつ同時に送り、答えたかを返す＝[{"ai": 判定の名前（通話の報告の ai_trouble と同じ）, "ok": 答えたか,
        "error": 失敗の中身, "s": 秒}]。読みの中身は見ない（どの読みでも、答えが返れば回復）。時間切れは通話と同じ DOWN_AFTER_S。止まった印には
        触らない（印は通話ごと）。✅ 2026-10-30 夜 ユーザー：「…その他に使う AI のどれかが止まったら、AI の不具合として回復するまで一時停止して
        ください。」＝発信の門（CL-2）が、AIの不具合で止めた新しい電話を再開する前に呼ぶ。"""
        import asyncio

        from common import redact
        q = probe_question()
        named: dict = {}
        for ms in self.judges.values():
            for n, f in ms:
                named.setdefault(n, f)
        if self.standin:
            named.setdefault(*self.standin)

        async def one(name, fn):
            t0 = self.clock()
            try:
                if name in self.forced_down:
                    raise OtherJudgeError(503, "試験：止まっているものとして扱う")
                await asyncio.wait_for(fn(q, PROBE_TEXT, None, before=None), DOWN_AFTER_S)
                return {"ai": name, "ok": True, "s": round(self.clock() - t0, 3)}
            except Exception as exc:
                return {"ai": name, "ok": False, "error": redact(repr(exc))[:160], "s": round(self.clock() - t0, 3)}
        return list(await asyncio.gather(*(one(n, f) for n, f in named.items())))

    async def _try(self, name: str, fn, q, text: str, asked: str | None, before: str | None, tried: list[dict]) -> Reading | None:
        t0 = self.clock()
        urgent = getattr(fn, "emergency", None)
        got = await routed_call(self, name, lambda: fn(q, text, asked, before=before),
                                (lambda: urgent(q, text, asked, before=before)) if urgent is not None else None, tried)
        if got is FAILED:
            return None
        if got is None:
            tried.append({"model": name, "error": "読めない返事", "s": round(self.clock() - t0, 3)})
            return None
        if not sure_reading(name, q, got):  # 確からしさを返す判定（Jev・d1）は、確かな読み（線から上・言い切りが幅の外）だけを答えにする
            tried.append({"model": name, "unsure": str(got), "sure": got.sure, "s": round(self.clock() - t0, 3)})
            return None
        return got

    def _taken(self, kind: str, key) -> set[str]:
        """この発話で、ほかの受け持ちが答えに使った会社と、代わりがほかの受け持ちに入っているならその会社（kind の答えに使わない）。"""
        fresh = self.clock() - DOWN_AFTER_S - 1.0  # 1つの発話の2つの受け持ちは、この秒のうちに答える（同じ言葉の前の発話の答えは見ない）
        taken = {m for k, (m, t) in self.answered.get(key, {}).items() if k != kind and t >= fresh}
        if self.standin and self.holder not in (None, kind):
            taken.add(maker(self.standin[0]))
        return taken

    def _keep(self, kind: str, key, name: str) -> None:
        self.answered.setdefault(key, {})[kind] = (maker(name), self.clock())
        self.answered.move_to_end(key)
        while len(self.answered) > 64:
            self.answered.popitem(last=False)

    async def ask(self, kind: str, q, text: str, asked: str | None = None, before: str | None = None, early=None) -> Reading:
        """受け持ち kind の判定で、言葉 text を読む。early＝組が確かな読みを返せないと分かった時点で裏で出した止める側だけの判定（STOP_ONLY）
        の読みが、組の答えより先に届いたら知らせる先（引数は読み。会話の側が、組の答えが待つ長さに間に合わなかったときだけ、止める側だけに
        使う＝convo.check_handler。✅ 2026-10-30 ユーザー：「(1)(2)ともお勧めの形でOK」）。"""
        import asyncio

        tried: list[dict] = []
        # 発話と質問ごと（⚠️ 呼んでいる段の、目的のお客様かの質問と、こちらに尋ねているかの質問は、同じ番号＝選べるもので分ける）
        key = (getattr(q, "id", None), tuple(getattr(q, "conditions", ()) or ()), text, asked, before)
        live = []
        qkind = question_kind(q)
        own = self._own(kind, qkind)   # 組の中で、この種類の質問に答える判定（takes_part）・止める側だけの判定を除いたもの（stop_only）
        if not own:  # この質問では止める側だけの受け持ち（answers）：問い合わせない（ほかの受け持ちが確かな読みを返さないと分かった時点で、そちらが裏で出す）
            raise NoJudgment([{"model": n, "error": "この質問では止める側だけ"} for n, _ in self.judges[kind]])
        for name, fn in own:
            if not self.usable(name, getattr(fn, "emergency", None)):
                tried.append({"model": name, "error": "止まっている（前の問い合わせ）"})
            elif maker(name) in self._taken(kind, key):
                tried.append({"model": name, "error": SAME_MAKER})
            else:
                live.append((name, fn))

        def start(name, fn):
            async def run():
                got = await self._try(name, fn, q, text, asked, before, tried)
                return None if got is None else (name, got)
            return run

        def usable(pair) -> bool:
            if maker(pair[0]) in self._taken(kind, key):  # 問い合わせている間に、ほかの受け持ちが同じ会社で答えた
                tried.append({"model": pair[0], "error": SAME_MAKER})
                return False
            return True
        backups = self._backups(kind, qkind)
        ahead = None  # 裏で出しておいた代わり（名前, 問い合わせ）

        def start_ahead():
            # 組の先に出す判定（d1-ultra・Jev）から確かな読みが届かず、ほかの判定（考えない Sonnet）を出す時点（first_of の on_rest）：代わりも
            # 裏で出しておく（混んだときの備え＝組の答えが待つ長さに間に合わないとき、会話の側が止める側だけに使う）。⛔ 2026-10-30：代わりを組が
            # 答えられなかったあとにだけ出していたので、Anthropic が混んで遅いだけのときは、10秒の時間切れまで入らなかった（Intent.md 9-4 の J10b）
            nonlocal ahead
            ready = [(n, f) for n, f in backups if not self._down(n) and maker(n) not in self._taken(kind, key)]
            if not ready:
                return
            bname, bfn = ready[0]

            async def go():
                got = await self._try(bname, bfn, q, text, asked, before, tried)
                if got is not None and early is not None:
                    early(self._mark(got, bname, ()))
                return got
            ahead = (bname, asyncio.ensure_future(go()))
        won = await first_of([start(n, f) for n, f in live], usable, leads=leads_of([n for n, _ in live]),
                             on_rest=start_ahead if backups else None)
        if won is not None:
            if ahead is not None:
                ahead[1].cancel()  # 組が答えた：裏の代わりは使わない
            if self.holder == kind:
                self.holder = None
            self._keep(kind, key, won[0])
            return self._mark(won[1], won[0], tried)
        # 受け持ちの判定がどれも答えなかった：確かでない読み（流れの制御が数える＝Reading.weak・NoJudgment の unsure）と、止める側だけの判定の読み
        # （答えにはしない＝NoJudgment の tried の stop_only。会話の側が止める側だけに使う）・止まった判定の代わり STANDIN の順
        weak = next((t["unsure"] for t in tried if "unsure" in t), None)
        if ahead is not None:  # 裏で出しておいた止める側だけの判定：その読みを待つ（同じ判定に2回は問い合わせない）
            bname, job = ahead
            got = await job
            if got is not None:
                tried.append({"model": bname, "stop_only": str(got)})
            backups = []
        for bname, bfn in backups:
            if self._down(bname):
                tried.append({"model": bname, "error": "止まっている（前の問い合わせ）"})
            elif maker(bname) in self._taken(kind, key):
                tried.append({"model": bname, "error": SAME_MAKER})
            elif (got := await self._try(bname, bfn, q, text, asked, before, tried)) is not None:
                tried.append({"model": bname, "stop_only": str(got)})
                break
        if self.standin is None or kind not in self.serves or self.holder not in (None, kind):
            raise NoJudgment(tried)
        sname, sfn = self.standin
        if maker(sname) in self._taken(kind, key):
            tried.append({"model": sname, "error": SAME_MAKER})
            raise NoJudgment(tried)
        self.holder = kind
        got = await self._try(sname, sfn, q, text, asked, before, tried)
        if got is None:
            raise NoJudgment(tried)
        self._keep(kind, key, sname)
        return self._mark(got, sname, tried, weak)

    @staticmethod
    def _mark(got: Reading, name: str, tried: list[dict], weak: str | None = None) -> Reading:
        got.by, got.tried, got.weak = name, tuple(tried), weak
        return got


def make_judges(args) -> dict:
    """add_judge_args で選んだ判定を、run_call にそのまま渡せる形で（{"judge": …, "judge2": …, "grounder": …}。選ばれていなければ None）。
    2つは1つの JudgePanel の受け持ち（止まった判定の代わりを、2つで分け合う）。組の名前（GROUPS）は、組の判定を同時に出す受け持ち。"""
    from common import say

    names = {k: getattr(args, k, None) for k in ("judge", "judge2")}
    used = {k: n for k, n in names.items() if n}
    if not used:
        return {k: None for k in names} | {"grounder": None}
    standin = None if getattr(args, "no_standin", False) else STANDIN
    panel = JudgePanel({k: tuple((m, make_judge(args, m)) for m in members(n)) for k, n in used.items()},
                       standin=(standin, make_judge(args, standin)) if standin else None, down=getattr(args, "judge_down", None) or (),
                       backup=not getattr(args, "no_standin", False))
    panel.emergency_first = getattr(args, "judge_route", None) == RESUME_EMERGENCY   # 測りの道具（通話を使わない）。通話は run_call の judge_route
    if standin and panel.standin is None:
        say(f"止まった判定の代わり（{standin}）は、どの受け持ちにも入れない（同じ会社の判定が組にある・ほかの受け持ちが同じ会社だけ）ので使いません")
    # AIが自分の言葉で言う文の中身の判定（grounding）も、判定を使う通話では一緒に作る（顔ぶれは grounding.GROUNDERS＝危ない文を通さないと
    # 測ったものだけ。意味の判定の顔ぶれとは別）。⛔ 2026-10-22 G-63：言葉で見る確かめは、通すべき世間話・お礼を12文止め、作り話を2文通した
    from grounding import GROUNDERS, make_grounder
    chosen = tuple(getattr(args, "grounder", None) or GROUNDERS)
    grounder = None if NO_GROUNDER in chosen else make_grounder(args, chosen, state=panel)   # 道の状態は意味の判定と同じもの（RouteState）
    return {k: panel.slot(k) if k in used else None for k in names} | {"grounder": grounder}


def make_judge(args, model_name: str):
    """通話の中で使う形：async (質問, 相手の言葉, こちらが言い直した言葉) -> 中の名前。キーは会話と同じく、上限・止められたキーを飛ばす
    （common.KeyRing.acall）。どの試験も、これを run_call(judge=…) に渡す（作り方を試験ごとに書かない）。"""
    from common import KeyRing, load_secret

    if model_name in OTHER_JUDGES:
        client = other_client(model_name, load_secret(OTHER_JUDGES[model_name]["key"], args.env), aio=True,
                              budget=getattr(args, "budget_yen", None))

        async def other(q, text, asked=None, before=None):
            return await ajudge_other(client, model_name, q, text, asked, before)
        route = OTHER_JUDGES[model_name].get("emergency")
        if route is not None:
            # 緊急の道（同じ判定・同じ問い合わせを、別の送り先で。使うかは JudgePanel._try の1か所が決める）。鍵が無ければ、緊急の道は無い
            try:
                urgent_client = other_client(model_name, load_secret(route["key"], args.env), aio=True,
                                             budget=getattr(args, "budget_yen", None), route=EMERGENCY_ROUTE)
            except (Exception, SystemExit) as exc:  # 鍵が無い（load_secret は名前だけを言って止める）・作れない
                from common import say
                say(f"{model_name} の緊急の道は使えません（{type(exc).__name__}）")
            else:
                async def urgent(q, text, asked=None, before=None):
                    return await ajudge_other(urgent_client, model_name, q, text, asked, before, route=EMERGENCY_ROUTE)
                other.emergency = urgent
        return other
    ring = KeyRing(args)
    model = ring.model_ids()[model_name]

    async def run(q, text, asked=None, before=None):
        # どのキーも上限で休み中なら、待たずに失敗にする（通話の中では待っても間に合わない＝JudgePanel が代わりへ回す）
        got, _ = await ring.acall(model, lambda client: ajudge(client, model, q, text, asked, before), max_wait_s=0.0)
        return got
    return run
