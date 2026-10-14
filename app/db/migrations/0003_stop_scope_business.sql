-- 0003 停止の範囲に「業務」と「回線」を足す（2026-10-13 CL-2・回線は 2026-10-14。DESIGN.md 9-1）
-- ⚠️ 値の一覧に足した値は、同じ取引の中では使えない（PostgreSQL の決まり）ので、使う作りは 0004 に分けた。
ALTER TYPE salescall.stop_scope ADD VALUE 'business';
ALTER TYPE salescall.stop_scope ADD VALUE 'line';      -- 回線の異常は、その回線だけを止める（9-1）
