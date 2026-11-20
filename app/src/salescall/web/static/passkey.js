// パスキー（ログインの2段目・登録）と Google のボタンの受け渡し（持ち主 CL-3）。確かめるのはサーバー（core の passkeys・google_login の1か所）。
// ここは、サーバーの問い（JSON）をブラウザの形に直して端末へ渡し、端末の答えを JSON にしてサーバーへ返すだけ。何も決めない。
(function () {
  function b64u(buf) {
    var s = "", bytes = new Uint8Array(buf);
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function unb64u(text) {
    var s = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
    var out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out.buffer;
  }
  function forCreate(o) {
    o.challenge = unb64u(o.challenge); o.user.id = unb64u(o.user.id);
    (o.excludeCredentials || []).forEach(function (c) { c.id = unb64u(c.id); });
    return o;
  }
  function forGet(o) {
    o.challenge = unb64u(o.challenge);
    (o.allowCredentials || []).forEach(function (c) { c.id = unb64u(c.id); });
    return o;
  }
  function answer(c) {
    var r = c.response, out = { id: c.id, rawId: b64u(c.rawId), type: c.type,
      clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {}, response: { clientDataJSON: b64u(r.clientDataJSON) } };
    if (r.attestationObject) {
      out.response.attestationObject = b64u(r.attestationObject);
      if (r.getTransports) out.response.transports = r.getTransports();
    }
    if (r.authenticatorData) {
      out.response.authenticatorData = b64u(r.authenticatorData);
      out.response.signature = b64u(r.signature);
      if (r.userHandle) out.response.userHandle = b64u(r.userHandle);
    }
    return out;
  }
  function formToken() { var m = document.querySelector('meta[name="form-token"]'); return m ? m.content : ""; }
  function say(el, text) { if (el) { el.textContent = text; el.hidden = !text; } }
  async function post(url, body, json) {
    var headers = { "X-Form-Token": formToken() };
    if (json) headers["Content-Type"] = "application/json";
    else if (body) headers["Content-Type"] = "application/x-www-form-urlencoded";
    var r = await fetch(url, { method: "POST", headers: headers, body: body, credentials: "same-origin" });
    var data = await r.json();
    if (!r.ok) throw new Error(data.error || "うまくいきませんでした。画面を開き直してください。");
    return data;
  }
  document.addEventListener("click", async function (e) {
    var b = e.target.closest && e.target.closest("[data-passkey]");
    if (!b) return;
    e.preventDefault();
    var msg = document.querySelector("[data-passkey-msg]");
    say(msg, "");
    if (!window.PublicKeyCredential || !navigator.credentials) { say(msg, "このブラウザではパスキーを使えません。予備の合言葉をお使いください。"); return; }
    b.disabled = true;
    try {
      if (b.getAttribute("data-passkey") === "login") {
        var o = await post("/login/second/options");
        var c = await navigator.credentials.get({ publicKey: forGet(o) });
        var done = await post("/login/second/verify", JSON.stringify(answer(c)), true);
        location.href = done.next;
      } else {
        var name = document.querySelector('[name="passkey_label"]');
        if (!name || !name.value.trim()) { say(msg, "パスキーの名前（「仕事の PC」など）を入れてください。"); return; }
        var opt = await post("/t/account/passkey/options");
        var made = await navigator.credentials.create({ publicKey: forCreate(opt) });
        var got = await post("/t/account/passkey", JSON.stringify({ credential: answer(made), label: name.value.trim() }), true);
        if (got.codes && got.codes.length) {   // 最初のパスキーのときだけ：予備の合言葉を、このときだけ見せる（サーバーは写しだけを残す）
          var box = document.querySelector("[data-codes]");
          box.querySelector("ol").innerHTML = "";
          got.codes.forEach(function (code) { var li = document.createElement("li"); li.textContent = code; box.querySelector("ol").appendChild(li); });
          box.hidden = false;
        } else {
          location.href = "/t/account?done=" + encodeURIComponent("パスキーを登録しました。");
        }
      }
    } catch (err) {
      say(msg, err && err.name === "NotAllowedError" ? "取りやめました（または時間切れです）。もう一度お試しください。" : (err && err.message) || "うまくいきませんでした。");
    } finally { b.disabled = false; }
  });
  // Google のボタンが返した印を、サーバーへ渡す（行き先はページの [data-google-post] の欄。ログインの画面とログインと安全の画面）
  window.salescallGoogle = function (resp) {
    var msg = document.querySelector("[data-google-msg]");
    post(document.querySelector("[data-google-post]").getAttribute("data-google-post"), "credential=" + encodeURIComponent(resp.credential))
      .then(function (d) { location.href = d.next; })
      .catch(function (err) { say(msg, err.message); });
  };
})();
