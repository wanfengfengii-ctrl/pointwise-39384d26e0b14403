# 双班共读

## 发读和收值

`Readout.begin_shared_read(other_shift_id)` 在已绑定本班的终端上核准一次共读，返回带 `order_id` 和锁存 `position` 的读单对象。两班须为当前档案中已登记的不同班次，终端绑定的本班清册须与档案登记一致，发读前两班都须覆盖当前实际档道。资格失败不得发读或占用在读位。选位和设备调用沿用原终端的逐次操作方式，操作员可在发读和收值之间继续选位。

`Readout.finish_shared_read(order_id, reply=None)` 沿用 `DeviceReply`、原回包校验和原码类型。省略回包时从设备领取，尚无回包返回 `ReadStage.PENDING`。回包必须匹配原读单和发读时锁存位置，按发读前冻结的两份口径分别精确换算和判定，超限件也签发。原码保留整数或原始文本。同一终端的单班与双班读单共用在读位。错用收值入口、未知读单及取消后迟回均报错，保全当前在办。取消沿用 `cancel_read` 的状态规则。

## 签发和领取

成功的 `finish_shared_read` 返回 `archive.receipts.SharedReceipt`，公开字段为 `order_id`、`position`、`raw`、`receipts`。`order_id` 为设备原读单号，`position` 为原锁存档道，`raw` 为原码。`receipts` 是由两个现有 `Receipt` 对象组成的元组，本班在前，另一班在后。每个成员携带本班的固定口径和判定，并保留共同的真实读单号、档道和原码。组内两个成员回执号彼此不同，并沿用原回执号空间。

双件同次提交后可见。提交失败须向调用方报告错误，保留同一在办原单和已验回包。解除故障后再次调用 `finish_shared_read(order_id)` 即可续办，无须重新投递或发读。成功后省略回包或重投完全相同的原证据，均返回原组及原回执号。原码整数 `6` 和文本 `"6.0"` 即使换算等值也属于不同证据。改原值、错原单、错档道或错绑成员均拒绝。已签原读单须保持原有单班或双班归属，旧单件写入口对已签双班原读单报冲突。

`read_receipt(store, receipt_id)` 仍返回一份 `Receipt`。`export_shift(store, shift_id)` 仍按持久回执号返回本班的记录，包含本班新签的双班成员。新增 `archive.readback.read_shared_receipt(store, receipt_id)`。任一成员号均返回相同的 `SharedReceipt`，旧单件号返回 `None`，未知号报 `LookupError`。停止设备并重开档案后，按号和按班须取得原结果，已有 V1 库的旧编号、内容和领取行为保留。本次在办续办限原会话。

## 样例和交付

`shared_examples.open_example(store)` 返回早班、复核班、计次设备、人工终端和读单对象。早班来自 `samples/day_shift.json`，复核班 `CW-CHECK-01` 仅覆盖 A/2，其余口径与早班 A/2 相同，上限为 5/2 kPa。终端选 A 再进一道，发读后切到 B/1。投递 A/2 原码 6 时，两班均为 3 kPa，早班为 `pass`，复核班为 `outside_limit`。设备的 `reads` 记录每次成功发读的实际锁存，可用来核对共读及重试。复核班在 B/1 无资格发读。

`shared_examples.reject_insert_commit(admin, table)` 接受同库管理连接和目标表名，在指定表插入后制造延迟外键冲突，使 SQLite 在提交时报错。可选择实际承接联合签发写入的表。V1 夹具自检 `python3 -B shared_examples.py` 使用原 `receipts` 表，检查失败的可见性、迟回及原读续办。

沿用根目录 `.pointwise-runtime.json` 的 version 1 和 cli 类型。`setup` 是准备命令列表，每条以 argv 表示，`verify` 和 `demo` 各为一个 argv。保留原回归，让核验入口覆盖双班资格、迟回、重投冲突、旧入口和联合提交失败，失败时返回非零。演示入口实际展示同读异判、失败后的原读续办，以及停源重开后从成员领取，两班回执和原单号可核对。简述共同原单与班次回执如何交接。
