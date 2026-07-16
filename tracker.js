/*
 * Gaze UX Recorder — tracker snippet
 * Drop this into YOUR OWN site (the one being usability-tested):
 *     <script src="tracker.js"></script>
 * It reports page changes, clicks, form-field fill times, and frustration
 * signals (dead clicks, rage clicks, scroll depth, scroll thrash) to the
 * recorder via postMessage. No data leaves the browser — it only talks to
 * the parent window that embedded your page.
 */
(function () {
  if (window.__uxrTrackerLoaded) return;
  window.__uxrTrackerLoaded = true;

  var send = function (payload) {
    try {
      payload.__uxr = true;
      payload.t = performance.now();
      payload.wallT = Date.now();
      if (window.parent && window.parent !== window) window.parent.postMessage(payload, "*");
      if (window.opener) window.opener.postMessage(payload, "*");
    } catch (e) {}
  };

  // scroll / frustration state
  var lastY = 0, maxDepth = 0, lastDir = 0, dirChanges = 0, dirWinStart = 0, scrollThrottle = 0;
  var recentClicks = [];

  // ---- page load / SPA route changes ----
  var announce = function (spa) {
    // reset per-page scroll signals so depth/thrash are page-scoped
    maxDepth = 0; lastDir = 0; dirChanges = 0;
    lastY = window.pageYOffset || document.documentElement.scrollTop || 0;
    send({ type: "page", url: location.href, title: document.title, spa: !!spa });
  };
  if (document.readyState === "complete" || document.readyState === "interactive") announce(false);
  else window.addEventListener("DOMContentLoaded", function () { announce(false); });

  ["pushState", "replaceState"].forEach(function (m) {
    var orig = history[m];
    history[m] = function () {
      var r = orig.apply(this, arguments);
      setTimeout(function () { announce(true); }, 0);
      return r;
    };
  });
  window.addEventListener("popstate", function () { announce(true); });
  window.addEventListener("hashchange", function () { announce(true); });

  // ---- is the clicked thing actually interactive? ----
  var CLICKABLE_TAG = /^(A|BUTTON|INPUT|SELECT|TEXTAREA|LABEL|SUMMARY|OPTION)$/;
  var CLICKABLE_ROLE = /^(button|link|tab|checkbox|radio|menuitem|menuitemcheckbox|switch|option)$/;
  function isClickable(el) {
    var node = el, depth = 0;
    while (node && node.nodeType === 1 && depth < 6) {
      if (CLICKABLE_TAG.test(node.tagName)) return true;
      if (node.getAttribute) {
        var role = node.getAttribute("role");
        if (role && CLICKABLE_ROLE.test(role)) return true;
        if (node.hasAttribute("onclick") || node.hasAttribute("href")) return true;
        var ti = node.getAttribute("tabindex");
        if (ti != null && ti !== "-1") return true;
      }
      try { if (getComputedStyle(node).cursor === "pointer") return true; } catch (e) {}
      node = node.parentElement; depth++;
    }
    return false;
  }

  // ---- clicks (+ dead-click flag + rage-click detection) ----
  document.addEventListener("click", function (e) {
    var el = e.target || {};
    var clickable = isClickable(el);
    send({
      type: "click",
      x: e.clientX, y: e.clientY,
      tag: el.tagName || "",
      id: el.id || "",
      cls: (typeof el.className === "string" ? el.className : "") || "",
      role: (el.getAttribute && el.getAttribute("role")) || "",
      txt: (el.innerText || el.value || (el.getAttribute && el.getAttribute("aria-label")) || "").toString().trim().slice(0, 60),
      clickable: clickable
    });
    // rage click: 3+ clicks within 1.2s inside a 40px radius
    var now = performance.now();
    recentClicks.push({ x: e.clientX, y: e.clientY, t: now });
    recentClicks = recentClicks.filter(function (c) { return now - c.t < 1200; });
    var near = recentClicks.filter(function (c) {
      return Math.hypot(c.x - e.clientX, c.y - e.clientY) < 40;
    });
    if (near.length >= 3) {
      send({ type: "rage_click", x: e.clientX, y: e.clientY, count: near.length, clickable: clickable });
      recentClicks = [];
    }
  }, true);

  // ---- form fields: focus -> blur = fill time ----
  var isField = function (el) {
    return el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT");
  };
  var fieldKey = function (el) {
    return el.name || el.id || el.getAttribute("placeholder") || el.getAttribute("aria-label") || el.tagName;
  };
  var focusStart = {};
  document.addEventListener("focusin", function (e) {
    if (!isField(e.target)) return;
    var k = fieldKey(e.target);
    focusStart[k] = performance.now();
    send({ type: "field_focus", field: k });
  }, true);
  document.addEventListener("focusout", function (e) {
    if (!isField(e.target)) return;
    var k = fieldKey(e.target);
    var dur = focusStart[k] != null ? performance.now() - focusStart[k] : null;
    send({ type: "field_blur", field: k, fillMs: dur });
  }, true);

  // ---- form submit ----
  document.addEventListener("submit", function (e) {
    var f = e.target || {};
    send({ type: "submit", form: f.id || f.name || "form" });
  }, true);

  // ---- scroll: depth + up/down thrash ----
  function docScrollable() {
    return Math.max(document.body ? document.body.scrollHeight : 0,
                    document.documentElement.scrollHeight) - window.innerHeight;
  }
  window.addEventListener("scroll", function () {
    var y = window.pageYOffset || document.documentElement.scrollTop || 0;
    var now = performance.now();
    var dh = docScrollable();
    var pct = dh > 0 ? Math.min(100, Math.round(y / dh * 100)) : 0;
    if (pct > maxDepth) maxDepth = pct;

    var dir = y > lastY ? 1 : (y < lastY ? -1 : 0);
    if (dir !== 0) {
      if (lastDir === 0) dirWinStart = now;
      else if (dir !== lastDir) {
        if (now - dirWinStart > 2500) { dirChanges = 0; dirWinStart = now; }
        dirChanges++;
        if (dirChanges >= 4) { // 4 direction flips in <2.5s = thrashing
          send({ type: "scroll_thrash", count: dirChanges, y: y, pct: pct });
          dirChanges = 0; dirWinStart = now;
        }
      }
      lastDir = dir;
    }
    lastY = y;

    if (now - scrollThrottle > 200) { // sampled position for the timeline
      scrollThrottle = now;
      send({ type: "scroll", y: y, pct: pct, dir: dir, maxPct: maxDepth });
    }
  }, { passive: true });
})();
