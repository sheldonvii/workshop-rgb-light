/* 浏览器同步后端灯态；控制权与实体颜色均由 K3 决定。 */
"use strict";

function validEmotion(emotion) {
  return (
    emotion === null ||
    (!!emotion &&
      typeof emotion.label === "string" &&
      typeof emotion.label_zh === "string" &&
      typeof emotion.uncertain === "boolean")
  );
}

function validateLight(light) {
  const pixels = (rows) =>
    Array.isArray(rows) &&
    rows.length > 0 &&
    rows.every(
      (pixel) =>
        Array.isArray(pixel) &&
        pixel.length === 3 &&
        pixel.every(
          (channel) =>
            Number.isFinite(channel) && channel >= 0 && channel <= 255,
        ),
    );
  const group = light?.group,
    transition = light?.transition;
  if (
    !light ||
    typeof light.instance !== "string" ||
    !Number.isFinite(light.sample_ms) ||
    !validEmotion(light.current) ||
    !["stable", "holding", "waiting"].includes(light.status) ||
    !light.device ||
    typeof light.device.status !== "string" ||
    !Number.isFinite(light.power) ||
    !pixels(light.pixels) ||
    !transition ||
    !pixels(transition.from_pixels) ||
    !pixels(transition.to_pixels) ||
    transition.from_pixels.length !== light.pixels.length ||
    transition.to_pixels.length !== light.pixels.length ||
    !pixels([transition.from, transition.to]) ||
    !["from_power", "to_power", "elapsed_ms", "duration_ms"].every((key) =>
      Number.isFinite(transition[key]),
    ) ||
    !group ||
    !Array.isArray(group.people) ||
    !Array.isArray(group.composition) ||
    typeof group.key !== "string" ||
    typeof group.agreed !== "boolean" ||
    !Number.isInteger(group.confirmed_count) ||
    !Number.isInteger(group.visible_count) ||
    !group.people.every(
      (person) =>
        Number.isInteger(person.id) &&
        Array.isArray(person.box) &&
        person.box.length === 4 &&
        person.box.every(Number.isFinite) &&
        typeof person.visible === "boolean" &&
        typeof person.status === "string" &&
        validEmotion(person.current) &&
        validEmotion(person.raw),
    ) ||
    !group.composition.every(
      (color) =>
        typeof color.label === "string" &&
        typeof color.label_zh === "string" &&
        Number.isInteger(color.count) &&
        color.count > 0,
    )
  ) {
    throw new Error("灯光响应数据不完整");
  }
}

class LightController {
  constructor() {
    this.reset();
  }
  reset() {
    this.current = null;
    this.status = "waiting";
    this.snapshot = null;
  }
  apply(light) {
    validateLight(light);
    // 同一进程内只接受较新的快照；重启后的单调时钟不能与旧进程比较。
    if (
      this.snapshot?.instance === light.instance &&
      this.snapshot.sample_ms > light.sample_ms
    )
      return;
    this.snapshot = light;
    this.current = light.current;
    this.status = light.status;
  }
}

let lightSessionQueue = Promise.resolve();
function lightSession(action, runId, keepalive = false) {
  const request = async () => {
    const response = await fetch("/api/light/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, run_id: runId }),
      keepalive,
      signal: AbortSignal.timeout(3000),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      const error = new Error(payload.error?.message || "灯光会话连接失败");
      error.code = payload.error?.code;
      throw error;
    }
    validateLight(payload.light);
    return payload.light;
  };
  // 会话命令顺序执行，避免停止与重新开启在网络中乱序。
  const pending = lightSessionQueue.then(request, request);
  lightSessionQueue = pending.catch(() => null);
  return pending;
}

function startLightSync(callbacks, heartbeatMs) {
  const notify = (callback, value) => {
    // 页面绘制失败只记前端错误，不应改写为 USB 或后端断联；下一轮仍继续。
    try {
      callback(value);
    } catch (error) {
      console.error("灯光页面更新失败", error);
    }
  };
  async function poll() {
    try {
      if (document.hidden) return;
      let light;
      try {
        const response = await fetch("/api/light", {
          cache: "no-store",
          signal: AbortSignal.timeout(2000),
        });
        if (!response.ok) throw new Error("灯光服务不可达");
        const payload = await response.json();
        validateLight(payload.light);
        light = payload.light;
      } catch (error) {
        notify(callbacks.onUnavailable, error);
        return;
      }
      notify(callbacks.onLight, light);
    } finally {
      setTimeout(poll, 100);
    }
  }
  async function heartbeat() {
    try {
      const runId = callbacks.getRunId();
      if (!runId || document.hidden) return;
      let light;
      try {
        light = await lightSession("start", runId);
      } catch (error) {
        if (callbacks.getRunId() === runId && error.code === "light_busy")
          notify(callbacks.onOwnershipLost, error);
        return;
      }
      if (callbacks.getRunId() === runId) notify(callbacks.onLight, light);
    } finally {
      setTimeout(heartbeat, heartbeatMs);
    }
  }
  void poll();
  setTimeout(heartbeat, heartbeatMs);
}
