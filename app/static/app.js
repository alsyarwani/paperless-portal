"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString("id-ID"));

let INST = [];
let currentJob = null, jobTimer = null, lastItemId = 0, jobStatusFilter = "";

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  const o = { headers: { "X-Portal": "1" }, ...opts };
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("belum login"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data?.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : r.statusText);
  return data;
}
function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 4500);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { if (e.message !== "belum login") toast(e.message, true); } };

/* ------------------------------------------------------------------ auth & nav */
function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); }
async function boot() {
  try { await api("/api/me"); } catch { return; }
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  await loadInstances();
  go(location.hash.slice(1) || "dash");
}
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault(); $("#loginErr").textContent = "";
  const f = new FormData(e.target);
  try { await api("/api/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } }); boot(); }
  catch (err) { $("#loginErr").textContent = err.message; }
});
$("#logout").onclick = guard(async () => { await api("/api/logout", { method: "POST" }); showLogin(); });

function go(page, arg) {
  if (page.startsWith("job-")) { arg = page.slice(4); page = "job"; }
  $$(".page").forEach((p) => p.classList.add("hidden"));
  $("#page-" + page)?.classList.remove("hidden");
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page || (page === "job" && a.dataset.page === "jobs")));
  if (page !== "job") { clearInterval(jobTimer); currentJob = null; }
  history.replaceState(null, "", "#" + (page === "job" ? "job-" + arg : page));
  ({ dash: loadDash, search: initSearch, migrate: initMigrate, jobs: loadJobs, job: () => openJob(+arg),
     check: initCheck, tasks: initTasks, settings: loadSettings })[page]?.();
}
$("#nav").addEventListener("click", (e) => { if (e.target.dataset.page) go(e.target.dataset.page); });
document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) go(g.dataset.goto); });

async function loadInstances() { INST = await api("/api/instances"); }
const instName = (id) => INST.find((i) => i.id === id)?.name ?? "?";
function instOptions(sel, withEmpty) {
  sel.innerHTML = (withEmpty ? `<option value="">— pilih —</option>` : "") + INST.map((i) => `<option value="${i.id}">${esc(i.name)}</option>`).join("");
}
function instChips(box, key) {
  const saved = JSON.parse(localStorage.getItem(key) || "null");
  box.innerHTML = INST.map((i) => `<span class="chip ${!saved || saved.includes(i.id) ? "on" : ""}" data-id="${i.id}">${esc(i.name)}</span>`).join("");
  box.onclick = (e) => {
    const c = e.target.closest(".chip"); if (!c) return; c.classList.toggle("on");
    try { localStorage.setItem(key, JSON.stringify(chosen(box))); } catch {}
  };
}
const chosen = (box) => $$(".chip.on", box).map((c) => +c.dataset.id);

/* ------------------------------------------------------------------ dasbor */
const loadDash = guard(async () => {
  if (!INST.length) { $("#dashCards").innerHTML = `<div class="panel">Belum ada instance. Tambahkan di <a data-goto="settings" href="#">Pengaturan</a>.</div>`; $("#dashTotals").innerHTML = ""; return; }
  $("#dashCards").innerHTML = INST.map((i) => `<div class="card"><h3><span class="dot"></span>${esc(i.name)}</h3><div class="muted">memuat…</div></div>`).join("");
  const data = await api("/api/dashboard");
  const on = data.filter((d) => d.online);
  const sum = (k) => on.reduce((a, d) => a + (d[k] || 0), 0);
  $("#dashTotals").innerHTML = [
    ["Instance online", `${on.length}/${data.length}`], ["Total dokumen", fmt(sum("documents_total"))],
    ["Di inbox", fmt(sum("documents_inbox"))], ["Task gagal", fmt(sum("failed_tasks"))],
  ].map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
  $("#dashCards").innerHTML = data.map((d) => `
    <div class="card">
      <h3><span class="dot ${d.online ? "on" : "off"}"></span>${esc(d.name)} ${d.online ? `<span class="badge">v${esc(d.version)}</span>` : `<span class="badge bad">offline</span>`}</h3>
      <div class="url">${esc(d.url)}</div>
      ${d.online ? `<div class="kv">
        <div><span>Dokumen</span><b>${fmt(d.documents_total)}</b></div>
        <div><span>Inbox</span><b>${fmt(d.documents_inbox)}</b></div>
        <div><span>Task gagal</span><b>${d.failed_tasks ? `<a href="#" data-tasks="${d.id}">${fmt(d.failed_tasks)}</a>` : fmt(d.failed_tasks)}</b></div>
        <div><span>Respons</span><b>${fmt(d.latency_ms)} ms</b></div>
      </div>
      <div class="small muted">${(d.file_types || []).slice(0, 4).map((f) => `${esc(f.mime_type?.split("/").pop())}: ${fmt(f.mime_type_count)}`).join(" · ")}</div>`
      : `<div class="err small">${esc(d.error)}</div>`}
      <div><a class="btn small" href="${esc(d.url)}" target="_blank" rel="noopener">Buka Paperless ↗</a></div>
    </div>`).join("");
  $$("[data-tasks]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); localStorage.setItem("tasksInst", a.dataset.tasks); go("tasks"); }));
});
$("#dashRefresh").onclick = loadDash;

/* ------------------------------------------------------------------ cari */
function initSearch() { instChips($("#searchInst"), "searchInst"); $("#searchQ").focus(); }
function highlight(h) {
  // highlight dari Paperless berisi <span class="match">; semua lainnya di-escape
  return esc(h).replace(/&lt;span class=&quot;match&quot;&gt;(.*?)&lt;\/span&gt;/g, '<span class="match">$1</span>');
}
$("#searchForm").addEventListener("submit", guard(async (e) => {
  e.preventDefault();
  const q = $("#searchQ").value.trim(); if (!q) return;
  $("#searchRes").innerHTML = `<p class="muted">Mencari…</p>`;
  const res = await api(`/api/search?q=${encodeURIComponent(q)}&instances=${chosen($("#searchInst")).join(",")}&size=15`);
  $("#searchRes").innerHTML = res.map((g) => `
    <div class="sgroup"><h3>${esc(g.name)} <span class="muted small">${g.error ? `<span class="err">${esc(g.error)}</span>` : fmt(g.count) + " hasil"}</span></h3>
      ${g.results.map((r) => `<div class="hit">
        <img loading="lazy" src="/api/instances/${g.id}/thumb/${r.id}" alt="">
        <div><a class="t" href="${esc(r.link)}" target="_blank" rel="noopener">${esc(r.title)}</a>
          <div class="small muted">#${r.id} · ${esc(r.created)} · ${esc(r.file || "")}</div>
          <div class="h">${highlight(r.highlights)}</div></div></div>`).join("") || (g.error ? "" : `<p class="muted small">Tidak ada hasil.</p>`)}
    </div>`).join("");
}));

/* ------------------------------------------------------------------ migrasi */
let migrateReady = false;
async function initMigrate() {
  if (!INST.length) { toast("Tambahkan minimal dua instance di Pengaturan dulu.", true); return; }
  if (!migrateReady) {
    instOptions($("#mSrc")); instOptions($("#mDst"));
    if (INST.length > 1) $("#mDst").selectedIndex = 1;
    $("#mSrc").onchange = guard(loadSrcMeta); $("#mDst").onchange = guard(loadMapInfo);
    migrateReady = true;
  }
  await guard(loadSrcMeta)();
}
async function loadSrcMeta() {
  const src = +$("#mSrc").value; if (!src) return;
  const keepSp = $("#mSp").value;
  $("#mSp").innerHTML = `<option value="">memuat…</option>`;
  let meta;
  try { meta = await api(`/api/instances/${src}/meta`); }
  catch (e) { $("#mSp").innerHTML = `<option value="">— sumber tidak bisa dihubungi —</option>`; $("#mTags").innerHTML = $("#mAnyTags").innerHTML = ""; throw e; }
  $("#mSp").innerHTML = `<option value="">— semua —</option>` + meta.storage_paths.map((s) => `<option value="${esc(s.name)}">${esc(s.name)} (${fmt(s.count)})</option>`).join("");
  $("#mSp").value = keepSp;
  const tagOpts = meta.tags.map((t) => `<option value="${esc(t.name)}">${esc(t.name)} (${fmt(t.count)})</option>`).join("");
  $("#mTags").innerHTML = tagOpts; $("#mAnyTags").innerHTML = tagOpts;
  await loadMapInfo();
}
async function loadMapInfo() {
  const src = +$("#mSrc").value, dst = +$("#mDst").value;
  if (!src || !dst || src === dst) { $("#mMapInfo").textContent = src === dst ? "Sumber dan tujuan tidak boleh sama." : ""; return; }
  const { count } = await api(`/api/mappings/count?src=${src}&dst=${dst}`);
  $("#mMapInfo").textContent = `${fmt(count)} dokumen sudah tercatat dipindah dari ${instName(src)} ke ${instName(dst)}; dokumen itu dilewati.`;
}
const selected = (sel) => [...sel.selectedOptions].map((o) => o.value);
function migrateFilter() {
  const f = { storage_path: $("#mSp").value || null, tags: selected($("#mTags")), any_tags: selected($("#mAnyTags")), query: $("#mQuery").value.trim() || null };
  if (!f.storage_path && !f.tags.length && !f.any_tags.length && !f.query) throw new Error("Isi minimal satu filter.");
  return f;
}
function migrateBase() {
  const src = +$("#mSrc").value, dst = +$("#mDst").value;
  if (src === dst) throw new Error("Sumber dan tujuan tidak boleh sama.");
  return { src, dst, filter: migrateFilter() };
}
async function submitJob(body) { const { id } = await api("/api/jobs", { method: "POST", body }); go("job-" + id); }
$("#mDry").onclick = guard(() => submitJob({ ...migrateBase(), kind: "migrate", options: migrateOpts(true) }));
$("#mRun").onclick = guard(() => {
  const b = migrateBase();
  if (!confirm(`Jalankan migrasi dari ${instName(b.src)} ke ${instName(b.dst)}?\nDokumen di sumber tidak dihapus.`)) return;
  return submitJob({ ...b, kind: "migrate", options: migrateOpts(false) });
});
function migrateOpts(dry) {
  return { dry_run: dry, mark_tag: $("#oMark").value.trim(), fail_tag: $("#oFail").value.trim(), dup_tag: $("#oDup").value.trim(), no_content: $("#oNoContent").checked };
}
$("#dPreview").onclick = guard(() => submitJob({ ...migrateBase(), kind: "delete", options: { dry_run: true, include_duplicates: $("#dDup").checked } }));
$("#impBtn").onclick = guard(async () => {
  const f = $("#impFile").files[0]; if (!f) throw new Error("Pilih file migrate_state.json dulu.");
  const state = JSON.parse(await f.text());
  const src = +$("#mSrc").value, dst = +$("#mDst").value;
  if (!confirm(`Impor ${Object.keys(state).length} mapping sebagai riwayat ${instName(src)} → ${instName(dst)}?`)) return;
  const r = await api("/api/import-state", { method: "POST", body: { src, dst, state } });
  $("#impMsg").textContent = `${fmt(r.imported)} mapping diimpor.`; loadMapInfo();
});

/* ------------------------------------------------------------------ jobs */
const kindLabel = (j) => (j.kind === "migrate" ? "Migrasi" : "Hapus dari sumber") + (j.params?.options?.dry_run ? (j.kind === "migrate" ? " (dry-run)" : " (pratinjau)") : "");
function filterLabel(f = {}) {
  return [f.storage_path && `storage path: ${f.storage_path}`, f.tags?.length && `tag: ${f.tags.join(" + ")}`,
          f.any_tags?.length && `salah satu tag: ${f.any_tags.join(", ")}`, f.query && `query: ${f.query}`].filter(Boolean).join(" · ");
}
const loadJobs = guard(async () => {
  const jobs = await api("/api/jobs");
  $("#jobList").innerHTML = jobs.length ? `<table class="tbl"><thead><tr><th>#</th><th>Jenis</th><th>Arah</th><th>Filter</th><th>Status</th><th class="num">Progres</th><th class="num">OK</th><th class="num">Dup</th><th class="num">Gagal</th><th>Dibuat</th></tr></thead><tbody>
    ${jobs.map((j) => `<tr class="click" data-job="${j.id}"><td>${j.id}</td><td>${kindLabel(j)}</td><td>${esc(j.src_name)} → ${esc(j.dst_name)}</td>
      <td class="msg">${esc(filterLabel(j.params.filter))}</td><td><span class="badge ${j.status}">${j.status}</span></td>
      <td class="num">${fmt(j.done)}/${fmt(j.total)}</td><td class="num">${fmt(j.ok)}</td><td class="num">${fmt(j.dup)}</td><td class="num">${fmt(j.fail)}</td><td class="small">${esc(j.created.replace("T", " "))}</td></tr>`).join("")}
    </tbody></table>` : `<div class="panel muted">Belum ada job.</div>`;
  $$("[data-job]").forEach((r) => (r.onclick = () => go("job-" + r.dataset.job)));
});
$("#jobsRefresh").onclick = loadJobs;

async function openJob(id) {
  currentJob = id; lastItemId = 0; jobStatusFilter = "";
  $("#jobItems").innerHTML = ""; $("#jobLog").textContent = ""; $("#jobDeleteBox").classList.add("hidden");
  $("#jobCsv").href = `/api/jobs/${id}/csv`;
  clearInterval(jobTimer);
  await refreshJob(true);
  jobTimer = setInterval(() => refreshJob(false).catch(() => {}), 2000);
}
let jobCache = null;
async function refreshJob(first) {
  const id = currentJob; if (!id) return;
  const j = await api(`/api/jobs/${id}`); jobCache = j;
  $("#jobTitle").textContent = `Job #${j.id}: ${kindLabel(j)}`;
  $("#jobMeta").innerHTML = `${esc(j.src_name)} → ${esc(j.dst_name)} · ${esc(filterLabel(j.params.filter))} · status <span class="badge ${j.status}">${j.status}</span>${j.message ? ` · <span class="err">${esc(j.message)}</span>` : ""}`;
  const pct = j.total ? Math.round((j.done / j.total) * 100) : (j.status === "selesai" ? 100 : 0);
  $("#jobBar").style.width = pct + "%";
  const labels = j.kind === "migrate" ? [["done", "diproses"], ["total", "total"], ["ok", j.params.options.dry_run ? "akan disalin" : "sukses"], ["dup", "duplikat"], ["fail", "gagal"], ["skipped", "dilewati (sudah tersalin)"]]
                                      : [["done", "diverifikasi"], ["total", "total"], ["ok", "terverifikasi"], ["skipped", "tidak dihapus"], ["fail", "error"], ["deleted", "terhapus"]];
  $("#jobCounts").innerHTML = labels.map(([k, l]) => `<span><b>${fmt(j[k])}</b> ${l}</span>`).join("");
  const running = ["berjalan", "antri"].includes(j.status);
  $("#jobCancel").classList.toggle("hidden", !running);

  if (first) {
    const statuses = j.kind === "migrate" ? ["OK", "AKAN_DISALIN", "DUPLIKAT", "GAGAL", "DILEWATI", "INFO"] : ["SIAP_HAPUS", "DIHAPUS", "DILEWATI", "TIDAK_ADA_DI_TARGET", "VERIFIKASI_GAGAL", "INFO"];
    $("#jobFilter").innerHTML = `<span class="chip on" data-s="">Semua</span>` + statuses.map((s) => `<span class="chip" data-s="${s}">${s}</span>`).join("");
    $("#jobFilter").onclick = (e) => {
      const c = e.target.closest(".chip"); if (!c) return;
      $$(".chip", $("#jobFilter")).forEach((x) => x.classList.toggle("on", x === c));
      jobStatusFilter = c.dataset.s; lastItemId = 0; $("#jobItems").innerHTML = ""; loadItems(j);
    };
  }
  await loadItems(j);
  if (j.kind === "delete" && j.params.options.dry_run && j.status === "selesai") {
    const n = j.ok; $("#delCount").textContent = fmt(n);
    $("#jobDeleteBox").classList.toggle("hidden", !n);
  }
  if (!running && !first) { clearInterval(jobTimer); }
}
async function loadItems(j) {
  let items;
  do {
    items = await api(`/api/jobs/${j.id}/items?after=${lastItemId}&status=${jobStatusFilter}`);
    const rows = [];
    for (const it of items) {
      lastItemId = it.id;
      if (it.status === "INFO") { $("#jobLog").textContent += `[${it.ts.slice(11)}] ${it.msg}\n`; if (jobStatusFilter !== "INFO") continue; }
      const srcL = it.src_doc ? `<a href="${esc(j.src_url)}/documents/${it.src_doc}/details" target="_blank" rel="noopener">#${it.src_doc}</a>` : "";
      const dstL = it.dst_doc ? `<a href="${esc(j.dst_url)}/documents/${it.dst_doc}/details" target="_blank" rel="noopener">#${it.dst_doc}</a>` : "";
      rows.push(`<tr><td class="small">${esc(it.ts.slice(11))}</td><td>${srcL}</td><td>${esc(it.title || "")}</td><td><span class="badge ${it.status}">${it.status}</span></td><td>${dstL}</td><td class="msg">${esc(it.msg || "")}</td></tr>`);
    }
    $("#jobItems").insertAdjacentHTML("beforeend", rows.join(""));
  } while (items.length >= 500);
}
$("#jobCancel").onclick = guard(async () => { if (confirm("Batalkan job ini?")) { await api(`/api/jobs/${currentJob}/cancel`, { method: "POST" }); toast("Pembatalan diminta; job berhenti setelah dokumen yang sedang diproses."); } });
$("#delGo").onclick = guard(async () => {
  if ($("#delConfirm").value.trim() !== "HAPUS") throw new Error("Ketik HAPUS untuk konfirmasi.");
  const j = jobCache;
  const ready = [];
  let after = 0, page;
  do {
    page = await api(`/api/jobs/${j.id}/items?status=SIAP_HAPUS&after=${after}`);
    page.forEach((i) => { ready.push(i.src_doc); after = i.id; });
  } while (page.length >= 500);
  if (!confirm(`Hapus ${ready.length} dokumen dari ${j.src_name}? Setiap dokumen diverifikasi ulang sebelum dihapus.`)) return;
  await submitJob({ kind: "delete", src: j.src_instance, dst: j.dst_instance, filter: j.params.filter,
                    options: { dry_run: false, include_duplicates: !!j.params.options.include_duplicates, doc_ids: ready, confirm: "HAPUS" } });
});

/* ------------------------------------------------------------------ cek file */
function initCheck() { instChips($("#chkInst"), "chkInst"); }
function parseNames(text) {
  // terima tempelan mentah dari daftar task Paperless: ambil token yang tampak seperti nama file
  const out = new Set();
  for (const line of text.split(/\n/)) {
    const m = line.trim().match(/^([^\s:][^:]*?\.[A-Za-z0-9]{2,5})(?::|$|\s)/);
    if (m) out.add(m[1].trim());
  }
  return [...out];
}
$("#chkGo").onclick = guard(async () => {
  const names = parseNames($("#chkNames").value);
  if (!names.length) throw new Error("Tidak ada nama file yang dikenali.");
  $("#chkNames").value = names.join("\n");
  $("#chkTbl").innerHTML = `<tr><td class="muted">Memeriksa ${names.length} file…</td></tr>`;
  const ids = chosen($("#chkInst"));
  const rows = await api("/api/checkfiles", { method: "POST", body: { names, instances: ids } });
  const insts = rows[0]?.instances || [];
  let none = 0, some = 0;
  $("#chkTbl").innerHTML = `<thead><tr><th>File</th>${insts.map((i) => `<th>${esc(i.name)}</th>`).join("")}<th>Status</th></tr></thead><tbody>` +
    rows.map((r) => {
      const anywhere = r.instances.some((i) => i.docs.length);
      anywhere ? some++ : none++;
      return `<tr><td><b>${esc(r.name)}</b></td>${r.instances.map((i) => `<td>${i.error ? `<span class="err small">${esc(i.error)}</span>` :
        i.docs.map((d) => `<a href="${esc(d.link)}" target="_blank" rel="noopener">#${d.id}</a> <span class="small muted">${esc(d.title)}</span>`).join("<br>") +
        (i.trash.length ? `<br><span class="badge DUPLIKAT">Trash #${i.trash.map((t) => t.id).join(", #")}</span>` : "") +
        (!i.docs.length && !i.trash.length ? `<span class="muted">—</span>` : "")}</td>`).join("")}
        <td>${!anywhere ? `<span class="badge bad">tidak ditemukan</span>` : r.found_in < 2 ? `<span class="badge DUPLIKAT">hanya di ${r.found_in} instance</span>`
          : r.identical ? `<span class="badge ok">ya</span>` : `<span class="badge bad">berbeda</span>`}</td></tr>`;
    }).join("") + "</tbody>";
  $("#chkSummary").innerHTML = `<div class="stat"><b>${some}</b><span>ditemukan</span></div><div class="stat"><b>${none}</b><span>tidak ditemukan di mana pun</span></div>`;
});

/* ------------------------------------------------------------------ task gagal */
function initTasks() {
  instOptions($("#tInst"));
  const pre = localStorage.getItem("tasksInst"); if (pre) { $("#tInst").value = pre; localStorage.removeItem("tasksInst"); }
  loadTasks();
}
const loadTasks = guard(async () => {
  const id = +$("#tInst").value; if (!id) return;
  $("#tRows").innerHTML = `<tr><td colspan="4" class="muted">Memuat…</td></tr>`;
  const rows = await api(`/api/instances/${id}/tasks?failed=true`);
  $("#tRows").innerHTML = rows.map((t) => `<tr><td><input type="checkbox" data-tid="${t.id}" data-file="${esc(t.file || "")}"></td>
    <td class="small">${esc(t.created)}</td><td>${esc(t.file || "")}</td><td class="msg">${esc(t.result)}</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">Tidak ada task gagal yang belum dibaca.</td></tr>`;
});
$("#tLoad").onclick = loadTasks;
$("#tAll").onchange = (e) => $$("#tRows input[type=checkbox]").forEach((c) => (c.checked = e.target.checked));
const pickedTasks = () => $$("#tRows input[type=checkbox]:checked");
$("#tToCheck").onclick = () => {
  const names = [...new Set(pickedTasks().map((c) => c.dataset.file).filter(Boolean))];
  if (!names.length) return toast("Pilih task dulu.", true);
  go("check"); $("#chkNames").value = names.join("\n"); $("#chkGo").click();
};
$("#tAck").onclick = guard(async () => {
  const ids = pickedTasks().map((c) => +c.dataset.tid);
  if (!ids.length) throw new Error("Pilih task dulu.");
  await api(`/api/instances/${$("#tInst").value}/tasks/ack`, { method: "POST", body: { ids } });
  toast(`${ids.length} task ditandai sudah dibaca.`); loadTasks();
});

/* ------------------------------------------------------------------ pengaturan */
const loadSettings = guard(async () => {
  await loadInstances();
  $("#instRows").innerHTML = INST.map((i) => `<tr><td><b>${esc(i.name)}</b></td><td class="url">${esc(i.url)}</td><td>${i.verify_tls ? "verifikasi" : "<span class='badge DUPLIKAT'>tanpa verifikasi</span>"}</td>
    <td class="row gap"><button class="btn small" data-edit="${i.id}">Edit</button><button class="btn small danger" data-del="${i.id}">Hapus</button></td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">Belum ada instance.</td></tr>`;
  $$("[data-edit]").forEach((b) => (b.onclick = () => {
    const i = INST.find((x) => x.id === +b.dataset.edit), f = $("#instForm");
    f.id.value = i.id; f.name.value = i.name; f.url.value = i.url; f.token.value = ""; f.verify_tls.checked = !!i.verify_tls;
    f.token.placeholder = "(kosongkan = pakai token lama)"; $("#instFormTitle").textContent = `Edit: ${i.name}`; f.name.focus();
  }));
  $$("[data-del]").forEach((b) => (b.onclick = guard(async () => {
    if (!confirm(`Hapus instance "${instName(+b.dataset.del)}" dari portal? (Paperless-nya tidak tersentuh)`)) return;
    await api(`/api/instances/${b.dataset.del}`, { method: "DELETE" }); loadSettings();
  })));
});
function instBody() {
  const f = $("#instForm");
  return { name: f.name.value.trim(), url: f.url.value.trim(), token: f.token.value.trim(), verify_tls: f.verify_tls.checked };
}
function resetInst() {
  const f = $("#instForm"); f.reset(); f.id.value = ""; f.token.placeholder = "Profil → API Auth Token";
  $("#instFormTitle").textContent = "Tambah instance"; $("#instMsg").textContent = "";
}
$("#instReset").onclick = resetInst;
$("#instTest").onclick = guard(async () => {
  $("#instMsg").textContent = "menguji…";
  const id = $("#instForm").id.value;
  const r = await api(`/api/instances/test${id ? "?id=" + id : ""}`, { method: "POST", body: instBody() });
  $("#instMsg").innerHTML = r.ok ? `<span class="badge ok">OK</span> versi ${esc(r.version)}, ${fmt(r.documents_total)} dokumen, ${r.latency_ms} ms`
                                 : `<span class="err">${esc(r.error)}</span>`;
});
$("#instForm").addEventListener("submit", guard(async (e) => {
  e.preventDefault();
  const id = $("#instForm").id.value, body = instBody();
  await api(id ? `/api/instances/${id}` : "/api/instances", { method: id ? "PUT" : "POST", body });
  toast("Instance disimpan."); resetInst(); migrateReady = false; loadSettings();
}));

boot();
