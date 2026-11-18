// 画面の小さな動き（docs/DASHBOARD.md 7-5・7-6）。データは読まない・決めない。
(function () {
  // 依頼者を名前で探す欄の絞り込み（英字は大文字・小文字を問わない）。Enter で、いちばん上に残った依頼者を開く
  function visible(input) {
    return Array.prototype.filter.call(document.querySelectorAll(input.getAttribute("data-filter")),
      function (a) { return a.style.display !== "none"; });
  }
  document.addEventListener("input", function (e) {
    if (!e.target.matches("[data-filter]")) return;
    var q = e.target.value.trim().toLowerCase();
    document.querySelectorAll(e.target.getAttribute("data-filter")).forEach(function (a) {
      a.style.display = a.textContent.toLowerCase().indexOf(q) >= 0 ? "" : "none";
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || !e.target.matches("[data-filter]")) return;
    var first = visible(e.target)[0];
    if (first) { e.preventDefault(); location.href = first.getAttribute("href"); }
  });
  // 探す欄を開いたら、すぐ打てるようにする
  document.addEventListener("toggle", function (e) {
    if (e.target.matches && e.target.matches(".finder") && e.target.open) {
      var i = e.target.querySelector("input"); if (i) i.focus();
    }
  }, true);
  // 依頼者のタブが横に入りきらないとき、いま開いている依頼者のタブを見える所へ送る
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-scroll-current]").forEach(function (s) {
      var cur = s.querySelector("[aria-current=page]");
      if (cur) s.scrollLeft = cur.offsetLeft - s.clientWidth / 2 + cur.clientWidth / 2;
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
  // 選ぶとすぐ絞る（ボタンを押さなくてよい）
  document.addEventListener("change", function (e) {
    if (e.target.matches("[data-autosubmit]")) e.target.form.submit();
  });
  // 送っている間は二度押しできない
  document.addEventListener("submit", function (e) {
    if ((e.target.method || "").toLowerCase() !== "post") return;
    e.target.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = true; b.textContent = "送っています…"; });
  });
})();
