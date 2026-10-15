/* 群体情绪组成与逐像素物理输出分别展示，避免把过渡中颜色当作识别结果。 */
let selectedPerson = null;
const personSelect = document.createElement("select");
personSelect.id = "personSelect";
personSelect.setAttribute("aria-label", "查看某人的模型分数");
$("rawResult").before(personSelect);
personSelect.addEventListener("change", () => {
  selectedPerson = Number(personSelect.value);
  updateWeather();
});
const peopleList = document.createElement("div");
peopleList.className = "people-list";
peopleList.id = "peopleList";
peopleList.setAttribute("aria-label", "画面中的人");
$("stageMeta").before(peopleList);
const outputPreview = document.createElement("div");
outputPreview.className = "output-preview";
outputPreview.innerHTML =
  '<div class="output-heading"><span>灯带输出</span><span id="outputPower"></span></div><div id="pixelPreview" class="pixel-preview" aria-label="逐灯珠输出预览"></div><p id="groupSummary"></p>';
$("lightConnection").before(outputPreview);
const sceneLayers = [0, 1].map(() => {
  const el = document.createElement("div");
  el.className = "group-scene";
  el.setAttribute("aria-hidden", "true");
  document.body.prepend(el);
  return el;
});
let sceneKey = "",
  sceneIndex = 0,
  peopleKey = "";
function selectedRaw() {
  if (S.phase !== "running" || S.stale) return null;
  const people = LAMP.snapshot ? LAMP.snapshot.group.people : [];
  const person =
    people.find((p) => p.id === selectedPerson) ||
    people.find((p) => p.visible);
  return people.length ? (person?.visible ? person.raw : null) : S.lastPrimary;
}
function selectPerson(id) {
  selectedPerson = id;
  personSelect.value = String(id);
  if (!drawer.open) drawer.showModal();
  document.body.classList.add("drawer-open");
  $("infoToggle").setAttribute("aria-expanded", "true");
  updateWeather();
  personSelect.focus();
}
els.overlay.addEventListener("click", (e) => {
  const rect = els.overlay.getBoundingClientRect();
  let x = ((e.clientX - rect.left) / rect.width) * els.overlay.width;
  const y = ((e.clientY - rect.top) / rect.height) * els.overlay.height;
  if (document.body.dataset.mirror !== "false") x = els.overlay.width - x;
  const person = LAMP.snapshot?.group?.people.find(
    (p) =>
      p.visible &&
      x >= p.box[0] &&
      x <= p.box[2] &&
      y >= p.box[1] &&
      y <= p.box[3],
  );
  if (person) selectPerson(person.id);
});

function updateGroupView() {
  const group = LAMP.snapshot ? LAMP.snapshot.group : null;
  const people = group ? group.people : [],
    composition = group ? group.composition : [];
  const multi = people.length > 1;
  document.body.dataset.group = multi ? "true" : "false";
  const key = JSON.stringify(
    people.map((p) => [p.id, p.current?.label, p.status, p.visible]),
  );
  if (key !== peopleKey) {
    peopleKey = key;
    if (!people.some((p) => p.id === selectedPerson))
      selectedPerson = people.find((p) => p.visible)?.id ?? null;
    personSelect.replaceChildren(
      ...people.map(
        (p) =>
          new Option(`P${p.id}${p.visible ? "" : " · 暂时离开"}`, String(p.id)),
      ),
    );
    if (selectedPerson !== null) personSelect.value = String(selectedPerson);
    peopleList.replaceChildren(
      ...people.map((p) => {
        const btn = document.createElement("button");
        btn.type = "button";
        const label = p.current?.label_zh || "确认中";
        btn.textContent = `P${p.id} · ${label}${!p.visible ? " · 暂离" : p.status === "holding" ? " · 保留" : ""}`;
        btn.style.setProperty(
          "--person-color",
          MOOD_COLOR[p.current?.label] || "#8c8195",
        );
        btn.setAttribute("aria-label", `${btn.textContent}，查看模型分数`);
        btn.addEventListener("click", () => selectPerson(p.id));
        return btn;
      }),
    );
  }
  peopleList.hidden = !people.length;
  personSelect.hidden = !people.length;
  $("captureHint").textContent = "支持多人入镜 · 画面仅在本机处理";
  $("groupSummary").textContent = multi
    ? composition.map((c) => `${c.label_zh} ${c.count} 人`).join(" / ") +
      (people.length > group.confirmed_count
        ? ` · ${people.length - group.confirmed_count} 人待确认`
        : "")
    : "";
  updateGroupBackground(group, multi);
}

function updateGroupBackground(group, multi) {
  const composition = group ? group.composition : [];
  const dark = document.documentElement.dataset.theme !== "light";
  const nextKey = multi && composition.length > 1 ? `${group.key}:${dark}` : "";
  if (nextKey !== sceneKey) {
    sceneKey = nextKey;
    sceneIndex = 1 - sceneIndex;
    sceneLayers.forEach((el) => (el.style.opacity = "0"));
    if (nextKey) {
      let offset = 0;
      const stops = [];
      for (const c of composition) {
        const start = offset;
        offset += (100 * c.count) / group.confirmed_count;
        const color = `color-mix(in srgb, ${MOOD_COLOR[c.label]} ${dark ? 24 : 22}%, ${dark ? "#1e1a26" : "#faf4ef"})`;
        stops.push(
          `${color} ${Math.min(start + 4, offset)}%`,
          `${color} ${Math.max(start, offset - 4)}%`,
        );
      }
      sceneLayers[sceneIndex].style.background =
        `linear-gradient(110deg,${stops.join(",")})`;
      sceneLayers[sceneIndex].style.opacity = "1";
    }
  }
}

function paintPixels(track, t, power) {
  const a = track?.from_pixels || LAMP.snapshot?.pixels || [[234, 183, 207]];
  const b = track ? track.to_pixels : a;
  const colors = a.map(
    (p, i) =>
      `rgb(${p.map((v, j) => Math.round((v + (b[i][j] - v) * t) * power)).join(",")})`,
  );
  const stops = colors.flatMap((c, i) => [
    `${c} ${(100 * i) / colors.length}%`,
    `${c} ${(100 * (i + 1)) / colors.length}%`,
  ]);
  $("pixelPreview").style.background =
    `linear-gradient(90deg,${stops.join(",")})`;
  $("outputPower").textContent =
    `${Math.round(power * 100)}% · ${LAMP.snapshot?.device?.status === "connected" ? "控制器在线" : "待连接"}`;
}

let curveCache = null;
function paintGroupCurve(ctx, w, h, composition) {
  // 沿原有四段贝塞尔曲线按弧长分配颜色，保持弯曲灯线的造型与占比。
  if (!curveCache || curveCache.w !== w || curveCache.h !== h) {
    const points = [[0.1 * w, 0.77 * h, 0]];
    const curves = [
      [-0.01, 0.45, 0.1, 0.15, 0.38, 0.13],
      [0.68, 0.08, 0.99, 0.13, 0.92, 0.47],
      [0.89, 0.71, 0.54, 0.77, 0.34, 0.83],
      [0.14, 0.91, 0.44, 1.02, 0.67, 0.91],
    ];
    for (const c of curves) {
      const start = points[points.length - 1];
      for (let step = 1; step <= 48; step++) {
        const t = step / 48,
          u = 1 - t;
        const x =
          u * u * u * start[0] +
          3 * u * u * t * c[0] * w +
          3 * u * t * t * c[2] * w +
          t * t * t * c[4] * w;
        const y =
          u * u * u * start[1] +
          3 * u * u * t * c[1] * h +
          3 * u * t * t * c[3] * h +
          t * t * t * c[5] * h;
        const prev = points[points.length - 1];
        points.push([x, y, prev[2] + Math.hypot(x - prev[0], y - prev[1])]);
      }
    }
    curveCache = { w, h, points, length: points[points.length - 1][2] };
  }
  const total = composition.reduce((sum, c) => sum + c.count, 0);
  let offset = 0;
  const zones = composition.map((c) => {
    offset += c.count / total;
    return {
      end: offset,
      rgb: [1, 3, 5].map((i) =>
        parseInt(MOOD_COLOR[c.label].slice(i, i + 2), 16),
      ),
    };
  });
  const { points, length } = curveCache;
  function colorAt(position) {
    let color =
      zones.find((z) => position <= z.end)?.rgb || zones[zones.length - 1].rgb;
    for (let j = 0; j < zones.length - 1; j++) {
      const distance = position - zones[j].end;
      if (Math.abs(distance) < 0.035) {
        const t = (distance + 0.035) / 0.07;
        color = zones[j].rgb.map((a, k) =>
          Math.round(a + (zones[j + 1].rgb[k] - a) * t),
        );
        break;
      }
    }
    return `rgb(${color.join(",")})`;
  }
  ctx.lineCap = "round";
  for (let i = 1; i < points.length; i++) {
    const p = points[i],
      q = points[i - 1],
      gradient = ctx.createLinearGradient(q[0], q[1], p[0], p[1]);
    gradient.addColorStop(0, colorAt(q[2] / length));
    gradient.addColorStop(1, colorAt(p[2] / length));
    ctx.strokeStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(q[0], q[1]);
    ctx.lineTo(p[0], p[1]);
    ctx.stroke();
  }
}
