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


class AuthError(Exception):
    """授权本服务的所有错误基类。"""


class InvalidRole(AuthError):
    """角色不在经办 / 复核两道印之内。"""


class AuthEditionNotFound(AuthError):
    """授权本版本不存在或尚未发布过任何版本。"""


class SigningError(Exception):
    """会签询证的所有错误基类；拒绝时只说明理由，不产出空白凭证。"""


class InvalidPaymentAmount(SigningError):
    """请付金额不是正整数。"""


class CommitmentNotOpen(SigningError):
    """承诺已取消，不能再询证会签。"""


class InsufficientCommitmentRemaining(SigningError):
    """请付金额超过未取消承诺的真实剩余。"""


class NoSignerAvailable(SigningError):
    """两道印中至少一道在本次取值下没有任何被授予的人。"""


class SignableListNotFound(SigningError):
    """询证名单编号不存在。"""


class PrintRoomError(Exception):
    """印务室备印与正式登记的所有错误基类。"""


class StampFeeMissing(PrintRoomError):
    """某个候选“人、角色”缺少一次印金。"""


class InfeasiblePrep(PrintRoomError):
    """即使全备也无法让某个缺席预案盖齐全批单。"""


class StampNotPrepared(PrintRoomError):
    """正式登记时用到了未备印的“人、角色”。"""


class PersonOffDuty(PrintRoomError):
    """正式登记时用到了实际缺席的人。"""


class PersonOccupied(PrintRoomError):
    """同一人在一轮内被第二次占印。"""


class IncompatibleAssignees(PrintRoomError):
    """同一张单配对了本轮互斥名单上的两个人。"""


class ReceiptConflict(PrintRoomError):
    """同回执号被用不同的签单内容重复打开。"""
