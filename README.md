# 🦉 Toc Memex

Toc Memex 把本地 PDF、扫描件、Word 与 Markdown 编成一个可核对的馆藏：目录还原到小节，印刷页码对齐到 PDF 物理页，检索结果、引用、批注与勘误都落在同一个地址上（`doc_id → node_id → page`）。用于交付物需要原文与页码的场景：标准、法规、合同、专利、检测报告、审计底稿。索引与记录留在本地 SQLite，原始文件与 OCR 产物留在数据目录。

`Python 3.12 / 3.13` · `本地 SQLite + FTS5` · `CLI / MCP / Web` · `无向量库`

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) [![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

![图标：本地目录图书馆](assets/icon.png)

![概览：馆藏计数、待处理问题、任务与最近手记](assets/overview.png)

---

## 馆藏结构与检索

文档入库后，章节树从 PDF 书签、印刷目录页、Word 标题样式或 OCR 段落标题重建，印刷页码映射到 1-based 的 PDF 物理页。检索与阅读接口统一返回 `doc_id`、`node_id`、`heading_path`、`pdf_page_start` / `pdf_page_end`。`read_section` 另给出 `citation_confidence`（`high` / `page` / `low`），按页码兜底时附带 `citation_note`；返回里包含该节已确认的勘误，以及挂到覆盖切片上的图 / 表资源。缺页、重复标题、短切片、页眉页脚混入、标题与声明页不符等结构问题在入库与巡检时记入质量信号。

检索走 SQLite FTS5 BM25 + jieba，打分之前有确定性路由：章节号（`8.3`）、文档号（`ACME 100`）、分区过滤、查询中逐字出现的目录标题。没有 embedding、reranker 或 LLM 查询改写；请求 `semantic` / `hybrid` 时按词法检索执行，并在返回里置 `degraded=true` 与 `mode_note`。同义词和跨语言词表由 `TOCMEMEX_SEARCH_SYNONYMS` 配置，默认不启用。

## 派生层的治理闭环

| 层 | 内容 | 写入规则 |
|---|---|---|
| 原文层 | PDF / 图片 / Word 文件 | 永不修改 |
| 派生层 | OCR 文本、TOC 节点与页码、切片边界与内容、元数据 | 经提案修改，带快照与回滚 |
| 记录层 | 阅读、引用、批注、勘误、校勘记、交接 | 只追加，修正以新记录追加 |

治理动作只作用于派生层。修正通过 `propose_ops` 提交：`correct_text`、`merge_chunks`、`split_chunk`、`drop_chunk` 等低风险操作可自动落地，`set_page`、`merge_nodes`、`split_node` 列为高风险、等待人工批准。每次应用前写快照，复测变差自动回滚（`auto_rollback`），之后仍可按提案 `undo`。`list_quality_signals` 提供只读的跨文档快照，把零命中查询、被反复检索却从未 `mark_used` 的文档、待批提案、近期自动回滚与勘误计数汇总在一处；编修 agent、MCP 与 `python -m app.curator signals` 拿到同一份 JSON。

## 挂在源位置上的记录

`activity`、`agent_notes`、`errata`、`usage_log`、`tasks` 以 `doc_id / node_id / page` 为锚点，不绑定会话。勘误生命周期是 `proposed → confirmed / rejected`，确认需要第二个 agent 独立复核，修正不覆盖旧记录而是追加新行。`task_timeline` 把同一任务的读写、批注与勘误合成一条时间线；`add_note` 与 `activity_write` 写入的交接和勘误会出现在下一次会话的检索与阅读结果里。未经 `confirmed` 的勘误不进入检索返回，`used / unused` 由 `mark_used` 单独记录。

## 评测记录

本地 round A 评测为 5 份文档、16 道评分题，题目与期望保存在 `benchmarks/papers/`。`benchmarks/` 与 `docs/` 按 `.gitignore` 不随公开仓库分发，仓库内不含 PDF 与语料。

| 指标（2026-11-23 首轮） | 值 |
|---|---|
| 题目 / 文档 | 16 / 5 |
| hit@k（k=5） | 68.75% |
| primary@1 | 25.00% |
| page@1 | 37.50% |
| MRR | 0.5156 |
| citation_page_rate | 100.00% |
| citation_leak_rate | 0.00% |
| ask_page_rate | 62.50% |

配置查询侧双语词表后，同一组 16 题的 hit@k 升至 87.50%；单独隔离的 4 道跨语言题从 0/4 到 3/4。首轮的 5 次未命中都来自中文提问查英文文档；无书签也无印刷目录的 5 页期刊文章目录退化为单个 root；`4.2` 这类章节号在不同文档间重号时仍会撞车。

Web 的测试台支持同一份文档切换 OCR provider（Paddle / Mistral / MinerU / Azure）与 LLM，把规则目录和 LLM 目录并排打分，对照跑在隔离目录，不写正式库。

## 快速开始

安装后提供三个入口：`toc-memex`（CLI）、`toc-memex-mcp`（MCP）、`toc-memex-web`（Web，前端产物随包分发）。

```bash
pipx install git+https://github.com/111aligiant/toc-memex

toc-memex init                                   # 建库
toc-memex ingest path/to/document.pdf           # 入库一篇文档
toc-memex search "8.3" --top 5                  # 章节号直达，返回带页码
toc-memex-web                                    # 启动 Web 界面
```

不安装也可以直接启动 MCP server：

```bash
uvx --from git+https://github.com/111aligiant/toc-memex toc-memex-mcp
```

`pip` 卡在无进度（部分 IPv6 网络）时换镜像：`--index-url https://pypi.tuna.tsinghua.edu.cn/simple`。

## MCP 接入

把 `toc-memex-mcp` 接进 Claude Code / Cursor。默认工具面 8 个：`memex_overview` · `search` · `get_toc` · `read_section` · `add_note` · `mark_used` · `activity_write` · `propose_ops`；`TOCMEMEX_TOOLS=full` 展开治理、档案与页面级工具。

```json
{
  "mcpServers": {
    "toc-memex": {
      "command": "toc-memex-mcp",
      "env": { "TOCMEMEX_DB_PATH": "C:/your/data/tocmemex.db" }
    }
  }
}
```

其余配置见 [`.env.example`](.env.example)。

## Web 界面

| 知识库列表 | 目录树与页码 |
|---|---|
| ![文档列表：页数、节点数、切片数与覆盖率](assets/library.png) | ![目录树：章节节点、PDF 页码、节点来源与置信度](assets/doc-toc.png) |

| 跨文档检索 | 正文与切片复核 |
|---|---|
| ![检索结果：章节路径、页码与 BM25 分值](assets/search.png) | ![正文、切片详情与逐条复核](assets/doc-read.png) |

## 实现与规模

- Python 3.12 / 3.13；SQLAlchemy + aiosqlite + SQLite FTS5；FastAPI；pypdfium2 / pdfplumber 读 PDF；jieba 分词；前端为预构建静态产物。
- `app/` 约 3.26 万行、`tests/` 约 2.28 万行，79 个测试文件。本地全量 `pytest -q`（2026-11-25）：`898 passed, 1 skipped`。
- 文本层文档的抽取、建目录与检索不调用外部 API，也没有按查询计费；扫描件 OCR 与治理裁决用的 LLM 为可选外部调用。
- 索引与记录落在 `data/tocmemex.db` 与数据目录，`scripts/backup.py` 事务性备份两者。

## 适用范围与限制

- 主要适配目录结构完整的规范文档（PDF 书签、印刷目录页或 Word 标题样式）。无目录的散页扫描件与二手资料适配有限。
- Excel / CSV / TSV 在 CLI 与 Web 的分发点拒绝入库。
- 词法检索对同义换词召回弱；跨语言检索依赖 `TOCMEMEX_SEARCH_SYNONYMS` 配置，词表过宽会把英文全文排到中文目标章节之前。
- OCR 默认走云端 PaddleOCR-VL，可换本地或私有 provider，`TOCMEMEX_OCR_ALLOW_PRIVATE_HOSTS=1` 放开内网端点。
- DOCX / DOC 直读依赖 Word COM（仅 Windows）；没有 Word 时先转 PDF。
- Web 服务无登录，默认只绑 `127.0.0.1`；对外暴露前看 `TOCMEMEX_ALLOW_NONLOCAL` 的说明。
- 单机 SQLite，未做多租户、权限与团队协作。

## 致谢

站在这些开源工具的肩膀上：

- **OCR** —— [PaddleOCR-VL](https://github.com/PaddlePaddle/PaddleOCR) 提供结构化输出（`parsing_res_list` 的 label / bbox / order），是目录重建与页码对齐的原始依据。
- **LLM 接入** —— [magpie](https://github.com/yetone/magpie) 本地模型网关提供低成本模型调用，Toc Memex 用 `/api/endpoints/magpie/probe` 探测本机默认端口。
- **目录结构处理** —— [PageIndex](https://github.com/VectifyAI/PageIndex)（MIT）。书签/目录解析的逻辑借鉴它，见 [`vendor/pageindex/`](vendor/pageindex/NOTICE.md)。
- **离线 PDF 引擎** —— [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)（PDFium 绑定）读 PDF 书签、提文本、渲染；[pdfplumber](https://github.com/jsvine/pdfplumber) 提供表格与版面矩形。

## 贡献

欢迎提交 issue 与 PR。目录重建失败的报告请尽量附上真实文档（可公开或脱敏可复现）；也接受 bug 修复、测试补充与 provider 适配。

测试在 `tests/`：`pip install -e ".[dev]" && python -m pytest -q`。

## 许可

MIT License，见 [`LICENSE`](LICENSE)。第三方代码保留自身许可——`vendor/pageindex/` 为 MIT（Vectify AI），不重新授权。
