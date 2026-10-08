# 本地 PaddleOCR 接入

项目采用“原生解析优先、OCR 按需补充”的离线入库方式，不使用 MinerU。OCR
不会在聊天查询时运行，也不会修改原始 PDF、PPTX 或 MySQL 数据。

## 1. 安装独立 OCR 环境

在 PowerShell 中执行：

```powershell
cd D:\my_project
.\scripts\setup_ocr.ps1
```

脚本创建 `.venv-ocr`（默认 Python 3.11），并安装 `ocr-requirements.txt`
中的 PaddlePaddle CPU 版和 PaddleOCR。它不会修改项目主 `.venv`。

首次实际识别时，PaddleOCR 可能需要联网下载模型。完成后模型保存在本机缓存中。

## 2. PDF 渲染器

扫描 PDF 页需要先渲染为 300 DPI PNG。安装 Poppler 后，将 `pdftoppm.exe`
加入 PATH，或设置：

```powershell
$env:PDFTOPPM_PATH = "C:\path\to\pdftoppm.exe"
```

也可以在索引命令中传入 `--pdftoppm`。PPTX 内嵌图片 OCR 不需要 Poppler。

## 3. 仅预览

```powershell
.\.venv\Scripts\python.exe -m scripts.index_case_documents --ocr auto
```

预览会显示每份文档的 OCR 页数、OCR 新增字符数、低置信度页数和源文件
SHA-256，但不会写入 Chroma。

需要先验证单个文件时，可重复使用 `--document`：

```powershell
.\.venv\Scripts\python.exe -m scripts.index_case_documents `
  --document "data\case_documents\川湘菜火锅-阿嫂印象小红书BF.pptx" `
  --ocr auto
```

需要逐页审核 OCR 新增文字时，输出 Markdown 或 JSON 报告：

```powershell
.\.venv\Scripts\python.exe -m scripts.index_case_documents `
  --document "data\case_documents\川湘菜火锅-阿嫂印象小红书BF.pptx" `
  --ocr auto `
  --ocr-audit-output "data\evaluation\case_documents\ocr_audit_阿嫂.md"
```

报告包含页码/幻灯片号、OCR 新增文字、平均置信度、原生/OCR 字符数和低置信度标记；生成报告不会写入 Chroma。

OCR 模式：

- `auto`：PDF 仅识别原生有效文字少于 30 字的页面；PPTX 在原生文字少于
  8 字，或所有可见栅格图片合计覆盖至少 50% 幻灯片时识别页面图片。
  完全位于幻灯片画布外的图片不会参与面积计算或 OCR。选中的可见图片会按
  实际位置和裁剪状态合成为 1920 像素宽的幻灯片图片层，再统一识别。
- `always`：所有 PDF 页面均 OCR；PPTX 的所有可见栅格图片均 OCR。
- `never`：完全保持旧的原生解析方式。

可调整参数：

```powershell
.\.venv\Scripts\python.exe -m scripts.index_case_documents `
  --ocr auto `
  --ocr-min-confidence 0.5 `
  --ocr-min-native-characters 30 `
  --ocr-pptx-min-native-characters 8 `
  --ocr-pptx-image-min-coverage 0.5 `
  --ocr-pdf-dpi 300
```

## 4. 确认后写入索引

核对预览范围及识别质量后再执行：

```powershell
.\.venv\Scripts\python.exe -m scripts.index_case_documents --ocr auto --apply
```

写入范围仅为 `case_documents` collection 中对应源文件的片段；不会修改
`sales_scripts`、MySQL 或原始文件。OCR 缓存位于 `.ocr_cache`，缓存键包含图片
内容、OCR/Paddle 版本、语言和协议版本。

首次识别未缓存的 PPTX 可能需要数分钟。Worker 每批最多处理 4 张图片，并在
每批成功后立即保存缓存；后续相同内容的预览会明显加快。

## 5. 识别边界

- 原生文字、原生 PPTX 表格和图表数据优先，OCR 不覆盖它们。
- 置信度低于阈值的文字不会进入索引。
- 与原生文字高度相似的 OCR 行会去重。
- PPTX OCR 使用幻灯片画布内的可见栅格图片，避免识别编辑区中画布外的备用图。
- 普通 OCR 不保证恢复复杂多栏、表格结构或图片图表中的数据关系。
- WMF/EMF 等矢量图片当前不识别；需要先转换为常见栅格格式。
