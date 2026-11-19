-- 0250 提出用の控えは、依頼者ごとに同じ中身を1つだけ置く（2026-11-19。書いた人：CL-2。DESIGN.md 3-10・15-2 の887。CL-3 の連絡帳 4-1c の 31）
-- ⛔ 前は、画面（output/files.submit）が「同じ中身（sha256）がもう控えにあるか」を確かめてから置いていたので、二度押しが同時に届くと両方置けた
-- （確かめと書き込みのすき間）。⇒ データベースの一意の約束（依頼者・中身の指紋）で守り、置くのは core/backup.record_submission の1か所
-- （同じ中身なら、前に置いた控えの番号を返し、何も書かない）。写したあと（中身を消したあと）も指紋は残るので、同じ中身は置き直さない。
CREATE UNIQUE INDEX submission_files_same_content ON salescall.submission_files (client_id, sha256);
