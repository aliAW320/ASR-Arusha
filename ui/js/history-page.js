import { api } from "./api.js";
import { $, escapeHtml, formatDate, renderSidebar, requireUser, toast } from "./layout.js";

async function loadHistory(filters = {}) {
  try {
    const params = new URLSearchParams(Object.entries(filters).filter(([, value]) => value));
    const items = await api(`/history?${params}`);
    $("#history-list").innerHTML = items.length ? items.map((item) => `<article class="timeline-item"><span class="timeline-dot"></span><div><h4>${escapeHtml(item.event_type)}</h4><p>${escapeHtml(item.action_description)}</p><small>${item.correlation_id ? `Correlation: ${escapeHtml(item.correlation_id)}` : "بدون شناسه هم‌بستگی"}</small></div><div class="timeline-meta">${formatDate(item.created_at, true)}<br>${escapeHtml(item.ip_address || "")}</div></article>`).join("") : `<div class="empty-state"><p>رویدادی پیدا نشد.</p></div>`;
  } catch (exception) { toast(exception.message, "error"); }
}
const user = await requireUser({ admin: true });
if (user) { renderSidebar(user, "history"); loadHistory(); }
$("#refresh-history").addEventListener("click", () => loadHistory());
$("#history-filter").addEventListener("submit", (event) => { event.preventDefault(); loadHistory(Object.fromEntries(new FormData(event.currentTarget).entries())); });
