async function j(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

function fmt(item) {
  return `<div class="item"><strong>${item.name || item.patient_name}</strong><br>${item.phone || item.patient_phone || ""}</div>`;
}

function setMsg(text, ok = true) {
  const el = document.getElementById("form-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = `hint ${ok ? "ok" : "err"}`;
}

async function loadDirections() {
  const dirs = await j("/web-api/booking/directions");
  const select = document.getElementById("direction_id");
  select.innerHTML = dirs.map((d) => `<option value="${d.id}">${d.name} (${d.duration_min} мин)</option>`).join("");
}

async function loadSpecialists() {
  const specs = await j("/web-api/booking/specialists");
  const filter = document.getElementById("specialist-filter");
  const formSel = document.getElementById("specialist_id");
  const opts = [`<option value="">Все</option>`].concat(
    specs.map((s) => `<option value="${s.id}">${s.full_name}</option>`)
  );
  filter.innerHTML = opts.join("");
  formSel.innerHTML = specs.map((s) => `<option value="${s.id}">${s.full_name}</option>`).join("");
}

async function loadQueue() {
  const q = await j("/web-api/booking/queue");
  const list = document.getElementById("queue-list");
  list.innerHTML = q
    .map(
      (x) =>
        `<button class="item queue-pick" data-phone="${x.phone}" data-name="${x.name}" data-manager="${x.responsible_manager_id || ""}">
          <strong>${x.name}</strong><br>${x.phone}<br><small>Менеджер: ${x.responsible_manager_id || "-"}</small>
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
  const d = document.getElementById("date-filter").value;
  const s = document.getElementById("specialist-filter").value;
  const qs = new URLSearchParams();
  if (d) qs.set("date", d);
  if (s) qs.set("specialist_id", s);
  const items = await j(`/web-api/booking/appointments?${qs.toString()}`);
  const list = document.getElementById("appointments-list");
  list.innerHTML = items
    .map(
      (x) =>
        `<div class="item">
          <strong>${x.start_at} - ${x.end_at}</strong><br>
          ${x.patient_name} (${x.patient_phone})<br>
          ${x.specialist_name || ""} / ${x.direction_name || ""}<br>
          Статус: <b>${x.status}</b>
        </div>`
    )
    .join("");
}

function init() {
  const dateInput = document.getElementById("date-filter");
  const now = new Date();
  dateInput.value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(
    now.getDate()
  ).padStart(2, "0")}`;

  document.getElementById("refresh-btn").addEventListener("click", loadAppointments);
  document.getElementById("specialist-filter").addEventListener("change", loadAppointments);
  dateInput.addEventListener("change", loadAppointments);

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

  loadDirections().then(loadSpecialists).then(loadAppointments);
  loadQueue();
}

init();
