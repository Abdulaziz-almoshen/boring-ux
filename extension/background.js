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

chrome.tabs.onUpdated.addListener(async (tabId, info) => {
  if (info.status !== "complete") return;
  const key = "autoinject:"+tabId;
  const s = await chrome.storage.session.get(key);
  if (!s[key]) return;
  await chrome.storage.session.remove(key);
  try { await inject(tabId); } catch (e) {}
});
