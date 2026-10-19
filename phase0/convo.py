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
import time
from contextlib import nullcontext

from audio import is_fax_tone
from common import HERE, read_wav_pcm16_mono, write_wav
from offscript import SpeechGate
from flowctl import CHOICE_TYPES, Flow, tool_declarations
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
def check_handler(flow: Flow, *, ear=None, judge=None, judge2=None, pcm: bytes | None = None, rate: int = 16000, wav: str | None = None,
                  tasks: list | None = None, audio_at: float | None = None):
    """相手の1つの発話の確かめ（DESIGN.md 1章・6-10）：相手の声を、会話AIへ送るのと同時に別の耳（通話ごとに1本つないだ ears.Ear）へも流し、
    相手の言葉が出揃ったら（Flow.words_complete＝会話AIの話し終わりの知らせ）、会話AIの聞き取りの文字（無ければ別の耳の文字）を意味の判定
    （judge＝async (質問, 言葉, こちらが言い直した言葉) -> 中の名前。meaning_judge）へ渡す。judge2＝後ろで走らせる2つ目の判定（待たない）。
    届いたものは、いつ届いても、この発話の番号と見た言葉を付けて flow.on_check へ渡す（受け取ったあとに届けば、流れの制御が同じ決まりで
    当てる）。文字で送る相手の言葉（pcm 無し）は、はじめから出揃っている。
    答えをそのまま受け取りうる報告（flow.wants_checks）が来たら、返事を作る前に、この報告と同じ相手の言葉の確かめ（別の耳・判定）を待つ（hold）。
    返り値＝(報告の返事を作る処理, 声を流す作業, 記録, 判定を待つ処理)。判定を待つ処理（settle）：AIが報告しなかったターンのあと、報告を
    促すかを決める前に呼ぶ。tasks：始めた確かめの作業を足す先（通話の終わりに、まだ走っているものを待つため）。
    audio_at：この発話の声を送り始める、会話AIへ送った音の上の位置（live_util.audio_sent_s）。声の終わりの位置を流れの制御へ渡す（Flow.on_voice。
    本番は電話接続側の声の見張りが、送った音の上で決める）。"""
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
    q, asked = flow.judge_context()  # 判定の質問（目的の人を呼んでいる段では、呼ぶ一言）
    runs = {k: r for k, r in (("judge", judge), ("judge2", judge2)) if r is not None and q}

    def started(coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        tasks.append(t)
        return t

    judging_text: dict[str, str | None] = {}  # 判定ごとに、いま判定している言葉（None＝出揃うのを待っている＝出揃った言葉で判定する）

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
                got = await asyncio.wait_for(runs[kind](q, text, asked), LATE_CHECK_S)
            except Exception as exc:  # 判定が落ちても通話は止めない（届かなかった＝通話後の検証へ）
                info[f"{kind}_error"] = repr(exc)[:200]
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
            flow.on_check(kind, got, turn, said=said)
            return got
    jobs = {kind: started(judging(kind)) for kind in runs}

    async def hold(kinds: tuple[str, ...], ack: bool = True) -> None:
        """報告を受け取る前（・報告を促すかを決める前）に、いまの相手の言葉の確かめ（kinds）がそろうのを待つ。相手が話している間は待ち続ける
        （会話AIの話し終わりの知らせまで。相手が話しているので、こちらが黙っていても間にはならない。⛔ AIが相手の言葉の間で報告した
        ＝G-57 の「岡田ですね。」「少々お待ちください。」）。話し終わってからは flow.limits.hold_s 秒まで。報告を待たせる間は「はい、」の相づち
        （ack。held_ack）。
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
        for kind in (k for k in runs if k in kinds or (k == "judge2" and "judge" in kinds)):
            # いまの言葉の判定が無く、走っている判定も無いか古い言葉のもの ⇒ いまの言葉で判定し直す（2つ目の判定は待たない）
            stale = jobs[kind].done() or judging_text.get(kind) not in (None, flow.callee_said)
            if complete() and flow.callee_said and kind not in flow.check and stale:
                jobs[kind] = started(judging(kind, flow.callee_said, flow.callee_said))
                info.setdefault("rejudged", []).append(kind)
        waits = [t for t in ([etask] if "ear" in kinds else []) + [jobs[k] for k in kinds if k in jobs] if t is not None and not t.done()]
        if ack and (waits or ready - t0 > 0.05):
            info["held_ack"] = "hai"
        if waits and not info.get("incomplete"):  # 出揃っていなければ、判定を待っても使わない（流れの制御は決めずに待つ）
            await asyncio.wait(waits, timeout=max(0.0, max(ready, quiet) + flow.limits.hold_s - loop.time()))
        info.setdefault("waits", []).append({"for": "report" if ack else "settle", "speaking_s": round(ready - t0, 3),
                                             "waited_s": round(loop.time() - t0, 3),
                                             "late": [k for k, t in (("ear", etask), *((k, jobs.get(k)) for k in kinds if k != "ear"))
                                                      if t is not None and not t.done()]})
        if ack:
            info.setdefault("waited_s", info["waits"][-1]["waited_s"])
            info.setdefault("late", info["waits"][-1]["late"])

    async def handle(fc):
        midway = not complete()  # AIが、相手の言葉が出揃う前に報告した（Flow.handle の midway）
        if flow.wants_checks(fc.name, fc.args):
            await hold(("ear", "judge"))
        return flow.handle_call(fc, midway)

    async def settle() -> None:
        await hold(("judge",), ack=False)
    return handle, task, info, settle


# AIが黙ったとき（相手が話し終えてから、声も操作も出ない）に、つなぎを張り替えるまでの秒（DESIGN.md 6-7：2秒で「少々お待ちいただいても
# よろしいでしょうか。」の録音・さらに10秒で張り替え＝案の値。試験では録音は流さず、張り替えだけをする）。⛔ 2026-10-17：記録の通話 2,069本中7本で、
# AIが「はい、先週しました。」などのあとに90秒黙り（試験の待ちの上限）、報告が無いまま終わっていた（試験の側に 6-7 の張り替えが無かった）
SILENT_SWAP_S = 12.0
MAX_AGAIN = 5  # 試験の歯止め（聞き返しの回数は、流れの制御が決める。これはその決まりが壊れたときに、通話を終わらせるためだけ）


def asked_back(entries: list[dict]) -> bool:
    """そのターンで受け付けた最後の報告への返事が、聞き返しか（流れの制御の記録から読む）。"""
    accepted = [e for e in entries if (e.get("reply") or {}).get("result") != "rejected"]
    return bool(accepted) and accepted[-1]["reply"].get("result") == "ask_back"


def followup_reason(turn) -> str | None:
    """相手の続きの言葉への返事を、次の台詞を送る前に、もう1回ぶん待つ理由（待たなくてよいなら None）。
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
    return None


async def run_call(client, model: str, business: Business, voice_type: str, callee_lines: list, *,
                   swap_before: int | None = None, stall_at: int | None = None, stall_wait_s: float = 1.0,
                   opening_mode: str = "ai", barge_in: tuple[list[str], list[str]] | None = None,
                   thinking_budget: int | None = None, speech_end: dict | None = None, use_ear: bool = True, save_audio: str = "bad",
                   audio_label: str = "", confirm=None, gap_s: float = 0.0, lead_ins: bool = True,
                   followup_wait_s: float | None = None, callee_voice: str | None = None, play_recordings: bool = False,
                   ear_kind: str | None = None, judge=None, judge2=None, off_script_at: int | None = None,
                   off_script_heard: str | None = None) -> dict:
    """followup_wait_s：相手の言葉の途中でAIが話し出していたとき・AIが黙って待ったとき（followup_reason）は、相手の続きの言葉への
    返事を、次の台詞を送る前に、この秒数まで、もう1回ぶん待つ（来なければ、相手が黙って待たされたことになる。
    記録の行は callee={"followup_of": 台詞, "reason": 理由}）。G-31・G-34
    callee_voice：お客役の声（"swap"＝会話のAIの声と入れ替え。callee_voice_for）。台詞の声を、その声のファイルに読み替える（G-36〜）
    ear_kind：別の耳の名前（ears.EAR_KINDS）。相手の声を、会話AIと同時に別の耳へも流し、確かめない型の答えの報告の返事を作る前に、別の耳の
    文字を待って流れの制御へ渡す（DESIGN.md 6-10 の (5)。G-15。check_handler）。⛔ 既定では使わない（None）：Gemini の書き起こしのモデルを
    別の耳にすると、要らない確かめが8.5%増え、拾えた誤りは0だった（G-15 の続き）。独立した別の耳（AmiVoice・Soniox・ReazonSpeech）を試す入口
    judge：意味の判定（async (質問, 相手の言葉, こちらが言い直した言葉) -> 中の名前。meaning_judge。DESIGN.md 6-10 の (6)。G-53）。
    judge2：後ろで走らせる2つ目の判定（待たない。遅れて届けば同じ決まりで当てる）。check_handler
    off_script_at：試験だけ。この番号の台詞へのAIの声を、渡した言い方から外れたものとして止める（外れたあとの言い直しの仕組みを確かめる。
    外れることは、ふつうはめったに起きないので。DESIGN.md 6-7）。off_script_heard：試験だけ。そのとき相手に聞こえていたものを、この種類と
    みなす（offscript.restate の3つの言い方を、それぞれ確かめる）"""
    if callee_voice == "swap":
        callee_voice = callee_voice_for(voice_type)
    callee_lines = [{**x, "wav": callee_wav(x["wav"], callee_voice), **({"again": callee_wav(x["again"], callee_voice)} if "again" in x else {})}
                    if isinstance(x, dict) and "wav" in x else
                    {**x, "yes": callee_wav(x["yes"], callee_voice), "no": callee_wav(x["no"], callee_voice)}
                    if isinstance(x, dict) and "if_value" in x else x for x in callee_lines]
    flow = Flow(business.questions, busy_ask=business.busy_ask, lead_ins=lead_ins, play_recordings=play_recordings,
                voicemail_message=business.voicemail_message,
                # 目的の人のある業務：繋がるまで最初の質問を渡さない（DESIGN.md 4-3）。名乗り・用件の文は、AIが話す形でも流れの制御が持つ
                target=business.replay_parts() if business.target is not None else None,
                opening=business.replay_parts() if opening_mode == "recorded" else None,  # 名乗りを録音で流す形なら、流し直しもできる（6-13）
                **({"confirm": confirm} if confirm else {}))  # confirm：言い直しの言い方（G-25 の比べだけ）。lead_ins：G-27
    flow.use_checks(*[k for k, used in (("ear", ear_kind), ("judge", judge), ("judge2", judge2)) if used])
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
        return SpeechGate(lambda: flow.expected_say(log_start), force=off_script_at == i and sessions == 1)

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
                                                                                          target=business.target is not None),
                                   thinking_budget=thinking_budget, speech_end=speech_end)

    async def add_turn(turn, line, log_start: int, i: int, voice: str) -> None:
        """AIの1ターンぶんを記録する唯一の場所。"""
        r = turn.summary()
        audio, rate = bytes(turn.audio), turn.audio_rate or 24000
        row = {"index": i, "session": sessions, "callee": line, "heard": r["input_transcript"], "intended": r["output_transcript"],
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
               "phase_after": flow.phase, "current_after": flow.current}
        if use_ear and audio:
            row["ear"] = await hear(client, audio, rate, "transcribe35")
        # 見張りは別の耳の文字のあとで当てる（何語を話したかは、口から出た声で決める。turn_check）
        row["check"] = check_ai_turn(audio=audio, rate=rate, voice=voice, timed_out=turn.timed_out,
                                     intended_text=r["output_transcript"] or None, heard_text=row.get("ear"))
        if audio and (save_audio == "all" or (save_audio == "bad" and row["check"]["kind"] in BAD_KINDS)):
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            label = f"{audio_label}_" if audio_label else ""
            part = "f" if isinstance(line, dict) and "followup_of" in line else ""
            path = AUDIO_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{label}{voice_type}_{i}{part}_{row['check']['kind']}.wav"
            write_wav(path, audio, rate)
            row["audio_file"] = path.name
        turns.append(row)

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
                    if i == 0 and barge_in is not None and sessions == 1:
                        flow.on_opening_cut(barge_in[0])  # 流れの制御も、どこまで届いたかを覚える（流し直しで、届いていない部分を足す）
                        await send_note(s, barge_in_note(business, *barge_in))  # 録音のどこまでが届いたか
                    if replay != i:  # 送り直しは、相手がもう一度話したのではないので数えない（文字だけを受け取り直す）
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
                                                                             wav=line["wav"], tasks=pending, audio_at=audio_sent_s(s))
                                                               if checking else (flow.handle_call, None, None, None))
                        turn = await callee_turn(s, pcm, prate, on_tool_call=on_tool, timeout_s=90, on_heard=flow.on_heard,
                                                 silent_after_s=SILENT_SWAP_S if swapped != i else None, gate=watch(log_start, i))
                        if ear_task is not None:
                            await ear_task  # 声を流し終えるのを待つ（会話AIへ送るのと同じ長さ）
                            # 待つ時間とは別に、文字が実際に届いた時刻（話し終わりから。待つ時間を決めるための記録）
                            ear_info["arrived_s"] = round(ear.first_at - ear_info["_end"], 3) if ear.first_at else None
                    else:
                        t = time.monotonic()
                        flow.on_heard(line)  # 文字で送る相手の言葉は、そのまま相手の言葉の文字
                        on_tool, _, ear_info, settle = (check_handler(flow, judge=judge, judge2=judge2, tasks=pending) if checking
                                                        else (flow.handle_call, None, None, None))
                        await send_text(s, line)
                        turn = await collect_turn(s, started_at=t, on_tool_call=on_tool, timeout_s=90, on_heard=flow.on_heard,
                                                  silent_after_s=SILENT_SWAP_S if swapped != i else None, gate=watch(log_start, i))
                    if ear_info is not None:
                        ear_info.pop("_end", None)
                    await add_turn(turn, line, log_start, i, voice)
                    if turn.silent:
                        # AIが黙った（6-7）：このつなぎを閉じ、いまの位置を渡して開き直し、相手の同じ発話を送り直す（相手は1回しか話していない）
                        replay = swapped = i
                        break
                    if turn.off_script:
                        if ear_info is not None:
                            turns[-1]["ear_check"] = ear_info
                        stop_off_script(log_start, i, turn)
                        i += 1
                        break
                    if ear_info is not None:
                        turns[-1]["ear_check"] = ear_info
                    turn_log_start = log_start
                    reason = followup_reason(turn) if followup_wait_s else None
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
                            i += 1
                            break
                    spoke = any(t.get("audio_seconds") for t in turns if t["index"] == i and t["session"] == sessions)
                    ops = flow.log[turn_log_start:]
                    if not spoke and ops and all(e["reply"].get("result") == "rejected" for e in ops) and swapped != i:
                        # 報告がどれも差し戻され、そのあとAIが声を出さなかった＝相手には黙ったのと同じ（6-7：声が出ないのを見張る）。
                        # ⛔ 2026-10-17：「結構です。」で締めを頼んで差し戻されたあと、AIが声を出さず（AI自身の文字は関係の無い外国語の文）、
                        #    通話が止まった。張り替えは「声も操作も無い」ときだけだった
                        replay = swapped = i
                        break
                    if spoke and not flow.log[turn_log_start:] and settle is not None:
                        await settle()  # 判定が「答え」と決めたときだけ知らせる（Flow.unreported_note）
                    if spoke and not flow.log[turn_log_start:] and (note := flow.unreported_note()):
                        # AIが声は出したのに、報告なしでターンを終えた：報告を促す（流れの制御が決める。1つの相手の発話に1回まで）。
                        # 本番では、この知らせへのAIの声のうち、報告より前のものは相手に流さない（電話接続側）。試験では記録だけする
                        log_start = len(flow.log)
                        await send_text(s, note)
                        nudge = await collect_turn(s, started_at=time.monotonic(), on_tool_call=on_tool, timeout_s=followup_wait_s or 8.0,
                                                   on_heard=None)
                        await add_turn(nudge, {"nudge_of": line, "note": note}, log_start, i, voice)
                    if isinstance(line, dict) and "again" in line and asked_back(flow.log[turn_log_start:]) and again_n < MAX_AGAIN:
                        # 聞き返された：本物の相手は、次の台詞へは進まず、聞かれたことに答え直す（⛔ 2026-10-14 G-36：台本の相手が聞き返しの
                        # あとも次の台詞「はい、そうです」「3人です」へ進み、それが日時の答えとして報告された＝試験の作りの誤り）
                        again, again_n = {"wav": line["again"], "again": line["again"]}, again_n + 1
                        continue
                    again, again_n = None, 0
                    i += 1
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
    return {"voice_type": voice_type, "sessions": sessions, "turns": turns, "flow_log": flow.log,
            "items": {k: {"state": v.state, "value": v.value, "condition": v.condition, "ask_backs": v.ask_backs,
                          "rechecks": v.rechecks, "corrections": v.corrections, "hedged": v.hedged,
                          "history": [list(h) for h in v.history]}
                      for k, v in flow.items.items()},
            "export": flow.export(), "hedged_words": flow.hedged_words(), "final_phase": flow.phase, "hurried": flow.hurried,
            "facts": flow.facts,
            "checked_after_call": before_after - len(flow.unchecked()),
            "opening_mode": opening_mode, "barge_in": barge_in, "ear_errors": ear.errors if ear is not None else None,
            "unchecked": flow.unchecked()}
