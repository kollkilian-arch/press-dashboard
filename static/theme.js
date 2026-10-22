(function () {
  'use strict';

  const key = 'pressDashboard.theme';
  const root = document.documentElement;
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = null;

  try {
    const saved = localStorage.getItem(key);
    if (saved === 'light' || saved === 'dark') preference = saved;
  } catch (error) {
    // Switching still works when browser storage is unavailable.
  }

  function applyTheme() {
    const dark = (preference || (systemTheme.matches ? 'dark' : 'light')) === 'dark';
    root.dataset.bsTheme = dark ? 'dark' : 'light';
    document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
      const label = dark ? 'Hellen Modus aktivieren' : 'Dunklen Modus aktivieren';
      button.setAttribute('aria-label', label);
      button.setAttribute('title', label);
      button.setAttribute('aria-pressed', String(dark));
      const icon = button.querySelector('i');
      if (icon) icon.className = dark ? 'bi bi-sun' : 'bi bi-moon-stars';
    });
    window.dispatchEvent(new CustomEvent('themechange'));
  }

  // Run before styles load so navigation never flashes the wrong theme.
  applyTheme();

  document.addEventListener('DOMContentLoaded', applyTheme);
  document.addEventListener('click', function (event) {
    if (!event.target.closest('[data-theme-toggle]')) return;
    preference = root.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    try {
      localStorage.setItem(key, preference);
    } catch (error) {}
    applyTheme();
  });

  systemTheme.addEventListener('change', function () {
    if (!preference) applyTheme();
  });

  // Storage events also keep open article iframes and other tabs in sync.
  window.addEventListener('storage', function (event) {
    if (event.key !== key && event.key !== null) return;
    preference = event.newValue === 'dark' || event.newValue === 'light' ? event.newValue : null;
    applyTheme();
  });
})();
