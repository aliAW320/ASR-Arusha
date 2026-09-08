const sprite = `
<svg class="icon-sprite" aria-hidden="true" focusable="false">
  <symbol id="i-menu" viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></symbol>
  <symbol id="i-meetings" viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="16" rx="2.5"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/></symbol>
  <symbol id="i-history" viewBox="0 0 24 24"><path d="M12 7v5l3.5 2M20 12a8 8 0 1 1-2.34-5.66M20 5v4h-4"/></symbol>
  <symbol id="i-logout" viewBox="0 0 24 24"><path d="M10 21H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
  <symbol id="i-mic" viewBox="0 0 24 24"><rect x="8.5" y="3" width="7" height="12" rx="3.5"/><path d="M5 12a7 7 0 0 0 14 0M12 19v3M8.5 22h7"/></symbol>
  <symbol id="i-upload" viewBox="0 0 24 24"><path d="M12 16V4M7.5 8.5 12 4l4.5 4.5M5 17v2a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-2"/></symbol>
  <symbol id="i-users" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M3 21c.5-4 2.8-6.2 6-6.2s5.5 2.2 6 6M16 5.4a3.5 3.5 0 0 1 0 6.2M17 15c2.5.5 4 2.5 4.5 6"/></symbol>
  <symbol id="i-edit" viewBox="0 0 24 24"><path d="m14.5 4.5 5 5L8 21H3v-5zM12.5 6.5l5 5"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M6.5 7l1 14h9l1-14M10 11v6M14 11v6"/></symbol>
  <symbol id="i-arrow-forward" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></symbol>
  <symbol id="i-arrow-next" viewBox="0 0 24 24"><path d="M19 12H5M11 6l-6 6 6 6"/></symbol>
  <symbol id="i-document" viewBox="0 0 24 24"><path d="M7 3h7l5 5v13H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></symbol>
  <symbol id="i-image" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="8.5" cy="9" r="1.5"/><path d="m4 18 5-5 3.5 3.5L17 12l4 4"/></symbol>
  <symbol id="i-file-pdf" viewBox="0 0 24 24"><path d="M7 3h7l5 5v13H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2ZM14 3v5h5"/><path d="M8 17v-5h2a1.6 1.6 0 0 1 0 3.2H8M13 12v5h1a2.5 2.5 0 0 0 0-5h-1M18 17v-5h3M18 14h2.5"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 6.5"/></symbol>
  <symbol id="i-alert-circle" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7.5V13M12 16.5v.1"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M20 8V4M20 8h-4M20 8a9 9 0 1 0 1 7"/></symbol>
  <symbol id="i-x" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></symbol>
  <symbol id="i-paperclip" viewBox="0 0 24 24"><path d="m15.5 8.5-6 6a2.5 2.5 0 0 0 3.5 3.5l6.5-6.5a4.5 4.5 0 0 0-6.4-6.4l-7 7a6 6 0 0 0 8.5 8.5"/></symbol>
  <symbol id="i-inbox" viewBox="0 0 24 24"><path d="m4 14 3-9h10l3 9M4 14h5l1.2 2.5h3.6L15 14h5v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z"/></symbol>
  <symbol id="i-wave" viewBox="0 0 24 24"><path d="M3 12h2l2-6 3 12 3-14 3 12 2-4h3"/></symbol>
</svg>`;

export function mountIconSprite() {
  if (!document.querySelector(".icon-sprite")) document.body.insertAdjacentHTML("afterbegin", sprite);
}

export function icon(name, className = "icon") {
  return `<svg class="${className}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
}
