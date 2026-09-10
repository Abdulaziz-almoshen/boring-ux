// Toolbar click → inject WebGazer + the recorder panel into the current tab.
// Runs in the real page, so it works on ANY site (no iframe, no X-Frame-Options).
//
// Locked-down sites: some sites send `Permissions-Policy: camera=()` which forbids
// the camera for everything in their page (including this tool). For LOCAL TESTING we
// strip that header (and X-Frame-Options) from the response — only in this browser,
// only for domains you activate, changing nothing on the server. Standard QA practice.

async function ruleIdFor(domain){
  const key = "ruleid:"+domain;
  const got = await chrome.storage.local.get([key,"__ruleseq"]);
  if (got[key]) return got[key];
  const id = (got.__ruleseq || 1000) + 1;
  await chrome.storage.local.set({ [key]: id, __ruleseq: id });
  return id;
}
async function ensureStripRule(domain){
  const id = await ruleIdFor(domain);
  await chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: [id],
    addRules: [{
      id, priority: 1,
      action: { type: "modifyHeaders", responseHeaders: [
        { header: "permissions-policy", operation: "remove" },
        { header: "x-frame-options", operation: "remove" }
      ]},
      condition: { requestDomains: [domain], resourceTypes: ["main_frame","sub_frame"] }
    }]
  });
}
async function inject(tabId){
  await chrome.scripting.executeScript({ target:{tabId}, files:["webgazer.js"] });
  await chrome.scripting.executeScript({ target:{tabId}, files:["content.js"] });
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !/^https?:/.test(tab.url || "")) {
    chrome.action.setBadgeText({ text: "no", tabId: tab.id }); return;
  }
  // Already running? just toggle the panel.
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId: tab.id }, func: () => !!window.__boringUX
  });
  if (result) {
    await chrome.scripting.executeScript({ target:{tabId:tab.id}, func: () => window.__boringUX.toggle() });
    return;
  }
  const domain = new URL(tab.url).hostname;
  await ensureStripRule(domain);                 // strip camera-blocking headers for this domain (local only)
  // The header strip only applies to a fresh load — reload, then auto-inject when done.
  await chrome.storage.session.set({ ["autoinject:"+tab.id]: true });
  chrome.tabs.reload(tab.id);
});


/* ─────────────────────────────────────────────────────────────────────────────
   SESSION ACROSS TABS.
   The panel lives in one tab, but the participant works wherever they like. The worker owns the session so every tab can
   report its own clicks, mouse and URL: without this the report describes the tab the moderator started from, not the one
   under test. State is mirrored into chrome.storage.session because an MV3 worker is evicted after ~30 s of quiet.
   ───────────────────────────────────────────────────────────────────────────── */
const SES = { on: false, startWall: 0, events: [], mouse: [], contexts: {}, front: null };

async function sesSave(){ try{ await chrome.storage.session.set({ buxSes: SES }); }catch(_){} }
async function sesLoad(){
  try{ const g = await chrome.storage.session.get("buxSes"); if (g && g.buxSes && g.buxSes.on) Object.assign(SES, g.buxSes); }catch(_){}
}
sesLoad();

const INJECTABLE = u => /^https?:/.test(u || "");
function noteContext(tabId, c){
  if (!c) return;
  const k = String(tabId);
  const prev = SES.contexts[k] || { tabId, first_t: Date.now() - SES.startWall };
  SES.contexts[k] = Object.assign(prev, { url: c.url, title: c.title, vw: c.vw, vh: c.vh, dpr: c.dpr, sx: c.sx, sy: c.sy, sw: c.sw, sh: c.sh, last_t: Date.now() - SES.startWall });
}
function push(type, extra){ if (SES.on) SES.events.push(Object.assign({ t: Date.now() - SES.startWall, type }, extra || {})); }

async function injectLite(tabId){
  try { await chrome.scripting.executeScript({ target: { tabId }, files: ["lite.js"] }); return true; }
  catch (e) { return false; }
}
async function startTabCapture(){
  // 1. every tab open right now
  const tabs = await chrome.tabs.query({});
  let blocked = 0;
  for (const t of tabs) {
    if (!INJECTABLE(t.url)) { blocked++; continue; }
    await injectLite(t.id);
  }
  if (blocked) push("uninjectable_tabs", { txt: String(blocked) });
  // 2. tabs opened later — a registered script runs before the page's own scripts
  try {
    await chrome.scripting.unregisterContentScripts({ ids: ["bux-lite"] }).catch(() => {});
    await chrome.scripting.registerContentScripts([{ id: "bux-lite", js: ["lite.js"], matches: ["<all_urls>"],
      runAt: "document_start", allFrames: false, persistAcrossSessions: false }]);
  } catch (_) {}
  const [front] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (front) { SES.front = front.id; push("page", { txt: front.title || "", url: front.url || "" }); }
}
async function stopTabCapture(){
  try { await chrome.scripting.unregisterContentScripts({ ids: ["bux-lite"] }); } catch (_) {}
  const tabs = await chrome.tabs.query({});
  for (const t of tabs) { try { await chrome.tabs.sendMessage(t.id, { bux: "lite-stop" }); } catch (_) {} }
}

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  if (!SES.on) return;
  SES.front = tabId;
  try { const t = await chrome.tabs.get(tabId); push("tab_switch", { txt: t.title || "", url: t.url || "" });
        if (!INJECTABLE(t.url)) push("uninjectable_tab", { txt: t.title || "", url: t.url || "" }); } catch (_) {}
  sesSave();
});
chrome.windows.onFocusChanged.addListener(async (winId) => {
  if (!SES.on) return;
  if (winId === chrome.windows.WINDOW_ID_NONE) { push("browser_blur", {}); return sesSave(); }
  try { const [t] = await chrome.tabs.query({ active: true, windowId: winId });
        if (t) { SES.front = t.id; push("window_focus", { txt: t.title || "", url: t.url || "" }); } } catch (_) {}
  sesSave();
});
chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (!SES.on || info.status !== "complete") return;
  if (tabId === SES.front) push("page", { txt: tab.title || "", url: tab.url || "" });
  if (INJECTABLE(tab.url)) await injectLite(tabId);           /* belt and braces: the registered script may not have run */
  sesSave();
});

// Save a file into a real subfolder of Downloads (chrome.downloads honors subdirectories;
// the <a download> path attribute does not — it flattens "/" to "_").
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Proxy to the local processing service (127.0.0.1:7331). Runs here so the tested page's CSP can't block it.
  if (msg && msg.bux === "daemon") {
    fetch("http://127.0.0.1:7331" + msg.path, {
      method: msg.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: msg.body ? JSON.stringify(msg.body) : undefined
    }).then(async r => sendResponse({ ok: r.ok, status: r.status, json: await r.json().catch(() => null) }))
      .catch(e => sendResponse({ ok: false, status: 0, err: String(e && e.message || e) }));
    return true;
  }
  if (msg && msg.bux === "session-start") {
    Object.assign(SES, { on: true, startWall: msg.startWall, events: [], mouse: [], contexts: {}, front: sender.tab && sender.tab.id });
    startTabCapture().then(sesSave).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg && msg.bux === "lite-hello") {
    if (sender.tab) noteContext(sender.tab.id, msg.ctx);
    sendResponse({ recording: SES.on, startWall: SES.startWall });
    return true;
  }
  if (msg && msg.bux === "lite-batch") {
    if (SES.on && sender.tab) {
      const tid = sender.tab.id; noteContext(tid, msg.ctx);
      for (const e of msg.events || []) SES.events.push(Object.assign({}, e, { tab: tid }));
      for (const m of msg.mouse || []) SES.mouse.push(Object.assign({}, m, { tab: tid }));
      sesSave();
    }
    sendResponse({ ok: true });
    return true;
  }
  if (msg && msg.bux === "session-stop") {
    SES.on = false;
    stopTabCapture().then(() => {
      const data = { events: SES.events, mouse: SES.mouse, contexts: SES.contexts };
      SES.events = []; SES.mouse = []; sesSave();
      sendResponse(data);
    });
    return true;
  }
  if (msg && msg.bux === "download") {
    try {
      chrome.downloads.download(
        { url: msg.dataUrl, filename: msg.filename, conflictAction: "uniquify", saveAs: false },
        (id) => { const e = chrome.runtime.lastError; sendResponse({ ok: !e && id != null, err: e && e.message }); }
      );
    } catch (e) { sendResponse({ ok: false, err: String(e) }); }
    return true; // async response
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, info) => {
  if (info.status !== "complete") return;
  const key = "autoinject:"+tabId;
  const s = await chrome.storage.session.get(key);
  if (!s[key]) return;
  await chrome.storage.session.remove(key);
  try { await inject(tabId); } catch (e) {}
});
