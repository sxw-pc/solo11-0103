# 能耗抄表采录台

园区离线抄表结算证据流水线。读取本地批次契约、表位谱系、倍率生效区间、换表记录和手持终端 CSV。输入只用合成样本，运行时不联网。

## 运行

Python 3.11+，运行期仅标准库。`python -m pip install -e .` 安装；`python -m meterround demo` 一条命令创建临时数据库并打印 JSON 报告路径，旁边同时生成 Markdown；`python -m meterround --help` 查看独立初始化、导入、判定、查看和报告命令；`python -m pytest -q` 测试。

外部汇总发布：`python -m meterround publish-disclosures --db DB --requests fixtures/terminal_disclosure_requests.json --out OUT --policy minimum_safe_coarsening`（可选 `deny_unsafe`、`--dry-run`）。`python -m meterround recover-disclosures --db DB --out OUT` 重放中断发布并核验交付字节；`export-external --db DB --publication-id ID --out FILE` 按台账重放单个文件。

## 业务流与实体

批次 Batch → 表位 Slot → 按时段绑定的表计 Assignment；倍率 Multiplier 和换表 Replacement 与表位关联。来源 Source 用 SHA-256 去重，读数 Reading 用设备与序号幂等；原始行和行号不可覆盖。Issue 保留身份冲突、缺读、窗口、回卷、换表、倍率、零耗和尖峰证据；Decision 给出可结算或待核。所有写入在 SQLite 事务内完成，重复运行重算决策但不复制证据。错误由稳定 code 表示。

运行台账 Run 与业务判定在同一事务内写入；Evidence 逐条关联读数、来源哈希与载荷哈希。导入失败时整次事务回滚。

## 判定

同一表计计算读数差；接近量程末端跳到零附近才认为回卷，否则待核。换表需有旧表末读、新表初读和生效时间。倍率按读数时间区间拆段，缺口使结论待核。零耗、异常突增、缺读或超出抄表时窗也待核。报告保存分段算式和证据读数 ID。当前仅处理单表位结算，暂不引入计价、分摊或遥测。

## 披露边界

`report` 生成的内部完整证据报告含设备号、原始行和来源名，仅供独立授权审计。外部承包方只能通过 `publish-disclosures` 取得 H2 类手持终端覆盖表位数；交付文件仅含发布 ID、批次、版本、实际表位集合和计数。

发布前把该受众全部历史发布等式与拟发布等式联立，枚举满足二元终端约束的所有世界，只要有任何单个表位被新增唯一推出即拒绝。历史已暴露的表位不重复计数。`deny_unsafe` 拒绝；`minimum_safe_coarsening` 从 `allowed_expansion` 按最小基数枚举候选组合，发布整体安全的扩展集合并把实际发布集合写入台账。拒绝只进内部 `disclosure_refusal`，绝不影响外部可见知识。每受众分别落账历史/新发布、数值与文件摘要；发布请求 ID 幂等，`BEGIN IMMEDIATE` 串行化同受众并发核验；`pending` 台账先于文件提交，文件按台账字节原子替换并校验 SHA-256，恢复时重放或修复。
