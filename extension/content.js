/* Boring UX — content script. Runs INSIDE the real page, so it eye-tracks any site.
   Captures gaze + clicks + mouse + scroll + pages natively (no tracker.js), records
   face + mic (+ optional screen), and downloads an AI-ready session on stop. */
(function () {
if (window.__boringUX) { window.__boringUX.toggle(); return; }

const S = {
  recording:false, calibrated:false, accuracyPx:null, startPerf:0, startWall:0,
  gaze:[], mouse:[], events:[], pages:[], lastGx:null, lastGy:null, lastRegion:null, lastGazeT:0,
  regionTime:{L:0,C:0,R:0}, recorders:[], chunks:{face:[],audio:[],screen:[]}, recentClicks:[],
  lastMouse:0, sw:0, lastDir:0, dirChanges:0, dirWinStart:0, maxScroll:0
};
window.__boringUX = { toggle: () => { panel.style.display = panel.style.display==="none"?"block":"none"; } };
const nowRel = () => S.startPerf ? performance.now()-S.startPerf : 0;

/* ---------- UI ---------- */
const css = document.createElement("style");
css.textContent = `
#bux-panel{position:fixed;top:14px;right:14px;z-index:2147483647;width:230px;background:#151a23;color:#e7ecf3;
 font:13px -apple-system,Segoe UI,Roboto,Arial;border:1px solid #2a3243;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.5);padding:12px}
#bux-panel h4{margin:0 0 8px;font-size:13px}#bux-panel small{color:#8a94a6}
#bux-panel button{width:100%;margin:4px 0;padding:8px;border-radius:8px;border:1px solid #2a3243;background:#1b2130;color:#e7ecf3;font-size:13px;cursor:pointer}
#bux-panel button.pri{background:#4f8cff;border-color:#4f8cff;color:#fff;font-weight:600}
#bux-panel button.go{background:#37d67a;border-color:#37d67a;color:#04210f;font-weight:700}
#bux-panel button.stop{background:#ff5470;border-color:#ff5470;color:#fff;font-weight:700}
#bux-panel .row{display:flex;justify-content:space-between;margin:4px 0}
#bux-proc{display:none;margin-top:6px}
#bux-proc .stage{font-size:12.5px;font-weight:600;color:#e7ecf3;margin:2px 0}
#bux-proc .bar{height:8px;border-radius:999px;background:#232a38;overflow:hidden;margin:6px 0}
#bux-proc .fill{height:100%;width:0;border-radius:999px;background:linear-gradient(90deg,#4f8cff,#8b5cf6,#22d3ee,#4f8cff);background-size:300% 100%;animation:bux-sh 2.4s linear infinite;transition:width .6s ease}
@keyframes bux-sh{0%{background-position:0 0}100%{background-position:300% 0}}
#bux-proc .eta{font-size:11px;color:#8a94a6;min-height:14px}
#bux-proc .ctl{display:flex;gap:6px;margin-top:6px}
#bux-proc .ctl button{width:auto;flex:1;padding:6px;font-size:12px;margin:0}
#bux-proc .ctl button.done{background:#37d67a;border-color:#37d67a;color:#04210f;font-weight:700}
#bux-proc .log{font-size:10.5px;color:#6b7488;margin-top:4px;max-height:42px;overflow:hidden;line-height:1.3}
#bux-dot{position:fixed;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;border:3px solid #4f8cff;
 background:rgba(79,140,255,.25);box-shadow:0 0 18px rgba(79,140,255,.6);z-index:2147483646;pointer-events:none;display:none}
#bux-cal{position:fixed;inset:0;background:rgba(6,8,12,.94);z-index:2147483647;display:none}
#bux-cal .msg{position:absolute;top:24px;left:0;right:0;text-align:center;font-size:15px;color:#fff}
.bux-caldot{position:absolute;width:34px;height:34px;border-radius:50%;background:#ff5470;border:3px solid #fff;
 cursor:pointer;transform:translate(-50%,-50%);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#fff}
.bux-caldot.done{background:#37d67a;color:#04210f}
#webgazerVideoContainer{display:none!important;z-index:2147483645!important;top:auto!important;bottom:64px!important;left:10px!important;right:auto!important;width:180px!important;height:auto!important;opacity:.92;border-radius:10px;overflow:hidden;box-shadow:0 6px 24px rgba(0,0,0,.5)}
html.bux-show-cam #webgazerVideoContainer{display:block!important}
#webgazerFaceOverlay,#webgazerFaceFeedbackBox{display:none!important}`;
document.documentElement.appendChild(css);

const panel = document.createElement("div"); panel.id="bux-panel";
panel.innerHTML = `<h4>😴 Boring UX <small>any-site</small></h4>
 <button class="pri" id="bux-cam">Enable camera</button>
 <button id="bux-cam-view" disabled style="font-size:12px;padding:6px">👁 Show camera preview</button>
 <button id="bux-cal-btn" disabled>Calibrate gaze</button>
 <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#8a94a6;margin:4px 0"><input type="checkbox" id="bux-dot-toggle" checked> show gaze dot (hide for participant)</label>
 <button class="go" id="bux-start" disabled>● Start</button>
 <button id="bux-selftest" disabled style="font-size:12px;padding:6px" title="8-second look LEFT/RIGHT/TOP/BOTTOM prompt — proves the left/right orientation of this session">👀 Sign self-test (8 s)</button>
 <button class="stop" id="bux-stop" disabled>■ Stop &amp; save</button>
 <div class="row"><span>Region</span><b id="bux-region">—</b></div>
 <div class="row"><span>Gaze</span><b id="bux-status">idle</b></div>
 <div style="font-size:11px;color:#8a94a6;margin-top:6px" id="bux-hint">Enable camera → Calibrate → Start. Stay on this tab while recording.</div>
 <div id="bux-proc">
   <div class="stage" id="bux-proc-stage">Processing…</div>
   <div class="bar"><div class="fill" id="bux-proc-fill"></div></div>
   <div class="eta" id="bux-proc-eta"></div>
   <div class="ctl"><button id="bux-proc-pause">⏸ Pause</button><button id="bux-proc-open" disabled>Open report</button></div>
   <div class="log" id="bux-proc-log"></div>
 </div>`;
document.documentElement.appendChild(panel);
const dot = document.createElement("div"); dot.id="bux-dot"; document.documentElement.appendChild(dot);
const cal = document.createElement("div"); cal.id="bux-cal"; cal.innerHTML='<div class="msg"></div>'; document.documentElement.appendChild(cal);
const $ = id => document.getElementById(id);
const setHint = t => $("bux-hint").textContent = t;

/* ---------- gaze ---------- */
function onGaze(data){
  if(!data) return; const gx=data.x, gy=data.y; S.lastGx=gx; S.lastGy=gy;
  if(S.showDot!==false){ dot.style.display="block"; dot.style.left=gx+"px"; dot.style.top=gy+"px"; } else dot.style.display="none";
  const W=innerWidth,H=innerHeight; let col=null,cell="outside";
  if(gx>=0&&gx<=W&&gy>=0&&gy<=H){
    const fx=gx/W, fy=gy/H;
    col = fx<1/3?"L":fx>2/3?"R":"C";
    const row = fy<1/3?"T":fy>2/3?"B":"M";
    cell = row+col;
  }
  $("bux-region").textContent = col?({L:"◄ LEFT",C:"● CTR",R:"RIGHT ►"}[col]):"—";
  if(S.recording){
    const t=nowRel();
    S.gaze.push({t:Math.round(t),x:Math.round(gx),y:Math.round(gy),col:col||"",cell});
    if(col){ if(S.lastRegion&&S.lastGazeT) S.regionTime[S.lastRegion]+=(t-S.lastGazeT); S.lastRegion=col; S.lastGazeT=t; }
  }
}
async function waitFeed(){ for(let i=0;i<40;i++){ const v=document.getElementById("webgazerVideoFeed"); if(v&&v.srcObject&&v.srcObject.getVideoTracks().length) return v.srcObject.getVideoTracks()[0]; await sleep(200);} return null; }

$("bux-cam-view").onclick = () => {
  const on = document.documentElement.classList.toggle("bux-show-cam");
  $("bux-cam-view").textContent = on ? "🙈 Hide camera preview" : "👁 Show camera preview";
};
$("bux-dot-toggle").onchange = e => { S.showDot = e.target.checked; if(!S.showDot) dot.style.display="none"; };
S.showDot = true;
$("bux-cam").onclick = async () => {
  $("bux-cam").disabled=true; $("bux-cam").textContent="Starting…";
  try{
    if(!window.webgazer){ alert("WebGazer not loaded"); return; }
    webgazer.params.showVideoPreview=true; webgazer.showPredictionPoints(false); webgazer.applyKalmanFilter(true);
    await webgazer.setRegression("ridge").setGazeListener(onGaze).begin();
    // WebGazer trains on mouse move/click by default → the dot "follows the cursor".
    // Turn that off so gaze is driven by the eyes + our explicit calibration dots only.
    try{ webgazer.removeMouseEventListeners(); }catch(_){}
    try{ webgazer.showFaceOverlay(false); webgazer.showFaceFeedbackBox(false); }catch(_){}
    S.camTrack = await waitFeed(); S.camReady=true;
    $("bux-cam").textContent="Camera on ✓"; $("bux-cal-btn").disabled=false; $("bux-start").disabled=false; $("bux-cam-view").disabled=false;
    dot.style.display="block"; setHint("Face hidden by default. Calibrate, then Start. (Camera still records to face.webm.)");
  }catch(e){
    $("bux-cam").disabled=false; $("bux-cam").textContent="Enable camera";
    // Distinguish site-policy block from a normal permission/in-use error
    let policyBlocked=false;
    try{ if(document.featurePolicy && document.featurePolicy.allowsFeature && !document.featurePolicy.allowsFeature("camera")) policyBlocked=true; }catch(_){}
    if(e && e.name==="NotAllowedError" && policyBlocked){
      alert("This site blocks the camera via its own security policy (Permissions-Policy: camera=()).\n\nThe user permission is fine — the SITE forbids camera for anything in its page, including this tool. It can't be eye-tracked in this mode.\n\nWorks on the vast majority of sites that don't set this; for locked-down sites we need the offscreen-camera build.");
      setHint("⚠ Site policy blocks camera (Permissions-Policy). Try another site, or ask for the offscreen build.");
    } else if(e && e.name==="NotReadableError"){
      alert("Camera is in use by another tab/app. Close the other tab (e.g. the localhost recorder) and try again.");
    } else {
      alert("Camera failed: "+(e&&e.message||e)+"\nAllow camera for this site and make sure Chrome has camera access in macOS System Settings › Privacy › Camera.");
    }
  }
};

/* ---------- calibration + validation ---------- */
$("bux-cal-btn").onclick = startCal;
function startCal(){
  cal.style.display="block"; cal.querySelectorAll(".bux-caldot").forEach(d=>d.remove());
  cal.querySelector(".msg").innerHTML='Click each red dot <b>4 times</b> while looking at it. <small>13 points, then an accuracy check.</small>';
  const pts=[[10,12],[50,12],[90,12],[30,30],[70,30],[10,50],[50,50],[90,50],[30,70],[70,70],[10,88],[50,88],[90,88]];
  let remaining=pts.length;
  pts.forEach(([x,y])=>{ const d=document.createElement("div"); d.className="bux-caldot";
    d.style.left=x+"vw"; d.style.top=y+"vh"; let c=0; d.textContent="0/4";
    d.onclick=()=>{ c++; d.textContent=c+"/4"; if(c>=4){ d.classList.add("done"); d.style.pointerEvents="none";
      if(--remaining===0) setTimeout(validate,300); } };
    cal.appendChild(d); });
}
async function validate(){
  cal.querySelectorAll(".bux-caldot").forEach(d=>d.remove());
  cal.querySelector(".msg").innerHTML='<b>Accuracy check</b> — just LOOK at each dot.';
  const vpts=[[25,25],[75,25],[25,75],[75,75]], errs=[];
  for(const [vx,vy] of vpts){ const d=document.createElement("div"); d.className="bux-caldot";
    d.style.left=vx+"vw"; d.style.top=vy+"vh"; d.style.pointerEvents="none"; d.textContent="👁"; cal.appendChild(d);
    await sleep(700); const tx=vx/100*innerWidth, ty=vy/100*innerHeight, s=[];
    for(let i=0;i<12;i++){ await sleep(100); if(S.lastGx!=null) s.push(Math.hypot(S.lastGx-tx,S.lastGy-ty)); }
    if(s.length){ s.sort((a,b)=>a-b); errs.push(s[s.length>>1]); } d.remove(); }
  cal.style.display="none"; S.calibrated=true;
  S.accuracyPx = errs.length?Math.round(errs.reduce((a,b)=>a+b,0)/errs.length):null;
  setHint("Calibrated ✓ accuracy ≈ "+(S.accuracyPx??"?")+"px. Press Start.");
}

/* ---------- sign self-test (design §7.1): LEFT 2s → RIGHT 2s → TOP 2s → BOTTOM 2s, logged as `selftest` events ---------- */
function beep(){ try{ const A=new (window.AudioContext||window.webkitAudioContext)(); const o=A.createOscillator(), g=A.createGain();
  o.frequency.value=880; g.gain.value=0.15; o.connect(g).connect(A.destination); o.start(); setTimeout(()=>{ try{o.stop(); A.close();}catch(_){} },90); }catch(_){} }
$("bux-selftest").onclick = runSelfTest;
async function runSelfTest(){
  if(!S.recording){ alert("Start the recording first, then run the self-test."); return; }
  $("bux-selftest").disabled=true;
  const phases=[["LEFT","◀ Look at the LEFT edge","left:3vw;top:50vh"],["RIGHT","Look at the RIGHT edge ▶","right:3vw;top:50vh"],
                ["TOP","▲ Look at the TOP edge","left:50vw;top:4vh"],["BOTTOM","Look at the BOTTOM edge ▼","left:50vw;bottom:4vh"]];
  cal.style.cssText=CAL_CSS; cal.innerHTML="";
  const msg=document.createElement("div"); msg.style.cssText="position:fixed;top:24px;left:0;right:0;text-align:center;font:15px -apple-system,Arial;color:#fff;z-index:2147483647";
  msg.textContent="Sign self-test — follow the dot with your EYES (head can move too). 8 seconds."; cal.appendChild(msg);
  const dot2=document.createElement("div"); cal.appendChild(dot2);
  for(const [phase,label,pos] of phases){
    dot2.style.cssText="position:fixed;transform:translate(-50%,-50%);width:44px;height:44px;border-radius:50%;background:#ff5470;border:4px solid #fff;box-shadow:0 0 24px #ff5470;z-index:2147483647;"+pos;
    if(pos.startsWith("right")) dot2.style.transform="translate(50%,-50%)";
    if(pos.includes("bottom")) dot2.style.transform="translate(-50%,50%)";
    msg.textContent=label; beep(); ev({type:"selftest",txt:phase});
    await sleep(2000);
  }
  ev({type:"selftest",txt:"END"}); cal.style.cssText="display:none"; cal.innerHTML="";
  setHint("Self-test recorded ✓ (analysed offline: LEFT/RIGHT/TOP/BOTTOM medians prove the orientation).");
}

/* ---------- native capture (no snippet — we are the page) ---------- */
function isClickable(el){ let n=el,d=0; while(n&&n.nodeType===1&&d<6){ if(/^(A|BUTTON|INPUT|SELECT|TEXTAREA|LABEL|SUMMARY|OPTION)$/.test(n.tagName))return true;
  if(n.getAttribute){ const r=n.getAttribute("role"); if(r&&/^(button|link|tab|checkbox|radio|menuitem|switch|option)$/.test(r))return true;
    if(n.hasAttribute("onclick")||n.hasAttribute("href"))return true; const ti=n.getAttribute("tabindex"); if(ti!=null&&ti!=="-1")return true; }
  try{ if(getComputedStyle(n).cursor==="pointer")return true; }catch(e){} n=n.parentElement; d++; } return false; }
function ev(o){ if(S.recording){ o.t=Math.round(nowRel()); S.events.push(o); } }

document.addEventListener("click",e=>{ if(!S.recording)return; const el=e.target||{}; const clickable=isClickable(el);
  let rect=null; try{ const b=(el.getBoundingClientRect?el:el.parentElement).getBoundingClientRect(); rect=[Math.round(b.left),Math.round(b.top),Math.round(b.width),Math.round(b.height)]; }catch(_){}
  ev({type:"click",x:e.clientX,y:e.clientY,tag:el.tagName||"",txt:(el.innerText||el.value||"").toString().trim().slice(0,60),clickable,rect,gazeRegion:S.lastRegion||"?"});
  try{ if(window.webgazer) webgazer.recordScreenPosition(e.clientX,e.clientY,"click"); }catch(_){}
  const now=performance.now(); S.recentClicks.push({x:e.clientX,y:e.clientY,t:now});
  S.recentClicks=S.recentClicks.filter(c=>now-c.t<1200);
  if(S.recentClicks.filter(c=>Math.hypot(c.x-e.clientX,c.y-e.clientY)<40).length>=3){ ev({type:"rage_click",x:e.clientX,y:e.clientY}); S.recentClicks=[]; }
},true);
document.addEventListener("mousemove",e=>{ if(!S.recording)return; const now=performance.now(); if(now-S.lastMouse<66)return; S.lastMouse=now; S.mouse.push({t:Math.round(nowRel()),x:e.clientX,y:e.clientY}); },{passive:true,capture:true});
let lastY=0;
window.addEventListener("scroll",()=>{ if(!S.recording)return; const y=scrollY||0, dh=Math.max(document.body.scrollHeight,document.documentElement.scrollHeight)-innerHeight, pct=dh>0?Math.min(100,Math.round(y/dh*100)):0;
  if(pct>S.maxScroll)S.maxScroll=pct; const dir=y>lastY?1:y<lastY?-1:0;
  if(dir){ if(!S.lastDir)S.dirWinStart=performance.now(); else if(dir!==S.lastDir){ if(performance.now()-S.dirWinStart>2500){S.dirChanges=0;S.dirWinStart=performance.now();} if(++S.dirChanges>=4){ ev({type:"scroll_thrash",pct}); S.dirChanges=0; } } S.lastDir=dir; } lastY=y;
},{passive:true});
function announce(){ if(!S.recording)return; const p={type:"page",url:location.href,title:document.title}; ev(p); S.pages.push({url:location.href,title:document.title,startT:Math.round(nowRel()),endT:null}); }
["pushState","replaceState"].forEach(m=>{ const o=history[m]; history[m]=function(){ const r=o.apply(this,arguments); setTimeout(announce,0); return r; }; });
addEventListener("popstate",announce);
document.addEventListener("visibilitychange",()=>{ if(!S.recording)return; if(document.hidden){ ev({type:"tracking_paused"}); } else { ev({type:"tracking_resumed"}); alert("Boring UX: eye tracking paused while the tab was hidden — gaze has a gap."); }});

/* ---------- record ---------- */
function mime(){ return ["video/webm;codecs=vp9,opus","video/webm;codecs=vp8,opus","video/webm"].find(m=>MediaRecorder.isTypeSupported(m))||""; }
$("bux-start").onclick = async () => {
  if(!S.camReady){ alert("Enable camera first"); return; }
  if(!S.calibrated && !confirm("Gaze not calibrated — accuracy will be poor. OK = record anyway, Cancel = calibrate.")){ startCal(); return; }
  try{ S.mic = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}}); }catch(e){ S.mic=null; }
  let screen=null;
  if(confirm("Also record the SCREEN? (OK = pick this tab to share; Cancel = gaze+face+audio only)")){
    try{ screen = await navigator.mediaDevices.getDisplayMedia({video:{frameRate:15},audio:false}); }catch(e){}
  }
  const m=mime(); S.recorders=[]; S.chunks={face:[],audio:[],screen:[]};
  S.recording=true; S.startWall=Date.now(); S.startPerf=performance.now();
  S.gaze=[]; S.mouse=[]; S.events=[]; S.pages=[]; S.regionTime={L:0,C:0,R:0}; S.lastGazeT=0; S.lastRegion=null; S.maxScroll=0;
  const rec=(stream,key,vb)=>{ if(!stream)return; const opts={mimeType:m}; if(vb)opts.videoBitsPerSecond=vb; let r; try{r=new MediaRecorder(stream,opts);}catch(e){r=new MediaRecorder(stream);}
    r.ondataavailable=e=>e.data.size&&S.chunks[key].push(e.data); r.start(1000); S.recorders.push(r); };
  if(S.camTrack) rec(new MediaStream([S.camTrack.clone(), ...(S.mic?[S.mic.getAudioTracks()[0].clone()]:[])]),"face",1200000);
  if(S.mic) rec(S.mic,"audio",0);
  if(screen){ const sv=screen.getVideoTracks()[0]; sv.addEventListener("ended",()=>S.recording&&stop());
    rec(new MediaStream([sv,...(S.mic?[S.mic.getAudioTracks()[0].clone()]:[])]),"screen",2500000); }
  announce();
  // lock the panel to recording state — Start/camera/calibrate off, only Stop is live
  $("bux-start").disabled=true; $("bux-cam").disabled=true; $("bux-cal-btn").disabled=true; $("bux-cam-view").disabled=true;
  $("bux-stop").disabled=false; $("bux-selftest").disabled=false; $("bux-status").textContent="REC ●";
  setHint("Recording… stay on this tab. Press Stop to save.");
};
$("bux-stop").onclick = stop;

// Fully release the camera/mic and tear WebGazer down — no lingering mirror.
function killCamera(){
  try{ if(window.webgazer){ webgazer.clearGazeListener&&webgazer.clearGazeListener(); webgazer.pause&&webgazer.pause(); webgazer.end&&webgazer.end(); } }catch(e){}
  try{ const v=document.getElementById("webgazerVideoFeed"); if(v&&v.srcObject) v.srcObject.getTracks().forEach(t=>t.stop()); }catch(e){}
  try{ if(S.camTrack) S.camTrack.stop(); }catch(e){}
  try{ if(S.mic) S.mic.getTracks().forEach(t=>t.stop()); }catch(e){}
  ["webgazerVideoContainer","webgazerVideoFeed","webgazerFaceOverlay","webgazerFaceFeedbackBox"].forEach(id=>{ const el=document.getElementById(id); if(el) el.remove(); });
  document.documentElement.classList.remove("bux-show-cam");
  dot.style.display="none"; S.camTrack=null; S.mic=null; S.camReady=false; S.calibrated=false; S.accuracyPx=null;
}
async function stop(){
  if(!S.recording)return; S.recording=false; $("bux-stop").disabled=true; $("bux-status").textContent="saving…";
  // 1) stop every recorder and release ALL its tracks
  await Promise.all(S.recorders.map(r=>new Promise(res=>{ if(r.state==="inactive")return res(); r.onstop=res; try{r.stop();}catch(e){res();} })));
  S.recorders.forEach(r=>{ try{ r.stream.getTracks().forEach(t=>t.stop()); }catch(e){} });
  S.recorders=[];
  // 2) fully shut down the camera/WebGazer (kills the mirror)
  killCamera();
  // 3) save into a folder named by site + time
  const p=S.pages[S.pages.length-1]; if(p&&p.endT==null)p.endT=Math.round(nowRel());
  const stamp=new Date(S.startWall).toISOString().replace(/[:.]/g,"-").slice(0,19);
  const host=(location.hostname||"site").replace(/[^a-z0-9.-]/gi,"_");
  const folder=`boring-ux/${host}-${stamp}`;
  const m=mime();
  // Build every file once: saved to Downloads (the user's copy) AND uploaded to the local processing service.
  const files={}; const txt=s=>new Blob([s],{type:"text/plain"});
  files["gaze.csv"]=txt(csv(["t_ms","x","y","h_region","cell"], S.gaze.map(g=>[g.t,g.x,g.y,g.col,g.cell])));
  files["mouse.csv"]=txt(csv(["t_ms","x","y"], S.mouse.map(g=>[g.t,g.x,g.y])));
  files["events.csv"]=txt(csv(["t_ms","type","detail","gazeRegion","extra","x","y","clickable","rect"],
    S.events.map(e=>[e.t,e.type,e.txt||e.title||e.note||"",e.gazeRegion||"",e.url||e.pct||"",e.x??"",e.y??"",e.clickable===undefined?"":(e.clickable?1:0),e.rect?e.rect.join(" "):""])));
  files["session.json"]=txt(JSON.stringify(summary(),null,2));
  files["SESSION-AI.md"]=txt(aiBundle());
  if(S.chunks.face.length) files["face.webm"]=new Blob(S.chunks.face,{type:m});
  if(S.chunks.audio.length) files["audio.webm"]=new Blob(S.chunks.audio,{type:"audio/webm"});
  if(S.chunks.screen.length) files["screen.webm"]=new Blob(S.chunks.screen,{type:m});
  S.chunks={face:[],audio:[],screen:[]};
  const results=[];
  for(const [name,blob] of Object.entries(files)) results.push(await dlBlob(folder+"/"+name, blob));
  // 4) reset the panel to the initial state (must Enable camera again for a new session)
  $("bux-cam").disabled=false; $("bux-cam").textContent="Enable camera";
  $("bux-cal-btn").disabled=true; $("bux-cam-view").disabled=true; $("bux-start").disabled=true; $("bux-stop").disabled=true; $("bux-selftest").disabled=true;
  $("bux-status").textContent="saved ✓ · idle"; $("bux-region").textContent="—";
  // 5) HONEST result: did the files really go into one folder, or did we fall back to loose files?
  const failed=results.filter(r=>r&&!r.ok);
  if(failed.length===0){
    setHint("✅ Saved as ONE folder → Downloads/"+folder+"/ ("+S.gaze.length+" gaze, "+S.events.length+" events). Camera off.");
    startProcessing(`${host}-${stamp}`, files);   // upload to the local processing service → panel becomes the processing view
  }else{
    const reason=failed[0].err||"unknown";
    const needsReload=/receiving end|establish connection|no response/i.test(reason);
    setHint("⚠ Saved as LOOSE files (couldn't make a folder). "+(needsReload?"Fix: reload the extension at chrome://extensions, then run again.":"Reason: "+reason)+" Camera off.");
  }
}
/* ---------- processing view — the local service (127.0.0.1:7331) owns the job; this panel is just a view of it ---------- */
const daemon=(method,path,body)=>new Promise(res=>{ try{ chrome.runtime.sendMessage({bux:"daemon",method,path,body},r=>res(r||{ok:false,err:(chrome.runtime.lastError&&chrome.runtime.lastError.message)||"no response"})); }catch(e){ res({ok:false,err:String(e)}); } });
const fmtEta=s=>s==null?"":s<60?`about ${Math.max(5,Math.round(s/5)*5)} s left`:`about ${Math.ceil(s/60)} min left`;
const PANEL_BTNS=["bux-cam","bux-cam-view","bux-cal-btn","bux-start","bux-selftest","bux-stop"];
let procTimer=null;
function showProc(job){
  $("bux-proc").style.display="block"; PANEL_BTNS.forEach(id=>$(id).style.display="none");
  const pct=Math.round((job.progress||0)*100), fill=$("bux-proc-fill");
  fill.style.width=(job.status==="done"?100:pct)+"%";
  const labels={queued:"Queued…",running:job.stage_label||"Processing…",paused:"Paused",done:"Report ready ✓",error:"Something went wrong",cancelled:"Cancelled"};
  $("bux-proc-stage").textContent=(labels[job.status]||job.status)+(job.status==="running"?` · ${pct}%`:"");
  const rel=(job.folder||"").split("/").pop()||"";
  $("bux-proc-eta").textContent=job.status==="running"||job.status==="queued"?fmtEta(job.eta_s):job.status==="done"?"Opened · saved to Downloads/boring-ux/"+rel+"/report.pdf":job.status==="error"?(job.error||"see the service log"):job.status==="paused"?"Paused — resume when you're ready":"";
  if(job.status==="done") saveReportOnce(job);
  $("bux-proc-log").textContent=(job.warnings||[]).concat((job.log||[]).slice(-2)).join(" · ");
  const pb=$("bux-proc-pause"), st=job.status;
  pb.textContent=st==="paused"?"▶ Resume":(st==="running"||st==="queued")?"⏸ Pause":(st==="error"||st==="cancelled")?"↻ Retry":"New session";
  pb.onclick=async()=>{ if(st==="paused")await daemon("POST",`/jobs/${job.id}/resume`); else if(st==="running"||st==="queued")await daemon("POST",`/jobs/${job.id}/pause`); else if(st==="error"||st==="cancelled")await daemon("POST",`/jobs/${job.id}/retry`); else return endProc(); pollJob(job.id); };
  const ob=$("bux-proc-open"); ob.disabled=st!=="done"; ob.className=st==="done"?"done":""; ob.onclick=()=>daemon("POST",`/jobs/${job.id}/open`);
  if(st==="done"){ fill.style.animation="none"; fill.style.background="#37d67a"; } else { fill.style.animation=""; fill.style.background=""; }
  if(st==="done"||st==="error"||st==="cancelled"){ clearInterval(procTimer); procTimer=null; }
}
function endProc(){ try{ chrome.storage.local.remove("buxJob"); }catch(_){} clearInterval(procTimer); procTimer=null; $("bux-proc").style.display="none"; PANEL_BTNS.forEach(id=>$(id).style.display=""); setHint("Ready. Enable camera to begin."); }
async function pollJob(id){
  const r=await daemon("GET",`/jobs/${id}`);
  if(!r.ok||!r.json){ $("bux-proc").style.display="block"; $("bux-proc-stage").textContent="Processing service not reachable"; $("bux-proc-eta").textContent="Start it once: bash tools/install-daemon.sh (Claude Code can do this for you)."; return; }
  if(r.json.status==="done" && (Date.now()/1000-(r.json.updated||0))>3600){ endProc(); return; }   // stale finished job from long ago
  showProc(r.json);
  if(!procTimer && ["queued","running","paused"].includes(r.json.status)) procTimer=setInterval(()=>pollJob(id),2000);
}
// macOS blocks background services from reading ~/Downloads, so the session is UPLOADED to the service over
// localhost (it lives in ~/.boring-ux/sessions/<name>/); the finished report is fetched back and saved to Downloads.
async function startProcessing(name, files){
  const h=await daemon("GET","/health");
  if(!h.ok){ setHint("Saved ✓. Automatic processing is off — the local service isn't running. Run once: bash tools/install-daemon.sh (or ask Claude Code). You can also analyze later with tools/bux-analyze-video.py."); return; }
  $("bux-proc").style.display="block"; PANEL_BTNS.forEach(id=>$(id).style.display="none");
  $("bux-proc-stage").textContent="Handing the session to the processing service…"; $("bux-proc-fill").style.width="2%"; $("bux-proc-eta").textContent="";
  try{
    let done=0, total=Object.keys(files).length;
    for(const [fname,blob] of Object.entries(files)){
      const r=await fetch(`http://127.0.0.1:7331/sessions/${encodeURIComponent(name)}/${encodeURIComponent(fname)}`,{method:"POST",body:blob});
      if(!r.ok) throw new Error(fname+" → HTTP "+r.status);
      done++; $("bux-proc-fill").style.width=(2+6*done/total)+"%";
    }
  }catch(e){ $("bux-proc-stage").textContent="Couldn't hand the files to the processing service"; $("bux-proc-eta").textContent=String(e&&e.message||e)+" — the session is still saved in Downloads/boring-ux/"+name; return; }
  const r=await daemon("POST","/jobs",{session:name, product:location.hostname});
  if(!r.ok||!r.json||!r.json.id){ $("bux-proc-stage").textContent="The processing service refused the job"; $("bux-proc-eta").textContent=String(r.err||(r.json&&r.json.error)||r.status); return; }
  try{ chrome.storage.local.set({buxJob:{id:r.json.id,name}}); }catch(_){}
  showProc(r.json); procTimer=setInterval(()=>pollJob(r.json.id),2000);
}
// When the job is done, pull report.pdf from the service once and drop it next to the session in Downloads.
async function saveReportOnce(job){
  try{
    const v=await new Promise(res=>chrome.storage.local.get("buxJob",res)); const bj=(v&&v.buxJob)||{};
    if(bj.saved===job.id) return;
    const name=bj.name||(job.folder||"").split("/").pop();
    const r=await fetch(`http://127.0.0.1:7331/jobs/${job.id}/report.pdf`); if(!r.ok) return;
    await save("boring-ux/"+name+"/report.pdf", await r.blob());
    chrome.storage.local.set({buxJob:Object.assign({},bj,{saved:job.id})});
  }catch(_){}
}
// Re-attach to a job in progress (survives page refresh, new tabs, browser restart — the service keeps the job).
try{ chrome.storage.local.get("buxJob",v=>{ if(v&&v.buxJob&&v.buxJob.id) pollJob(v.buxJob.id); }); }catch(_){}

addEventListener("beforeunload",()=>{ if(S.recording){ try{ S.recorders.forEach(r=>r.state!=="inactive"&&r.stop()); }catch(e){} } killCamera(); });

/* ---------- outputs ---------- */
function summary(){ const g=S.regionTime, gt=g.L+g.C+g.R||1;
  return { tool:"Boring UX extension", site:location.href, startedAt:new Date(S.startWall).toISOString(),
    durationSec:+(nowRel()/1000).toFixed(2), calibrated:S.calibrated, gazeAccuracyPx:S.accuracyPx,
    viewport:{stageW:innerWidth,stageH:innerHeight,winW:innerWidth,winH:innerHeight},
    screen:{width:screen.width,height:screen.height,availHeight:screen.availHeight}, dpr:devicePixelRatio,
    window:{screenX,screenY,outerWidth,outerHeight,innerWidth,innerHeight},
    totals:{pages:S.pages.length,gazeSamples:S.gaze.length,mouseSamples:S.mouse.length,clicks:S.events.filter(e=>e.type==="click").length},
    gazeDistribution:{left:+(g.L/gt*100).toFixed(1),center:+(g.C/gt*100).toFixed(1),right:+(g.R/gt*100).toFixed(1)},
    events:S.events }; }
// The FULL analysis brief, embedded so SESSION-AI.md is one self-contained file:
// data + exactly how to turn it into the graded report/dashboard. No other file needed.
const ANALYZE_INSTRUCTIONS = `## ▶ How to turn this session into the full UX report

Give an AI agent (Claude/ChatGPT, or Claude Code in this folder) **this file** plus the other
files in this same folder (audio.webm, gaze.csv, mouse.csv, events.csv, session.json). Then:

**Role:** You are a senior UX researcher + product manager. I ran a moderated, think-aloud
usability test with webcam eye-tracking. Analyze this session and produce ONE self-contained,
print-to-PDF HTML report. Be honest: webcam gaze is directional within the measured accuracy
radius (see gazeAccuracyPx); n=1 unless more sessions were combined; if events.csv is empty, say so.

### Step 1 — Transcribe the think-aloud audio (100% local, private)
\`\`\`bash
brew install whisper-cpp ffmpeg
ffmpeg -i audio.webm -ar 16000 -ac 1 audio.wav
whisper-cli -m ggml-large-v3-turbo.bin -f audio.wav -l auto -osrt -otxt -of transcript
\`\`\`
Collapse repeated identical lines (silence hallucinations). Keep the original language and add an English translation for every quote.

### Step 2 — Fuse everything on one clock (t=0 = recording start)
The timeline above, gaze.csv, events.csv and transcript.srt share the same clock. For every spoken
segment compute the dominant gaze region (L/C/R) + 3×3 cell + on-screen % during it. Tie every
finding to THREE signals: what they said + where the eyes were that second + how long it took.
Classify each segment's intent (taking-action / seeking-data / confused-searching) and pool gaze per
intent into a 3×3 grid. Compute: L/C/R dwell, 3×3 heatmap, attention-over-time, engagement (on-screen %),
scanning intensity (region-switches/min), fixations (>1s = deep), silent-gap latency (pauses ≥4s + what
the eyes did), per-page time and per-field fill-time (events.csv), and frustration signals.

### Step 3 — Produce the report / dashboard with EXACTLY these sections
1. Title + product name.
2. Overall grade /10 with sub-scores (AI, visual design, data clarity, delight, task efficiency, actionability, discoverability) + one-line verdict.
3. Executive summary (dark card): fix-first (P0), fix-next (P1), keep (delighters).
4. Method & confidence — state gazeAccuracyPx; flag uncalibrated gaze as directional; flag empty events.csv.
5. Journey map per session: stage · time · emotion (emoji) · gaze/behaviour · opportunity.
6. Attention analytics — GRAPHED as inline SVG: gaze-region ribbon (orange=L, blue=C, green=R, grey=away) + engagement line + searching-intensity line + feature-phase bar. This is the dashboard.
7. Feature-by-feature scorecard (table): feature · time · gaze L/C/R bar · on-screen % · search/min · signal (Delight/Friction/Confusion/Request) · friction index 0–100 · recommendation.
8. Attention×friction matrix + prioritised roadmap.
9. Detailed findings P0→P1→Keep→P2 — each: original-language quote + English + 👁 eyes-at-that-moment + ⏱ timing + recommendation.
10. Latency & timing table (silent gaps, what the eyes did, meaning).
11. RICE backlog (ticket-ready).
12. Conclusion — where the eyes go by intent: three 3×3 heatmaps (action / data / confused) + a "where to place actions, data, filters" table.
13. Appendix: the complete fused timeline, every segment verbatim, with gaze region/cell, on-screen %, and the latency gap before it. Nothing summarised.

### Design (inline <style>, no external assets)
System sans-serif; max-width ~960px. text #1a2233, muted #6b7488, borders #e6eaf2; P0 #d33, P1 #e8871e,
accent #4f8cff, delight #0a9d54. Original-language quotes in a green-left-border box (direction:rtl for
Arabic) with English italic beneath. 👁 callouts in a light-blue box. Table headers #f0f3f9. @media print{} for clean PDF.

### Example finding (match this shape)
> **P0 · "New Leave" is invisible.** 〔quote, original〕 "كيف أضيف إجازة جديدة؟ أنا ما أدري" — _"How do I add a new leave? I don't know."_
> 👁 Eyes scanning top-right→left, 30% off-screen, never settled for ~8 min. ⏱ First click at 0:27, task not started until 8:00.
> **Recommendation:** a high-contrast, text-labelled "＋ New Leave" button, fixed top-right, on Home and the list. **RICE 60.**

### RICE backlog format (one row per recommendation)
| # | Ticket | Reach(1–10) | Impact(0.25–3) | Confidence(0.5–1) | Effort(person-months) | RICE = R×I×C÷E | Ship (Now/Next/Later) |

### Step 4 — Save
Write report.html in this folder, then render a PDF:
\`\`\`bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --no-pdf-header-footer --print-to-pdf=report.pdf report.html
\`\`\`
Never invent quotes or events not present in the data.`;

function aiBundle(){ const sum=summary(); const {events,...meta}=sum;
  const ts=ms=>{const s=ms/1000;return String(Math.floor(s/60)).padStart(2,"0")+":"+(s%60).toFixed(1).padStart(4,"0");};
  const L=[];
  for(const e of S.events){ let l=e.type.toUpperCase();
    if(e.type==="page")l=`PAGE → ${e.title||e.url}`; else if(e.type==="click")l=`CLICK ${e.clickable===false?"[DEAD] ":""}"${e.txt||e.tag}" gaze=${e.gazeRegion}`;
    else if(e.type==="rage_click")l="RAGE-CLICK"; else if(e.type==="scroll_thrash")l=`SCROLL-THRASH @${e.pct}%`;
    else if(e.type==="tracking_paused")l="⚠ EYE-TRACKING PAUSED (tab hidden)"; else if(e.type==="tracking_resumed")l="EYE-TRACKING RESUMED"; L.push([e.t,l]); }
  let last=-1e9; for(const g of S.gaze){ if(g.t-last>=500){ last=g.t; L.push([g.t,`GAZE ${g.col||"·"} ${g.cell} (${g.x},${g.y})`]); } }
  last=-1e9; for(const m of S.mouse){ if(m.t-last>=500){ last=m.t; L.push([m.t,`MOUSE (${m.x},${m.y})`]); } }
  L.sort((a,b)=>a[0]-b[0]);
  return `# Boring UX — AI-ready session bundle (${location.hostname})
Captured live on the real site via the Boring UX browser extension.

## Metadata
\`\`\`json
${JSON.stringify(meta,null,2)}
\`\`\`
Data quality: calibrated=${S.calibrated}${S.accuracyPx!=null?`, gaze accuracy ≈ ${S.accuracyPx}px`:""}. Coordinates are viewport pixels (${innerWidth}×${innerHeight}). Clicks/mouse captured natively.

## Unified timeline (t=0 = start; GAZE & MOUSE @2Hz — full data in the CSVs)
\`\`\`
${L.map(([t,l])=>`[${ts(t)}] ${l}`).join("\n")}
\`\`\`

${ANALYZE_INSTRUCTIONS}`; }

/* ---------- helpers ---------- */
function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
function csv(head,rows){ const esc=v=>{v=(v==null?"":String(v)).replace(/"/g,'""');return /[",\n]/.test(v)?'"'+v+'"':v;};
  return [head.join(","), ...rows.map(r=>r.map(esc).join(","))].join("\n"); }
function toDataURL(blob){ return new Promise((res,rej)=>{ const fr=new FileReader(); fr.onload=()=>res(fr.result); fr.onerror=rej; fr.readAsDataURL(blob); }); }
// Save via chrome.downloads (through the background) so files land in a REAL subfolder
// (chrome.downloads honors "folder/file"; the <a download> attribute flattens "/" to "_").
// Returns {ok, err} — ok:true means it went into the real folder. On failure we fall back
// to a flat <a download> so no data is ever lost, and report WHY so the panel can tell the user.
async function save(name, blob){
  let err=null;
  try{
    const raw = await toDataURL(blob);
    // Rebuild the data URL with a comma-free octet-stream MIME. Two reasons:
    //  1) Chrome keeps the EXACT filename+extension we pass (text/plain would force .txt).
    //  2) A data URL's media-type ends at the FIRST comma — "video/webm;codecs=vp9,opus"
    //     contains a comma, which made Chrome treat "opus;base64,..." as the payload and
    //     write base64 TEXT instead of the video (corrupt face.webm/screen.webm). Slicing
    //     after the fixed ";base64," marker is exact regardless of the blob's MIME.
    const i = raw.indexOf(";base64,");
    if (i < 0) throw new Error("unexpected data URL");
    const dataUrl = "data:application/octet-stream;base64," + raw.slice(i + 8);
    const resp = await chrome.runtime.sendMessage({ bux:"download", filename:name, dataUrl });
    if(resp && resp.ok) return { ok:true };
    err = (resp && resp.err) || "no response from extension background";
  }catch(e){
    // Most common cause: the extension wasn't reloaded, so the new background listener
    // doesn't exist yet ("Could not establish connection. Receiving end does not exist.").
    err = (e && e.message) || String(e);
  }
  console.warn("[BoringUX] folder save failed for", name, "→ falling back to loose file. Reason:", err);
  try{
    const a=document.createElement("a"); a.href=URL.createObjectURL(blob); a.download=name.split("/").pop();
    document.documentElement.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href),8000);
  }catch(e2){ err = err+" | fallback also failed: "+((e2&&e2.message)||e2); }
  return { ok:false, err };
}
function dl(name,text){ return save(name,new Blob([text],{type:"text/plain"})); }
function dlBlob(name,blob){ return save(name,blob); }

// Show whether the local header-override made the camera usable on a locked-down site
try{
  const ok = !(document.featurePolicy && document.featurePolicy.allowsFeature && !document.featurePolicy.allowsFeature("camera"));
  setHint(ok ? "Ready. Enable camera to begin." : "⚠ This site still blocks camera by policy — reload once via the toolbar icon to apply the testing override.");
}catch(_){ setHint("Ready. Enable camera to begin."); }
})();
