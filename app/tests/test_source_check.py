"""業務の資料を文字に読む1か所（import_files.read_document）と、組む AI の出典の確かめの1か所（source_check）の試験（データベースは使わない）。

✅ 2026-11-05 ユーザー：「案A」。CL-1 の連絡帳 4-92 の (b)：「出典は、プログラムが資料の中にそのまま在るかを確かめてください」。
"""

from __future__ import annotations

import io
import unittest
import zipfile

from salescall.core import source_check
from salescall.core.import_files import Document, EncodingAmbiguous, FileRejected, read_document
from tests.test_import_files import xlsx

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(paragraphs: list[str], table: list[list[str]] = ()) -> bytes:
    """試験用の Word の文書（段落と、表の欄）。"""
    def p(text: str) -> str:
        runs = "".join(f"<w:r><w:t xml:space=\"preserve\">{part}</w:t></w:r>" if i == 0 else
                       f"<w:r><w:br/><w:t xml:space=\"preserve\">{part}</w:t></w:r>" for i, part in enumerate(text.split("\n")))
        return f"<w:p>{runs}</w:p>"
    rows = "".join("<w:tr>" + "".join(f"<w:tc>{p(c)}</w:tc>" for c in row) + "</w:tr>" for row in table)
    body = "".join(p(t) for t in paragraphs) + (f"<w:tbl>{rows}</w:tbl>" if table else "")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def pdf(text: str | None, *, blank_after: bool = False) -> bytes:
    """試験用の PDF（英数字の文字の1ページ。text が None なら文字の無いページ。blank_after なら、うしろに文字の無いページを足す）。"""
    import pypdf
    content = b"" if text is None else f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    if not blank_after:
        return bytes(out)
    w = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(bytes(out))))
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class ReadDocument(unittest.TestCase):
    def test_each_kind_becomes_text(self):
        """文章・CSV・xlsx（すべてのシート）・Word（段落と表）を、資料の文字にする。"""
        t = read_document("台本.txt", "株式会社架空の山田と申します。\n".encode("utf-8"))
        self.assertEqual((t.file_kind, t.text), ("text", "株式会社架空の山田と申します。\n"))
        x = read_document("業務.xlsx", xlsx([("質問", "答え"), ("ご予約の日は？", None)], sheets=("台本", "概要")))
        self.assertEqual((x.file_kind, x.text), ("xlsx", "【台本】\n質問\t答え\nご予約の日は？\n【概要】"))
        f = read_document("計算.xlsx", xlsx([("料金", "=1+1")]))
        self.assertEqual((f.text, f.problems), ("【一覧】\n料金\t", ("一覧 の 1 行目：式の結果が保存されていません（表計算ソフトで開いて保存し直してください）",)))
        p = read_document("概要.pdf", pdf("Opening hours 9 to 18", blank_after=True))
        self.assertEqual((p.file_kind, p.text.strip(), p.problems), ("pdf", "Opening hours 9 to 18", ("2 ページ目に文字がありません（画像だけのページ）",)))
        d = read_document("会社概要.docx", docx(["会社概要", "創業 2001年\n本社 東京"], [["業務", "架空の点検"]]))
        self.assertEqual((d.file_kind, d.text), ("docx", "会社概要\n創業 2001年\n本社 東京\n業務\n架空の点検"))

    def test_unreadable_files_are_refused_with_the_reason(self):
        """読めない種類・壊れたファイルは断る（推し量らない）。文字の決まりが決められない文章は、発信対象の CSV と同じく選んでいただく。"""
        for name, data in (("資料.pdf", b"%PDF-1.4"), ("資料.pdf", pdf(None)), ("資料.doc", b"x"), ("資料.docx", b"not a zip"),
                           ("資料", b"x")):
            with self.subTest(name=name), self.assertRaises(FileRejected):
                read_document(name, data)
        with self.assertRaises(EncodingAmbiguous):
            read_document("台本.txt", "あい".encode("utf-8"))
        self.assertEqual(read_document("台本.txt", "あい".encode("utf-8"), encoding="utf-8").text, "あい")


class SourceCheck(unittest.TestCase):
    DOCS = (Document("台本.docx", "docx", "x", "お電話ありがとうございます。\n株式会社架空の　山田と申します。\nご予約の日は\nいつがよろしいでしょうか？"),
            Document("概要.txt", "text", "y", "受付時間：９時～１８時"))

    def test_a_quote_is_found_only_when_it_is_in_the_documents_as_it_is(self):
        """出典の文は、形（全角・半角）と空白・改行を除けば、一字一句そのまま資料にあるときだけ見つかる。言い換え・一部の違いは見つからない。"""
        found = {q: source_check.locate(self.DOCS, q) for q in (
            "株式会社架空の山田と申します。",                 # 全角の空白を除いて同じ
            "ご予約の日はいつがよろしいでしょうか?",          # 改行をまたぐ・「？」と「?」
            "受付時間:9時~18時",                            # 全角の数字・記号
            "株式会社架空の田中と申します。",                 # 名前が違う
            "ご予約の日はいつがよろしいですか？",             # 言い換え
            "", "　\n", None)}
        self.assertEqual(found, {"株式会社架空の山田と申します。": "台本.docx", "ご予約の日はいつがよろしいでしょうか?": "台本.docx",
                                 "受付時間:9時~18時": "概要.txt", "株式会社架空の田中と申します。": None,
                                 "ご予約の日はいつがよろしいですか？": None, "": None, "　\n": None, None: None})

    def test_items_without_a_found_quote_are_not_confirmed(self):
        """項目ごとに照らす：出典の文が無い・見つからない項目は確かめられない（理由つき。呼ぶ側は値を入れずに決まらなかった所にする）。"""
        got = source_check.check(self.DOCS, [("名乗り", "山田", "株式会社架空の 山田と申します。"),
                                             ("受付時間", "9時〜18時", "受付時間は9時から18時"),
                                             ("最終目的", "ご予約", None)])
        self.assertEqual([(c.key, c.confirmed, c.found) for c in got],
                         [("名乗り", True, "台本.docx"), ("受付時間", False, None), ("最終目的", False, None)])
        self.assertIsNone(got[0].why)
        self.assertIn("見つかりません", got[1].why)
        self.assertIn("出典の文がありません", got[2].why)
