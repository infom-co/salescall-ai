// 画面の小さな動き（docs/DASHBOARD.md 7-5・7-6）。データは読まない・決めない。
(function () {
  // 上に留まる飾り（上の帯とタブ）の高さを1か所で測り、--chrome に入れる（タブが1段か2段か・スマホで折り返すかで変わる）。
  // 上に留まるほかのもの（まとめての「かけない」の帯・飛び先の余白）は、この高さの下に置く（重ねて隠さない）。
  function measure() {
    var h = 0;
    document.querySelectorAll(".top, .tabs").forEach(function (el) {
      if (getComputedStyle(el).position === "sticky") h += el.getBoundingClientRect().height;
    });
    document.documentElement.style.setProperty("--chrome", Math.round(h) + "px");
  }
  window.addEventListener("resize", measure);
  document.addEventListener("DOMContentLoaded", measure);
  if (window.ResizeObserver) document.addEventListener("DOMContentLoaded", function () {
    var ro = new ResizeObserver(measure);
    document.querySelectorAll(".top, .tabs").forEach(function (el) { ro.observe(el); });
  });
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
    if (first && first.getAttribute("href")) { e.preventDefault(); location.href = first.getAttribute("href"); }
  });
  // 窓（探す・＋ 依頼者・＋ 業務・＋ 実施回）は1つずつ：1つ開いたら、ほかは閉じる
  document.addEventListener("toggle", function (e) {
    if (!e.target.matches || !e.target.matches(".finder, .adder") || !e.target.open) return;
    document.querySelectorAll(".finder[open], .adder[open]").forEach(function (d) { if (d !== e.target) d.open = false; });
    var first = e.target.querySelector("input[type=text]"); if (first) first.focus();
  }, true);
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
  // 理由が要る操作：理由が空のあいだはボタンを押せない（サーバーも断る）。押せない理由を、ボタンのすぐ横に出す（どの画面も同じ1か所）
  function reasonState(form) {
    var btn = form.querySelector("[data-submit]");
    var box = form.querySelector("[name=reason]");
    if (!btn || !box) return;
    var empty = !box.value.trim();
    btn.disabled = empty;
    var hint = form.querySelector(".why-disabled");
    if (!hint) {
      hint = document.createElement("span");
      hint.className = "why-disabled";
      hint.textContent = "理由を書くと押せます";
      btn.insertAdjacentElement("afterend", hint);
    }
    hint.hidden = !empty;
  }
  document.addEventListener("input", function (e) {
    var form = e.target.closest && e.target.closest("form[data-needs-reason]");
    if (form) reasonState(form);
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-needs-reason]").forEach(reasonState);
    // 狭い画面では、表の1行を「見出し：値」の札に並べ替える（CSS）。見出しは表の見出しから付ける（画面ごとに書かない）
    document.querySelectorAll("table.list").forEach(function (table) {
      var heads = Array.prototype.map.call(table.querySelectorAll("thead th"), function (th) { return th.textContent.trim(); });
      table.querySelectorAll("tbody tr").forEach(function (tr) {
        Array.prototype.forEach.call(tr.children, function (td, i) { if (heads[i]) td.setAttribute("data-label", heads[i]); });
      });
    });
  });
  // 選ぶとすぐ絞る（ボタンを押さなくてよい）
  document.addEventListener("change", function (e) {
    if (e.target.matches("[data-autosubmit]")) e.target.form.submit();
  });
  // 直す欄が名前の欄のときだけ、読みの欄を出す（読みを持つ欄は core の決まり＝ひな形に渡した一覧）
  function readingBox(sel) {
    var box = sel.form && sel.form.querySelector(".reading-box");
    if (!box) return;
    var names = (sel.getAttribute("data-reading-fields") || "").split(" ");
    box.hidden = names.indexOf(sel.value) < 0;
  }
  document.addEventListener("change", function (e) { if (e.target.matches("[data-reading-fields]")) readingBox(e.target); });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-reading-fields]").forEach(readingBox);
  });
  // レ点：すべて選ぶ・選んだ数（選んでいないあいだは一括のボタンを押せない。理由が空でも押せない）
  function bulkState(form) {
    var n = form.querySelectorAll("input[name=task_id]:checked").length;
    var c = form.querySelector("[data-checked-count]"); if (c) c.textContent = n;
    var btn = form.querySelector("[data-submit]");
    var reason = form.querySelector("[name=reason]");
    if (btn) btn.disabled = n === 0 || !(reason && reason.value.trim());
  }
  document.addEventListener("change", function (e) {
    var form = e.target.closest && e.target.closest(".bulk-form");
    if (!form) return;
    if (e.target.matches("[data-check-all]")) {
      form.querySelectorAll("input[name=task_id]").forEach(function (b) { b.checked = e.target.checked; });
    }
    bulkState(form);
  });
  document.addEventListener("input", function (e) {
    var form = e.target.closest && e.target.closest(".bulk-form");
    if (form) bulkState(form);
  });
  // 送っている間は二度押しできない。
  // ボタンを止めるのは、ブラウザが送る中身を作り終えたあと（次の順番）にする。
  // 先に止めると、押したボタンの名前と値（例：許可する／断る）が送る中身から落ちてしまう。
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if ((form.method || "").toLowerCase() !== "post" || form.hasAttribute("data-download")) return;   // 書き出しは画面が替わらない
    if (form.dataset.sending) { e.preventDefault(); return; }
    form.dataset.sending = "1";
    setTimeout(function () {
      form.querySelectorAll("button[type=submit]").forEach(function (b) {
        b.dataset.label = b.textContent; b.disabled = true; b.textContent = "送っています…";
      });
    }, 0);
  });
  // 「戻る」で前の画面に戻ったときは、ボタンを元に戻す
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-sending]").forEach(function (form) {
      delete form.dataset.sending;
      form.querySelectorAll("button[type=submit]").forEach(function (b) {
        b.disabled = false; if (b.dataset.label) b.textContent = b.dataset.label;
      });
    });
  });
})();

// 表の各升に、その列の見出しの名前を付ける（表の部品の1か所。2026-11-20 の見直し：狭い画面で表を「見出し：値」の札に並べ替えるとき、
// 見出しの名前が無くて数字だけが並び、何の数か分からなかった）。どの表にも自動で付くので、画面ごとに書かない。
(function () {
  function label(root) {
    (root || document).querySelectorAll("table.list").forEach(function (table) {
      var heads = [];
      table.querySelectorAll("thead th").forEach(function (th) {
        var span = parseInt(th.getAttribute("colspan") || "1", 10);
        for (var i = 0; i < span; i++) heads.push(th.textContent.trim());
      });
      if (!heads.length) return;
      table.querySelectorAll("tbody tr").forEach(function (tr) {
        var col = 0;
        Array.prototype.forEach.call(tr.children, function (td) {
          if (td.tagName === "TD" && !td.hasAttribute("data-label") && heads[col]) td.setAttribute("data-label", heads[col]);
          col += parseInt(td.getAttribute("colspan") || "1", 10);
        });
      });
    });
  }
  document.addEventListener("DOMContentLoaded", function () { label(document); });
  document.addEventListener("htmx:afterSwap", function (e) { label(e.target); });
})();
