"""会签授权本的纯领域规则（不含任何 I/O）。

题面规则：

* 一道请付单要经过 ``HANDLER``（经办）和 ``REVIEWER``（复核）两道印；
* 行政可在任一部门对每个“角色、人”登记授予或禁止，某版同一位置
  （部门、角色、人）只许一项；
* 请付的两个角色分别找人：沿真实承诺所在叶到根，对每个人和每个角色
  只取最近一条登记；查不到的人无权，被最近的禁止命中也无权；
* 授权仅在询证那一刻取值，解析结果由调用方冻结进可签名单。

本模块刻意不依赖任何服务或数据库，便于独立测试与审计重算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 两道印：经办与复核。印务室按角色收取印金、登记占用。
HANDLER = "HANDLER"
REVIEWER = "REVIEWER"
ROLES: tuple[str, ...] = (HANDLER, REVIEWER)

GRANT = "GRANT"
DENY = "DENY"


@dataclass(frozen=True)
class AuthEntry:
    """某部门对一个“角色、人”位置的一项登记。"""

    department_id: str
    role: str
    person_id: str
    effect: str  # GRANT 或 DENY

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"非法角色 {self.role!r}，只允许 {ROLES}")
        if self.effect not in (GRANT, DENY):
            raise ValueError(f"非法授权效果 {self.effect!r}，只允许 GRANT / DENY")


@dataclass(frozen=True)
class AuthBook:
    """某一版授权本的不可变切面。

    同一 (部门, 角色, 人) 位置在一版内至多一条；授权本只冻结登记事实，
    组织树由调用方以 :class:`~orgbudget.org_service.OrgProduct` 提供，
    与预算方案绑定的组织版本一致，从而“预算换版未获准时不能提前换用新印”。
    """

    edition: Optional[int]
    entries: tuple[AuthEntry, ...]
    published_at: Optional[str] = None

    def __post_init__(self) -> None:
        seen: set[tuple[str, str, str]] = set()
        for entry in self.entries:
            slot = (entry.department_id, entry.role, entry.person_id)
            if slot in seen:
                raise ValueError(f"授权本同一位置重复登记: {slot}")
            seen.add(slot)


@dataclass(frozen=True)
class PersonAuthority:
    """一个人在某个角色上的解析结果。"""

    person_id: str
    role: str
    granted: bool
    # 命中的最近一条登记；为 None 表示叶到根全程查不到 -> 无权。
    matched: Optional[AuthEntry]

    @property
    def layer(self) -> Optional[str]:
        """从哪一层继承（授予时的部门）；禁止或查不到时为 None。"""
        if self.matched is not None and self.matched.effect == GRANT:
            return self.matched.department_id
        return None

    @property
    def denied_by(self) -> Optional[str]:
        """被哪一层的禁令禁掉；未被禁时为 None。"""
        if self.matched is not None and self.matched.effect == DENY:
            return self.matched.department_id
        return None


def resolve_role(
    book: AuthBook,
    role: str,
    leaf_id: str,
    ancestors_root_first: tuple[str, ...],
) -> tuple[PersonAuthority, ...]:
    """对单个角色，沿“叶 → 根”解析所有出现过的人。

    ``ancestors_root_first`` 是根在最前、直接父级在最后的祖先链（即
    :meth:`OrgProduct.ancestors` 的返回形态），本函数从叶本身开始向上走。
    对每个人只取遍历中命中的第一条（最近一条）登记；查不到的人不出现在
    结果里（即无权，且无可展示的继承/禁止依据）。
    """
    if role not in ROLES:
        raise ValueError(f"非法角色 {role!r}，只允许 {ROLES}")
    # 叶 → 根：自身在最前，根在最后。
    path = (leaf_id,) + tuple(reversed(ancestors_root_first))
    path_index = {dept: i for i, dept in enumerate(path)}
    latest: dict[str, AuthEntry] = {}
    for entry in book.entries:
        if entry.role != role or entry.department_id not in path_index:
            continue
        idx = path_index[entry.department_id]
        prior = latest.get(entry.person_id)
        if prior is None or idx < path_index[prior.department_id]:
            latest[entry.person_id] = entry
    return tuple(
        PersonAuthority(
            person_id=person,
            role=role,
            granted=(entry.effect == GRANT),
            matched=entry,
        )
        for person, entry in sorted(latest.items())
    )


def granted_people(authorities: tuple[PersonAuthority, ...]) -> tuple[str, ...]:
    """从解析结果中取出仍被授予的人（按人 ID 升序）。"""
    return tuple(a.person_id for a in sorted(authorities, key=lambda a: a.person_id)
                 if a.granted)
