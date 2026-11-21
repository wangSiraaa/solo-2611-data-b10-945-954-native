"""层级组织编制服务与预算执行仓。

两个相互独立、仅通过已发布的不可变组织产品版本衔接的服务：

* :mod:`orgbudget.org_service` —— 组织编制服务，维护唯一根的父子树；
* :mod:`orgbudget.budget_service` —— 预算执行仓，绑定某个组织产品版本做
  权重分解、承诺与实际支出。

所有金额均为整数分。
"""

from .budget import allocate_children
from .budget_service import BudgetService, Commitment, NodeSummary, SchemeInfo
from .contract_service import (
    Contract,
    ContractService,
    GoodsReturn,
    PayableStatement,
    Receipt,
    ReceiptNet,
    SpendFact,
)
from .errors import (
    BudgetError,
    CommitmentClosedError,
    CommitmentNotFound,
    ContractConflict,
    ContractError,
    ContractNotFound,
    CycleError,
    DuplicateDepartment,
    IdempotencyConflict,
    InactiveDepartment,
    InfeasibleSchedule,
    InsufficientFunds,
    InvalidContract,
    InvalidTree,
    NotALeaf,
    OrganizationVersionNotFound,
    PayableError,
    ReceiptError,
    ReturnError,
    RootError,
    WeightError,
)
from .org_service import OrgProduct, OrgService
from .payment import Bill, DayLedger, PaymentSchedule, schedule_payments

__all__ = [
    "OrgService",
    "OrgProduct",
    "BudgetService",
    "Commitment",
    "NodeSummary",
    "SchemeInfo",
    "ContractService",
    "Contract",
    "Receipt",
    "GoodsReturn",
    "ReceiptNet",
    "SpendFact",
    "PayableStatement",
    "schedule_payments",
    "Bill",
    "DayLedger",
    "PaymentSchedule",
    "allocate_children",
    "BudgetError",
    "CommitmentClosedError",
    "CommitmentNotFound",
    "ContractError",
    "ContractNotFound",
    "ContractConflict",
    "InvalidContract",
    "ReceiptError",
    "ReturnError",
    "PayableError",
    "InfeasibleSchedule",
    "CycleError",
    "DuplicateDepartment",
    "IdempotencyConflict",
    "InactiveDepartment",
    "InsufficientFunds",
    "InvalidTree",
    "NotALeaf",
    "OrganizationVersionNotFound",
    "RootError",
    "WeightError",
]
