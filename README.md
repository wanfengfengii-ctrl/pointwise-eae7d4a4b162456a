# 青岬压力试验录制交接

设备员把分站记录器的录制入档并追加更正。资料员冻结原卷段序和许可修订，分别发行站件。夜班乱序收件，关闭源站后指定实收件成卷，凭记录合计和逐值原位定位压力异常。次班可按原报告号复领报告和首执。

## 运行

使用 Python 3.11 标准库。从项目根目录运行。

```sh
python3 -B check_project.py
python3 -B demo.py
```

控制台提供首条命令的检查入口。也可直接运行完整套件 `python3 -B -m unittest discover`。演示读取 `samples/night_shift.json`，使用临时目录内的四个库，关闭源库及发行库后成卷，重开报告库后复领及核账。演示输出为实际签存值。

## 业务 API

各 `open_*` 函数接收 SQLite 路径，返回由调用方关闭的连接。用 `source.raw_store.open_source_store` 打开源库，用 `issue.issued_store.open_issued_store` 打开发行库及其中的交接单表。`issue.manifests.open_manifest_store` 可单独打开交接单库。隔离端使用 `receipt.inventory.open_inventory` 和 `reports.snapshot_store.open_report_store`。四类持久状态应使用独立库文件。重开只创建缺少的表，样例由调用方明确装载。写入型业务入口管理本次事务，应在连接空闲时调用。

| 入口 | 参数与结果 |
| --- | --- |
| `source.recordings.record` | `(source, roll, segment, raw_bytes, *, body_start=0, body_end=None)`。登记新段并返回初始 `Revision`。重复段或无效来源抛出 `ValueError`。 |
| `source.revisions.revise` | 同样的参数，在已登记段追加修订，首次修订为 1。未知段抛出 `LookupError`。`read_revision(source, ref)` 精确读旧版，`revision.locate(body_offset)` 给出原段位置。 |
| `issue.manifests.freeze` | `(source, issuer, roll, slots)`。`slots` 按原序排列，每个 `ManifestSlot(segment, candidates)` 接收由 `RevisionRef` 组成的元组。缺引用、串卷、空候选或重复段位抛出 `ValueError`。`read_manifest(issuer, manifest_id)` 按号读回。 |
| `issue.issued_store.issue` | `(source, issuer, manifest_id, segment, revision)`。返回固定站件的 V1 字节串。同一发行键返回原件。未许可的修订抛出 `ValueError`。`reissue(issuer, piece_id)` 按原件号返回原字节串，未知号抛出 `LookupError`。 |
| `receipt.intake.receive` | `(inventory, payload)`。验证并持久点收，返回整数实收号。原件重投返回原号。损坏件、同号异件抛出 `ValueError`。 |
| `assembly.close.close` | `(inventory, reports, manifest, accepted_ids, *, before_commit=None)`。每段指定一个实收号，传入次序不限，返回 `(ReportSnapshot, FirstReceipt)`。缺件、错件、未闭合卷或不足以解释的证据抛出 `ValueError`。 |
| `reports.retrieve.get` | `(reports, report_id)`，返回原 `ReportSnapshot` 和 `FirstReceipt`。未知号抛出 `LookupError`。 |
| `reports.audit.audit` | `(reports, report_id, record_number)`，返回含原记录的 `AuditResult`。记录序号从 1 起。未知报告或记录抛出 `LookupError`。 |

用 `FrozenManifest.to_bytes()` 导出冻结清单，用 `FrozenManifest.from_bytes(payload)` 在隔离端恢复视图。夜班通过受控交接渠道获得清单，并以它核对站件所绑定的清单、卷、段及修订身份。清单号为不透明字符串。实收号及报告号在各自库内有效。

固定件是规范 UTF-8 JSON，件号由 V1 身份及精确版本推导，摘要覆盖外层身份和派生正文。`issue.wire.decode_piece(payload)` 可读件号及正文结构，`receive` 另核对摘要和库存身份。摘要用于检查传输完整性，发行来源由交接渠道确认。原始站段及服务封头由源站保管。点收校验件的结构和完整性，成卷核定所选证据是否足以连续解释。

## 核账口径

`body_start` 含端点，`body_end` 不含端点，缺省使用整段。逐值出处以原段首字节为零点，包含卷、段、修订和偏移。每个读数按无符号字节解释。未转义的 `0A` 发出当前记录，零个读数也发出一条。未转义的 `1B` 使下一字节成为读数，出处为转义位置和数据位置。空段保留进入状态。合计使用整数原单位，无舍入、插补或缺测替代。只在整卷结束时裁定闭合。末尾悬空转义或尚有读数未遇到结束符时，连已完成的前缀一并拒签。

报告保留选件身份、顺序、逐记录读数、合计及出处。报告、记录和首执一起提交后方可领取。可选的 `before_commit` 无参回调在写入全部成果后、提交前执行，供调用方验证失败边界。回调抛出的异常和 SQLite 提交错误向调用方传播，事务回滚。正常调用可省略回调。历史查询使用报告库快照，后续更正与另签不改旧号下的值。
