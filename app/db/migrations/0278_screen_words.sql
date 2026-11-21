-- 0278 契約者の画面に出る設定の説明（help）の呼び方をそろえる（2026-11-21。書いた人：CL-2。DESIGN_15-2.md の943。CL-3 の連絡帳のお願い 64・65）
--
-- ✅ 2026-11-20 管理者「イ」（お願い 64）：画面では「実施回」を使わず「お客様リスト」と呼ぶ。✅ 2026-11-21 管理者「伺い1：ア」（お願い 65）：お客様リストの
-- 中の1件は「お客様」と呼ぶ（「行」は取り込むファイル・渡すファイルの中の1行だけ）。プログラムの中の名前と計画書・設計書の言葉はそのまま。
-- ⇒ 説明（0268 で入れた help）の言い方だけを直す。直す言い方ごとに、その説明にその言い方があることを確かめてから直す（無ければ止める＝黙って直し漏らさない）。
ALTER TABLE salescall.setting_definitions DISABLE TRIGGER USER;
DO $$
DECLARE r record; n int;
BEGIN
  FOR r IN SELECT * FROM (VALUES
    ('approval.human', 'かける前に契約者が行を承認し', 'かける前に契約者がお客様ごとに承認し'),
    ('business.call_declined_again', '（新しい実施回）', '（新しいお客様リスト）'),
    ('business.declined_wait_days', '次の実施回で前の電話から', '次のお客様リストで前の電話から'),
    ('business.declined_wait_days', 'その実施回では', 'そのお客様リストでは'),
    ('business.declined_wait_days', '次の実施回でもかけません', '次のお客様リストでもかけません'),
    ('callback.month_ahead', '（行は終えません）', '（そのお客様への電話は終えません）'),
    ('calling.call_after_refusal', 'ほかの実施回でかけるか', 'ほかのお客様リストでかけるか'),
    ('dial.failed_retry_days', '行の回数には数えません', 'お客様にかけてよい回数には数えません'),
    ('dnc.former_refusal_calls_new_person', 'で直した行で', 'で直したお客様で'),
    ('gate.max_dials_per_contact_per_day', '（実施回をまたいでも数えます）', '（お客様リストをまたいでも数えます）'),
    ('gate.same_number_gap', 'ほかの実施回や', 'ほかのお客様リストや'),
    ('retry.ask_number_again_max', '（行の回数とは別に数え', '（お客様にかけてよい回数とは別に数え'),
    ('retry.max_attempts_per_task', '1つの行にかけてよい回数の上限です', '同じお客様にかけてよい回数の上限です'),
    ('retry.no_opening_recheck', '見つからない行（', '見つからないお客様（'),
    ('stop.trouble.stop_after_rows', '同じ理由で違う行に', '同じ理由で違うお客様に'),
    ('stop.trouble.stop_after_rows', 'すべての行にかかわる', 'すべてのお客様にかかわる'),
    ('stop.trouble.stop_after_rows', '同じ行で2回続いたときは、その行だけを休ませます', '同じお客様で2回続いたときは、そのお客様だけを休ませます')
  ) AS v(key, old, new) LOOP
    UPDATE salescall.setting_definitions SET help = replace(help, r.old, r.new) WHERE key = r.key AND strpos(help, r.old) > 0;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n <> 1 THEN
      RAISE EXCEPTION '設定 % の説明に「%」がありません（直し漏れ・説明が変わった）', r.key, r.old;
    END IF;
  END LOOP;
END $$;
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER USER;
