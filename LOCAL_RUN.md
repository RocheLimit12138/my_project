# 本地运行指南

本文档描述 Windows 本机的稳定启动方式。项目使用 Python 3.13、uv、MySQL、FastAPI 和 Next.js；DeepSeek 配置从项目根目录的 `deepseek.local.toml` 读取。

## 1. 准备环境

在 PowerShell 中进入项目目录并同步锁定依赖：

```powershell
Set-Location D:\my_project
uv sync --locked
```

确认环境：

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m uvicorn --version
node --version
npm --version
```

安装前端锁定依赖：

```powershell
Set-Location D:\my_project\frontend
npm install
```

不要在正在运行 FastAPI 或 Next.js 时执行依赖同步。停止服务后再执行 `uv sync --locked` 或 `npm install`，避免 Windows 锁住依赖文件。

## 2. 启动 MySQL

当前本机服务名为 `MySQL84`，数据库名为 `my_project`。可用管理员 PowerShell 启动：

```powershell
Start-Service MySQL84
```

启动查询服务前设置数据库环境变量。密码不会写入项目文件：

```powershell
$env:MYSQL_HOST = "127.0.0.1"
$env:MYSQL_PORT = "3306"
$env:MYSQL_USER = "root"
$env:MYSQL_DATABASE = "my_project"
$env:MYSQL_PASSWORD = Read-Host "MySQL password" -AsSecureString | ForEach-Object {
    [System.Net.NetworkCredential]::new("", $_).Password
}
```

案例数据首次准备时执行：

```powershell
.\.venv\Scripts\python.exe scripts\import_case_data.py
.\.venv\Scripts\python.exe scripts\import_case_data.py --apply
```

案例 PDF/PPTX 索引需要先预览，确认范围后再显式写入：

```powershell
.\.venv\Scripts\python.exe -m scripts.index_case_documents
.\.venv\Scripts\python.exe -m scripts.index_case_documents --apply
```

## 3. 配置 DeepSeek

复制 `deepseek.example.toml` 为 `deepseek.local.toml`，填写真实 API Key：

```toml
[deepseek]
enabled = true
api_key = "本机真实 API Key"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-flash"
timeout_seconds = 90
max_input_chars = 120000
```

客户端不会向 DeepSeek 请求发送 `max_tokens`，由供应商和所选模型决定输出上限；旧配置中的 `max_tokens` 字段可暂时保留，但不会生效。

检查配置并执行一次真实连通检查：

```powershell
.\.venv\Scripts\python.exe -m scripts.check_deepseek
.\.venv\Scripts\python.exe -m scripts.check_deepseek --live
```

修改 DeepSeek 配置后必须重启服务。

## 4. 分别启动服务

FastAPI 默认监听 `127.0.0.1:8000`：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```

Next.js 默认监听 `127.0.0.1:3000`，并通过同源路由代理到 FastAPI：

```powershell
Set-Location D:\my_project\frontend
$env:FASTAPI_BASE_URL = "http://127.0.0.1:8000"
npm run dev -- --hostname 127.0.0.1 --port 3000
```

验证地址：

- FastAPI 健康检查：`http://127.0.0.1:8000/health`
- FastAPI 文档：`http://127.0.0.1:8000/docs`
- Next.js 网站：`http://127.0.0.1:3000`

历史案例能力需要 `MYSQL_PASSWORD`；销售话术和营销方案使用本地 BGE/Chroma 索引，营销方案生成还需要启用 DeepSeek。

## 5. 一键启动

双击项目根目录的 `start_project.cmd`。它会：

1. 检查 `.venv` 和本地配置文件；
2. 启动 FastAPI 和 Next.js 两个进程；
3. 将日志写入 `logs\fastapi.out.log`、`logs\fastapi.err.log`、`logs\frontend.out.log` 和 `logs\frontend.err.log`；
4. 等待健康检查后打开 `http://127.0.0.1:3000`。

也可以在 PowerShell 中运行：

```powershell
.\scripts\start_project.ps1
```

如需换端口：

```powershell
$env:FASTAPI_PORT = "8001"
$env:FRONTEND_PORT = "3001"
.\scripts\start_project.ps1
```

脚本不自动停止已有服务；如果端口被占用，会给出明确错误。请先关闭占用端口的服务后再启动。启动检查默认最多等待 120 秒（首次 Next.js 首页编译可能需要较长时间）；可用 `STARTUP_TIMEOUT_SECONDS` 环境变量调整为 30—600 秒。

## 6. 在线冒烟检查

启动后依次检查：

1. 历史合作案例查询和“导出详细数据”；
2. 销售话术查询；
3. 营销方案生成、PDF 下载和 PPTX 下载；
4. 清空输入、切换业务窗口和会话内追问。

营销方案若失败，先查看 `logs\frontend.err.log` 和 `logs\fastapi.err.log` 中的异常类型；日志不会记录 API Key、商家资料或模型原文。
## 售中和售后功能首次启用

“项目跟踪与周报”和“合作总结报告”使用独立 MySQL 业务表持久化项目状态，不能只依赖浏览器会话。首次使用前先预览建表范围：

    .\.venv\Scripts\python.exe -m scripts.init_operations_tracking

预览只读取 sql/operations_tracking.sql，不连接或修改 MySQL。按照 PROJECT_RULES.md 完成写入范围审核并获得用户明确确认后，才执行：

    .\.venv\Scripts\python.exe -m scripts.init_operations_tracking --apply

建表完成后重新启动 FastAPI 和 Next.js。售中和售后不使用单独外置页签，而是在主对话中选择“项目跟踪与周报”或“合作总结报告”；输入框会自动带入中文文字模板，用户补充项目 ID、日期、事项和指标后直接发送。

首次登录使用销售或审核角色。默认本机试用账号为 `sales/sales123` 和 `reviewer/reviewer123`；生产或多人试用前请设置 `SALES_USERNAME`、`SALES_PASSWORD`、`REVIEWER_USERNAME`、`REVIEWER_PASSWORD` 环境变量覆盖默认值。销售保存周数据只进入待审核表，审核员在页面的“待审核周数据”面板通过后才同步正式周度记录；销售也可以用当前输入直接生成不落库周报。

## 7. 导入 5 个售中/售后 QA 项目

QA 数据文件为 `data/operations_demo/qa_projects.json`，包含 5 个商家、每个项目 5 个指标、两期周数据和周报；其中 2 个已完成项目额外包含最终指标和合作总结。业务字段使用正常业务文案，内部来源统一标记为 `synthetic_qa_fixture_not_verified`，不能描述为已经核实的真实业绩。

预览：

\`\`\`powershell
.\.venv\Scripts\python.exe -m scripts.import_operations_demo
\`\`\`

设置数据库密码后导入（PowerShell 推荐，密码不会写入项目文件）：

\`\`\`powershell
$env:MYSQL_HOST = "127.0.0.1"
$env:MYSQL_PORT = "3306"
$env:MYSQL_USER = "root"
$env:MYSQL_DATABASE = "my_project"
$env:MYSQL_PASSWORD = Read-Host "MySQL password" -AsSecureString | ForEach-Object {
    [System.Net.NetworkCredential]::new("", $_).Password
}
.\.venv\Scripts\python.exe -m scripts.import_operations_demo --apply
\`\`\`

`--apply` 会先删除 `tracking_projects` 中编码为 `DEMO-OPS-*` 或 `QA-OPS-*` 的旧种子项目，其关联记录按外键级联删除；不会删除 `TRK-*` 用户项目，也不会修改 `merchants`、`cases` 或 `case_metrics`。

## 8. 一键启动的数据库检查

双击 start_project.cmd 时，脚本会读取 scripts/start_project.ps1 中的本机 MySQL 密码，检查 MySQL84 服务，并只读检查售中/售后 9 张表是否存在。表不完整时，脚本会停止启动网页并提示先运行 init_operations_tracking --apply。

当前脚本采用便捷本机模式，密码以明文写在脚本变量中。请不要把该脚本上传到公共仓库；如需安全模式，删除 ConfiguredMysqlPassword 的赋值，启动时会改为提示输入密码。

CMD 中设置密码时使用 set "MYSQL_PASSWORD=你的密码"，不要使用 set "MYSQL_PASSWORD"="你的密码"。

## 9. 营销方案表单输入

营销方案页不要求用户编写 JSON。选择“营销方案”后，展开“营销方案商家资料”表单，按字段填写商家基本情况、主营产品、已知问题、目标客群、目标、预算、周期和平台等信息。

- 标签中的“（必填）”表示缺失时不能提交；“（可为空）”表示可以留空，方案会标记为待补充。
- “已知问题”“商家期望目标”“意向平台”使用逐行输入，每行一项。
- 当前网页表单不再要求适用门店数量/范围、经营规模、资料截至日期、经营阶段、经营现状、经营基线、执行约束、效果衡量与复盘安排或资料来源。
- 数字字段会拒绝负数、非整数（整数项）和 NaN。
- 表单提交后由后端统一转换为 `MerchantProfile` 并校验，历史 JSON 调用仍保留兼容，不会在网页上要求用户输入 JSON。

## 10. 对话式售中与售后

- 选择“项目跟踪与周报”后，输入框会预填“动作、商家名称、日期、执行事项、风险和指标”等文字模板；项目名称和门店范围只用于同名项目辅助定位。
- 选择“合作总结报告”后，输入框会预填“结束日期、最终状态、完成事项、客户反馈、后续方向和最终指标”等文字模板。
- 指标使用 `名称=实际值；来源=数据来源；备注=备注` 格式，每行一项；后端会转换为既有结构化 Agent 输入。将动作改为“生成周报”时，可直接使用当前输入生成周报，不写入正式数据库。
- “新建对话”会创建一条新的空会话，不删除旧记录；左侧记录保存在当前浏览器的本地存储中，刷新页面后仍可恢复。删除按钮只删除所选记录。
