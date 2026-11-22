"""层级组织编制服务与预算执行仓。

两个相互独立、仅通过已发布的不可变组织产品版本衔接的服务：

* :mod:`orgbudget.org_service` —— 组织编制服务，维护唯一根的父子树；
* :mod:`orgbudget.budget_service` —— 预算执行仓，绑定某个组织产品版本做
  权重分解、承诺与实际支出。

所有金额均为整数分。
"""

from .budget import allocate_children
from .budget_service import BudgetService, Commitment, NodeSummary, SchemeInfo
from .errors import (
    BudgetError,
    CommitmentClosedError,
    CommitmentNotFound,
    CycleError,
    DuplicateDepartment,
    IdempotencyConflict,
    InactiveDepartment,
    InsufficientFunds,
    InvalidTree,
    NotALeaf,
    OrganizationVersionNotFound,
    PlateError,
    PlateInputError,
    RootError,
    VoucherConflict,
    VoucherError,
    VoucherHandOffError,
    VoucherNotFound,
    VoucherVerifyError,
    WeightError,
)
from .flow_voucher import FlowBand, FlowVoucher, OccupancyFact, VoucherLine
from .org_service import OrgProduct, OrgService
from .plate import (
    Crossing,
    PlateConstraints,
    SideConstraints,
    WorkOrder,
    issue_work_order,
    issue_work_order_from_payload,
)
from .voucher_service import VoucherService

__all__ = [
    "OrgService",
    "OrgProduct",
    "BudgetService",
    "Commitment",
    "NodeSummary",
    "SchemeInfo",
    "allocate_children",
    "VoucherService",
    "FlowVoucher",
    "FlowBand",
    "VoucherLine",
    "OccupancyFact",
    "PlateConstraints",
    "SideConstraints",
    "Crossing",
    "WorkOrder",
    "issue_work_order",
    "issue_work_order_from_payload",
    "BudgetError",
    "CommitmentClosedError",
    "CommitmentNotFound",
    "CycleError",
    "DuplicateDepartment",
    "IdempotencyConflict",
    "InactiveDepartment",
    "InsufficientFunds",
    "InvalidTree",
    "NotALeaf",
    "OrganizationVersionNotFound",
    "PlateError",
    "PlateInputError",
    "RootError",
    "VoucherError",
    "VoucherHandOffError",
    "VoucherNotFound",
    "VoucherConflict",
    "VoucherVerifyError",
    "WeightError",
]
