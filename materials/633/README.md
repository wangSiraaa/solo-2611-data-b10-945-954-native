case.json的比分始终按该match的主/客槽位，不直接附着球队；before_fixture每对一次、主客1/2平衡，flipped_match_ids同时翻三场得到另一合法赛程。scores固定，按两个真实edition解读应不同。所有时间/赛程为虚构；不提供排名或赛程目标代码。

本微型输入的完整映射：每轮唯一可用slot为s，容量均为slot_capacity=2；blackouts=[]表示所有球队无禁用slot。scores各行为state=played的摘要，稳定result_id取r-加match_id，按数组顺序组成一次原子初始批request_id=initial-results，引用同名match_id；score_home/score_away分别取home_goals/away_goals。该事实切面固定为此批提交后，无额外判罚。

扩展（客队器材托运）：league/transport.py 按已发布 edition 的真实主客与 slot 生成版本化客队班车凭单（主队取自 edition_match 而非 match_id 字母；有向站对时长倒推去程、顺推回程；同队车段不得严格重叠；可退运/改接货站台，旧版永久保留；车站无托运单也可查）。league/shipping.py 是独立器材库，只读赛程库：登记密封箱（至多6）与付费直送单（至多12，整单只买一次、按容量带多箱），用真实班车车段加已采购直送求全部如约到站的最小直送费，输出每箱含留站等待的完整交接单，不可行时给可定位原因且不虚构免费转站。演示见 demo.py 第 7、8 节；对照测试 tests/test_transport.py、tests/test_shipping.py、tests/test_shipping_oracle.py。
