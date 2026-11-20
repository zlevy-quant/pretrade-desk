"use strict";
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var token = null, role = "advisor", tenant = "RIA", user = null;

  // ---- theme ----
  try { var s = localStorage.getItem("ptd-theme"); if (s) document.documentElement.setAttribute("data-theme", s); } catch (e) {}
  $("themeBtn").addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme");
    var dark = cur ? cur === "dark" : matchMedia("(prefers-color-scheme:dark)").matches;
    var next = dark ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("ptd-theme", next); } catch (e) {}
  });

  function api(method, path, body) {
    var opts = { method: method, headers: { "Content-Type": "application/json" } };
    if (token) opts.headers["Authorization"] = "Bearer " + token;
    if (body) opts.body = JSON.stringify(body);
    return fetch("/api" + path, opts).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }

  function stamp(iso) { return new Date(iso).toTimeString().slice(0, 8); }
  function px(n) { return Number(n).toFixed(2); }

  // ---- login ----
  $("loginBtn").addEventListener("click", function () {
    $("loginErr").hidden = true;
    var username = $("user").value.trim() || "trader";
    role = $("role").value; tenant = $("tenant").value;
    api("POST", "/login", { username: username, role: role, tenant: tenant }).then(function (res) {
      token = res.token; user = res.username; role = res.role; tenant = res.tenant;
      $("loginView").hidden = true; $("deskView").hidden = false;
      $("userPill").hidden = false; $("tenantPill").hidden = false; $("killBtn").hidden = false; $("logoutBtn").hidden = false;
      $("userPill").innerHTML = user + " &middot; <strong>" + (role === "supervisor" ? "Supervisor" : "Advisor") + "</strong>";
      $("tenantName").textContent = tenant;
      return Promise.all([loadMarket(), loadKill(), loadBlotter(), loadAudit()]);
    }).catch(function (e) { $("loginErr").textContent = e.message; $("loginErr").hidden = false; });
  });
  $("logoutBtn").addEventListener("click", function () { location.reload(); });

  // ---- market data ----
  function loadMarket() {
    return api("GET", "/market").then(function (res) {
      var sel = $("symbol"), cur = sel.value;
      sel.innerHTML = res.quotes.map(function (q) { return '<option>' + q.symbol + '</option>'; }).join("");
      if (cur) sel.value = cur;
      $("quotes").innerHTML = res.quotes.map(function (q) {
        var ageCls = q.stale ? "q-age stale" : "q-age";
        var ageTxt = q.stale ? ("stale, " + q.age_sec + "s old") : ("fresh, " + q.age_sec + "s");
        return '<div class="quote"><span class="sym">' + q.symbol + '</span>' +
          '<span class="q-nums"><b>' + px(q.bid) + '</b> <i>' + px(q.ask) + '</i> ' +
          '<span class="' + ageCls + '">' + ageTxt + '</span></span></div>';
      }).join("");
      // reflect the stale toggle to the selected symbol's state
      var selected = res.quotes.filter(function (q) { return q.symbol === sel.value; })[0];
      $("staleToggle").checked = !!(selected && selected.stale);
    });
  }

  $("staleToggle").addEventListener("change", function () {
    var sym = $("symbol").value;
    var path = "/market/" + encodeURIComponent(sym) + ($("staleToggle").checked ? "/stale" : "/fresh");
    api("POST", path, {}).then(loadMarket).then(loadAudit);
  });
  $("symbol").addEventListener("change", loadMarket);

  // ---- kill switch ----
  function renderKill(engaged) {
    $("killBtn").textContent = "Kill switch: " + (engaged ? "ON" : "off");
    $("killBtn").classList.toggle("live", engaged);
    $("statusDot").classList.toggle("off", engaged);
    $("killBanner").hidden = !engaged;
  }
  function loadKill() { return api("GET", "/killswitch").then(function (r) { renderKill(r.engaged); }); }
  $("killBtn").addEventListener("click", function () {
    var on = !$("killBtn").classList.contains("live");
    api("POST", "/killswitch", { engaged: on }).then(function (r) { renderKill(r.engaged); }).then(loadAudit);
  });

  // ---- submit order ----
  $("submitBtn").addEventListener("click", function () {
    var order = {
      account_id: $("account").value.trim(),
      symbol: $("symbol").value,
      side: $("side").value,
      order_type: $("otype").value,
      quantity: parseInt($("qty").value, 10),
      limit_price: $("limit").value === "" ? null : Number($("limit").value)
    };
    api("POST", "/orders", order).then(function (res) {
      renderVerdict(res);
      return Promise.all([loadBlotter(), loadAudit(), loadMarket()]);
    }).catch(function (e) { alert(e.message); });
  });

  function badge(status) {
    if (status === "pass") return '<span class="badge pass">pass</span>';
    if (status === "block") return '<span class="badge block">block</span>';
    if (status === "audit") return '<span class="badge audit">audit</span>';
    return '<span class="badge skip">skipped</span>';
  }

  function renderVerdict(res) {
    var b = $("verdictBody"), html = "";
    var cls = res.decision === "ALLOW" ? "allow" : "block";
    var text = res.decision === "ALLOW" ? "ORDER ACCEPTED, routed to venue"
      : (res.overridable ? "BLOCKED, overridable (soft control)" : "BLOCKED, final");
    html += '<div class="verdict-head ' + cls + '"><span class="big">' + (res.decision === "ALLOW" ? "✓ " : "✗ ") + text + '</span></div>';
    html += '<div class="controls-list">';
    res.controls.forEach(function (c) {
      html += '<div class="ctrl"><div class="nm">' + c.name.replace(/_/g, " ") + '<span class="tag">' + c.kind + '</span></div>' +
        '<div class="rs">' + c.reason + '</div>' + badge(c.status) + '</div>';
    });
    html += '</div>';
    b.innerHTML = html;

    if (res.decision === "BLOCK" && res.overridable) {
      var box = document.createElement("div");
      if (role === "supervisor") {
        box.className = "override-box";
        box.innerHTML = '<h4>Supervisory override (Series 24)</h4>' +
          '<p style="font-size:.82rem;margin:.1rem 0 .5rem;color:var(--warn)">A soft control blocked this order. Release it with a reason; the reason is recorded before release.</p>' +
          '<textarea id="ovReason" placeholder="Reason for override (required)"></textarea>' +
          '<button id="ovBtn">Override &amp; release</button>';
        b.appendChild(box);
        $("ovBtn").addEventListener("click", function () {
          var reason = ($("ovReason").value || "").trim();
          if (!reason) { $("ovReason").focus(); return; }
          api("POST", "/orders/" + encodeURIComponent(res.id) + "/override", { reason: reason }).then(function () {
            var head = b.querySelector(".verdict-head");
            head.className = "verdict-head allow";
            head.innerHTML = '<span class="big">✓ RELEASED ON OVERRIDE, routed to venue</span>';
            box.innerHTML = '<h4>Override recorded</h4><p style="font-size:.82rem;margin:0;color:var(--warn)">Released by ' + user + ' (supervisor). Reason written to the approval audit trail.</p>';
            return Promise.all([loadBlotter(), loadAudit()]);
          }).catch(function (e) { alert(e.message); });
        });
      } else {
        box.className = "override-box denied";
        box.innerHTML = '<h4>Override requires supervisor authority</h4>' +
          '<p style="font-size:.82rem;margin:0;color:var(--muted)">This soft control is overridable, but only a trade-desk supervisor (Series 24) may release it. Sign in as supervisor to override.</p>';
        b.appendChild(box);
      }
    }
  }

  // ---- blotter + audit ----
  function loadBlotter() {
    return api("GET", "/blotter").then(function (res) {
      var t = $("blotter"), tb = t.querySelector("tbody");
      if (!res.orders.length) { t.hidden = true; $("blotterEmpty").hidden = false; return; }
      t.hidden = false; $("blotterEmpty").hidden = true;
      tb.innerHTML = res.orders.map(function (o) {
        var st = o.status === "ACCEPTED" ? '<span class="st ok">ACCEPTED</span>'
          : o.status === "OVERRIDDEN" ? '<span class="st ov">OVERRIDDEN</span>'
          : '<span class="st no">BLOCKED</span>';
        return '<tr><td class="mono">' + o.id + '</td><td>' + o.symbol + ' ' + o.side + '</td><td class="mono">' + o.quantity + '</td><td>' + st + '</td></tr>';
      }).join("");
    });
  }
  function loadAudit() {
    return api("GET", "/audit").then(function (res) {
      var a = $("audit");
      if (!res.audit.length) { a.innerHTML = ""; $("auditEmpty").hidden = false; return; }
      $("auditEmpty").hidden = true;
      a.innerHTML = res.audit.map(function (e) {
        return '<div class="audit-item"><span class="ts">' + stamp(e.ts) + '</span><span>' + e.detail + '</span></div>';
      }).join("");
    });
  }

  // ---- scenarios ----
  var SC = {
    clean: function () { set("AAPL", "buy", "market", "100", ""); unstale(); },
    collar: function () { set("AAPL", "buy", "limit", "100", "260.00"); unstale(); },
    dupe: function () { set("MSFT", "buy", "market", "500", ""); unstale(); },
    stale: function () { set("NVDA", "buy", "market", "100", ""); $("staleToggle").checked = true; $("staleToggle").dispatchEvent(new Event("change")); },
    bp: function () { set("NVDA", "buy", "market", "5000", ""); unstale(); }
  };
  function set(sym, side, type, qty, limit) {
    $("symbol").value = sym; $("side").value = side; $("otype").value = type; $("qty").value = qty; $("limit").value = limit;
  }
  function unstale() { if ($("staleToggle").checked) { $("staleToggle").checked = false; $("staleToggle").dispatchEvent(new Event("change")); } }
  $("scenarios").addEventListener("click", function (e) {
    var btn = e.target.closest("[data-sc]"); if (!btn) return; SC[btn.getAttribute("data-sc")]();
  });
})();
