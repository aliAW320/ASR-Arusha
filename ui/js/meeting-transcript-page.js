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

let pollTimer = null;
let versions = [];
let selectedId = null;

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
            `<div class="list-row"><div class="list-row-main"><span class="file-icon">♫</span><div><strong>فایل ${source.position + 1}</strong><small>ترتیب جلسه ${
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
  button.textContent = versions.length ? "ترکیب مجدد" : "شروع ترکیب";
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

const user = await requireUser();
if (user) {
  renderSidebar(user);
  loadVersions().catch((exception) => toast(exception.message, "error"));
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
