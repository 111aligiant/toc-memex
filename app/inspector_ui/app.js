/* Chunk Inspector — vanilla JS, no build step. */
(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const REVIEW_KEY = "chunk-inspector-reviews-v1";
  const THEME_KEY = "chunk-inspector-theme";

  const state = {
    docs: [],
    docId: null,
    doc: null,
    nodes: [],
    chunks: [],
    issues: [],
    coverage: [],
    stats: null,
    selectedNodeId: null,
    selectedChunkId: null,
    pageFilter: null,
    pageText: null,
    pageTextPage: null,
    chunkQuery: "",
    chunkFilter: "all",
    includeSubtree: true,
    docQuery: "",
    expanded: new Set(),
    expandAll: false,
    reviews: loadReviews(),
  };

  /* ── utilities ───────────────────────────────────────── */

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function highlight(value, query) {
    const escaped = escapeHtml(value);
    const q = (query || "").trim();
    if (!q) return escaped;
    const safe = escapeHtml(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    try {
      return escaped.replace(new RegExp(`(${safe})`, "gi"), "<mark>$1</mark>");
    } catch {
      return escaped;
    }
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("zh-CN").format(value ?? 0);
  }

  function loadReviews() {
    try {
      return JSON.parse(localStorage.getItem(REVIEW_KEY) || "{}");
    } catch {
      return {};
    }
  }

  function saveReviews() {
    localStorage.setItem(REVIEW_KEY, JSON.stringify(state.reviews));
  }

  function toast(message) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.classList.remove("show"), 2200);
  }

  async function api(path) {
    const response = await fetch(path, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${response.status}`);
    }
    return response.json();
  }

  function docReview(chunkId) {
    return (state.reviews[state.docId] || {})[String(chunkId)];
  }

  function setReview(chunkId, status, note) {
    const doc = state.reviews[state.docId] || (state.reviews[state.docId] = {});
    const key = String(chunkId);
    if (!status && !note) {
      delete doc[key];
    } else {
      doc[key] = { status: status || "", note: note || "", updatedAt: new Date().toISOString() };
    }
    if (!Object.keys(doc).length) delete state.reviews[state.docId];
    saveReviews();
    renderDetail();
    renderTopStats();
    renderDocSummary();
  }

  function updateReviewNote(chunkId, note) {
    const doc = state.reviews[state.docId] || (state.reviews[state.docId] = {});
    const key = String(chunkId);
    const current = doc[key] || {};
    if (!current.status && !note) {
      delete doc[key];
    } else {
      doc[key] = { ...current, note, updatedAt: new Date().toISOString() };
    }
    if (!Object.keys(doc).length) delete state.reviews[state.docId];
    saveReviews();
    renderTopStats();
  }

  function nodeById(id) {
    return state.nodes.find((node) => node.node_id === id) || null;
  }

  function childrenOf(id) {
    return state.nodes.filter((node) => node.parent_id === id);
  }

  function isDescendant(nodeId, ancestorId) {
    let node = nodeById(nodeId);
    const seen = new Set();
    while (node && node.parent_id) {
      if (node.parent_id === ancestorId) return true;
      if (seen.has(node.node_id)) return false;
      seen.add(node.node_id);
      node = nodeById(node.parent_id);
    }
    return false;
  }

  function ensureExpanded(nodeId) {
    let node = nodeById(nodeId);
    while (node && node.parent_id) {
      state.expanded.add(node.parent_id);
      node = nodeById(node.parent_id);
    }
  }

  function selectedChunk() {
    return state.chunks.find((chunk) => chunk.id === state.selectedChunkId) || null;
  }

  function filteredChunks() {
    const query = state.chunkQuery.trim().toLowerCase();
    const nodeId = state.selectedNodeId;
    return state.chunks.filter((chunk) => {
      if (state.pageFilter !== null) {
        const start = chunk.page_start;
        const end = chunk.page_end;
        if (start === null || end === null || state.pageFilter < start || state.pageFilter > end) {
          return false;
        }
      }
      if (state.chunkFilter === "orphan" && chunk.node_id) return false;
      if (state.chunkFilter === "short" && (chunk.content || "").length >= 50) return false;
      if (state.chunkFilter === "node" && !nodeId) return false;
      if (nodeId) {
        const inNode = chunk.node_id === nodeId;
        const inSubtree = state.includeSubtree && chunk.node_id && isDescendant(chunk.node_id, nodeId);
        if (!inNode && !inSubtree) return false;
      }
      if (query) {
        const haystack = `${chunk.heading_path || ""}\n${chunk.content || ""}`.toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      return true;
    });
  }

  /* ── data loading ────────────────────────────────────── */

  async function loadDocs() {
    try {
      const data = await api("/api/documents");
      state.docs = data.documents || [];
      renderDocs();
      renderTopStats();
      const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
      const hashDoc = hash.get("doc");
      const initial = state.docs.find((doc) => doc.id === hashDoc) || state.docs[0];
      if (initial) await selectDoc(initial.id, hash.get("chunk"));
    } catch (error) {
      $("#doc-list").innerHTML = `<div class="empty"><b>无法加载数据库</b>${escapeHtml(error.message)}</div>`;
      toast(error.message);
    }
  }

  async function selectDoc(docId, chunkId = null) {
    if (state.docId === docId && state.doc) return;
    state.docId = docId;
    state.selectedNodeId = null;
    state.selectedChunkId = null;
    state.pageFilter = null;
    state.pageText = null;
    state.pageTextPage = null;
    state.chunkQuery = "";
    state.chunkFilter = "all";
    $("#chunk-search").value = "";
    $("#chunk-filter").value = "all";
    renderDocs();

    try {
      const payload = await api(`/api/documents/${encodeURIComponent(docId)}`);
      state.doc = payload.document;
      state.nodes = payload.nodes || [];
      state.chunks = payload.chunks || [];
      state.issues = payload.issues || [];
      state.coverage = payload.coverage || [];
      state.stats = payload.stats || {};
      state.expanded = new Set(
        state.nodes.filter((node) => node.level <= 1).map((node) => node.node_id)
      );
      state.expandAll = false;
      $("#expand-toggle").textContent = "全部展开";
      if (chunkId) {
        state.selectedChunkId = Number(chunkId);
        const picked = state.chunks.find((chunk) => chunk.id === state.selectedChunkId);
        if (picked?.node_id) ensureExpanded(picked.node_id);
      } else {
        const first = filteredChunks()[0];
        state.selectedChunkId = first ? first.id : null;
      }
      renderAll();
      updateHash();
    } catch (error) {
      toast(error.message);
    }
  }

  function updateHash() {
    const params = new URLSearchParams();
    if (state.docId) params.set("doc", state.docId);
    if (state.selectedChunkId) params.set("chunk", String(state.selectedChunkId));
    history.replaceState(null, "", `#${params.toString()}`);
  }

  /* ── rendering: shell ────────────────────────────────── */

  function renderTopStats() {
    const target = $("#top-stats");
    if (!state.doc) {
      target.innerHTML = `<span><b>${formatNumber(state.docs.length)}</b> 份文档</span>`;
      return;
    }
    const stats = state.stats || {};
    const reviewCount = Object.keys(state.reviews[state.docId] || {}).length;
    const parts = [
      `<span><b>${formatNumber(stats.nodes)}</b> 节点</span>`,
      `<span><b>${formatNumber(stats.chunks)}</b> 切片</span>`,
      `<span class="${stats.missing_pages ? "warn" : ""}">覆盖 <b>${formatNumber(stats.covered_pages)}/${formatNumber(stats.content_pages ?? state.doc.page_count)}</b></span>`,
      `<span class="${stats.errors ? "danger" : stats.warnings ? "warn" : ""}">校验 <b>${formatNumber(stats.errors)} 错误 · ${formatNumber(stats.warnings)} 警告</b></span>`,
      reviewCount ? `<span>复核 <b>${formatNumber(reviewCount)}</b></span>` : "",
    ].filter(Boolean);
    target.innerHTML = parts.join(`<i class="sep"></i>`);
  }

  function renderDocs() {
    const query = state.docQuery.trim().toLowerCase();
    const docs = state.docs.filter((doc) => {
      if (!query) return true;
      return `${doc.title || ""}\n${doc.filename || ""}`.toLowerCase().includes(query);
    });
    $("#doc-count").textContent = `${docs.length}/${state.docs.length}`;
    if (!docs.length) {
      $("#doc-list").innerHTML = `<div class="empty"><b>没有匹配的文档</b>调整筛选条件。</div>`;
      return;
    }
    $("#doc-list").innerHTML = docs
      .map(
        (doc) => `
        <button class="doc-item ${doc.id === state.docId ? "active" : ""}" data-doc="${escapeHtml(doc.id)}">
          <div class="doc-title">
            <span class="status-dot ${doc.status === "ready" ? "" : "error"}"></span>
            <span>${escapeHtml(doc.title || doc.filename || "未命名文档")}</span>
          </div>
          <div class="doc-meta">${formatNumber(doc.page_count)} 页 · ${formatNumber(doc.node_count)} 节点 · ${formatNumber(doc.chunk_count)} 切片</div>
        </button>`
      )
      .join("");
    for (const item of document.querySelectorAll("[data-doc]")) {
      item.addEventListener("click", () => selectDoc(item.dataset.doc));
    }
  }

  function renderAll() {
    renderTopStats();
    renderDocSummary();
    renderOutline();
    renderChecks();
    renderCoverage();
    renderChunks();
    renderDetail();
  }

  function renderDocSummary() {
    if (!state.doc) return;
    const doc = state.doc;
    const stats = state.stats || {};
    const meta = [
      doc.namespace_id && escapeHtml(doc.namespace_id),
      doc.doc_type && escapeHtml(doc.doc_type),
      doc.doc_date && escapeHtml(doc.doc_date),
      escapeHtml(doc.status || "unknown"),
      `${formatNumber(doc.page_count)} 页`,
      stats.non_content_pages ? `${formatNumber(stats.non_content_pages)} 非正文页` : "",
      `${formatNumber(stats.nodes)} 节点`,
      `${formatNumber(stats.chunks)} 切片`,
      stats.errors || stats.warnings
        ? `<span class="${stats.errors ? "danger" : "warn"}">${formatNumber(stats.errors)} 错误 · ${formatNumber(stats.warnings)} 警告</span>`
        : "校验通过",
    ].filter(Boolean);
    $("#doc-summary").innerHTML = `
      <h2>${escapeHtml(doc.title || doc.filename || "未命名文档")}</h2>
      <p class="doc-file">${escapeHtml(doc.filename || "")}</p>
      <div class="meta">${meta.map((item) => `<span>${item}</span>`).join(`<i class="sep"></i>`)}</div>`;
  }

  /* ── rendering: outline ──────────────────────────────── */

  function renderOutline() {
    const root = $("#outline");
    if (!state.nodes.length) {
      root.innerHTML = `<div class="empty">当前文档没有目录节点。</div>`;
      $("#node-count").textContent = "0";
      return;
    }
    $("#node-count").textContent = formatNumber(state.nodes.length);
    const roots = state.nodes.filter((node) => node.parent_id === null);
    root.innerHTML = roots.map((node) => treeNodeHtml(node)).join("");
    bindTreeRows(root);
  }

  function nodeTone(node) {
    if (node.issues?.some((message) => /reversed|missing parent|level jump|outside parent/i.test(message))) return "danger";
    if (node.issues?.length) return "warn";
    if (node.page_start === null) return "warn";
    if (!node.direct_chunks && !node.child_count) return "danger";
    return "";
  }

  function treeNodeHtml(node) {
    const children = childrenOf(node.node_id);
    const open = state.expandAll || state.expanded.has(node.node_id);
    const tone = nodeTone(node);
    const badges = [];
    if (node.subtree_chunks) badges.push(`<span class="tree-badge ok">${node.subtree_chunks}</span>`);
    if (tone === "warn") badges.push(`<span class="tree-badge warn">!</span>`);
    if (tone === "danger") badges.push(`<span class="tree-badge danger">!</span>`);
    const caret = children.length
      ? `<button class="tree-caret ${open ? "open" : ""}" data-toggle="${escapeHtml(node.node_id)}" aria-label="展开">
           <svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>
         </button>`
      : `<span class="tree-caret placeholder"></span>`;
    return `
      <div class="tree-node" data-node-wrap="${escapeHtml(node.node_id)}">
        <div class="tree-row ${node.node_id === state.selectedNodeId ? "selected" : ""}"
             data-node="${escapeHtml(node.node_id)}"
             title="${escapeHtml(node.title)}">
          ${caret}
          <span class="tree-number">${escapeHtml(node.heading_num || "")}</span>
          <span class="tree-title">${escapeHtml(node.title)}</span>
          <span class="tree-badges">${badges.join("")}</span>
        </div>
        ${children.length && open ? `<div class="tree-children">${children.map(treeNodeHtml).join("")}</div>` : ""}
      </div>`;
  }

  function bindTreeRows(root) {
    for (const row of root.querySelectorAll("[data-node]")) {
      row.addEventListener("click", () => {
        state.selectedNodeId = row.dataset.node;
        state.chunkFilter = "all";
        state.pageFilter = null;
        ensureExpanded(row.dataset.node);
        renderOutline();
        renderCoverage();
        renderChunks();
      });
    }
    for (const caret of root.querySelectorAll("[data-toggle]")) {
      caret.addEventListener("click", (event) => {
        event.stopPropagation();
        const id = caret.dataset.toggle;
        if (state.expanded.has(id)) state.expanded.delete(id);
        else state.expanded.add(id);
        renderOutline();
      });
    }
  }

  /* ── rendering: checks ───────────────────────────────── */

  function renderChecks() {
    const target = $("#checks");
    if (!state.doc) {
      target.innerHTML = "";
      return;
    }
    const stats = state.stats || {};
    const tiles = [
      ["无直接切片", stats.nodes_without_direct, stats.nodes_without_direct ? "warn" : ""],
      ["叶子无切片", stats.leaf_nodes_without_chunks, stats.leaf_nodes_without_chunks ? "danger" : ""],
      ["未归属切片", stats.orphan_chunks, stats.orphan_chunks ? "warn" : ""],
      ["短切片", stats.short_chunks, stats.short_chunks ? "warn" : ""],
      ["未定位节点", stats.unplaced_nodes, stats.unplaced_nodes ? "warn" : ""],
      ["缺失页", stats.missing_pages, stats.missing_pages ? "danger" : ""],
    ];
    const issues = state.issues.slice(0, 10);
    const issueCount = state.issues.length;
    target.innerHTML = `
      <div class="checks-head">
        <span class="checks-title">质量检查</span>
        <span>${issueCount ? `${formatNumber(issueCount)} 条问题` : "结构校验通过"}</span>
      </div>
      <div class="check-grid">
        ${tiles
          .map(
            ([label, value, tone]) =>
              `<div class="check-row ${tone}"><span>${escapeHtml(label)}</span><b>${formatNumber(value)}</b></div>`
          )
          .join("")}
      </div>
      ${
        issues.length
          ? `<div class="issue-list">${issues
              .map(
                (issue) => `
                <div class="issue-item ${issue.severity}" ${issue.node_id ? `data-issue-node="${escapeHtml(issue.node_id)}"` : ""}>
                  <span class="issue-dot"></span>
                  <span>${escapeHtml(issue.message)}</span>
                </div>`
              )
              .join("")}</div>`
          : ""
      }`;
    for (const item of target.querySelectorAll("[data-issue-node]")) {
      item.addEventListener("click", () => {
        state.selectedNodeId = item.dataset.issueNode;
        ensureExpanded(item.dataset.issueNode);
        renderOutline();
        renderCoverage();
        renderChunks();
      });
    }
  }

  /* ── rendering: coverage ─────────────────────────────── */

  function renderCoverage() {
    const target = $("#coverage");
    if (!state.doc) {
      target.innerHTML = "";
      $("#coverage-note").textContent = "";
      return;
    }
    const total = state.doc.page_count || 0;
    const stats = state.stats || {};
    $("#coverage-note").textContent = [
      stats.non_content_pages ? `${formatNumber(stats.non_content_pages)} 非正文页` : "",
      `${formatNumber(stats.covered_pages || 0)} 有切片`,
      `${formatNumber(stats.missing_pages || 0)} 缺失`,
    ].filter(Boolean).join(" · ");
    if (!total) {
      target.innerHTML = `<span class="muted coverage-empty">无页码信息</span>`;
      return;
    }
    target.innerHTML = state.coverage
      .map((row) => {
        const nonContent = Boolean(row.non_content);
        const covered = row.chunks.length > 0 && !nonContent;
        const classes = [
          "page-cell",
          nonContent ? "non-content" : covered ? "covered" : "missing",
        ];
        if (state.pageFilter === row.page) classes.push("selected");
        const title = nonContent
          ? `P${row.page} · 封面 / 目录（不参与切片）`
          : `P${row.page}${covered ? ` · ${row.chunks.length} 个切片` : " · 无切片"}`;
        const pageAttr = nonContent ? "" : ` data-page="${row.page}"`;
        return `<div class="${classes.join(" ")}"${pageAttr} title="${escapeHtml(title)}"></div>`;
      })
      .join("");
    for (const cell of target.querySelectorAll("[data-page]")) {
      cell.addEventListener("click", () => {
        const page = Number(cell.dataset.page);
        state.pageFilter = state.pageFilter === page ? null : page;
        renderCoverage();
        renderChunks();
      });
    }
  }

  /* ── rendering: chunks ───────────────────────────────── */

  function renderChunks() {
    const target = $("#chunk-list");
    if (!state.doc) {
      target.innerHTML = `<div class="empty">选择文档</div>`;
      return;
    }
    const chunks = filteredChunks();
    const filters = [];
    if (state.selectedNodeId) {
      const node = nodeById(state.selectedNodeId);
      if (node) filters.push(`节点: ${node.title}`);
    }
    if (state.pageFilter !== null) filters.push(`P${state.pageFilter}`);
    if (state.chunkFilter !== "all") {
      filters.push({ orphan: "未归属节点", short: "短切片", node: "当前节点" }[state.chunkFilter]);
    }
    if (state.chunkQuery.trim()) filters.push(`搜索: ${state.chunkQuery.trim()}`);
    const head = `<div class="chunk-list-header"><span>${formatNumber(chunks.length)} / ${formatNumber(state.chunks.length)} 个切片</span><span>${escapeHtml(filters.join(" · ")) || "全部"}</span></div>`;
    if (!chunks.length) {
      target.innerHTML = `${head}<div class="empty"><b>没有匹配的切片</b>调整筛选条件或清除节点选择。</div>`;
      return;
    }
    target.innerHTML =
      head +
      chunks
        .map((chunk) => {
          const node = nodeById(chunk.node_id);
          const depth = node ? Math.min(3, node.level) : 0;
          const orphan = !chunk.node_id;
          const review = docReview(chunk.id);
          const page = `P${chunk.page_start ?? "?"}${chunk.page_end !== chunk.page_start ? `–${chunk.page_end ?? "?"}` : ""}`;
          const foot = [
            page,
            `${formatNumber((chunk.content || "").length)} 字`,
            chunk.token_count ? `${formatNumber(chunk.token_count)} tok` : "",
            node ? `L${node.level}` : "",
          ].filter(Boolean);
          const marks = [
            review?.status === "pass" ? `<span class="chunk-status pass">通过</span>` : "",
            review?.status === "issue" ? `<span class="chunk-status issue">需调整</span>` : "",
            orphan ? `<span class="chunk-status orphan">无节点</span>` : "",
          ].filter(Boolean);
          return `
            <article class="chunk-row ${chunk.id === state.selectedChunkId ? "selected" : ""} ${orphan ? "orphan" : ""}"
                     data-chunk="${chunk.id}" data-depth="${depth}">
              <div class="chunk-row-top">
                <span class="chunk-index">#${chunk.chunk_index}</span>
                <span class="chunk-heading">${highlight(chunk.heading_path || node?.title || "(无标题)", state.chunkQuery)}</span>
                <span class="chunk-marks">${marks.join("")}</span>
              </div>
              <div class="chunk-preview">${highlight((chunk.content || "").slice(0, 220), state.chunkQuery)}</div>
              <div class="chunk-foot">${foot.map((item) => `<span>${item}</span>`).join(`<i class="sep"></i>`)}</div>
            </article>`;
        })
        .join("");
    for (const card of target.querySelectorAll("[data-chunk]")) {
      card.addEventListener("click", () => selectChunk(Number(card.dataset.chunk)));
    }
  }

  function selectChunk(id) {
    state.selectedChunkId = id;
    state.pageText = null;
    state.pageTextPage = null;
    updateHash();
    renderChunks();
    renderDetail();
    requestAnimationFrame(() => {
      document.querySelector(".chunk-row.selected")?.scrollIntoView({ block: "nearest" });
    });
  }

  /* ── rendering: detail ───────────────────────────────── */

  function renderDetail() {
    const target = $("#detail");
    const chunk = selectedChunk();
    if (!state.doc || !chunk) {
      target.innerHTML = `<div class="empty">选择切片查看详情</div>`;
      return;
    }
    const node = nodeById(chunk.node_id);
    const review = docReview(chunk.id) || {};
    const nodeIssues = node?.issues || [];
    const content = highlight(chunk.content || "", state.chunkQuery);
    const pageReady = state.pageTextPage === chunk.page_start && state.pageText !== null;
    const pageLabel = chunk.page_start === null
      ? "?"
      : `${chunk.page_start}${chunk.page_end !== chunk.page_start ? `–${chunk.page_end}` : ""}`;
    const meta = [
      `P${pageLabel}`,
      `${formatNumber((chunk.content || "").length)} 字`,
      `${formatNumber(chunk.token_count)} tokens`,
      node ? `L${node.level}` : "无节点",
      node ? `直接 ${formatNumber(node.direct_chunks)} · 子树 ${formatNumber(node.subtree_chunks)}` : "",
    ].filter(Boolean);
    const reviewState = review.status === "pass" ? "已通过" : review.status === "issue" ? "需调整" : "未标注";
    target.innerHTML = `
      <div class="detail-head">
        <div class="detail-title-wrap">
          <div class="detail-title">${escapeHtml(chunk.heading_path || node?.title || "未命名切片")}</div>
          <div class="detail-sub">chunk #${chunk.chunk_index} · ${escapeHtml(chunk.node_id || "无节点")}</div>
        </div>
        <div class="detail-nav">
          <button id="prev-chunk" title="上一个切片 (k)" aria-label="上一个切片">↑</button>
          <button id="next-chunk" title="下一个切片 (j)" aria-label="下一个切片">↓</button>
        </div>
      </div>
      <div class="detail-meta">${meta.map((item) => `<span>${item}</span>`).join(`<i class="sep"></i>`)}</div>
      ${
        nodeIssues.length
          ? `<div class="section-head"><span class="section-label">节点问题</span></div>${nodeIssues
              .map((message) => `<div class="issue-inline">${escapeHtml(message)}</div>`)
              .join("")}`
          : ""
      }
      <div class="section-head">
        <span class="section-label">复核</span>
        <span class="section-hint">${reviewState}</span>
      </div>
      <div class="review-bar">
        <button class="seg-btn pass ${review.status === "pass" ? "active" : ""}" data-review="pass">通过</button>
        <button class="seg-btn issue ${review.status === "issue" ? "active" : ""}" data-review="issue">需调整</button>
        <button class="seg-btn" data-review="">清除</button>
      </div>
      <textarea class="review-note" id="review-note" placeholder="记录问题或修改建议…">${escapeHtml(review.note || "")}</textarea>
      <div class="detail-actions">
        <button class="text-btn" id="copy-chunk">复制内容</button>
        <button class="text-btn" id="show-page">查看页文本</button>
        ${node ? `<button class="text-btn" id="focus-node">定位目录</button>` : ""}
      </div>
      <div class="section-head">
        <span class="section-label">切片内容</span>
        <span class="section-hint">${formatNumber((chunk.content || "").length)} 字</span>
      </div>
      <div class="detail-body">${content || "<span class='faint'>（空）</span>"}</div>
      ${pageReady ? `<div class="section-head"><span class="section-label">页面文本 P${chunk.page_start}</span><span class="section-hint">OCR</span></div><div class="page-text">${escapeHtml(state.pageText)}</div>` : ""}
    `;
    bindDetail(chunk);
  }

  function bindDetail(chunk) {
    for (const button of document.querySelectorAll("[data-review]")) {
      button.addEventListener("click", () => setReview(chunk.id, button.dataset.review, $("#review-note").value));
    }
    $("#review-note").addEventListener("input", (event) => {
      updateReviewNote(chunk.id, event.target.value);
    });
    $("#prev-chunk").addEventListener("click", () => moveChunk(-1));
    $("#next-chunk").addEventListener("click", () => moveChunk(1));
    $("#copy-chunk").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(chunk.content || "");
        toast("已复制切片内容");
      } catch {
        toast("复制失败，请手动选择");
      }
    });
    $("#show-page").addEventListener("click", () => loadPageText(chunk.page_start));
    const focus = $("#focus-node");
    if (focus) {
      focus.addEventListener("click", () => {
        state.selectedNodeId = chunk.node_id;
        ensureExpanded(chunk.node_id);
        renderOutline();
        renderCoverage();
        renderChunks();
      });
    }
  }

  function moveChunk(delta) {
    const chunks = filteredChunks();
    const index = chunks.findIndex((chunk) => chunk.id === state.selectedChunkId);
    const next = chunks[index + delta];
    if (next) selectChunk(next.id);
  }

  async function loadPageText(page) {
    if (page === null || page === undefined) {
      toast("该切片没有页码");
      return;
    }
    if (state.pageTextPage === page && state.pageText !== null) {
      state.pageText = null;
      state.pageTextPage = null;
      renderDetail();
      return;
    }
    try {
      const payload = await api(`/api/documents/${encodeURIComponent(state.docId)}/page/${page}`);
      state.pageText = payload.text || "";
      state.pageTextPage = page;
      renderDetail();
    } catch (error) {
      toast(`页文本不可用：${error.message}`);
    }
  }

  /* ── actions ─────────────────────────────────────────── */

  function bindShell() {
    $("#doc-search").addEventListener("input", (event) => {
      state.docQuery = event.target.value;
      renderDocs();
    });
    $("#chunk-search").addEventListener("input", (event) => {
      state.chunkQuery = event.target.value;
      renderChunks();
    });
    $("#chunk-filter").addEventListener("change", (event) => {
      state.chunkFilter = event.target.value;
      renderChunks();
    });
    $("#include-subtree").addEventListener("change", (event) => {
      state.includeSubtree = event.target.checked;
      renderChunks();
    });
    $("#expand-toggle").addEventListener("click", () => {
      state.expandAll = !state.expandAll;
      $("#expand-toggle").textContent = state.expandAll ? "全部折叠" : "全部展开";
      renderOutline();
    });
    $("#export-reviews").addEventListener("click", exportReviews);
    $("#theme-toggle").addEventListener("click", toggleTheme);

    document.addEventListener("keydown", (event) => {
      const tag = (event.target.tagName || "").toLowerCase();
      if (["input", "textarea", "select"].includes(tag)) {
        if (event.key === "Escape") event.target.blur();
        return;
      }
      if (event.key === "/") {
        event.preventDefault();
        $("#chunk-search").focus();
      } else if (event.key === "j") {
        moveChunk(1);
      } else if (event.key === "k") {
        moveChunk(-1);
      } else if (event.key === "p" && state.selectedChunkId) {
        setReview(state.selectedChunkId, "pass", $("#review-note")?.value || docReview(state.selectedChunkId)?.note || "");
      } else if (event.key === "x" && state.selectedChunkId) {
        setReview(state.selectedChunkId, "issue", $("#review-note")?.value || docReview(state.selectedChunkId)?.note || "");
      }
    });
  }

  function exportReviews() {
    if (!state.doc) return;
    const docReviews = state.reviews[state.docId] || {};
    const entries = Object.entries(docReviews).map(([chunkId, review]) => {
      const chunk = state.chunks.find((item) => String(item.id) === chunkId);
      return {
        chunk_id: Number(chunkId),
        chunk_index: chunk?.chunk_index ?? null,
        heading_path: chunk?.heading_path || null,
        page_start: chunk?.page_start ?? null,
        page_end: chunk?.page_end ?? null,
        status: review.status,
        note: review.note,
        updated_at: review.updatedAt,
      };
    });
    if (!entries.length) {
      toast("当前文档还没有复核记录");
      return;
    }
    const payload = {
      document: {
        id: state.doc.id,
        title: state.doc.title,
        filename: state.doc.filename,
        pages: state.doc.page_count,
      },
      exported_at: new Date().toISOString(),
      reviews: entries,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `chunk-review-${state.doc.id.slice(0, 8)}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  /* ── theme ───────────────────────────────────────────── */

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }

  function toggleTheme() {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  }

  /* ── boot ────────────────────────────────────────────── */

  function boot() {
    applyTheme(localStorage.getItem(THEME_KEY) || "light");
    bindShell();
    loadDocs();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();