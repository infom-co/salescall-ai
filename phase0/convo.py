"""台本どおりの通話を1本動かす唯一の場所（G-6・G-16・G-13・G-5 で使う）。

- 指示文は instruction.build、操作の宣言は flowctl.tool_declarations、操作への返事は flowctl.Flow が作る（どれも1か所）。
- 相手の発話は文字で送る（流れの決まりを、毎回同じ入力で確かめるため）。
- AIの発話は、ターンごとに (1) AIの1ターンの見分け方（turn_check）と (2) 別の耳（listen）で確かめる。
  ⛔ AI自身の文字起こしは、口から出た声の確かめに使わない（G-7・G-17：届かない・言い間違いが写らない）。
- swap_before：その番号の相手の発話の前で、セッションを閉じて開き直す（張り替え。DESIGN.md 6-6・G-5）。
  開き直すときは、instruction.build に流れの制御のいまの位置を渡す。
- 相手の発話は、文字（str）か、録った声（{"wav": ファイル名}。results/callee/ の電話の音質の声）。声のときは、
  実際の通話と同じく少しずつ送り、話し終わりのあとは無音を送りながら返事を待つ（live_util.callee_turn）。
- opening_mode="recorded"：名乗りと最初の質問は録音で流した形（DESIGN.md 6-13 の案）。AIは相手の最初の発話から続ける。
  barge_in＝(届いた文, まだの文)：録音の途中で相手が話し始めて止めた形。最初の相手の発話の前に、どこまで届いたかを
  AIへ知らせる（instruction.barge_in_note・live_util.send_note）。
- gap_s：相手の発話を送る前に空ける秒数（ふだんは0。別の耳を会話の途中で動かすと、その間だけ間が空く。G-26）。
- 見張り（turn_check）が引っかかったターンは、AIの声を results/convo_audio/ に残す（人が聞いて確かめるため。GitHub には上げない）。
  save_audio="all" なら、全部のターンを残す（ユーザーが聞いて決める試験。G-22）。
- stall_at：AIが止まったときの張り替え（DESIGN.md 6-7 の案）を試す。その番号の相手の発話を古いセッションへ送り、
  返事を待たずに閉じ、新しいセッションへ同じ発話を送り直す（相手は1回しか話していないので、相手の発話は1回と数える）。
- 相手の発話の代わりに {"silence_s": 秒} を置くと、相手が黙っている間（考えている途中など）を作る：無音だけをその秒数送り、
  その間にAIが話し出すかを記録する（話さなければ、記録の行は timed_out＝黙って待った）。相手の発話としては数えない。G-34
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext

from audio import is_fax_tone
from common import HERE, read_wav_pcm16_mono, write_wav
from offscript import SpeechGate
from flowctl import ASKS_CHECKS, CHOICE_TYPES, RETURN_CHECKS, Flow, tool_declarations
from phrases import LINE_TROUBLE
from instruction import Business, barge_in_note, build
from ears import open_ear
from listen import hear
from live_util import (audio_sent_s, build_config, callee_turn, collect_turn, inbox_of, open_session, send_note, send_text,
                       silence_while)
from turn_check import BAD_KINDS, check_ai_turn

AUDIO_DIR = HERE / "results" / "convo_audio"


# 相手の声を送り終えた直後より前に、AIの声が届いていた＝相手の言葉の途中（か、声のあとの短い無音の間）で話し出していた
# （届いた時刻は送り終えた時刻から数える＝送っている間に届いた声は負の秒。live_util.Inbox）
CUT_IN_S = 0.05
# お客役の声（✅ 2026-10-14 ユーザー：Leda と Sadachbia に入れ替わりでお客役をさせる）：会話のAIの声と違う声にする
CALLEE_FOR = {"Sadachbia": "Leda", "Leda": "Sadachbia"}


def callee_voice_for(voice_type: str) -> str:
    """会話のAIの声の型に対する、お客役の声（同じ声どうしで話させない）。"""
    from instruction import voice_style
    return CALLEE_FOR[voice_style(voice_type)[0]]


def callee_wav(name: str, callee_voice: str | None) -> str:
    """台詞の声のファイル名。台詞は「Puck_（台詞の名前）.wav」で書き、お客役の声を指定したら、その声のファイルに読み替える。"""
    if callee_voice and name.startswith("Puck_"):
        return f"{callee_voice}_{name[len('Puck_'):]}"
    return name


# 確かめ（別の耳の文字・意味の判定）を、遅れても受け取る長さ（相手の話し終わりから）。これより遅いものは届かなかったとして、通話後の検証へ回す
# （Flow.unchecked）。待つのではない（返事は flow.limits.hold_s までしか待たない）
LATE_CHECK_S = 30.0
def recording_seconds(voice_type: str, part: str) -> float:
    """契約者の単位の録音（make_ack_clips の results/ack）の、声の長さ（秒）。判定を待つ間に流す録音（Flow.hold_play）の分だけ、待つ長さを
    延ばすのに使う（本番は、電話接続側が流す録音の長さをそのまま使う）。録音が無ければ止める（長さの見込みで待たない）。使う録音かは
    make_ack_clips.usable の1か所（人が聞いて選んだ録音も使う）。"""
    from make_ack_clips import usable, words
    index = json.loads((HERE / "results" / "ack" / "index.json").read_text(encoding="utf-8"))
    table = words(False)[0]
    got = next((m for m in index if m.get("type") == voice_type and m.get("id") == part and usable(m, table)), None)
    if got is None:
        raise RuntimeError(f"録音「{part}」（声の型 {voice_type}）がありません：python make_ack_clips.py --id {part}")
    return float(got["seconds"])


def answers(judge, q) -> bool:
    """判定の受け持ち judge が、質問 q に自分で答えるか（meaning_judge.JudgePanel.answers の1か所。お呼びする段の 3.5 Flash Lite は、もう1つの
    受け持ちが確かな読みを返さないときに裏で出す止める側だけの判定＝STOP_ONLY）。受け持ちの仕組みを持たない判定（試験の判定）は、どの質問にも答える。"""
    return getattr(judge, "answers", None) is None or judge.answers(q)


def check_handler(flow: Flow, *, ear=None, judge=None, judge2=None, pcm: bytes | None = None, rate: int = 16000, wav: str | None = None,
                  tasks: list | None = None, audio_at: float | None = None, clip_s=None, grounder=None):
    """相手の1つの発話の確かめ（DESIGN.md 1章・6-10）：相手の声を、会話AIへ送るのと同時に別の耳（通話ごとに1本つないだ ears.Ear）へも流し、
    相手の言葉が出揃ったら（Flow.words_complete＝会話AIの話し終わりの知らせ）、会話AIの聞き取りの文字（無ければ別の耳の文字）を意味の判定
    （judge・judge2＝async (質問, 言葉, こちらが言い直した言葉) -> 中の名前。会社の違う2つを同時に出す。meaning_judge.JudgePanel の受け持ち）へ渡す。
    届いたものは、いつ届いても、この発話の番号と見た言葉を付けて flow.on_check へ渡す（受け取ったあとに届けば、流れの制御が同じ決まりで
    当てる）。文字で送る相手の言葉（pcm 無し）は、はじめから出揃っている。
    答えをそのまま受け取りうる報告（flow.wants_checks）が来たら、返事を作る前に、この報告と同じ相手の言葉の確かめを待つ（hold）：別の耳と、
    判定はどちらか先に届いた1つ（遅れて届いたほうは、流れの制御が同じ決まりで当てる）。
    返り値＝(報告の返事を作る処理, 声を流す作業, 記録, 判定を待つ処理)。判定を待つ処理（settle）：AIが報告しなかったターンのあと、報告を
    促すかを決める前に呼ぶ。tasks：始めた確かめの作業を足す先（通話の終わりに、まだ走っているものを待つため）。
    audio_at：この発話の声を送り始める、会話AIへ送った音の上の位置（live_util.audio_sent_s）。声の終わりの位置を流れの制御へ渡す（Flow.on_voice。
    本番は電話接続側の声の見張りが、送った音の上で決める）。
    clip_s：判定を待つ間に流す録音（Flow.hold_play）の長さを返す（録音の部分の名前 -> 秒）。流したら、その長さだけ待つ長さを延ばす。
    grounder：AIが自分の言葉で言う文の中身の判定（grounding.make_grounder。async (知っていること, お客様の言葉, 返す文) -> 読み）。聞くべき操作
    （Flow.grounding_job）が来たら、返事を作る前に flow.limits.hold_s 秒まで待ち、届いた読みを flow.on_grounded へ渡す（待つ間は「はい、」の相づち。
    届かなければ渡さない＝流れの制御は、その文を声に出さずに受け皿へ回す。遅れて届いた読みは記録だけ＝通話後の検証が見る）。"""
    loop = asyncio.get_running_loop()
    if pcm is not None:
        from stitch_gap import callee_speech

        tail = callee_speech(wav)[2]
        speech_end = loop.time() + len(pcm) / 2 / rate - tail  # 相手の声の終わり（声のあとの無音を引く）
        task = asyncio.create_task(ear.feed(pcm, rate, tail)) if ear is not None else None
    else:
        speech_end, task, ear = loop.time(), None, None
    tasks = [] if tasks is None else tasks
    info: dict = {"_end": speech_end}
    if pcm is not None and audio_at is not None:
        info["voice_end_at"] = round(audio_at + len(pcm) / 2 / rate - tail, 3)  # 声の終わりの位置（送った音の上の秒）
        flow.on_voice(info["voice_end_at"])
    turn = flow.callee_turns
    q, asked, before = flow.judge_context()  # 判定の質問（目的の人を呼んでいる段では、呼ぶ一言と、その前に相手が言ったこと）
    runs = {k: r for k, r in (("judge", judge), ("judge2", judge2)) if r is not None and q and answers(r, q)}
    if before and runs:
        info["judge_before"] = before
    # 呼んでいる段だけ：こちらに尋ねているかを、目的の人の判定とは別の問い合わせとして同時に出す（受け持ちは1つ目の判定＝止まれば代わりが入る。
    # Flow.asks_context。✅ 2026-10-22 ユーザー：「相手が喋っていても、それが質問だった場合の対処は？」）
    questions = {k: (q, asked, before) for k in runs}
    # ⛔ 2026-10-25 J10b：受け持ちが1つ目の判定だけだと、その会社が混むと質問に気づけない ⇒ 2つ目の判定にも出す（flowctl.ASKS_CHECKS）
    if (ac := flow.asks_context()) is not None:
        for kind, j in (("asks", judge), ("asks2", judge2)):
            if j is not None and answers(j, ac[0]):
                runs[kind], questions[kind] = j, (ac[0], None, ac[1])
    # 取り次ぎで待ったあとに話した方の最初の発話だけ：待たせた方が戻ったか（Flow.returned_context。2つがそろったときだけ名乗りを省く＝待たない）
    if (rc := flow.returned_context()) is not None:
        for kind, j in (("returned", judge), ("returned2", judge2)):
            if j is not None and answers(j, rc[0]):
                runs[kind], questions[kind] = j, (rc[0], None, rc[1])

    def started(coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        tasks.append(t)
        return t

    judging_text: dict[str, str | None] = {}  # 判定ごとに、いま判定している言葉（None＝出揃うのを待っている＝出揃った言葉で判定する）
    ahead: dict[str, tuple] = {}  # 判定ごとに、組より先に届いた、裏で出した代わりの読み（言葉, 見た言葉の印, 読み）＝hold が止める側だけに使う

    def complete() -> bool:
        return flow.words_complete

    async def hearing():
        await asyncio.sleep(max(0.0, speech_end - loop.time()))
        text = await ear.wait(LATE_CHECK_S)
        info.update(text=text, ear_s=round(loop.time() - speech_end, 3) if text is not None else None)
        flow.on_check("ear", text, turn)
        return text
    etask = started(hearing()) if ear is not None else None

    async def words(prev: str | None = None) -> tuple[str, str | None] | None:
        """判定に渡す言葉＝出揃った相手の言葉（complete）のうち、前に判定した言葉（prev）と違うもの。(言葉, 見た言葉の印)：会話AIの聞き取りの
        文字なら、その文字（流れの制御が、いまの言葉の判定かを見分ける＝Flow.on_check の said）。無ければ別の耳の文字（発話まるごと＝None）。
        この発話が終わって次の発話に移ったら、やめる（次の発話の文字で判定しない）。
        ⛔ 2026-10-18：話し終わりの時点の文字で始めていたが、会話AIの聞き取りの文字は、そのあとに届く ⇒ 別の耳の無い通話で判定が走らなかった
        ⛔ 2026-10-19 G-57：声の終わりから決めた秒数だけ続きを待つ形 ⇒ 受付の方の「岡田ですね。」だけで判定した（Flow.words_complete）"""
        stop = loop.time() + LATE_CHECK_S
        while loop.time() < stop and flow.callee_turns == turn:
            if complete():
                if flow.callee_said and flow.callee_said != prev:
                    return flow.callee_said, flow.callee_said
                if not flow.callee_said and etask is not None and etask.done() and etask.result() and etask.result() != prev:
                    return etask.result(), None
            await asyncio.sleep(0.02)
        return None

    async def judging(kind: str, text: str | None = None, said: str | None = None):
        """その判定（kind）を、出揃った相手の言葉で1回、流れの制御へ渡す。判定している間に相手がまた話し、言葉が増えたら（判定した言葉は
        もう古い）、渡さずに、出揃った新しい言葉で判定し直す。text・said：この言葉で始める（hold が判定し直すとき）。"""
        judged = None
        while True:
            if text is None:
                judging_text[kind] = None
                got_words = await words(judged)
                if got_words is None:
                    return None
                text, said = got_words
            t0 = loop.time()
            judging_text[kind] = text
            try:
                kq, kasked, kbefore = questions[kind]
                # 受け持ちの仕組み（JudgePanel）の判定には、裏で出した代わりの読みを知らせてもらう（early。meaning_judge.JudgePanel.ask）
                kw = ({"early": lambda r, k=kind, t=text, s=said: ahead.__setitem__(k, (t, s, r))}
                      if getattr(runs[kind], "panel", None) is not None else {})
                got = await asyncio.wait_for(runs[kind](kq, text, kasked, before=kbefore, **kw), LATE_CHECK_S)
            except Exception as exc:  # 判定が落ちても通話は止めない（届かなかった＝通話後の検証へ）
                info[f"{kind}_error"] = repr(exc)[:200]
                if getattr(exc, "tried", None):  # 受け持ちの判定・代わりの、答えが無かった理由（meaning_judge.NoJudgment）
                    info[f"{kind}_tried"] = list(exc.tried)
                    if any("unsure" in t for t in exc.tried):
                        # 確からしさが線より下の読みだけが届いた：どう数えるかは流れの制御が決める
                        flow.on_unsure(kind, next(t["unsure"] for t in exc.tried if "unsure" in t), turn, said)
                    for t in exc.tried:
                        if "stop_only" in t:
                            # 止める側だけの判定（お呼びする段の 3.5 Flash Lite＝meaning_judge.STOP_ONLY）の読み：答えにはせず、止める側だけに使う
                            # （✅ 2026-10-30 夜 ユーザー：「(1)(2)ともお勧めの形でOKです」）
                            flow.on_unsure(kind, t["stop_only"], turn, said, answer=False)
                return None
            info.setdefault(f"{kind}_calls_s", []).append(round(loop.time() - t0, 3))  # 判定そのものにかかった秒（待つ長さを決めるため）
            # 判定の間に相手がまた話し始めたら、話し終わるのを待ってから、言葉が増えたかを見る（増えていれば、この判定は渡さない）
            while said is not None and not complete() and flow.callee_turns == turn and loop.time() < t0 + LATE_CHECK_S:
                await asyncio.sleep(0.02)
            if said is not None and said != flow.callee_said and flow.callee_turns == turn:
                info.setdefault(f"{kind}_stale", []).append(text)
                judged, text, said = text, None, None
                continue
            info.update({kind: got, f"{kind}_firm": getattr(got, "firm", None), f"{kind}_text": text,
                         f"{kind}_s": round(loop.time() - speech_end, 3), f"{kind}_took_s": round(loop.time() - t0, 3)})
            if getattr(got, "by", None):  # どの判定が答えたか（止まった判定の代わりが入ったか。meaning_judge.JudgePanel）
                info[f"{kind}_by"] = got.by
                if got.tried:
                    info[f"{kind}_tried"] = list(got.tried)
            flow.on_check(kind, got, turn, said=said)
            if getattr(got, "weak", None) is not None:
                # 代わり（お呼びする段の 3.5 Flash Lite など）が答えた：受け持ちの組の確かでない読みも、答えが無いときと同じに数える（繋ぐのを
                # 止める側だけ＝Flow.on_unsure。⛔ 2026-10-21 G-57 tc10：弱い「代わった人」を捨て、読みが2つそろったとして別の方に繋いだ）
                info[f"{kind}_weak"] = got.weak
                flow.on_unsure(kind, got.weak, turn, said)
            return got
    jobs = {kind: started(judging(kind)) for kind in runs}

    def judged() -> bool:
        # いまの言葉の判定が、1つでも届いた（Flow.check はいまの言葉のものだけ）。尋ねているかの問い合わせ（asks）を出していれば、それも届いた
        asks = [k for k in ASKS_CHECKS if k in runs]
        return (any(k in flow.check for k in runs if k not in ASKS_CHECKS + RETURN_CHECKS)
                and (not asks or any(k in flow.check for k in asks)))

    async def judgments(every: bool) -> None:
        """いまの言葉の判定を待つ：1つ届くまで（every＝全部が終わるまで）。答えずに終わった判定は待たない（もう1つを待つ）。"""
        while every or not judged():
            live = [t for t in jobs.values() if not t.done()]
            if not live:
                return
            await asyncio.wait(live, return_when=asyncio.FIRST_COMPLETED)

    async def hold(ear_too: bool, ack: bool = True, every: bool = False, play_for=None) -> None:
        """報告を受け取る前（・報告を促すかを決める前）に、いまの相手の言葉の確かめがそろうのを待つ：別の耳（ear_too）と、判定はどちらか先に
        届いた1つ（⛔ 2026-10-19 G-57 の9回目：その場の判定だけを待ち、2秒超が328回中58回・報告を待たせた185回中21回で間に合わなかった。
        G-61：同じ会社の2本は遅れが一緒に出るが、違う会社の2つで早く届いたほうは、遅いほうから1割 1.53秒）。
        every：取り消せない報告（Flow.binding＝目的の人に繋ぐ）は、待つ長さの中で判定を全部待つ（届いた判定どうしが食い違えば確かめる）。
        相手が話している間は待ち続ける（会話AIの話し終わりの知らせまで。相手が話しているので、こちらが黙っていても間にはならない。
        ⛔ AIが相手の言葉の間で報告した＝G-57 の「岡田ですね。」「少々お待ちください。」）。話し終わってからは flow.limits.hold_s 秒まで。
        報告を待たせる間は「はい、」の相づち（ack。held_ack）。
        判定した言葉より相手の言葉が増えていたら（判定のあとに相手が話し続けた）、いまの言葉で判定し直す。"""
        t0 = quiet = loop.time()
        # 出揃うまで待つ：相手が話している間は待ち続け、話していないのに出揃わない（会話AIが声の終わりまで聞き取っていない＝遅れ）のは、
        # 相手が話し終えてから hold_s まで（その間に判定もそろえる）
        while not complete() and flow.callee_turns == turn and loop.time() < t0 + LATE_CHECK_S:
            if flow.callee_speaking:
                quiet = loop.time()
            elif loop.time() >= quiet + flow.limits.hold_s:
                break
            await asyncio.sleep(0.02)
        ready = loop.time()
        if pcm is not None and not flow.callee_ended and not flow.callee_speaking and not complete():
            info["no_activity"] = True  # 話し始め・話し終わりの知らせが1つも来ない（来ないつなぎ）：届いた言葉で確かめる
            flow.on_voice(None)
        elif not complete():
            info["incomplete"] = True   # 会話AIが、まだ声の終わりまで聞き取っていない：途中の言葉では判定しない（流れの制御も使わない）
        for kind in runs:
            # いまの言葉の判定が無く、走っている判定も無いか古い言葉のもの ⇒ いまの言葉で判定し直す
            stale = jobs[kind].done() or judging_text.get(kind) not in (None, flow.callee_said)
            if complete() and flow.callee_said and kind not in flow.check and stale:
                jobs[kind] = started(judging(kind, flow.callee_said, flow.callee_said))
                info.setdefault("rejudged", []).append(kind)
        waits = [etask] if ear_too and etask is not None and not etask.done() else []
        first = asyncio.create_task(judgments(every)) if runs and (every or not judged()) else None
        if first is not None:
            waits.append(first)
        extra = 0.0
        if ack and (waits or ready - t0 > 0.05):
            # 判定を待つ間に流す録音（取り消せない報告で、新しい方へのあいさつ＝Flow.hold_play）があれば「はい、」の代わりに流し、その長さだけ
            # 待つ長さを延ばす（✅ 2026-10-22 ユーザー：「『お忙しいところ、失礼いたします。』を入れておいて、その間に判定を待てばいい」）
            running = any(not t.done() for t in jobs.values())  # まだ走っている判定がある（そろっていれば、待つ間の録音は要らない）
            held = play_for() if (play_for is not None and running and not info.get("incomplete")) else None
            if held is not None:
                info["held_play"], extra = held["part"], clip_s(held["part"]) if clip_s is not None else 0.0
            else:
                info["held_ack"] = "hai"
        if waits and not info.get("incomplete"):  # 出揃っていなければ、判定を待っても使わない（流れの制御は決めずに待つ）
            await asyncio.wait(waits, timeout=max(0.0, max(ready, quiet) + flow.limits.hold_s + extra - loop.time()))
        if first is not None and not first.done():
            first.cancel()
        # 組の答えが待つ長さに間に合わなかった判定：組より先に、裏で出した代わり（お呼びする段の 3.5 Flash Lite）の読みが届いていれば、止める側
        # だけに使う（弱い読みと同じ＝Flow.on_unsure：黙って待つ・確かめる側。繋ぐ・かけ直しへは進ませない）。✅ 2026-10-30 ユーザー：「(1)(2)とも
        # お勧めの形でOK」。⛔ 前は、Anthropic が混んで遅いだけのとき、d1-ultra と Jev が確かでない言葉に読みが1つも無かった（Intent.md 9-4 の J10b）
        # 答える側（尋ねている）には数えない（answer=False＝flowctl.unsure_counts の1か所。⛔ 2026-10-30 夜：弱い読みと同じに数えていたので、
        # 確かめへの「いいえ、私は山田と申します。」の Flash Lite の「尋ねている」で、聞かれていない質問に答える側へ進みえた）
        for kind, (atext, asaid, got) in list(ahead.items()):
            if not jobs[kind].done() and judging_text.get(kind) == atext and flow.callee_turns == turn:
                del ahead[kind]
                flow.on_unsure(kind, str(got), turn, asaid, answer=False)
                info.setdefault("ahead", []).append({"kind": kind, "reading": str(got), "by": getattr(got, "by", None)})
        info.setdefault("waits", []).append({"for": "report" if ack else "settle", "every": every, "speaking_s": round(ready - t0, 3),
                                             "waited_s": round(loop.time() - t0, 3),
                                             "judged": [k for k in runs if k in flow.check],  # 決めるときに届いていた判定
                                             "late": [k for k, t in ((("ear", etask),) if ear_too else ()) + tuple(jobs.items())
                                                      if t is not None and not t.done()]})
        if ack:
            info.setdefault("waited_s", info["waits"][-1]["waited_s"])
            info.setdefault("late", info["waits"][-1]["late"])

    async def ground(draft: str, task, t0: float) -> None:
        """答えの文の中身の判定（t0 に出した task）を、出してから待つ長さまで待つ（Flow.grounding_job・on_grounded）。⛔ 2026-10-22 の5回目：
        言葉で見る確かめは、世間話・お礼まで止め、作り話を通していた（G-63）⇒ 中身で読む。待つ間は「はい、」（報告を待たせるときと同じ相づち）。"""
        info.setdefault("held_ack", "hai")
        done, _ = await asyncio.wait([task], timeout=max(0.0, t0 + flow.limits.hold_s - loop.time()))
        row = {"draft": draft, "waited_s": round(loop.time() - t0, 3)}
        if task in done and task.exception() is None and task.result() is not None:
            v = task.result()
            flow.on_grounded(draft, v)
            row |= {"verdict": str(v), "part": v.part, "by": v.by, "seconds": v.seconds}
        elif task in done:
            row["error"] = repr(task.exception()) if task.exception() is not None else "読めなかった"  # どの判定も答えなかった
        else:
            row["late"] = True
            if not task.done():
                def late(t, row=row):
                    if not t.cancelled() and t.exception() is None and t.result() is not None:
                        row |= {"late_verdict": str(t.result()), "late_by": t.result().by}
                task.add_done_callback(late)
                tasks.append(task)
        info.setdefault("grounding", []).append(row)

    async def handle(fc):
        midway = not complete()  # AIが、相手の言葉が出揃う前に報告した（Flow.handle の midway）
        # 答えの文の中身の判定は、相手の言葉の確かめ（hold）と同時に出す（順に待つと、待たせる長さが足し算になる）
        job = flow.grounding_job(fc.name, fc.args) if grounder is not None else None
        grounding = (asyncio.create_task(grounder(*job)), loop.time()) if job is not None else None
        if flow.wants_checks(fc.name, fc.args):
            binding = flow.binding(fc.name, fc.args)
            await hold(ear_too=True, every=flow.waits_every(fc.name, fc.args),
                       play_for=(lambda: flow.hold_play(fc.name, fc.args)) if binding else None)
        if grounding is not None:
            await ground(job[2], *grounding)
        return flow.handle_call(fc, midway)

    async def settle() -> None:
        await hold(ear_too=False, ack=False)
    return handle, task, info, settle


# AIが黙ったとき（相手が話し終えてから、声も操作も出ない）に、つなぎを張り替えるまでの秒（DESIGN.md 6-7：2秒で「少々お待ちいただいても
# よろしいでしょうか。」の録音・さらに10秒で張り替え＝案の値。試験では録音は流さず、張り替えだけをする）。⛔ 2026-10-17：記録の通話 2,069本中7本で、
# AIが「はい、先週しました。」などのあとに90秒黙り（試験の待ちの上限）、報告が無いまま終わっていた（試験の側に 6-7 の張り替えが無かった）
SILENT_SWAP_S = 12.0
MAX_AGAIN = 5  # 試験の歯止め（聞き返しの回数は、流れの制御が決める。これはその決まりが壊れたときに、通話を終わらせるためだけ）


def asked_back(entries: list[dict]) -> bool:
    """そのターンで受け付けた最後の報告への返事が、聞き返しか（流れの制御の記録から読む）。"""
    return last_reply(entries) == "ask_back"


def last_reply(entries: list[dict]) -> str | None:
    """そのターンで受け付けた最後の報告への返事の種類（流れの制御の記録から読む。受け付けた報告が無ければ None）。"""
    accepted = [e for e in entries if (e.get("reply") or {}).get("result") != "rejected"]
    return accepted[-1]["reply"].get("result") if accepted else None


# 試験の相手役が、次の台詞へ進まずに答え直す返事（返事の種類 ⇒ 台詞の鍵）：聞き返された（"again"）・目的の人かを聞かれた（"if_asked_target"＝
# 目的の人ご本人の台詞だけが持つ。本物の方は「はい、そうです。」と答える。⛔ 2026-10-19 G-62：判定が遅れて目的の人かを確かめたとき、台本の
# 相手は次の台詞「はい、予約しています。」へ進み、呼んでいる段のまま答えが差し戻され続けた＝試験の作りの誤り）
ANSWER_AGAIN = {"ask_back": "again", "confirm_target": "if_asked_target"}


def answer_again(line, entries: list[dict], n: int) -> tuple[dict | None, int]:
    """試験の相手役が、次の台詞へ進まずに答え直すか（唯一の場所）：そのターンで受け付けた最後の報告への返事が、聞き返し・目的の人かの確かめで、
    台詞がその答え直しを持てば、(答え直す台詞, 回数)。そうでなければ (None, 0)＝次の台詞へ。AIの声を止めて言い直させたときも同じ（言い直しを
    聞いた相手は、聞かれたことに答える。⛔ 2026-10-19 G-62：止めたあとは必ず次の台詞へ進めていたので、言い直した確かめに相手が答えなかった）。
    ⛔ 本物の相手は、次の台詞へは進まず、聞かれたことに答え直す（2026-10-14 G-36：台本の相手が聞き返しのあとも次の台詞「はい、そうです」
    「3人です」へ進み、それが日時の答えとして報告された＝試験の作りの誤り）"""
    key = ANSWER_AGAIN.get(last_reply(entries))
    if isinstance(line, dict) and key in line and n < MAX_AGAIN:
        return {"wav": line[key], key: line[key]}, n + 1
    accepted = [e for e in entries if (e.get("reply") or {}).get("result") != "rejected"]
    if (isinstance(line, dict) and "wav" in line and n < MAX_AGAIN and accepted and accepted[-1].get("op") == "confirm_field"
            and (accepted[-1].get("args") or {}).get("result") == "affirmed" and accepted[-1]["reply"].get("result") == "confirm_required"):
        # 「合っている」の報告に、同じ値をもう一度確かめられた（返事の言葉では決められなかった＝Flow._confirm_again）：本物の相手は、同じ返事を
        # もう一度言う。⛔ 2026-10-25 J12b（伝言の申し出・S4）：相手役の「はい。そうです。」を会話AIの聞き取りが「Hi.」と書き（その録音の
        # 3,579回中14回）、流れは決まりどおり確かめ直したが、相手役は次の台詞（無い）へ進み、戻る時刻を取れないまま終わった
        return {"wav": line["wav"]}, n + 1
    return None, 0


def followup_reason(turn, words_complete: bool = True) -> str | None:
    """相手の続きの言葉への返事を、次の台詞を送る前に、もう1回ぶん待つ理由（待たなくてよいなら None）。
    - "words"：AIが報告したのに、台詞の声がまだ出揃っていない（文と文の間でAIのターンが終わった。流れの制御の Flow.words_complete の1か所）。
      ⛔ 2026-10-24 G-57 J2・J3（留守番電話のあとに人が出る・L4）：案内の文の間でAIが留守番電話と報告し、試験は次の台詞（人の「もしもし」）を
         送った＝AIは案内の後ろ半分を聞いて、また留守番電話と報告し、台詞が1つずつずれた（試験の作りの誤り。本物の電話では、相手は言い終えて
         から返事を待つ）
    - "cut_in"：相手の言葉の途中で、AIが話し出していた（G-31）
    - "waited"：AIが声も報告も出さずにターンを終えた（相手の「えーとですね、」のあと、黙って待った形）
      ⛔ 2026-10-14 G-34 の1回目：この空のターンを「AIの返事」と受け取って、すぐ次の台詞を送ったため、相手の続きへのAIの返事
         （言い直し）に次の台詞「はい、そうです。」が重なり、会話が1つずれた（試験の作りの誤り。本物の電話では、相手は続きを言い終えて、
         AIの返事を待つ）"""
    if turn.timed_out:
        return None
    if turn.first_audio_after_s is not None and turn.first_audio_after_s < CUT_IN_S:
        return "cut_in"
    if turn.first_audio_after_s is None and not turn.tool_calls:
        return "waited"  # 声も報告も無い
    if not words_complete:
        return "words"
    return None


async def check_ais(*, judges=(), ear_kind: str | None = None, ring=None, model: str | None = None) -> list[dict]:
    """回復の確かめの唯一の入口：通話で使うAI（判定・別の耳・会話AI）に、通話と同じ部品でつないで短く問い合わせ、答えたかを返す＝
    [{"ai", "ok", "error", "s"}]（名前は通話の報告の ai_trouble と同じ＝止まったAIが答えるようになったかを、名前で突き合わせられる）。
    ✅ 2026-10-30 夜 ユーザー：「基本的にアンソロピックが使えなくなったりその他に使う AI のどれかが止まったら、AI の不具合として回復するまで一時停止
    してください。」＝発信の門（CL-2）が、AIの不具合で止めた新しい電話を再開する前に呼ぶ（全部 ok なら回復。CL-1.md 4-62）。
    judges＝判定の受け持ち（meaning_judge.JudgePanel。中身の判定も同じAI）・ear_kind＝別の耳（つないで閉じるだけ＝声は送らない）・
    ring と model＝会話AI（つなぎを開いて閉じるだけ＝声も文字も送らない）。会話AIと別の耳は、通話と同じくキーの輪（common.KeyRing.acall）を
    通す＝上限のキーは飛ばして、使えるキーで確かめる（⛔ 2026-10-30：1本目のキー（上限の値が0）だけで確かめ、会話AIを「答えない」と誤った）。
    料金は判定の短い質問だけ（meaning_judge.probe_question）。"""
    from common import redact
    out: list[dict] = []
    for panel in judges:
        out += await panel.probe()

    async def opened(name, cm_of):
        async def once(client):
            closing: list = []
            async with cm_of(client, closing):
                pass
            await asyncio.gather(*closing)
        t0 = time.monotonic()
        try:
            if ring is not None:
                await ring.acall(model, once, max_wait_s=0)
            else:
                await once(None)
            out.append({"ai": name, "ok": True, "s": round(time.monotonic() - t0, 3)})
        except Exception as exc:
            out.append({"ai": name, "ok": False, "error": redact(repr(exc))[:160], "s": round(time.monotonic() - t0, 3)})
    if ear_kind:
        await opened(f"別の耳（{ear_kind}）", lambda client, closing: open_ear(ear_kind, client))
    if ring is not None and model:
        await opened("会話AI", lambda client, closing: open_session(client, model, build_config(voice=None, system_instruction="確かめ"),
                                                                    closing))
    return out


async def run_call(client, model: str, business: Business, voice_type: str, callee_lines: list, *,
                   swap_before: int | None = None, stall_at: int | None = None, stall_wait_s: float = 1.0,
                   opening_mode: str = "ai", barge_in: tuple[list[str], list[str]] | None = None,
                   thinking_budget: int | None = None, speech_end: dict | None = None, use_ear: bool = True, save_audio: str = "bad",
                   audio_label: str = "", confirm=None, gap_s: float = 0.0, lead_ins: bool = True,
                   followup_wait_s: float | None = None, callee_voice: str | None = None, play_recordings: bool = False,
                   ear_kind: str | None = None, judge=None, judge2=None, off_script_at: int | None = None,
                   off_script_heard: str | None = None, grounder=None, judge_route: str | None = None) -> dict:
    """followup_wait_s：相手の言葉の途中でAIが話し出していたとき・AIが黙って待ったとき（followup_reason）は、相手の続きの言葉への
    返事を、次の台詞を送る前に、この秒数まで、もう1回ぶん待つ（来なければ、相手が黙って待たされたことになる。
    記録の行は callee={"followup_of": 台詞, "reason": 理由}）。G-31・G-34
    callee_voice：お客役の声（"swap"＝会話のAIの声と入れ替え。callee_voice_for）。台詞の声を、その声のファイルに読み替える（G-36〜）
    ear_kind：別の耳の名前（ears.EAR_KINDS）。相手の声を、会話AIと同時に別の耳へも流し、確かめない型の答えの報告の返事を作る前に、別の耳の
    文字を待って流れの制御へ渡す（DESIGN.md 6-10 の (5)。G-15。check_handler）。⛔ 既定では使わない（None）：Gemini の書き起こしのモデルを
    別の耳にすると、要らない確かめが8.5%増え、拾えた誤りは0だった（G-15 の続き）。独立した別の耳（AmiVoice・Soniox・ReazonSpeech）を試す入口
    grounder：AIが自分の言葉で言う文の中身の判定（grounding.make_grounder。check_handler）。
    judge・judge2：意味の判定（async (質問, 相手の言葉, こちらが言い直した言葉) -> 中の名前。会社の違う2つを同時に出し、早く届いたほうで決め、
    遅れて届いたほうも同じ決まりで当てる。meaning_judge.make_judges＝止まった判定の代わりも入る。DESIGN.md 6-10 の (6)。G-53・G-61）。check_handler
    off_script_at：試験だけ。この番号の台詞へのAIの声を、渡した言い方から外れたものとして止める（外れたあとの言い直しの仕組みを確かめる。
    外れることは、ふつうはめったに起きないので。DESIGN.md 6-7）。off_script_heard：試験だけ。そのとき相手に聞こえていたものを、この種類と
    みなす（offscript.restate の3つの言い方を、それぞれ確かめる）"""
    if callee_voice == "swap":
        callee_voice = callee_voice_for(voice_type)
    callee_lines = [{**x, "wav": callee_wav(x["wav"], callee_voice), **{k: callee_wav(x[k], callee_voice) for k in ANSWER_AGAIN.values() if k in x}}
                    if isinstance(x, dict) and "wav" in x else
                    {**x, "yes": callee_wav(x["yes"], callee_voice), "no": callee_wav(x["no"], callee_voice)}
                    if isinstance(x, dict) and "if_value" in x else x for x in callee_lines]
    flow = business.flow(recorded=opening_mode == "recorded", play_recordings=play_recordings, lead_ins=lead_ins,
                         **({"confirm": confirm} if confirm else {}))  # confirm：言い直しの言い方（G-25 の比べだけ）。lead_ins：G-27
    flow.use_checks(*[k for k, used in (("ear", ear_kind), ("judge", judge), ("judge2", judge2)) if used])
    # 判定の道の状態（meaning_judge.RouteState。意味の判定と中身の判定で同じもの）に、この通話の知らせ先（止まったAI・緊急の道＝Flow.on_ai_trouble）
    # と、緊急の道から始めるか（judge_route＝RESUME_EMERGENCY：契約者以上の権限で、緊急の道を使って再開した通話。決めるのは発信の門＝CL-2）を入れる。
    # 通話の終わりに外す。⚠️ 1つの状態を同時に2つの通話で使わない（試作の道具は通話を1本ずつ流す。本番は通話ごとに作る）
    from meaning_judge import RESUME_EMERGENCY
    route_states = list({id(st): st for st in (getattr(j, "panel", None) for j in (judge, judge2)) + (getattr(grounder, "state", None),)
                         if st is not None}.values())
    for st in route_states:
        st.on_trouble, st.emergency_first = flow.on_ai_trouble, judge_route == RESUME_EMERGENCY
    checking = bool(ear_kind or judge or judge2)
    pending: list = []  # 確かめの作業（通話の終わりに、まだ走っているものは届くまで待つ＝通話後の検証と同じ。待っても届かなければ止める）
    turns: list[dict] = []
    sessions = 0
    replay: int | None = None  # 送り直す相手の発話の番号（止まったときの張り替え）
    again: dict | None = None  # 聞き返されたので、相手が次に言い直す台詞（台詞の "again"。i は進めない）
    again_n = 0
    swapped: int | None = None  # 黙ったので張り替えた相手の発話の番号（1つの発話に1回まで）
    restate_next: dict | None = None  # 外れた言葉を止めたあと、新しいつなぎに言わせること（Flow.on_off_script）
    stopped_at = 0.0                  # 外れた言葉を止めた時刻（言い直しの声が出るまでの間を測る）
    closing: list = []                # 後ろで閉じている古いつなぎ（live_util.open_session。通話の終わりにまとめて待つ）

    def watch(log_start: int, i: int):
        """そのターンの見張り：渡した言い方から外れたか（offscript.SpeechGate。DESIGN.md 6-7）。1つの相手の発話に1回まで。"""
        if swapped == i:
            return None
        # 外れたものとして止めるのは、その台詞で1回だけ（⛔ 2026-10-24 G-57 J5：その前にAIが黙ってつなぎを張り替えた通話では、1つ目のつなぎの
        # ときだけ止める作りだったので止めず、場面が外れた）
        return SpeechGate(lambda: flow.expected_say(log_start), force=off_script_at == i and not any(t.get("off_script") for t in turns))

    def ai_failed(why: str, i: int) -> bool:
        """会話AIが、相手の発話に声も受け付けた報告も返さなかった：張り替える（True）か、こちらの不具合で終えた（False）か。決めるのは
        Flow.on_ai_failed の1か所（同じ発話で張り替えたあとも戻らなければ、会話AIの異常で終える）。終えるときのお詫びの2つの一言は、電話接続側
        （CL-2 の call.end_for_trouble）が流して切る＝試作では、通話の終わりに記録を残すだけ（trouble_row）。"""
        nonlocal swapped
        if flow.on_ai_failed(why)["result"] == "swap":
            swapped = i
            return True
        return False

    def trouble_row(i: int) -> dict:
        """こちらの不具合で電話を終えた記録（Flow._trouble）。電話接続側が流す2つの一言と、そのあいだのお客様のお返事（AIは聞かない）。"""
        return {"index": i, "session": sessions, "callee": None, "signal": "end_trouble", "reason": flow.trouble["reason"], "why": flow.trouble["why"],
                "heard": "", "intended": "", "tool_events": [], "audio_seconds": 0,
                "played": [LINE_TROUBLE["ask"], "（お客様のお返事）", LINE_TROUBLE["bye"]]}

    def stop_off_script(log_start: int, i: int, turn) -> None:
        """AIの声が渡した言い方から外れた（6-7。見張りの watch が止めた）：このつなぎを閉じ、新しいつなぎに「失礼いたしました。改めまして、…」と
        言わせる（Flow.on_off_script）。相手の発話への報告は済んでいるので、送り直さずに次の台詞へ（⛔ G-57 の3回目：用件と関係の無い話を12秒した）。
        呼ぶ側は、i を進めてセッションの輪を抜ける。どのターン（相手の発話への返事・続きを待つ間）でも、この1か所"""
        nonlocal restate_next, swapped, stopped_at
        turns[-1]["off_script"] = flow.on_off_script(flow.expected_say(log_start), heard=off_script_heard or turn.heard["kind"])
        turns[-1]["heard_before_stop"] = turn.heard  # 止めたときに相手に聞こえていたもの（電話接続側は、決まるまで最初の文の切れ目で待つ）
        restate_next, swapped, stopped_at = turns[-1]["off_script"], i, time.monotonic()

    def config():
        voice, text = build(business, voice_type, resume=flow if sessions > 1 else None, opening_mode=opening_mode,
                            play_recordings=play_recordings)
        return voice, build_config(voice=voice, system_instruction=text, tools=tool_declarations(business.questions, replay=bool(flow.opening),
                                                                                          target=business.target is not None,
                                                                                          answers=tuple(n for n, _ in business.answers),
                                                                                          known=tuple(flow.known_lines())),
                                   thinking_budget=thinking_budget, speech_end=speech_end)

    played_upto = 0

    def deliver_plays() -> None:
        """流れの制御の返事で流した録音を、最後まで流れたと知らせる（Flow.on_played。本番は電話接続側が、流し終えたときに知らせる）。試作は
        名乗りの場面のほかは録音を止めないので、相手が次に話す前・電話が切れる前に、流した録音は全部流れ終わっている。"""
        nonlocal played_upto
        texts = [p["text"] for log in flow.log[played_upto:] for p in (log["reply"].get("play") or []) if p.get("text")]
        played_upto = len(flow.log)
        if texts:
            flow.on_played(texts)

    async def add_turn(turn, line, log_start: int, i: int, voice: str) -> None:
        """AIの1ターンぶんを記録する唯一の場所。"""
        r = turn.summary()
        audio, rate = bytes(turn.audio), turn.audio_rate or 24000
        row = {"index": i, "session": sessions, "callee": line, "heard": r["input_transcript"], "intended": r["output_transcript"],
               # 呼んでいる段で、話させる返事の無いAIの声は相手に流さない（Flow.speech_allowed。試作では流さなかった印を付け、数え方が除く）
               "muted": not flow.speech_allowed(log_start),
               # 操作の記録は、流れの制御の記録（中の名前）から取る。時刻はAIからの届いた順に合わせる。
               # ack：その報告が届いた瞬間に、電話接続側が流す相づち（flowctl.Flow._ack。試験では流さず、時刻と中身だけを記録する）
               "tool_events": [{"at_s": live.get("at_s"), "answered_s": live.get("answered_s"), "name": log["op"], "args": log["args"],
                                "reply": log["reply"].get("result"),
                                "ack": log["reply"].get("ack"), "say": log["reply"].get("say"), "play": log["reply"].get("play")}
                               for live, log in zip([e for e in r["events"] if e["kind"] == "tool_call"], flow.log[log_start:])],
               "first_audio_after_s": r["first_audio_after_s"], "finished_after_s": r["finished_after_s"],
               "audio_seconds": r["audio_seconds"], "timed_out": turn.timed_out,
               # 報告の返事を待たせている間のAIの声（相手に流さない＝live_util の held_audio）
               **({"held_audio_s": r["held_audio_s"], "held_said": r["held_text"]} if r["held_audio_s"] else {}),
               # AI自身の文字が届いた時刻と、その時点で受け取り済みの声の長さ（文字が声より先に届くかを測る）
               "said_events": [{k: e[k] for k in ("at_s", "audio_s", "text") if k in e} for e in r["events"] if e["kind"] == "ai_said"],
               # 相手の言葉の文字が届いた時刻（報告より先に届くか＝報告の時点でプログラムが相手の言葉を見て決められるか。G-36 の3回目）
               "heard_events": [{k: e.get(k) for k in ("at_s", "text", "finished")} for e in r["events"] if e["kind"] == "heard"],
               # 声のまとまりの始まり・返事を送った時刻（報告の返事の前と後の声を分けて、間を測る）
               "timeline": [{k: e[k] for k in ("at_s", "kind", "chunk", "speaking", "offset_s", "ids") if k in e} for e in r["events"]
                            if e["kind"] in ("audio_start", "tool_response_sent", "turn_complete", "interrupted", "tool_call_cancelled", "activity")],
               "phase_after": flow.phase, "current_after": flow.current,
               # 会話AIの量（Live の usage_metadata をそのまま。1通話の1分あたりの量＝CL-2 の設定 gate.ai_tpm_per_call を測る。CL-2 の 4-19）
               "usage": r["usage"]}
        if use_ear and audio:
            row["ear"] = await hear(client, audio, rate, "transcribe35")
        # 見張りは別の耳の文字のあとで当てる（何語を話したかは、口から出た声で決める。turn_check）
        row["check"] = check_ai_turn(audio=audio, rate=rate, voice=voice, timed_out=turn.timed_out,
                                     intended_text=r["output_transcript"] or None, heard_text=row.get("ear"))
        # 外れた言葉を止めたあとの言い直しのターン（restate）も、見張りにかかわらず残す（試験の耳の書き違いか、声の言い違いかを、あとから
        # 独立した耳で聞き分けるため。⛔ 2026-10-25 J11a・J12b：言い直しの「午後2時で」が試験の耳で「午後2で」と書かれ、音が無く決められなかった。
        # 同じ日に独立した2つの耳で聞いた14本は、3つの耳とも14本とも「午後2時」＝ARATAME-EARS-4）
        restated = isinstance(line, dict) and "restate" in line
        if audio and (save_audio == "all" or (save_audio == "bad" and (row["check"]["kind"] in BAD_KINDS or restated))):
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            label = f"{audio_label}_" if audio_label else ""
            part = "f" if isinstance(line, dict) and "followup_of" in line else ""
            path = AUDIO_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{label}{voice_type}_{i}{part}_{row['check']['kind']}.wav"
            write_wav(path, audio, rate)
            row["audio_file"] = path.name
        turns.append(row)

    if opening_mode == "recorded":
        # 電話が繋がった：こちらは話さずに、出た方の第一声を聞く（名乗りは、出た方が人と分かり、言葉が終わってから＝Flow.on_connect。
        # ✅ 2026-10-24 ユーザー。CL-1.md 4-45）。台詞の1つ目は、出た方の第一声
        flow.on_connect()
    async with (open_ear(ear_kind, client) if ear_kind else nullcontext()) as ear:  # 別の耳は通話ごとに1本（本番の形。ears.open_ear）
        i = 0
        while i < len(callee_lines) and flow.phase != "closing":
            sessions += 1
            opened_at = time.monotonic()
            voice, cfg = config()
            async with open_session(client, model, cfg, closing) as s:
                # 相手の言葉の文字と、話し始め・話し終わりの知らせは、届いた瞬間に流れの制御へ（words_complete。受け取りを始める前に届いても）
                inbox_of(s).on_heard, inbox_of(s).on_activity = flow.on_heard, flow.on_activity
                if restate_next is not None:
                    # 外れた言葉を止めたあと（6-7）：電話接続側は、止めた瞬間にお詫びの録音（ack）を流している。新しいつなぎに、言い直しを言わせる
                    log_start, plan, restate_next = len(flow.log), restate_next, None
                    t = time.monotonic()
                    await send_text(s, plan["message"])
                    turn = await collect_turn(s, started_at=t, on_tool_call=flow.handle_call, timeout_s=30, on_heard=flow.on_heard)
                    await add_turn(turn, {"restate": plan["say"]}, log_start, i - 1, voice)
                    # 止めてから言い直しの声が出るまで（つなぎ直しの時間を含む。お詫びの録音「失礼いたしました。」は約1秒）
                    turns[-1]["gap_s"] = (round(t + turn.first_audio_after_s - stopped_at, 2) if turn.first_audio_after_s is not None else None)
                    # 内訳：古いつなぎを閉じる・新しいつなぎを開く・知らせてから声が出る（どこを縮めるかを決めるため）
                    turns[-1]["gap_parts_s"] = {"close": round(opened_at - stopped_at, 2), "open": round(t - opened_at, 2),
                                                "speak": turn.first_audio_after_s}
                while i < len(callee_lines) and flow.phase != "closing":
                    if swap_before is not None and i == swap_before and sessions == 1:
                        break  # 張り替え：このセッションを閉じて、いまの位置を渡して開き直す
                    line = again or callee_lines[i]
                    if isinstance(line, dict) and "if_value" in line:
                        # こちらの言った値で答えが変わる相手（G-46：名前の読みを違う読みで聞かれたら、もう一度直す）。
                        # その項目がもう確かめ済みなら、この台詞は言わない（次の台詞へ）。選択肢の型は、こちらが言い直した選択肢で比べる
                        # （G-14 の続き：「念のためご予約はされているということでよろしいでしょうか？」に、相手の思っていた答えかで返事が変わる）
                        qid, want = line["if_value"]
                        it = flow.items[qid]
                        if it.state in ("confirmed", "dont_know", "refused"):
                            i += 1
                            continue
                        said = it.condition if flow.questions[qid].type in CHOICE_TYPES else it.value
                        line = {"wav": line["yes"] if said == want else line["no"]}
                    if isinstance(line, dict) and "silence_s" in line:
                        log_start = len(flow.log)
                        async with silence_while(s):
                            turn = await collect_turn(s, started_at=time.monotonic(), on_tool_call=flow.handle_call, timeout_s=line["silence_s"],
                                                     on_heard=flow.on_heard)
                        if (r := flow.on_start_silence()) is not None:
                            await send_note(s, r["message"])  # 第一声が無いまま黙っている：「もしもし」の録音を流した（電話接続側の知らせ）
                        await add_turn(turn, line, log_start, i, voice)
                        i += 1
                        continue
                    if stall_at is not None and i == stall_at and sessions == 1:
                        # AIが止まった：古いセッションへ相手の発話を送り、返事を受け取らずに閉じる
                        flow.on_callee()
                        await send_text(s, line)
                        await asyncio.sleep(stall_wait_s)
                        replay = i
                        break
                    if i == 1 and barge_in is not None and sessions == 1:
                        # 第一声のあとに流した名乗りの録音の途中で、相手が話し始めた：最後まで流れたとは数えない（deliver_plays に渡さない）
                        played_upto = len(flow.log)
                        flow.on_opening_cut(barge_in[0])  # 流れの制御も、どこまで届いたかを覚える（流し直しで、届いていない部分を足す）
                        await send_note(s, barge_in_note(business, *barge_in))  # 録音のどこまでが届いたか
                    if replay != i:  # 送り直しは、相手がもう一度話したのではないので数えない（文字だけを受け取り直す）
                        deliver_plays()
                        flow.on_callee()
                    else:
                        flow.on_resend()
                    log_start = len(flow.log)
                    if gap_s and i > 0:  # 相手が話し始めるまでの間（G-26：間の長さで、AIのふるまいが変わるかを見る）
                        await asyncio.sleep(gap_s)
                    ear_info, on_tool, settle = None, flow.handle_call, None
                    if isinstance(line, dict):  # 録った相手の声
                        pcm, prate = read_wav_pcm16_mono(HERE / "results" / "callee" / line["wav"])
                        if is_fax_tone(pcm, prate):
                            # 音で分かる相手（ファクス）：会話AIに聞かせる前に、プログラムが決めて切る（DESIGN.md 4-3 の優先順位の1。本番では
                            # 電話接続側が、受けた音を流しながら同じ見分け方を当てる）
                            flow.on_signal("fax")
                            turns.append({"index": i, "session": sessions, "callee": line, "signal": "fax", "heard": "", "intended": "",
                                          "tool_events": [], "audio_seconds": 0})
                            i += 1
                            continue
                        on_tool, ear_task, ear_info, settle = (check_handler(flow, ear=ear, judge=judge, judge2=judge2, pcm=pcm, rate=prate,
                                                                             wav=line["wav"], tasks=pending, audio_at=audio_sent_s(s),
                                                                             clip_s=lambda part: recording_seconds(voice_type, part),
                                                                             grounder=grounder)
                                                               if checking else (flow.handle_call, None, None, None))
                        turn = await callee_turn(s, pcm, prate, on_tool_call=on_tool, timeout_s=90, on_heard=flow.on_heard,
                                                 silent_after_s=SILENT_SWAP_S, gate=watch(log_start, i))
                        if ear_task is not None:
                            await ear_task  # 声を流し終えるのを待つ（会話AIへ送るのと同じ長さ）
                            # 待つ時間とは別に、文字が実際に届いた時刻（話し終わりから。待つ時間を決めるための記録）
                            ear_info["arrived_s"] = round(ear.first_at - ear_info["_end"], 3) if ear.first_at else None
                    else:
                        t = time.monotonic()
                        flow.on_heard(line)  # 文字で送る相手の言葉は、そのまま相手の言葉の文字
                        on_tool, _, ear_info, settle = (check_handler(flow, judge=judge, judge2=judge2, tasks=pending,
                                                                      clip_s=lambda part: recording_seconds(voice_type, part), grounder=grounder)
                                                        if checking
                                                        else (flow.handle_call, None, None, None))
                        await send_text(s, line)
                        turn = await collect_turn(s, started_at=t, on_tool_call=on_tool, timeout_s=90, on_heard=flow.on_heard,
                                                  silent_after_s=SILENT_SWAP_S, gate=watch(log_start, i))
                    if ear_info is not None:
                        ear_info.pop("_end", None)
                    await add_turn(turn, line, log_start, i, voice)
                    if turn.silent:
                        # AIが黙った（6-7）：このつなぎを閉じ、いまの位置を渡して開き直し、相手の同じ発話を送り直す（相手は1回しか話していない）。
                        # 張り替えたあとも黙ったら、こちらの不具合で終える（決めるのは Flow.on_ai_failed の1か所）
                        if ear_info is not None:
                            turns[-1]["ear_check"] = ear_info  # 張り替えたターンの確かめも残す（中身の判定の待ち・遅れを数えるため）
                        if ai_failed("声も報告も無いまま黙った", i):
                            replay = i
                        break
                    if turn.off_script:
                        if ear_info is not None:
                            turns[-1]["ear_check"] = ear_info
                        stop_off_script(log_start, i, turn)
                        again, again_n = answer_again(line, flow.log[log_start:], again_n)
                        i += again is None
                        break
                    if ear_info is not None:
                        turns[-1]["ear_check"] = ear_info
                    turn_log_start = log_start
                    reason = followup_reason(turn, flow.words_complete) if followup_wait_s else None
                    if reason:
                        log_start = len(flow.log)
                        # 待つ間も、電話と同じく無音を送り続ける（会話AIは、無音が続くことで相手の話し終わりを知る）
                        # ⛔ 2026-10-14 G-31 の1回目：無音を送らずに待っていたため、相手の続きの言葉の話し終わりが会話AIに伝わらず、
                        #    返事が来なかった（試験の作りの誤り。本物の電話では無音が流れ続ける）
                        async with silence_while(s, prate if isinstance(line, dict) else 16000):
                            follow = await collect_turn(s, started_at=time.monotonic(), on_tool_call=on_tool, timeout_s=followup_wait_s,
                                                        on_heard=flow.on_heard, gate=watch(turn_log_start, i))  # 言い方は、相手の発話への返事で渡したもの
                        await add_turn(follow, {"followup_of": line, "reason": reason}, log_start, i, voice)
                        if follow.off_script:
                            stop_off_script(turn_log_start, i, follow)
                            again, again_n = answer_again(line, flow.log[turn_log_start:], again_n)
                            i += again is None
                            break
                    if (r := flow.on_first_words_end()) is not None:
                        # 出た方の言葉が出揃った（続きの言葉を待ったあと。本番は、会話AIの話し終わりの知らせ）：人と分かっていれば、名乗りの録音を
                        # 流した（電話接続側の知らせ）。AIが言葉の途中で人と報告したとき（言葉の終わりで流す）
                        await send_note(s, r["message"])
                    spoke = any(t.get("audio_seconds") for t in turns if t["index"] == i and t["session"] == sessions)
                    ops = flow.log[turn_log_start:]
                    if not spoke and ops and all(e["reply"].get("result") == "rejected" for e in ops):
                        # 報告がどれも差し戻され、そのあとAIが声を出さなかった＝相手には黙ったのと同じ（6-7：声が出ないのを見張る）。
                        # ⛔ 2026-10-17：「結構です。」で締めを頼んで差し戻されたあと、AIが声を出さず（AI自身の文字は関係の無い外国語の文）、
                        #    通話が止まった。張り替えは「声も操作も無い」ときだけだった
                        if ai_failed("報告がどれも差し戻され、声も出なかった", i):
                            replay = i
                        break
                    # 呼んでいる段で、報告を決めずに待たせたまま（相手の言葉の途中の報告）AIのターンが終わった：言葉が出揃ったら報告し直すように
                    # 知らせる（知らせるかは流れの制御が決める＝Flow.unreported_note の1か所）
                    waited = bool(ops) and ops[-1]["reply"].get("result") in ("wait", "noted", "hold")
                    # 流さなかったAIの声（Flow.speech_allowed）：受け付けた報告が無ければ、報告し直すように知らせる（決めるのは Flow.unreported_note）
                    muted = any(t.get("muted") and t.get("audio_seconds") for t in turns if t["index"] == i and t["session"] == sessions)
                    if (spoke and not ops or waited or muted) and settle is not None:
                        await settle()  # 判定が「答え」と決めたときだけ知らせる（Flow.unreported_note）
                    if (spoke and not ops or waited or muted) and (note := flow.unreported_note(muted=muted)):
                        # AIが声は出したのに、報告なしでターンを終えた：報告を促す（流れの制御が決める。1つの相手の発話に1回まで）。
                        # 本番では、この知らせへのAIの声のうち、報告より前のものは相手に流さない（電話接続側）。試験では記録だけする
                        log_start = len(flow.log)
                        await send_text(s, note)
                        nudge = await collect_turn(s, started_at=time.monotonic(), on_tool_call=on_tool, timeout_s=followup_wait_s or 8.0,
                                                   on_heard=None)
                        await add_turn(nudge, {"nudge_of": line, "note": note}, log_start, i, voice)
                        if muted and all(e["reply"].get("result") == "rejected" for e in flow.log[turn_log_start:]):
                            # 流さなかった声のあと、知らせのあとも報告を受け付けていない（黙った・流せない声だけ）：相手には無言が続く＝黙ったのと
                            # 同じ（DESIGN.md 6-7）⇒ 張り替えて、相手の同じ発話を送り直す。⛔ 2026-10-22 G-57 tc34（名乗らない方・L4）：知らせのあと
                            # AIは黙ったまま報告せず、試作は次の台詞へ進んだ（流せない声のときだけ張り替えていた）
                            if ai_failed("流せない声のあと、知らせても報告が無い", i):
                                replay = i
                            break
                    again, again_n = answer_again(line, flow.log[turn_log_start:], again_n)
                    if again is not None:
                        continue
                    i += 1
    # 電話が切れた瞬間（切れ方の材料＝Flow.on_line_end の1か所）。試作では、AIのターンを話し終えるまで待ってから次へ進むので、切れた瞬間は
    # だれも話していない：締めた（こちらが締めて切った）か、相手の台詞が尽きた（AIが話し終えたあとに、相手が何も言わずに切った）
    if (r := flow.on_voicemail_wait_end()) is not None:
        turns.append({"index": i, "session": sessions, "callee": None, "signal": "voicemail_wait_end", "heard": "", "intended": "",
                      "tool_events": [], "audio_seconds": 0})   # 留守番電話の案内のあと、人が出なかった（試作では、台詞が尽きたら待ちきった）
    if flow.trouble is not None:
        turns.append(trouble_row(i))   # こちらの不具合で終えた（Flow._trouble）：電話接続側がお詫びの2つの一言を流して切る
    deliver_plays()
    flow.on_line_end(ai_speaking=False, callee_speaking=False)
    # 通話は終わった：まだ走っている確かめは、届くまで待って同じ決まりで当てる（通話後の検証＝DESIGN.md 6-12 と同じ判定・同じ決まり。
    # Flow.on_check ⇒ _late）。LATE_CHECK_S まで待っても届かないものは止める（届かなかった＝unchecked。通話後の検証でやり直す）。
    # ⛔ 2026-10-18 に見つけた漏れ：通話の終わりに止めていたので、1秒以上かかる2つ目の判定（Sonnet 5.5）が、短い試験の通話ではほとんど
    #    当たらないまま「受け取った」と数えていた（本番では通話後の検証で当たる）
    before_after = len(flow.unchecked())
    running = [t for t in pending if not t.done()]
    if running:
        await asyncio.wait(running, timeout=LATE_CHECK_S)
    for t in pending:
        t.cancel()
    await asyncio.gather(*closing, return_exceptions=True)
    for st in route_states:
        st.on_trouble, st.emergency_first = None, False
    for e in (ear.errors if ear is not None else ()):
        flow.on_ai_trouble({"model": f"別の耳（{ear_kind}）", "error": e, "outage": True})   # 別の耳が止まった（ears.Ear.errors）
    return {"voice_type": voice_type, "sessions": sessions, "turns": turns, "flow_log": flow.log, "trouble": flow.trouble,
            "items": {k: {"state": v.state, "value": v.value, "condition": v.condition, "reason": v.reason, "ask_backs": v.ask_backs,
                          "rechecks": v.rechecks, "corrections": v.corrections, "hedged": v.hedged,
                          "history": [list(h) for h in v.history]}
                      for k, v in flow.items.items()},
            "export": flow.export(), "hedged_words": flow.hedged_words(), "final_phase": flow.phase, "hurried": flow.hurried,
            "facts": flow.facts,
            "not_reached": flow.not_reached,  # 目的の人に繋がらずに終わった理由（会話の報告の not_reached。Flow.not_reached の1か所）
            "cut": flow.cut,  # 切れ方の材料（会話の報告の cut。Flow.on_line_end の1か所。CL-2 の 4-151）
            "apology_delivered": flow.apology_delivered,  # お詫びの電話で、お詫びを伝えられたか（会話の報告の apology_delivered。お詫びの電話でなければ None）
            "checked_after_call": before_after - len(flow.unchecked()),
            "opening_mode": opening_mode, "barge_in": barge_in, "ear_errors": ear.errors if ear is not None else None,
            "unchecked": flow.unchecked(),
            "ai_trouble": flow.ai_trouble}  # 止まったAI・緊急の道で答えたこと（会話の報告の ai_trouble。Flow.on_ai_trouble の1か所＝CL-2 が新しい電話を一時停止する）
