-- 0140 まだ分からない続きを決めにいくまでの長さ（line.decide_horizon）の根拠の文を、いまの作りに合わせる
--      （見直し20b の問題7。書いた人：CL-2。DESIGN.md 7-2-1）
--
-- 0136 で書いた根拠「テスト番号の間（30分）と同じ」は、10/25 にテスト番号をあける長さが 15→30→60→120→240分（line.test_ring_gaps。
-- 0139）になってから正しくない。この長さは、テスト番号の間とは別のもの＝「まもなくかける行の電話で決まるのを待つ長さ」。値（30分）は
-- 変えない（仮の値のまま。管理者に伺う）。設定の記録は凍らせてあるので、0139 と同じく、この移行の中だけ仕掛けを外して根拠の文だけを直す。
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
UPDATE salescall.setting_values
SET rationale = 'まもなくかける行の電話で決まるのを待つ長さ（テスト番号をあける長さ line.test_ring_gaps とは別の値）。この長さのうちに'
                'その回線でかける行があれば、テスト番号を鳴らすより、その電話の結果で決まるのを待つほうが、契約者の電話を鳴らさずに済む',
    review_when = 'ユーザーに伺う（見直し19・20）'
WHERE key = 'line.decide_horizon' AND scope = 'system' AND set_by = 'CL-2' AND status = 'provisional';
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
