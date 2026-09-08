import { api } from "./api.js";
import { $, escapeHtml, formatDate, renderSidebar, requireUser, toast } from "./layout.js";

const meetingId = new URLSearchParams(location.search).get("id");
if (!meetingId) location.replace("/meetings.html");

const STATUS_LABELS = {
  queued: "در صف",
  running: "در حال پردازش",
  succeeded: "آماده",
  failed: "ناموفق",
  cancelled: "لغوشده",
};
const PUBLICATION_STATUS_LABELS = {
  awaiting_approval: "منتظر تأیید",
  queued: "در صف انتشار",
  running: "در حال خلاصه‌سازی و انتشار",
  published: "منتشرشده",
  failed: "انتشار ناموفق",
};

let pollTimer = null;
let versions = [];
let selectedId = null;
let publicationPollTimer = null;

function formatTime(milliseconds) {
  const totalSeconds = Math.floor((milliseconds || 0) / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  return `${String(minutes).padStart(2, "0")}:${String(totalSeconds % 60).padStart(2, "0")}`;
}

function renderStatusPill(status) {
  const pill = $("#meeting-transcript-status");
  pill.textContent = STATUS_LABELS[status] || status || "";
  pill.dataset.status = status || "";
}

function renderProcessingError(error) {
  const panel = $("#meeting-processing-error");
  if (!error) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  $("#meeting-processing-error-title").textContent = "ترکیب متن جلسه ناموفق بود";
  $("#meeting-processing-error-code").textContent = error.code || "meeting_composer_error";
  $("#meeting-processing-error-message").textContent = error.message || "علت خطا ثبت نشده است.";
}

function renderSources(sources) {
  $("#meeting-sources").innerHTML = sources.length
    ? sources
        .map(
          (source) =>
            `<div class="list-row"><div class="list-row-main"><span class="file-icon"><svg class="icon"><use href="#i-mic"/></svg></span><div><strong>فایل ${source.position + 1}</strong><small>ترتیب جلسه ${
              source.voice_sequence_snapshot ?? "—"
            } · شروع ${formatTime(source.source_offset_ms)} · مدت ${formatTime(
              source.source_duration_ms
            )}</small></div></div></div>`
        )
        .join("")
    : "";
}

function renderTranscript(transcript) {
  const content = $("#meeting-transcript-content");
  content.replaceChildren();
  const segments = transcript.segments || [];
  if (!segments.length) {
    content.textContent = transcript.text || "متنی برای نمایش وجود ندارد.";
    return;
  }
  content.classList.add("speaker-transcript");
  for (const segment of segments) {
    const turn = document.createElement("section");
    turn.className = "speaker-turn";
    const heading = document.createElement("header");
    const speaker = document.createElement("strong");
    speaker.textContent = segment.speaker_display_name || segment.speaker_label || "—";
    const time = document.createElement("span");
    time.textContent = `${formatTime(segment.start_ms)} تا ${formatTime(segment.end_ms)}`;
    const text = document.createElement("p");
    text.textContent = segment.text || "—";
    heading.append(speaker, time);
    turn.append(heading, text);
    content.append(turn);
  }
}

function populateVersionSelect() {
  const select = $("#version-select");
  select.innerHTML = versions
    .map(
      (version, index) =>
        `<option value="${escapeHtml(version.id)}">نسخه ${versions.length - index} - ${escapeHtml(
          formatDate(version.generated_at, true)
        )} ${version.completed_at ? "" : "(در حال پردازش)"}</option>`
    )
    .join("");
  select.value = selectedId;
}

async function loadSelectedVersion() {
  stopPolling();
  const detail = await api(`/meeting-results/${encodeURIComponent(selectedId)}`);
  renderSources(detail.sources || []);
  $("#meeting-transcript-meta").textContent = `${detail.sources.length} فایل صوتی · نسخه ${detail.schema_version} · تولید در ${formatDate(
    detail.generated_at,
    true
  )}`;

  if (!detail.completed_at) {
    renderStatusPill("running");
    $("#meeting-transcript-content").textContent = "ترکیب متن جلسه هنوز آماده نیست…";
    renderProcessingError(null);
    pollTimer = setTimeout(() => loadSelectedVersion().catch((exception) => toast(exception.message, "error")), 3000);
    return;
  }

  try {
    const transcript = await api(`/meeting-results/${encodeURIComponent(selectedId)}/transcript`);
    renderStatusPill(transcript.processing_status || "succeeded");
    renderProcessingError(transcript.processing_error);
    renderTranscript(transcript);
  } catch (exception) {
    renderStatusPill("failed");
    $("#meeting-transcript-content").textContent = "متن ترکیبی هنوز آماده نیست یا دسترسی به آن ممکن نیست.";
    toast(exception.message, "error");
  }
}

function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
}

async function loadVersions() {
  versions = await api(`/meetings/${encodeURIComponent(meetingId)}/results`);
  const button = $("#recompose-button");
  button.classList.remove("hidden");
  $("#recompose-button-label").textContent = versions.length ? "ترکیب مجدد" : "شروع ترکیب";
  if (!versions.length) {
    $("#version-select").innerHTML = "";
    $("#meeting-sources").innerHTML = "";
    $("#meeting-transcript-content").textContent = "هنوز متن ترکیبی برای این جلسه ساخته نشده است.";
    renderStatusPill("");
    return;
  }
  selectedId = versions[0].id;
  populateVersionSelect();
  await loadSelectedVersion();
}

function attachmentIcon(contentType) {
  return contentType === "application/pdf" ? "file-pdf" : "image";
}

function renderAttachments(attachments, publication) {
  const locked = ["queued", "running"].includes(publication.status);
  $("#attachment-input").disabled = locked;
  $("#attachment-list").innerHTML = attachments.length
    ? attachments.map((attachment) => `<div class="list-row"><div class="list-row-main"><span class="file-icon"><svg class="icon"><use href="#i-${attachmentIcon(attachment.content_type)}"/></svg></span><div><strong>${escapeHtml(attachment.original_filename)}</strong><small>${escapeHtml(attachment.content_type)} · ${(attachment.size_bytes / 1048576).toFixed(2)} مگابایت</small></div></div><button class="delete-icon" type="button" aria-label="حذف ${escapeHtml(attachment.original_filename)}" data-delete-attachment="${attachment.id}" ${locked ? "disabled" : ""}><svg class="icon"><use href="#i-trash"/></svg></button></div>`).join("")
    : `<div class="empty-state"><svg class="icon"><use href="#i-paperclip"/></svg><p>پیوستی برای این جلسه ثبت نشده است.</p></div>`;
}

function renderPublication(publication) {
  const status = publication.status || "awaiting_approval";
  const active = ["queued", "running"].includes(status);
  $("#publication-status").textContent = PUBLICATION_STATUS_LABELS[status] || status;
  $("#publication-status").dataset.status = status;
  $("#destination-path").value = publication.destination_path || "پروژه‌های کارآموزی/ASR test";
  $("#destination-path").disabled = active;
  const button = $("#approve-publication");
  button.disabled = active;
  $("#approve-publication-label").textContent = active ? "در حال انجام…" : status === "published" ? "تأیید و به‌روزرسانی دوباره" : status === "failed" ? "تلاش مجدد" : "تأیید نهایی و ارسال";

  const error = $("#publication-error");
  if (status === "failed") {
    error.classList.remove("hidden");
    $("#publication-error-code").textContent = publication.error_code || "publication_failed";
    $("#publication-error-message").textContent = publication.error_message || "علت خطا ثبت نشده است.";
  } else {
    error.classList.add("hidden");
  }
  const vision = $("#vision-note");
  if (publication.vision_status === "unsupported") {
    vision.textContent = "پردازش چندوجهی فعلاً ممکن نیست؛ تصاویر در تولید خلاصه نادیده گرفته شدند، اما در سند خلاصه قرار گرفتند.";
    vision.classList.remove("hidden");
  } else if (publication.vision_status === "used") {
    vision.textContent = "تصاویر در تولید خلاصه استفاده شدند.";
    vision.classList.remove("hidden");
  } else {
    vision.classList.add("hidden");
  }
}

async function loadPublication() {
  if (publicationPollTimer) clearTimeout(publicationPollTimer);
  publicationPollTimer = null;
  const [publication, attachments] = await Promise.all([
    api(`/meetings/${encodeURIComponent(meetingId)}/publication`),
    api(`/meetings/${encodeURIComponent(meetingId)}/attachments`),
  ]);
  renderPublication(publication);
  renderAttachments(attachments, publication);
  if (["queued", "running"].includes(publication.status)) {
    publicationPollTimer = setTimeout(() => loadPublication().catch((exception) => toast(exception.message, "error")), 3000);
  }
}

const user = await requireUser();
if (user) {
  renderSidebar(user);
  Promise.all([loadVersions(), loadPublication()]).catch((exception) => toast(exception.message, "error"));
}

$("#version-select").addEventListener("change", (event) => {
  selectedId = event.target.value;
  loadSelectedVersion().catch((exception) => toast(exception.message, "error"));
});

$("#recompose-button").addEventListener("click", async () => {
  try {
    await api(`/meetings/${encodeURIComponent(meetingId)}/compose`, {
      method: "POST",
      body: JSON.stringify({ force_new_version: versions.length > 0 }),
    });
    toast("درخواست ترکیب ثبت شد.");
    await loadVersions();
  } catch (exception) {
    toast(exception.message, "error");
  }
});

$("#attachment-input").addEventListener("change", async (event) => {
  const files = [...event.target.files];
  if (!files.length) return;
  const progress = $("#attachment-progress");
  progress.classList.remove("hidden");
  try {
    for (const file of files) {
      const data = new FormData();
      data.append("upload", file);
      await api(`/meetings/${encodeURIComponent(meetingId)}/attachments`, { method: "POST", body: data });
    }
    toast("پیوست‌ها ذخیره شدند و برای تأیید بعدی آماده‌اند.");
    await loadPublication();
  } catch (exception) {
    toast(exception.message, "error");
  } finally {
    progress.classList.add("hidden");
    event.target.value = "";
  }
});

$("#attachment-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-attachment]");
  if (!button || !confirm("این پیوست حذف شود؟")) return;
  try {
    await api(`/meetings/${encodeURIComponent(meetingId)}/attachments/${encodeURIComponent(button.dataset.deleteAttachment)}`, { method: "DELETE" });
    toast("پیوست حذف شد.");
    await loadPublication();
  } catch (exception) {
    toast(exception.message, "error");
  }
});

$("#publication-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#approve-publication");
  button.disabled = true;
  try {
    await api(`/meetings/${encodeURIComponent(meetingId)}/publication/approve`, {
      method: "POST",
      body: JSON.stringify({ destination_path: $("#destination-path").value.trim() }),
    });
    toast("تأیید ثبت شد؛ خلاصه‌سازی و انتشار در صف قرار گرفت.");
    await loadPublication();
  } catch (exception) {
    toast(exception.message, "error");
    $("#publication-error").classList.remove("hidden");
    $("#publication-error-code").textContent = "approval_failed";
    $("#publication-error-message").textContent = exception.message;
    $("#publication-error").focus();
  } finally {
    button.disabled = false;
  }
});
