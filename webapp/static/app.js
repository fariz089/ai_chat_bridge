/* FakeFluencer V2 — front-end (CDP + profile model). */
"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind; t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; }, 3200);
  setTimeout(() => t.remove(), 3600);
}
async function api(url, opts) {
  const r = await fetch(url, opts);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}
function go(page) {
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === page));
  $$(".nav-links button").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  if (page === "bank") loadProfiles();
  if (page === "create" || page === "chat") refreshProfileSelectors();
  if (page === "history") loadHistory();
  if (page === "settings") loadSettings();
  window.scrollTo({ top: 0, behavior: "smooth" });
}
$$(".nav-links button").forEach((b) => b.addEventListener("click", () => go(b.dataset.page)));

function seg(el) {
  el.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      el.querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      el.dispatchEvent(new CustomEvent("change", { detail: b.dataset.v }));
    }));
  return () => el.querySelector("button.active")?.dataset.v;
}
const getOutput = seg($("#cOutput"));
const getMergePortrait = seg($("#mergePortrait"));
$("#cOutput").addEventListener("change", () => {
  const img = getOutput() === "image";
  $("#vdurWrap").style.display = img ? "none" : "";
  $("#vresWrap").style.display = img ? "none" : "";
});

function logLine(box, line) {
  const span = document.createElement("span");
  span.className = "ln";
  if (line.time) { const ts = document.createElement("span"); ts.className = "ts"; ts.textContent = line.time; span.appendChild(ts); }
  span.appendChild(document.createTextNode(line.msg));
  box.appendChild(span); box.scrollTop = box.scrollHeight;
}
function clearLog(box, ph) { box.innerHTML = ""; if (ph) logLine(box, { msg: ph }); }
function streamJob(jobId, box, onEnd, onAsk) {
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  es.onmessage = (e) => { try { logLine(box, JSON.parse(e.data)); } catch (_) {} };
  es.addEventListener("ask", (e) => { let p = {}; try { p = JSON.parse(e.data); } catch (_) {} onAsk && onAsk(p); });
  es.addEventListener("end", (e) => { es.close(); let s = {}; try { s = JSON.parse(e.data); } catch (_) {} onEnd && onEnd(s); });
  es.onerror = () => { es.close(); onEnd && onEnd({ status: "error" }); };
  return es;
}

// ---- mid-job product confirmation modal ----
function confirmProduct(jobId, prompt) {
  const backdrop = $("#confirmModal");
  $("#cfTitle").textContent = prompt.title || "Konfirmasi Produk";
  $("#cfBrand").value = prompt.brand || "";
  $("#cfName").value = prompt.prefill_name || prompt.name || "";
  const sz = (prompt.prefill_size_ml != null && prompt.prefill_size_ml !== "")
    ? prompt.prefill_size_ml : prompt.size_ml;
  $("#cfSize").value = (sz == null || sz === "") ? "" : sz;
  const detected = (prompt.brand || "").trim() || (prompt.name || "").trim();
  $("#cfNote").textContent = detected
    ? `Terdeteksi otomatis dari foto oleh ${prompt.by || "penyedia skrip"}. Edit bila perlu.`
    : "Tidak terbaca dari foto — isi manual.";
  backdrop.style.display = "flex";
  let done = false;
  const send = async (payload) => {
    if (done) return; done = true;
    backdrop.style.display = "none";
    try { await api(`/api/jobs/${jobId}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); }
    catch (e) { toast("Gagal kirim konfirmasi: " + e.message, "err"); }
  };
  $("#cfOk").onclick = () => send({ brand: $("#cfBrand").value, name: $("#cfName").value, size_ml: $("#cfSize").value });
  $("#cfCancel").onclick = () => send({ cancel: true });
}

// ---- status / dashboard ----
async function refreshStatus() {
  let s;
  try { s = await api("/api/status"); }
  catch (_) { $("#navDot").className = "dot off"; $("#navStatus").textContent = "offline"; return; }
  $("#navDot").className = "dot " + (s.profiles_reachable > 0 ? "on" : "warn");
  $("#navStatus").textContent = s.profiles_reachable > 0 ? "online" : "ready";
  $("#navMode").textContent = "CDP · " + s.cdp_host;
  $("#verTag").textContent = s.version;
  $("#stCdp").textContent = s.cdp_host;
  $("#stProfiles").textContent = s.profiles_total;
  $("#stReach").textContent = s.profiles_reachable;
  $("#stApi").textContent = s.api_server_running ? "on" : "off";
  $("#mTotal").textContent = s.jobs_total;
  $("#mActive").textContent = s.jobs_active;
  const busy = s.jobs_active > 0;
  $("#stageDot").className = "dot " + (busy ? "warn" : "on");
  $("#stageBadge").textContent = busy ? "WORKING" : "STANDBY";
  $("#stageState").textContent = busy ? "BUSY" : "IDLE";
  $("#stageSub").textContent = busy ? `${s.jobs_active} job berjalan` : "Awaiting input";
  $("#stageMsg").textContent = s.profiles_reachable ? `${s.profiles_reachable}/${s.profiles_total} profil siap` : "No active profile";
  $("#apiDot").className = "dot " + (s.api_server_running ? "on" : "off");
  $("#apiState").textContent = s.api_server_running ? "running" : "stopped";
}
async function loadRecentJobs() {
  try {
    const { jobs } = await api("/api/jobs");
    if (!jobs.length) return;
    const box = $("#recentJobs"); box.innerHTML = "";
    jobs.slice(0, 8).forEach((j) => {
      const cls = j.status === "done" ? "fresh" : j.status === "error" ? "stale" : "";
      const div = document.createElement("div"); div.className = "item";
      div.innerHTML = `<div class="avatar">${j.kind[0].toUpperCase()}</div>
        <div class="meta"><div class="name">${j.title}</div>
        <div class="sub">${new Date(j.created * 1000).toLocaleString()}</div></div>
        <span class="tag ${cls}">${j.status}</span>`;
      box.appendChild(div);
    });
  } catch (_) {}
}

// ---- profiles (Bank) ----
let PROFILES = [];
const loginTag = { ready: ["fresh", "login OK"], signin: ["stale", "perlu login"], unknown: ["", "tab kosong"], offline: ["stale", "offline"] };

async function loadProfiles() {
  const data = await api("/api/profiles");
  PROFILES = data.profiles;
  // noVNC link points at the same host, port 6080
  $("#novncLink").href = `http://${location.hostname}:6080`;
  const box = $("#profileList");
  if (!PROFILES.length) {
    box.innerHTML = `<div class="empty"><div class="ico">⛁</div><div class="t">Belum ada profil</div><div>Tambah profil di panel kanan.</div></div>`;
    return;
  }
  box.innerHTML = "";
  PROFILES.forEach((p) => {
    const [cls, txt] = loginTag[p.login] || ["", p.login];
    const div = document.createElement("div"); div.className = "item";
    div.innerHTML = `<div class="avatar">${p.platform.slice(0, 2).toUpperCase()}</div>
      <div class="meta"><div class="name">${p.platform_label} : ${p.label}</div>
      <div class="sub">port ${p.port} · ${p.reachable ? (p.version || "chrome") : "tidak terjangkau"}</div></div>
      <span class="tag ${cls}">${txt}</span>`;
    const acts = document.createElement("div"); acts.className = "actions";
    if (["chatgpt", "grok", "gemini"].includes(p.platform)) {
      const test = document.createElement("button");
      test.className = "btn ghost"; test.textContent = "Tes";
      test.onclick = () => testProfile(p);
      acts.appendChild(test);
    }
    const del = document.createElement("button");
    del.className = "btn danger"; del.textContent = "Hapus";
    del.onclick = async () => {
      if (!confirm(`Hapus profil ${p.id}? (folder di disk tidak ikut terhapus)`)) return;
      await api("/api/profiles/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: p.id }) });
      toast("Profil dihapus", "ok"); loadProfiles();
    };
    acts.appendChild(del); div.appendChild(acts); box.appendChild(div);
  });
}
async function testProfile(p) {
  clearLog($("#bankLog"), null);
  logLine($("#bankLog"), { msg: `Tes ${p.id}…` });
  try {
    const r = await api("/api/profiles/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: p.id }) });
    streamJob(r.job_id, $("#bankLog"), (s) => {
      if (s.status === "done") toast("Bridge OK", "ok");
      else if (s.status === "error") toast("Tes gagal: " + (s.error || ""), "err");
    });
  } catch (e) { toast(e.message, "err"); }
}
$("#btnAddProfile").addEventListener("click", async () => {
  const platform = $("#profPlatform").value;
  const label = $("#profLabel").value || "main";
  try {
    const r = await api("/api/profiles/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ platform, label }) });
    clearLog($("#bankLog"), null);
    logLine($("#bankLog"), { msg: `✓ ${r.profile.id} → port ${r.profile.port}` });
    logLine($("#bankLog"), { msg: r.note });
    toast("Profil ditambahkan", "ok");
    loadProfiles(); refreshProfileSelectors();
  } catch (e) { toast(e.message, "err"); }
});

// ---- profile selectors for Create & Chat ----
async function refreshProfileSelectors() {
  try {
    const o = await api("/api/generator/options");
    fillProfileSelect($("#cScriptProfile"), o.script_profiles);
    fillProfileSelect($("#cImageProfile"), o.image_profiles);
    // Render profile: same providers as image (grok/gemini), but with a
    // leading blank option meaning "ikut profil gambar".
    fillProfileSelect($("#cRenderProfile"), o.image_profiles, true);
    const data = await api("/api/profiles");
    const chatProfiles = data.profiles.filter((p) => ["chatgpt", "grok", "gemini"].includes(p.platform))
      .map((p) => ({ id: p.id, name: `${p.platform_label} : ${p.label}`, platform: p.platform }));
    fillProfileSelect($("#chatProfile"), chatProfiles);
    updateImaginePanel();
  } catch (_) {}
}
function fillProfileSelect(sel, profs, allowBlank) {
  const prev = sel.value;
  sel.innerHTML = "";
  if (allowBlank) { const b = document.createElement("option"); b.value = ""; b.textContent = "— ikut profil gambar —"; sel.appendChild(b); }
  // "Auto" entries: one per distinct platform present. Selecting Auto lets the
  // server load-balance across every logged-in account of that platform and
  // queue when they're all busy. value="" but data-auto-platform set.
  const platforms = [...new Set((profs || []).map((p) => p.platform).filter(Boolean))];
  const platLabel = { grok: "Grok", gemini: "Gemini", chatgpt: "ChatGPT", aistudio: "AI Studio" };
  platforms.forEach((plat) => {
    const o = document.createElement("option");
    o.value = "";
    o.dataset.autoPlatform = plat;
    o.dataset.platform = plat;
    o.textContent = `🔀 Auto ${platLabel[plat] || plat} (load-balance)`;
    sel.appendChild(o);
  });
  if (!profs.length) { if (!allowBlank && !platforms.length) { const o = document.createElement("option"); o.value = ""; o.textContent = "— belum ada profil —"; sel.appendChild(o); } return; }
  profs.forEach((p) => { const o = document.createElement("option"); o.value = p.id; o.textContent = p.name; o.dataset.platform = p.platform || ""; sel.appendChild(o); });
  if (prev) sel.value = prev;
}

// Read a profile-select into the {id, platform} the API expects. When the
// chosen <option> is an "Auto" entry, we send the platform (server balances);
// otherwise we send the specific profile id.
function readProfileSel(sel) {
  const opt = sel.selectedOptions[0];
  if (!opt) return { id: "", platform: "" };
  const auto = opt.dataset.autoPlatform;
  if (auto) return { id: "", platform: auto };
  return { id: opt.value || "", platform: opt.dataset.platform || "" };
}
function updateImaginePanel() {
  const opt = $("#chatProfile").selectedOptions[0];
  const grok = opt && opt.dataset.platform === "grok";
  $("#imaginePanel").style.display = grok ? "grid" : "none";
}
$("#chatProfile").addEventListener("change", updateImaginePanel);

// ---- generator options ----
async function loadGeneratorOptions() {
  const o = await api("/api/generator/options");
  const fill = (sel, arr, v = (x) => x, t = (x) => x) => { sel.innerHTML = ""; arr.forEach((x) => { const op = document.createElement("option"); op.value = v(x); op.textContent = t(x); sel.appendChild(op); }); };
  fill($("#cMode"), o.modes, (m) => m.key, (m) => m.label);
  fill($("#cAspect"), o.aspects);
  fill($("#cVoice"), o.voices);
  fill($("#cTone"), o.tones);
  $("#cTone").value = o.tones.includes("antusias") ? "antusias" : o.tones[0];
  const ia = $("#imgAspect"); ia.innerHTML = ""; o.aspects.forEach((a) => { const op = document.createElement("option"); op.textContent = a; ia.appendChild(op); });
}

// ---- uploads ----
let modelPaths = [], modelPrev = [], productPaths = [], productPrev = [];
function renderThumbs() {
  const draw = (box, prev, n) => {
    box.innerHTML = "";
    prev.forEach((u) => { const i = document.createElement("img"); i.className = "thumb"; i.src = u; box.appendChild(i); });
    if (n) { const t = document.createElement("span"); t.className = "tag fresh"; t.style.alignSelf = "center"; t.textContent = n + " foto"; box.appendChild(t); }
  };
  draw($("#thumbsModel"), modelPrev, modelPaths.length);
  draw($("#thumbsProduct"), productPrev, productPaths.length);
}
async function uploadFiles(input, which) {
  if (!input.files.length) return;
  const fd = new FormData(); Array.from(input.files).forEach((f) => fd.append("files", f));
  try {
    const r = await api("/api/upload", { method: "POST", body: fd });
    const prev = (r.files || []).map((f) => f.preview);
    if (which === "model") { modelPaths.push(...r.paths); modelPrev.push(...prev); }
    else { productPaths.push(...r.paths); productPrev.push(...prev); }
    renderThumbs(); toast(`${r.paths.length} foto diunggah`, "ok");
  } catch (e) { toast("Upload gagal: " + e.message, "err"); }
  input.value = "";
}
$("#dropModel").addEventListener("click", () => $("#fileModel").click());
$("#dropProduct").addEventListener("click", () => $("#fileProduct").click());
$("#fileModel").addEventListener("change", (e) => uploadFiles(e.target, "model"));
$("#fileProduct").addEventListener("change", (e) => uploadFiles(e.target, "product"));

// ---- generate ----
$("#btnGenerate").addEventListener("click", async () => {
  const scriptSel = readProfileSel($("#cScriptProfile"));
  const imageSel = readProfileSel($("#cImageProfile"));
  const renderSel = readProfileSel($("#cRenderProfile"));
  const hasScript = scriptSel.id || scriptSel.platform;
  const hasImage = imageSel.id || imageSel.platform;
  if (!hasScript || !hasImage) { toast("Pilih profil/Auto untuk skrip & gambar dulu (tambah di Bank).", "err"); return; }
  const body = {
    mode: $("#cMode").value, num_scenes: parseInt($("#cScenes").value || "2", 10),
    script_profile_id: scriptSel.id, script_platform: scriptSel.platform,
    image_profile_id: imageSel.id, image_platform: imageSel.platform,
    render_profile_id: renderSel.id, render_platform: renderSel.platform,
    output_mode: getOutput(), project_name: $("#cProject").value, aspect: $("#cAspect").value,
    voice_key: $("#cVoice").value, tone_key: $("#cTone").value, background: $("#cBg").value,
    video_duration: $("#cVdur").value, video_resolution: $("#cVres").value,
    product_name: $("#cProdName").value, model_imgs: modelPaths, product_imgs: productPaths,
    autochain: $("#cAutochain").checked,
  };
  $("#btnGenerate").disabled = true; $("#btnGenerate").innerHTML = '<span class="spin"></span> Memproses…';
  $("#genResult").innerHTML = ""; clearLog($("#genLog"), null);
  try {
    const r = await api("/api/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#btnCancelGen").style.display = "block";
    $("#btnCancelGen").onclick = () => api(`/api/jobs/${r.job_id}/cancel`, { method: "POST" });
    streamJob(r.job_id, $("#genLog"), async () => {
      $("#btnGenerate").disabled = false; $("#btnGenerate").innerHTML = "🚀 Generate"; $("#btnCancelGen").style.display = "none";
      const job = await api(`/api/jobs/${r.job_id}`);
      if (job.status === "done") {
        let html = "";
        const rendered = job.result.rendered || [];
        if (rendered.length) {
          html += `<div class="final-grid mt">` + rendered.map((m) =>
            m.type === "video"
              ? `<video src="${m.url}" controls playsinline class="final-media"></video>`
              : `<img src="${m.url}" class="final-media" alt="Scene ${m.scene}">`
          ).join("") + `</div>`;
          html += `<div class="note mt">Render final: ${job.result.rendered_ok}/${job.result.scenes} scene.</div>`;
        }
        if (job.result.download)
          html += `<a class="btn ${rendered.length ? "ghost" : "primary"} block mt" href="${job.result.download}">⬇ Unduh ${job.result.zip_name}</a>`;
        $("#genResult").innerHTML = html;
        toast(rendered.length ? "Render final siap" : "ZIP siap", "ok");
      } else if (job.status === "error") toast("Gagal: " + job.error, "err");
      refreshStatus();
    }, (prompt) => confirmProduct(r.job_id, prompt));
  } catch (e) { toast("Gagal mulai: " + e.message, "err"); $("#btnGenerate").disabled = false; $("#btnGenerate").innerHTML = "🚀 Generate"; }
});

// ---- chat ----
let chatAttach = [], chatAttachPrev = [];
function chatBubble(role, text) {
  const span = document.createElement("span"); span.className = "ln";
  span.style.color = role === "you" ? "var(--pine)" : "var(--ink)";
  span.textContent = (role === "you" ? "› " : "‹ ") + text;
  $("#chatLog").appendChild(span); $("#chatLog").scrollTop = $("#chatLog").scrollHeight;
}
$("#btnChatAttach").addEventListener("click", () => $("#chatFile").click());
$("#chatFile").addEventListener("change", async (e) => {
  if (!e.target.files.length) return;
  const fd = new FormData(); Array.from(e.target.files).forEach((f) => fd.append("files", f));
  const r = await api("/api/upload", { method: "POST", body: fd });
  chatAttach.push(...r.paths); chatAttachPrev.push(...(r.files || []).map((f) => f.preview));
  const box = $("#chatAttachThumbs"); box.innerHTML = "";
  chatAttachPrev.forEach((u) => { const i = document.createElement("img"); i.className = "thumb"; i.src = u; box.appendChild(i); });
  e.target.value = "";
});
$("#btnChatSend").addEventListener("click", async () => {
  const pid = $("#chatProfile").value;
  if (!pid) { toast("Pilih profil dulu (tambah di Bank).", "err"); return; }
  const message = $("#chatInput").value.trim();
  if (!message && !chatAttach.length) return;
  if (message) chatBubble("you", message);
  $("#chatInput").value = ""; $("#btnChatSend").disabled = true; $("#chatMedia").innerHTML = "";
  let imagine = null;
  if ($("#imaginePanel").style.display !== "none" && $("#imgMode").value)
    imagine = { mode: $("#imgMode").value, aspect: $("#imgAspect").value, resolution: $("#imgRes").value };
  const body = { profile_id: pid, message, attachments: chatAttach, new_chat: $("#chatNew").checked, imagine };
  chatAttach = []; chatAttachPrev = []; $("#chatAttachThumbs").innerHTML = "";
  try {
    const r = await api("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    streamJob(r.job_id, $("#chatLog"), async (s) => {
      $("#btnChatSend").disabled = false; $("#chatNew").checked = false;
      if (s.status === "done") {
        const job = await api(`/api/jobs/${r.job_id}`);
        if (job.result.response) chatBubble("ai", job.result.response);
        (job.result.media || []).forEach((m) => {
          if (!m.url) return;
          const tile = document.createElement("div"); tile.className = "tile";
          tile.innerHTML = m.type === "video" ? `<video src="${m.url}" controls></video>` : `<img src="${m.url}">`;
          $("#chatMedia").appendChild(tile);
        });
      } else if (s.status === "error") toast("Chat gagal: " + (s.error || ""), "err");
      refreshStatus();
    });
  } catch (e) { toast(e.message, "err"); $("#btnChatSend").disabled = false; }
});
$("#btnChatNew").addEventListener("click", async () => {
  const pid = $("#chatProfile").value; if (!pid) return;
  try { await api("/api/chat/new", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile_id: pid }) });
    clearLog($("#chatLog"), "Chat baru dimulai."); toast("Chat direset", "ok"); } catch (e) { toast(e.message, "err"); }
});

// ---- history ----
async function loadHistory() {
  const { items } = await api("/api/history");
  const box = $("#historyGrid");
  if (!items.length) { box.innerHTML = `<div class="empty"><div class="ico">▤</div><div class="t">Belum ada output</div><div>Hasil generate muncul di sini.</div></div>`; return; }
  box.className = "gallery"; box.innerHTML = "";
  items.forEach((it) => {
    const tile = document.createElement("div"); tile.className = "tile";
    let media = it.kind === "image" ? `<img src="${it.url}" loading="lazy">`
      : it.kind === "video" ? `<video src="${it.url}" muted></video>`
      : `<div style="height:120px;display:grid;place-items:center;background:var(--mist);font-size:30px">${it.kind === "zip" ? "🗜" : "📄"}</div>`;
    tile.innerHTML = `${media}<div class="cap"><div class="n">${it.name}</div><div class="s">${it.size_kb} KB · ${it.kind}</div></div>`;
    tile.style.cursor = "pointer"; tile.onclick = () => window.open(it.url, "_blank");
    box.appendChild(tile);
  });
}

// ---- settings ----
async function loadSettings() {
  const s = await api("/api/settings");
  $("#setApiPort").value = s.api_port; $("#setApiKey").value = s.api_key;
  $("#apiPortEcho").textContent = s.api_port; $("#setCdpHost").textContent = s.cdp_host;
}
$("#btnSaveSettings").addEventListener("click", async () => {
  await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_port: parseInt($("#setApiPort").value || "5100", 10), api_key: $("#setApiKey").value }) });
  toast("Pengaturan disimpan", "ok"); refreshStatus();
});
$("#btnApiStart").addEventListener("click", async () => { await api("/api/apiserver", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "start" }) }); toast("API server jalan", "ok"); refreshStatus(); });
$("#btnApiStop").addEventListener("click", async () => { await api("/api/apiserver", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "stop" }) }); toast("API server stop"); refreshStatus(); });

// ---- live merge ----
$("#btnMerge").addEventListener("click", async () => {
  clearLog($("#mergeLog"), null); $("#mergeResult").innerHTML = "";
  const body = { src: $("#mergeSrc").value || null, portrait: getMergePortrait() === "true", loop_min: $("#mergeLoop").value || null };
  try {
    const r = await api("/api/live/merge", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    streamJob(r.job_id, $("#mergeLog"), async (s) => {
      if (s.status === "done") {
        const job = await api(`/api/jobs/${r.job_id}`);
        if (job.result.download) $("#mergeResult").innerHTML = `<a class="btn primary block mt" href="${job.result.download}">⬇ Unduh video</a>`;
        toast("Video digabung", "ok");
      } else if (s.status === "error") toast("Gagal: " + (s.error || ""), "err");
    });
  } catch (e) { toast(e.message, "err"); }
});

// ---- boot ----
(async function boot() {
  await loadGeneratorOptions().catch((e) => toast("Gagal load opsi: " + e.message, "err"));
  await refreshProfileSelectors();
  refreshStatus(); loadRecentJobs();
  setInterval(() => { refreshStatus(); if ($("#dashboard").classList.contains("active")) loadRecentJobs(); }, 4000);
})();
window.go = go; window.loadHistory = loadHistory;
