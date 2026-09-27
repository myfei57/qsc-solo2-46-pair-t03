const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  return response.json();
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function renderCards(entries) {
  const cards = $("overview-cards");
  cards.innerHTML = "";
  for (const [label, value, tone] of entries) {
    const card = document.createElement("div");
    card.className = "card" + (tone ? " " + tone : "");
    const labelNode = document.createElement("div");
    labelNode.className = "label";
    labelNode.textContent = label;
    const valueNode = document.createElement("div");
    valueNode.className = "value";
    valueNode.textContent = String(value);
    card.appendChild(labelNode);
    card.appendChild(valueNode);
    cards.appendChild(card);
  }
}

function setOnline(online) {
  const node = $("conn-status");
  node.textContent = online ? "已连接" : "连接中断";
  node.classList.toggle("offline", !online);
}

async function refresh() {
  let state;
  try {
    state = await api("/api/state");
  } catch (error) {
    setOnline(false);
    return;
  }
  setOnline(true);
  const data = state.data || {};
  const zone = data.zone || {};
  const slew = data.slew || {};
  const hoist = data.hoist || {};
  const wind = data.wind || {};
  const limit = data.limit || {};
  const cluster = data.cluster || {};
  const pair = cluster.nearest_pair || {};
  renderCards([
    ["塔吊数量", Object.keys(cluster.positions || {}).length],
    ["分区占用", (zone.occupants || []).length, zone.conflict ? "danger" : ""],
    ["冲突闩锁", zone.latch ? "锁定" : "释放", zone.latch ? "danger" : ""],
    ["回转状态", slew.moving ? "运转" : "停止", wind.active ? "warn" : ""],
    ["落钩状态", hoist.hook_lowered ? "已落钩" : "未落钩", hoist.hook_lowered ? "" : "warn"],
    ["制动状态", hoist.brake_engaged ? "抱闸" : "松闸"],
    ["风速", (wind.level || 0).toFixed(1) + " m/s", wind.active ? "danger" : ""],
    ["限位闩锁", limit.latch ? "锁定" : "释放", limit.latch ? "danger" : ""],
    ["最近间距", pair.distance ? pair.distance.toFixed(1) + " 米" : "—", pair.below_safe ? "danger" : ""],
    ["审计记录", data.audit || 0],
  ]);
  $("crane-data").textContent = JSON.stringify(cluster, null, 2);
  $("zone-data").textContent = JSON.stringify(zone, null, 2);
  $("hoist-data").textContent = JSON.stringify(hoist, null, 2);
  $("alarm-data").textContent = JSON.stringify({ wind, limit }, null, 2);
  $("config-data").textContent = JSON.stringify(data.config || {}, null, 2);
  const audit = await api("/api/audit?limit=15");
  $("audit-data").textContent = JSON.stringify(audit.data || [], null, 2);
  $("overview-log").textContent = JSON.stringify((audit.data || []).slice(0, 5), null, 2);
}

document.querySelectorAll("#nav button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("#nav button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    document.querySelectorAll("main section").forEach((section) => section.classList.add("hidden"));
    $("page-" + button.dataset.page).classList.remove("hidden");
  });
});

$("survey-btn").addEventListener("click", async () => {
  await post("/api/cluster/survey", {
    cranes: { "crane-1": { x: 0, y: 0 }, "crane-2": { x: 82, y: 1 } },
  });
  refresh();
});

$("reset-crane-btn").addEventListener("click", async () => {
  await post("/api/crane/reset");
  refresh();
});

$("occupy-a-btn").addEventListener("click", async () => {
  await post("/api/zone/occupy", { crane_id: "crane-1" });
  refresh();
});

$("occupy-b-btn").addEventListener("click", async () => {
  await post("/api/zone/occupy", { crane_id: "crane-2" });
  refresh();
});

$("clear-conflict-btn").addEventListener("click", async () => {
  await post("/api/zone/clear");
  refresh();
});

$("slew-start-btn").addEventListener("click", async () => {
  await post("/api/slew/start");
  refresh();
});

$("lower-btn").addEventListener("click", async () => {
  await post("/api/crane/command", { source: "cab", action: "lower", axis: "hoist" });
  refresh();
});

$("feedback-btn").addEventListener("click", async () => {
  await post("/api/hoist/feedback", { speed: 1.2 });
  refresh();
});

$("release-btn").addEventListener("click", async () => {
  await post("/api/hoist/release");
  refresh();
});

$("trolley-btn").addEventListener("click", async () => {
  await post("/api/crane/command", { source: "cab", action: "trolley", axis: "trolley" });
  refresh();
});

$("hoist-stop-btn").addEventListener("click", async () => {
  await post("/api/hoist/stop");
  refresh();
});

$("wind-alarm-btn").addEventListener("click", async () => {
  await post("/api/wind/alarm", { level: 14 });
  refresh();
});

$("wind-clear-btn").addEventListener("click", async () => {
  await post("/api/wind/clear");
  refresh();
});

$("limit-check-btn").addEventListener("click", async () => {
  await post("/api/limit/check");
  refresh();
});

$("slew-fault-btn").addEventListener("click", async () => {
  await post("/api/slew/fault", { faulted: true });
  refresh();
});

$("slew-recover-btn").addEventListener("click", async () => {
  await post("/api/slew/fault", { faulted: false });
  refresh();
});

$("config-save-btn").addEventListener("click", async () => {
  await post("/api/config", { site_name: "华东三号工地", safe_separation: 15 });
  refresh();
});

// ------------------------------------------------------------- 作业留痕

async function refreshJobs() {
  const result = await api("/api/jobs");
  const data = result.data || { active: null, jobs: [] };
  $("jobs-data").textContent = JSON.stringify(
    {
      current: data.active ? `${data.active.title}（${data.active.job_id.slice(0, 8)}，进行中）` : "无进行中的作业",
      jobs: (data.jobs || []).map((job) => ({
        job_id: job.job_id,
        title: job.title,
        status: job.status,
        started_at: job.started_at,
        ended_at: job.ended_at,
        seq_window: `${job.start_seq}-${job.end_seq || "…"}`,
      })),
    },
    null,
    2,
  );
}

$("job-start-btn").addEventListener("click", async () => {
  const title = $("job-title").value.trim();
  if (!title) {
    $("ledger-report").textContent = "请先填写作业名称。";
    return;
  }
  const result = await post("/api/jobs/start", { title });
  $("ledger-report").textContent = result.ok
    ? `已开工留痕：${result.data.job_id}（从第 ${result.data.start_seq} 条记录开始）`
    : `开工失败：${result.error}`;
  $("job-title").value = "";
  refreshJobs();
});

$("job-end-btn").addEventListener("click", async () => {
  const result = await post("/api/jobs/end");
  $("ledger-report").textContent = result.ok
    ? `已收工留痕：作业窗口 第 ${result.data.start_seq}–${result.data.end_seq} 条`
    : `收工失败：${result.error}`;
  refreshJobs();
});

$("ledger-export-btn").addEventListener("click", async () => {
  const result = await post("/api/ledger/export", {});
  if (!result.ok) {
    $("ledger-report").textContent = `导出失败：${result.error}`;
    return;
  }
  const { materials, verification } = result.data;
  $("ledger-materials").textContent = JSON.stringify(materials, null, 2);
  $("ledger-report").textContent = verification_text(verification);
});

$("ledger-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  let parsed;
  try {
    parsed = JSON.parse(await file.text());
  } catch (error) {
    $("ledger-report").textContent = `材料无法解析：${error}`;
    return;
  }
  const response = await fetch("/api/ledger/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ materials: parsed }),
  });
  const result = await response.json();
  if (!result.ok) {
    $("ledger-report").textContent = `核验请求失败：${result.error}`;
    return;
  }
  $("ledger-materials").textContent = JSON.stringify(parsed, null, 2);
  $("ledger-report").textContent = result.data.text || verification_text(result.data);
  event.target.value = "";
});

function verification_text(report) {
  const lines = [];
  lines.push(`结论：${report.conclusion}`);
  for (const row of report.entry_results || []) {
    lines.push(`[${row.status === "ok" ? "✓" : "✗"}] 第 ${row.seq} 条 ${row.action} — ${row.message}`);
  }
  for (const check of report.checks || []) {
    if (check.message) lines.push(`[${check.status === "ok" ? "✓" : "✗"}] ${check.message}`);
  }
  for (const finding of report.findings || []) {
    if (!lines.includes(finding.message)) lines.push(`✗ [${finding.scope}] ${finding.message}`);
  }
  return lines.join("\n");
}

refreshJobs();
setInterval(refreshJobs, 5000);

setInterval(refresh, 3000);
refresh();
