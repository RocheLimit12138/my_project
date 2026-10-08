# 点评销售智能助手

点评销售智能助手是一套面向销售全流程的本地智能工作台，覆盖历史合作案例查询、销售话术检索、营销方案生成、售中项目周报和售后合作总结。系统将 MySQL 中的结构化业务数据、Markdown 话术库、历史 PDF/PPTX 案例与生成模型连接起来，并保留来源、审核状态和导出边界。

当前主界面使用 Next.js，后端使用 FastAPI；LangGraph 负责工作流编排，本地 BGE 模型与 Chroma 提供语义检索，DeepSeek 用于可选的意图识别和结构化内容生成。

## 核心能力

| 模块 | 主要能力 | 主要输出 |
| --- | --- | --- |
| 历史案例 | 按城市、赛道和商家查询合作案例及指标 | 网页摘要、案例详情、CSV |
| 销售话术 | 从维护过的话术库中检索候选片段 | 原文、章节、距离和审核提示 |
| 营销方案 | 规范化商家资料，按八个章节检索并生成方案 | 网页预览、可编辑 PPTX |
| 项目跟踪 | 记录周度指标、风险和执行事项，支持审核流程 | 项目周报、可编辑 PPTX |
| 合作总结 | 汇总项目周期数据、最终状态和后续建议 | 合作总结、可编辑 PPTX |
| 案例文档 OCR | 抽取 PDF/PPTX 原生文本，并按需识别图片文字 | 可追溯的文档切片和向量索引 |

生成内容不会自动等同于已核实业务事实。系统会保留商家输入、历史来源、模拟数据标记和待审核状态，最终交付前仍需人工业务验收。

## 系统结构

```text
Next.js 工作台
    -> FastAPI 接口与认证
        -> LangGraph 工作流
            -> SupervisorAgent
                -> 案例查询 / 销售话术 / 营销方案 / 项目跟踪 / 合作总结
                    -> MySQL / BGE + Chroma / DeepSeek
                        -> 网页结果 / CSV / PPTX
```

主要技术组件：

- Python 3.13、FastAPI、Pydantic、LangGraph
- Next.js 16、React 19、TypeScript
- MySQL 8、Chroma
- Sentence Transformers、`bge-base-zh-v1.5`
- DeepSeek Chat Completions API（可选）
- PaddleOCR 独立环境（可选）
- ReportLab、python-pptx、PptxGenJS

## 目录结构

```text
agents/       业务 Agent、方案生成、引用审核和意图路由
app/          FastAPI、认证、数据库服务、导出和旧版 Gradio 界面
frontend/     Next.js 主工作台及同源 API 代理
retrieval/    话术/案例文档切片、BGE 向量化、Chroma 和 OCR
workflows/    LangGraph 对话状态和节点编排
scripts/      启动、导入、索引、查询、评估和运维命令
sql/          历史案例及售中/售后业务表结构
templates/    营销方案 Markdown 模板
data/         原始案例、示例输入、待导入数据和离线评估记录
assets/       方案展示使用的静态资源
artifacts/    项目总结等交付成果
tests/        话术检索、评估和 OCR 回归测试
```

完整逐文件说明见 [PROJECT_FILE_INVENTORY.md](PROJECT_FILE_INVENTORY.md)。

## 环境要求

- Windows 10/11 与 PowerShell
- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Node.js 20 或更高版本、npm
- MySQL 8.x
- 本地案例/话术检索需要项目中的 BGE 模型和 Chroma 数据；也可以按下文命令重建索引

## 快速开始

### 1. 安装后端依赖

```powershell
Set-Location D:\my_project
uv sync --locked
```

项目以 `pyproject.toml` 和 `uv.lock` 为权威依赖来源。不要在 FastAPI 正在使用虚拟环境时执行同步。

### 2. 安装前端依赖

```powershell
Set-Location D:\my_project\frontend
npm install
```

### 3. 配置 MySQL

启动本机 MySQL，并在启动服务的同一个 PowerShell 窗口中设置连接参数：

```powershell
$env:MYSQL_HOST = "127.0.0.1"
$env:MYSQL_PORT = "3306"
$env:MYSQL_USER = "root"
$env:MYSQL_DATABASE = "my_project"
$env:MYSQL_PASSWORD = [System.Net.NetworkCredential]::new(
    "",
    (Read-Host "MySQL password" -AsSecureString)
).Password
```

历史案例表结构位于 `sql/001_create_merchants.sql`，售中/售后表结构位于 `sql/operations_tracking.sql`。

所有数据库导入和建表命令默认只预览。先检查预览范围，并按 [PROJECT_RULES.md](PROJECT_RULES.md) 获得明确确认后，才能使用 `--apply`：

```powershell
# 预览历史案例 CSV
.\.venv\Scripts\python.exe scripts\import_case_data.py

# 预览售中/售后建表 SQL
.\.venv\Scripts\python.exe -m scripts.init_operations_tracking
```

确认后才执行对应写入：

```powershell
.\.venv\Scripts\python.exe scripts\import_case_data.py --apply
.\.venv\Scripts\python.exe -m scripts.init_operations_tracking --apply
```

### 4. 配置 DeepSeek

营销方案正文和模型辅助分析需要 DeepSeek。新建根目录 `deepseek.local.toml`：

```toml
[deepseek]
enabled = false
api_key = ""
base_url = "https://api.deepseek.com"
model = "deepseek-flash"
timeout_seconds = 90
max_input_chars = 120000
```

需要启用时，将 `enabled` 改为 `true` 并仅在本机填写密钥。该文件已被 `.gitignore` 排除，不要把真实密钥写入源码、README、终端历史或聊天记录。

检查配置：

```powershell
# 只检查本地配置，不发起计费请求
.\.venv\Scripts\python.exe -m scripts.check_deepseek

# 发送一次最小真实请求
.\.venv\Scripts\python.exe -m scripts.check_deepseek --live
```

详细配置和数据发送边界见 [DEEPSEEK_SETUP.md](DEEPSEEK_SETUP.md)。

### 5. 准备本地索引

销售话术索引来源于 `sell_talk.md`：

```powershell
.\.venv\Scripts\python.exe -m scripts.index_sales_scripts
```

案例 PDF/PPTX 索引先预览，确认后再写入 Chroma：

```powershell
.\.venv\Scripts\python.exe -m scripts.index_case_documents
.\.venv\Scripts\python.exe -m scripts.index_case_documents --apply
```

OCR 是可选能力；安装与自检方法见 [OCR_SETUP.md](OCR_SETUP.md)。

### 6. 启动服务

分别启动时，先在项目根目录运行 FastAPI：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```

然后在另一个 PowerShell 中启动 Next.js：

```powershell
Set-Location D:\my_project\frontend
$env:FASTAPI_BASE_URL = "http://127.0.0.1:8000"
npm run dev -- --hostname 127.0.0.1 --port 3000
```

访问地址：

- 工作台：<http://127.0.0.1:3000>
- FastAPI 健康检查：<http://127.0.0.1:8000/health>
- OpenAPI 文档：<http://127.0.0.1:8000/docs>

也可以运行 `start_project.cmd`，或在 PowerShell 中执行：

```powershell
.\scripts\start_project.ps1
```

一键脚本会检查环境、MySQL、售中/售后表和端口，启动 FastAPI 与 Next.js，并将日志写入 `logs/`。当前脚本属于本机便捷模式，使用前应检查其中的 `ConfiguredMysqlPassword`；不要把真实数据库密码提交到版本库。端口可通过 `FASTAPI_PORT` 和 `FRONTEND_PORT` 覆盖。

更完整的启动及故障排查说明见 [LOCAL_RUN.md](LOCAL_RUN.md)。

## 本地账号

本机试用默认账号：

| 角色 | 用户名 | 密码 |
| --- | --- | --- |
| 销售 | `sales` | `sales123` |
| 审核员 | `reviewer` | `reviewer123` |

生产或多人试用前，必须使用 `SALES_USERNAME`、`SALES_PASSWORD`、`REVIEWER_USERNAME` 和 `REVIEWER_PASSWORD` 环境变量覆盖默认值。

销售提交的周数据先进入待审核表；只有审核员通过后，数据才会同步到正式周度记录。

## 主要接口

- `GET /health`：进程健康检查，不连接 MySQL。
- `GET /cases`：查询历史合作案例。
- `GET /cases/{case_id}`：读取单条案例详情。
- `GET /cases/export`：导出案例 CSV。
- `POST /agent/chat`：统一自然语言工作流入口。
- `POST /agent/proposal/export`：导出营销方案 PPTX。
- `POST /agent/operations/export`：导出项目周报或合作总结 PPTX。
- `POST /auth/login`、`GET /auth/me`、`POST /auth/switch`、`POST /auth/logout`：认证与账号切换。
- `GET /agent/operations/pending-weekly`：审核员查看待审核周数据。
- `POST /agent/operations/pending-weekly/{submission_id}/review`：审核员通过或退回提交。

请求结构、字段约束和示例见 [API_USAGE.md](API_USAGE.md)。

## 常用开发命令

运行 Python 回归测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

检查前端：

```powershell
Set-Location D:\my_project\frontend
npm run lint
npm run build
```

查询本地话术索引：

```powershell
.\.venv\Scripts\python.exe -m scripts.query_sales_scripts "客户觉得价格太贵，怎么回复？" --top-k 3
```

查询案例文档索引：

```powershell
.\.venv\Scripts\python.exe -m scripts.query_case_documents "餐饮门店如何做新客增长？"
```

命令行生成营销方案：

```powershell
.\.venv\Scripts\python.exe -m scripts.generate_proposal "根据商家资料生成营销方案" `
    --profile data\examples\merchant_profile.json `
    --top-k 3
```

## 数据与安全边界

- `deepseek.local.toml`、`.env*`、虚拟环境、日志和前端构建目录不得提交。
- 数据库写入必须先执行预览并审核写入范围；没有明确确认时不要使用 `--apply`。
- `data/operations_demo/` 中的数据是 QA 演示数据，不能描述为已核实的真实商家业绩。
- DeepSeek 可接收当前问题、相关商家资料和检索候选；数据库密码和 API Key 不会作为提示词发送。
- 模型生成、OCR 文本、历史案例引用和业务指标仍需人工验收。
- `.chroma_db/` 是持久化向量数据库，不应把它当作普通缓存随意删除。

## 相关文档

- [LOCAL_RUN.md](LOCAL_RUN.md)：完整本地启动与故障排查
- [API_USAGE.md](API_USAGE.md)：接口和请求示例
- [DEEPSEEK_SETUP.md](DEEPSEEK_SETUP.md)：模型配置、安全和生成边界
- [OCR_SETUP.md](OCR_SETUP.md)：独立 OCR 环境和审计流程
- [PROPOSAL_WRITING_RULES.md](PROPOSAL_WRITING_RULES.md)：营销方案写作及引用规则
- [PROJECT_RULES.md](PROJECT_RULES.md)：开发和数据写入约束
- [CURRENT_STATUS.md](CURRENT_STATUS.md)：当前进度、验收记录和已知问题
- [PROJECT_FILE_INVENTORY.md](PROJECT_FILE_INVENTORY.md)：逐文件用途清单

## 当前状态

登录、历史案例查询、销售话术检索、营销方案生成、售中周报、售后总结和三类 PPTX 导出链路已经具备可运行实现。项目仍处于本地开发与验收阶段；生产部署前需要完成密钥与账号外置、HTTPS/Secure Cookie、数据库最小权限、并发与超时治理、业务数据复核以及更完整的自动化测试。
