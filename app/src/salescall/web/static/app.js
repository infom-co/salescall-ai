// 画面の小さな動き（docs/DASHBOARD.md 7-5・7-6）。データは読まない・決めない。
(function () {
  // 依頼者を選ぶ欄の絞り込み
  document.addEventListener("input", function (e) {
    if (!e.target.matches("[data-filter]")) return;
    var q = e.target.value.trim();
    document.querySelectorAll(e.target.getAttribute("data-filter")).forEach(function (a) {
      a.style.display = a.textContent.indexOf(q) >= 0 ? "" : "none";
    });
  });
  // 確かめの窓を開く・閉じる（取り消せない操作は、窓で「何に効くか」を見せてから、もう一度押して決める）
  document.addEventListener("click", function (e) {
    var open = e.target.closest("[data-open]");
    if (open) { var d = document.getElementById(open.getAttribute("data-open")); if (d) d.showModal(); }
    var close = e.target.closest("[data-close]");
    if (close) { var c = close.closest("dialog"); if (c) c.close(); }
    if (e.target.closest(".menu-toggle")) document.body.classList.toggle("nav-open");
  });
  // 理由が要る操作：理由が空のあいだはボタンを押せない（サーバーも断る）
  document.addEventListener("input", function (e) {
    var form = e.target.closest("form[data-needs-reason]");
    if (!form) return;
    var btn = form.querySelector("[data-submit]");
    var empty = !form.querySelector("[name=reason]").value.trim();
    btn.disabled = empty;
  });
  // 送っている間は二度押しできない
  document.addEventListener("submit", function (e) {
    e.target.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = true; b.textContent = "送っています…"; });
  });
})();
