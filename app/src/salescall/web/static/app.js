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
      var hit = a.textContent.toLowerCase().indexOf(q) >= 0;
      a.style.display = hit ? "" : "none";
      // 探したものが、たたんだ所（くわしい設定など）の中にあれば開く（探しても見えない、を作らない）
      if (hit && q) for (var d = a.parentElement; d; d = d.parentElement) if (d.tagName === "DETAILS" && !d.classList.contains("setting")) d.open = true;
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
  // 飛び先（#…）が閉じた窓（details）なら開く（例：承認の札の「違う（直す）」→ 行の画面の「結果を直す」）。同じ画面の中の飛び先も同じ
  function openTarget() {
    var id = decodeURIComponent(location.hash.slice(1));
    var el = id && document.getElementById(id);
    if (!el) return;
    for (var d = el; d; d = d.parentElement) if (d.tagName === "DETAILS") d.open = true;
    el.scrollIntoView({ block: "start" });
  }
  document.addEventListener("DOMContentLoaded", openTarget);
  window.addEventListener("hashchange", openTarget);
  // ファイルを選ぶ部品（parts.file_pick）：選んだファイルの名前を出す。枠の上にファイルを置いても選べる
  function showFile(input) {
    var label = input.closest(".drop"), out = label && label.querySelector("[data-file-name]");
    if (!out) return;
    var f = input.files && input.files[0];
    out.textContent = f ? "選んだファイル：" + f.name + "（" + Math.max(1, Math.round(f.size / 1024)) + " KB）" : "まだ選んでいません";
    out.classList.toggle("chosen", !!f);
  }
  document.addEventListener("change", function (e) { if (e.target.matches(".drop input[type=file]")) showFile(e.target); });
  ["dragenter", "dragover"].forEach(function (t) {
    document.addEventListener(t, function (e) {
      var d = e.target.closest && e.target.closest(".drop"); if (!d) return;
      e.preventDefault();
      if (!d.querySelector("input[type=file]").disabled) d.classList.add("dragging");   // 押せない形の欄（取り込めないとき）は受けない
    });
  });
  ["dragleave", "drop"].forEach(function (t) {
    document.addEventListener(t, function (e) {
      var d = e.target.closest && e.target.closest(".drop"); if (!d) return;
      e.preventDefault(); d.classList.remove("dragging");
      if (t === "drop" && e.dataTransfer && e.dataTransfer.files.length) {
        var input = d.querySelector("input[type=file]"); if (input.disabled) return;
        input.files = e.dataTransfer.files; showFile(input);
      }
    });
  });
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
  // 押せるか（1か所）：理由が要る操作・まとめての操作で、足りないもの（お客様を選ぶ・どうするかを選ぶ・理由を書く）をまとめて数え、
  // 足りないあいだはボタンを押せない。足りないものを、ボタンのすぐ横にすべて出す（前は理由だけを見ていて、行を選んでいなくても
  // 理由を書くと「押せます」の案内が消えた＝2026-11-20 の見直し）。サーバーも同じものを断る
  function missingOf(form) {
    var out = [];
    if (form.classList.contains("bulk-form") && form.querySelectorAll("input[name=task_id]:checked").length === 0) out.push("お客様を選ぶ");
    Array.prototype.forEach.call(form.querySelectorAll("select[required]"), function (sel) { if (!sel.value) out.push("どうするかを選ぶ"); });
    var box = form.querySelector("[name=reason]");
    if (box && !box.value.trim()) out.push("理由を書く");
    return out;
  }
  function submitState(form) {
    var btn = form.querySelector("[data-submit]");
    if (!btn) return;
    var picked = form.querySelectorAll("input[type=checkbox][name=task_id]:checked").length;
    var c = form.querySelector("[data-checked-count]"); if (c) c.textContent = picked;
    // まとめて変える欄は、お客様を選んでから開く（選ぶまでは案内の一文だけ。CSS の1か所が見せ方を決める）
    if (form.classList.contains("bulk-form")) { form.classList.add("js-ready"); form.classList.toggle("has-checked", picked > 0); }
    var missing = missingOf(form);
    btn.disabled = missing.length > 0;
    var hint = form.querySelector(".why-disabled");
    if (!hint) {
      hint = document.createElement("span");
      hint.className = "why-disabled";
      btn.insertAdjacentElement("afterend", hint);
    }
    hint.textContent = missing.length ? "押せるまでに：" + missing.join("・") : "";
    hint.hidden = !missing.length;
  }
  function stateful(t) { return t.closest && (t.closest("form[data-needs-reason]") || t.closest(".bulk-form")); }
  ["input", "change"].forEach(function (k) {
    document.addEventListener(k, function (e) {
      var form = stateful(e.target);
      if (!form) return;
      // 「すべて選ぶ」は、いま見えているお客様だけ（「探す」で絞り込んで隠れた行は選ばない＝隠れたお客様にうっかり印を付けない）。
      // すべて選ぶの欄が2つ（表の見出し・狭い画面の帯）あっても、同じ1つの状態にそろえる
      // 印の箱だけを見る（お客様1件の画面の「かけるかを変える」は、隠れた欄でお客様を渡すので、表の行を持たない）
      var boxes = Array.prototype.filter.call(form.querySelectorAll("input[type=checkbox][name=task_id]"), function (b) { var tr = b.closest("tr"); return !tr || tr.style.display !== "none"; });
      if (e.target.matches("[data-check-all]")) {
        if (k === "change") {
          boxes.forEach(function (b) { b.checked = e.target.checked; });
          form.querySelectorAll("[data-check-all]").forEach(function (c) { c.checked = e.target.checked; });
        }
      } else if (e.target.matches("input[type=checkbox][name=task_id]")) {   // お客様の印を押したときだけ、すべて選ぶの印をそろえ直す
        var all = boxes.length > 0 && boxes.every(function (b) { return b.checked; });
        form.querySelectorAll("[data-check-all]").forEach(function (c) { c.checked = all; });
      }
      submitState(form);
    });
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-needs-reason], .bulk-form").forEach(submitState);
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
  // 送っている間は二度押しできない。
  // ボタンを止めるのは、ブラウザが送る中身を作り終えたあと（次の順番）にする。
  // 先に止めると、押したボタンの名前と値（例：許可する／断る）が送る中身から落ちてしまう。
  // 取り消せない操作の確かめ（data-confirm の付いた送信は、すべてこの1か所で確かめの窓を通す。計画書 5-1「名前つきで見せてから」）。
  // 文の {n} は選んだお客様の数、{select:名前} はその選ぶ欄でいま選んでいる言葉（送る直前の値で書く）
  var pendingConfirm = null;
  function confirmText(form) {
    return form.getAttribute("data-confirm")
      .replace(/\{n\}/g, String(form.querySelectorAll("input[name=task_id]:checked").length))
      .replace(/\{select:([a-z_]+)\}/g, function (_, name) {
        var sel = form.querySelector("select[name=" + name + "]");
        // 選ぶ欄の言葉に「（いま）」などの印を添えた項目は、印のない言葉を data-word に持つ（文には言葉だけを書く）
        var opt = sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex] : null;
        return opt ? (opt.getAttribute("data-word") || opt.text).trim() : "";
      });
  }
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches("form[data-confirm]") || form.getAttribute("data-confirmed") === "1") return;
    var d = document.getElementById("confirm-dialog");
    if (!d) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    pendingConfirm = { form: form, submitter: e.submitter || null };
    d.querySelector("[data-confirm-text]").textContent = confirmText(form);
    // 題は、決めるボタンの言葉から（何の操作かを題で言う）。取り消せない・電話を止める側の操作（data-confirm-tone="danger"）は、
    // 決めるボタンも赤（前の段の赤いボタンと同じ色。2026-11-21 の見直し：最後の一押しだけ、ふつうの決定の藍になっていた）
    var ok = form.getAttribute("data-confirm-ok") || "決める";
    var danger = form.getAttribute("data-confirm-tone") === "danger";
    d.querySelector("#confirm-title").textContent = "「" + ok + "」の前に、確かめてください";
    var btn = d.querySelector("[data-confirm-ok]");
    btn.textContent = ok;
    btn.classList.toggle("danger", danger);
    btn.classList.toggle("main", !danger);
    d.showModal();
  }, true);
  document.addEventListener("click", function (e) {
    if (!e.target.closest("[data-confirm-ok]") || !pendingConfirm) return;
    var p = pendingConfirm; pendingConfirm = null;
    document.getElementById("confirm-dialog").close();
    p.form.setAttribute("data-confirmed", "1");
    if (p.submitter && p.form.requestSubmit) p.form.requestSubmit(p.submitter); else if (p.form.requestSubmit) p.form.requestSubmit(); else p.form.submit();
    setTimeout(function () { p.form.removeAttribute("data-confirmed"); }, 1000);   // 書き出し（画面が移らない送信）は、次も確かめる
  });
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

// 表の右にまだ欄があるか（表の部品の1か所）。あれば .more-right（CSS が右の端をぼかす）。開いたとき・横に送ったとき・幅が変わったときに見直す
(function () {
  function edge(w) { w.classList.toggle("more-right", w.scrollWidth - w.clientWidth - w.scrollLeft > 2); }
  function all() { document.querySelectorAll(".table-scroll").forEach(edge); }
  document.addEventListener("scroll", function (e) { if (e.target.classList && e.target.classList.contains("table-scroll")) edge(e.target); }, true);
  window.addEventListener("resize", all);
  document.addEventListener("DOMContentLoaded", all);
  document.addEventListener("htmx:afterSwap", all);
})();

// 表の各升に、その列の見出しの名前を付ける（表の部品の1か所。2026-11-20 の見直し：狭い画面で表を「見出し：値」の札に並べ替えるとき、
// 見出しの名前が無くて数字だけが並び、何の数か分からなかった）。どの表にも自動で付くので、画面ごとに書かない。
(function () {
  function label(root) {
    (root || document).querySelectorAll("table.list").forEach(function (table) {
      var heads = [], minor = [];
      table.querySelectorAll("thead th").forEach(function (th) {
        var span = parseInt(th.getAttribute("colspan") || "1", 10);
        for (var i = 0; i < span; i++) { heads.push(th.textContent.trim()); minor.push(th.classList.contains("minor")); }
      });
      if (!heads.length) return;
      table.querySelectorAll("tbody tr").forEach(function (tr) {
        var col = 0;
        Array.prototype.forEach.call(tr.children, function (td) {
          if (td.tagName === "TD" && !td.hasAttribute("data-label") && heads[col]) td.setAttribute("data-label", heads[col]);
          var here = col;
          col += parseInt(td.getAttribute("colspan") || "1", 10);
          if (td.tagName !== "TD") return;
          var t = td.textContent.trim();
          if (t.length <= 8) td.classList.add("short");   // 短い値は途中で折らない（「対応 / 中」と折れていた）
          // スマホの札の形では、0 の段を出さないのは、見出しに minor の付いた欄だけ（CSS）。単位つきの 0（「0円」「0本」「0件」）も同じ。
          // 「0.5」「0:30」のような 0 で始まる別の値は当てない。報酬・費用・今日かけた などの主な欄は、0 でも必ず出す
          // （2026-11-21 の見直し：すべての欄で隠していたので、費用と報酬の画面から報酬・費用の数が消え、依頼者どうしを比べられなかった）
          if (minor[here] && td.classList.contains("num") && /^0[^\d.,:]*$/.test(t)) td.classList.add("zero");
        });
      });
    });
  }
  document.addEventListener("DOMContentLoaded", function () { label(document); });
  document.addEventListener("htmx:afterSwap", function (e) { label(e.target); });
  // 依頼者のタブの印（止まっている＝赤・承認待ち＝橙。材料は帯の数え方の内訳 [data-tab-marks]。帯を読み直すたびに付け直す）
  document.addEventListener("htmx:afterSwap", function (e) {
    var m = e.target.querySelector && e.target.querySelector("[data-tab-marks]");
    if (!m) return;
    var stop = (m.getAttribute("data-stop") || "").split(" "), wait = (m.getAttribute("data-wait") || "").split(" ");
    document.querySelectorAll(".tab[data-client]").forEach(function (t) {
      var id = t.getAttribute("data-client"), s = stop.indexOf(id) >= 0, w = wait.indexOf(id) >= 0;
      t.classList.toggle("mark-stop", s); t.classList.toggle("mark-wait", !s && w);
      t.title = s ? "止まっているものがあります" : (w ? "承認待ちがあります" : "");
    });
  });
})();
