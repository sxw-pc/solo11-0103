# 能耗抄表采录台

园区离线抄表结算证据流水线。读取本地批次契约、表位谱系、倍率生效区间、换表记录和手持终端 CSV。输入只用合成样本，运行时不联网。

## 运行

Python 3.11+，运行期仅标准库。`python -m pip install -e .` 安装；`python -m meterround demo` 一条命令创建临时数据库并打印 JSON 报告路径，旁边同时生成 Markdown；`python -m meterround --help` 查看独立初始化、导入、判定、查看和报告命令；`python -m pytest -q` 测试。

## 业务流与实体

批次 Batch → 表位 Slot → 按时段绑定的表计 Assignment；倍率 Multiplier 和换表 Replacement 与表位关联。来源 Source 用 SHA-256 去重，读数 Reading 用设备与序号幂等；原始行和行号不可覆盖。Issue 保留身份冲突、缺读、窗口、回卷、换表、倍率、零耗和尖峰证据；Decision 给出可结算或待核。所有写入在 SQLite 事务内完成，重复运行重算决策但不复制证据。错误由稳定 code 表示。

运行台账 Run 与业务判定在同一事务内写入；Evidence 逐条关联读数、来源哈希与载荷哈希。导入失败时整次事务回滚。

## 退役旧表保管

换表结算后旧表仍是实物资产。`meterround/custody.py` 以只追加流水管理保管状态机：已拆下 → 已封存 → 待交接 → 已签收，每步记录操作身份、时间、来源、请求号与前状态。建退役资产前必须确认 replacement 中旧表在指定时刻被新表取代，且不存在仍在生效的旧表 assignment；封存时重新计算真实封条文件字节的 SHA-256，与交接清单和资产绑定比对，封条号与旧表序列号唯一绑定。无封条、封条被改、错误序列号、时窗外或跳步均不产生最终转移。

发交接单与接收方签收是两步：仓管 dispatch 凭据与接收方 receipt 凭据必须绑定同一旧表、封条和交接单，receipt 须在合同指定时窗内到达；双向凭据闭合前实物始终由仓管 WAREHOUSE 持有，收件方名称不等于保管权变更。相同请求号重放幂等；两个接收方并发竞领时只有一个签收事务提交，败方留 custody_conflict 冲突记录。两种合同产生不同实物责任：`return_to_owner` 闭合后占有与处置权归 ASSET-OWNER，原仓管不得再送检；`quarantine_at_lab` 闭合后归 METROLOGY-LAB 并加检定隔离，业主只获暂存证明尚不能领取。保管流水不读取或改写 replacement、reading、source、evidence、decision。

命令：`retire`、`seal`、`handoff-issue`、`handoff-sign`（`--direction dispatch|receipt`）与 `custody-view`，均以 `--db --handoff --fixtures-dir --request-id` 离线运行，失败以稳定原因码和非零退出码返回。


## 判定

同一表计计算读数差；接近量程末端跳到零附近才认为回卷，否则待核。换表需有旧表末读、新表初读和生效时间。倍率按读数时间区间拆段，缺口使结论待核。零耗、异常突增、缺读或超出抄表时窗也待核。报告保存分段算式和证据读数 ID。当前仅处理单表位结算，暂不引入计价、分摊或遥测。
