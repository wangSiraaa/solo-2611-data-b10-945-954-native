"""领域异常。组织错误与预算错误各自成体系，二者不互相抛出。"""

from __future__ import annotations


class OrgError(Exception):
    """组织编制服务的所有错误基类。"""


class DuplicateDepartment(OrgError):
    """department_id 已存在。"""


class DepartmentNotFound(OrgError):
    """部门不存在。"""


class RootError(OrgError):
    """违反单根约束（如停用根、把根当作普通节点移动）。"""


class CycleError(OrgError):
    """移动会产生环（把节点移入自身子孙）。"""


class NotALeaf(OrgError):
    """操作只允许叶部门（如停用），但该部门仍有孩子。"""


class InactiveDepartment(OrgError):
    """引用了已停用部门，或在已停用节点上继续建孩子。"""


class InvalidTree(OrgError):
    """发布前校验发现树不变量被破坏。"""


class OrganizationVersionNotFound(OrgError):
    """请求的组织产品版本不存在。"""


class BudgetError(Exception):
    """预算执行仓的所有错误基类。"""


class WeightError(BudgetError):
    """权重不是正整数，或直接孩子权重没有补齐/有多余项。"""


class SchemeError(BudgetError):
    """预算方案状态非法（如未绑定组织版本、重复换版到同一版本）。"""


class InactiveLeaf(BudgetError):
    """在已停用叶部门上开立承诺。"""


class InsufficientFunds(BudgetError):
    """可用额不足以开立承诺，或承诺剩余额不足以登记实际支出。"""


class CommitmentNotFound(BudgetError):
    """请求号对应的承诺不存在。"""


class CommitmentClosedError(BudgetError):
    """承诺已取消，不能再登记支出。"""


class IdempotencyConflict(BudgetError):
    """同一幂等键被用不同的请求内容重试。"""


class VoucherError(BudgetError):
    """迁编流转凭单体系的错误基类（预算侧产品，沿用预算错误体系）。"""


class VoucherHandOffError(VoucherError):
    """凭单不能交版：存在缺覆盖 / 多归 / 栏头互不为祖先等问题，逐条列出原单。"""

    def __init__(self, problems: "list[tuple[str, str | None, str]] | tuple") -> None:
        self.problems: tuple[tuple[str, str | None, str], ...] = tuple(problems)
        detail = "；".join(
            f"[{code}] {req or '(-)'}: {msg}" for code, req, msg in self.problems
        )
        super().__init__(f"凭单不能交版，共 {len(self.problems)} 项问题：{detail}")


class VoucherNotFound(VoucherError):
    """封存凭单号不存在。"""


class VoucherConflict(VoucherError):
    """同号凭单已封存，但本次编制输入与封存内容不一致。"""


class VoucherVerifyError(VoucherError):
    """凭单守恒 / 结构校验失败（封存内容被破坏或载荷被篡改）。"""


class PlateError(Exception):
    """印厂制版体系的错误基类，与组织 / 预算错误体系完全分离。"""


class PlateInputError(PlateError):
    """制版输入非法（引用不存在的栏头、隔墨预算为负、凭单无正带宽等）。"""
