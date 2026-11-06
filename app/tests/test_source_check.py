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


def docx_parts(body: str, **parts: str) -> bytes:
    """試験用の Word の文書（本文の XML と、ほかの部品＝名前→中身の XML。部品の名前の「_」は「/」に戻す）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_W}" xmlns:mc="{_MC}">'
                                        f"<w:body>{body}</w:body></w:document>")
        for name, xml in parts.items():
            z.writestr(name.replace("_", "/") + ".xml", f'<?xml version="1.0" encoding="UTF-8"?><w:root xmlns:w="{_W}">{xml}</w:root>')
    return buf.getvalue()


_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def locked_pdf(*, user: str) -> bytes:
    """試験用の鍵のかかった PDF（user＝開く鍵。空なら権限の鍵だけ＝どのソフトでも鍵なしで開ける）。"""
    import pypdf
    w = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(pdf("Opening hours 9 to 18"))))
    w.encrypt(user_password=user, owner_password="owner-only", algorithm="AES-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class ReadDocumentParts(unittest.TestCase):
    """見直し(B) の7：読めない所は黙って抜け落ちない・壊れたファイルは処理を落とさず理由つきで断る。"""

    def test_true_false_cells_keep_their_words(self):
        """Excel の TRUE・FALSE の欄は、その文字のまま資料の文字にする（前は空になり、値だけが消えた）。"""
        x = read_document("業務.xlsx", xlsx([("土日の対応", True), ("祝日の対応", False)]))
        self.assertEqual(x.text, "【一覧】\n土日の対応\tTRUE\n祝日の対応\tFALSE")

    def test_headers_footers_notes_and_comments_are_read_and_text_boxes_once(self):
        """Word のヘッダー・フッター・脚注・文末脚注・コメントも読む（見出しつき）。テキストボックスの段落は、外の段落に混ぜず、その段落の
        あとに1回だけ（互換の写し mc:Fallback は読まない）。部品が壊れていれば、ほかは読んで理由を残す。"""
        box = ("<w:p><w:r><w:t>前の文</w:t></w:r><mc:AlternateContent><mc:Choice><w:r><w:txbxContent><w:p><w:r><w:t>枠の中の文"
               "</w:t></w:r></w:p></w:txbxContent></w:r></mc:Choice><mc:Fallback><w:r><w:txbxContent><w:p><w:r><w:t>枠の中の文"
               "</w:t></w:r></w:p></w:txbxContent></w:r></mc:Fallback></mc:AlternateContent><w:r><w:t>後の文</w:t></w:r></w:p>")
        d = read_document("台本.docx", docx_parts(
            box, word_header1="<w:p><w:r><w:t>株式会社架空</w:t></w:r></w:p>", word_footer1="<w:p><w:r><w:t>社外秘</w:t></w:r></w:p>",
            word_footnotes="<w:footnote><w:p><w:r><w:t>料金は税込み</w:t></w:r></w:p></w:footnote>",
            word_comments="<w:comment><w:p><w:r><w:t>要確認</w:t></w:r></w:p></w:comment>", word_endnotes="<w:p>壊れた"))
        self.assertEqual(d.text, "前の文後の文\n枠の中の文\n【ヘッダー】\n株式会社架空\n【フッター】\n社外秘\n【脚注】\n料金は税込み"
                                 "\n【コメント】\n要確認")
        self.assertEqual(len(d.problems), 1)
        self.assertIn("文末脚注", d.problems[0])

    def test_a_pdf_locked_only_for_changes_is_read_and_one_locked_to_open_is_refused(self):
        """権限の鍵だけの PDF（鍵なしで開ける）は読む。開く鍵の要る PDF は断る。"""
        self.assertEqual(read_document("概要.pdf", locked_pdf(user="")).text.strip(), "Opening hours 9 to 18")
        with self.assertRaises(FileRejected) as e:
            read_document("概要.pdf", locked_pdf(user="secret"))
        self.assertIn("鍵のかかった PDF", str(e.exception))

    def test_broken_files_are_refused_with_a_reason_instead_of_failing(self):
        """壊れたファイル（中身の途中が壊れた PDF・Word・Excel）は、処理を落とさずに理由をつけて断る（お客様リストの表の取込も同じ）。"""
        from salescall.core.import_files import read
        good = {"概要.pdf": pdf("Opening hours 9 to 18"), "台本.docx": docx(["会社概要"]), "業務.xlsx": xlsx([("a", "b")])}
        for name, data in good.items():
            for cut in range(40, len(data), max(1, len(data) // 25)):
                broken = data[:cut] + bytes(b ^ 0x5A for b in data[cut:cut + 8]) + data[cut + 8:]
                with self.subTest(name=name, cut=cut):
                    try:
                        read_document(name, broken)
                    except FileRejected:
                        pass
                    if name.endswith(".xlsx"):
                        try:
                            read(name, broken)
                        except FileRejected:
                            pass

    def test_a_character_set_is_chosen_only_for_text_and_csv(self):
        """文字の決まりを選べるのは文章と CSV だけ（ほかの種類に渡せば断る）。"""
        with self.assertRaises(FileRejected):
            read_document("業務.xlsx", xlsx([("a", "b")]), encoding="utf-8")
        self.assertEqual(read_document("台本.csv", "あい".encode("utf-8"), encoding="utf-8").text, "あい")


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

    def test_marks_that_look_the_same_are_matched(self):
        """見た目の同じ記号（波ダッシュ・全角チルダ・チルダ／ハイフン・ダッシュ・マイナス）は同じ文字として照らす。長音「ー」はそろえない
        （見直し(B) の6：資料の「9:00〜18:00」を AI が「～」で写すと見つからなかった）。"""
        docs = (Document("概要.txt", "text", "z", "営業時間 9:00〜18:00\n電話 03−1234—5678\nメール"),)
        for quote in ("9:00～18:00", "9:00~18:00", "03-1234-5678"):
            with self.subTest(quote=quote):
                self.assertEqual(source_check.locate(docs, quote), "概要.txt")
        self.assertIsNone(source_check.locate(docs, "メ-ル"))

    def test_items_without_a_found_quote_are_not_confirmed(self):
        """項目ごとに照らす：出典の文が無い・見つからない項目は確かめられない（理由つき。呼ぶ側は値を入れずに決まらなかった所にする）。"""
        got = source_check.check(self.DOCS, [("名乗り", "山田", "株式会社架空の 山田と申します。"),
                                             ("受付時間", "9時〜18時", "受付時間は9時から18時"),
                                             ("最終目的", "ご予約", None)])
        self.assertEqual([(c.key, c.confirmed, c.found) for c in got],
                         [("名乗り", True, "台本.docx"), ("受付時間", False, None), ("最終目的", False, None)])
        self.assertIsNone(got[0].why)
        self.assertIn("見つかりません", got[1].why)
        self.assertNotIn("出典の文がありません", got[1].why)
        self.assertIn("出典の文がありません", got[2].why)
        self.assertNotIn("見つかりません", got[2].why)
