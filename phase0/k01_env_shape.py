"""K-1：.env の「形」だけを見る（キーの値は出さない）。

出すもの：変数名、値の長さ、Google の API キーの形か（先頭が AIza か）、指紋（同じキーかを見分けるため）。
⇒ どの変数を Gemini のキーとして使うかを決める材料にする。
"""
from __future__ import annotations

from common import ApiKey, _LOADED_SECRETS, base_parser, read_env_file, require_python, save_result, say


def main() -> None:
    require_python()
    args = base_parser(__doc__).parse_args()
    pairs = read_env_file(args.env)
    rows = []
    for name, value in pairs:
        _LOADED_SECRETS.append(value)  # 念のため、すべての値を伏せる対象にする
        rows.append(
            {
                "name": name,
                "length": len(value),
                "looks_like_google_api_key": value.startswith("AIza"),
                "fingerprint": ApiKey(name, value).fingerprint if value else "",
            }
        )
    fingerprints = [r["fingerprint"] for r in rows if r["fingerprint"]]
    duplicates = sorted({f for f in fingerprints if fingerprints.count(f) > 1})
    for r in rows:
        say(f"{r['name']:<40} 長さ={r['length']:<4} Googleのキーの形={r['looks_like_google_api_key']} 指紋={r['fingerprint']}")
    if duplicates:
        say(f"同じ値の変数があります（指紋）：{duplicates}")
    save_result("K-1", {"env_path": args.env, "variables": rows, "duplicate_fingerprints": duplicates})


if __name__ == "__main__":
    main()
