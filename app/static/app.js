/* Address Matcher - vanilla JS front end. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const STATUS_LABEL = {
    auto_matched: "自动命中",
    needs_review: "待确认",
    no_match: "无匹配",
    reviewed: "已复核",
};

const DEMO_QUERIES = [
    "123 Main Street, New York, NY 10001",
    "1000 Lake Dr, Austin, TX",
    "45 Hillcrest Rd, Dallas, Texas 75201",
    "99 SW 26th Ave Unit 7B, Portland OR",
    "北京東路８８號 3楼, 上海",
    "742 Evergreen Tce, Springfield",
].join("\n");

// ------------------------------------------------------------------ helpers

function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

async function api(path, options = {}) {
    const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
    return data;
}

function pct(value) {
    return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

// ------------------------------------------------------------------- render

function renderTokens(cand) {
    // Group token chips by component so street and locality hits are distinct.
    const groups = [
        { key: "street_tokens", label: "道路", tokens: cand.street_tokens },
        { key: "locality_tokens", label: "地区", tokens: cand.locality_tokens },
    ];
    const rendered = new Set();
    const parts = [];
    groups.forEach((group) => {
        if (!group.tokens) return;
        const exact = new Set(group.tokens.matched.map((m) => m.candidate));
        group.tokens.matched.forEach((m) => {
            const cls = m.similarity >= 0.999 ? "tok" : "tok fuzzy";
            const title = `${group.label}${m.similarity >= 0.999 ? "完全命中" : "近似命中"}: “${m.query}” → “${m.candidate}” (${pct(m.similarity)})`;
            rendered.add(m.candidate);
            parts.push(`<span class="${cls}" title="${escapeHtml(title)}">${escapeHtml(m.candidate)}</span>`);
        });
        group.tokens.query_only.forEach((t) => {
            parts.push(`<span class="tok miss-q" title="${group.label}：仅查询中出现">${escapeHtml(t)}</span>`);
        });
        group.tokens.candidate_only.forEach((t) => {
            if (rendered.has(t)) return;
            parts.push(`<span class="tok miss-c" title="${group.label}：仅标准地址中出现">${escapeHtml(t)}</span>`);
            rendered.add(t);
        });
    });
    return `<div class="tokens">${parts.join("") || '<span class="muted">无命中词元</span>'}</div>`;
}

function renderConflicts(conflicts) {
    if (!conflicts || conflicts.length === 0) return "";
    // Deduplicate identical messages.
    const seen = new Set();
    return conflicts.filter((c) => {
        const key = `${c.field}:${c.query}:${c.candidate}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    }).map((c) => `
        <div class="conflict">
            ⚠ ${escapeHtml(c.message)}：查询 <b>${escapeHtml(c.query)}</b>
            ↔ 候选 <b>${escapeHtml(c.candidate)}</b>
            （相似度 ${pct(c.similarity)}）
        </div>`).join("");
}

function renderCandidate(cand, rank, opts = {}) {
    const a = cand.address;
    const bestCls = rank === 0 ? " best" : "";
    const actionAttrs = opts.reviewable
        ? `data-log="${opts.logId}" data-address="${a.id}"`
        : "";
    return `
    <div class="candidate${bestCls}">
        <div class="addr">#${rank + 1} ${escapeHtml(a.full_text)}</div>
        <div class="meta">
            标准地址 ID ${a.id}${a.external_id ? ` · 外部编号 ${escapeHtml(a.external_id)}` : ""}
            ${a.house ? ` · 门牌号 ${escapeHtml(a.house)}` : ""}
        </div>
        <div class="scores">
            <span class="score-chip">综合 <b>${pct(cand.score)}</b></span>
            <span class="score-chip">道路词元 <b>${pct(cand.street_f1)}</b></span>
            <span class="score-chip">地区词元 ${pct(cand.locality_f1)}</span>
            <span class="score-chip">门牌号 <b>${pct(cand.house_score)}</b></span>
        </div>
        ${renderTokens(cand)}
        ${renderConflicts(cand.conflicts)}
        ${opts.reviewable ? `
        <div class="review-actions">
            <button class="tiny confirm candidate-confirm" ${actionAttrs}>确认此候选</button>
            ${rank !== 0 ? `<button class="tiny override candidate-override" ${actionAttrs}>改派为该候选</button>` : ""}
        </div>` : ""}
    </div>`;
}

function renderResult(item, opts = {}) {
    const candidates = (item.candidates || [])
        .map((c, i) => renderCandidate(c, i, opts))
        .join("");
    return `
    <div class="result">
        <div class="query-line">🔍 ${escapeHtml(item.query)}</div>
        <div class="norm">
            归一化：${escapeHtml(item.normalized.text || "（空）")}
            · 门牌号 <b>${escapeHtml(item.normalized.house || "无")}</b>
            ${item.match_id ? ` · 记录 #${item.match_id}` : ""}
        </div>
        <span class="status ${item.status}">${STATUS_LABEL[item.status] || item.status}</span>
        <span class="muted">${escapeHtml(item.reason)}</span>
        ${item.runner_up_id ? `<div class="muted" style="margin-top:4px">次高分候选 #${item.runner_up_id}：${pct(item.runner_up_score)}（领先 ${pct((item.best_score ?? 0) - (item.runner_up_score ?? 0))}）</div>` : ""}
        ${candidates || '<div class="meta muted">标准地址表中没有任何候选</div>'}
        ${opts.reviewable && item.candidates.length > 0 ? `
        <div class="review-actions">
            <button class="tiny reject candidate-reject" data-log="${item.match_id}">标记为无匹配</button>
        </div>` : ""}
    </div>`;
}

// -------------------------------------------------------------------- tabs

$$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
        $$(".tab").forEach((b) => b.classList.remove("active"));
        $$(".panel").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        $(`#tab-${btn.dataset.tab}`).classList.add("active");
        if (btn.dataset.tab === "table") loadAddresses();
        if (btn.dataset.tab === "review") loadReview();
    });
});

// ------------------------------------------------------------------ matching

$("#btn-demo").addEventListener("click", () => { $("#query").value = DEMO_QUERIES; });

$("#btn-match").addEventListener("click", async () => {
    const raw = $("#query").value.trim();
    if (!raw) return;
    const queries = raw.split("\n").map((q) => q.trim()).filter(Boolean);
    const btn = $("#btn-match");
    btn.disabled = true;
    $("#results").innerHTML = '<p class="muted">匹配中…</p>';
    try {
        if (queries.length === 1) {
            const item = await api("/api/match", {
                method: "POST",
                body: JSON.stringify({ query: queries[0], save: true }),
            });
            $("#results").innerHTML = renderResult(item, {
                reviewable: item.status === "needs_review",
            });
            $("#match-summary").textContent = "";
        } else {
            const data = await api("/api/match/batch", {
                method: "POST",
                body: JSON.stringify({ queries, save: true }),
            });
            const s = data.summary;
            $("#match-summary").innerHTML =
                `<span class="muted">共 ${s.total} 条：自动命中 ${s.auto_matched}，` +
                `待确认 ${s.needs_review}，无匹配 ${s.no_match}</span>`;
            $("#results").innerHTML = data.results
                .map((item) => renderResult(item, { reviewable: item.status === "needs_review" }))
                .join("");
        }
        refreshHealth();
    } catch (err) {
        $("#results").innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    } finally {
        btn.disabled = false;
    }
});

// Delegate review clicks from freshly rendered match results.
$("#results").addEventListener("click", onReviewClick);

async function onReviewClick(event) {
    const btn = event.target.closest("button[data-log]");
    if (!btn) return;
    const logId = Number(btn.dataset.log);
    let body;
    if (btn.classList.contains("candidate-reject")) {
        body = { action: "rejected" };
    } else {
        body = {
            action: btn.classList.contains("candidate-override") ? "overridden" : "confirmed",
            selected_id: Number(btn.dataset.address),
        };
    }
    btn.disabled = true;
    try {
        await api(`/api/matches/${logId}/review`, { method: "POST", body: JSON.stringify(body) });
        btn.textContent = "✓ 已记录";
        btn.classList.add("confirm");
        await refreshHealth();
    } catch (err) {
        btn.disabled = false;
        alert(err.message);
    }
}

// ----------------------------------------------------------- address table

async function loadAddresses() {
    const wrap = $("#address-table-wrap");
    wrap.innerHTML = '<p class="muted">加载中…</p>';
    try {
        const data = await api("/api/addresses?limit=200");
        if (data.items.length === 0) {
            wrap.innerHTML = '<p class="muted">标准地址表为空，请上传 CSV 或加载示例数据。</p>';
            return;
        }
        const rows = data.items.map((a) => `
            <tr>
                <td>${a.id}</td>
                <td>${escapeHtml(a.full_text)}</td>
                <td>${escapeHtml(a.house || "")}</td>
                <td>${escapeHtml(a.postal_code || "")}</td>
                <td>${escapeHtml(a.external_id || "")}</td>
            </tr>`).join("");
        wrap.innerHTML = `
            <table>
                <thead><tr><th>ID</th><th>标准地址</th><th>门牌号</th><th>邮编</th><th>外部编号</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
            <p class="muted">共 ${data.total} 条${data.total > 200 ? "（仅显示前 200 条）" : ""}</p>`;
    } catch (err) {
        wrap.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-refresh-table").addEventListener("click", loadAddresses);

$("#btn-sample").addEventListener("click", async () => {
    try {
        const s = await api("/api/addresses/load-sample", { method: "POST" });
        $("#table-msg").innerHTML =
            `<span class="ok-msg">新增 ${s.inserted} 条，跳过重复 ${s.duplicates} 条</span>`;
        loadAddresses();
        refreshHealth();
    } catch (err) {
        $("#table-msg").innerHTML = `<span class="err-msg">${escapeHtml(err.message)}</span>`;
    }
});

$("#csv-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
        const res = await fetch("/api/addresses/upload", { method: "POST", body: form });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "上传失败");
        $("#table-msg").innerHTML =
            `<span class="ok-msg">${escapeHtml(file.name)}：新增 ${data.inserted} 条，` +
            `重复 ${data.duplicates} 条，无效 ${data.invalid} 条</span>`;
        loadAddresses();
        refreshHealth();
    } catch (err) {
        $("#table-msg").innerHTML = `<span class="err-msg">${escapeHtml(err.message)}</span>`;
    } finally {
        event.target.value = "";
    }
});

// ------------------------------------------------------------- review queue

async function loadReview() {
    const status = $("#review-filter").value;
    const list = $("#review-list");
    list.innerHTML = '<p class="muted">加载中…</p>';
    try {
        const data = await api(`/api/matches?status=${encodeURIComponent(status)}&limit=100`);
        if (data.items.length === 0) {
            list.innerHTML = '<p class="muted">暂无记录。</p>';
            return;
        }
        list.innerHTML = data.items.map((item) => {
            const best = item.candidates[0];
            const candidateHtml = item.candidates.length
                ? item.candidates.map((c, i) => renderCandidate(c, i, {
                    reviewable: item.status === "needs_review",
                    logId: item.id,
                })).join("")
                : '<div class="meta muted">无候选</div>';
            return `
            <div class="review-item">
                <div class="query-line">🔍 ${escapeHtml(item.query)}</div>
                <div class="muted">记录 #${item.id} · ${escapeHtml(item.created_at || "")}</div>
                <span class="status ${item.status}">${STATUS_LABEL[item.status] || item.status}</span>
                ${item.review_action ? `<span class="muted">复核动作：${escapeHtml(item.review_action)}</span>` : ""}
                ${candidateHtml}
                ${item.status === "needs_review" ? `
                <div class="review-actions">
                    <button class="tiny reject candidate-reject" data-log="${item.id}">标记为无匹配</button>
                </div>` : ""}
            </div>`;
        }).join("");
    } catch (err) {
        list.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-refresh-review").addEventListener("click", loadReview);
$("#review-filter").addEventListener("change", loadReview);
$("#review-list").addEventListener("click", async (event) => {
    await onReviewClick(event);
    if (event.target.closest("button[data-log]")) loadReview();
});

// ------------------------------------------------------------------ health

async function refreshHealth() {
    try {
        const h = await api("/api/health");
        $("#health").textContent = `标准地址 ${h.address_count} 条 · 待确认 ${h.needs_review}`;
        $("#health").className = "badge ok";
        $("#addr-count").textContent = h.address_count;
        $("#review-count").textContent = h.needs_review;
    } catch (err) {
        $("#health").textContent = "服务不可用";
        $("#health").className = "badge bad";
    }
}

refreshHealth();
