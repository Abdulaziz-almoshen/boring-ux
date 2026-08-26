// Toolbar click → inject WebGazer + the recorder panel into the current tab.
// Runs in the real page, so it works on ANY site (no iframe, no X-Frame-Options).
chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !/^https?:/.test(tab.url || "")) {
    chrome.action.setBadgeText({ text: "no", tabId: tab.id });
    return;
  }
  // If already injected, toggle the panel; else inject webgazer then the app.
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => !!window.__boringUX
  });
  if (result) {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: () => window.__boringUX.toggle() });
    return;
  }
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["webgazer.js"] });
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
});
