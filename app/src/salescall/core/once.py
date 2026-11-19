"""画面の操作の1回きりの札（2026-11-19。CL-3 の連絡帳 4-1 の 2・DASHBOARD.md 8-1「同じ札で2回送ると、2回目は何も変わらない」。表の作り 0247）。

画面を開いたときに `issue` で札を出し、送られた操作を行う取引の中で、まず `use` を呼ぶ。偽が返れば、その札はもう使った（二度押し・同時の2回）
ので、何も変えずに終える。操作が断られて取引が戻れば、札も使っていないまま（もう一度送れる）。出せるのも使えるのも、管理者か契約者
（いま操作している方）だけで、出した方が、出した操作の名前で、出した取引の契約者の中でだけ使える（データベースが守る＝action_token_guard）。
"""

from __future__ import annotations

from uuid import UUID

from salescall.core.access import Tx


def issue(tx: Tx, action: str) -> UUID:
    """操作（action＝画面が決める操作の名前）の札を出す（書くのはここだけ）。"""
    return tx.one("INSERT INTO salescall.action_tokens (tenant_id, action, issued_by)"
                  " VALUES (salescall.cur_tenant(), %s, salescall.cur_actor()) RETURNING token", (action,))["token"]


def use(tx: Tx, token: UUID, action: str) -> bool:
    """札を使う（操作と同じ取引で、操作より先に呼ぶ。書くのはここだけ）。初めて使えば真、もう使っていれば偽（何も書かない）。
    出した方・操作の名前・契約者が違う札、無い札は、データベースが断る（Rejected）。同時に2回送られても、真になるのは1つだけ（札ごとに
    1つの一意の約束。後の取引は、先の取引が確定するのを待ってから偽になる）。"""
    return tx.one("INSERT INTO salescall.action_token_uses (token, tenant_id, action, used_by)"
                  " VALUES (%s, salescall.cur_tenant(), %s, salescall.cur_actor())"
                  " ON CONFLICT (token) DO NOTHING RETURNING token", (token, action)) is not None
