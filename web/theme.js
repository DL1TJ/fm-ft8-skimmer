/* theme: follows the system setting unless the visitor chose one (localStorage "theme") */
(function () {
  var pref = null; try { pref = localStorage.getItem("theme"); } catch (e) {}
  if (pref === "light" || pref === "dark") document.documentElement.dataset.theme = pref;
  window.cycleTheme = function () {
    var cur = document.documentElement.dataset.theme || "auto";
    var next = cur === "auto" ? "light" : cur === "light" ? "dark" : "auto";
    if (next === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
    try { next === "auto" ? localStorage.removeItem("theme") : localStorage.setItem("theme", next); } catch (e) {}
    window.renderThemeBtn(); window.dispatchEvent(new Event("themechange"));
  };
  window.renderThemeBtn = function () {
    var b = document.getElementById("theme-btn"); if (!b) return;
    var cur = document.documentElement.dataset.theme || "auto";
    var sys = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    b.textContent = cur === "auto" ? "\u25d0 auto (" + sys + ")" : cur === "light" ? "\u2600 light" : "\u263e dark";
    b.title = "Theme: " + (cur === "auto" ? "follows your system setting" : cur) + " \u2014 click to change";
  };
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", function () { window.renderThemeBtn(); window.dispatchEvent(new Event("themechange")); });
  document.addEventListener("DOMContentLoaded", function () {
    var b = document.getElementById("theme-btn"); if (b) b.addEventListener("click", window.cycleTheme);
    window.renderThemeBtn();
  });
})();
