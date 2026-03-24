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
  grid.style.setProperty("--spec-count", String(resources.length));

  const head = document.createElement("div");
  head.className = "sched-head";
  head.innerHTML = `<div>Время</div>${resources
    .map((r) => `<div>${r.specialist.full_name}<br><small>${r.date}</small></div>`)
    .join("")}`;
  grid.appendChild(head);

  const body = document.createElement("div");
  body.className = "sched-body";
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
  });
  grid.appendChild(body);
  wrap.innerHTML = "";
  wrap.appendChild(grid);

  for (const appt of state.appointments) {
    const startKey = normalizeSlotDt(appt.start_at);
    const sid = appt.specialist_id != null ? String(appt.specialist_id) : "";
    const slot = wrap.querySelector(`.slot[data-specialist-id="${sid}"][data-start-at="${startKey}"]`);
    if (!slot) continue;
    const block = document.createElement("div");
    block.className = `appt ${appt.status}`;
    block.draggable = true;
    block.dataset.apptId = String(appt.id);
    block.title = `${appt.patient_name} ${appt.start_at}`;
    block.innerHTML = `<span class="title">${appt.patient_name}</span><span class="meta">${
      appt.start_at.split(" ")[1]
    }-${appt.end_at.split(" ")[1]} • ${appt.patient_phone}</span>`;
    const h = Math.max(30, Math.floor((durationMin(appt) / state.stepMin) * 34) - 6);
    block.style.height = `${h}px`;
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
    block.addEventListener("dblclick", async () => {
      const next = prompt("Статус: booked/completed/no_show/cancelled", appt.status);
      if (!next) return;
      try {
        await j(`/web-api/booking/appointments/${appt.id}/status`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: next }),
        });
        await loadAppointments();
      } catch (err) {
        setMsg(err.message, false);
      }
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
