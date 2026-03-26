async function j(url) {
  const r = await fetch(url);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

async function loadJournal() {
  const d = document.getElementById("journal-date").value;
  const qs = new URLSearchParams();
  if (d) qs.set("date", d);
  const items = await j(`/web-api/booking/appointments?${qs.toString()}`);
  document.getElementById("journal-list").innerHTML = items
    .map(
      (x) =>
        `<div class="item">
          <b>${x.start_at}</b> — ${x.patient_name} (${x.patient_phone})<br>
          ${x.specialist_name || ""} / ${x.direction_name || ""} / <b>${x.status}</b>
        </div>`
    )
    .join("");
}

const d = new Date();
document.getElementById("journal-date").value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(
  2,
  "0"
)}-${String(d.getDate()).padStart(2, "0")}`;
document.getElementById("journal-refresh").addEventListener("click", loadJournal);
loadJournal();
