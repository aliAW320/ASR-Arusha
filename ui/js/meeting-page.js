import { api } from "./api.js";
import { $, escapeHtml, faNumber, formatDate, renderSidebar, requireUser, toast } from "./layout.js";

const meetingId = new URLSearchParams(location.search).get("id");
let meeting;
if (!meetingId) location.replace("/meetings.html");
else $("#meeting-transcript-link").href = `/meeting-transcript.html?id=${encodeURIComponent(meetingId)}`;

function roleLabel(role) { return ({ owner: "مالک", contributor: "همکار", viewer: "مشاهده‌گر" })[role] || role; }
function renderVoices(voices, transcripts) {
  $("#voice-list").innerHTML = voices.length ? voices.map((voice) => {
    const transcript = transcripts[voice.id];
    return `<div class="list-row"><div class="list-row-main"><span class="file-icon">♫</span><div><strong>${escapeHtml(voice.original_filename || "فایل صوتی")}</strong><small>${voice.size_bytes ? `${faNumber((voice.size_bytes / 1048576).toFixed(2))} مگابایت` : "اندازه نامشخص"} · ${escapeHtml(voice.status)}</small>${transcript ? `<p class="transcript-text">${escapeHtml(transcript.text)}</p><a class="text-button" href="/transcript.html?result_id=${encodeURIComponent(transcript.id)}">مشاهده متن کامل ←</a>` : ""}</div></div><button class="delete-icon" data-delete-voice="${voice.id}">×</button></div>`;
  }).join("") : `<div class="empty-state"><span>♫</span><p>فایل صوتی ثبت نشده است.</p></div>`;
}
function renderMembers(members) {
  $("#member-list").innerHTML = members.map((member) => `<div class="list-row"><div class="list-row-main"><span class="avatar">${escapeHtml((member.user.full_name || member.user.email).slice(0, 1))}</span><div><strong>${escapeHtml(member.user.full_name || member.user.email)}</strong><small>${escapeHtml(member.user.email)} · ${roleLabel(member.role)}</small></div></div>${member.role === "owner" ? `<span class="status-pill">مالک</span>` : `<div class="row-actions"><select data-member-role="${member.user.id}"><option value="viewer" ${member.role === "viewer" ? "selected" : ""}>مشاهده‌گر</option><option value="contributor" ${member.role === "contributor" ? "selected" : ""}>همکار</option></select><button class="delete-icon" data-delete-member="${member.user.id}">×</button></div>`}</div>`).join("");
}
async function refreshCollections() {
  const [members, voices] = await Promise.all([api(`/meetings/${meetingId}/members`), api(`/meetings/${meetingId}/voices`)]);
  renderMembers(members);
  const transcripts = {};
  await Promise.all(voices.map(async (voice) => {
    const results = await api(`/voices/${voice.id}/results`);
    const latest = results.find((result) => result.artifacts.some((artifact) => artifact.artifact_type === "transcript_json"));
    if (latest) {
      try { transcripts[voice.id] = { id: latest.id, text: (await api(`/results/${latest.id}/transcript`)).text }; }
      catch (_) { /* result may finish between polling calls */ }
    }
  }));
  renderVoices(voices, transcripts);
}
let refreshInFlight = false;
async function pollCollections() {
  if (refreshInFlight || document.visibilityState === "hidden") return;
  refreshInFlight = true;
  try { await refreshCollections(); } catch (_) { /* keep the current view during transient API errors */ }
  finally { refreshInFlight = false; }
}
async function loadMeeting() {
  try {
    [meeting] = await Promise.all([api(`/meetings/${meetingId}`), refreshCollections()]);
    $("#detail-title").textContent = meeting.title;
    $("#detail-description").textContent = meeting.description || formatDate(meeting.date, true);
    document.title = `${meeting.title} | هم‌نشین`;
  } catch (exception) { toast(exception.message, "error"); }
}
const user = await requireUser();
if (user) { renderSidebar(user); loadMeeting(); }
setInterval(pollCollections, 3000);

const modal = $("#meeting-modal");
$("#edit-meeting").addEventListener("click", () => {
  const form = $("#meeting-form");
  form.elements.title.value = meeting.title;
  form.elements.description.value = meeting.description || "";
  form.elements.date.value = new Date(new Date(meeting.date).getTime() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  modal.showModal();
});
document.querySelectorAll("[data-close-modal]").forEach((button) => button.addEventListener("click", () => modal.close()));
$("#meeting-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget;
  const payload = { title: form.elements.title.value.trim(), description: form.elements.description.value.trim() || null, date: new Date(form.elements.date.value).toISOString() };
  try { meeting = await api(`/meetings/${meetingId}`, { method: "PATCH", body: JSON.stringify(payload) }); modal.close(); toast("تغییرات ذخیره شد."); loadMeeting(); }
  catch (exception) { toast(exception.message, "error"); }
});
$("#delete-meeting").addEventListener("click", async () => {
  if (!confirm(`جلسه «${meeting.title}» حذف شود؟`)) return;
  try { await api(`/meetings/${meetingId}`, { method: "DELETE" }); location.replace("/meetings.html"); }
  catch (exception) { toast(exception.message, "error"); }
});
$("#voice-input").addEventListener("change", async (event) => {
  const file = event.target.files[0]; if (!file) return;
  const progress = $("#upload-progress"); progress.classList.remove("hidden");
  const data = new FormData(); data.append("upload", file);
  try { await api(`/meetings/${meetingId}/voices`, { method: "POST", body: data }); toast("فایل ذخیره شد."); refreshCollections(); }
  catch (exception) { toast(exception.message, "error"); }
  finally { progress.classList.add("hidden"); event.target.value = ""; }
});
$("#voice-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-voice]"); if (!button || !confirm("فایل حذف شود؟")) return;
  try { await api(`/voices/${button.dataset.deleteVoice}`, { method: "DELETE" }); toast("فایل حذف شد."); refreshCollections(); }
  catch (exception) { toast(exception.message, "error"); }
});
$("#add-member-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget;
  try { await api(`/meetings/${meetingId}/members`, { method: "POST", body: JSON.stringify({ email: form.elements.email.value, role: form.elements.role.value }) }); form.reset(); toast("عضو اضافه شد."); refreshCollections(); }
  catch (exception) { toast(exception.message, "error"); }
});
$("#member-list").addEventListener("change", async (event) => {
  if (!event.target.matches("[data-member-role]")) return;
  try { await api(`/meetings/${meetingId}/members/${event.target.dataset.memberRole}`, { method: "PATCH", body: JSON.stringify({ role: event.target.value }) }); toast("نقش تغییر کرد."); refreshCollections(); }
  catch (exception) { toast(exception.message, "error"); }
});
$("#member-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-member]"); if (!button || !confirm("عضو حذف شود؟")) return;
  try { await api(`/meetings/${meetingId}/members/${button.dataset.deleteMember}`, { method: "DELETE" }); toast("عضو حذف شد."); refreshCollections(); }
  catch (exception) { toast(exception.message, "error"); }
});
