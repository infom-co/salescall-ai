# Rakuten Link デスクトップ版（2.1.0）の画面の部品（2026-11-20・管理者の PC で読んだ。docs/PHONE.md 2-1 の `SCREEN` の材料）

> 管理者の PC で `probe_screen.py` を動かして読んだものから、**画面の部品の形だけ**を写した（通話の履歴・連絡先の名前・番号・ブラウザのタブは写さない）。
> Rakuten Link は Chrome と同じ作りの窓（class `Chrome_WidgetWin_1`）で、部品は UI Automation で読める。

## いつもの窓「Rakuten Link」
| 部品 | 見分け方 | 使い道 |
|---|---|---|
| 「通話」のタブ | Button・AutomationId `calls_tab_btn` | 通話の画面へ |
| 「メッセージ」のタブ | Button・名前「メッセージ」（Id なし） | ショートメール（R-9） |
| 「連絡先」のタブ | Button・Id `contacts_tab_btn` | 使わない |
| ダイヤルパッド | Button・Id `dialpad_btn` | 番号を入れる画面を出す（⏳ 出たあとの番号の欄・発信のボタンは、次の読み取りで） |
| 通話を検索 | Edit・Id `input-search-bar` | 使わない（番号を入れる所ではない） |
| 最近・留守番電話 | ListItem・Id `recent_calls_tab_btn`・`voicemail_tab_btn` | 使わない |
| スマホとの連携の表示 | ⏳ いつもの画面には出ていない（連携が切れたときに出る見込み。切った状態で読む） | 2-6 |

## 通話中に出る別の窓「Rakuten Link Call」
| 部品 | 見分け方 | 使い道 |
|---|---|---|
| 窓そのもの | 窓の名前「Rakuten Link Call」 | **この窓があるあいだは通話中（呼び出し中を含む）**＝`in_call` の材料 |
| 通話の時間 | Document Id `call-ui` の中の Text（例「00:02」） | ⏳ 呼び出し中から数えるか、つながってから数えるかを確かめる（R-6） |
| 通話を終了 | Button・名前「通話を終了」 | `hang_up` |
| ミュート・音声設定・メッセージ・ダイヤルパッド | Button・それぞれの名前 | 使わない（音声設定は、音の出入りの確かめに使えるかも） |

## 次に読むもの
- ダイヤルパッドを押したあとの、番号を入れる欄と発信のボタン（`probe_screen.py dialpad`）。
- スマホとの連携が切れているときの表示（スマホの Wi-Fi を切って読む）。
- 呼び出し中とつながったあとで、通話の時間の表示が変わるか（R-6）。
