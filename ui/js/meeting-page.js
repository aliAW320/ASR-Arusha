import { api } from "./api.js?v=20260912a";
import { $, escapeHtml, faNumber, formatDate, renderSidebar, requireUser, toast } from "./layout.js?v=20260912a";

const meetingId = new URLSearchParams(location.search).get("id");
let meeting;
if (!meetingId) location.replace("/meetings.html");
else $("#meeting-transcript-link").href = `/meeting-transcript.html?id=${encodeURIComponent(meetingId)}`;

function roleLabel(role) { return ({ owner: "مالک", contributor: "همکار", viewer: "مشاهده‌گر" })[role] || role; }

const STAGE_LABELS = {
  preprocess: "پیش‌پردازش",
  transcription: "رونویسی",
  diarization: "تفکیک گوینده",
  alignment: "هم‌ترازی متن و گوینده",
  cleaning: "پاک‌سازی متن",
  minutes_generation: "خلاصه‌سازی",
  meeting_compose: "ترکیب جلسه",
};
const JOB_STATUS_LABELS = { queued: "در صف", running: "در حال اجرا", succeeded: "موفق", failed: "ناموفق", cancelled: "لغوشده" };
const VOICE_STATUS_LABELS = { uploaded: "دریافت‌شده", processing: "در حال پردازش", finished: "آماده", error: "خطا" };
const JOB_STATUS_PRIORITY = { failed: 0, running: 1, queued: 2, succeeded: 3, cancelled: 4 };
const STAGE_ORDER = { preprocess: 0, transcription: 1, diarization: 1, alignment: 2, cleaning: 3, minutes_generation: 4, meeting_compose: 5 };
let diarizationAvailable = false;

function pickActiveJob(jobs) {
  if (!jobs.length) return null;
  const latestResultId = jobs.reduce((latest, job) => (!latest || job.created_at > latest.created_at ? job : latest), null).result_id;
  return jobs
    .filter((job) => job.result_id === latestResultId)
    .sort((a, b) => (JOB_STATUS_PRIORITY[a.status] ?? 9) - (JOB_STATUS_PRIORITY[b.status] ?? 9) || (STAGE_ORDER[a.stage] ?? 9) - (STAGE_ORDER[b.stage] ?? 9))[0];
}

function describeVoiceProcessing(voice, job) {
  if (!job) return { text: VOICE_STATUS_LABELS[voice.status] || "در انتظار پردازش", className: "" };
  const stageLabel = STAGE_LABELS[job.stage] || job.stage;
  const statusLabel = JOB_STATUS_LABELS[job.status] || job.status;
  const latestAttempt = job.attempts && job.attempts.length ? job.attempts[job.attempts.length - 1] : null;
  if (job.status === "failed" && latestAttempt) {
    const code = latestAttempt.error_code || "خطای نامشخص";
    return { text: `${stageLabel} ناموفق (${code})`, className: "status-failed", title: latestAttempt.error_message || "" };
  }
  if (job.status === "running") {
    const attemptSuffix = latestAttempt && latestAttempt.attempt_number > 1 ? ` · تلاش ${faNumber(latestAttempt.attempt_number)}` : "";
    return { text: `${stageLabel}: ${statusLabel}${attemptSuffix}`, className: "status-running" };
  }
  return { text: `${stageLabel}: ${statusLabel}`, className: "" };
}

function renderVoices(voices, transcripts, processingByVoice) {
  $("#voice-list").innerHTML = voices.length ? voices.map((voice) => {
    const transcript = transcripts[voice.id];
    const job = pickActiveJob(processingByVoice[voice.id] || []);
    const detail = describeVoiceProcessing(voice, job);
    return `<div class="list-row"><div class="list-row-main"><span class="file-icon"><svg class="icon"><use href="#i-mic"/></svg></span><div><strong>${escapeHtml(voice.original_filename || "فایل صوتی")}</strong><small>${voice.size_bytes ? `${faNumber((voice.size_bytes / 1048576).toFixed(2))} مگابایت` : "اندازه نامشخص"} · <span class="voice-status ${detail.className}"${detail.title ? ` title="${escapeHtml(detail.title)}"` : ""}>${escapeHtml(detail.text)}</span></small>${transcript ? `<p class="transcript-text">${escapeHtml(transcript.text)}</p><a class="text-button" href="/transcript.html?result_id=${encodeURIComponent(transcript.id)}">مشاهده متن کامل <svg class="icon"><use href="#i-arrow-next"/></svg></a>` : ""}</div></div><button class="delete-icon" aria-label="حذف فایل" data-delete-voice="${voice.id}"><svg class="icon"><use href="#i-trash"/></svg></button></div>`;
  }).join("") : `<div class="empty-state"><svg class="icon"><use href="#i-mic"/></svg><p>فایل صوتی ثبت نشده است.</p></div>`;
}

function attachmentIcon(contentType) {
  return contentType === "application/pdf" ? "file-pdf" : "image";
}

function attachmentTypeLabel(contentType) {
  return contentType === "application/pdf" ? "سند PDF" : "تصویر";
}

function renderAttachments(attachments) {
  $("#attachment-count").textContent = faNumber(attachments.length);
  $("#attachment-list").innerHTML = attachments.length
    ? attachments.map((attachment) => `<div class="list-row"><div class="list-row-main"><span class="file-icon"><svg class="icon"><use href="#i-${attachmentIcon(attachment.content_type)}"/></svg></span><div><strong>${escapeHtml(attachment.original_filename)}</strong><small>${attachmentTypeLabel(attachment.content_type)} · ${faNumber((attachment.size_bytes / 1048576).toFixed(2))} مگابایت</small></div></div><button class="delete-icon" type="button" aria-label="حذف ${escapeHtml(attachment.original_filename)}" data-delete-attachment="${attachment.id}"><svg class="icon"><use href="#i-trash"/></svg></button></div>`).join("")
    : `<div class="empty-state compact-empty"><svg class="icon"><use href="#i-paperclip"/></svg><p>پیوستی برای این جلسه ثبت نشده است.</p></div>`;
}

function stageState(jobs, stages) {
  const relevant = jobs.filter((job) => stages.includes(job.stage));
  if (!relevant.length) return "idle";
  if (relevant.some((job) => ["queued", "running"].includes(job.status))) return "active";
  if (relevant.some((job) => job.status === "failed")) return "failed";
  if (relevant.some((job) => job.status === "succeeded")) return "done";
  return "idle";
}

function renderPipeline(voices, attachments, jobs, publication) {
  const states = {
    upload: voices.length || attachments.length ? "done" : "idle",
    transcription: stageState(jobs, ["preprocess", "transcription", "diarization", "alignment"]),
    cleaning: stageState(jobs, ["cleaning"]),
    meeting_compose: stageState(jobs, ["meeting_compose"]),
    publication: publication.status === "published" ? "done" : publication.status === "failed" ? "failed" : ["queued", "running"].includes(publication.status) || (publication.status === "pending" && voices.length) ? "active" : "idle",
  };
  const order = ["upload", "transcription", "cleaning", "meeting_compose", "publication"];
  let blocked = false;
  for (const key of order) {
    const item = document.querySelector(`[data-pipeline-stage="${key}"]`);
    let state = states[key];
    if (blocked && state === "idle") state = "blocked";
    item.dataset.state = state;
    item.setAttribute("aria-label", `${item.querySelector("strong").textContent}: ${{ done: "انجام‌شده", active: "در حال انجام", failed: "ناموفق", blocked: "در انتظار مرحله قبل", idle: "هنوز شروع نشده" }[state]}`);
    if (["active", "failed"].includes(state)) blocked = true;
  }
  const failed = order.find((key) => states[key] === "failed");
  const active = order.find((key) => states[key] === "active");
  const activeLabel = active && document.querySelector(`[data-pipeline-stage="${active}"] strong`).textContent;
  $("#pipeline-summary").textContent = failed ? "پردازش به بررسی نیاز دارد" : activeLabel ? `${activeLabel} در حال انجام است` : publication.status === "published" ? "خروجی جلسه در پایگاه دانش به‌روز است" : voices.length ? "فایل‌ها دریافت شدند" : "در انتظار نخستین فایل";
}
function renderMembers(members) {
  $("#member-list").innerHTML = members.map((member) => `<div class="list-row"><div class="list-row-main"><span class="avatar">${escapeHtml((member.user.full_name || member.user.email).slice(0, 1))}</span><div><strong>${escapeHtml(member.user.full_name || member.user.email)}</strong><small>${escapeHtml(member.user.email)} · ${roleLabel(member.role)}</small></div></div>${member.role === "owner" ? `<span class="status-pill">مالک</span>` : `<div class="row-actions"><select data-member-role="${member.user.id}"><option value="viewer" ${member.role === "viewer" ? "selected" : ""}>مشاهده‌گر</option><option value="contributor" ${member.role === "contributor" ? "selected" : ""}>همکار</option></select><button class="delete-icon" aria-label="حذف عضو" data-delete-member="${member.user.id}"><svg class="icon"><use href="#i-trash"/></svg></button></div>`}</div>`).join("");
}
async function refreshCollections() {
  const [members, voices, jobs, attachments, publication] = await Promise.all([
    api(`/meetings/${meetingId}/members`),
    api(`/meetings/${meetingId}/voices`),
    api(`/meetings/${meetingId}/processing`),
    api(`/meetings/${meetingId}/attachments`),
    api(`/meetings/${meetingId}/publication`),
  ]);
  renderMembers(members);
  renderAttachments(attachments);
  renderPipeline(voices, attachments, jobs, publication);
  const processingByVoice = {};
  for (const job of jobs) {
    if (!job.voice_id) continue;
    (processingByVoice[job.voice_id] ||= []).push(job);
  }
  const transcripts = {};
  await Promise.all(voices.map(async (voice) => {
    const results = await api(`/voices/${voice.id}/results`);
    const latest = results.find((result) => result.artifacts.some((artifact) => artifact.artifact_type === "transcript_json"));
    if (latest) {
      try { transcripts[voice.id] = { id: latest.id, text: (await api(`/results/${latest.id}/transcript`)).text }; }
      catch (_) { /* result may finish between polling calls */ }
    }
  }));
  renderVoices(voices, transcripts, processingByVoice);
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
if (user) {
  renderSidebar(user);
  loadMeeting();
  api("/processing/diarization").then((availability) => {
    diarizationAvailable = Boolean(availability.available && availability.enabled);
    $("#diarization-option").classList.toggle("hidden", !diarizationAvailable);
    renderStagedFiles();
  }).catch(() => { diarizationAvailable = false; });
}
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
// Files wait here until the user presses «شروع پردازش»; nothing reaches the
// backend — and so nothing starts processing — before that.
const stagedFiles = [];
let uploading = false;
const fileKey = (file) => `${file.name}:${file.size}`;
const stagedIcon = (file) => (file.type === "application/pdf" ? "file-pdf" : file.type.startsWith("image/") ? "image" : "mic");

function renderStagedFiles() {
  $("#staged-files").classList.toggle("hidden", !stagedFiles.length);
  $("#staged-count").textContent = faNumber(stagedFiles.length);
  $("#staged-list").innerHTML = stagedFiles.map((file, index) => `<li class="list-row"><div class="list-row-main"><span class="file-icon"><svg class="icon"><use href="#i-${stagedIcon(file)}"/></svg></span><div><strong>${escapeHtml(file.name)}</strong><small>${faNumber((file.size / 1048576).toFixed(2))} مگابایت · هنوز ارسال نشده</small></div></div><button class="delete-icon" type="button" aria-label="حذف ${escapeHtml(file.name)} از فهرست" data-unstage="${index}"><svg class="icon"><use href="#i-x"/></svg></button></li>`).join("");
  $("#start-process").disabled = uploading || !stagedFiles.length;
  $("#selected-files").textContent = stagedFiles.length ? `${faNumber(stagedFiles.length)} فایل آماده ارسال است.` : "";
  $("#start-process-hint").textContent = !stagedFiles.length
    ? "تا زمانی که این دکمه را نزنید، هیچ فایلی ارسال نمی‌شود."
    : diarizationAvailable
      ? "پیش از ارسال، تکلیف تفکیک گویندگان پرسیده می‌شود."
      : "فایل‌ها ارسال و بی‌درنگ وارد مسیر پردازش می‌شوند.";
}

function askAboutDiarization() {
  // Resolves true (do diarize), false (go ahead without it) or null (cancel).
  const dialog = $("#diarization-confirm");
  return new Promise((resolve) => {
    const answer = (value) => { resolve(value); dialog.close(); };
    $("#confirm-with-diarization").onclick = () => answer(true);
    $("#confirm-without-diarization").onclick = () => answer(false);
    $("[data-close-diarization]").onclick = () => answer(null);
    dialog.addEventListener("close", () => resolve(null), { once: true });
    dialog.showModal();
  });
}

async function startProcessing(withDiarization) {
  const files = [...stagedFiles];
  const progress = $("#upload-progress");
  uploading = true;
  $("#start-process").disabled = true;
  progress.classList.remove("hidden");
  const data = new FormData();
  files.forEach((file) => data.append("uploads", file));
  try {
    const result = await api(`/meetings/${meetingId}/files`, { method: "POST", body: data });
    stagedFiles.length = 0;
    if (diarizationAvailable && result.voices.length) {
      try {
        await Promise.all(result.voices.map((voice) => api(`/voices/${voice.id}/diarization`, { method: "POST", body: JSON.stringify({ enabled: withDiarization }) })));
      } catch (exception) {
        toast(`فایل‌ها ذخیره شدند، اما انتخاب تفکیک گویندگان ثبت نشد: ${exception.message}`, "error");
        await refreshCollections();
        return;
      }
    }
    toast(`پردازش ${faNumber(files.length)} فایل آغاز شد.`);
    await refreshCollections();
  }
  catch (exception) { toast(exception.message, "error"); }
  finally { uploading = false; progress.classList.add("hidden"); renderStagedFiles(); }
}

$("#meeting-files-input").addEventListener("change", (event) => {
  const known = new Set(stagedFiles.map(fileKey));
  for (const file of event.target.files) {
    if (known.has(fileKey(file))) continue;
    known.add(fileKey(file));
    stagedFiles.push(file);
  }
  event.target.value = "";
  renderStagedFiles();
});
$("#staged-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-unstage]"); if (!button) return;
  stagedFiles.splice(Number(button.dataset.unstage), 1);
  renderStagedFiles();
});
$("#clear-staged").addEventListener("click", () => { stagedFiles.length = 0; renderStagedFiles(); });
$("#start-process").addEventListener("click", async () => {
  if (uploading || !stagedFiles.length) return;
  let withDiarization = $("#diarization-enabled").checked;
  if (diarizationAvailable && !withDiarization) {
    // The service is up and the user passed on it — give them one more chance
    // before the choice is locked in for these voices.
    const answer = await askAboutDiarization();
    if (answer === null) return;
    withDiarization = answer;
    $("#diarization-enabled").checked = answer;
  }
  await startProcessing(diarizationAvailable && withDiarization);
});
$("#voice-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-voice]"); if (!button || !confirm("فایل حذف شود؟")) return;
  try { await api(`/voices/${button.dataset.deleteVoice}`, { method: "DELETE" }); toast("فایل حذف شد."); refreshCollections(); }
  catch (exception) { toast(exception.message, "error"); }
});
$("#attachment-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-attachment]"); if (!button || !confirm("پیوست حذف شود؟")) return;
  try { await api(`/meetings/${meetingId}/attachments/${button.dataset.deleteAttachment}`, { method: "DELETE" }); toast("پیوست حذف شد."); refreshCollections(); }
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
