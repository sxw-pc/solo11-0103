# 能耗抄表采录台

园区离线抄表结算证据流水线。读取本地批次契约、表位谱系、倍率生效区间、换表记录和手持终端 CSV。输入只用合成样本，运行时不联网。

## 运行

Python 3.11+，运行期仅标准库。`python -m pip install -e .` 安装；`python -m meterround demo` 一条命令创建临时数据库并打印 JSON 报告路径，旁边同时生成 Markdown；`python -m meterround --help` 查看独立初始化、导入、判定、查看和报告命令；`python -m pytest -q` 测试。

## 业务流与实体

批次 Batch → 表位 Slot → 按时段绑定的表计 Assignment；倍率 Multiplier 和换表 Replacement 与表位关联。来源 Source 用 SHA-256 去重，读数 Reading 用设备与序号幂等；原始行和行号不可覆盖。Issue 保留身份冲突、缺读、窗口、回卷、换表、倍率、零耗和尖峰证据；Decision 给出可结算或待核。所有写入在 SQLite 事务内完成，重复运行重算决策但不复制证据。错误由稳定 code 表示。

运行台账 Run 与业务判定在同一事务内写入；Evidence 逐条关联读数、来源哈希与载荷哈希。导入失败时整次事务回滚。

## 判定

同一表计的相邻读数组成区间，求解整数回卷次数 k：原始用量 = 读数差 + k×量程。每个候选与该段有效表计容量、倍率生效区间、换表前末读/新表初读、批次时窗和 spike_limit 联立，只有配置约束下唯一可行的候选才允许结算；零个候选（rollover_no_candidate）、多个候选（rollover_ambiguous）、量程缺失或无效（assignment_conflict、capacity_unknown）、序号缺口（sequence_gap）、换表证据不闭合（replacement_gap）都须待核。合同按 batch.rollover_rule 二选一：single 为单回卷规则，回卷区间跨越一个以上倍率周期即待核；capacity 为容量约束规则，允许长区间多次回卷，但回卷次数须由最大可达用量（spike_limit）唯一锁定。换表区间只接受 k=0，且要求旧表末读不低于换表前末次读数、新表读数不低于新表初读。判定明细为每个区间记录全部候选及其分段算式，分段保留读数证据 ID 与倍率来源，回卷差值随分段呈现而不只写入总量。零耗、异常突增、缺读或超出抄表时窗也待核。报告保存分段算式和证据读数 ID。当前仅处理单表位结算，暂不引入计价、分摊或遥测。
