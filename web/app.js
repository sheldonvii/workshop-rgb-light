/* 情绪晴雨表 — 页面逻辑。
 * 状态机：idle（未启动）→ starting（确认服务→确认摄像头轨道→预览）→ running → stopping → idle
 * running 内人数子状态：wait（无人）/ ok（单人识别中）/ multi（多人）
 * 异常：服务异常、摄像头异常（权限/无设备/占用/断开）、结果失效（stale）— 均有文案与恢复方式。
 * 调度：同时最多一个推理请求在途；响应按 run_id+seq 校验，过期与跨运行结果丢弃；页面隐藏暂停提交。
 */
"use strict";

const CFG = {
  submitIntervalMs: 350,   // 抓帧提交节奏（忙则跳帧）
  fetchTimeoutMs: 6000,
  staleAfterMs: 5000,      // 超过该时长无新结果 → 结果失效
  failBannerAfter: 3,      // 连续失败次数 → 服务异常横幅
  jpegQuality: 0.8,
  timelineRenderMax: 12,
  timelineKeepMax: 60,
};

const MOOD_COLOR = {
  happy: "#F2C14E", surprise: "#5BC8D8", neutral: "#7D93AD",
  sad: "#5B7DC8", angry: "#D95D4E", fear: "#8E7CC8", disgust: "#7FA35B",
};

/* 隔离的测试帧源模式（仅 ?frames=test 显式开启；正式入口默认摄像头，不受影响）。
 * 开启后：抓帧来自 web/test-frames/ 官方测试图，POST 同一 /api/infer，结果为真实推理。
 * 用于无现场人员时验证 无人/单人/多人 页面状态。运行时页面显示常驻标注。 */
const FRAME_TEST = new URLSearchParams(location.search).get("frames") === "test";
const TEST_CYCLE = ["none", "single", "single", "single", "multi", "multi", "single", "single", "none"];
const TEST_FRAME_MS = 1400;
const testFrames = { blobs: {}, idx: 0, lastSwitch: 0, imgEl: null };

const $ = (id) => document.getElementById(id);
const els = {
  btn: $("btnMain"), btnLabel: $("btnMainLabel"), btnIcon: $("btnMainIcon"),
  svc: $("svcBadge"), svcText: $("svcText"),
  banner: $("banner"), bannerIcon: $("bannerIcon"), bannerText: $("bannerText"), bannerBtn: $("bannerBtn"),
  video: $("video"), overlay: $("overlay"),
  stage: $("stage"), stageIdle: $("stageIdle"), stageStatus: $("stageStatus"), stageStatusIcon: $("stageStatusIcon"),
  stageStatusText: $("stageStatusText"), stageMeta: $("stageMeta"),
  currentPanel: $("currentPanel"), currentLabel: $("currentLabel"), currentEn: $("currentEn"),
  currentScore: $("currentScore"), currentTime: $("currentTime"), currentNote: $("currentNote"),
  comment: $("comment"), timeline: $("timeline"), timelineEmpty: $("timelineEmpty"),
};

const ICONS = window.__ICONS || {};  // index.html 内联的 Lucide path 数据

const S = {
  phase: "idle",            // idle | starting | running | stopping
  runId: null,
  seq: 0,
  lastAcceptedSeq: 0,
  inFlight: null,           // AbortController
  stream: null,
  submitTimer: null,
  trackInfo: null,          // {w,h,fps,label}
  lastOkTs: 0,
  runStartTs: 0,
  failCount: 0,
  stale: false,
  bannerKind: null,         // null | "warn" | "danger"
  bannerId: null,           // 当前横幅标识，避免覆盖更新的状态
  timeline: [],             // [{label,zh,mood,score,t0,t1}]
  accepted: [],             // 最近成功响应时间戳（推理更新率）
  lastPrimary: null,
  lastCount: 0,
  lastTiming: null,
  e2eMs: 0,
  svcState: "loading",
};

/* ---------------- 图标 ---------------- */
function icon(name, size = 18) {
  const paths = ICONS[name] || "";
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}

/* ---------------- 通用 UI ---------------- */
function setBanner(kind, id, html, action) {
  S.bannerKind = kind; S.bannerId = id;
  els.banner.dataset.kind = kind === "danger" ? "danger" : "warn";
  els.bannerIcon.innerHTML = icon(kind === "danger" ? "triangle-alert" : "info", 18);
  els.bannerText.innerHTML = html;
  if (action) {
    els.bannerBtn.hidden = false;
    els.bannerBtn.querySelector("span").textContent = action.label;
    els.bannerBtn.onclick = action.onClick;
  } else {
    els.bannerBtn.hidden = true;
    els.bannerBtn.onclick = null;
  }
  els.banner.hidden = false;
}
function clearBanner(id) {
  if (id && S.bannerId !== id) return;  // 只清自己那类
  S.bannerId = null;
  els.banner.hidden = true;
}
function setSvc(state, text) {
  S.svcState = state;
  els.svc.dataset.s = state;
  els.svcText.textContent = text;
}
function setMainButton(mode) {
  // mode: start（可点开始）| busy（初始化中，禁用）| stop（可点停止）
  if (mode === "start") {
    els.btn.disabled = false;
    els.btn.className = "btn btn-primary";
    els.btnIcon.innerHTML = icon("play", 17);
    els.btnLabel.textContent = "开始体验";
  } else if (mode === "busy") {
    els.btn.disabled = true;
    els.btn.className = "btn btn-primary";
    els.btnIcon.innerHTML = icon("loader-circle", 17);
    els.btnLabel.textContent = "初始化…";
  } else {
    els.btn.disabled = false;
    els.btn.className = "btn btn-ghost";
    els.btnIcon.innerHTML = icon("square", 16);
    els.btnLabel.textContent = "停止";
  }
}

/* ---------------- 服务健康 ---------------- */
async function fetchHealth(timeoutMs = 4000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch("/api/health", { signal: ctl.signal, cache: "no-store" });
    const j = await r.json().catch(() => ({}));
    return { httpOk: r.ok, body: j };
  } finally {
    clearTimeout(t);
  }
}

async function refreshSvcBadge() {
  try {
    const { httpOk, body } = await fetchHealth(3000);
    if (httpOk && body.ok) setSvc("ready", "推理服务就绪");
    else if (body.state === "loading") setSvc("loading", "模型加载中…");
    else setSvc("error", "推理服务异常");
  } catch {
    setSvc("error", "推理服务不可达");
  }
}

/* ---------------- 启动 / 停止 ---------------- */
async function start() {
  if (S.phase !== "idle") return;
  S.phase = "starting";
  setMainButton("busy");
  clearBanner();

  // 1) 先确认服务可用
  let health;
  try {
    health = await fetchHealth(5000);
  } catch {
    S.phase = "idle"; setMainButton("start"); setSvc("error", "推理服务不可达");
    setBanner("danger", "svc",
      "无法连接推理服务。请确认服务已启动（在工程目录执行 <code>./run.sh</code>），然后重试。",
      { label: "重试", onClick: () => { void start(); } });
    return;
  }
  if (!health.httpOk || !health.body.ok) {
    const st = health.body.state || "unknown";
    S.phase = "idle"; setMainButton("start");
    setSvc(st === "loading" ? "loading" : "error",
      st === "loading" ? "模型加载中…" : "推理服务异常");
    setBanner("danger", "svc",
      st === "loading"
        ? "模型正在加载，首次加载约需几秒。请稍候再开始。"
        : `推理服务未就绪（${health.body.message || st}）。可在终端查看 run.sh 输出定位。`,
      { label: "重试", onClick: () => { void start(); } });
    return;
  }
  setSvc("ready", "推理服务就绪");

  // 1.5) 隔离测试帧源：跳过摄像头，用官方测试图走同一推理接口
  if (FRAME_TEST) {
    try {
      for (const name of new Set(TEST_CYCLE)) {
        const r = await fetch(`/test-frames/${name}.jpg`, { cache: "no-store" });
        if (!r.ok) throw new Error(`fetch ${name}: ${r.status}`);
        testFrames.blobs[name] = await r.blob();
      }
    } catch (err) {
      S.phase = "idle"; setMainButton("start");
      setBanner("danger", "cam", `测试帧源加载失败（${err.message}）。请确认 web/test-frames/ 完整。`);
      return;
    }
    S.trackInfo = { w: 1280, h: 720, fps: 30, label: "测试帧源（官方测试图）" };
    beginRunning();
    setBanner("warn", "testsrc",
      "测试帧源模式：画面与结果来自官方测试图的真实推理，非摄像头采集。关闭地址里的 ?frames=test 即回到正式入口。");
    return;
  }

  // 2) 再请求摄像头（权限申请）
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } },
      audio: false,
    });
  } catch (err) {
    S.phase = "idle"; setMainButton("start");
    const name = err && err.name;
    if (name === "NotAllowedError") {
      setBanner("danger", "cam",
        "摄像头权限被拒绝。点击浏览器地址栏左侧的站点图标，把摄像头改为“允许”，然后重试。",
        { label: "重试", onClick: () => { void start(); } });
    } else if (name === "NotFoundError" || name === "OverconstrainedError") {
      setBanner("danger", "cam", "未检测到可用摄像头。请确认 USB 摄像头已连接并被系统识别，然后重试。",
        { label: "重试", onClick: () => { void start(); } });
    } else if (name === "NotReadableError") {
      setBanner("danger", "cam", "摄像头被其他程序占用，无法启动采集。请关闭占用摄像头的程序后重试。",
        { label: "重试", onClick: () => { void start(); } });
    } else {
      setBanner("danger", "cam", `摄像头启动失败（${name || err}）。请重新插拔摄像头后重试。`,
        { label: "重试", onClick: () => { void start(); } });
    }
    return;
  }

  // 3) 确认轨道实际参数（必须真实 ≥1280×720，不以放大冒充）
  const track = stream.getVideoTracks()[0];
  const st = track.getSettings();
  S.trackInfo = { w: st.width || 0, h: st.height || 0, fps: st.frameRate || 0, label: track.label || "" };
  if (S.trackInfo.w < 1280 || S.trackInfo.h < 720) {
    stream.getTracks().forEach((t) => t.stop());
    S.phase = "idle"; setMainButton("start");
    setBanner("danger", "cam",
      `采集分辨率不足：实际轨道 ${S.trackInfo.w}×${S.trackInfo.h}，要求至少 1280×720。请检查摄像头能力与连接（USB 端口/线缆），然后重试。`,
      { label: "重试", onClick: () => { void start(); } });
    return;
  }

  // 4) 预览 + 开始提交
  els.video.srcObject = stream;
  try { await els.video.play(); } catch { /* autoplay muted 通常可放行 */ }
  S.stream = stream;
  track.addEventListener("ended", onTrackEnded);
  beginRunning();
}

function beginRunning() {
  S.phase = "running";
  S.runId = (crypto.randomUUID && crypto.randomUUID()) || String(Date.now());
  S.seq = 0; S.lastAcceptedSeq = 0;
  S.lastOkTs = 0; S.failCount = 0; S.stale = false;
  S.accepted = [];
  S.runStartTs = Date.now();
  setMainButton("stop");
  els.stageIdle.hidden = true;
  els.stage.style.aspectRatio = `${S.trackInfo.w} / ${S.trackInfo.h}`;
  if (FRAME_TEST) {
    if (!testFrames.imgEl) {
      const img = document.createElement("img");
      img.className = "test-preview";
      img.alt = "测试帧画面";
      els.stage.insertBefore(img, els.overlay);
      testFrames.imgEl = img;
    }
    testFrames.idx = 0; testFrames.lastSwitch = 0;
  }
  clearBanner("cam");
  updateStageStatus();
  updateMeta();
  S.submitTimer = setInterval(submitTick, CFG.submitIntervalMs);
  submitTick();
}

function currentTestFrame(now) {
  if (!testFrames.lastSwitch || now - testFrames.lastSwitch >= TEST_FRAME_MS) {
    if (testFrames.lastSwitch) testFrames.idx = (testFrames.idx + 1) % TEST_CYCLE.length;
    testFrames.lastSwitch = now;
    const name = TEST_CYCLE[testFrames.idx];
    if (testFrames.imgEl && testFrames.urlName !== name) {
      if (testFrames.url) URL.revokeObjectURL(testFrames.url);
      testFrames.url = URL.createObjectURL(testFrames.blobs[name]);
      testFrames.imgEl.src = testFrames.url;
      testFrames.urlName = name;
    }
  }
  return TEST_CYCLE[testFrames.idx];
}

function stop() {
  if (S.phase !== "running" && S.phase !== "starting") return;
  S.phase = "stopping";
  // 先停新帧、拒绝旧结果回写
  if (S.submitTimer) { clearInterval(S.submitTimer); S.submitTimer = null; }
  S.runId = null;
  if (S.inFlight) { S.inFlight.abort(); S.inFlight = null; }
  // 再释放采集
  if (S.stream) { S.stream.getTracks().forEach((t) => t.stop()); S.stream = null; }
  els.video.srcObject = null;
  if (testFrames.imgEl) { testFrames.imgEl.remove(); testFrames.imgEl = null; }
  if (testFrames.url) { URL.revokeObjectURL(testFrames.url); testFrames.url = null; testFrames.urlName = null; }
  clearOverlay();
  // 界面复位（历史时间线保留，供回看）
  S.phase = "idle";
  S.stale = false;
  S.lastPrimary = null; S.lastCount = 0;
  document.body.dataset.mood = "none";
  els.stageIdle.hidden = false;
  setMainButton("start");
  clearBanner();
  updateStageStatus();
  renderCurrent();
  renderComment("已停止。点击“开始体验”重新进入。");
  updateMeta();
}

function onTrackEnded() {
  // 运行中设备断开
  if (S.phase !== "running") return;
  if (S.submitTimer) { clearInterval(S.submitTimer); S.submitTimer = null; }
  S.runId = null;
  if (S.inFlight) { S.inFlight.abort(); S.inFlight = null; }
  if (S.stream) { S.stream.getTracks().forEach((t) => t.stop()); S.stream = null; }
  els.video.srcObject = null;
  clearOverlay();
  S.phase = "idle";
  S.lastPrimary = null; S.lastCount = 0;
  document.body.dataset.mood = "none";
  els.stageIdle.hidden = false;
  setMainButton("start");
  setBanner("danger", "cam",
    "摄像头连接已中断（设备被拔出或掉线）。重新连接摄像头后点击重试。",
    { label: "重试", onClick: () => { void start(); } });
  updateStageStatus();
  renderCurrent();
  updateMeta();
}

/* ---------------- 帧提交 ---------------- */
const capCanvas = document.createElement("canvas");
const capCtx = capCanvas.getContext("2d", { willReadFrequently: false });

function submitTick() {
  if (S.phase !== "running" || document.hidden || S.inFlight) return;
  if (FRAME_TEST) {
    const name = currentTestFrame(performance.now());
    const blob = testFrames.blobs[name];
    if (blob) void sendFrame(blob);
    return;
  }
  const v = els.video;
  if (!v.videoWidth || !v.videoHeight) return;

  capCanvas.width = v.videoWidth;
  capCanvas.height = v.videoHeight;
  capCtx.drawImage(v, 0, 0);   // 预览与取帧共用同一条视频流；推理帧不做镜像
  capCanvas.toBlob((blob) => {
    if (blob) void sendFrame(blob);
  }, "image/jpeg", CFG.jpegQuality);
}

async function sendFrame(blob) {
  const seq = ++S.seq;
  const runId = S.runId;
  const ctl = new AbortController();
  S.inFlight = ctl;
  const sentTs = performance.now();
  const timer = setTimeout(() => ctl.abort(), CFG.fetchTimeoutMs);
  try {
    const r = await fetch("/api/infer", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Run-Id": runId, "X-Seq": String(seq) },
      body: blob,
      signal: ctl.signal,
    });
    const j = await r.json().catch(() => null);
    clearTimeout(timer);
    S.inFlight = null;

    if (S.phase !== "running" || S.runId !== runId) return;      // 跨运行：丢弃
    if (!j) { onFail(); return; }
    if (j.ok) {
      if (j.seq <= S.lastAcceptedSeq) return;                    // 过期：丢弃
      S.lastAcceptedSeq = j.seq;
      S.e2eMs = Math.round(performance.now() - sentTs);
      onResult(j);
    } else if (j.error && j.error.code === "busy") {
      // 服务端正忙：正常丢帧，不计失败
    } else {
      onFail(j.error && j.error.message);
    }
  } catch (err) {
    clearTimeout(timer);
    S.inFlight = null;
    if (S.phase !== "running" || S.runId !== runId) return;
    if (err && err.name === "AbortError") { onFail("推理请求超时"); return; }
    onFail();
  }
}

function onFail(msg) {
  S.failCount += 1;
  if (S.failCount >= CFG.failBannerAfter) {
    setSvc("error", "推理服务异常");
    setBanner("danger", "svc-live",
      `推理服务连续无响应${msg ? `（${msg}）` : ""}。页面会自动重试；若持续异常，请到终端检查 run.sh 输出。`);
  }
  updateMeta();
}

function onResult(j) {
  S.failCount = 0;
  S.lastOkTs = Date.now();
  if (S.bannerId === "svc-live") clearBanner("svc-live");
  if (S.svcState !== "ready") setSvc("ready", "推理服务就绪");

  S.lastCount = j.count;
  S.lastTiming = j.timing || null;
  const now = Date.now();
  S.accepted.push(now);
  while (S.accepted.length && now - S.accepted[0] > 10000) S.accepted.shift();

  const prevLabel = S.lastPrimary && S.lastPrimary.label;
  S.lastPrimary = j.primary || null;

  // 氛围：仅跟随真实有效结果
  if (j.primary) document.body.dataset.mood = j.primary.mood || "neutral";
  else document.body.dataset.mood = "none";

  // 时间线：只记录单人有效识别；标签变化新增节点，相同标签更新持续
  let pushed = false;
  if (j.primary) {
    const tail = S.timeline[S.timeline.length - 1];
    if (tail && tail.label === j.primary.label) {
      tail.t1 = now; tail.score = j.primary.score;
    } else {
      S.timeline.push({
        label: j.primary.label, zh: j.primary.label_zh, mood: j.primary.mood,
        score: j.primary.score, t0: now, t1: now,
      });
      if (S.timeline.length > CFG.timelineKeepMax) S.timeline.shift();
      pushed = true;
    }
    renderComment(changeComment(prevLabel, j.primary));
  } else {
    renderComment(j.count === 0
      ? (S.timeline.length ? "人离开了画面，等待入镜。" : "还没有人入镜。走到镜头前，保持面部可见。")
      : `画面中有 ${j.count} 张脸。请保持单人入镜，其他人暂时退后。`);
  }

  if (S.stale) { S.stale = false; }
  updateStageStatus();
  drawOverlay(j.faces || [], j.primary);
  renderCurrent();
  renderTimeline(pushed);   // 仅新条目带入场动画，其余情况静态刷新
  updateMeta();
}

function changeComment(prevLabel, p) {
  if (!prevLabel) return `镜头里出现了第一张脸：${p.label_zh}。`;
  if (prevLabel !== p.label) {
    const prev = S.timeline[S.timeline.length - 2];
    return `从${prev ? prev.zh : prevLabel}转为${p.label_zh}。`;
  }
  const tail = S.timeline[S.timeline.length - 1];
  const secs = tail ? Math.max(1, Math.round((tail.t1 - tail.t0) / 1000)) : 1;
  if (secs >= 10) return `${p.label_zh}的状态已经持续 ${secs} 秒。`;
  return `当前画面：${p.label_zh}。`;
}

/* ---------------- 结果失效巡检 ---------------- */
setInterval(() => {
  if (S.phase !== "running") return;
  const base = S.lastOkTs || S.runStartTs || 0;
  if (!base) return;
  const gap = Date.now() - base;
  if (gap > CFG.staleAfterMs && !S.stale) {
    S.stale = true;
    renderCurrent();
    renderComment(S.lastOkTs
      ? "结果更新中断，正在尝试恢复。若持续无更新，请检查推理服务。"
      : "启动后一直没有收到推理结果。请检查推理服务状态，或停止后重试。");
  }
}, 1000);

/* ---------------- 渲染 ---------------- */
function updateStageStatus() {
  const el = els.stageStatus;
  if (S.phase !== "running") {
    el.hidden = false;
    el.dataset.s = "wait";
    els.stageStatusIcon.innerHTML = icon("camera-off", 16);
    els.stageStatusText.textContent = "未启动";
    return;
  }
  if (S.lastCount === 0) {
    el.dataset.s = "wait";
    els.stageStatusIcon.innerHTML = icon("user", 16);
    els.stageStatusText.textContent = "等待入镜";
  } else if (S.lastCount === 1) {
    el.dataset.s = "ok";
    els.stageStatusIcon.innerHTML = icon("scan-face", 16);
    els.stageStatusText.textContent = S.stale ? "识别中 · 结果失效" : "识别中";
  } else {
    el.dataset.s = "multi";
    els.stageStatusIcon.innerHTML = icon("users", 16);
    els.stageStatusText.textContent = `多人入镜（${S.lastCount} 张脸）· 请调整位置`;
  }
}

function renderCurrent() {
  const p = S.lastPrimary;
  const on = S.phase === "running" && p && !S.stale;
  els.currentPanel.classList.toggle("current-panel-stale", S.phase === "running" && S.stale && !!p);
  if (S.phase !== "running") {
    els.currentLabel.textContent = "待开始";
    els.currentLabel.dataset.on = "false";
    els.currentEn.textContent = "";
    els.currentScore.textContent = "";
    els.currentTime.textContent = "";
    els.currentNote.textContent = "未启动";
    return;
  }
  if (!p) {
    els.currentLabel.textContent = "未识别";
    els.currentLabel.dataset.on = "false";
    els.currentEn.textContent = "";
    els.currentScore.textContent = "";
    els.currentTime.textContent = "";
    els.currentNote.textContent = S.lastCount === 0 ? "等待入镜" : "多人入镜时不做识别";
    return;
  }
  els.currentLabel.textContent = p.label_zh;
  els.currentLabel.dataset.on = String(!!on);
  els.currentEn.textContent = p.label;
  els.currentScore.textContent = `模型分数 ${p.score}`;
  els.currentTime.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  els.currentNote.textContent = S.stale ? "结果已失效，等待恢复" : "实时";
}

function renderComment(text) {
  if (text) els.comment.textContent = text;
}

function renderTimeline(markNew) {
  const items = S.timeline.slice(-CFG.timelineRenderMax).reverse();
  els.timelineEmpty.hidden = items.length > 0;
  els.timeline.innerHTML = items.map((it, i) => {
    const cur = i === 0 && S.lastPrimary && S.lastPrimary.label === it.label && !S.stale;
    const dur = Math.max(0, Math.round((it.t1 - it.t0) / 1000));
    const t = new Date(it.t0).toLocaleTimeString("zh-CN", { hour12: false });
    const cls = ["tl-row", cur ? "tl-current" : "", markNew && i === 0 ? "tl-new" : ""].join(" ").trim();
    return `<li class="${cls}" style="--c:${MOOD_COLOR[it.mood] || "#5D6B7A"}">
      <span class="tl-time">${t}</span>
      <span class="tl-dot" aria-hidden="true"></span>
      <span class="tl-label">${it.zh}<span class="en">${it.label}</span></span>
      <span class="tl-score">${dur > 0 ? `${dur}s` : ""} · ${it.score}</span>
    </li>`;
  }).join("");
}

function updateMeta() {
  if (S.phase !== "running") {
    els.stageMeta.textContent = S.trackInfo
      ? `${S.trackInfo.w}×${S.trackInfo.h} @${Math.round(S.trackInfo.fps)} · 已停止`
      : "";
    return;
  }
  const rate = (S.accepted.length / 10).toFixed(1);
  const t = S.lastTiming;
  els.stageMeta.textContent =
    `${S.trackInfo.w}×${S.trackInfo.h} @${Math.round(S.trackInfo.fps)}` +
    ` · 推理 ${rate} 次/秒 · 端到端 ${S.e2eMs}ms` +
    (t ? ` · 检测 ${t.detect_ms}ms 分类 ${t.emotion_ms}ms` : "");
}

/* ---------------- 叠加框 ---------------- */
function clearOverlay() {
  const c = els.overlay;
  c.getContext("2d").clearRect(0, 0, c.width, c.height);
}
function drawOverlay(faces, primary) {
  const v = els.video;
  const c = els.overlay;
  const fw = v.videoWidth || (S.trackInfo && S.trackInfo.w) || 1280;
  const fh = v.videoHeight || (S.trackInfo && S.trackInfo.h) || 720;
  if (c.width !== fw || c.height !== fh) {
    c.width = fw; c.height = fh;
  }
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  if (!faces.length) return;
  const multi = faces.length > 1;
  ctx.lineWidth = Math.max(2, c.width / 480);
  for (const f of faces) {
    const w = f.x2 - f.x1, h = f.y2 - f.y1;
    const isPrimary = primary && primary.box &&
      Math.abs(primary.box[0] - f.x1) < 1 && Math.abs(primary.box[1] - f.y1) < 1;
    ctx.strokeStyle = multi ? "#E5534B" : "#E8B34B";
    ctx.strokeRect(f.x1, f.y1, w, h);
    if (!multi && isPrimary && primary) {
      const text = `${primary.label_zh} ${primary.score}`;
      ctx.font = `600 ${Math.max(14, c.width / 46)}px "Noto Sans CJK SC", sans-serif`;
      const tw = ctx.measureText(text).width;
      const pad = Math.max(6, c.width / 160);
      const bh = Math.max(24, c.width / 30);
      const bx = f.x1, by = Math.max(0, f.y1 - bh - 4);
      ctx.fillStyle = "rgba(13,18,24,0.82)";
      ctx.fillRect(bx, by, tw + pad * 2, bh);
      ctx.fillStyle = "#E8B34B";
      ctx.textBaseline = "middle";
      ctx.fillText(text, bx + pad, by + bh / 2 + 1);
    }
  }
}

/* ---------------- 事件 ---------------- */
els.btn.addEventListener("click", () => {
  if (S.phase === "running") stop();
  else if (S.phase === "idle") void start();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (S.submitTimer) { clearInterval(S.submitTimer); S.submitTimer = null; }
  } else if (S.phase === "running" && !S.submitTimer) {
    S.submitTimer = setInterval(submitTick, CFG.submitIntervalMs);
  }
});
window.addEventListener("pagehide", () => { if (S.phase === "running") stop(); });

/* ---------------- 初始 ---------------- */
document.body.dataset.mood = "none";
setMainButton("start");
updateStageStatus();
renderCurrent();
renderTimeline(false);
void refreshSvcBadge();
setInterval(() => { if (S.phase === "idle") void refreshSvcBadge(); }, 30000);
