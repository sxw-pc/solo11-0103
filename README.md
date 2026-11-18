# 能耗抄表采录台

园区离线抄表结算证据流水线。读取本地批次契约、表位谱系、倍率生效区间、换表记录和手持终端 CSV。输入只用合成样本，运行时不联网。

## 运行

Python 3.11+，运行期仅标准库。`python -m pip install -e .` 安装；`python -m meterround demo` 一条命令创建临时数据库并打印 JSON 报告路径，旁边同时生成 Markdown；`python -m meterround --help` 查看独立初始化、导入、判定、查看和报告命令；`python -m pytest -q` 测试。

## 业务流与实体

批次 Batch → 表位 Slot → 按时段绑定的表计 Assignment；倍率 Multiplier 和换表 Replacement 与表位关联。来源 Source 用 SHA-256 去重，读数 Reading 用设备与序号幂等；原始行和行号不可覆盖。Issue 保留身份冲突、缺读、窗口、回卷、换表、倍率、零耗和尖峰证据；Decision 给出可结算或待核。所有写入在 SQLite 事务内完成，重复运行重算决策但不复制证据。错误由稳定 code 表示。

运行台账 Run 与业务判定在同一事务内写入；Evidence 逐条关联读数、来源哈希与载荷哈希。导入失败时整次事务回滚。

## 判定

同一表计计算读数差；接近量程末端跳到零附近才认为回卷，否则待核。换表需有旧表末读、新表初读和生效时间。倍率按读数时间区间拆段，缺口使结论待核。零耗、异常突增、缺读或超出抄表时窗也待核。报告保存分段算式和证据读数 ID。当前仅处理单表位结算，暂不引入计价、分摊或遥测。


## 现场换电废电池流程

`meterround/battery_waste.py` 在原有抄表证据之上增加废电池包台账：

- `register` 把每包冻结的 `device`/`slot`/来源 SHA 与 `battery_readings.csv` 导入的读数、证据、来源逐条绑定到 `battery_pack_evidence`；改包、改来源摘要或缺失读数都拒绝登记。
- `load`/`ship`/`process` 仍是单步实物端点，只接受一个包/箱，容量按精确分数比较，重复操作 ID 依据持久化意图重放，冲突复用报 `operation_id_conflict`。
- `plan_campaign` 在 12 包、6 箱上限内用子集动态规划全局精确求解；`maximize_recovery` 先最大化实际可回收金属，再最小化箱数，`minimize_shipments` 反之。混装化学体系只能热处置，金属不计回收。
- `execute_campaign` 按箱逐包装入、出运、处理，每步先写意图再真实调用端点；崩溃或重复执行时按操作 ID 与 `state` 实况恢复，已出运箱不复装、已处理包不二次处理。
- 缺包、来源不符、容量不足或端点拒绝时隔离受影响的包/箱，再对剩余可执行包重规划；整批无解会明确返回 `infeasible`。
- `build_report`/`battery-run --out` 输出逐包来源与箱号、逐箱载重与化学组成、操作流水、逐包/逐箱/整批守恒核对，并把计划预测值与实际产物分列。

CLI：`python -m meterround battery-register|battery-plan|battery-run|battery-state`。`battery-run` 支持 `--stop-after box1:ship` 模拟中断后重启，`--plan-id` 绑定已冻结的计划。
