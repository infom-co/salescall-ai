-- 0031 設定の値の形に「間隔か、かけない」（gap_or_never）を足す（0032 で使う。型に足した値は、足した取引の中では使えないので、
-- このファイルを分けた）。書いた人：CL-2
ALTER TYPE salescall.setting_value_type ADD VALUE 'gap_or_never';
