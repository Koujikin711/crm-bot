const state = {
  directions: [],
  specialists: [],
  queue: [],
  appointments: [],
  dayStartHour: 8,
  dayEndHour: 20,
  stepMin: 30,
  dragApptId: null,
};

async function j(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function formatDate(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function setMsg(text, ok = true) {
  const el = document.getElementById("form-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = `hint ${ok ? "ok" : "err"}`;
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function specialistSubtitle(sp) {
  return (sp.specialty_label && String(sp.specialty_label).trim()) || sp.direction_name || "—";
}

let currentAppointmentId = null;
let activeSpecMenuId = null;

function drawerBackdrop() {
  return document.getElementById("drawer-backdrop");
}

function closeDrawers() {
  document.getElementById("client-drawer")?.classList.add("is-hidden");
  document.getElementById("spec-drawer")?.classList.add("is-hidden");
  drawerBackdrop()?.classList.add("is-hidden");
  currentAppointmentId = null;
}

function closeSpecMenu() {
  const menu = document.getElementById("sched-spec-menu");
  if (!menu) return;
  menu.classList.add("is-hidden");
  menu.setAttribute("aria-hidden", "true");
  activeSpecMenuId = null;
}

function openSpecMenuAt(specialistId, anchorEl) {
  const menu = document.getElementById("sched-spec-menu");
  if (!menu || !anchorEl) return;
  const rect = anchorEl.getBoundingClientRect();
  menu.style.left = `${Math.round(rect.right - 156)}px`;
  menu.style.top = `${Math.round(rect.bottom + 6)}px`;
  menu.classList.remove("is-hidden");
  menu.setAttribute("aria-hidden", "false");
  activeSpecMenuId = Number(specialistId);
}

function openDrawersCommon() {
  drawerBackdrop()?.classList.remove("is-hidden");
}

async function openClientDrawer(apptId) {
  currentAppointmentId = apptId;
  openDrawersCommon();
  const panel = document.getElementById("client-drawer");
  panel.classList.remove("is-hidden");
  document.getElementById("spec-drawer")?.classList.add("is-hidden");
  let a;
  try {
    a = await j(`/web-api/booking/appointments/${apptId}`);
  } catch (e) {
    setMsg(e.message, false);
    closeDrawers();
    return;
  }
  document.getElementById("cd-title").textContent = a.patient_name || "Запись";
  const phone = a.patient_phone || "—";
  document.getElementById("cd-sub").textContent = phone;
  const t0 = normalizeSlotDt(a.start_at);
  const t1 = normalizeSlotDt(a.end_at);
  const rows = [
    ["Время", `${t0} — ${t1}`],
    ["Врач", a.specialist_name || "—"],
    ["Направление", a.direction_name || "—"],
    ["Статус", a.status || "—"],
    ["Менеджер (ID)", a.responsible_manager_id != null ? String(a.responsible_manager_id) : "—"],
    ["Лид (тел.)", a.lead_phone || "—"],
    ["Комментарий", (a.comment && String(a.comment).trim()) || "—"],
  ];
  document.getElementById("cd-dl").innerHTML = rows
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
    .join("");
  document.getElementById("cd-status").value = a.status || "booked";
}

function openSpecDrawer(spec) {
  openDrawersCommon();
  closeSpecMenu();
  document.getElementById("client-drawer")?.classList.add("is-hidden");
  const panel = document.getElementById("spec-drawer");
  panel.classList.remove("is-hidden");
  const sel = document.getElementById("sd-direction_id");
  sel.innerHTML = state.directions
    .map((d) => `<option value="${d.id}">${esc(d.name)} (${d.duration_min} мин)</option>`)
    .join("");
  const form = document.getElementById("form-add-spec");
  const editId = document.getElementById("sd-edit-id");
  const title = document.getElementById("sd-title");
  const submit = document.getElementById("sd-submit-btn");
  form.reset();
  document.getElementById("sd-msg").textContent = "";
  ["1", "2", "3", "4", "5"].forEach((v) => {
    const el = document.querySelector(`#form-add-spec input[name="wd"][value="${v}"]`);
    if (el) el.checked = true;
  });
  ["6", "7"].forEach((v) => {
    const el = document.querySelector(`#form-add-spec input[name="wd"][value="${v}"]`);
    if (el) el.checked = false;
  });
  const wf = document.querySelector('#form-add-spec input[name="work_time_from"]');
  const wt = document.querySelector('#form-add-spec input[name="work_time_to"]');
  if (wf) wf.value = "09:00";
  if (wt) wt.value = "18:00";
  if (spec) {
    if (title) title.textContent = "Редактировать врача / ресурс";
    if (submit) submit.textContent = "Сохранить изменения";
    if (editId) editId.value = String(spec.id);
    form.elements.full_name.value = spec.full_name || "";
    form.elements.specialty_label.value = spec.specialty_label || "";
    form.elements.direction_id.value = String(spec.direction_id || "");
    form.elements.phone.value = spec.phone || "";
    form.elements.work_schedule_note.value = spec.work_schedule_note || "";
    form.elements.work_time_from.value = spec.work_time_from || "09:00";
    form.elements.work_time_to.value = spec.work_time_to || "18:00";
    form.elements.default_duration_min.value = spec.default_duration_min || "";
    const wd = String(spec.work_days || "1,2,3,4,5")
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
    document.querySelectorAll('#form-add-spec input[name="wd"]').forEach((el) => {
      el.checked = wd.includes(el.value);
    });
  } else {
    if (title) title.textContent = "Новый врач / ресурс";
    if (submit) submit.textContent = "Добавить врача";
    if (editId) editId.value = "";
  }
}

/** Единый формат для сопоставления слота в сетке и start_at из API (с секундами или без). */
function normalizeSlotDt(text) {
  if (!text) return "";
  const s = String(text).trim();
  if (s.length >= 16) return s.slice(0, 16);
  return s;
}

function parseDT(text) {
  const [d, t] = text.split(" ");
  const [y, m, day] = d.split("-").map(Number);
  const parts = (t || "00:00").split(":");
  const hh = Number(parts[0]);
  const mm = Number(parts[1] || 0);
  return new Date(y, m - 1, day, hh, mm, 0);
}

function durationMin(a) {
  return Math.max(30, (parseDT(a.end_at) - parseDT(a.start_at)) / 60000);
}

function getVisibleDates() {
  const mode = document.getElementById("view-mode").value;
  const base = parseDT(`${document.getElementById("date-filter").value} 00:00`);
  if (mode === "week") {
    const list = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(base);
      d.setDate(base.getDate() + i);
      list.push(formatDate(d));
    }
    return list;
  }
  return [document.getElementById("date-filter").value];
}

async function loadDirections() {
  state.directions = await j("/web-api/booking/directions");
  const select = document.getElementById("direction_id");
  select.innerHTML = state.directions
    .map((d) => `<option value="${d.id}">${d.name} (${d.duration_min} мин)</option>`)
    .join("");
}

async function loadSpecialists() {
  state.specialists = await j("/web-api/booking/specialists");
  const filter = document.getElementById("specialist-filter");
  const formSel = document.getElementById("specialist_id");
  const opts = [`<option value="">Все</option>`].concat(
    state.specialists.map((s) => `<option value="${s.id}">${s.full_name}</option>`)
  );
  filter.innerHTML = opts.join("");
  formSel.innerHTML = state.specialists
    .map((s) => `<option value="${s.id}">${s.full_name}</option>`)
    .join("");
}

async function loadQueue() {
  state.queue = await j("/web-api/booking/queue");
  renderQueue();
  document.getElementById("chip-waiting").textContent = String(state.queue.length);
}

function renderQueue() {
  const search = (document.getElementById("queue-search").value || "").trim().toLowerCase();
  const list = document.getElementById("queue-list");
  const filtered = state.queue.filter((x) => {
    if (!search) return true;
    return `${x.name} ${x.phone}`.toLowerCase().includes(search);
  });
  list.innerHTML = filtered
    .map(
      (x) => `<button class="queue-item queue-pick" data-phone="${x.phone}" data-name="${x.name}" data-manager="${
        x.responsible_manager_id || ""
      }">
        <b>${x.name}</b><br>${x.phone}<br><small>Менеджер: ${x.responsible_manager_id || "-"}</small>
      </button>`
    )
    .join("");

  document.querySelectorAll(".queue-pick").forEach((el) =>
    el.addEventListener("click", () => {
      document.getElementById("lead_phone").value = el.dataset.phone || "";
      document.getElementById("patient_phone").value = el.dataset.phone || "";
      document.getElementById("patient_name").value = el.dataset.name || "";
      document.getElementById("responsible_manager_id").value = el.dataset.manager || "";
    })
  );
}

async function loadAppointments() {
  const spec = document.getElementById("specialist-filter").value;
  const dates = getVisibleDates();
  const all = [];
  for (const d of dates) {
    const qs = new URLSearchParams();
    qs.set("date", d);
    if (spec) qs.set("specialist_id", spec);
    const rows = await j(`/web-api/booking/appointments?${qs.toString()}`);
    all.push(...rows);
  }
  state.appointments = all;
  renderScheduler();
  updateChips();
}

function updateChips() {
  const totalBooked = state.appointments.filter((a) => a.status === "booked").length;
  const now = new Date();
  const late = state.appointments.filter((a) => a.status === "booked" && parseDT(a.start_at) < now).length;
  document.getElementById("chip-booked").textContent = String(totalBooked);
  document.getElementById("chip-late").textContent = String(late);
}

function renderScheduler() {
  const wrap = document.getElementById("scheduler-wrap");
  const specFilter = document.getElementById("specialist-filter").value;
  let specialists = state.specialists.slice();
  if (specFilter) specialists = specialists.filter((s) => String(s.id) === String(specFilter));
  if (!specialists.length) {
    wrap.innerHTML = "<div class='panel'>Нет специалистов</div>";
    return;
  }
  const dates = getVisibleDates();
  const resources = [];
  for (const d of dates) {
    for (const s of specialists) {
      resources.push({ key: `${d}__${s.id}`, date: d, specialist: s });
    }
  }

  const rows = [];
  for (let h = state.dayStartHour; h < state.dayEndHour; h++) {
    for (let m = 0; m < 60; m += state.stepMin) {
      rows.push(`${pad(h)}:${pad(m)}`);
    }
  }
  const grid = document.createElement("div");
  grid.className = "sched-grid";
  const nRes = resources.length;
  const colTpl = `70px repeat(${nRes}, minmax(200px, 1fr)) minmax(130px, 160px)`;
  grid.style.minWidth = `${70 + nRes * 200 + 160}px`;

  const weekMode = document.getElementById("view-mode").value === "week";

  const head = document.createElement("div");
  head.className = "sched-head";
  head.style.gridTemplateColumns = colTpl;
  head.innerHTML =
    `<div>Время</div>` +
    resources
      .map((r) => {
        const s = r.specialist;
        const sub = esc(specialistSubtitle(s));
        const name = esc(s.full_name);
        const dateLine = weekMode ? `<div class="sched-head-date">${esc(r.date)}</div>` : "";
        return `<div class="sched-head-resource"><div class="sched-resource-text"><div class="sched-resource-name">${name}</div><div class="sched-resource-sub">${sub}</div>${dateLine}</div><div class="sched-head-actions"><button type="button" class="sched-kebab" data-specialist-id="${s.id}" title="Действия">⋯</button></div></div>`;
      })
      .join("") +
    `<div class="sched-head-add"><button type="button" class="btn-add-resource" id="btn-open-spec-drawer">+ Добавить</button></div>`;
  grid.appendChild(head);

  const body = document.createElement("div");
  body.className = "sched-body";
  body.style.gridTemplateColumns = colTpl;
  rows.forEach((time) => {
    body.insertAdjacentHTML("beforeend", `<div class="time-cell">${time}</div>`);
    resources.forEach((r) => {
      const dt = normalizeSlotDt(`${r.date} ${time}`);
      const slot = document.createElement("div");
      slot.className = "slot";
      slot.dataset.specialistId = String(r.specialist.id);
      slot.dataset.startAt = dt;
      slot.title = "Клик — выбрать слот для записи";
      slot.addEventListener("dragover", (e) => {
        e.preventDefault();
        slot.classList.add("hover");
      });
      slot.addEventListener("dragleave", () => slot.classList.remove("hover"));
      slot.addEventListener("drop", async (e) => {
        e.preventDefault();
        slot.classList.remove("hover");
        if (!state.dragApptId) return;
        try {
          await j(`/web-api/booking/appointments/${state.dragApptId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              specialist_id: Number(slot.dataset.specialistId),
              start_at: slot.dataset.startAt,
            }),
          });
          await loadAppointments();
          setMsg("Запись перенесена", true);
        } catch (err) {
          setMsg(err.message, false);
        }
      });
      slot.addEventListener("click", (e) => {
        if (e.target.closest(".appt")) return;
        document.querySelectorAll(".slot.slot-picked").forEach((el) => el.classList.remove("slot-picked"));
        slot.classList.add("slot-picked");
        const sid = Number(slot.dataset.specialistId);
        document.getElementById("specialist_id").value = String(sid);
        const spec = state.specialists.find((x) => Number(x.id) === sid);
        if (spec && spec.direction_id) {
          document.getElementById("direction_id").value = String(spec.direction_id);
        }
        const raw = slot.dataset.startAt;
        const [datePart, timePart] = raw.split(" ");
        document.getElementById("start_at").value = `${datePart}T${timePart}`;
        setMsg("Слот выбран — укажите пациента и нажмите «Записать пациента»", true);
        document.getElementById("patient_name").focus();
      });
      body.appendChild(slot);
    });
    const filler = document.createElement("div");
    filler.className = "sched-filler";
    body.appendChild(filler);
  });
  grid.appendChild(body);
  wrap.innerHTML = "";
  wrap.appendChild(grid);

  document.getElementById("btn-open-spec-drawer")?.addEventListener("click", (e) => {
    e.preventDefault();
    openSpecDrawer();
  });
  wrap.querySelectorAll(".sched-kebab").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const sid = Number(btn.dataset.specialistId || 0);
      if (!sid) return;
      if (activeSpecMenuId === sid) {
        closeSpecMenu();
        return;
      }
      openSpecMenuAt(sid, btn);
    });
  });

  for (const appt of state.appointments) {
    const startKey = normalizeSlotDt(appt.start_at);
    const sid = appt.specialist_id != null ? String(appt.specialist_id) : "";
    const slot = wrap.querySelector(`.slot[data-specialist-id="${sid}"][data-start-at="${startKey}"]`);
    if (!slot) continue;
    const block = document.createElement("div");
    block.className = `appt ${appt.status}`;
    block.draggable = true;
    block.dataset.apptId = String(appt.id);
    block.title = "Клик — карточка записи";
    block.innerHTML = `<span class="title">${esc(appt.patient_name)}</span>`;
    const rowPx = 32;
    const h = Math.max(28, Math.floor((durationMin(appt) / state.stepMin) * rowPx) - 6);
    block.style.height = `${h}px`;
    block.addEventListener("click", (e) => {
      e.stopPropagation();
      openClientDrawer(appt.id);
    });
    block.addEventListener("dragstart", () => {
      state.dragApptId = Number(block.dataset.apptId);
      block.classList.add("appt-dragging");
      block.style.opacity = "0.88";
    });
    block.addEventListener("dragend", () => {
      state.dragApptId = null;
      block.classList.remove("appt-dragging");
      block.style.opacity = "1";
    });
    slot.appendChild(block);
  }
}

function init() {
  const dateInput = document.getElementById("date-filter");
  const mini = document.getElementById("mini-calendar");
  const now = new Date();
  const today = formatDate(now);
  dateInput.value = today;
  mini.value = today;

  document.getElementById("refresh-btn").addEventListener("click", loadAppointments);
  document.getElementById("specialist-filter").addEventListener("change", loadAppointments);
  document.getElementById("view-mode").addEventListener("change", loadAppointments);
  document.getElementById("queue-search").addEventListener("input", renderQueue);
  dateInput.addEventListener("change", () => {
    mini.value = dateInput.value;
    loadAppointments();
  });
  mini.addEventListener("change", () => {
    dateInput.value = mini.value;
    loadAppointments();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#sched-spec-menu") && !e.target.closest(".sched-kebab")) {
      closeSpecMenu();
    }
  });

  drawerBackdrop()?.addEventListener("click", closeDrawers);
  document.getElementById("cd-close")?.addEventListener("click", closeDrawers);
  document.getElementById("sd-close")?.addEventListener("click", closeDrawers);
  document.getElementById("sched-spec-menu-edit")?.addEventListener("click", () => {
    if (!activeSpecMenuId) return;
    const spec = state.specialists.find((s) => Number(s.id) === Number(activeSpecMenuId));
    if (!spec) return;
    closeSpecMenu();
    openSpecDrawer(spec);
  });
  document.getElementById("sched-spec-menu-delete")?.addEventListener("click", async () => {
    if (!activeSpecMenuId) return;
    const spec = state.specialists.find((s) => Number(s.id) === Number(activeSpecMenuId));
    const label = spec?.full_name || "этого специалиста";
    if (!confirm(`Удалить ${label}?`)) return;
    try {
      await j(`/web-api/booking/specialists/${activeSpecMenuId}`, { method: "DELETE" });
      closeSpecMenu();
      await loadSpecialists();
      await loadAppointments();
      setMsg("Специалист удалён", true);
    } catch (err) {
      setMsg(err.message, false);
    }
  });

  document.getElementById("cd-save-status")?.addEventListener("click", async () => {
    if (!currentAppointmentId) return;
    const st = document.getElementById("cd-status").value;
    try {
      await j(`/web-api/booking/appointments/${currentAppointmentId}/status`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: st }),
      });
      await loadAppointments();
      closeDrawers();
      setMsg("Статус обновлён", true);
    } catch (err) {
      setMsg(err.message, false);
    }
  });

  document.getElementById("form-add-spec")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const msg = document.getElementById("sd-msg");
    const work_days =
      [...document.querySelectorAll('#form-add-spec input[name="wd"]:checked')]
        .map((x) => x.value)
        .sort((a, b) => Number(a) - Number(b))
        .join(",") || "1,2,3,4,5";
    const durRaw = fd.get("default_duration_min");
    const durNum = durRaw != null && String(durRaw).trim() !== "" ? Number(durRaw) : null;
    const payload = {
      full_name: String(fd.get("full_name") || "").trim(),
      specialty_label: String(fd.get("specialty_label") || "").trim(),
      direction_id: Number(fd.get("direction_id")),
      phone: String(fd.get("phone") || "").trim(),
      work_schedule_note: String(fd.get("work_schedule_note") || "").trim(),
      work_time_from: String(fd.get("work_time_from") || "09:00"),
      work_time_to: String(fd.get("work_time_to") || "18:00"),
      work_days,
      default_duration_min: durNum != null && !Number.isNaN(durNum) ? durNum : null,
    };
    try {
      const editId = Number(document.getElementById("sd-edit-id").value || 0);
      await j(editId ? `/web-api/booking/specialists/${editId}` : "/web-api/booking/specialists", {
        method: editId ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      msg.textContent = "";
      msg.className = "hint ok";
      await loadSpecialists();
      await loadAppointments();
      closeDrawers();
      setMsg(editId ? "Изменения сохранены" : "Врач добавлен", true);
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "hint err";
    }
  });

  document.getElementById("booking-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const start = document.getElementById("start_at").value;
      const payload = {
        lead_phone: document.getElementById("lead_phone").value,
        patient_name: document.getElementById("patient_name").value.trim(),
        patient_phone: document.getElementById("patient_phone").value.trim(),
        direction_id: Number(document.getElementById("direction_id").value),
        specialist_id: Number(document.getElementById("specialist_id").value),
        start_at: start.replace("T", " "),
        responsible_manager_id: document.getElementById("responsible_manager_id").value
          ? Number(document.getElementById("responsible_manager_id").value)
          : null,
        comment: document.getElementById("comment").value.trim(),
      };
      await j("/web-api/booking/appointments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setMsg("Запись создана", true);
      await loadAppointments();
      await loadQueue();
    } catch (err) {
      setMsg(err.message, false);
    }
  });

  loadDirections().then(loadSpecialists).then(loadQueue).then(loadAppointments);
}

init();
