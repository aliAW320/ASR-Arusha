import { api, getToken, saveSession } from "./api.js";

if (getToken()) location.replace("/meetings.html");
const form = document.querySelector("#auth-form");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button[type=submit]");
  const error = document.querySelector("#auth-error");
  button.disabled = true;
  error.textContent = "";
  try {
    const payload = Object.fromEntries(new FormData(form).entries());
    if (!payload.full_name) delete payload.full_name;
    const result = await api(`/auth/${form.dataset.mode}`, { method: "POST", body: JSON.stringify(payload) });
    saveSession(result);
    location.replace("/meetings.html");
  } catch (exception) { error.textContent = exception.message; }
  finally { button.disabled = false; }
});
