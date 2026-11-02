-- 0171 2026-11-03 どの依頼者にも同じ一言（共通の一言＝単位 contractor。recordings.CONTRACTOR_PARTS）の録音を、システムの持ち物にする
-- （設計書 3-8-3 の作る順の (1)・15-2 の456。書いた人：CL-2）。
--
-- ✅ 2026-11-02 管理者：「システムで共通する名前や対応などは、契約者や依頼者ごとではなくて、システムで共通で録音を持っておくということ
--   になります。業務で持つのは明らかにその業務特有のトークスクリプトや問答だけになります。」
-- ⛔ 前の形：共通の一言を契約者ごとに録音し、契約者が増えるたびに同じ録音を作り直して聞き直すことになっていた（6-13 の 2026-10-15 の
--   お言葉「契約者単位でシステム的に録音」をもとにした形）。
--
-- (1) 共通の一言の組は、システムの行（契約者の無い行）だけ。作れて承認できるのは管理者だけ（システムの取引で）。すべての契約者が読む。
--     守りは、名前の録音をシステムの持ち物にしたときの1か所（0134 の system_recording_check・行ごとの決まり system_names_*）をそのまま
--     使う（同じ決まりを2つ作らない。system_recording_check は、知らせる文だけ共通の一言も含む形に直す）。依頼者の単位（business）と
--     お詫び（apology）は、これまでどおり契約者の行だけ（下の recording_sets_system_units）。
--     単位の名前 contractor は、録音の道具と会話の側の比べる試験（CL-1）が読んでいるので、いまは変えない（共通の一言＝common への
--     名前の替えは、CL-1 と合わせて1回で行う＝CL-2 の連絡帳 4-194）。
-- (2) 前に作った契約者の行の共通の一言の組は、もう読まない（話してよい声 recordings.ready_voices は、システムの組だけを見る）。消さない
--     （変えられない表）。新しく作ることはできない（下の守りは NOT VALID＝前の行は確かめず、新しい行だけ確かめる）。

-- 契約者の無い組は、共通の一言と名前の録音の台帳だけ（業務特有の録音・お詫びの電話の文は、その依頼者の持ち物）
ALTER TABLE salescall.recording_sets DROP CONSTRAINT recording_sets_system_names;
ALTER TABLE salescall.recording_sets ADD CONSTRAINT recording_sets_system_units
  CHECK (tenant_id IS NOT NULL OR unit IN ('contractor', 'name_role', 'name', 'name_tail'));
-- 共通の一言の組は、システムの行だけ（名前の録音の台帳は、契約者の行もシステムの行もある＝0134。ここは (3) で替える）
ALTER TABLE salescall.recording_sets ADD CONSTRAINT recording_sets_common_is_system
  CHECK (unit <> 'contractor' OR tenant_id IS NULL) NOT VALID;

-- システムの組・部分・承認を書けるのは、管理者だけ（1か所。0134 の文を、名前の録音だけでなくシステムの録音すべてに広げる）
CREATE OR REPLACE FUNCTION salescall.system_recording_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.tenant_id IS NULL AND salescall.cur_actor_kind() IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'システムの録音（共通の一言・名前の録音）を作れて承認できるのは、管理者だけです（システムの取引で）'
      USING ERRCODE = 'insufficient_privilege', CONSTRAINT = 'system_recording_by_admin';
  END IF;
  RETURN NEW;
END $$;
