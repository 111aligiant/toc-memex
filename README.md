# Toc Memex

**给 agent 的本地文档图书馆：目录由它重建，页码它来对齐，引用钉在原文那一页。**

Toc Memex 把一摞本地 PDF / 扫描件 / Word / Markdown / 图片变成一台可随时核对的引擎：重建目录树、把印刷页码对齐到 PDF 物理页、把检索结果钉在具体页面上。agent 通过 MCP 查库，每条结果都带章节路径和 1-based 页码——它报出的引用和 PDF 阅读器显示的完全一致。

`MIT` · `Python 3.12+` · 本地 SQLite · MCP · 无向量库

![概览：一屏看清整个馆藏](assets/overview.png)

## 目录
- [每一步都锚在具体的页上](#每一步都锚在具体的页上)
- [围绕「出处」做了一整套治理](#围绕出处做了一整套治理)
- [拿 OCR 当实验对象](#拿-ocr-当实验对象)
- [快速开始](#快速开始)
- [给 agent 用](#给-agent-用)
- [界面](#界面)
- [边界](#边界)
- [许可](#许可)

## 每一步都锚在具体的页上

普通文档 RAG 给结论，Toc Memex 给「结论 + 出处」。整条链路没有一步是模糊的：

- **建目录**：从印刷目录页、PDF 书签、Word 标题、OCR 段落标题还原章节树，把书上印的 `-47-` 对齐到 PDF 的第 53 页；缺失页和重复节点会标出来。
- **检索**：FTS5 BM25 + jieba，支持章节号直达（`8.3`）、文档号直达（`ACME 100`）、目录标题命中置顶、可配同义词表。不做向量检索。
- **记账**：阅读、引用、备注、勘误都绑定到具体页面；跨 agent 的交接条、外部会话都在同一本账上，下一位接手的人或 agent 先看到这里。

核心是通用规则：章节号、文档号、页码映射；论文、财报、国标只是不同的配置。

> **当前适配范围**：面向目录结构完整的规范文档（有书签、印刷目录页、或 Word 标题样式）。无目录结构的二手资料、散页扫描件，是后续适配的重点。

站在两个开源底座上：

- **OCR** —— 扫描件解析默认走 [PaddleOCR-VL](https://github.com/PaddlePaddle/PaddleOCR)，其结构化输出（`parsing_res_list` 的 label / bbox / order）是目录重建与页码对齐的原始依据。
- **LLM 接入** —— 通过 [magpie](https://github.com/yetone/magpie)（本地模型网关）提供低成本的模型调用方式（Toc Memex 用 `/api/endpoints/magpie/probe` 探测本机默认端口）；前端薄组件层也受它启发。

感谢这两个底座让 Toc Memex 能把「脏文档」变成「可核对的库」。

## 围绕「出处」做了一整套治理

引用只解决「能找到」，这本账还解决「能不能信」。质量信号会自动汇总——搜了但零命中的查询、被搜了很多但从来没人标为已用的文档、待人工批准的修正提案、最近被回滚的改动——凝成一个只读快照，供人或 agent 看一眼就知道哪里不对：

- 修正走**提案**而非直接改：低风险的自动落地，高风险的（改页码、合并/拆分目录节点）等人工批准；
- 每次应用都留快照，可**单步回滚**，备份是事务性的；
- 页面上每一条阅读、每一条勘误都有归属，下一位 agent 不会重蹈覆辙。

这不是「自动修库」，是「把修库这件事本身变成有记录、可回滚的过程」。

## 拿 OCR 当实验对象

同一个库内置「测试台」：同一份文档 × 不同 OCR（Paddle / Mistral / MinerU / Azure）× 不同 LLM，规则目录和 LLM 目录并排打分。对比跑在隔离目录里，不动正式库。

选型有据、有对比记录，而不是凭感觉换 provider。

## 快速开始

```bash
pipx install git+https://github.com/111aligiant/toc-memex

toc-memex init
toc-memex ingest path/to/documents/     # 一整个文件夹
toc-memex search "8.3" --top 5
toc-memex search "ACME 100" --top 5

toc-memex-web                            # 可选 Web 界面，已预构建，无需 npm
```

不想安装，直接跑 MCP：

```bash
uvx --from git+https://github.com/111aligiant/toc-memex toc-memex-mcp
```

`pip` 卡在无进度（部分 IPv6 网络）时换镜像：`--index-url https://pypi.tuna.tsinghua.edu.cn/simple`。

## 给 agent 用

把 `toc-memex-mcp` 接进 Claude Code / Cursor，默认暴露 8 个工具：
`memex_overview` · `search` · `get_toc` · `read_section` · `add_note` · `mark_used` · `activity_write` · `propose_ops`。

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

完整配置见 [`.env.example`](.env.example)。

## 界面

每一屏都对应上面的一步，操作即所见：

| 入库与文档管理 | 目录树与页码对齐 |
|---|---|
| ![文档列表](assets/library.png) | ![目录树：书签/印刷目录重建，带真实页码](assets/doc-toc.png) |

| 跨文档检索 | 正文、切片与复核 |
|---|---|
| ![检索结果：章节路径 + 页码](assets/search.png) | ![切片级核对与手记](assets/doc-read.png) |

## 边界

- 当前主要适配目录结构完整的规范文档（书签、印刷目录页、Word 标题样式）；无目录的散页、二手扫描件是后续适配重点，不做虚假承诺。
- Excel / CSV / TSV 暂未开放（Web 与 CLI 均已在分发点拒绝，属后续适配）。
- 无向量检索：同义换词会漏；跨语言可用查询侧双语词表（`TOCMEMEX_SEARCH_SYNONYMS`）在配置层面解锁，默认不预设领域词。
- OCR 默认走云端 PaddleOCR-VL（本地 / 私有 provider 可用）；`TOCMEMEX_OCR_ALLOW_PRIVATE_HOSTS=1` 放开内网端点。
- DOCX/DOC 直读走 Word COM，仅 Windows；没有 Word 就先把文档转 PDF。
- 服务无登录，默认只绑 `127.0.0.1`；对外暴露前看 `TOCMEMEX_ALLOW_NONLOCAL` 的说明。
- 数据都在本地：SQLite（`data/tocmemex.db`）+ 数据目录；`scripts/backup.py` 可事务性备份两者。

## 工程底数

- 应用约 3.2 万行、测试约 2.4 万行（912 项测试全绿，80 个测试文件）——在没有向量库的约束下，把「出处可核对」这一件事做扎实。
- 本地优先：一次 `pipx install` 拿到 CLI + MCP + Web，前端无第三方 UI 依赖。

## 许可

MIT License，见 [`LICENSE`](LICENSE)。第三方代码保留自身许可——`vendor/pageindex/` 为 MIT（Vectify AI），不重新授权。