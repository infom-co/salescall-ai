# phase0 — フェーズ0の確認スクリプト（PC で動かす）

`docs/PHASE0_VERIFICATION.md` の G（会話AI）と K（APIキー）の項目を、**手で迷わず確かめるための道具**です。
⚠️ 製品のプログラムではありません。フェーズ0の合否の根拠（結果の JSON と音声）を残すためのものです。

## 作った環境での確認（2026-10-12）
- Google の公式ライブラリ `google-genai` **2.29.0** の中身に合わせて書きました（関数名・設定の項目を、ライブラリから直接確かめています）。
- 接続しない形（`--dry-run`）では、全スクリプトの設定を、ライブラリが **Google へ送る形に変換できる**ことまで確かめました。
- `test_offline.py`（11件）が通ります：`.env` の読み方、**キーの値が画面・結果のファイルに出ないこと**、試験用の流れの制御の決まり。
- ⛔**作った環境からは Google につながらないため、実際の接続は一度もしていません。** 初めて動かすのは PC です。

## 準備（Windows）
```bat
cd （このリポジトリを置いた場所）\phase0
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m unittest test_offline
```
- キーは `C:\Users\User\SalesCall\.env` から読みます（別の場所なら `--env パス`、または環境変数 `SALESCALL_ENV`）。
- **キーの値は、画面にも結果のファイルにも出しません**（識別名と、値から作った短い指紋だけを出します）。

## 動かす順番
| 順 | コマンド | 確かめる項目 | 料金 |
|---|---|---|---|
| 1 | `python k01_env_shape.py` | K-1：`.env` の形（変数名・長さ・指紋だけ） | なし |
| 2 | `python g01_connect.py --key-name 変数名` | G-1・G-2・G-9：接続できるか、音声の形式、利用量が返るか | ごく少し |
| 3 | `python g03_voices.py --key-name 変数名` | G-3：Sadachbia・Leda の日本語の声（`results/G-03_*.wav` を3人以上で聞く） | ごく少し |
| 4 | `python g06_flow.py --key-name 変数名` | G-6・G-7・G-16 の入口：AIが操作を呼び、プログラムの返事どおりに進むか／聞き返しは1回か／復唱するか／文字起こし | 少し |
| 5 | `python k03_invalid_key.py` | K-3：無効なキーのときのエラーの形（本物のキーは使わない） | なし |
| 6 | `python g04_session_limit.py --key-name 変数名 --max-minutes 20 --resume` | G-4・G-5：何分で切れるか、切れる前の知らせ、再開・指示の渡し直し | 20分ぶんの音声入力 |

- `--key-name` は、手順1で見た変数名を指定します（省略すると、名前に GEMINI か GOOGLE と KEY を含むものを使います）。
- モデルIDは `--model` で変えられます（既定は計画書の `gemini-3.8-live`。G-1 で公式の値を確かめてください）。
- 声は `--voice Sadachbia` のように変えられます（g01・g06）。

## 結果の扱い
- 結果は `phase0/results/<項目>_<日時>.json` に保存されます。音声は同じ場所の `.wav` です（`.wav` は git に入れません）。
- **`docs/PHASE0_VERIFICATION.md` の各項目の「結果」の欄に、JSON のファイル名を根拠として書き込んでください。**
- G-6 の JSON の `checks` が、確かめる点の合否です：
  - `question_id_matched`：AIが、いまの質問のIDで答えを報告したか
  - `held_until_confirm`：確かめる型の答えで、復唱の報告まで次へ進まなかったか
  - `ask_back_requested` / `ask_back_at_most_once`：聞き返しを求め、1回までだったか
  - `readback_spoken`：「15日」「2時」をAIが言い直したか
  - `transcript_available`：AIの発話の文字起こしが取れたか
  - `rejected_calls`：プログラムが差し戻した呼び出し（多ければ、指示文の書き方を見直す）

## 次に足すもの（フェーズ0の残り）
- G-11（応答の速さ）：実際の音声を送って測る形（g06 を音声の入力に替えたもの）。
- G-13（言ってはいけないこと）：相手役が値引き・契約を何度も求める台本。
- G-14（聞き取りの試験集）：電話の音質で録った答え 200 以上と、その正解。ライブラリの次の2つの設定も、効き目を測る候補にする。
  - 文字起こしの言葉のヒント（`AudioTranscriptionConfig` の `adaptation_phrases`・`custom_vocabulary`）
  - 相手が話し始めた・話し終えたと判断する感度（`RealtimeInputConfig.automatic_activity_detection`）
- R（Rakuten Link）：PC の音声装置を経由した、相手の声の取り出しと、AIの声の送り込み。
