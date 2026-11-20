"""電話の部品（電話をつなぐ部分。持ち主 CL-3＝2026-11-20 管理者のご決定。設計 docs/PHONE.md）。

方式（Rakuten Link・ハンズフリー・電話API）ごとに部品を1つ作り、どれも `salescall.link.phone.Phone` の形にする。
方式によらない道具（声の形を変える `audio`・電話の音を聞き分ける `tones`）は、ここに1つずつ置く。
PC の上で動くので、データベース・サーバーの部品を読まない（機械が見張る＝checks/rules.py の PC_SIDE）。
"""
