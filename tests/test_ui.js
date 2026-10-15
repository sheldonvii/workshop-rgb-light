/* 共用 CDP 支持；Node.js 22+ 内置 WebSocket，不增加 npm 运行依赖。 */
const fs = require("node:fs");
const assert = require("node:assert/strict");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const ROOT = path.resolve(__dirname, "..");
const BASE = process.env.BAROMETER_URL || "http://127.0.0.1:8321";
const CHROMIUM = process.env.CHROMIUM || "/usr/lib/chromium/chromium";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function output(suite) {
  const folder = path.join(
    process.env.TEST_OUTPUT_DIR || path.join(ROOT, "tests/output/latest"),
    suite,
  );
  fs.mkdirSync(folder, { recursive: true });
  return folder;
}

function serverPid() {
  const port = new URL(BASE).port || "80";
  const matches = [];
  for (const pid of fs.readdirSync("/proc").filter((x) => /^\d+$/.test(x))) {
    try {
      const args = fs
        .readFileSync(`/proc/${pid}/cmdline`, "utf8")
        .split("\0")
        .filter(Boolean);
      const cwd = fs.readlinkSync(`/proc/${pid}/cwd`);
      const entry = args.find((a) => a.endsWith("server/app.py"));
      if (
        entry &&
        path.resolve(cwd, entry) === path.join(ROOT, "server/app.py") &&
        args[args.indexOf("--port") + 1] === port
      )
        matches.push(Number(pid));
    } catch {}
  }
  if (matches.length !== 1)
    throw Error(
      `Expected one project server on port ${port}; found ${matches.length}`,
    );
  return matches[0];
}

async function openBrowser({ camera = false } = {}) {
  if (!globalThis.WebSocket)
    throw Error("Browser tests require Node.js 22 or newer");
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "barometer-browser-"));
  const browser = spawn(
    CHROMIUM,
    [
      "--headless=new",
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "--window-size=1440,900",
      "--force-device-scale-factor=1",
      "--lang=zh-CN",
      ...(camera ? ["--use-fake-ui-for-media-stream"] : []),
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  let spawnError,
    ws,
    sid,
    id = 0;
  const pending = new Map(),
    errors = [];
  browser.on("error", (e) => {
    spawnError = e;
  });
  const terminate = () => {
    if (browser.exitCode === null) browser.kill("SIGTERM");
  };
  process.once("exit", terminate);
  async function close() {
    for (const { timer, reject } of pending.values()) {
      clearTimeout(timer);
      reject(Error("Browser closed"));
    }
    pending.clear();
    // 先让 Chromium 关闭辅助进程并写完配置，再删除临时目录。
    if (
      ws?.readyState === WebSocket.OPEN &&
      browser.exitCode === null &&
      browser.signalCode === null
    ) {
      const exited = new Promise((resolve) => browser.once("exit", resolve));
      ws.send(JSON.stringify({ id: ++id, method: "Browser.close" }));
      await Promise.race([exited, sleep(2000)]);
    }
    ws?.close();
    if (
      browser.exitCode === null &&
      browser.signalCode === null &&
      browser.pid
    ) {
      const exited = new Promise((resolve) => browser.once("exit", resolve));
      browser.kill("SIGTERM");
      await Promise.race([exited, sleep(2000)]);
      if (browser.exitCode === null && browser.signalCode === null) {
        browser.kill("SIGKILL");
        await exited;
      }
    }
    process.removeListener("exit", terminate);
    // 父进程退出后辅助进程仍可能短暂写入配置，删除时有限重试。
    fs.rmSync(profile, {
      recursive: true,
      force: true,
      maxRetries: 10,
      retryDelay: 200,
    });
  }
  try {
    const started = Date.now();
    let version;
    while (Date.now() - started < 20000) {
      if (spawnError) throw spawnError;
      if (browser.exitCode !== null)
        throw Error("Chromium exited before CDP became ready");
      try {
        const port = fs
          .readFileSync(path.join(profile, "DevToolsActivePort"), "utf8")
          .split("\n")[0];
        version = await (
          await fetch(`http://127.0.0.1:${port}/json/version`, {
            signal: AbortSignal.timeout(1500),
          })
        ).json();
        break;
      } catch {
        await sleep(150);
      }
    }
    if (!version) throw Error("Chromium CDP startup timed out");
    ws = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(Error("CDP WebSocket startup timed out")),
        10000,
      );
      ws.addEventListener(
        "open",
        () => {
          clearTimeout(timer);
          resolve();
        },
        { once: true },
      );
      ws.addEventListener(
        "error",
        (e) => {
          clearTimeout(timer);
          reject(e);
        },
        { once: true },
      );
    });
    ws.addEventListener("message", (event) => {
      const m = JSON.parse(event.data),
        p = pending.get(m.id);
      if (p) {
        pending.delete(m.id);
        clearTimeout(p.timer);
        m.error
          ? p.reject(Error(JSON.stringify(m.error)))
          : p.resolve(m.result);
      }
      if (m.method === "Runtime.exceptionThrown") errors.push(m.params);
    });
    const send = (method, params = {}, sessionId = sid) =>
      new Promise((resolve, reject) => {
        const mid = ++id;
        const timer = setTimeout(() => {
          pending.delete(mid);
          reject(Error(`${method} timed out`));
        }, 20000);
        pending.set(mid, { resolve, reject, timer });
        ws.send(
          JSON.stringify({
            id: mid,
            method,
            params,
            ...(sessionId ? { sessionId } : {}),
          }),
        );
      });
    const target = await send(
      "Target.createTarget",
      { url: "about:blank" },
      null,
    );
    sid = (
      await send(
        "Target.attachToTarget",
        { targetId: target.targetId, flatten: true },
        null,
      )
    ).sessionId;
    await send("Page.enable");
    await send("Runtime.enable");
    const ev = async (expression) => {
      const r = await send("Runtime.evaluate", {
        expression,
        returnByValue: true,
        awaitPromise: true,
      });
      if (r.exceptionDetails) throw Error(JSON.stringify(r.exceptionDetails));
      return r.result.value;
    };
    const until = async (expression, limit = 15000) => {
      const start = Date.now();
      while (Date.now() - start < limit) {
        if (await ev(expression)) return true;
        await sleep(150);
      }
      return false;
    };
    return {
      send,
      ev,
      until,
      close,
      errors,
      version: version.Browser,
      navigate: async (url) => {
        await send("Page.navigate", { url });
        if (
          !(await until(
            "document.readyState==='complete' && typeof S!=='undefined'",
          ))
        )
          throw Error("Page did not initialize");
        await sleep(300);
      },
      viewport: async (w, h) => {
        await send("Emulation.setDeviceMetricsOverride", {
          width: w,
          height: h,
          deviceScaleFactor: 1,
          mobile: false,
        });
        await sleep(200);
      },
      screenshot: async (file) => {
        const r = await send("Page.captureScreenshot", {
          format: "png",
          captureBeyondViewport: false,
        });
        fs.writeFileSync(file, Buffer.from(r.data, "base64"));
      },
    };
  } catch (error) {
    await close();
    throw error;
  }
}

function recordCheck(report, name, ok, details) {
  report.checks.push({ name, ok: !!ok, details });
  console.log(ok ? "PASS" : "FAIL", name);
  if (!ok) report.failed = true;
}

async function visual() {
  const OUT = output("ui");
  const report = {
    date: new Date().toISOString(),
    checks: [],
    screenshots: [],
  };
  const check = (name, ok, details) => recordCheck(report, name, ok, details);
  let browser, send, ev, until, viewport, navigate;
  async function shot(name) {
    await browser.screenshot(path.join(OUT, name + ".png"));
    report.screenshots.push(name + ".png");
  }
  async function main() {
    browser = await openBrowser();
    ({ send, ev, until, viewport, navigate } = browser);
    report.browser = browser.version;
    await navigate(BASE);
    await viewport(1440, 900);
    await ev("localStorage.setItem('mood-studio-theme','dark')");
    await navigate(BASE);
    for (const [w, h] of [
      [1440, 900],
      [1280, 720],
      [480, 900],
      [320, 740],
    ]) {
      await viewport(w, h);
      const layout = await ev(
        `(()=>{const b=$('btnMain').getBoundingClientRect(),c=$('stage').getBoundingClientRect(),l=$('barometer').getBoundingClientRect();return {overflow:document.documentElement.scrollWidth>innerWidth,buttonBottom:b.bottom,camera:c.width,lamp:l.width}})()`,
      );
      check(
        "responsive " + w,
        !layout.overflow && layout.buttonBottom < h,
        layout,
      );
      await shot("idle-" + w);
    }
    await viewport(1440, 900);
    await ev("$('infoToggle').click()");
    check(
      "drawer opens and focuses",
      await ev(
        "$('settingsDrawer').open&&$('settingsDrawer').contains(document.activeElement)&&$('infoToggle').getAttribute('aria-expanded')==='true'",
      ),
    );
    await shot("settings-dark");
    await send("Input.dispatchKeyEvent", {
      type: "keyDown",
      key: "Escape",
      code: "Escape",
      windowsVirtualKeyCode: 27,
    });
    await send("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: "Escape",
      code: "Escape",
      windowsVirtualKeyCode: 27,
    });
    check(
      "escape restores focus",
      await ev(
        "!$('settingsDrawer').open&&document.activeElement===$('infoToggle')&&!document.body.classList.contains('drawer-open')",
      ),
    );
    await ev("$('themeToggle').click();$('infoToggle').click()");
    await shot("settings-light");
    await ev("$('closeSettings').click();$('themeToggle').click()");
    await navigate(BASE + "/?frames=test");
    await ev("$('btnMain').click()");
    check(
      "real B0 stable light",
      await until("LAMP.current?.label==='neutral'", 18000),
    );
    check(
      "raw and stable separate",
      await ev(
        "S.lastPrimary.distribution.length===8&&$('currentLabel').textContent==='平静'&&document.body.dataset.mood==='neutral'",
      ),
    );
    await sleep(550);
    await shot("neutral-real-dark");
    const dark = await ev("getComputedStyle(document.body).backgroundColor");
    check("whole background changes", dark === "rgb(23, 62, 57)", dark);
    await ev("$('btnMain').click()");
    check(
      "stop resets output",
      await ev(
        "!LAMP.current&&document.body.dataset.mood==='none'&&S.phase==='idle'",
      ),
    );
    // 显式合成输入用于验证颜色、过渡和可访问性，不评价模型准确率。
    // 只有合成视觉用例才冻结后端轮询，避免真实状态覆盖夹具。
    await ev(
      "window.qaFetch=window.fetch;window.fetch=(url,opts)=>url==='/api/light'?new Promise(()=>{}):qaFetch(url,opts);lightSessionQueue",
    );
    await sleep(350);
    await ev(
      `S.phase='running';S.lastCount=1;S.stale=false;LAMP.snapshot=null;LAMP.current={label:'happy',label_zh:'开心',score:.8,score_type:'softmax'};LAMP.status='stable';renderCurrent();`,
    );
    await sleep(550);
    await shot("happy-ui-fixture-dark");
    check(
      "warm background",
      await ev(
        "getComputedStyle(document.body).backgroundColor==='rgb(73, 59, 29)'",
      ),
    );
    await ev("$('themeToggle').click()");
    await sleep(550);
    await shot("happy-ui-fixture-light");
    await ev(
      "LAMP.current={label:'neutral',label_zh:'平静',score:.6,score_type:'softmax'};renderCurrent()",
    );
    await sleep(550);
    await shot("neutral-ui-fixture-light");
    await ev(
      "S.phase='idle';LAMP.reset();renderCurrent();$('infoToggle').click()",
    );
    await viewport(320, 740);
    await shot("settings-mobile");
    check(
      "mobile drawer fits",
      await ev(
        "$('settingsDrawer').getBoundingClientRect().width<=innerWidth&&document.documentElement.scrollWidth<=innerWidth",
      ),
    );
    await ev("$('closeSettings').click()");
    await send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-reduced-motion", value: "reduce" }],
    });
    check(
      "reduced motion",
      await ev("getComputedStyle(document.body).transitionDuration==='0s'"),
    );

    // 合成情绪覆盖所有配色，不用于模型准确率测试。
    await send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-reduced-motion", value: "reduce" }],
    });
    const accents = await ev(`(()=>{
   const rows=[];
   const rgb=s=>s.startsWith('color(srgb')?s.match(/[\\d.]+/g).map(Number).map(v=>v*255):s.match(/[\\d.]+/g).map(Number);
   const lum=s=>rgb(s).slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
   for(const theme of ['dark','light']){
     document.documentElement.dataset.theme=theme;
     for(const label of Object.keys(MOOD_COLOR)){
       S.phase='running';S.stale=false;LAMP.current={label,label_zh:LABEL_ZH[label],score:.8,score_type:'softmax'};LAMP.status='stable';renderCurrent();
       for(const selector of ['.studio-sticker','#btnMain','.photo-tab']){
         const c=getComputedStyle(document.querySelector(selector)),a=lum(c.backgroundColor),b=lum(c.color);
         rows.push({theme,label,selector,background:c.backgroundColor,contrast:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)});
       }
     }
   }
   LAMP.reset();S.phase='idle';renderCurrent();return rows;
 })()`);
    check(
      "all mood accents readable",
      accents.every((r) => r.contrast >= 4.5),
      { minimumContrast: Math.min(...accents.map((r) => r.contrast)) },
    );
    check(
      "each mood has its own accents",
      new Set(
        accents
          .filter((r) => r.theme === "dark" && r.selector === "#btnMain")
          .map((r) => r.background),
      ).size === 8,
    );
    report.accents = accents;
    await ev("$('closeSettings').click()");
    await send("Input.dispatchKeyEvent", {
      type: "keyDown",
      key: "Tab",
      code: "Tab",
      windowsVirtualKeyCode: 9,
    });
    await send("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: "Tab",
      code: "Tab",
      windowsVirtualKeyCode: 9,
    });
    check(
      "keyboard focus visible",
      await ev(
        "document.activeElement!==document.body && getComputedStyle(document.activeElement).outlineStyle!=='none'",
      ),
    );
    check(
      "live result announced",
      await ev("$('currentLabel').getAttribute('aria-live')==='polite'"),
    );
    check("no javascript errors", !browser.errors.length, browser.errors);
  }
  return main()
    .catch((e) => {
      report.failed = true;
      report.error = e.stack;
      console.error(e);
    })
    .finally(async () => {
      report.javascriptErrors = browser?.errors || [];
      if (report.javascriptErrors.length) report.failed = true;
      await browser?.close();
      fs.writeFileSync(
        path.join(OUT, "report.json"),
        JSON.stringify(report, null, 2),
      );
      process.exitCode = report.failed ? 1 : 0;
    });
}
async function group() {
  /* 独立合成群体展示用例；不以真实模型成功作为前置条件。 */
  const assert = require("node:assert/strict"),
    fs = require("node:fs"),
    path = require("node:path");
  let browser;
  async function main() {
    browser = await openBrowser();
    await browser.navigate(BASE + "/?frames=test");
    // 页面展示只依赖本轮离线夹具；真实 3/6 脸推理由独立 API 阶段检查。
    await browser.ev(
      "window.qaFetch=fetch;window.fetch=(url,opts)=>url==='/api/light'?new Promise(()=>{}):qaFetch(url,opts)",
    );
    await sleep(350);
    const fixture = JSON.parse(
      fs.readFileSync(path.join(output("multi-face"), "mixed-ui.json"), "utf8"),
    );
    const same = structuredClone(fixture.light);
    for (const person of same.group.people)
      person.current = { ...person.current, label: "happy", label_zh: "开心" };
    same.current = {
      label: "happy",
      label_zh: "开心",
      score_type: "group",
      uncertain: false,
    };
    same.group.composition = [{ label: "happy", label_zh: "开心", count: 3 }];
    same.group.agreed = true;
    same.group.key = "happy:3";
    await browser.ev(
      `S.phase='running';S.stale=false;LAMP.reset();LAMP.apply(${JSON.stringify(same)});renderCurrent()`,
    );
    assert.equal(await browser.ev("$('currentEn').textContent"), "3 人一致");

    fixture.light.owner = null;
    await browser.ev(
      `S.phase='running';S.runId=null;S.lastCount=3;S.stale=false;S.lastOkTs=Date.now();S.lastFaces=${JSON.stringify(fixture.faces)};LAMP.reset();LAMP.apply(${JSON.stringify(fixture.light)});setBanner('warn','fixture','界面验证：合成情绪构成，非模型识别结果');renderCurrent();`,
    );
    assert.equal(await browser.ev("$('currentLabel').textContent"), "多种情绪");
    assert(
      await browser.ev(
        "$('groupSummary').textContent.includes('开心 2 人')&&$('groupSummary').textContent.includes('难过 1 人')",
      ),
    );
    for (const theme of ["dark", "light"]) {
      for (const [w, h] of [
        [1440, 900],
        [1280, 720],
        [390, 844],
        [320, 740],
      ]) {
        await browser.viewport(w, h);
        await browser.ev(
          `document.documentElement.dataset.theme=${JSON.stringify(theme)};S.lastOkTs=Date.now();updateWeather()`,
        );
        await sleep(650);
        assert(
          await browser.ev("document.documentElement.scrollWidth<=innerWidth"),
        );
        assert(
          await browser.until(
            "[...document.querySelectorAll('.group-scene')].some(el=>Number(getComputedStyle(el).opacity)>.999)",
            3000,
          ),
          JSON.stringify(
            await browser.ev(
              "({theme:document.documentElement.dataset.theme,mood:document.body.dataset.mood,sceneKey,styles:[...document.querySelectorAll('.group-scene')].map(el=>({style:el.style.cssText,opacity:getComputedStyle(el).opacity}))})",
            ),
          ),
        );
        await browser.screenshot(
          path.join(output("multi-face"), `mixed-${theme}-${w}.png`),
        );
      }
    }
    await browser.ev("$('peopleList').children[0].click()");
    assert(
      await browser.ev(
        "$('settingsDrawer').open&&$('personSelect').value===String(selectedPerson)",
      ),
    );
    await browser.ev(
      "$('personSelect').value=$('personSelect').options[2].value;$('personSelect').dispatchEvent(new Event('change'))",
    );
    assert(await browser.ev("$('rawResult').textContent.includes('开心')"));
    await browser.screenshot(
      path.join(output("multi-face"), "person-settings.png"),
    );
    await browser.ev(
      "$('closeSettings').click();S.phase='idle';S.runId=null;LAMP.reset();renderCurrent()",
    );
    assert.equal(browser.errors.length, 0, JSON.stringify(browser.errors));
    console.log(
      "PASS synthetic group light/background, 8 viewports/themes and per-person scores; model inference is a separate stage",
    );
  }
  return main()
    .catch((e) => {
      console.error(e);
      process.exitCode = 1;
    })
    .finally(async () => {
      await browser?.close();
    });
}
async function shared() {
  /* 两个真实浏览器通过已标注测试图片验证共享灯态和控制权。 */
  let owner, observer;
  async function main() {
    owner = await openBrowser();
    observer = await openBrowser();
    await owner.navigate(BASE + "/?frames=test");
    await owner.ev("$('btnMain').click()");
    assert(await owner.until("S.phase==='running'"));
    await owner.ev(
      "testFrames.idx=1;testFrames.lastSwitch=performance.now()+60000",
    );
    assert(await owner.until("LAMP.current?.label==='neutral'"));
    const id = await owner.ev("S.runId");
    await observer.navigate(BASE + "/?frames=test");
    assert(
      await observer.until("S.phase==='idle'&&LAMP.current?.label==='neutral'"),
    );
    assert.equal(await observer.ev("LAMP.snapshot.owner"), id);
    await observer.ev("$('btnMain').click()");
    assert(
      await observer.until("S.phase==='idle'&&S.bannerId==='light-owner'"),
    );
    assert.equal(await owner.ev("LAMP.snapshot.owner"), id);
    await observer.navigate(BASE);
    assert(await observer.until("LAMP.current?.label==='neutral'"));
    await owner.screenshot(path.join(output("lighting"), "owner-neutral.png"));
    await observer.screenshot(
      path.join(output("lighting"), "observer-neutral.png"),
    );
    await owner.ev(
      "testFrames.idx=4;testFrames.lastSwitch=performance.now()+60000",
    );
    assert(
      await owner.until(
        "S.lastCount===3&&LAMP.snapshot.group.people.length===3",
      ),
    );
    assert(await observer.until("LAMP.snapshot.group.people.length===3"));
    assert(
      await owner.ev(
        "S.lastFaces.every(f=>f.emotion?.distribution.length===8)",
      ),
    );
    await sleep(500);

    await owner.screenshot(path.join(output("lighting"), "multi-group.png"));
    await owner.ev("$('btnMain').click();lightSessionQueue");
    assert(await observer.until("LAMP.snapshot.owner===null"));
    assert.equal(owner.errors.length + observer.errors.length, 0);
    console.log(
      "PASS shared backend color, observer reload, ownership conflict, multi-person light, stop release",
    );
  }
  return main()
    .catch((e) => {
      console.error(e);
      process.exitCode = 1;
    })
    .finally(async () => {
      await owner?.close();
      await observer?.close();
    });
}

async function browserContract(caseName) {
  const OUT = output("contract");
  const report = {
    date: new Date().toISOString(),
    checks: [],
    screenshots: [],
  };
  const check = (name, ok, details) => recordCheck(report, name, ok, details);
  let browser, send, ev, until, viewport, navigate;
  async function shot(name) {
    await browser.screenshot(path.join(OUT, name + ".png"));
    report.screenshots.push(name + ".png");
  }
  const LIFECYCLE_ONLY = caseName === "lifecycle";
  async function switchModel(key) {
    await ev(
      `if(!document.getElementById('settingsDrawer').open)document.getElementById('infoToggle').click();document.getElementById('modelSelect').value=${JSON.stringify(key)};document.getElementById('modelSelect').dispatchEvent(new Event('change'))`,
    );
    const ready = await until(
      `!changingModel && currentModel===${JSON.stringify(key)}`,
      20000,
    );
    await ev("document.getElementById('settingsDrawer').close()");
    check(
      "switch " + key,
      ready,
      await ev("document.getElementById('modelStatus').textContent"),
    );
  }
  async function main() {
    browser = await openBrowser();
    ({ send, ev, until, viewport, navigate } = browser);
    report.browser = browser.version;
    await navigate(BASE);
    if (LIFECYCLE_ONLY) {
      report.scope =
        "Isolated synthetic media and delayed callbacks. Not camera hardware validation.";
      await ev(`window.qaMedia=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices); window.qaStopped=0;
     navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{window.qaResolve=()=>resolve({getTracks:()=>[{stop:()=>window.qaStopped++}]});});
     document.getElementById('btnMain').click();`);
      check(
        "permission request begins",
        await until("typeof window.qaResolve==='function'"),
      );
      check(
        "starting can cancel",
        await ev(
          "!document.getElementById('btnMain').disabled && document.getElementById('btnMainLabel').textContent==='取消开启'",
        ),
      );
      await ev("document.getElementById('btnMain').click();window.qaResolve()");
      await sleep(200);
      check(
        "late media stream released",
        await ev("S.phase==='idle' && !S.stream && window.qaStopped===1"),
      );
      await ev(`navigator.mediaDevices.getUserMedia=qaMedia;
     window.qaSource=document.createElement('canvas');qaSource.width=1280;qaSource.height=720;
     const q=qaSource.getContext('2d');q.fillStyle='#abc';q.fillRect(0,0,1280,720);
     window.qaStream=qaSource.captureStream(30);navigator.mediaDevices.getUserMedia=async()=>qaStream;
     window.qaToBlob=capCanvas.toBlob.bind(capCanvas);capCanvas.toBlob=callback=>{window.qaEncoded=callback;};
     window.qaFetch=window.fetch;window.qaInferRequests=0;window.fetch=(url,opts)=>{if(url==='/api/infer')qaInferRequests++;return qaFetch(url,opts)};
     document.getElementById('btnMain').click();`);
      check(
        "frame encoding pending",
        await until("typeof window.qaEncoded==='function'"),
      );
      await ev(
        "document.getElementById('btnMain').click();qaEncoded(new Blob(['late frame']))",
      );
      await sleep(250);
      check(
        "late encoded frame discarded",
        await ev(
          "S.phase==='idle' && !S.inFlight && qaInferRequests===0 && qaStream.getTracks().every(t=>t.readyState==='ended')",
        ),
      );
      await ev(
        "capCanvas.toBlob=qaToBlob;window.fetch=qaFetch;navigator.mediaDevices.getUserMedia=qaMedia",
      );
      await navigate(BASE + "/?frames=test");
      await ev("document.getElementById('btnMain').click()");
      check(
        "real pipeline before failure injection",
        await until("LAMP.current && !S.stale"),
      );
      await ev(`window.qaRender=renderCurrent;window.qaPaintFailures=0;window.qaPolls=0;
        window.qaFetch=window.fetch;window.fetch=(url,opts)=>{if(url==='/api/light')qaPolls++;return qaFetch(url,opts)};
        renderCurrent=()=>{qaPaintFailures++;throw new Error('QA 页面绘制失败');};`);
      await sleep(650);
      check(
        "render failures keep polling and controller state",
        await ev(
          "qaPolls>=2 && qaPaintFailures>=2 && LAMP.snapshot.device.status!=='unavailable' && S.failCount===0",
        ),
      );
      await ev("renderCurrent=qaRender;window.fetch=qaFetch;renderCurrent()");
      check(
        "session conflict preserves stable code",
        await ev(`(async()=>{
        window.qaFetch=fetch;window.fetch=async()=>new Response(JSON.stringify({ok:false,error:{code:'light_busy',message:'当前控制权冲突'}}),{status:409});
        try{await lightSession('start','observer');return false;}
        catch(error){return error.code==='light_busy';}finally{window.fetch=qaFetch;}
      })()`),
      );
      await ev(
        `window.qaFetch=window.fetch;window.fetch=(url,opts)=>url==='/api/infer'?Promise.reject(new TypeError('QA simulated network outage')):qaFetch(url,opts);`,
      );
      check(
        "service failure invalidates",
        await until(
          "S.stale && document.body.dataset.mood==='none' && document.getElementById('currentNote').textContent.includes('失效')",
          12000,
        ),
      );
      const before = await ev("S.timeline.length");
      await ev("window.fetch=qaFetch");
      check(
        "service recovers real inference",
        await until(
          "!S.stale && LAMP.current && !S.stale && S.bannerId!=='svc-live'",
          16000,
        ),
      );
      check(
        "outage separates timeline",
        (await ev("S.timeline.length")) > before,
      );
      await ev("document.getElementById('btnMain').click()");
      check(
        "final resources released",
        await ev(
          "S.phase==='idle' && !S.inFlight && !S.stream && !S.submitTimer",
        ),
      );
      check("no javascript errors", !browser.errors.length, browser.errors);
      return;
    }
    await switchModel("hse-b2");
    await switchModel("official");
    await switchModel("hse-b0");
    await navigate(BASE + "/?frames=test");
    await viewport(1440, 900);
    await ev("document.getElementById('btnMain').click()");
    check("test source starts", await until("S.phase==='running'"));
    check(
      "eight class result",
      await until(
        "S.lastPrimary?.distribution?.length===8 && !S.lastPrimary.uncertain",
      ),
    );
    const result = await ev(
      "({p:S.lastPrimary,history:S.timeline.length,rows:document.getElementById('distribution').children.length})",
    );
    check(
      "probabilities normalize",
      Math.abs(result.p.distribution.reduce((s, x) => s + x.score, 0) - 1) <
        0.00001,
      result,
    );
    await shot("recognizing-1440x900");
    await ev("testFrames.idx=4;testFrames.lastSwitch=performance.now()+60000");
    check(
      "multiple faces detected",
      await until(
        "S.lastCount===3 && $('stageStatusText').textContent.includes('3 人入镜')",
      ),
    );
    check(
      "multiple faces tracked independently",
      await until(
        "S.lastCount===3 && LAMP.snapshot.group.people.length===3",
        5000,
      ),
    );
    check(
      "multiple faces have per-person classification",
      await ev("S.lastPrimary===null && S.lastFaces.every(f=>f.emotion)"),
    );
    await sleep(1500);
    const history = await ev("S.timeline.length");
    await sleep(900);
    check(
      "unchanged group does not flood history",
      (await ev("S.timeline.length")) === history,
    );
    await ev("testFrames.idx=0;testFrames.lastSwitch=performance.now()+60000");
    check("no face state", await until("S.lastCount===0 && !LAMP.current"));
    await ev("testFrames.idx=1;testFrames.lastSwitch=performance.now()+60000");
    check(
      "single face recovers",
      await until("S.lastCount===1 && LAMP.current?.label==='neutral'"),
    );
    await ev("document.getElementById('mirrorToggle').click()");
    check("mirror toggle", await ev("document.body.dataset.mirror==='false'"));
    await ev("document.getElementById('btnMain').click()");
    check(
      "stop releases",
      await ev(
        "S.phase==='idle' && !S.stream && !S.inFlight && !S.submitTimer && document.body.dataset.mood==='none'",
      ),
    );
    await switchModel("hse-b2");
    await ev("document.getElementById('btnMain').click()");
    check("uncertain result", await until("S.lastPrimary?.uncertain===true"));
    check(
      "uncertainty no false history",
      await ev(
        "S.timeline.length===0 && document.body.dataset.mood==='none' && document.getElementById('currentLabel').textContent==='观察中'",
      ),
    );
    await shot("uncertain-1440x900");
    await ev("document.getElementById('btnMain').click()");
    await switchModel("hse-b0");
    await navigate(BASE);
    await send(
      "Browser.setPermission",
      { permission: { name: "camera" }, setting: "denied", origin: BASE },
      null,
    );
    await ev("document.getElementById('btnMain').click()");
    check(
      "camera denied recovers",
      await until(
        "S.phase==='idle' && !document.getElementById('banner').hidden",
      ),
    );
    await shot("camera-permission-denied");
    check("no javascript errors", !browser.errors.length, browser.errors);
  }
  return main()
    .catch((e) => {
      report.failed = true;
      report.error = e.stack;
      console.error(e);
    })
    .finally(async () => {
      report.javascriptErrors = browser?.errors || [];
      if (report.javascriptErrors.length) report.failed = true;
      await browser?.close();
      fs.writeFileSync(
        path.join(
          OUT,
          LIFECYCLE_ONLY ? "lifecycle-report.json" : "report.json",
        ),
        JSON.stringify(report, null, 2),
      );
      process.exitCode = report.failed ? 1 : 0;
    });
}

async function camera() {
  const OUT = output("camera"),
    report = { date: new Date().toISOString(), checks: [] };
  let browser;
  const check = (name, ok, details) => recordCheck(report, name, ok, details);
  async function main() {
    browser = await openBrowser({ camera: true });
    report.browser = browser.version;
    const { ev, until, navigate } = browser;
    await navigate(BASE);
    check(
      "initial state",
      await ev("S.phase==='idle' && !$('video').srcObject"),
    );
    await ev("$('btnMain').click()");
    const started = await until(
      "S.phase==='running' && S.lastAcceptedSeq>0",
      20000,
    );
    const cameraState = await ev(
      "({phase:S.phase,banner:$('bannerText').textContent})",
    );
    check("camera starts", started, cameraState);
    if (!started) throw Error(cameraState.banner || "Camera did not start");
    const before = await ev(
      `(()=>{const v=$('video'),t=S.stream?.getVideoTracks()[0],s=t?.getSettings();return {width:s?.width,height:s?.height,fps:s?.frameRate,device:t?.label,frames:v.getVideoPlaybackQuality().totalVideoFrames,seq:S.lastAcceptedSeq}})()`,
    );
    check(
      "camera at least 720p",
      before.width >= 1280 && before.height >= 720,
      before,
    );
    await sleep(4000);
    check(
      "video and inference progress",
      await ev(
        `$('video').getVideoPlaybackQuality().totalVideoFrames>${before.frames} && S.lastAcceptedSeq>${before.seq} && !S.stale`,
      ),
    );
    await browser.screenshot(path.join(OUT, "running.png"));
    await ev("window.qaTracks=S.stream.getTracks();$('btnMain').click()");
    check(
      "stop releases camera",
      await until(
        "S.phase==='idle' && !$('video').srcObject && !S.inFlight && !S.submitTimer && qaTracks.every(t=>t.readyState==='ended')",
      ),
    );
    check("no javascript errors", !browser.errors.length, browser.errors);
  }
  return main()
    .catch((e) => {
      report.failed = true;
      report.error = e.stack;
      console.error(e);
    })
    .finally(async () => {
      await browser?.close();
      fs.writeFileSync(
        path.join(OUT, "report.json"),
        JSON.stringify(report, null, 2),
      );
      process.exitCode = report.failed ? 1 : 0;
    });
}

async function soak() {
  /* 摄像头持续采集并叠加真实单脸推理压力，默认 30 分钟。 */
  const fixtureMode = process.argv.includes("--frames");
  const OUT = output("soak"),
    duration = Number(process.env.SOAK_SECONDS || 1800) * 1000;
  if (!Number.isFinite(duration) || duration < 10000)
    throw Error("SOAK_SECONDS must be at least 10");
  const report = {
    date: new Date().toISOString(),
    source: fixtureMode
      ? "test images with real inference; no camera validation"
      : "real USB camera",
    requestedSeconds: duration / 1000,
    samples: [],
    classification: { count: 0, busy: 0, errors: [], first: null, last: null },
  };
  let browser,
    pressureTask,
    pressureStop = false;
  async function pressure() {
    const image = fs.readFileSync(
      path.join(ROOT, "web/test-frames/single.jpg"),
    );
    let seq = 0;
    while (!pressureStop) {
      const start = Date.now();
      try {
        const r = await fetch(BASE + "/api/infer", {
          method: "POST",
          body: image,
          headers: { "X-Run-Id": "soak", "X-Seq": String(++seq) },
          signal: AbortSignal.timeout(8000),
        });
        if (r.status === 409) report.classification.busy++;
        else {
          if (!r.ok) throw Error(`Inference HTTP ${r.status}`);
          const j = await r.json(),
            p = j.primary;
          if (
            j.count !== 1 ||
            p?.model !== "hse-b0" ||
            p.distribution?.length !== 8 ||
            Math.abs(p.distribution.reduce((s, v) => s + v.score, 0) - 1) > 1e-5
          )
            throw Error("Invalid B0 single-face result");
          const row = { label: p.label, score: p.score, ms: j.timing.total_ms };
          report.classification.count++;
          report.classification.first ??= row;
          report.classification.last = row;
        }
      } catch (e) {
        report.classification.errors.push(String(e));
      }
      if (!pressureStop) await sleep(Math.max(10, 1000 - (Date.now() - start)));
    }
  }
  async function main() {
    const pid = serverPid(),
      health = await (await fetch(BASE + "/api/health")).json();
    if (health.emotion_key !== "hse-b0" || health.state !== "ready")
      throw Error("Start the service with B0 before running the soak");
    browser = await openBrowser({ camera: !fixtureMode });
    report.browser = browser.version;
    const { ev, until, navigate } = browser;
    await navigate(BASE + (fixtureMode ? "/?frames=test" : ""));
    await ev("$('btnMain').click()");
    if (!(await until("S.phase==='running' && S.lastAcceptedSeq>0", 20000)))
      throw Error(
        (await ev("$('bannerText').textContent")) || "Input did not start",
      );
    pressureTask = pressure();
    const start = Date.now(),
      interval = Math.min(30000, duration / 4);
    while (Date.now() - start < duration) {
      const s = await ev(
        `(()=>{const v=$('video');return {t:Date.now(),phase:S.phase,stale:S.stale,failCount:S.failCount,frames:FRAME_TEST?null:v.getVideoPlaybackQuality().totalVideoFrames,seq:S.lastAcceptedSeq,track:FRAME_TEST?{width:S.trackInfo.w,height:S.trackInfo.h}:S.stream?.getVideoTracks()[0]?.getSettings(),submittedSeq:S.seq,encoding:S.encoding,inFlight:!!S.inFlight,lastOkTs:S.lastOkTs,e2eMs:S.e2eMs,timing:S.lastTiming,heapBytes:performance.memory?.usedJSHeapSize}})()`,
      );
      s.serverRssKiB = Number(
        fs
          .readFileSync(`/proc/${pid}/status`, "utf8")
          .match(/VmRSS:\s+(\d+)/)[1],
      );
      report.samples.push(s);
      console.log(
        "SAMPLE",
        report.samples.length,
        "frames",
        s.frames,
        "seq",
        s.seq,
        "RSS KiB",
        s.serverRssKiB,
      );
      await sleep(
        Math.min(interval, Math.max(0, duration - (Date.now() - start))),
      );
    }
    report.elapsedSeconds = (Date.now() - start) / 1000;
    pressureStop = true;
    await pressureTask;
    await ev("window.qaTracks=S.stream?.getTracks()||[];$('btnMain').click()");
    report.stopped = await until(
      "S.phase==='idle' && !$('video').srcObject && !S.inFlight && !S.submitTimer && qaTracks.every(t=>t.readyState==='ended')",
    );
    // 短程采样可能短于单次请求；允许相同读数，但整段必须推进且每次均无 stale。
    report.passed =
      report.samples.length >= 3 &&
      report.samples.every(
        (s, i) =>
          s.phase === "running" &&
          !s.stale &&
          s.failCount === 0 &&
          s.track?.width >= 1280 &&
          s.track?.height >= 720 &&
          (i === 0 ||
            ((fixtureMode || s.frames > report.samples[i - 1].frames) &&
              s.seq >= report.samples[i - 1].seq)),
      ) &&
      report.samples.at(-1).seq > report.samples[0].seq &&
      report.stopped &&
      report.classification.count >=
        Math.max(3, Math.floor((duration / 1000) * 0.25)) &&
      !report.classification.errors.length &&
      !browser.errors.length;
    report.javascriptErrors = browser.errors;
  }
  return main()
    .catch((e) => {
      report.passed = false;
      report.error = e.stack;
      console.error(e);
    })
    .finally(async () => {
      pressureStop = true;
      await pressureTask;
      await browser?.close();
      fs.writeFileSync(
        path.join(OUT, "report.json"),
        JSON.stringify(report, null, 2),
      );
      console.log(
        report.passed ? "PASS" : "FAIL",
        report.source,
        "and classifier pressure",
        report.requestedSeconds,
        "seconds",
      );
      process.exitCode = report.passed ? 0 : 1;
    });
}

const cases = {
  visual,
  group,
  shared,
  contracts: () => browserContract("contracts"),
  lifecycle: () => browserContract("lifecycle"),
  camera,
  soak,
};
async function run() {
  const index = process.argv.indexOf("--case");
  const selected =
    index < 0
      ? ["visual", "group", "shared", "contracts", "lifecycle", "camera"]
      : [process.argv[index + 1]];
  for (const name of selected) {
    if (!cases[name]) throw new Error("未知页面用例: " + name);
    console.log("CASE", name);
    await cases[name]();
    if (process.exitCode) throw new Error("页面用例失败: " + name);
  }
}
run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
