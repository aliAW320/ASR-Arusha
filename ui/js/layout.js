import { api, clearSession, getStoredUser, getToken } from "./api.js?v=20260912a";
import { icon, mountIconSprite } from "./icons.js?v=20260912a";

mountIconSprite();

export const $ = (selector, root = document) => root.querySelector(selector);
export const faNumber = (value) => Number(value || 0).toLocaleString("fa-IR");
export const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
export const formatDate = (value, withTime = false) => new Intl.DateTimeFormat("fa-IR", { dateStyle: "medium", timeStyle: withTime ? "short" : undefined }).format(new Date(value));

export function toast(message, type = "success") {
  const element = $("#toast");
  if (!element) return;
  element.textContent = message;
  element.className = `toast show ${type === "error" ? "error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.className = "toast"; }, 3200);
}

export async function requireUser({ admin = false } = {}) {
  if (!getToken()) { location.replace("/login.html"); return null; }
  try {
    const user = await api("/auth/me");
    sessionStorage.setItem("meeting_user", JSON.stringify(user));
    if (admin && user.role !== "admin") { location.replace("/meetings.html"); return null; }
    return user;
  } catch (_) { return null; }
}

export function renderSidebar(user = getStoredUser(), active = "meetings") {
  const sidebar = $("[data-sidebar]");
  if (!sidebar || !user) return;
  const name = user.full_name || user.email.split("@")[0];
  sidebar.innerHTML = `<div class="brand"><div class="brand-mark">هـ</div><div><strong>هم‌نشین</strong><small>هوشمندی جلسه‌ها</small></div></div><div class="signal-rail" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div><nav aria-label="ناوبری اصلی"><a class="nav-item ${active === "meetings" ? "active" : ""}" ${active === "meetings" ? 'aria-current="page"' : ""} href="/meetings.html">${icon("meetings")}<span>جلسه‌ها</span></a>${user.role === "admin" ? `<a class="nav-item ${active === "history" ? "active" : ""}" ${active === "history" ? 'aria-current="page"' : ""} href="/history.html">${icon("history")}<span>تاریخچه سامانه</span></a>` : ""}</nav><div class="sidebar-foot"><div class="user-chip"><span>${escapeHtml(name.slice(0, 1))}</span><div><strong>${escapeHtml(name)}</strong><small>${escapeHtml(user.email)}</small></div></div><button id="logout-button" class="icon-button" title="خروج" aria-label="خروج از حساب">${icon("logout")}</button></div>`;
  $("#logout-button").addEventListener("click", () => { clearSession(); location.replace("/login.html"); });
  const menuButton = $("[data-mobile-menu]");
  const overlay = $("[data-sidebar-overlay]");
  const setMenu = (open) => {
    sidebar.classList.toggle("open", open);
    overlay?.classList.toggle("visible", open);
    menuButton?.setAttribute("aria-expanded", String(open));
  };
  menuButton?.addEventListener("click", () => setMenu(!sidebar.classList.contains("open")));
  overlay?.addEventListener("click", () => setMenu(false));
}
