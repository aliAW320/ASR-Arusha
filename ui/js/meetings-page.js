import { api } from "./api.js?v=20260908b";
import { $, escapeHtml, faNumber, formatDate, renderSidebar, requireUser, toast } from "./layout.js?v=20260908b";

function card(meeting) {
  const date = new Date(meeting.date);
  return `<a class="meeting-card" href="/meeting.html?id=${meeting.id}"><div class="meeting-card-top"><div class="date-tile"><strong>${faNumber(date.getDate())}</strong><small>${new Intl.DateTimeFormat("fa-IR", { month: "short" }).format(date)}</small></div><span class="status-pill">آماده</span></div><h4>${escapeHtml(meeting.title)}</h4><p>${escapeHtml(meeting.description || "بدون توضیحات")}</p><footer>${formatDate(meeting.date, true)} · مشاهده جزئیات <svg class="icon"><use href="#i-arrow-next"/></svg></footer></a>`;
}
async function loadMeetings() {
  try {
    const meetings = await api("/meetings");
    $("#meeting-count").textContent = faNumber(meetings.length);
    $("#meetings-grid").innerHTML = meetings.map(card).join("");
    $("#meetings-grid").classList.toggle("hidden", !meetings.length);
    $("#meetings-empty").classList.toggle("hidden", Boolean(meetings.length));
  } catch (exception) { toast(exception.message, "error"); }
}
const user = await requireUser();
if (user) { renderSidebar(user); loadMeetings(); }
const modal = $("#meeting-modal");
$("#new-meeting-button").addEventListener("click", () => modal.showModal());
$("[data-open-modal]").addEventListener("click", () => modal.showModal());
document.querySelectorAll("[data-close-modal]").forEach((button) => button.addEventListener("click", () => modal.close()));
$("#refresh-meetings").addEventListener("click", loadMeetings);
$("#meeting-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = { title: form.elements.title.value.trim(), description: form.elements.description.value.trim() || null };
  if (form.elements.date.value) payload.date = new Date(form.elements.date.value).toISOString();
  try { await api("/meetings", { method: "POST", body: JSON.stringify(payload) }); modal.close(); form.reset(); toast("جلسه ساخته شد."); loadMeetings(); }
  catch (exception) { toast(exception.message, "error"); }
});
