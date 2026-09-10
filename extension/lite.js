/* Boring UX — lite recorder.
   Runs in EVERY tab during a session (registered at document_start by the service worker). No UI, no camera, no WebGazer:
   it only reports what the participant does in THIS tab, stamped with this tab's own URL and viewport, so the report no
   longer describes the tab the moderator happened to start from. The panel tab keeps its own recorder and skips this one. */
(() => {
  if (window.__boringUX || window.__buxLite) return;          /* the panel tab records itself; never record a tab twice */
  window.__buxLite = 1;
  let on = false, t0 = 0, lastMouse = 0, lastScroll = 0, maxScroll = 0;
  const evs = [], mice = [];
  const now = () => Date.now() - t0;
  const ctx = () => ({ url: location.href, title: document.title, vw: innerWidth, vh: innerHeight, dpr: devicePixelRatio,
                       sx: screenX, sy: screenY, sw: screen.width, sh: screen.height });

  const send = (msg) => new Promise(res => { try { chrome.runtime.sendMessage(msg, r => { void chrome.runtime.lastError; res(r); }); } catch (_) { res(null); } });
  let flushTimer = null;
  function flush() {
    if (!evs.length && !mice.length) return;
    const e = evs.splice(0), m = mice.splice(0);
    send({ bux: "lite-batch", events: e, mouse: m, ctx: ctx() });
  }
  function schedule() { if (!flushTimer) flushTimer = setInterval(flush, 1000); }

  function isClickable(el) { let n = el, d = 0;
    while (n && n.nodeType === 1 && d < 6) { if (/^(A|BUTTON|INPUT|SELECT|TEXTAREA|LABEL|SUMMARY)$/.test(n.tagName) || n.getAttribute("role") === "button" ||
      n.onclick || n.getAttribute("tabindex") !== null || getComputedStyle(n).cursor === "pointer") return true; n = n.parentElement; d++; } return false; }

  document.addEventListener("click", e => {
    if (!on) return;
    const el = e.target || {};
    let rect = null;
    try { const b = (el.getBoundingClientRect ? el : el.parentElement).getBoundingClientRect(); rect = [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; } catch (_) {}
    const txt = ((el.innerText || el.value || el.getAttribute && el.getAttribute("aria-label") || "") + "").trim().slice(0, 60).replace(/\s+/g, " ");
    evs.push({ t: now(), type: "click", txt, x: Math.round(e.clientX), y: Math.round(e.clientY), clickable: isClickable(el) ? 1 : 0, rect, url: location.href });
  }, true);

  document.addEventListener("mousemove", e => {
    if (!on) return;
    const t = Date.now(); if (t - lastMouse < 60) return; lastMouse = t;
    mice.push({ t: now(), x: Math.round(e.clientX), y: Math.round(e.clientY) });
  }, true);

  addEventListener("scroll", () => {
    if (!on) return;
    const t = Date.now(); if (t - lastScroll < 400) return; lastScroll = t;
    const h = Math.max(1, document.documentElement.scrollHeight - innerHeight);
    const pct = Math.min(100, Math.round(100 * scrollY / h));
    if (pct > maxScroll) { maxScroll = pct; evs.push({ t: now(), type: "scroll", pct, url: location.href }); }
  }, true);

  addEventListener("pagehide", () => { if (on) { evs.push({ t: now(), type: "page_leave", url: location.href }); flush(); } });

  /* ask the worker whether a session is running (this script is also injected into tabs opened mid-session) */
  send({ bux: "lite-hello", ctx: ctx() }).then(r => {
    if (!r || !r.recording) return;
    on = true; t0 = r.startWall;
    evs.push({ t: now(), type: "page", txt: document.title, url: location.href });
    schedule(); flush();
  });

  chrome.runtime.onMessage.addListener((msg, s, reply) => {
    if (!msg || msg.bux !== "lite-stop") return;
    on = false; flush(); if (flushTimer) clearInterval(flushTimer); flushTimer = null; reply({ ok: true });
  });
})();
