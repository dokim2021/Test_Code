"use strict";

const STAGE = {
  installing: "설치 중",
  verifying: "확인 대기",
  confirming: "컨펌 대기",
  rework: "보완 필요",
  ready: "인수 대기",
  handed_over: "인수 완료",
};
const ITEM_STATE = {
  pending: "미설치",
  installed: "설치 완료 · 확인 대기",
  verified: "확인 완료 · 컨펌 대기",
  confirmed: "컨펌 완료",
  rework: "보완 필요",
};
const STEP = { install: "설치", verify: "설치 확인", confirm: "재확인(컨펌)" };
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
        <th>Thor</th><th>Orin</th><th>보완 필요</th><th>인수</th></tr></thead>
      <tbody>
        ${shown.map((v) => `
          <tr class="link" data-id="${v.id}">
            <td><b>${esc(v.name)}</b></td>
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
  const locked = v.stage === "handed_over";
  const groups = {};
  v.items.forEach((i) => (groups[i.grp] ||= []).push(i));

  app.innerHTML = `
    <p><a href="#/">← 전체 현황</a></p>
    <div class="bar">
      <div>
        <b style="font-size:18px">${esc(v.name)}</b>
        <span class="badge s-${v.stage}">${STAGE[v.stage]}</span>
        <span class="muted">필수 ${v.confirmed}/${v.required} 컨펌 완료${v.rework ? ` · 보완 필요 ${v.rework}건` : ""}</span>
      </div>
      <div>
        ${locked
          ? `인수 완료: ${esc(v.handover_by)} <span class="muted">${esc(v.handover_at)}</span>`
          : `<button class="primary" id="handover" ${v.stage === "ready" ? "" : "disabled"}
               title="필수 항목이 모두 컨펌되어야 인수할 수 있습니다">우리 팀 인수 완료</button>`}
      </div>
    </div>
    <p class="muted">진행 순서: 설치 → 설치 확인 → 재확인(컨펌). 같은 항목에서 설치자·확인자·컨펌자는 서로 다른 사람이어야 합니다. 불합격이면 '보완 필요'로 돌아가 재설치부터 다시 진행합니다.</p>
    ${Object.entries(groups).map(([g, items]) => `
      <h2>${GROUP[g] || esc(g)}</h2>
      <table>
        <thead><tr><th style="width:22%">항목</th><th style="width:16%">상태</th>
          <th style="width:18%">설치자 / 확인자</th><th>진행</th></tr></thead>
        <tbody>${items.map((i) => `
          <tr>
            <td>${esc(i.name)} ${i.required ? "" : `<span class="badge opt">인계 필수 아님</span>`}</td>
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
      </table>`).join("")}`;

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
    if (!confirm(`${v.name}을(를) ${person} 이름으로 인수 완료 처리합니다.`)) return;
    await api(`/api/vehicles/${id}/handover`, { person });
    toast("인수 완료 처리되었습니다.");
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
