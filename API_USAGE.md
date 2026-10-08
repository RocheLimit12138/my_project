# FastAPI 接口使用说明

已支持通过统一客户端接入 DeepSeek。默认关闭；配置、密钥输入和连通检查见 [DEEPSEEK_SETUP.md](DEEPSEEK_SETUP.md)。下文检索参数仍适用；第 8 节说明启用后的生成行为。

## 1. 安装接口依赖

依赖已记录在 `pyproject.toml` 和 `uv.lock` 中，在项目目录执行：

```cmd
uv sync
```

该命令安装项目依赖。

## 2. 运行自动化测试

```cmd
uv run python -m unittest discover -s tests -v
```

接口测试使用模拟查询函数，不连接 MySQL。

## 3. 启动 API

先在当前 CMD 窗口配置数据库环境变量，再启动服务：

```cmd
uv run uvicorn app.api:app --reload
```

启动后可访问：

- `http://127.0.0.1:8000/docs`：交互式接口文档
- `http://127.0.0.1:8000/health`：进程健康检查
- `http://127.0.0.1:8000/cases?city=武汉&industry=烧烤`：案例查询

`/health` 不连接数据库；`/cases` 只执行查询，不写入数据库。

## 4. 自然语言案例、话术和方案查询

在 Swagger 中选择 `POST /agent/chat`，点击 Try it out 并提交：

```json
{
  "question": "客户觉得价格太贵，怎么回复？",
  "top_k": 3
}
```

话术分支使用项目内的 `bge-base-zh-v1.5` 模型与现有 `.chroma_db` 索引，不需要配置 MySQL。首次话术查询需要加载模型，后续请求在同一服务进程内复用模型。并发话术查询串行执行。

| 参数 | 默认值 | 适用范围 |
| --- | --- | --- |
| `question` | 必填 | 自然语言问题，1–1000 字符 |
| `limit` | 20 | 案例查询分页大小，1–100 |
| `offset` | 0 | 案例查询偏移量，非负整数 |
| `top_k` | 5 | 话术或无资料方案的候选数；提供商家资料时为每章候选数，1–20 的整数 |
| `max_distance` | null | 话术或方案参考的 cosine 距离上限，0–2，需分别评估后配置 |
| `merchant_profile` | null | 仅用于营销方案，结构化商家资料；字段见第 7 节 |

未启用 DeepSeek 时，话术答复为候选原文的模板化整理；启用后增加基于候选的模型建议，原文、章节、片段编号及审核提示继续由代码保留。距离越小越相似，不是置信概率；默认关闭阈值，命中候选不代表相关性已验证。城市和行业保留在检索问题中，未用于话术库过滤，话术响应的 `filters` 为 null 值。

- HTTP 200 / `completed`：查询正常完成，包括没有候选的情况；空结果时 `sources` 为空，回答说明未检索到。
- HTTP 200 / `needs_clarification`：无法识别意图，或案例查询需要澄清。
- HTTP 200 / `not_available`：保留给尚未接入的能力；当前案例、话术、方案草稿三个分支均已接入。
- HTTP 422：请求参数不合法。
- HTTP 400：业务参数不合法。
- HTTP 503：数据库、话术或案例文档检索不可用；话术索引缺失、模型不匹配及超过模型 token 上限等检索异常均返回稳定提示，不暴露内部异常内容。

当前 API 的字符上限不保证符合模型的 512-token 上限，超长话术问题应缩短后重试。

## 5. 本地联调样例

2026-09-21 使用 FastAPI TestClient 经真实 API 路由查询本地模型与索引，未启动监听服务，未连接 MySQL，也未重建索引。

| 问题 | 实际结果（top_k=3） |
| --- | --- |
| 客户觉得价格太贵，怎么回复？ | HTTP 200，命中价格异议片段 32、34、33 |
| 客户说预算不足，如何回答？ | HTTP 200，候选为片段 37、48、34，相关性仍需改进 |
| 老板说推广没效果，怎么沟通？ | HTTP 200，命中推广效果相关片段 41、29、40 |
| 今天会下雨吗？ | HTTP 200，unknown / needs_clarification，未检索话术 |

这组样例仅验证链路可用并记录初步检索结果，尚未形成足够的相关性评估集；尤其预算不足问题仍需补充标注样例，不能据此设置生产距离阈值。

## 6. 营销方案草稿

`POST /agent/chat` 已支持方案分支。例如：

```json
{
  "question": "参考历史案例，为武汉烧烤门店生成营销方案，预算和周期待确认",
  "top_k": 3
}
```

流程为总控 → ProposalAgent → 独立的 `case_documents` 集合 → ResponseAgent → 固定模板草稿。该分支不查询 MySQL 或话术集合；本地模型延迟加载，同一进程内的方案检索串行执行。城市和行业仅作为检索问题的一部分，没有结构化筛选，响应 `filters` 为 null 值。

固定模板保存在 `templates/marketing_proposal.md`，采用用户确认的八个必需章节：商家基本情况、商家现有问题分析、商家位置与商圈分析、目标客群分析、推广目标与预期、营销策略与活动设计、执行计划与预算、效果评估与调整。未提供商家资料时，历史参考集中列在第八节；提供 `merchant_profile` 时，各章分别呈现用户资料、待补字段及该章检索候选（见第 7 节）。历史文档无需按这八章重新排版。原文逐条保留文件名、页码/幻灯片号和审核状态。启用 DeepSeek 后可提炼策略并生成八章分析与建议预算分配，见第 8 节；建议不代表已确认报价或效果保证。输出为 Markdown，尚不导出 PPTX/PDF。

- 检索成功：HTTP 200 / `completed`，返回草稿及引用。
- 正常空结果（包括阈值过滤后为空）：HTTP 200，返回待完善框架，`sources=[]`。
- 索引缺失、模型不匹配、查询过长或检索故障：HTTP 503；不会伪装成无匹配结果。
- 话术和方案的距离阈值需要分别评估，不能直接沿用同一个已调参数。

当前案例目录的 7 份文件已获用户确认并完成索引写入（96 个片段）；真实文档检索及 API 联调通过。6 份文件明确标为模拟案例，引用保留该标记。索引写入记录见 `data/evaluation/case_documents/INDEX_RESULT.md`；新的逐章检索调试结果见 `SECTION_RETRIEVAL_RESULT.md`，广泛相关性评估仍待完成。可先在 CMD 中预览模板，无需模型、索引或服务：

```cmd
cd /d D:\my_project
.\.venv\Scripts\python.exe -m scripts.generate_proposal "为武汉烧烤门店生成营销方案，预算和周期待确认" --outline-only
```

此模式明确标记未执行检索。准备案例后，按 `data/case_documents/README.md` 先预览、确认范围，再显式执行索引写入。随后去掉 `--outline-only` 即可检索并生成草稿：

```cmd
.\.venv\Scripts\python.exe -m scripts.generate_proposal "为武汉烧烤门店生成营销方案" --top-k 3
```

CLI 成功返回 0，参数错误返回 2，检索或模板错误返回 1。

## 7. 商家资料与八章定向检索

在方案请求中增加 `merchant_profile`，即可将商家资料放入对应章节，并分别检索八章参考：

```json
{
  "question": "根据商家资料生成营销方案",
  "merchant_profile": {
    "name": "示例商家",
    "industry": "烧鸟",
    "city": "成都",
    "budget_yuan": 12000,
    "campaign_days": 56,
    "problems": ["工作日晚间订单不足"],
    "goals": ["增加工作日晚餐订单"],
    "data_source": "示例资料，待替换为实际来源"
  },
  "top_k": 3
}
```

完整示例在 `data/examples/merchant_profile.json`，填写说明见同目录 `README.md`。示例数据全部虚构。资料按请求传入，不会自动保存到 MySQL 或案例索引。

### 资料字段

所有字段均可缺省；不完整资料仍返回八章和缺失字段。文本空白会按缺失处理；列表不接受空白条目；未知字段返回 422。

| 字段 | 类型与含义 |
| --- | --- |
| `name`、`industry`、`city`、`address` | 商家名称、品类、城市、具体地址，字符串 |
| `store_count`、`business_stage` | 门店数量（1–100000 的整数）、经营阶段 |
| `products`、`average_spend_yuan` | 主营产品描述、客单价（数值，0–1 亿） |
| `business_status`、`problems` | 经营现状字符串、已知问题字符串列表（最多 10 条） |
| `location_notes`、`target_customers` | 当前门店商圈资料、目标客群描述 |
| `baseline_metrics` | 最多 20 条经营基线；每条含字符串 `name`、`value`，以及可选 `unit`、`period`、`source` |
| `goals` | 商家期望目标字符串列表（最多 10 条），不代表效果承诺 |
| `budget_yuan`、`campaign_days` | 预算上限（数值，0–10 亿）、周期天数（1–3660 的整数） |
| `platforms` | 意向平台字符串列表（最多 10 条） |
| `constraints`、`measurement_plan`、`data_source` | 执行约束、效果衡量与复盘安排、当前资料来源 |

金额和数量字段不接受字符串或布尔值；预算 `0` 是有效输入，不会当作缺失。基线指标的 `value` 使用字符串，例如 `"780"`；未提供统计周期或来源时会列入缺失项。短文本一般最多 80 字符，地址/来源/列表条目一般最多 200 字符，经营描述类字段最多 600 字符，详细限制见 Swagger。未填写的字符串/数值可省略或使用 `null`，列表可省略或使用 `[]`。

缺失检查只表示基础字段是否提供，不判断内容是否准确、完整或足以形成结论。例如“提高订单”可以作为商家目标输入，但具体目标值仍需评估。

### 返回值与兼容行为

原有 `answer`、`sources`、`warnings` 保留。提供资料时新增的 `proposal` 对象包含：

- `merchant_profile` 和 `profile_status=user_provided_unverified`：规范化的输入及未核验状态。
- `sections`：按固定顺序返回八章，每章包含 `key`、`title`、`query`、`provided_information`、`information_status`、`missing_fields`、`references`、`found`、`count`、`warnings` 和 `retrieval_performed`。
- `missing_fields`：各章缺失项的去重汇总，每项包含 `field` 与中文 `label`。指标缺失项使用 `baseline_metrics.0.period` 等路径。

每条参考保留原文、文件名、页码/幻灯片号、来源哈希、审核状态、模拟来源标记及原始距离，并增加 `section_heading_match`。原文与当前商家输入分开显示；缺失地址或商圈资料不会被历史商家信息补齐，也不会自动调用地图查询。

未启用 DeepSeek 时，未提供资料或使用 `merchant_profile: null` 保留原有单次检索和通用模板，响应 `proposal: null`；启用后会用空资料进入八章生成并返回缺失项。提供空对象 `{}` 则启用八章检索，并列出缺失项。资料必须用于能识别为营销方案的请求；用于案例、话术或未识别意图时返回 400，不会静默忽略。

### 逐章检索规则

每章用“城市 + 品类 + 章节标题 + 主题关键词”形成查询。当前版本不把完整问题、具体地址、商家名称或长篇经营资料拼入逐章向量查询；这些资料保留在对应章节。城市和品类是语义上下文，不是硬性筛选，`filters` 仍为 null 值。

`top_k` 在此模式下表示**每章最多返回的候选数**，默认 5，允许 1–20。每章先取 `max(12, top_k)` 个语义候选，应用可选距离阈值，再优先排列明确包含对应章节标题或已配置别名的片段，最后截取 `top_k`。仅检查片段前八个非空行；过短的标题页和列出多个模板章节的目录页不获标题优先。未匹配标题的文档仍可通过语义检索入选，历史案例无需重排或重新索引。

标题排序是启发式规则，不能证明业务适用性；排序后的原始距离可能不递增。`section_heading_match=true` 仅表示识别到了对应标题，`found=true` 仅表示有候选。总引用列表是各章去重后的合集，不是全局相关性排名；同一片段可出现在多个章节。

某章正常无候选时仍返回该章、输入与缺失项；任一章节检索异常则整个请求返回 503。八次查询增加了请求耗时，现有进程内串行检索保护继续适用。

### 命令行

在 CMD 中运行：

```cmd
cd /d D:\my_project
.\.venv\Scripts\python.exe -m scripts.generate_proposal "根据商家资料生成营销方案" --profile data\examples\merchant_profile.json --top-k 3
```

添加 `--json` 输出结构化结果；添加 `--outline-only` 只呈现资料和待补项，不加载模型或读取索引。这两个参数可以组合。资料文件使用 UTF-8，可带 BOM；文件读取或参数校验失败退出 2，检索或模板失败退出 1。

2026-09-22 已通过 164 项自动化测试，并用一份虚构资料完成真实本地 API/BGE/Chroma 验证。结果与局限见 `data/evaluation/case_documents/SECTION_RETRIEVAL_RESULT.md`。该次真实本地验证使用资料准备模式。后续 DeepSeek 集成已实现，完整测试增至 196 项；真实 API 和生成质量仍待配置密钥后验证。

## 8. 启用 DeepSeek 后的行为

完整配置见 [DEEPSEEK_SETUP.md](DEEPSEEK_SETUP.md)。请求格式不变，五个 Agent 共享由本地 `deepseek.local.toml` 与环境变量配置创建的客户端（环境变量优先）；已有 BGE、Chroma 和 MySQL 查询工具继续使用。案例和话术通常依次调用总控、当前专业 Agent、答复 Agent。方案八章正文在一次专业 Agent 调用中完成，生成后执行一次依据审核；使用提示由代码直接依据输入生成。因此正常方案聊天通常为三次模型调用、独立方案 CLI 通常为两次；任一步失败即停止，不自动重试。

- 总控：JSON 意图与实体经校验后才路由；澄清时停止后续查询。
- 案例/话术：检索成功且有候选时生成分析或建议，与原始记录分开展示。正常空结果保持为空。
- 方案：无商家资料时使用空资料，仍检索八章并生成待补提示；原有输入与 references 不被覆盖。
- `proposal.generated` 包含 `chapters`、`budget_items`、`budget_note`；`generation_sources` 映射模型来源 ID。八章 key 与顺序固定；预算金额按十进制字符串返回。
- 结构、来源 ID 和预算边界由代码检查。新增方案事实与引用审核覆盖正文、预算说明和预算项目；方案使用提示由代码生成以避免二次改写引入新事实；代码检查逐条审核覆盖、来源范围和原文摘录，并对明确归属历史案例的半径及常见平台做有限的字面核对。审核未通过、结果不完整或摘录不符均返回 503，不输出被拒绝的草稿。
- 语义审核仍由模型完成，可能误判，实际可行性和最终文案仍需业务审核；warnings 继续保留模型生成提示。资料缺失与离线预览行为保持兼容。
- 模型配置缺失、请求失败、超时、限流、输出截断/格式错误、无效引用或超预算返回 503，不伪装成正常空结果，也不静默退回离线模板。
- `--outline-only` 始终跳过云端模型。未启用 DeepSeek 时，现有接口行为保持不变。

2026-09-23 完整离线回归为 230 项通过。用户此前已完成真实 DeepSeek 基础联调；本轮修正的独立真实模型验证见 [R01/R02 修正记录](/D:/my_project/data/evaluation/case_documents/R01_R02_FIX_RESULT_20260923.md)。检索评估、模拟服务测试与生成内容验收分别记录。

### 7.1 方案生成结构化字段（R03-R09）

MerchantProfile 还可接收 store_scope、usiness_scale、seat_count、usiness_hours、data_as_of。生成方案的 generated 保留 xecution_tasks、udget_inquiries、measurement_metrics、ction_plans；其中待询价项目不使用金额 0 表示未知，指标定义由第五、八章共用。
## 项目跟踪与合作总结 Agent

SupervisorAgent 现支持 project_tracking 和 closure_report 两个意图。读取或生成动作可以通过 POST /agent/chat 调用；涉及项目、周数据或结束数据的写入时，必须在 operations_payload 中提供结构化字段，Agent 不会从自然语言猜测数值。

生成已保存项目的最新周报：

    {
      "question": "生成本周项目周报并分析风险",
      "operations_payload": {
        "action": "generate_weekly_report",
        "project_id": 1
      }
    }

生成合作总结草稿：

    {
      "question": "生成合作总结报告",
      "operations_payload": {
        "action": "generate_report",
        "closure": {
          "project_id": 1,
          "closure_date": "2026-09-30",
          "final_status": "completed",
          "completed_items": "由用户填写",
          "unresolved_items": "由用户填写"
        },
        "final_metrics": [
          {
            "name": "订单量",
            "actual_value": 100,
            "source": "经用户确认的最终报表",
            "note": "统计口径由用户填写"
          }
        ]
      }
    }

DeepSeek 启用时，ProjectTrackingAgent 和 ClosureReportAgent 只追加带来源标识的建议；指标对比、环比、完成状态和持久化仍由代码工具完成。

