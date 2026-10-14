-- 0001 データの土台（DESIGN.md 3章・3-0・3-1・4-1・4-2・10章・11章）
-- 書いた人：CL-2。⚠️ 一度流した表の作りは書き換えない（変えるときは 0002 以降を足す。流した中身の指紋を、流す処理が確かめる）。
--
-- 作りで守ること（どれも、処理に誤りがあってもデータベースが拒む）
--   (1) 依頼主の取り違え：依頼主のデータの表は契約者ID・依頼主IDを持ち、親へは「組の参照」で張る（3-0 の1）。
--   (2) 状態の正本は1つ：進行中＝試行の状態だけ／業務の状態＝結果の版の最新／次の予定＝次の動きの記録の最新（3-0 の2）。
--   (3) 変えてはいけない記録は、書き換え・消しを拒む（3-0 の3）。
--   (4) 行ごとに読める人を制限する（RLS）。取引で渡された契約者・依頼主の行しか、読めない・書けない（11章）。

CREATE SCHEMA salescall;
REVOKE ALL ON SCHEMA salescall FROM PUBLIC;
GRANT USAGE ON SCHEMA salescall TO salescall_app;

-- ── 取引ごとに、読み書きの層が渡す「いま誰が・どの契約者・どの依頼主か」（11章）。渡されていなければ NULL ⇒ どの行も見えない。
CREATE FUNCTION salescall.cur_tenant() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('salescall.tenant_id', true), '')::uuid $$;
CREATE FUNCTION salescall.cur_client() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('salescall.client_id', true), '')::uuid $$;
CREATE FUNCTION salescall.cur_actor() RETURNS text LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('salescall.actor', true), '') $$;
CREATE FUNCTION salescall.cur_actor_kind() RETURNS text LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('salescall.actor_kind', true), '') $$;

-- ── 変えてはいけない記録の守り（3-0 の3）
CREATE FUNCTION salescall.forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '変えてはいけない記録です（%）。書き換え・消しはできません', TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END $$;

-- ── 値の一覧（名前の正本は DESIGN.md 4章。ここはその写しを、データベースが守る形にしたもの）
CREATE TYPE salescall.attempt_state AS ENUM (
  'claimed', 'dialing', 'ringing', 'answered', 'in_conversation', 'ending',          -- 進行中（4-2）
  'ended', 'released', 'call_failed', 'no_answer', 'busy', 'dropped', 'technical_error'  -- 終わり
);
CREATE TYPE salescall.verification_state AS ENUM ('not_needed', 'pending', 'done', 'failed');  -- 通話後の検証（4-2）
CREATE TYPE salescall.hangup_side AS ENUM ('callee', 'line', 'unknown');  -- dropped の切れ方（4-2）
CREATE TYPE salescall.business_state AS ENUM (                             -- 業務の状態（4-1）
  'open', 'completed', 'callback_scheduled', 'incomplete', 'declined',
  'do_not_call', 'out_of_scope', 'not_reached', 'concluded', 'unknown'
  -- not_reached＝目的の人に繋がらなかった／concluded＝業務設定で決めた結論で終えた（7-1。2026-10-14）。
  -- needs_human（人の対応が要る）は持たない（2026-10-13 ユーザー決定：人は仲介しない）
);
CREATE TYPE salescall.result_kind AS ENUM ('judged', 'human', 'rejudged');  -- 結果の版の出どころ（4-1）
CREATE TYPE salescall.next_kind AS ENUM ('call', 'end');                     -- 次の動き（7-2）
CREATE TYPE salescall.item_state AS ENUM (                                 -- 項目の状態（4-4。持ち主 CL-1）
  'empty', 'heard', 'unclear', 'checking', 'confirmed', 'accepted', 'unresolved', 'refused'
);
CREATE TYPE salescall.dnc_reason AS ENUM (                                 -- 発信禁止の登録の理由（Q3）
  'refused_in_call', 'refused_found_after', 'wrong_party', 'manual', 'client_list'
);
CREATE TYPE salescall.stop_scope AS ENUM ('system', 'tenant', 'client');    -- 停止の範囲（Q13）
CREATE TYPE salescall.stop_action AS ENUM ('stop', 'resume');
CREATE TYPE salescall.actor_kind AS ENUM ('operator', 'system');            -- 誰が（人か、システムの処理か）
CREATE TYPE salescall.delivery_state AS ENUM ('pending', 'sending', 'sent', 'failed', 'retrying', 'dead');  -- 連携（4-5）

-- ═════════ 契約者・依頼主・回線 ═════════
CREATE TABLE salescall.tenants (            -- 契約者（このシステムを使う人・会社。Intent.md 11-2）
  tenant_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name       text NOT NULL CHECK (btrim(name) <> ''),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE salescall.clients (            -- 依頼主
  tenant_id  uuid NOT NULL REFERENCES salescall.tenants,
  client_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name       text NOT NULL CHECK (btrim(name) <> ''),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, client_id)
);

CREATE TABLE salescall.lines (              -- 回線（契約者ごと。見守り役が期限つきで借りる。4-2）
  tenant_id        uuid NOT NULL REFERENCES salescall.tenants,
  line_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name             text NOT NULL CHECK (btrim(name) <> ''),
  method           text NOT NULL CHECK (btrim(method) <> ''),   -- 接続方式（例：rakuten_link）
  lease_holder     text CHECK (lease_holder IS NULL OR btrim(lease_holder) <> ''),
  lease_expires_at timestamptz,
  CHECK ((lease_holder IS NULL) = (lease_expires_at IS NULL)),
  UNIQUE (tenant_id, line_id)
);

-- ═════════ 業務と業務設定の版 ═════════
CREATE TABLE salescall.businesses (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  business_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL CHECK (btrim(name) <> ''),
  -- 対象の区分。個人を選べるのは Intent.md 13章の確かめが済んでから（3-1）。それまでは法人だけをデータベースが許す。
  target_kind text NOT NULL CHECK (target_kind = 'corporate'),
  created_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  UNIQUE (client_id, business_id)
);

CREATE TABLE salescall.business_versions (  -- 業務設定の版（変更不可。3-2）。中身の形は引き継ぎの (10) で足す
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  business_id uuid NOT NULL,
  version_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version_no  integer NOT NULL CHECK (version_no >= 1),
  created_at  timestamptz NOT NULL DEFAULT now(),
  created_by  text NOT NULL CHECK (btrim(created_by) <> ''),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, business_id) REFERENCES salescall.businesses (client_id, business_id),
  UNIQUE (business_id, version_no),
  UNIQUE (client_id, version_id),
  UNIQUE (client_id, business_id, version_id)
);

-- 型の一覧（Q19。持ち主 CL-1）。名前は CL-1 の試作（phase0/flowctl.py の CONFIRM_TYPES・CHOICE_TYPES）と同じ。
-- readback＝聞いたその場で言い直して確かめるか（4-4）。⏳「自由な意見」の型の名前は CL-1 に決めてもらう（連絡帳）。
CREATE TABLE salescall.field_types (
  type_name text PRIMARY KEY CHECK (type_name ~ '^[a-z_]+$'),
  readback  boolean NOT NULL
);
INSERT INTO salescall.field_types (type_name, readback) VALUES
  ('date', true), ('time', true), ('datetime', true), ('number', true), ('amount', true),
  ('name', true), ('phone', true), ('email', true), ('address', true),
  ('yes_no', false), ('choice', false);

CREATE TABLE salescall.questions (          -- 質問（業務設定の版の一部。変更不可）
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  version_id  uuid NOT NULL,
  question_id text NOT NULL CHECK (question_id ~ '^[A-Za-z0-9_-]{1,32}$'),
  position    integer NOT NULL CHECK (position >= 1),
  type_name   text NOT NULL REFERENCES salescall.field_types,  -- 一覧に無い型は作れない（I-16）
  required    boolean NOT NULL,
  PRIMARY KEY (version_id, question_id),
  UNIQUE (version_id, position),
  UNIQUE (client_id, version_id, question_id),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id)
);

-- ═════════ 実施回・連絡先・タスク ═════════
CREATE TABLE salescall.runs (               -- 実施回（取込単位。3-2・3-3）
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  business_id uuid NOT NULL,
  run_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL CHECK (btrim(name) <> ''),
  created_at  timestamptz NOT NULL DEFAULT now(),
  created_by  text NOT NULL CHECK (btrim(created_by) <> ''),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, business_id) REFERENCES salescall.businesses (client_id, business_id),
  UNIQUE (client_id, run_id),
  UNIQUE (client_id, run_id, business_id)
);

CREATE TABLE salescall.run_versions (       -- 版の割り当ての記録（追記だけ。最新が、いま使う版。3-2）
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  run_id      uuid NOT NULL,
  business_id uuid NOT NULL,
  seq         integer NOT NULL CHECK (seq >= 1),
  version_id  uuid NOT NULL,
  assigned_at timestamptz NOT NULL DEFAULT now(),
  assigned_by text NOT NULL CHECK (btrim(assigned_by) <> ''),
  reason      text NOT NULL CHECK (btrim(reason) <> ''),
  PRIMARY KEY (run_id, seq),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, run_id, business_id) REFERENCES salescall.runs (client_id, run_id, business_id),
  -- 割り当てられるのは、その実施回の業務の版だけ
  FOREIGN KEY (client_id, business_id, version_id) REFERENCES salescall.business_versions (client_id, business_id, version_id)
);

CREATE TABLE salescall.contacts (           -- 連絡先（正規化した番号。依頼主ごとに1つ。3-1）
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  contact_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  phone_e164 text NOT NULL CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),  -- 正規化できた番号だけ（Q1）
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  UNIQUE (client_id, phone_e164),
  UNIQUE (client_id, contact_id),
  UNIQUE (client_id, contact_id, phone_e164)
);

CREATE TABLE salescall.tasks (              -- タスク（実施回 × 連絡先。状態の欄は持たない＝3-0 の2）
  tenant_id    uuid NOT NULL,
  client_id    uuid NOT NULL,
  task_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id       uuid NOT NULL,
  contact_id   uuid NOT NULL,
  -- 依頼主のファイルのまま（3-4。依頼主が結果を自分の表と突き合わせる鍵）。NULL＝依頼主が付けていない（空文字で代用しない）
  client_ref   text CHECK (client_ref IS NULL OR btrim(client_ref) <> ''),
  source_name  text CHECK (source_name IS NULL OR btrim(source_name) <> ''),
  source_phone text CHECK (source_phone IS NULL OR btrim(source_phone) <> ''),
  source_row   integer CHECK (source_row IS NULL OR source_row >= 1),
  created_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, run_id) REFERENCES salescall.runs (client_id, run_id),
  FOREIGN KEY (client_id, contact_id) REFERENCES salescall.contacts (client_id, contact_id),
  UNIQUE (run_id, contact_id),                 -- 1つの実施回で、同じ連絡先は1タスク（3-1）
  UNIQUE (client_id, task_id),
  UNIQUE (client_id, task_id, contact_id)
);

-- ═════════ 試行（通話の状態。4-2） ═════════
CREATE TABLE salescall.attempt_transitions (  -- 移ってよい組（4-2 の表。これ以外はデータベースが拒む）
  from_state salescall.attempt_state NOT NULL,
  to_state   salescall.attempt_state NOT NULL,
  PRIMARY KEY (from_state, to_state)
);
INSERT INTO salescall.attempt_transitions (from_state, to_state) VALUES
  ('claimed', 'dialing'), ('claimed', 'released'),
  ('dialing', 'ringing'), ('dialing', 'answered'), ('dialing', 'busy'), ('dialing', 'no_answer'),
  ('dialing', 'call_failed'), ('dialing', 'technical_error'),
  ('ringing', 'answered'), ('ringing', 'busy'), ('ringing', 'no_answer'), ('ringing', 'call_failed'),
  ('ringing', 'technical_error'),
  ('answered', 'in_conversation'), ('answered', 'ending'), ('answered', 'dropped'), ('answered', 'technical_error'),
  ('in_conversation', 'ending'), ('in_conversation', 'dropped'), ('in_conversation', 'technical_error'),
  ('ending', 'ended'), ('ending', 'dropped'), ('ending', 'technical_error');

CREATE FUNCTION salescall.is_active(s salescall.attempt_state) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT s IN ('claimed', 'dialing', 'ringing', 'answered', 'in_conversation', 'ending') $$;

CREATE TABLE salescall.attempts (
  tenant_id        uuid NOT NULL,
  client_id        uuid NOT NULL,
  attempt_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id          uuid NOT NULL,
  contact_id       uuid NOT NULL,
  phone_e164       text NOT NULL,     -- 予約のときに連絡先の番号を写す（組の参照で、連絡先の番号と食い違えない）
  line_id          uuid NOT NULL,
  version_id       uuid NOT NULL,     -- 実際に使った業務設定の版（3-2。予約のときの実施回の版でなければ拒む）
  state            salescall.attempt_state NOT NULL,
  claimed_at       timestamptz NOT NULL,   -- データベースの時刻（5-2-1 の1。仕掛けが入れる）
  claim_expires_at timestamptz NOT NULL,
  ended_at         timestamptz,             -- 終わりの状態に入った時刻（仕掛けが入れる）
  hangup_side      salescall.hangup_side,   -- dropped のときだけ。「不明」も値として持つ（既定値で埋めない）。回線の信号のまま
  -- 切れ方の判定（7-1c）：判定（Q5）が1回だけ書く。normal＝ふつうに終えた／refused＝相手が断った・自分の意思で切った／
  -- interrupted＝不自然な中断（相手の意思と関係なく切れた）。相手が出なかった通話・相手が人でなかった通話は NULL のまま
  disconnect       text CHECK (disconnect IS NULL OR disconnect IN ('normal', 'refused', 'interrupted')),
  verification     salescall.verification_state,  -- 終わりの状態に入ったときに仕掛けが決める（4-2）
  CHECK (claim_expires_at > claimed_at),
  CHECK (salescall.is_active(state) = (ended_at IS NULL)),
  CHECK (salescall.is_active(state) = (verification IS NULL)),
  CHECK ((state = 'dropped') = (hangup_side IS NOT NULL)),
  CHECK (disconnect IS NULL OR NOT salescall.is_active(state)),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id, contact_id) REFERENCES salescall.tasks (client_id, task_id, contact_id),
  FOREIGN KEY (client_id, contact_id, phone_e164) REFERENCES salescall.contacts (client_id, contact_id, phone_e164),
  FOREIGN KEY (tenant_id, line_id) REFERENCES salescall.lines (tenant_id, line_id),
  FOREIGN KEY (client_id, version_id) REFERENCES salescall.business_versions (client_id, version_id),
  UNIQUE (client_id, attempt_id),
  UNIQUE (client_id, attempt_id, task_id)
);
-- 進行中は1つだけ（3-1）。番号の制約は、すべての依頼主・契約者の行にまたがって効く（RLS に左右されない）。
CREATE UNIQUE INDEX attempts_one_active_per_contact ON salescall.attempts (contact_id) WHERE salescall.is_active(state);
CREATE UNIQUE INDEX attempts_one_active_per_number  ON salescall.attempts (phone_e164) WHERE salescall.is_active(state);
CREATE UNIQUE INDEX attempts_one_active_per_line    ON salescall.attempts (line_id)    WHERE salescall.is_active(state);
CREATE INDEX attempts_by_contact ON salescall.attempts (contact_id, claimed_at);
CREATE INDEX attempts_by_number ON salescall.attempts (phone_e164, claimed_at);  -- 依頼主をまたいだ間隔（門）
CREATE INDEX attempts_by_line ON salescall.attempts (line_id, claimed_at);       -- 回線ごとの1日の上限（門）

CREATE TABLE salescall.attempt_state_history (  -- 試行の状態の履歴（仕掛けが自動で残す。変更不可）
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  attempt_id uuid NOT NULL,
  seq        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  from_state salescall.attempt_state,           -- 作られたときは NULL
  to_state   salescall.attempt_state NOT NULL,
  at         timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (client_id, attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);

-- その試行は、相手が出た（応答まで進んだ）ことがあるか（状態の履歴から。話したかもしれないかの1か所の決まり）
CREATE FUNCTION salescall.reached_answer(p_attempt uuid) RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT EXISTS (SELECT 1 FROM salescall.attempt_state_history h WHERE h.attempt_id = p_attempt AND h.to_state = 'answered') $$;

-- その実施回の、いま使う版（版の割り当ての記録の最新。3-2）
CREATE FUNCTION salescall.run_current_version(p_run uuid) RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT version_id FROM salescall.run_versions WHERE run_id = p_run ORDER BY seq DESC LIMIT 1 $$;

CREATE FUNCTION salescall.attempt_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_run uuid;
BEGIN
  IF NEW.state <> 'claimed' THEN
    RAISE EXCEPTION '試行は予約（claimed）からしか始まりません' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.ended_at IS NOT NULL OR NEW.verification IS NOT NULL OR NEW.hangup_side IS NOT NULL THEN
    RAISE EXCEPTION '予約の試行は、終わった時刻・検証の状態・切れ方を持てません' USING ERRCODE = 'check_violation';
  END IF;
  NEW.claimed_at := now();   -- 時計はデータベースの時刻1つ（5-2-1 の1。渡された時刻は使わない）
  SELECT run_id INTO v_run FROM salescall.tasks WHERE task_id = NEW.task_id;
  IF salescall.run_current_version(v_run) IS DISTINCT FROM NEW.version_id THEN
    RAISE EXCEPTION '試行の版が、実施回のいまの版と違います' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION salescall.attempt_before_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.tenant_id, NEW.client_id, NEW.attempt_id, NEW.task_id, NEW.contact_id, NEW.phone_e164, NEW.line_id,
      NEW.version_id, NEW.claimed_at, NEW.claim_expires_at)
     IS DISTINCT FROM
     (OLD.tenant_id, OLD.client_id, OLD.attempt_id, OLD.task_id, OLD.contact_id, OLD.phone_e164, OLD.line_id,
      OLD.version_id, OLD.claimed_at, OLD.claim_expires_at) THEN
    RAISE EXCEPTION '試行の中身（相手・回線・版・予約の時刻）は変えられません' USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.state IS DISTINCT FROM OLD.state THEN
    IF NOT EXISTS (SELECT 1 FROM salescall.attempt_transitions WHERE from_state = OLD.state AND to_state = NEW.state) THEN
      RAISE EXCEPTION '試行の状態は % から % へは移れません（4-2）', OLD.state, NEW.state USING ERRCODE = 'check_violation';
    END IF;
    -- 期限切れの予約では発信しない（5-3・I-29）
    IF OLD.state = 'claimed' AND NEW.state = 'dialing' AND OLD.claim_expires_at <= now() THEN
      RAISE EXCEPTION '予約証の期限が切れています' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT salescall.is_active(NEW.state) THEN
      NEW.ended_at := now();
      -- 声が届いていない終わり方は検証が要らない。話していた可能性があるものは「待ち」（4-2）。
      -- 結果不明でも、相手が出る前（応答まで進んでいない）なら、誰とも話していない（2026-10-14）
      NEW.verification := CASE WHEN NEW.state IN ('released', 'call_failed', 'no_answer', 'busy')
                                    OR (NEW.state = 'technical_error' AND NOT salescall.reached_answer(NEW.attempt_id))
                               THEN 'not_needed'::salescall.verification_state
                               ELSE 'pending'::salescall.verification_state END;
    ELSE
      NEW.ended_at := NULL;
    END IF;
    IF NEW.state <> 'dropped' AND NEW.hangup_side IS NOT NULL THEN
      RAISE EXCEPTION '切れ方を持てるのは dropped だけです' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.disconnect IS NOT NULL THEN
      RAISE EXCEPTION '切れ方の判定は、終わったあとに判定が書きます' USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    IF NEW.ended_at IS DISTINCT FROM OLD.ended_at OR NEW.hangup_side IS DISTINCT FROM OLD.hangup_side THEN
      RAISE EXCEPTION '終わった時刻・切れ方は、状態が移るときにしか決まりません' USING ERRCODE = 'check_violation';
    END IF;
    -- 切れ方の判定は、終わった試行に1回だけ（書くのは判定の結果を記録する側＝core/settle.py から attempts.record_disconnect）
    IF NEW.disconnect IS DISTINCT FROM OLD.disconnect AND OLD.disconnect IS NOT NULL THEN
      RAISE EXCEPTION '切れ方の判定は1回だけ決まります' USING ERRCODE = 'check_violation';
    END IF;
    -- 検証の状態は「待ち」から「済み」か「確かめられなかった」へだけ（書くのは通話後の検証 Q21 だけ）
    IF NEW.verification IS DISTINCT FROM OLD.verification
       AND NOT (OLD.verification = 'pending' AND NEW.verification IN ('done', 'failed')) THEN
      RAISE EXCEPTION '通話後の検証の状態は、待ちから済み・確かめられなかったへだけ移れます' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION salescall.attempt_after_write() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' OR NEW.state IS DISTINCT FROM OLD.state THEN
    INSERT INTO salescall.attempt_state_history (tenant_id, client_id, attempt_id, from_state, to_state)
    VALUES (NEW.tenant_id, NEW.client_id, NEW.attempt_id,
            CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.state END, NEW.state);
  END IF;
  RETURN NULL;
END $$;

CREATE TRIGGER attempts_before_insert BEFORE INSERT ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_before_insert();
CREATE TRIGGER attempts_before_update BEFORE UPDATE ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_before_update();
CREATE TRIGGER attempts_after_write AFTER INSERT OR UPDATE ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.attempt_after_write();
CREATE TRIGGER attempts_no_delete BEFORE DELETE ON salescall.attempts
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- ═════════ 追記の順番待ち（結果の版・次の動きの記録に番号を振る前に、ここで順番を取る） ═════════
-- ⚠️ 直列化の水準でも「いまの最大の番号 + 1」を同時に足すと、やり直しにならず一意の違反として返ることがある
--    （2026-10-13 に試験で確かめた）。同じタスクのこの行を書き換えてから番号を振れば、ぶつかったほうは
--    「やり直し」（直列化の失敗）として返り、読み書きの層がやり直す。書くのは core/tasks.py だけ。
CREATE TABLE salescall.task_append_locks (
  tenant_id uuid NOT NULL,
  client_id uuid NOT NULL,
  task_id   uuid PRIMARY KEY,
  bumps     bigint NOT NULL CHECK (bumps >= 1),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);

-- ═════════ 項目の値（4-4。状態の移り方を決めるのは CL-1 の流れの制御。ここは保存の形だけ） ═════════
CREATE TABLE salescall.item_values (
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  task_id     uuid NOT NULL,
  version_id  uuid NOT NULL,
  question_id text NOT NULL,
  state       salescall.item_state NOT NULL,
  value       jsonb,          -- 型ごとに決まった形（CL-3 のお願い 6）。⏳ 形は CL-1 の「会話から返すもの」と合わせて決める
  condition   text CHECK (condition IS NULL OR btrim(condition) <> ''),  -- 選んだ選択肢（「わからない」「答えたくない」を含む）
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, question_id),                -- 1タスクの1項目は値が1つ（3-1）
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id),
  FOREIGN KEY (client_id, version_id, question_id) REFERENCES salescall.questions (client_id, version_id, question_id)
);

-- ═════════ 結果の版・送ってよい印・次の動き（4-1・7-2・10章。どれも追記だけ） ═════════
CREATE TABLE salescall.task_results (
  tenant_id      uuid NOT NULL,
  client_id      uuid NOT NULL,
  task_id        uuid NOT NULL,
  version_no     integer NOT NULL CHECK (version_no >= 1),
  kind           salescall.result_kind NOT NULL,
  business_state salescall.business_state NOT NULL,
  attempt_id     uuid,            -- 判定のもとにした試行（そのタスクの試行に限る）
  judge_version  text,            -- 判定の処理の版（判定した版だけが持つ。人の版は持たない）
  complaint      boolean NOT NULL,  -- 苦情・怒りがあった印（7-1。依頼主への報告と、次の動きの「終える」に使う）。既定値は持たない
  detail         jsonb NOT NULL,    -- 判定の中身（切れ方・目的の人に繋がらなかった理由・折り返し・結論の名前・再発信の方針の鍵）
  snapshot       jsonb NOT NULL,  -- そのときの項目の値と状態の写し
  decided_at     timestamptz NOT NULL DEFAULT now(),
  decided_by     text NOT NULL CHECK (btrim(decided_by) <> ''),
  CHECK ((kind IN ('judged', 'rejudged')) = (judge_version IS NOT NULL AND btrim(judge_version) <> '')),
  PRIMARY KEY (task_id, version_no),
  UNIQUE (client_id, task_id, version_no),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id),
  FOREIGN KEY (client_id, attempt_id, task_id) REFERENCES salescall.attempts (client_id, attempt_id, task_id)
);

CREATE TABLE salescall.result_releases (    -- 送ってよい印（付けるのは承認の部分だけ。10章）
  tenant_id   uuid NOT NULL,
  client_id   uuid NOT NULL,
  task_id     uuid NOT NULL,
  version_no  integer NOT NULL,
  released_at timestamptz NOT NULL DEFAULT now(),
  released_by text NOT NULL CHECK (btrim(released_by) <> ''),
  PRIMARY KEY (task_id, version_no),
  UNIQUE (client_id, task_id, version_no),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id, version_no) REFERENCES salescall.task_results (client_id, task_id, version_no)
);

CREATE TABLE salescall.task_next_actions (  -- 次の動きの記録（書くのは next_action だけ。最新が、いまの予定。4-1・7-2）
  tenant_id  uuid NOT NULL,
  client_id  uuid NOT NULL,
  task_id    uuid NOT NULL,
  seq        integer NOT NULL CHECK (seq >= 1),
  kind       salescall.next_kind NOT NULL,
  call_at    timestamptz,
  end_reason text,
  cause      text NOT NULL CHECK (btrim(cause) <> ''),   -- 何を受けて決めたか（判定・門の不許可・人の操作 など）
  decided_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'call') = (call_at IS NOT NULL)),
  CHECK ((kind = 'end') = (end_reason IS NOT NULL AND btrim(end_reason) <> '')),
  PRIMARY KEY (task_id, seq),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, task_id) REFERENCES salescall.tasks (client_id, task_id)
);

-- ═════════ 送り先と連携（4-5。台帳の中身は CL-3 の 4-5 のとおり） ═════════
CREATE TABLE salescall.destinations (
  tenant_id      uuid NOT NULL,
  client_id      uuid NOT NULL,
  destination_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name           text NOT NULL CHECK (btrim(name) <> ''),
  method         text NOT NULL CHECK (btrim(method) <> ''),
  upsert_capable boolean NOT NULL,        -- 「同じ鍵なら上書き」で受けられるか（4-5）
  credential_ref text CHECK (credential_ref IS NULL OR btrim(credential_ref) <> ''),  -- 認証情報は値でなく参照だけ
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  UNIQUE (client_id, destination_id)
);

CREATE TABLE salescall.deliveries (
  tenant_id      uuid NOT NULL,
  client_id      uuid NOT NULL,
  task_id        uuid NOT NULL,
  destination_id uuid NOT NULL,
  version_no     integer NOT NULL,
  state          salescall.delivery_state NOT NULL,
  send_count     integer NOT NULL CHECK (send_count >= 0),
  last_error     text,
  remote_ref     text,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, destination_id, version_no),   -- 1つの送り先へは、同じ結果を1回だけ（3-1）
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, destination_id) REFERENCES salescall.destinations (client_id, destination_id),
  -- 送れるのは「送ってよい印」の付いた版だけ（10章）。印の無い版の連携は、データベースが作らせない
  FOREIGN KEY (client_id, task_id, version_no) REFERENCES salescall.result_releases (client_id, task_id, version_no)
);

-- ═════════ 発信禁止台帳（Q3）・停止の記録（Q13）・番号の台帳（5-2） ═════════
CREATE TABLE salescall.dnc_entries (        -- 依頼主の発信禁止台帳（消さない。取り消しは人だけが、記録つきで）
  tenant_id         uuid NOT NULL,
  client_id         uuid NOT NULL,
  entry_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  phone_e164        text NOT NULL CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  reason            salescall.dnc_reason NOT NULL,
  source_attempt_id uuid,
  registered_at     timestamptz NOT NULL DEFAULT now(),
  registered_by     text NOT NULL CHECK (btrim(registered_by) <> ''),
  revoked_at        timestamptz,
  revoked_by        text,
  revoke_reason     text,
  CHECK ((revoked_at IS NULL) = (revoked_by IS NULL) AND (revoked_at IS NULL) = (revoke_reason IS NULL)),
  CHECK (revoke_reason IS NULL OR btrim(revoke_reason) <> ''),
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id),
  FOREIGN KEY (client_id, source_attempt_id) REFERENCES salescall.attempts (client_id, attempt_id)
);
CREATE UNIQUE INDEX dnc_one_active ON salescall.dnc_entries (client_id, phone_e164) WHERE revoked_at IS NULL;

CREATE FUNCTION salescall.dnc_before_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '発信禁止の登録は消せません（取り消しは記録つきで）' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.revoked_at IS NOT NULL
     OR (NEW.tenant_id, NEW.client_id, NEW.entry_id, NEW.phone_e164, NEW.reason, NEW.source_attempt_id,
         NEW.registered_at, NEW.registered_by)
        IS DISTINCT FROM
        (OLD.tenant_id, OLD.client_id, OLD.entry_id, OLD.phone_e164, OLD.reason, OLD.source_attempt_id,
         OLD.registered_at, OLD.registered_by) THEN
    RAISE EXCEPTION '発信禁止の登録は、取り消すこと（1回）しかできません' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NEW.revoked_at IS NOT NULL AND salescall.cur_actor_kind() IS DISTINCT FROM 'operator' THEN
    RAISE EXCEPTION '発信禁止の取り消しは、人だけができます' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER dnc_entries_guard BEFORE UPDATE OR DELETE ON salescall.dnc_entries
  FOR EACH ROW EXECUTE FUNCTION salescall.dnc_before_change();

CREATE TABLE salescall.system_dnc_entries ( -- システム共通の発信禁止（依頼主を持たない。消さない）
  phone_e164    text PRIMARY KEY CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  reason        text NOT NULL CHECK (btrim(reason) <> ''),
  registered_at timestamptz NOT NULL DEFAULT now(),
  registered_by text NOT NULL CHECK (btrim(registered_by) <> '')
);

CREATE TABLE salescall.stop_events (        -- 停止の記録（追記だけ。範囲ごとの最新が、いまの状態。Q13）
  event_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scope      salescall.stop_scope NOT NULL,
  tenant_id  uuid REFERENCES salescall.tenants,
  client_id  uuid,
  action     salescall.stop_action NOT NULL,
  at         timestamptz NOT NULL DEFAULT now(),
  actor      text NOT NULL CHECK (btrim(actor) <> ''),
  actor_kind salescall.actor_kind NOT NULL,
  reason     text NOT NULL CHECK (btrim(reason) <> ''),
  CHECK ((scope = 'system' AND tenant_id IS NULL AND client_id IS NULL)
      OR (scope = 'tenant' AND tenant_id IS NOT NULL AND client_id IS NULL)
      OR (scope = 'client' AND tenant_id IS NOT NULL AND client_id IS NOT NULL)),
  CONSTRAINT stop_events_resume_by_operator CHECK (action = 'stop' OR actor_kind = 'operator'),  -- 停止を解くのは人だけ（Intent.md 10-3）
  FOREIGN KEY (tenant_id, client_id) REFERENCES salescall.clients (tenant_id, client_id)
);

-- 番号の台帳は持たない（2026-10-14 見直し。DESIGN.md 15-2 の33）：「その番号へ最後にかけた時刻」は試行の記録から数える
-- （同じことを2か所に持つと食い違う。台帳はどの依頼主がかけたかを持たないので、同じ依頼主の再発信まで止めていた）。

-- 変えてはいけない記録（3-0 の3）
CREATE TRIGGER business_versions_frozen BEFORE UPDATE OR DELETE ON salescall.business_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER questions_frozen BEFORE UPDATE OR DELETE ON salescall.questions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER run_versions_frozen BEFORE UPDATE OR DELETE ON salescall.run_versions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER task_results_frozen BEFORE UPDATE OR DELETE ON salescall.task_results
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER result_releases_frozen BEFORE UPDATE OR DELETE ON salescall.result_releases
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER task_next_actions_frozen BEFORE UPDATE OR DELETE ON salescall.task_next_actions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER attempt_state_history_frozen BEFORE UPDATE OR DELETE ON salescall.attempt_state_history
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER stop_events_frozen BEFORE UPDATE OR DELETE ON salescall.stop_events
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER system_dnc_frozen BEFORE UPDATE OR DELETE ON salescall.system_dnc_entries
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER field_types_frozen BEFORE UPDATE OR DELETE ON salescall.field_types
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();
CREATE TRIGGER attempt_transitions_frozen BEFORE UPDATE OR DELETE ON salescall.attempt_transitions
  FOR EACH ROW EXECUTE FUNCTION salescall.forbid_change();

-- ═════════ 行ごとに読める人を制限する（11章） ═════════
ALTER TABLE salescall.tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.tenants
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());

ALTER TABLE salescall.clients ENABLE ROW LEVEL SECURITY;  -- 契約者の範囲の取引では、その契約者の依頼主の一覧が見える
CREATE POLICY client_list ON salescall.clients
  USING (tenant_id = salescall.cur_tenant()
         AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()))
  WITH CHECK (tenant_id = salescall.cur_tenant()
         AND (salescall.cur_client() IS NULL OR client_id = salescall.cur_client()));

ALTER TABLE salescall.lines ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON salescall.lines
  USING (tenant_id = salescall.cur_tenant()) WITH CHECK (tenant_id = salescall.cur_tenant());

ALTER TABLE salescall.stop_events ENABLE ROW LEVEL SECURITY;  -- 全体の停止は誰でも読める。契約者・依頼主の停止はその契約者だけ
CREATE POLICY stop_scope ON salescall.stop_events
  USING (scope = 'system' OR tenant_id = salescall.cur_tenant())
  WITH CHECK ((scope = 'system' AND salescall.cur_tenant() IS NULL)
              OR (scope = 'tenant' AND tenant_id = salescall.cur_tenant())
              OR (scope = 'client' AND tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client()));

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['businesses', 'business_versions', 'questions', 'runs', 'run_versions', 'contacts', 'tasks',
                           'attempts', 'attempt_state_history', 'item_values', 'task_results', 'result_releases',
                           'task_next_actions', 'destinations', 'deliveries', 'dnc_entries', 'task_append_locks'] LOOP
    EXECUTE format('ALTER TABLE salescall.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY client_scope ON salescall.%I
                      USING (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())
                      WITH CHECK (tenant_id = salescall.cur_tenant() AND client_id = salescall.cur_client())', t);
  END LOOP;
END $$;

-- ═════════ システムの処理の役の権限（消す権限は渡さない。変えてよい表だけ書き換えを許す） ═════════
GRANT SELECT ON ALL TABLES IN SCHEMA salescall TO salescall_app;
GRANT INSERT ON salescall.tenants, salescall.clients, salescall.lines, salescall.businesses, salescall.business_versions,
                salescall.questions, salescall.runs, salescall.run_versions, salescall.contacts, salescall.tasks,
                salescall.attempts, salescall.item_values, salescall.task_results, salescall.result_releases,
                salescall.task_next_actions, salescall.destinations, salescall.deliveries, salescall.dnc_entries,
                salescall.system_dnc_entries, salescall.stop_events,
                salescall.task_append_locks
             TO salescall_app;
-- 試行の状態の履歴は仕掛けだけが書く（システムの処理の役が書く仕掛けなので INSERT は要る。書き換えは仕掛けが拒む）
GRANT INSERT ON salescall.attempt_state_history TO salescall_app;
GRANT UPDATE ON salescall.lines, salescall.attempts, salescall.item_values, salescall.deliveries,
                salescall.dnc_entries, salescall.task_append_locks
             TO salescall_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA salescall TO salescall_app;
