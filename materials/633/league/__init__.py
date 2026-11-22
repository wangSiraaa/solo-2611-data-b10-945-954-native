"""虚构联赛的赛程生产服务、赛季排名仓与旧场记簿逐球卡奖务系统。

- :class:`~league.schedule_service.ScheduleService`：赛程数据生产服务。
- :class:`~league.standings_repo.StandingsRepository`：赛季排名仓（比分/判罚事实）。
- :mod:`league.ranking`：不依赖存储的纯函数排名引擎，可独立用于复算与断链对照。
- :mod:`league.playbyplay`：逐球卡纯函数解释引擎（分层状态图，不保存排列）。
- :mod:`league.awards`：制胜球收件者判定与立即发证/待核听审。
- :mod:`league.tribunal`：合庭/分庭、回避与传卷优化的排庭求解。
- :class:`~league.card_repo.PlayByPlayRepository`：逐球卡版本仓与冻结奖证快照。
"""

from .schedule_service import ScheduleService, Issue, CandidateRejected
from .standings_repo import StandingsRepository, BatchRejected
from .ranking import compute_standings
from .card_repo import PlayByPlayRepository, CardBatchRejected

__all__ = [
    "ScheduleService",
    "Issue",
    "CandidateRejected",
    "StandingsRepository",
    "BatchRejected",
    "compute_standings",
    "PlayByPlayRepository",
    "CardBatchRejected",
]
