"use strict";

const STAGE = {
  installing: "개조 중",
  verifying: "점검: 확인 대기",
  confirming: "점검: 컨펌 대기",
  rework: "재개조 필요",
  ready: "출고 대기",
  handed_over: "광주 출고",
  broken: "고장",
};
const HISTORY = { release: "광주 출고", fault: "고장 등록", recheck: "점검 시작" };
const ITEM_STATE = {
  pending: "미설치",
  installed: "설치 완료 · 확인 대기",
  verified: "확인 완료 · 컨펌 대기",
  confirmed: "컨펌 완료",
  rework: "보완 필요",
};
const STEP = { install: "설치", verify: "설치 확인", confirm: "재확인(컨펌)", recheck: "고장 재점검" };
const GROUP = { camera: "카메라 (8)", lidar: "라이다 (8)", thor: "Thor", orin: "Orin" };

const app = document.getElementById("app");
const meInput = document.getElementById("me");
let stageFilter = "";
let search = "";

meInput.value = localStorage.getItem("me") || "";
meInput.addEventListener("input", () => localStorage.setItem("me", meInput.value.trim()));

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function toast(msg, isError) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 3000);
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "요청 실패");
  return data;
}

function requireMe() {
  const me = meInput.value.trim();
  if (!me) {
    meInput.focus();
    throw new Error("상단에 작업자 이름을 먼저 입력하세요.");
  }
  return me;
}

/* ---------- 현황판 ---------- */

async function renderDashboard() {
  const vehicles = await api("/api/vehicles");
  const counts = Object.fromEntries(Object.keys(STAGE).map((k) => [k, 0]));
  vehicles.forEach((v) => counts[v.stage]++);

  const shown = vehicles.filter((v) =>
    (!stageFilter || v.stage === stageFilter) &&
    (!search || v.name.toLowerCase().includes(search.toLowerCase())));

  const card = (key, label, n) =>
    `<button class="card ${stageFilter === key ? "active" : ""}" data-stage="${key}">
       <div class="muted">${label}</div><div class="n">${n}</div></button>`;

  const grp = (v, g) => {
    const x = v.groups[g] || { done: 0, total: 0 };
    return `<td>${x.done}/${x.total}</td>`;
  };

  app.innerHTML = `
    <div class="cards">
      ${card("", "전체", vehicles.length)}
      ${Object.entries(STAGE).map(([k, l]) => card(k, l, counts[k])).join("")}
    </div>
    <div class="toolbar">
      <input id="search" placeholder="차량 검색 (예: 12호차)" value="${esc(search)}">
      <span class="muted">${shown.length}대 표시 · 숫자는 컨펌 완료/필수 항목</span>
    </div>
    <table>
      <thead><tr><th>차량</th><th>상태</th><th>진행</th><th>카메라</th><th>라이다</th>
        <th>Thor</th><th>Orin</th><th>재개조</th><th>광주 출고</th></tr></thead>
      <tbody>
        ${shown.map((v) => `
          <tr class="link" data-id="${v.id}">
            <td><b>${esc(v.name)}</b>${v.cycle > 1 ? ` <span class="muted">${v.cycle}회차</span>` : ""}</td>
            <td><span class="badge s-${v.stage}">${STAGE[v.stage]}</span></td>
            <td>${v.confirmed}/${v.required}</td>
            ${grp(v, "camera")}${grp(v, "lidar")}${grp(v, "thor")}${grp(v, "orin")}
            <td>${v.rework ? `<span class="badge s-rework">${v.rework}건</span>` : "-"}</td>
            <td>${v.handover_at ? `${esc(v.handover_by)}<div class="muted">${esc(v.handover_at)}</div>` : "-"}</td>
          </tr>`).join("") || `<tr><td colspan="9" class="muted">차량이 없습니다.</td></tr>`}
      </tbody>
    </table>
    <details ${vehicles.length ? "" : "open"}>
      <summary>차량 등록</summary>
      <p class="muted">쉼표나 줄바꿈으로 구분합니다. "1-60" 처럼 범위를 입력하면 1호차~60호차가 등록됩니다. 이미 있는 차량은 건너뜁니다.</p>
      <textarea id="names" placeholder="1-60"></textarea>
      <p><button class="primary" id="register">등록</button></p>
    </details>`;

  app.querySelectorAll(".card").forEach((b) => b.addEventListener("click", () => {
    stageFilter = b.dataset.stage;
    renderDashboard();
  }));
  const s = document.getElementById("search");
  s.addEventListener("input", () => {
    search = s.value;
    renderDashboard().then(() => {
      const el = document.getElementById("search");
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    });
  });
  app.querySelectorAll("tr.link").forEach((tr) =>
    tr.addEventListener("click", () => (location.hash = `#/vehicle/${tr.dataset.id}`)));
  document.getElementById("register").addEventListener("click", () => run(async () => {
    const raw = document.getElementById("names").value.trim();
    const range = raw.match(/^(\d+)\s*[-~]\s*(\d+)$/);
    let names = raw;
    if (range) {
      const [a, b] = [Number(range[1]), Number(range[2])].sort((x, y) => x - y);
      names = Array.from({ length: b - a + 1 }, (_, i) => `${a + i}호차`).join(",");
    }
    const r = await api("/api/vehicles", { names });
    toast(`${r.created.length}대 등록` + (r.skipped.length ? `, ${r.skipped.length}대 중복 건너뜀` : ""));
    renderDashboard();
  }));
}

/* ---------- 차량 상세 ---------- */

function itemActions(item) {
  const memo = `<input data-memo="${item.id}" placeholder="메모 (버전·확인 내용, 불합격 시 사유 필수)">`;
  const btn = (step, result, label, cls = "") =>
    `<button class="${cls}" data-item="${item.id}" data-step="${step}" data-result="${result}">${label}</button>`;
  switch (item.state) {
    case "pending":
    case "rework":
      return memo + btn("install", "pass", item.state === "rework" ? "재설치 완료" : "설치 완료", "primary");
    case "installed":
      return memo + btn("verify", "pass", "확인 합격", "primary") + btn("verify", "fail", "확인 불합격", "danger");
    case "verified":
      return memo + btn("confirm", "pass", "컨펌 합격", "primary") + btn("confirm", "fail", "컨펌 불합격", "danger");
    default:
      return "";
  }
}

async function renderVehicle(id) {
  const v = await api(`/api/vehicles/${id}`);
  const locked = v.stage === "handed_over" || v.stage === "broken";
  const picking = v.stage === "broken";
  const groups = {};
  v.items.forEach((i) => (groups[i.grp] ||= []).push(i));

  app.innerHTML = `
    <p><a href="#/">← 전체 현황</a></p>
    <div class="bar">
      <div>
        <b style="font-size:18px">${esc(v.name)}</b>
        <span class="badge s-${v.stage}">${STAGE[v.stage]}</span>
        <span class="muted">${v.cycle}회차 · 필수 ${v.confirmed}/${v.required} 컨펌 완료${v.rework ? ` · 재개조 필요 ${v.rework}건` : ""}</span>
      </div>
      <div class="actions">
        ${v.stage === "handed_over"
          ? `<span>광주 출고: ${esc(v.handover_by)} <span class="muted">${esc(v.handover_at)}</span></span>
             <input id="fault-reason" placeholder="고장 내용 (필수)">
             <button class="danger" id="fault">고장 등록</button>`
          : v.stage === "broken"
          ? `<span><b>고장</b>: ${esc(v.fault_reason)} <span class="muted">${esc(v.fault_by)} · ${esc(v.fault_at)}</span></span>`
          : `<button class="primary" id="handover" ${v.stage === "ready" ? "" : "disabled"}
               title="필수 항목이 모두 컨펌되어야 출고할 수 있습니다">광주 출고 (우리 팀 인수)</button>`}
      </div>
    </div>
    ${picking ? `<div class="bar" style="margin-top:12px">
        <span>다시 점검할 항목을 선택한 뒤 점검을 시작하세요. 선택한 항목만 '확인 대기'로 돌아갑니다.</span>
        <span class="actions"><button id="pick-all">전체 선택</button>
        <button class="primary" id="recheck">점검 시작</button></span></div>` : ""}
    <p class="muted">사이클: 개조(설치) → 개조 점검(설치 확인 → 재확인·컨펌) → 광주 출고 → 고장 시 다시 점검 → 불합격이면 재개조. 같은 항목의 설치자·확인자·컨펌자는 서로 달라야 하고, 불합격은 사유가 필요합니다.</p>
    ${Object.entries(groups).map(([g, items]) => `
      <h2>${GROUP[g] || esc(g)}</h2>
      <table>
        <thead><tr><th style="width:22%">항목</th><th style="width:16%">상태</th>
          <th style="width:18%">설치자 / 확인자</th><th>진행</th></tr></thead>
        <tbody>${items.map((i) => `
          <tr>
            <td>${picking && i.state === "confirmed" ? `<input type="checkbox" class="pick" value="${i.id}"> ` : ""}${esc(i.name)} ${i.required ? "" : `<span class="badge opt">인계 필수 아님</span>`}</td>
            <td><span class="badge s-${i.state}">${ITEM_STATE[i.state]}</span></td>
            <td>${esc(i.installed_by || "-")} / ${esc(i.verified_by || "-")}</td>
            <td>
              ${locked ? "" : `<div class="actions">${itemActions(i)}</div>`}
              ${i.events.length ? `
                <details style="padding:0;margin-top:6px"><summary class="muted">이력 ${i.events.length}건</summary>
                  <ol class="history">${i.events.map((e) => `
                    <li>${esc(e.created_at)} · ${STEP[e.step]} ${e.result === "pass" ? "합격" : "<b>불합격</b>"}
                      · ${esc(e.person)}${e.memo ? ` · ${esc(e.memo)}` : ""}</li>`).join("")}
                  </ol></details>` : ""}
            </td>
          </tr>`).join("")}
        </tbody>
      </table>`).join("")}
    ${v.history.length ? `<h2>차량 이력</h2><ol class="history" style="font-size:14px">${v.history.map((h) =>
      `<li>${esc(h.created_at)} · ${v.cycle > 1 || h.cycle > 1 ? `${h.cycle}회차 · ` : ""}<b>${HISTORY[h.kind] || esc(h.kind)}</b> · ${esc(h.person)}${h.memo ? ` · ${esc(h.memo)}` : ""}</li>`).join("")}</ol>` : ""}`;

  app.querySelectorAll("button[data-step]").forEach((b) => b.addEventListener("click", () => run(async () => {
    const person = requireMe();
    const memo = app.querySelector(`input[data-memo="${b.dataset.item}"]`).value;
    await api(`/api/items/${b.dataset.item}/steps`,
      { step: b.dataset.step, result: b.dataset.result, person, memo });
    toast("기록되었습니다.");
    renderVehicle(id);
  })));
  const h = document.getElementById("handover");
  if (h) h.addEventListener("click", () => run(async () => {
    const person = requireMe();
    if (!confirm(`${v.name}을(를) ${person} 이름으로 광주 출고 처리합니다.`)) return;
    await api(`/api/vehicles/${id}/handover`, { person });
    toast("광주 출고 처리되었습니다.");
    renderVehicle(id);
  }));
  const f = document.getElementById("fault");
  if (f) f.addEventListener("click", () => run(async () => {
    const person = requireMe();
    const reason = document.getElementById("fault-reason").value;
    await api(`/api/vehicles/${id}/fault`, { person, reason });
    toast("고장이 등록되었습니다.");
    renderVehicle(id);
  }));
  const all = document.getElementById("pick-all");
  if (all) all.addEventListener("click", () => {
    const boxes = [...app.querySelectorAll("input.pick")];
    const on = !boxes.every((b) => b.checked);
    boxes.forEach((b) => (b.checked = on));
  });
  const rc = document.getElementById("recheck");
  if (rc) rc.addEventListener("click", () => run(async () => {
    const person = requireMe();
    const item_ids = [...app.querySelectorAll("input.pick:checked")].map((b) => Number(b.value));
    const r = await api(`/api/vehicles/${id}/recheck`, { person, item_ids });
    toast(`${r.cycle}회차 점검을 시작했습니다.`);
    renderVehicle(id);
  }));
}

/* ---------- 라우팅 ---------- */

async function run(fn) {
  try {
    await fn();
  } catch (e) {
    toast(e.message, true);
  }
}

function route() {
  const m = location.hash.match(/^#\/vehicle\/(\d+)$/);
  run(() => (m ? renderVehicle(m[1]) : renderDashboard()));
}

window.addEventListener("hashchange", route);
route();
