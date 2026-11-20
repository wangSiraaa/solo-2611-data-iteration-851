# SOLO 流式字符编码服务与原件保真修订库

本题面定义的 SOLO 编码（仅实现题面格式，不接入任何外部编码标准）的 Python 3.11
实现，以及在其之上构建的、按字符区间打补丁但保持未修改原始字节的修订库。

## 交付物

| 路径 | 内容 |
| --- | --- |
| `solo/codec.py` | 流式解码器 `Decoder`、检查点 `Checkpoint`、整件解码 `decode_document` / `decode_all` / `iter_decode`、独立编码入口 `encode_text` / `encode_document`，错误类型与路径记录 |
| `solo/revision.py` | 修订库 `RevisionLibrary`、`replace` / `preserve` 补丁、版本与批次记录、边界模式调整、字节保真校验 |
| `solo/blocking.py` | 从某修订版本还原横排场记：`parse_score` 输出每角色半开在场区间、服装与逐列原字节定位；悬空 `~` 报真实字节位置 |
| `solo/wardrobe.py` | 服装部独立复核：分角时间线、同饰两角时间冲突、换装窗与工时、班表约束下的最少换装工穷举排班 `solve` 与独立校验 `verify_plan` |
| `solo/stagedemo.py` | 题面两版场记、双字节服装与 `~` 改稿继承、一人分饰两角/班表断开/非等宽列三个端到端例子，写出 `artifacts/stage/` |
| `solo/sample.py` | 确定性合成原件生成器（ESC/PAIR 故意跨 128 KiB 块，含模式切换、空替换、连续补丁与非法尾部所需素材） |
| `solo/demo.py` | 演示：真实原件、两批修订、未修改字节核对、错误报告、较大语料，写出 `artifacts/` |
| `solo/__main__.py` | CLI：`decode/check/encode/revise/demo` 与场记专用 `stage/plan/stagedemo` |
| `tests/test_solo.py` | 独立参考状态机、差分测试、跨块、检查点、修订、规模测试 |
| `tests/test_stage.py` | 场记还原、修订继承、分角冲突、换装排班，并以独立朴素枚举器对 60 个随机实例交叉验证最少人数 |
| `artifacts/` | 运行 `python3 -m solo.demo` 与 `python3 -m solo.stagedemo` 生成的真实原件、各版本字节、`manifest.json` 与 `stage/stage_report.json` |

## SOLO 编码（题面定义）

- 正文从 `SINGLE` 模式开始。
- `SINGLE`：`00..7F` → 同码点 ASCII；`FF` 为 ESC；`80..FE` 非法。
- ESC 后单字节指令：`01` → 切到 `PAIR`；`00` → 切回 `SINGLE`；`FF` → 字面
  `U+00FF` 且不改模式；其它 → 错误。模式切换本身不产字符。
- `PAIR`：除 `FF`（仍是 ESC）外，两个字节组成无符号大端整数 `n`，映射
  `U+4E00 + n`，限定 `0 ≤ n ≤ 0x0FFF`。第一字节正常时，第二字节即使是 `FF`
  也属于该数值（`00 FF` = n=0x00FF），不会重新开转义。
- 正文结束遇到半个 PAIR 或半条 ESC 均为错误。
- 错误报告原字节偏移与到达该状态的路径（`DecodeError.offset` / `.path`），绝不
  用 `?` 替换后继续。解码失败后解码器进入失败态，不会假成功。

## 流式语义

`Decoder` 按任意字节块增量运行：`feed()` / `feed_into()`，偏移始终是原件正文
偏移。跨块半条 ESC、半个 PAIR 会被保存为待续状态，下一块首字节续接，因此不会
像旧的按块重新初始化解码器那样丢掉块首字符。`finish()` 校验正文末尾并返回结束
模式。

### 检查点

`Decoder.checkpoint()` 返回 `Checkpoint`，包含位置、模式、待续状态与**前缀
SHA-256**；`Checkpoint.resume(body)` / `Decoder.from_checkpoint()` 只在给定正文
前缀摘要一致时才从检查点继续（`verify=False` 可跳过，供已自行校验前缀的调用方
使用）。`Version.checkpoint_at(byte_offset)` 可直接从某个版本继续解码。

## 修订库

- 版本是不可变的真实原件字节 + 有序补丁批次；一个库先经 `add_original()` 载入
  原件（会完整解码校验，失败即拒绝）。
- `replace(start_char, end_char, text)`：按某固定原件版本的**字符**半开区间替换
  为 Unicode 文本。`preserve(start_byte, end_byte)`：要求该**字节**区间原样保留
  （可以落在字符内部；包含它的字符因此不可被替换；若边界落在纯切换字节上则拒绝）。
- 一批补丁先校验：同一基准版本、替换区间两两不相交（相邻允许）、preserve 区间
  互不重叠且不被替换触碰、替换文本可编码；任一失败则整批不发布。
- 组装：未替换字符按原字节复制（`kept_runs` 记录旧字符区间与新字节区间的对应），
  只对替换文本编码；当前输出模式与下一段原始字节解码模式不一致时，才在拼接边界
  插入合法切换（`BoundaryAdjustment`，`splice` / `boundary` / `final`）。整文件
  从不重新编码，因此不会掩盖原件差异。
- 新版本结束模式强制为 `SINGLE`，不会给后续保留字节留下悬空模式。
- 发布前独立校验：重新流式解码新正文，逐段核对未替换区间字节一致、preserve
  区间逐字节一致，并核对解码文本等于“旧 Unicode 做同批替换”的结果。全部通过
  才登记新版本；否则抛出 `RevisionError`，库不变化。

### 版本记录

`Version.records` / `BatchRecord` 记录每个替换的旧字符区间、旧字节区间、新字符
区间、新字节区间、新增字节与边界切换；`boundary_adjustments` 记录新字节偏移、
前后模式和切换字节；`kept_runs` 与 `dropped_switch_spans` 记录原样复制和未保留的
纯切换跨度。

## 场记还原与换装工排班

剧团横排场记是一份 SOLO 文档：每行一条角色线，每列一列，列宽（分钟）由外部
正整数序列给出，相邻列时间相接。

- `.` 不在场；`~` 同角色紧前列同一服装继续在场；其余字符是服装号。双字节
  PAIR 服装号仍只占一列，模式切换不占列。
- 首列或空场之后的 `~` 没有承接对象，`BlockingError` 报出该单元格在版本正文中的
  真实字节区间与行列坐标，绝不用占位符糊过去。
- `parse_score(version, widths, roles)` 合并同服装连续占台，输出每个角色的半开
  `[start,end)` 在场区间、服装号，以及区间内逐列的原字节 `[byte_start,byte_end)`。
- 改稿只替换开头服装时，后续 `~` 在新版本跟随新服装合并；旧版本字节保留、仍可
  重开解码（见 `BlockingRevisionTests`）。

场记端不知道演员与分角。服装部用自己保存的分角单、工时与班表复核（`wardrobe.py`）：

- 每人首段开始前已着装；同一演员完整时间线上相邻两段服装不同才产生一项换装，
  换装须落在前段退场到后段登台之间的半开窗内；即使是不同角色也在同一人时间线上
  找前后段。同一演员同时饰两角直接报 `casting_conflict`。
- 换装不可暂停、不可跨工人；一名工人一次只帮一人，整段 `[start,start+duration)`
  必须完整落在该工人某条实际值班区间内；只计整数分钟。
- 缺工时单列 `missing_worktime`；窗口短于工时列 `window_too_short`；没有任何值班
  区间能完整覆盖列 `no_worker_covers_window`。
- `solve` 按工人数从小到大穷举工人子集并回溯，返回最少人数与逐人逐次起止安排；
  排不成时给出可逐条核对的候选工人与争用约束。规模上限：12 项换装、6 名工人、
  开场到收场 60 分钟。

题面例子（六列各一分钟，P=`A...B.`、Q=`C...D.`，A→B 与 C→D 各两分钟、窗
`[1,4)`）：两名全日工最少 2 人；Q 改稿为 `C....D` 后一人可依次 `[1,3)`、`[3,5)`
完成；保留原稿仅把 C→D 工时改成 1 分钟则一人也够。三个结论均由测试锁定。

## 运行

```bash
python3 -m unittest discover -s tests -v      # 全部测试
python3 -m solo.demo                          # 演示并写 artifacts/
python3 -m solo.stagedemo                     # 场记/排班端到端，写 artifacts/stage/
python3 -m solo check artifacts/original.bin
python3 -m solo decode --checkpoint 10 artifacts/version_1.bin
python3 -m solo revise artifacts/original.bin patches.json > new.bin
python3 -m solo stage sheet.bin 1 1 1 1 1 1 --roles P,Q
python3 -m solo plan  sheet.bin plan.json
```

`patches.json` 格式：

```json
{"patches": [
  {"replace": [0, 3, "新文本"]},
  {"preserve": [100, 200]}
]}
```

`plan.json` 是服装部自己的独立分角单：

```json
{"widths": [1, 1, 1, 1, 1, 1],
 "roles": ["P", "Q"],
 "casting": {"PA": ["P"], "QB": ["Q"]},
 "worktimes": {"A->B": 2, "C->D": 2},
 "workers": {"W1": [[0, 60]], "W2": [[0, 30], [40, 60]]}}
```

`plan` 成功退出码 0、无法排班退出码 1、输入或原件错误退出码 2。

## 测试覆盖

- 1/2/7/4096 字节分块对独立朴素状态机逐字符、逐字节区间核对（含非法输入与
  错误偏移）。
- 生成器把 ESC/PAIR 精确放在 128 KiB 边界上；跨块、模式切换无字符、替换为空、
  连续/相邻补丁、非法尾部均有专项测试。
- 修订：未修改区间字节等同、每版本可重开解码、失败批次不发布、preserve 逐字节
  保留、新文本等于同批替换、无悬空模式。
- 场记：非等宽列边界、`~` 合并与悬空报错的真实字节位置、双字节服装占一列两字节、
  改稿后 `~` 跟随新服装且旧版仍可打开、修订后字节定位仍精确。
- 排班：分角时间线合并、同饰两角冲突、缺工时、零长度窗、断开班表、重叠换装强制
  两人；`tests/test_stage.py` 内的独立朴素枚举器对 60 个随机小实例逐项核对生产
  求解器的可行性与最少人数。
- 规模：10 MiB 原件、一批 100 个补丁（`ScaleTests`），未修改运行按原字节复制。
