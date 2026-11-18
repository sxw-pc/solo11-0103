# 能耗抄表采录台

园区离线抄表结算证据流水线。读取本地批次契约、表位谱系、倍率生效区间、换表记录和手持终端 CSV。输入只用合成样本，运行时不联网。

## 运行

Python 3.11+，运行期仅标准库。`python -m pip install -e .` 安装；`python -m meterround demo` 一条命令创建临时数据库并打印 JSON 报告路径，旁边同时生成 Markdown；`python -m meterround --help` 查看独立初始化、导入、判定、查看和报告命令；`python -m pytest -q` 测试。

## 业务流与实体

批次 Batch → 表位 Slot → 按时段绑定的表计 Assignment；倍率 Multiplier 和换表 Replacement 与表位关联。来源 Source 用 SHA-256 去重，读数 Reading 用设备与序号幂等；原始行和行号不可覆盖。Issue 保留身份冲突、缺读、窗口、回卷、换表、倍率、零耗和尖峰证据；Decision 给出可结算或待核。所有写入在 SQLite 事务内完成，重复运行重算决策但不复制证据。错误由稳定 code 表示。

运行台账 Run 与业务判定在同一事务内写入；Evidence 逐条关联读数、来源哈希与载荷哈希。导入失败时整次事务回滚。

## 判定

同一表计计算读数差；接近量程末端跳到零附近才认为回卷，否则待核。换表需有旧表末读、新表初读和生效时间。倍率按读数时间区间拆段，缺口使结论待核。零耗、异常突增、缺读或超出抄表时窗也待核。报告保存分段算式和证据读数 ID。当前仅处理单表位结算，暂不引入计价、分摊或遥测。

## 绿色属性凭证

`meterround/attributes.py` 把可结算用量换算为可转让的绿色属性凭证，台账落在 `attribute_*` 表。发行前必须同时满足：三个表位（发电、自用、上网）同批次/期间、decision 均可结算、总发电严格等于自用+上网；缺读、待核、逆流或闭合差额一律拒绝发行，绝不用不完整证据猜可交易凭证。

```
python -m meterround attribute-issue  --db D --config fixtures/solar_attribute_contract.json --title gross_generation_title
python -m meterround attribute-reissue --db D --config ... --title ... [--reason evidence_revised]
python -m meterround attribute-revoke  --db D --config ... [--reason ...]
python -m meterround attribute-transfer --db D --certificate RC-... --to HOLDER
python -m meterround attribute-recall-done --db D --certificate RC-...
python -m meterround attribute-ledger --db D [--batch B] [--summary]
```

- 合同文件字节以 SHA-256 冻结；权属基础另冻结 decision/rule_version 与 reading/evidence/source 摘要。同一合同与证据快照重跑返回 `unchanged`，不新发。
- `gross_generation_title` 把全部发电归场站运营方（样本 100 单位 → 10 张）；`net_export_title` 只把上网电量归购电方（样本 60 单位 → 6 张，自用不归买方）。同一批次只能有一种有效权属，`ux_attribute_issuance_active` 保证并发申请只有一方成功。
- 未满一张的余量按同一合同/场站/权属/持有人链记入 `attribute_carry`，只结转、不四舍五入，来源期间必须早于本期。
- 证据修订时 `reissue` 在单个事务里先全量撤销旧发行再按新基础发行；旧证已转让的转为 `attribute_recall` 召回任务，保留原持有人，绝不静默换主或复用旧有效 ID。
- 本模块不修改 `source`/`reading`/`evidence`/`decision`。
