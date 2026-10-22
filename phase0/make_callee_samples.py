"""相手役の声の試験材料を作る（G-11・G-14・G-16 で使う）。

やること：相手役の短い発話を、会話に使う声（Sadachbia・Leda）とは別の声で gemini-3.8-live に読ませ、
電話の音質（8kHz）に落としてから 16kHz の WAV にして results/callee/ に保存する。
作った声は、その場で別の耳（listen.hear）に聞かせて確かめる（sample_ok）。通らなければ、--tries 回まで作り直す：
- 読みが台本と合うこと（listen.differences が空）
- 問いかけの文（「？」で終わる文）は、別の耳の文字も「？」で終わること
  ⛔ 2026-10-13 G-22：「はい？」（聞き返し）の声が、別の耳には「はい。」と聞こえていた。読みだけを比べていたので見逃し、
     AIが「はい」（答え）と受け取ったのを、AIの誤りと数えるところだった ⇒ 問いかけは「？」まで確かめる。読み方の指示も渡せるようにした
⚠️ 限界：作った声であって、人の声ではない。人の声での確かめは、別に録った音声で行う（G-14）。
   「？」は、別の耳が語尾の上がりを聞き取った印として使う（声の高さそのものは測っていない）。
"""
from __future__ import annotations

import asyncio
import json
import time

from audio import duration_s, phone_quality
from common import (HERE, base_parser, describe_error, make_client, pick_key, read_wav_pcm16_mono, require_python, save_result,
                    say, write_wav)
from listen import differences, hear
from live_util import build_config, collect_turn, send_text

# 相手役の声。Puck・Kore は、会話に使う声とは別の声（これまでの試験の材料。作り直さない）。
# ✅ 2026-10-14 ユーザー：「相手役があまりにも機械っぽい声で臨場感がないので、Leda と Sadachbia に、入れ替わりでお客役をやらせたら
#    いいんじゃないですか？話し方もお客っぽいものに変更して！」⇒ Leda・Sadachbia をお客の話し方で作る。会話のAIが Sadachbia（S2・S4）の
#    ときは Leda が、Leda（L4）のときは Sadachbia がお客役になる（convo.callee_voice_for）。同じ声どうしで話させない
CALLEE_VOICES = ("Puck", "Kore", "Leda", "Sadachbia")
UTTERANCES = {
    "hello": "はい、もしもし。",
    "yes_reserved": "はい、予約しています。",
    "no_not_yet": "いいえ、まだしていません。",
    "datetime_pm": "15日の午後2時でお願いします。",
    "datetime_ambiguous": "15日の2時です。",
    "affirm": "はい、そうです。",
    "unclear": "えっと、じゅう、の……",
    # 2026-10-13：録音の名乗りへの聞き返し・途中で話し始める場面（DESIGN.md 6-13 の案を確かめる）
    "whois": "はい、どちら様ですか？",
    "what_for": "はい、何のご用件でしょうか？",
    "again": "すみません、もう一度お願いできますか？",
    "hai_q": ("はい？", "相手の言葉が聞き取れなかったので、語尾をはっきり上げて、聞き返すように"),
    "busy": "すみません、今ちょっと忙しいんですけど。",
    "transfer": "担当の者に代わりますので、少々お待ちください。",
    "taken_over": "お電話代わりました。担当の山田です。",
    "barge_who": "あ、はい、どちら様？",
    "barge_what": "はい、何でしょう？",
    "barge_yes": "あ、予約の件ですね。はい、してます。",  # ⛔ 別の耳には「はい、知ってます」と聞こえた（意味が変わる）⇒ 下の言い方に替えた
    "barge_yes2": "あ、予約の件ですね。はい、予約しています。",
    "backchannel": "はい。",
    # G-30（会話の間）：言葉の途中で間をあける相手。「えーとですね、」＋無音＋「3人です。」をつないで使う（AIが間に割り込むかを数える）
    "filler_eto": "えーとですね、",
    # ⛔ 2026-10-15 G-37：Sadachbia の声は、会話AIに 31回中14回「寒いです」「32 です」などと聞き取られた（別の耳の確かめは通っていた）。
    #    試験の相手役の声の誤りで、値が取れなかった2本の原因 ⇒ 「さんにん」をはっきり読む読み方を添えて、Sadachbia の声だけ作り直す
    "three_people": ("3人です。", "「さんにん」の「ん」まで、はっきりと"),
    # 相手が答えを変えた（最初に「15日」と言ったあと）。⚠️ G-36 の2回目：AIは4本とも「相手が言い直した」と報告した＝台本どおりなら
    # 正しい読み（AIは「15日」を正しく聞いていた）。こちらの聞き違いの場面は、下の said_16 で作る
    "correct_16": "いえ、16日の午後2時です。",
    # こちらの聞き違い：相手は「16日と言った」と言う（相づち「失礼いたしました。」＋「改めまして、」）。
    # ⛔ 2026-10-14 の1回目「いえ、…」：Leda の声は5回とも、別の耳に「え、…」と聞こえた（答えの言葉が変わる）⇒「いや、」に替えた
    "said_16": "いや、16日って言ったんですけど。",
    # 日時を決めない答え（✅ 2026-10-14 ユーザー：「いつでも」「なるべく早く」は「即」＝よい答え。「-」はありえない）
    "itsudemo": "いつでも大丈夫です。",
    # 相手の言い間違い：相手が自分で言い直す（✅ 2026-10-14 ユーザー：「承知しました。それでは、16日の午後2時で、よろしいでしょうか？」）
    "restate_16": "あ、すみません、間違えました。16日の午後2時でお願いします。",
    # 人数の質問への、数になっていない答え（数の型の形の確かめ。聞き返されたら「3人です。」と答え直す）
    "kazoku": "家族で行こうと思ってます。",
    # 名前の読みを直す（G-45。こちらが「なかじま」と言ったのを、相手が「なかしま」と直す。カタカナで書いて読みを決める＝G-44 で、
    # カタカナの「ナカシマ」は2本とも なかしま と読んだ（「ヤマサキ」はどの書き方でも やまざき になったので使わない）。
    # ⛔「いえ、」は Leda の声で「え、」「ええ、」に聞こえた（2026-10-14 と同じ）⇒「いや、」
    "correct_nakashima": "いや、ナカシマです。",
    "name_only_nakashima": "ナカシマです。",  # 打ち消しの言葉を言わずに、名前だけを言い直す
    # 読みの候補が3つある名前（G-46。「角田」＝つのだ・かくた・かどた。こちらが「つのだ」と言ったのを、相手が直す）
    "correct_kakuta": "いや、カクタです。",
    "correct_kadota": "いや、カドタです。",
    "name_only_kadota": "カドタです。",
    # こちら側の名前（読みが決まらない担当者以外の人の名前）を、AIが口にしてしまうかを見る（G-47）。答えと一緒に、当日の担当を聞く
    "ask_staff": "はい、予約しています。当日はどなたが担当されるんですか？",
    # 2026-10-18：電話ならではの場面（Intent.md 9-4・16-2 の模擬通話の回帰。scenes.py）
    # 「ピー」は、Sadachbia の声が3回とも「ピーッ」と読み、読みの比べで落ちた ⇒ 「ピーッ」に合わせた（Leda も同じ文で作り直す）
    "voicemail": ("ただいま電話に出ることができません。ピーッという発信音のあとに、お名前とご用件をお話しください。",
                  "留守番電話の案内の録音のように、ていねいに、落ち着いた平らな調子で"),
    # 「そのほか」は別の耳が「その他」と書き、読みの比べで落ちた（2回）⇒ 書き方を合わせた
    "ivr": ("お電話ありがとうございます。ご予約の方は1番を、その他のお問い合わせの方は2番を押してください。",
            "会社の自動音声の案内の録音のように、ていねいに、平らな調子で"),
    "dnc": "営業の電話ですよね。もうかけてこないでください。",
    "refuse_sales": "すみません、営業のお電話はお断りしてるんですよ。",
    "complaint": ("何回電話してくるんですか。迷惑なんですけど。", "少し怒って、いらいらした感じで"),
    "ai_q": "これって、AIですか？",
    # 「何か問題でもございましたでしょうか？」への返事（✅ 2026-10-19 ユーザーの言い方）：問題は無い／重ねて聞く
    "ai_fine": "いや、ちょっと気になっただけです。",
    "ai_again": ("本当に人ですか？", "少し疑うように"),
    "out_of_scope": "あ、ついでに聞きたいんですけど、キャンセル料っていくらかかりますか？",
    # 「お間違え」は、声も別の耳も「お間違い」になった（3回とも）⇒ 話し言葉の「お間違い」に合わせた
    "wrong_number": "え、うちは山田ですけど、番号お間違いじゃないですか？",
    "absent_family": "あ、本人は今ちょっと出かけてるんですけど。",
    "later": "今ちょっと手が離せないので、あとでかけてもらえますか？",
    "other_number": "この番号じゃなくて、携帯のほうにかけてもらえますか？",
    "number_answer": "090の1234の5678です。",
    # 「ごろ」と書くと、読みの比べが別の耳の「頃」を「ころ」と読んで落ちた（声は正しい。3回とも）⇒ 書き方を「頃」に合わせた
    "back_5pm": "夕方の5時頃には戻ると思います。",
    "later_time": "そうですね、夜の7時以降なら大丈夫です。",
    "reachable_dunno": "ちょっと分からないです。",
    # 目的の人のある業務（DESIGN.md 4-3。目的の人＝営業部部長の岡田様）
    "target_self": "はい、岡田は私ですが。",
    "hold_target": "岡田ですね。少々お待ちください。",
    "taken_over_okada": "お電話代わりました。岡田です。",
    "taken_over_unnamed": "はい、お電話代わりました。",
    # 目的の人かを確かめたら、別の方だった（2026-10-21 ユーザー：「二つ以上揃わなかった場合に関しても正しく設計できているんですか？」で足した。
    # 確かめに「いいえ」と答えた方には、同じ方にもう一度確かめず、目的の人をお呼びし直す＝flowctl.Flow._call_target）
    "not_okada": "いいえ、私は山田と申します。",
    # ✅ 2026-10-22 ユーザーのご質問から：名前を確かめたあとに名乗りを聞かれる（話の途中が質問）・違うお名前の方がお呼びした方の不在を伝える
    "okada_whois": "岡田ですね。失礼ですが、どちら様でしょうか？",
    "taken_over_absent": "お電話代わりました。担当の山田です。あいにく岡田は本日不在にしております。",  # ⛔「いえ、」は別の耳に「ええ、」と聞こえた（Sadachbia 3回とも）
    "absent_okada": "あいにく岡田は席を外しておりまして。",
    # 受付の方が不在を伝えてから、別の方に取り次ぐ（2026-10-22 の見直しで見つけた形：不在の読みは電話口の方ごとに数えていたので、代わった方に
    # 岡田様をお呼びし直していた）
    "away_to_other": "岡田は外出しておりますので、別の者に代わります。少々お待ちください。",
    # 受付の方が、名乗り・用件のほかのことを聞く（✅ 2026-10-22 ユーザー：「想定の質問ならそれでいいけど、録音や想定にない質問だった場合は？」）
    "relation_ask": "失礼ですが、岡田とはどういったご関係でしょうか？",
    "number_from": "この番号はどちらでお知りになりましたか？",
    "taken_over_yamada": "お電話代わりました。山田です。",
    "reception_refuse": "申し訳ございません、営業のお電話はお取り次ぎしておりません。",
    # 取り次いだあとに、受付の方がまた出て、出られないと伝える（⚠️ 2026-10-19 ユーザー：「取り次いだ後に、『岡田は取り込んでおり、対応できません』と
    # もう一度受付が言うこともあるので、取次後は必ずしも、本人とは限らない」）
    "busy_after_hold": "お待たせいたしました。申し訳ございません、岡田は取り込んでおりまして、ただいま電話に出られないとのことです。",
}
# 人の声でない相手の音（作った声ではないので、別の耳の確かめは無い）。ファクスの答える音（CED 2100Hz のあと、V.21 の信号＝1650・1850Hz を
# 300ボーで切り替える音。ITU-T T.30）。声の名前ごとに同じ音のファイルを置く（台詞の声の読み替え＝convo.callee_wav が使えるように）
TONES = {"fax_tone": "fax"}
# 声も言葉もそのままで、文と文の間だけを長くした台詞（言葉の間を長くあける相手）：名前 → (もとの台詞, 間の秒)。もとの台詞が確かめに
# 通っていれば、使う前に作る（verified）。⛔ 2026-10-19 G-57：受付の方の「岡田ですね。」「少々お待ちください。」（間0.6秒）の間で、
# 会話AIが話し終わりと決めてAIが報告し、判定は「岡田ですね。」だけを見た（18本中1本ほど）。間を長くすると、毎回その形になる
# （G-60：間1.6秒の「ええとですね。」「3人です。」は、5回とも間で話し終わりの知らせが来た）
PAUSED = {"hold_target_pause": ("hold_target", 1.5), "okada_whois_pause": ("okada_whois", 1.5)}
# 別の耳は名前を漢字で書くので、カタカナで書いた名前の読みは比べられない（「中島」＝なかじま と読んでしまう）。名前だけ、その漢字に
# 直してから比べる（読みそのものは G-44 の書き起こしで確かめた）。名前の一覧ではなく、この試験の材料の名前だけ
NAME_SPELLINGS = {"ナカシマ": "中島", "カクタ": "角田", "カドタ": "角田"}
INSTRUCTION = "あなたは音声の材料を作る係です。渡された言葉だけを、そのまま、自然な日本語で読み上げます。言葉を足しません。"
# お客役の話し方（Leda・Sadachbia）：お店や会社から電話を受けた、ふつうのお客さん。営業や受付のような話し方にしない
CUSTOMER_INSTRUCTION = ("あなたは、お店から電話を受けた、ふつうのお客さんです。渡された言葉だけを、そのまま、電話の相手に答えるように話します。"
                        "普段どおりの、自然でくだけた話し方で、ふつうの速さで話します。明るすぎたり、丁寧すぎたり、読み上げるような話し方にはしません。"
                        "言葉を足しません。")
INSTRUCTIONS = {"Leda": CUSTOMER_INSTRUCTION, "Sadachbia": CUSTOMER_INSTRUCTION}
OUT = HERE / "results" / "callee"


def text_and_how(v) -> tuple[str, str | None]:
    return (v, None) if isinstance(v, str) else v


def tone_pcm(kind: str, rate: int = 16000) -> bytes:
    """人の声でない相手の音（TONES）を作る。いまはファクスの答える音だけ：CED（2100Hz を3秒）・75ミリ秒の無音・V.21 の信号
    （1650Hz と 1850Hz を 300ボーで切り替える音を2秒。中身は HDLC の旗＝0111 1110 の並び）。電話の音質に落として返す。"""
    import numpy as np

    assert kind == "fax", kind
    r = 8000
    ced = np.sin(2 * np.pi * 2100 * np.arange(int(3.0 * r)) / r)
    gap = np.zeros(int(0.075 * r))
    bits = [int(b) for b in "01111110" * 75]  # 300ボーで2秒分
    per = r // 300
    freq = np.repeat([1650 if b else 1850 for b in bits], per)
    fsk = np.sin(2 * np.pi * np.cumsum(freq) / r)
    x = 0.5 * np.concatenate([ced, gap, fsk, np.zeros(int(0.5 * r))])
    return phone_quality((x * 32767).astype("<i2").tobytes(), r, rate)


def write_tones() -> list[str]:
    """TONES の音のファイルを、声の名前ごとに書く（同じ音）。キーを使わない。"""
    out = []
    for uid, kind in TONES.items():
        pcm = tone_pcm(kind)
        for voice in CALLEE_VOICES:
            path = OUT / f"{voice}_{uid}.wav"
            write_wav(path, pcm, 16000)
            out.append(path.name)
    return out


def verified(wav: str) -> bool:
    """その声が、確かめ（sample_ok）に通っているか。試験は、通っていない声を使わない。"""
    if any(wav.endswith(f"_{uid}.wav") for uid in TONES):
        return (OUT / wav).exists()  # 作った音（声ではない）：別の耳の確かめは無い。ファイルがあればよい
    for uid, (base, pause_s) in PAUSED.items():
        if wav.endswith(f"_{uid}.wav"):
            source = wav[: -len(f"{uid}.wav")] + f"{base}.wav"
            if not verified(source):
                return False
            from stitch_gap import widen_pause
            widen_pause(source, pause_s, wav)  # もとの声から作り直す（間の秒を変えたら、すぐ効く）
            return True
    index = OUT / "index.json"
    made = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
    return any(m.get("wav") == wav and m.get("ok") is True for m in made)


# 日本語の言葉の種類として範囲が決まっている一覧（業務や場面ごとの一覧ではない）
ANSWER_WORDS = {"はい", "ええ", "うん", "いいえ", "いえ", "いや"}  # 応答の言葉：答えの向きを決めるので、必ず比べる
QUESTION_WORDS = {"何", "なに", "なん", "誰", "だれ", "どなた", "どちら", "どっち", "どこ", "どれ", "どの", "どう", "いつ", "いくら",
                  "なぜ", "どうして", "いかが"}  # 問いかけの言葉


def _without_fillers(text: str) -> str:
    """応答の言葉以外の感動詞（「あ」「えっと」「あの」など）を除く。意味を変えないので、別の耳が落としても数えない。
    ⛔ 2026-10-13：「あ、はい、どちら様？」の頭の「あ」を別の耳が落として、確かめに落ちていた。"""
    from jtext import words

    return "".join(w.surface for w in words(text) if not (w.feature.pos1 == "感動詞" and w.surface not in ANSWER_WORDS))


def question_by_words(text: str) -> bool:
    """言葉だけで問いかけと分かるか（問いかけの言葉を含む・文が終助詞「か」で終わる）。「はい？」は分からない。"""
    from jtext import words

    ws = [w for w in words(text) if w.feature.pos1 != "補助記号"]
    return any(w.surface in QUESTION_WORDS for w in ws) or (bool(ws) and ws[-1].surface == "か" and ws[-1].feature.pos2 == "終助詞")


def sample_ok(text: str, heard: str) -> dict:
    """作った声の確かめ：(1) 読みが合う（応答の言葉以外の感動詞は数えない） (2) 言葉だけでは問いかけと分からない文（「はい？」）は、
    別の耳の文字も「？」で終わる（語尾の上がりが聞こえた印）。"""
    for kana, kanji in NAME_SPELLINGS.items():
        text = text.replace(kana, kanji)
    diffs = differences(_without_fillers(text), _without_fillers(heard))
    q_need = text.rstrip().endswith(("？", "?")) and not question_by_words(text)
    q_ok = (not q_need) or heard.rstrip().endswith(("？", "?"))
    return {"heard": heard, "diffs": diffs, "question_needed": q_need, "question_ok": q_ok, "ok": not diffs and q_ok}


async def run(args) -> dict:
    if args.recheck:  # 確かめの決まりを直したとき：保存した別の耳の文字に、いまの決まりを当て直す（キーを使わない）
        index = OUT / "index.json"
        made = json.loads(index.read_text(encoding="utf-8"))
        for m in made:
            if "heard" in m:
                m.update(sample_ok(m["text"], m["heard"]))
                say(f"{m['voice']} {m['id']}：聞こえた「{m['heard']}」 合格={m['ok']}")
        index.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"rechecked": made}
    if args.dry_run:
        for v in CALLEE_VOICES:
            build_config(voice=v, system_instruction=INSTRUCTION)
        return {"dry_run": True}
    say("作った音：", write_tones())
    client = make_client(pick_key(args))
    index = OUT / "index.json"
    made = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
    if args.verify:  # 作り直さずに、作ってある声を確かめだけする（確かめの決まりを足したとき）
        for m in made:
            if "error" in m or "ok" in m or not (OUT / m["wav"]).exists():
                continue
            pcm, rate = read_wav_pcm16_mono(OUT / m["wav"])
            m.update(sample_ok(m["text"], await hear(client, pcm, rate, "transcribe35")))
            say(f"{m['voice']} {m['id']}：聞こえた「{m['heard']}」 合格={m['ok']}")
        index.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"model": args.model, "verified": made}
    have = {(m.get("voice"), m.get("id")) for m in made if "error" not in m}
    for voice in args.voice or CALLEE_VOICES:
        for uid, value in UTTERANCES.items():
            text, how = text_and_how(value)
            if uid not in (args.redo or []) and (voice, uid) in have and (OUT / f"{voice}_{uid}.wav").exists():
                continue  # 作ってある声は作り直さない（ほかの確かめの材料が変わらないように）。作り直すのは --redo で名指ししたものだけ
            made = [m for m in made if (m.get("voice"), m.get("id")) != (voice, uid)]
            tries = []
            for n in range(args.tries):
                try:
                    inst = INSTRUCTIONS.get(voice, INSTRUCTION)
                    async with client.aio.live.connect(model=args.model, config=build_config(voice=voice, system_instruction=inst)) as s:
                        t = time.monotonic()
                        await send_text(s, f"読み上げる言葉：{text}" + (f"\n読み方：{how}" if how else ""))
                        turn = await collect_turn(s, started_at=t, timeout_s=60)
                    pcm = phone_quality(bytes(turn.audio), turn.audio_rate or 24000, 16000)
                    check = sample_ok(text, await hear(client, pcm, 16000, "transcribe35"))
                    tries.append(check)
                    say(f"{voice} {uid} {n + 1}回目：聞こえた「{check['heard']}」 合格={check['ok']}")
                    if check["ok"] or n == args.tries - 1:
                        path = OUT / f"{voice}_{uid}.wav"
                        write_wav(path, pcm, 16000)
                        made.append({"voice": voice, "id": uid, "text": text, "how": how, "said": turn.summary()["output_transcript"],
                                     **check, "tries": len(tries), "seconds": round(duration_s(pcm, 16000), 2), "wav": path.name})
                        break
                except Exception as exc:
                    tries.append({"error": describe_error(exc)})
                    if n == args.tries - 1:
                        made.append({"voice": voice, "id": uid, "text": text, "error": describe_error(exc)})
    index.write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"model": args.model, "samples": made}


def main() -> None:
    require_python()
    p = base_parser(__doc__)
    p.add_argument("--tries", type=int, default=3, help="別の耳の確かめに通らないとき、作り直す回数の上限")
    p.add_argument("--redo", action="append", choices=sorted(UTTERANCES), help="作ってある声でも作り直す（名指ししたものだけ）")
    p.add_argument("--verify", action="store_true", help="作り直さずに、まだ確かめていない声を確かめだけする")
    p.add_argument("--recheck", action="store_true", help="保存した別の耳の文字に、いまの確かめの決まりを当て直す（キーを使わない）")
    p.add_argument("--voice", action="append", choices=CALLEE_VOICES, help="作る声（無ければ全部）")
    args = p.parse_args()
    save_result("CALLEE-SAMPLES", asyncio.run(run(args)))


if __name__ == "__main__":
    main()
