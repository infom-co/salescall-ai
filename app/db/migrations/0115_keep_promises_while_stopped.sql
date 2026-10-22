-- 0115 止めているあいだの、お客様との約束を果たす電話・本番の品質の数では止めない（2026-10-22 ユーザー決定）。書いた人：CL-2
--
-- (1) お客様との約束を果たす電話の種類を、データベースの1か所（salescall.keeping_kind）で決める：お詫びの電話（apology）・途中で
--     切れた通話のお詫びのかけ直し（resume）・約束したかけ直し（promised）。それ以外（営業として新しくかける電話）は NULL。
--     「新しくかける電話か」（salescall.is_new_call。「かけない」の例外・業務ごとの1日の上限が読む）は、これの裏返しにする（同じ決まりを
--     2か所に書かない）。門と候補を出す処理は、停止の原因ごとに「この停止でも通す種類」（core/stops.py の CAUSES）とこれを突き合わせる。
--     ユーザー（2026-10-22）：費用の理由（赤字）で止めたときは、約束の電話・途中で切れたお客様へのお詫びのかけ直し・お詫びの電話の
--     3つだけはかける。緊急停止・回線や会話AIの異常・品質の理由で止めたときは、かけない（品質の停止は、前から、お詫びの電話だけ通す）。
-- (2) 本番（検証モードでない業務）の品質の数（quality.production_same_cause_per_day）は、業務を止めない。担当への修正依頼と、管理者への
--     知らせだけ（ユーザー（2026-10-22）：「本番は止めない（担当への修正依頼と管理者への知らせ）ただし、大問題は止める」。大問題＝
--     このまま進めるとお客様に害が出るとき＝緊急停止・回線や会話AIの異常で止める仕組み。原則6）。設定の意味を直す。

CREATE FUNCTION salescall.keeping_kind(p_purpose text, p_promised boolean) RETURNS text LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN p_purpose = 'apology' THEN 'apology'
               WHEN p_purpose = 'resume' THEN 'resume'
               WHEN coalesce(p_promised, false) THEN 'promised' END $$;
GRANT EXECUTE ON FUNCTION salescall.keeping_kind(text, boolean) TO salescall_app;

CREATE OR REPLACE FUNCTION salescall.is_new_call(p_purpose text, p_promised boolean) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT salescall.keeping_kind(p_purpose, p_promised) IS NULL $$;

ALTER TABLE salescall.setting_definitions DISABLE TRIGGER setting_definitions_frozen;
UPDATE salescall.setting_definitions SET meaning =
  '本番（検証モードでない業務）で、その日のうちに同じ原因（言ってはいけないこと・判定できないの原因ごと）がこの件数になったら、'
  '担当へ修正依頼を出し、管理者へ知らせる（業務は止めない。2026-10-22 ユーザー：「本番は止めない」）'
WHERE key = 'quality.production_same_cause_per_day';
ALTER TABLE salescall.setting_definitions ENABLE TRIGGER setting_definitions_frozen;
ALTER TABLE salescall.setting_values DISABLE TRIGGER setting_values_frozen;
UPDATE salescall.setting_values SET rationale = rationale
  || '（2026-10-22 ユーザー：「前にも言ったはずで本番は止めない（担当への修正依頼と管理者への知らせ）ただし、大問題は止める」⇒ この件数で'
  || 'は止めず、管理者へ知らせる）'
WHERE key = 'quality.production_same_cause_per_day' AND scope = 'system';
ALTER TABLE salescall.setting_values ENABLE TRIGGER setting_values_frozen;
