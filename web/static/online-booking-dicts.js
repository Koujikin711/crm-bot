async function j(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

function setMsg(text, ok = true) {
  const el = document.getElementById("dict-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = `hint ${ok ? "ok" : "err"}`;
}

async function loadDirections() {
  const dirs = await j("/web-api/booking/directions");
  document.getElementById("dir-list").innerHTML = dirs
    .map((d) => `<div class="item"><b>${d.name}</b> (${d.duration_min} мин)</div>`)
    .join("");
  document.getElementById("spec-direction").innerHTML = dirs
    .map((d) => `<option value="${d.id}">${d.name}</option>`)
    .join("");
}

async function loadSpecialists() {
  const specs = await j("/web-api/booking/specialists?all=1");
  document.getElementById("spec-list").innerHTML = specs
    .map((s) => `<div class="item"><b>${s.full_name}</b> — ${s.direction_name || "-"} ${s.phone || ""}</div>`)
    .join("");
}

document.getElementById("dir-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await j("/web-api/booking/directions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: document.getElementById("dir-name").value.trim(),
        duration_min: Number(document.getElementById("dir-duration").value || 30),
      }),
    });
    document.getElementById("dir-name").value = "";
    setMsg("Направление добавлено", true);
    await loadDirections();
  } catch (err) {
    setMsg(err.message, false);
  }
});

document.getElementById("spec-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await j("/web-api/booking/specialists", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        full_name: document.getElementById("spec-name").value.trim(),
        direction_id: Number(document.getElementById("spec-direction").value),
        phone: document.getElementById("spec-phone").value.trim(),
      }),
    });
    document.getElementById("spec-name").value = "";
    document.getElementById("spec-phone").value = "";
    setMsg("Специалист добавлен", true);
    await loadSpecialists();
  } catch (err) {
    setMsg(err.message, false);
  }
});

loadDirections().then(loadSpecialists);
