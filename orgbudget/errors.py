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


# ------------------------------------------------------------- 装封与抽查


class TicketError(BudgetError):
    """装封名册相关错误基类（与预算金额错误同属预算仓）。"""


class SpendEventNotFound(TicketError):
    """被引用的实际支出事件（幂等键）不存在，或尚未实际入账。"""


class TicketNotFound(TicketError):
    """请求的原票不存在。"""


class RosterNotFound(TicketError):
    """请求的封样名册不存在。"""



class TicketConflict(TicketError):
    """支出事件的归票与该事件已登记的具名纸票冲突。"""


class IncompleteRoster(TicketError):
    """批次未配齐（仍有已实付事件未归票），不能签发完整名册。"""


class InvalidAuditRequest(TicketError):
    """抽查参数非法（层数/封数/概率/成本等不满足约束）。"""

