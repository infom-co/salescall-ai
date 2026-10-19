"""AIと同じ声で、プログラムが流す録音を作る（DESIGN.md 6-13・6-15）。
- 契約者の単位（声の型ごとに1回。どの依頼主にも同じもの）：相づち・つなぎの言葉・締め・待ってもらう一言（phrases.RECORDED）⇒ results/ack/
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
from phrases import CALLBACK_ASK, RECORDED
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
       "ai_again": "電話で、不安にさせてしまったお客様に、心から申し訳なく思いながら、誠実に、落ち着いた調子で、言葉の途中で区切らずに"}
DEFAULT_HOW = "電話で、相手の話に相づちを打つように、短く"
MAX_S = {"hai": 1.0, "shouchi": 1.4, "arigatou": 1.6, "shitsurei": 1.6, "moushiwake": 1.6, "moushiwake_heavy": 1.8, "lead_soredeha": 1.0,
         "lead_tsuzukimashite": 1.2, "lead_deha": 0.8, "closing": 8.0, "closing_declined": 7.0, "wait": 3.5, "wait_heavy": 3.8, "question": 5.0,
         "greeting": 2.5, "intro": 4.5, "purpose": 4.5, "ai": 3.0, "voicemail": 12.0, "ai_again": 7.0}
# 録音の中の名前ごとの読み方の鍵（無ければ中の名前のまま）。かけ直しで足す質問は、質問の読み方（⛔ 2026-10-19 の見直しで見つけた漏れ：
# 足す質問を契約者の単位の録音に入れたとき、読み方も長さの上限も無く、作ると止まった＝ClipTablesTest で、表の漏れを見る）
HOW_OF = {f"callback_{k}": "question" for k in CALLBACK_ASK}
# 「いかがなさいましたか？」「どうなさいましたか？」（phrases.SILENCE_PROMPT）：考えている相手にも、「AIですか」と3回目から聞かれたときにも
# 流す。どちらも、何か気になる点があったかを心配して聞く一言なので、「AIですか」への言い方と同じ読み方
HOW_OF.update({"ikaga": "ai", "dou": "ai"})


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


def script_table(business) -> tuple[dict, dict]:
    """依頼主の単位の録音の表 {中の名前: (言葉, 読み方と長さの上限の鍵)} と、{中の名前: その録音で読んだ名前の読み}。
    ✅ 2026-10-16 ユーザー：「こちら側の担当者名は、依頼者ごとの録音時に録音しておくといいでしょう」⇒ 名乗り（自社名・担当者名）と
    用件の一文も、依頼主の単位で録音する（前は質問の文だけ）。名前の読みは、人が聞いて選ぶときに確かめる（別の耳では確かめられない）。
    読みが決まらない名前が入る文は、読みの候補ごとに録音を作る（instruction.reading_variants）。選ばれた録音の読みが、その名前の読みになる。"""
    from flowctl import spoken_question
    from instruction import reading_variants
    from phrases import LEAD_RECORDED

    items = [("intro", business.opening_parts()[1], "intro")]
    if business.purpose_line:
        items.append(("purpose", business.purpose_line, "purpose"))
    # 相手に聞かれたことへの決まった言い方（「AIですか？」）と留守番電話の伝言（設定があれば）も、依頼主の単位（DESIGN.md 6-13 の3）。
    # 目的の人を呼ぶ一言・確かめる一言は、電話をかける相手ごとに名前が変わるので、ここではなく名前の録音の台帳（DESIGN.md 4-3）
    parts = business.replay_parts()
    items += [(k, parts[k], k) for k in ("ai", "voicemail") if parts.get(k)]
    for q in business.questions:  # つなぎの言葉と質問を1本で録音する（順番で決まるつなぎの言葉。flowctl.spoken_question）
        if q.reading_of:
            continue  # 名前の読みの確かめは、まず言ってみる読みが相手ごとに変わるので、AIが言う（録音にしない）
        text, lead = spoken_question(business.questions, q.id)
        items.append((f"{q.id}_{LEAD_RECORDED[lead]}" if lead else q.id, text, "question"))
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
    for g in sorted(HERE.glob("results/G-38_*.json")):
        cands.update({r["wav"][:-4]: (HERE / "results" / "voice_match", r) for r in json.loads(g.read_text(encoding="utf-8"))["rows"]
                      if r.get("wav") and r.get("ok") and r["id"] in table})
    done = []
    for name in names:
        where, row = cands[name]
        row = {k: v for k, v in row.items() if k not in ("features", "st_from_ref", "cond", "take")}
        assert row["text"] == table[row["id"]][0], (name, row["text"])  # 文が変わったあとの古い候補は使わない
        if as_id and as_id != row["id"]:
            assert table[as_id][0] == row["text"], (name, as_id)  # 言葉が違う録音は使わない
            row.update({"made_as_id": row["id"], "id": as_id})
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
    have = {(m["type"], m["id"]) for m in made if m.get("ok") and m.get("text") == table.get(m["id"], (None,))[0]}
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
            await send_text(s, f"読み上げる言葉：{context}{text}")
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
