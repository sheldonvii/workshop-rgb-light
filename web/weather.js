"use strict";
const WEATHER_ICON = {
  happy: "sun",
  neutral: "moon",
  surprise: "sparkles",
  sad: "cloud-rain",
  angry: "flame",
  fear: "cloud-fog",
  disgust: "leaf",
  contempt: "cloud",
};
const LIGHT_NAME = {
  happy: "日光黄",
  neutral: "薄荷青",
  surprise: "琥珀橙",
  sad: "雨夜蓝",
  angry: "朱红",
  fear: "紫罗兰",
  disgust: "青柠绿",
  contempt: "玫瑰粉",
};
let savedTheme = null;
try {
  savedTheme = localStorage.getItem("mood-studio-theme");
} catch {}
document.documentElement.dataset.theme = savedTheme || "dark";
$("themeToggle").addEventListener("click", () => {
  savedTheme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = savedTheme;
  try {
    localStorage.setItem("mood-studio-theme", savedTheme);
  } catch {}
  updateWeather();
});
const drawer = $("settingsDrawer");
$("infoToggle").addEventListener("click", () => {
  drawer.showModal();
  document.body.classList.add("drawer-open");
  $("infoToggle").setAttribute("aria-expanded", "true");
});
$("closeSettings").addEventListener("click", () => drawer.close());
drawer.addEventListener("close", () => {
  document.body.classList.remove("drawer-open");
  $("infoToggle").setAttribute("aria-expanded", "false");
  $("infoToggle").focus();
});
drawer.addEventListener("click", (e) => {
  const r = drawer.getBoundingClientRect();
  if (
    e.target === drawer &&
    (e.clientX < r.left ||
      e.clientX > r.right ||
      e.clientY < r.top ||
      e.clientY > r.bottom)
  )
    drawer.close();
});

function createDistribution() {
  $("colorLegend").replaceChildren(
    ...activeLabels.map((label) => {
      const chip = document.createElement("span");
      chip.className = "color-chip";
      chip.dataset.label = label;
      chip.style.setProperty("--c", MOOD_COLOR[label]);
      const swatch = document.createElement("i");
      swatch.setAttribute("aria-hidden", "true");
      const name = document.createElement("span");
      name.textContent = LABEL_ZH[label];
      chip.append(swatch, name);
      return chip;
    }),
  );
  $("distribution").replaceChildren(
    ...activeLabels.map((label) => {
      const row = document.createElement("div");
      row.className = "dist-item";
      row.dataset.label = label;
      row.style.setProperty("--c", MOOD_COLOR[label]);
      const name = document.createElement("span");
      name.textContent = LABEL_ZH[label];
      const score = document.createElement("span");
      score.className = "dist-value";
      score.textContent = "待识别";
      const line = document.createElement("span");
      line.className = "dist-line";
      line.setAttribute("aria-hidden", "true");
      line.append(document.createElement("i"));
      row.append(name, score, line);
      return row;
    }),
  );
}
function updateWeather() {
  updateGroupView();
  const raw = selectedRaw();
  const p = LAMP.current;
  const valid = !!p;
  $("clearHistory").disabled = !S.timeline.length;
  $("modelSelect").disabled =
    S.phase !== "idle" || changingModel || !!LAMP.snapshot?.owner;
  renderRawScores(raw);
  const name = valid
    ? WEATHER_ICON[p.label]
    : S.phase === "idle"
      ? "power"
      : "scan-face";
  document.body.dataset.light = valid ? "on" : "standby";
  document.body.dataset.mood = p?.label || "none";
  const standby = presentation?.standby.color || "#eab7cf";
  setLightTarget(
    valid
      ? MOOD_COLOR[p.label] ||
          MOOD_COLOR[LAMP.snapshot.group.composition[0].label]
      : standby,
    valid ? 1 : 0.24,
  );
  $("lightName").textContent = valid
    ? p.label === "mixed"
      ? "各有颜色"
      : LIGHT_NAME[p.label]
    : "待机光";
  $("lightState").textContent = valid
    ? LAMP.snapshot?.group?.people.length > 1
      ? "从画面左侧到右侧 · 分区渐变"
      : LAMP.status === "stable"
        ? "情绪已确认 · 柔和换色"
        : "保留上一灯色 · 继续观察"
    : S.stale
      ? "结果暂未更新 · 已回到待机光"
      : raw?.uncertain
        ? "判断不确定 · 保持待机光"
        : "待机光 · 等待稳定表情";
  renderControllerStatus();
  for (const chip of $("colorLegend").children)
    chip.classList.toggle(
      "active",
      !!valid &&
        (chip.dataset.label === p.label ||
          LAMP.snapshot?.group?.composition.some(
            (c) => c.label === chip.dataset.label,
          )),
    );
  const symbol = $("weatherSymbol");
  if (symbol.dataset.icon !== name) {
    symbol.dataset.icon = name;
    symbol.innerHTML = `<i data-lucide="${name || "users"}"></i>`;
    window.lucide?.createIcons();
  }
  drawWeather();
}

function renderRawScores(raw) {
  $("distributionNote").textContent = raw?.distribution
    ? "原始分数"
    : raw
      ? "此模型只返回首选类别"
      : S.stale
        ? "结果已失效"
        : "等待识别";
  for (const row of $("distribution").children) {
    const v = raw?.distribution?.find((x) => x.label === row.dataset.label);
    row.style.setProperty("--score", v ? v.score : 0);
    row.classList.toggle("is-leading", !!v && raw.label === v.label);
    row.querySelector(".dist-value").textContent = v
      ? `${(v.score * 100).toFixed(1)}%`
      : raw
        ? "未提供"
        : "待识别";
  }
  $("rawResult").textContent = raw
    ? `${raw.uncertain ? "不确定，较接近" : "首选类别"}${raw.label_zh}${raw.score_type === "softmax" ? ` · ${(raw.score * 100).toFixed(1)}%` : ""}`
    : S.stale
      ? "结果已失效"
      : "等待入镜";
}

function renderControllerStatus() {
  const device = LAMP.snapshot?.device;
  $("lightConnection").dataset.status = device?.status || "connecting";
  $("lightConnection").textContent =
    device?.status === "connected"
      ? `控制器已连接 · 配置 ${device.led_count} 颗灯珠`
      : device?.status === "disabled"
        ? "屏幕预览 · 实体灯带未启用"
        : device?.status === "disconnected"
          ? "控制器未连接 · 正在重试"
          : device?.status === "unavailable"
            ? "灯光服务暂不可达"
            : "正在连接控制器…";
  $("lightDeviceDetail").textContent =
    device?.message ||
    (device?.status === "connected"
      ? `WLED ${device.version} · USB 在线；灯带接线和供电需现场确认`
      : "检查 USB 连接和 README 中的串口权限说明。");
}

// 按快照采样时间继续后端过渡；当前轮询会重建动画，性能优化另行评估。
const reducedLight = matchMedia("(prefers-reduced-motion: reduce)");
const lightPaint = { hex: "#eab7cf", power: 0.55, target: "", raf: 0 };
function setLightTarget(hex, power) {
  const state = LAMP.snapshot;
  const track =
    state?.device.status !== "unavailable" ? state?.transition : null;
  const target = track
    ? `${state.instance}:${state.sample_ms}`
    : hex + ":" + power;
  if (lightPaint.target === target) return;
  lightPaint.target = target;
  document.body.style.setProperty("--mood-accent", hex);
  cancelAnimationFrame(lightPaint.raf);
  const from = lightPaint.hex,
    fromPower = lightPaint.power,
    start = performance.now();
  const channels = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const a = track ? track.from : channels(from),
    b = track ? track.to : channels(hex);
  const duration = reducedLight.matches ? 0 : track?.duration_ms || 400;
  const elapsed = track?.elapsed_ms || 0;
  const startPower = track ? track.from_power * 4 : fromPower,
    endPower = track ? track.to_power * 4 : power;
  function frame(now) {
    const t = duration ? Math.min(1, (elapsed + now - start) / duration) : 1,
      e = t * t * (3 - 2 * t);
    lightPaint.hex =
      "#" +
      a
        .map((v, i) =>
          Math.round(v + (b[i] - v) * e)
            .toString(16)
            .padStart(2, "0"),
        )
        .join("");
    lightPaint.power = Math.min(1, startPower + (endPower - startPower) * e);
    paintPixels(
      track,
      e,
      track
        ? track.from_power + (track.to_power - track.from_power) * e
        : LAMP.snapshot?.power || 0.06,
    );
    document.body.style.setProperty("--mood", lightPaint.hex);
    drawWeather();
    if (t < 1) lightPaint.raf = requestAnimationFrame(frame);
    else lightPaint.raf = 0;
  }
  lightPaint.raf = requestAnimationFrame(frame);
}
function drawWeather() {
  const canvas = $("weatherCanvas"),
    box = canvas.getBoundingClientRect(),
    dpr = Math.min(devicePixelRatio, 2);
  if (!box.width) return;
  canvas.width = box.width * dpr;
  canvas.height = box.height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const w = box.width,
    h = box.height;
  const lit = lightPaint.power > 0.01;
  const hue = lightPaint.hex;
  const dark = document.documentElement.dataset.theme !== "light";
  const path = new Path2D();
  path.moveTo(w * 0.1, h * 0.77);
  path.bezierCurveTo(
    w * -0.01,
    h * 0.45,
    w * 0.1,
    h * 0.15,
    w * 0.38,
    h * 0.13,
  );
  path.bezierCurveTo(
    w * 0.68,
    h * 0.08,
    w * 0.99,
    h * 0.13,
    w * 0.92,
    h * 0.47,
  );
  path.bezierCurveTo(
    w * 0.89,
    h * 0.71,
    w * 0.54,
    h * 0.77,
    w * 0.34,
    h * 0.83,
  );
  path.bezierCurveTo(
    w * 0.14,
    h * 0.91,
    w * 0.44,
    h * 1.02,
    w * 0.67,
    h * 0.91,
  );
  const color = ctx.createLinearGradient(w * 0.08, h * 0.8, w * 0.91, h * 0.15);
  const composition = LAMP.snapshot ? LAMP.snapshot.group.composition : [];
  if (composition.length > 1) {
    let offset = 0;
    const total = composition.reduce((sum, c) => sum + c.count, 0);
    for (const c of composition) {
      const start = offset;
      offset += c.count / total;
      color.addColorStop(Math.min(start + 0.025, offset), MOOD_COLOR[c.label]);
      color.addColorStop(Math.max(start, offset - 0.025), MOOD_COLOR[c.label]);
    }
  } else {
    color.addColorStop(0, hue);
    color.addColorStop(0.48, hue);
    color.addColorStop(1, hue);
  }
  const spread = ctx.createRadialGradient(
    w * 0.54,
    h * 0.54,
    0,
    w * 0.54,
    h * 0.54,
    w * 0.43,
  );
  spread.addColorStop(0, hue + (lit ? (dark ? "22" : "10") : "00"));
  spread.addColorStop(1, hue + "00");
  ctx.fillStyle = spread;
  ctx.fillRect(0, 0, w, h);
  ctx.save();
  ctx.translate(w * 0.52, h * 0.93);
  ctx.scale(1, 0.095);
  const floor = ctx.createRadialGradient(0, 0, 0, 0, 0, w * 0.4);
  floor.addColorStop(0, hue + (lit ? "60" : "10"));
  floor.addColorStop(1, hue + "00");
  ctx.fillStyle = floor;
  ctx.fillRect(-w * 0.45, -w * 0.45, w * 0.9, w * 0.9);
  ctx.restore();
  const size = Math.max(7, Math.min(w * 0.024, 15));
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.save();
  ctx.translate(0, 5);
  ctx.strokeStyle = dark ? "#100c1c80" : "#6b547344";
  ctx.lineWidth = size + 5;
  ctx.shadowBlur = 10;
  ctx.shadowColor = "#09071480";
  ctx.stroke(path);
  ctx.restore();
  ctx.strokeStyle = color;
  ctx.lineWidth = size;
  ctx.shadowColor = hue;
  ctx.shadowBlur = lit ? 25 : 0;
  ctx.globalAlpha = 0.32 + 0.68 * lightPaint.power;
  ctx.stroke(path);
  if (lit) {
    ctx.lineWidth = size * 0.72;
    ctx.shadowBlur = 12;
    ctx.stroke(path);
  }
  if (composition.length > 1) {
    ctx.shadowBlur = 0;
    ctx.lineWidth = size;
    paintGroupCurve(ctx, w, h, composition);
  }
  if (lit) {
    ctx.shadowBlur = 0;
    ctx.strokeStyle = dark ? "#fff2ea" : "#fff9f2";
    ctx.lineWidth = size * 0.26;
    ctx.globalAlpha = 0.88 * lightPaint.power;
    ctx.stroke(path);
  }
  ctx.globalAlpha = 1;
  ctx.shadowBlur = 0;
}
document.addEventListener("barometer-update", updateWeather);
new ResizeObserver(drawWeather).observe($("barometer"));
window.lucide?.createIcons();
createDistribution();
updateWeather();

function renderTiming(timing) {
  const ms = (seconds) => Math.round(seconds * 1000);
  $("confirmTime").textContent = `${ms(timing.confirm_seconds)} 毫秒`;
  $("changeTime").textContent = `${ms(timing.change_interval_seconds)} 毫秒`;
  $("transitionTime").textContent = `${timing.transition_ms} 毫秒`;
  $("timingDescription").textContent =
    `每张脸独立确认。连续可靠识别达到 ${ms(timing.confirm_seconds)} 毫秒、至少 ${timing.confirm_samples} 个结果，或最近 ${timing.vote_window_seconds} 秒内同一表情至少 ${timing.vote_samples} 次且占 ${timing.vote_ratio * 100}%，即可确认。已确认情绪切换至少间隔 ${ms(timing.change_interval_seconds)} 毫秒；人数或分区变化不受此间隔限制。短暂不确定保留旧色，${ms(timing.expire_grace_seconds)} 毫秒后仍无可靠结果则退出颜色占比。停止体验后回到待机光。`;
}
