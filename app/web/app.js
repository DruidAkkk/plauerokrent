"use strict";

/* ---------- мост JS ↔ Python ----------
   В pywebview методы бэкенда доступны как window.pywebview.api.<name>(...).
   Для превью в обычном браузере (без pywebview) используется window.__MOCK_API__. */
function bridgeReady() {
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) return resolve("pywebview");
    if (window.__MOCK_API__) return resolve("mock");
    const t = setTimeout(() => resolve(window.__MOCK_API__ ? "mock" : "pywebview"), 1500);
    window.addEventListener("pywebviewready", () => { clearTimeout(t); resolve("pywebview"); });
  });
}
let MODE = "pywebview";
async function api(method, ...args) {
  const src = MODE === "mock" ? window.__MOCK_API__ : window.pywebview.api;
  if (!src || typeof src[method] !== "function") return { error: "нет метода " + method };
  try { return await src[method](...args); }
  catch (e) { return { error: String(e) }; }
}

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (n) => (Math.round(n * 100) / 100).toLocaleString("ru-RU");

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), 3800);
}

/* копирование в буфер (WebView2): clipboard API, иначе fallback на выделение */
async function copyText(text, btn) {
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch (e) { /* no-op */ }
  if (!ok) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); ok = document.execCommand("copy"); ta.remove();
    } catch (e) { /* no-op */ }
  }
  if (btn) { const t = btn.textContent; btn.textContent = "✓"; setTimeout(() => { btn.textContent = t; }, 1200); }
  toast(ok ? "Скопировано" : "Не удалось скопировать", ok ? "ok" : "err");
}

/* модал «доступ к аккаунту»: логин + пароль (скрыт), кнопки показать/копировать */
function credsModal(title, login, password, note) {
  infoModal(title, `
    <div style="padding:6px 0">
      <div class="field"><label>Логин</label>
        <div style="display:flex;gap:8px">
          <input id="cm-login" readonly value="${esc(login)}" style="flex:1">
          <button class="btn sm" id="cm-login-c">Копировать</button></div></div>
      <div class="field"><label>Пароль</label>
        <div style="display:flex;gap:8px">
          <input id="cm-pass" readonly type="password" value="${esc(password)}" style="flex:1">
          <button class="btn sm icon" id="cm-pass-eye" title="Показать/скрыть">👁️</button>
          <button class="btn sm" id="cm-pass-c">Копировать</button></div></div>
      ${note ? `<div class="sub" style="margin-top:6px;line-height:1.5">${esc(note)}</div>` : ""}
    </div>`);
  $("#cm-login-c").onclick = (e) => copyText(login, e.currentTarget);
  $("#cm-pass-c").onclick = (e) => copyText(password, e.currentTarget);
  $("#cm-pass-eye").onclick = () => { const p = $("#cm-pass"); p.type = p.type === "password" ? "text" : "password"; };
}

async function openAccountCreds(id) {
  const r = await api("account_credentials", id);
  if (r.error || !r.ok) return toast(r.error || r.message || "ошибка", "err");
  credsModal("Доступ к аккаунту — " + r.login, r.login, r.password,
    r.has_mafile ? "maFile задан — Steam Guard коды через кнопку 🔑 Код." : "maFile не задан.");
}

/* ---------- экран блокировки ---------- */
function enterApp() {
  $("#mp").value = "";
  if ($("#code2fa")) $("#code2fa").value = "";
  $("#lock").style.display = "none";
  $("#app").classList.add("show");
  refreshWorker();
  go("dashboard");
}

async function initLock() {
  // 0) ключ из env (ENCRYPTION_SECRET/SALT) → вход без пароля (панель на той же VPS, что воркер)
  const st0 = await api("status");
  if (st0 && st0.env_mode) {
    const r = await api("env_unlock");
    if (r && r.ok) { enterApp(); return; }
  }
  // 1) авто-вход по сохранённой сессии
  const rs = await api("resume");
  if (rs && rs.resumed) { enterApp(); return; }

  const st = st0 || await api("status");
  const initialized = st && st.initialized;
  // сброс шагов
  const creds = $("#lock-step-creds"), step2 = $("#lock-step-2fa");
  if (creds) creds.style.display = "block";
  if (step2) step2.style.display = "none";
  if ($("#username")) $("#username").value = (st && st.username) || "";
  $("#lock-title").textContent = initialized ? "Вход" : "Создание хранилища";
  $("#lock-hint").textContent = initialized
    ? "Логин и мастер-пароль. Пароль — он же ключ шифрования; пароли и maFile не покидают эту машину."
    : "Задайте логин и мастер-пароль. Из пароля выводится ключ шифрования — восстановить забытый нельзя.";
  $("#lock-btn").textContent = initialized ? "Войти" : "Создать и войти";

  let mode = initialized ? "unlock" : "setup";
  const submit = async () => {
    $("#lock-err").textContent = "";
    if (mode === "2fa") {
      const r = await api("verify_2fa", $("#code2fa").value);
      if (r.ok) enterApp(); else $("#lock-err").textContent = r.error || "Ошибка";
      return;
    }
    const mp = $("#mp").value;
    const username = $("#username") ? $("#username").value : "";
    if (!mp) { $("#lock-err").textContent = "Введите пароль"; return; }
    const remember = $("#remember") && $("#remember").checked;
    const r = mode === "setup"
      ? await api("setup", mp, username, remember)
      : await api("unlock", mp, username, remember);
    if (r.ok) { enterApp(); return; }
    if (r.need_2fa) {
      mode = "2fa";
      if (creds) creds.style.display = "none";
      if (step2) step2.style.display = "block";
      $("#lock-title").textContent = "Подтверждение входа";
      $("#lock-hint").textContent = "Код отправлен в ваш Telegram. Введите его (действует 5 минут).";
      $("#lock-btn").textContent = "Подтвердить";
      $("#code2fa").value = ""; $("#code2fa").focus();
      return;
    }
    $("#lock-err").textContent = r.error || "Ошибка";
  };
  $("#lock-btn").onclick = submit;
  $("#mp").onkeydown = (e) => { if (e.key === "Enter") submit(); };
  if ($("#username")) $("#username").onkeydown = (e) => { if (e.key === "Enter") submit(); };
  if ($("#code2fa")) $("#code2fa").onkeydown = (e) => { if (e.key === "Enter") submit(); };
}

/* ---------- навигация ---------- */
const VIEWS = {};
let current = "dashboard";
function go(view) {
  current = view;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  (VIEWS[view] || VIEWS.dashboard)();
}
document.querySelectorAll("#nav a").forEach((a) => (a.onclick = () => go(a.dataset.view)));

async function refreshBadges() {
  const nbAcc = $("#nb-acc"), nbLots = $("#nb-lots"), nbRent = $("#nb-rent");
  if (!nbAcc && !nbLots && !nbRent) return;  // бейджи живут в таб-баре плагина, если он открыт
  const [acc, lots, rent] = await Promise.all([api("accounts"), api("lots"), api("rentals")]);
  if (nbAcc && acc.items) nbAcc.textContent = acc.items.length;
  if (nbLots && lots.items) nbLots.textContent = lots.items.length;
  if (nbRent && rent.groups) nbRent.textContent = (rent.groups.active.length + rent.groups.queued.length);
}

/* ---------- воркер ---------- */
async function refreshWorker() {
  const st = await api("status");
  const on = st && st.worker;
  $("#worker").classList.toggle("on", on);
  $("#worker-text").textContent = on ? "воркер работает" : "воркер остановлен";
  $("#btn-worker").textContent = on ? "Остановить воркер" : "Запустить воркер";
}
$("#btn-worker").onclick = async () => {
  const r = await api("worker_toggle");
  if (r.error) toast(r.error, "err");
  else toast(r.worker ? "Воркер запущен" : "Воркер остановлен", "ok");
  await refreshWorker();
};
$("#btn-lock").onclick = async () => {
  await api("lock");
  $("#app").classList.remove("show");
  $("#lock").style.display = "flex";
  initLock();
};

/* ---------- дашборд ---------- */
VIEWS.dashboard = async () => {
  const d = await api("dashboard");
  if (d.error) return fail(d.error);
  const chips = Object.entries(d.accounts_by_state)
    .map(([k, v]) => `<span class="chip">${esc(k)}: ${v}</span>`).join("") || "<span class='chip'>пусто</span>";
  const util = (d.utilization || []).filter((a) => a.rentals > 0 || a.utilization > 0);
  const rows = util.length ? util.map((a) => `
    <tr><td>${esc(a.login)}</td><td>${a.rentals}</td><td>${money(a.revenue)} ₽</td>
    <td><div class="util"><div class="track"><div class="fill" style="width:${Math.min(a.utilization, 100)}%"></div></div><span>${a.utilization}%</span></div></td></tr>
  `).join("") : `<tr><td colspan="4" class="empty">Пока нет завершённых аренд за месяц</td></tr>`;

  const t = d.today || {}, pl = d.plugins || {};
  const ko = pl.kosell || {}, mob = pl.mobile || {};
  setMain("Дашборд", "сводка за последние 30 дней", `
    <div class="kpis">
      <div class="kpi g"><div class="ic">✅</div><div class="label">Выдач сегодня</div><div class="value">${t.total ?? 0}</div></div>
      <div class="kpi"><div class="ic">💰</div><div class="label">Выручка за месяц</div><div class="value">${money(d.revenue_month)} <small>₽</small></div></div>
      <div class="kpi v"><div class="ic">▶️</div><div class="label">Аренд за месяц</div><div class="value">${d.rentals_month}</div></div>
      <div class="kpi a ${d.quarantine ? "danger" : ""}"><div class="ic">${d.quarantine ? "⚠️" : "🛡️"}</div><div class="label">В карантине</div><div class="value">${d.quarantine}</div></div>
    </div>
    <div class="card"><h3>Состояние плагинов</h3><div class="body">
      <table><thead><tr><th>Плагин</th><th>Статус</th><th>Активные аренды</th><th>Детали</th></tr></thead><tbody>
        <tr><td>🔑 Steam-аренда</td><td><span class="badge-state s-active">включён</span></td><td>${d.active} <span class="sub">/ ${d.queued} в очереди</span></td><td>${d.accounts_total} аккаунтов${d.quarantine ? `, ${d.quarantine} в карантине` : ""}</td></tr>
        <tr><td>🛒 KoSell-реселл</td><td><span class="badge-state s-${ko.enabled ? "active" : "paused"}">${ko.enabled ? "подключён" : "нет ключа"}</span></td><td>${ko.active ?? 0}</td><td>дропшиппинг-реселл</td></tr>
        <tr><td>📱 PUBG Mobile</td><td><span class="badge-state s-${mob.pool ? "active" : "paused"}">${mob.pool ? "готов" : "пул пуст"}</span></td><td>${mob.active ?? 0}</td><td>${mob.pool ? `свободно ${mob.free}/${mob.pool}` : "добавьте аккаунты"}</td></tr>
        <tr><td>🛡️ Гарантия</td><td><span class="badge-state s-${(d.warranty && d.warranty.enabled) ? "active" : "paused"}">${(d.warranty && d.warranty.enabled) ? "включена" : "выкл"}</span></td><td>—</td><td>${(d.warranty && d.warranty.open) || 0} открытых случаев</td></tr>
      </tbody></table>
    </div></div>
    <div class="card"><h3>Состояние пула Steam (${d.accounts_total} аккаунтов)</h3><div class="body" style="padding:16px 18px">${chips}</div></div>
    <div class="card"><h3>Загрузка аккаунтов Steam</h3><div class="body">
      <table><thead><tr><th>Логин</th><th>Аренд</th><th>Выручка</th><th>Загрузка</th></tr></thead><tbody>${rows}</tbody></table>
    </div></div>
  `);
};

/* ---------- аккаунты ---------- */
const STATES = ["free", "rented", "rotating", "resting", "quarantine", "paused"];
/* ---------- ПЛАГИНЫ ---------- */
VIEWS.plugins = async () => {
  setMain("Плагины", "модули бота — включайте нужные под свои лоты", `
    <div class="kpis" style="grid-template-columns:repeat(3,1fr)">
      <div class="card" style="margin:0;cursor:pointer" id="plg-rental">
        <h3 style="border:none">🔑 Аренда Steam-аккаунтов</h3>
        <div class="body" style="padding:0 18px 16px">
          <span class="badge-state s-active">активен</span>
          <p class="sub" style="margin:12px 0 14px;line-height:1.5">Автовыдача аккаунта на срок, таймеры, продление, автовозврат (смена пароля + деавторизация), очередь, Guard-коды по <b>!код</b>, чёрный список.</p>
          <button class="btn primary" id="open-rental">Открыть →</button>
        </div>
      </div>
      <div class="card" style="margin:0;cursor:pointer" id="plg-kosell">
        <h3 style="border:none">🛒 KoSell-аренда (реселл)</h3>
        <div class="body" style="padding:0 18px 16px">
          <span class="badge-state s-active">активен</span>
          <p class="sub" style="margin:12px 0 14px;line-height:1.5">Дропшиппинг почасовой аренды Steam: берём у KoSell, продаём дороже на Playerok. Автовыдача логина+пароля, Guard-коды по <b>!код</b>, продление, завершение — без maFile на твоей стороне.</p>
          <button class="btn primary" id="open-kosell">Открыть →</button>
        </div>
      </div>
      <div class="card" style="margin:0;cursor:pointer" id="plg-warranty">
        <h3 style="border:none">🛡️ Гарантия / Автозамена</h3>
        <div class="body" style="padding:0 18px 16px">
          <span class="badge-state s-active">активен</span>
          <p class="sub" style="margin:12px 0 14px;line-height:1.5">Нет свободного аккаунта → бот сам повторяет аренду. Покупатель пишет <b>!замена</b> → перевыдача доступа и кода. Журнал гарантийных случаев.</p>
          <button class="btn primary" id="open-warranty">Открыть →</button>
        </div>
      </div>
      <div class="card" style="margin:0;cursor:pointer" id="plg-repricer">
        <h3 style="border:none">📈 Репрайсер</h3>
        <div class="body" style="padding:0 18px 16px">
          <span class="badge-state s-active">активен</span>
          <p class="sub" style="margin:12px 0 14px;line-height:1.5">Сканирует все твои лоты Playerok и показывает по каждому статистику похожих лотов конкурентов: минимальная, средняя и максимальная цена. Видно, где ты дороже рынка.</p>
          <button class="btn primary" id="open-repricer">Открыть →</button>
        </div>
      </div>
      <div class="card" style="margin:0;cursor:pointer" id="plg-review">
        <h3 style="border:none">⭐ Просьба об отзыве</h3>
        <div class="body" style="padding:0 18px 16px">
          <span class="badge-state s-active" id="plg-review-badge">активен</span>
          <p class="sub" style="margin:12px 0 14px;line-height:1.5">После успешного завершения аренды (Steam / KoSell / PUBG) бот вежливо просит покупателя оставить отзыв. Текст сообщения настраивается. По умолчанию выключено.</p>
          <button class="btn primary" id="open-review">Открыть →</button>
        </div>
      </div>
      <div class="card" style="margin:0;cursor:pointer" id="plg-mobile">
        <h3 style="border:none">📱 Аренда PUBG Mobile</h3>
        <div class="body" style="padding:0 18px 16px">
          <span class="badge-state s-active">в разработке</span>
          <p class="sub" style="margin:12px 0 14px;line-height:1.5">Аренда мобильных аккаунтов (соц-логин Facebook/Google/VK + коды на подконтрольную почту). Сейчас: пул аккаунтов. Дальше: лоты, автовыдача, код по почте, автовозврат.</p>
          <button class="btn primary" id="open-mobile">Открыть →</button>
        </div>
      </div>
      <div class="card" style="margin:0;opacity:.55">
        <h3 style="border:none">🎁 Telegram NFT-подарки</h3>
        <div class="body" style="padding:0 18px 16px"><span class="badge-state s-paused">скоро</span>
          <p class="sub" style="margin:12px 0;line-height:1.5">Аренда NFT-подарков с возвратом по таймеру через MTProto.</p></div>
      </div>
    </div>
    <div class="sub">Каждый плагин несёт свои лоты, аккаунты и настройки. Аренда — платный модуль, базовые функции (автовыдача, автоответчик, уведомления) — во вкладке «Автоматизация».</div>
  `);
  const open = () => openRentalPlugin();
  $("#open-rental").onclick = open;
  $("#plg-rental").onclick = open;
  const openK = () => openKoSellPlugin();
  $("#open-kosell").onclick = openK;
  $("#plg-kosell").onclick = openK;
  $("#open-warranty").onclick = $("#plg-warranty").onclick = renderWarranty;
  $("#open-repricer").onclick = $("#plg-repricer").onclick = renderRepricer;
  $("#open-review").onclick = $("#plg-review").onclick = renderReview;
  $("#open-mobile").onclick = $("#plg-mobile").onclick = openMobilePlugin;
};

/* ===================== Плагин Аренда PUBG Mobile ===================== */
const MOB_TABS = [["accounts", "👤 Аккаунты"], ["lots", "🏷️ Лоты"], ["rentals", "⏱️ Аренды"], ["mbl", "🚫 Чёрный список"], ["roadmap", "🗺️ Дальше"]];
const MOB_LOGIN = { facebook: "Facebook", google: "Google", twitter: "Twitter/X", vk: "VK", apple: "Apple", email: "Почта/телефон" };
let mobTab = "accounts";
function openMobilePlugin() { mobTab = "accounts"; renderMobTab(); }
function renderMobTab() { ({ accounts: renderMobAccounts, lots: renderMobLots, rentals: renderMobRentals, mbl: renderMobBlacklistTab, roadmap: renderMobRoadmap }[mobTab] || renderMobAccounts)(); }
VIEWS.mobile = renderMobTab;

function mobPluginMain(active, body) {
  const bar = MOB_TABS.map(([k, l]) => `<button class="${k === active ? "active" : ""}" data-mtab="${k}">${l}</button>`).join("");
  $("#main").innerHTML = `
    <div class="topbar"><div>
      <div style="margin-bottom:6px"><a href="#" id="back-plugins-m" style="color:var(--accent);text-decoration:none;font-size:12.5px">← Все плагины</a></div>
      <h1>📱 Аренда PUBG Mobile</h1><div class="sub">пул мобильных аккаунтов и доступы</div>
    </div></div>
    <div class="tabs" style="flex-wrap:wrap;margin-bottom:16px">${bar}</div>
    <div id="mbody">${body}</div>`;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === "plugins"));
  $("#back-plugins-m").onclick = (e) => { e.preventDefault(); go("plugins"); };
  document.querySelectorAll("[data-mtab]").forEach((b) => (b.onclick = () => { mobTab = b.dataset.mtab; renderMobTab(); }));
}

async function renderMobAccounts() {
  mobPluginMain("accounts", `<div class="card"><div class="body" style="padding:18px">Загрузка…</div></div>`);
  const r = await api("mobile_accounts");
  const items = r.items || [];
  const rows = items.length ? items.map((a) => `
    <tr>
      <td class="mono">#${a.id}</td>
      <td>${esc(MOB_LOGIN[a.login_type] || a.login_type)}</td>
      <td><b>${esc(a.login)}</b>${a.note ? `<div class="mono">${esc(a.note)}</div>` : ""}</td>
      <td>${esc(a.email_mask || "—")}${a.has_email_password ? " 🔑" : ""}</td>
      <td><span class="badge-state s-${a.state === "rented" ? "active" : a.state === "paused" ? "paused" : "free"}">${a.state}</span></td>
      <td>${(a.tags || []).map((t) => `<span class="chip">${esc(t)}</span>`).join("") || "—"}</td>
      <td style="white-space:nowrap">
        <button class="btn sm icon" data-mcreds="${a.id}" title="Показать доступы">👁️</button>
        <button class="btn sm icon" data-mtest="${a.id}" title="Проверить код с почты (IMAP)">📧</button>
        <button class="btn sm icon" data-medit="${a.id}" title="Настройки">⚙️</button>
        <button class="btn sm icon danger" data-mdel="${a.id}" title="Удалить">🗑️</button>
      </td>
    </tr>`).join("") : `<tr><td colspan="7" class="empty">Пул пуст. Добавьте мобильный аккаунт.</td></tr>`;
  mobPluginMain("accounts", `
    <div class="toolbar" style="flex-wrap:wrap;gap:10px;align-items:center">
      <button class="btn primary" id="mob-add">+ Добавить аккаунт</button>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-left:auto">
        <input id="mob-autokick" type="checkbox" ${r.autokick ? "checked" : ""} style="width:auto;margin:0">
        <span>Авто-кик по окончании аренды <span class="sub">(эксперим.)</span></span>
      </label>
    </div>
    <div class="card"><div class="body">
      <table><thead><tr><th>ID</th><th>Тип входа</th><th>Логин</th><th>Почта</th><th>Состояние</th><th>Теги</th><th>Действия</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
    <div class="sub">Почта — подконтрольный ящик аккаунта: туда приходят коды входа (их бот сможет пересылать покупателю) и через неё делается возврат. 🔑 = задан пароль почты.</div>
    <div class="sub" style="margin-top:6px">⚠️ <b>Авто-кик (эксперим.)</b>: по окончании аренды бот попытается выкинуть арендатора (разлогин соц-входа). В отличие от Steam, у PUBG Mobile нет чистого API «выйти везде» — механизм зависит от провайдера и пока не доведён, поэтому безопасно срабатывает фолбэк (аккаунт освобождается + напоминание сменить доступ). Включать и проверять — только на пустом аккаунте.</div>
  `);
  const byId = Object.fromEntries(items.map((a) => [a.id, a]));
  $("#mob-add").onclick = () => openMobForm(null);
  const ak = $("#mob-autokick");
  if (ak) ak.onchange = async () => {
    const res = await api("mobile_set_autokick", ak.checked);
    res.ok ? toast(ak.checked ? "Авто-кик включён (эксперим.)" : "Авто-кик выключен", "ok")
           : toast(res.error || "ошибка", "err");
  };
  document.querySelectorAll("[data-mcreds]").forEach((b) => (b.onclick = () => openMobCreds(+b.dataset.mcreds)));
  document.querySelectorAll("[data-mtest]").forEach((b) => (b.onclick = () => openMobTestCode(+b.dataset.mtest)));
  document.querySelectorAll("[data-medit]").forEach((b) => (b.onclick = () => openMobForm(byId[+b.dataset.medit])));
  document.querySelectorAll("[data-mdel]").forEach((b) => (b.onclick = () => confirmMobDelete(byId[+b.dataset.mdel])));
}

async function openMobTestCode(id) {
  infoModal("Проверка кода с почты", `<div style="text-align:center;padding:18px 0"><div style="font-size:30px">⏳</div><div class="sub" style="margin-top:10px">Читаю почту по IMAP…</div></div>`);
  const r = await api("mobile_test_code", id);
  infoModal("Код с почты — " + (r.ok ? "найден" : "нет"), `
    <div style="text-align:center;padding:14px 0">
      ${r.ok ? `<div style="font-size:40px;font-weight:700;letter-spacing:6px;color:var(--accent);font-family:Consolas,monospace">${esc(r.code)}</div>` : `<div style="font-size:38px">📭</div>`}
      <div class="sub" style="margin-top:14px">${esc(r.message || "")}</div>
      ${r.ok ? "" : `<div class="sub" style="margin-top:8px">Запросите код входа в игре/провайдере, затем проверьте снова. Для Gmail/Яндекс нужен «пароль приложения» и включённый IMAP.</div>`}
    </div>`);
}

/* ---- мобильные: лоты ---- */
async function renderMobLots() {
  mobPluginMain("lots", `<div class="card"><div class="body" style="padding:18px">Загрузка…</div></div>`);
  const r = await api("mobile_lots");
  const items = r.items || [], games = r.games || ["pubg_mobile"];
  const rows = items.length ? items.map((l) => `
    <tr>
      <td class="mono">${esc(l.lot_id)}</td>
      <td>${esc(l.game)}</td>
      <td>${l.hours} ч</td>
      <td>${l.free > 0 ? `🟢 ${l.free}` : "🔴 0"}</td>
      <td>${l.enabled ? "вкл" : "выкл"}</td>
      <td><button class="btn sm icon danger" data-mldel="${esc(l.lot_id)}" title="Удалить">🗑️</button></td>
    </tr>`).join("") : `<tr><td colspan="6" class="empty">Нет привязок. Свяжите лот Playerok с пулом PUBG-аккаунтов.</td></tr>`;
  mobPluginMain("lots", `
    <div class="toolbar"><button class="btn primary" id="ml-add">+ Привязать лот</button></div>
    <div class="card"><div class="body">
      <table><thead><tr><th>Лот Playerok</th><th>Пул (game)</th><th>Срок</th><th>Свободно</th><th>Статус</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
    <div class="sub">Когда покупают привязанный лот, бот выдаёт свободный аккаунт из пула на указанный срок. «Свободно» — сколько аккаунтов пула сейчас доступно.</div>
  `);
  $("#ml-add").onclick = () => {
    const gopts = games.map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join("");
    modal("Привязать лот к пулу PUBG", `
      <div class="field"><label>ID лота на Playerok</label><input id="ml-lot" placeholder="slug или id лота"></div>
      <div class="row2">
        <div class="field"><label>Пул (метка game)</label><select id="ml-game">${gopts}</select></div>
        <div class="field"><label>Срок аренды, часы</label><input id="ml-hours" type="number" value="24" min="1"></div>
      </div>
      <div class="form-err" id="ml-err"></div>`, async () => {
      const data = { lot_id: $("#ml-lot").value.trim(), game: $("#ml-game").value, hours: +$("#ml-hours").value };
      if (!data.lot_id) { $("#ml-err").textContent = "Укажите id лота"; return false; }
      const res = await api("mobile_lot_set", data);
      if (res.ok) { toast("Лот привязан", "ok"); closeModal(); renderMobLots(); }
      else { $("#ml-err").textContent = res.error; return false; }
    }, "Привязать");
  };
  document.querySelectorAll("[data-mldel]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Удалить привязку лота " + b.dataset.mldel + "?")) return;
    const res = await api("mobile_lot_delete", { lot_id: b.dataset.mldel });
    res.ok ? (toast("Удалено", "ok"), renderMobLots()) : toast(res.error || "ошибка", "err");
  }));
}

/* ---- мобильные: аренды ---- */
async function renderMobRentals() {
  mobPluginMain("rentals", `<div class="card"><div class="body" style="padding:18px">Загрузка…</div></div>`);
  const r = await api("mobile_rentals");
  const items = r.items || [];
  const now = Date.now() / 1000;
  const rows = items.length ? items.map((x) => {
    const left = Math.max(0, Math.round((x.expires - now) / 3600));
    return `<tr>
      <td><b>${esc(x.login || "#" + x.account_id)}</b></td>
      <td>${esc(x.game)}</td>
      <td class="mono">${x.deal_id ? esc(x.deal_id) : "—"}</td>
      <td><span class="badge-state s-${x.status === "active" ? "active" : "paused"}">${x.status}</span></td>
      <td>${x.status === "active" ? left + " ч" : "—"}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="empty">Аренд пока нет.</td></tr>`;
  mobPluginMain("rentals", `
    <div class="toolbar"><button class="btn" id="mr-refresh">↻ Обновить</button></div>
    <div class="card"><div class="body">
      <table><thead><tr><th>Аккаунт</th><th>Пул</th><th>Заказ</th><th>Статус</th><th>Осталось</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
    <div class="sub">Активные аренды выдаёт воркер по заказам Playerok. По окончании срока аккаунт освобождается автоматически.</div>
  `);
  $("#mr-refresh").onclick = renderMobRentals;
}

function openMobForm(a) {
  const edit = !!a;
  const ltOpts = Object.entries(MOB_LOGIN).map(([v, l]) => `<option value="${v}" ${a && a.login_type === v ? "selected" : ""}>${l}</option>`).join("");
  const stateSel = edit ? `<div class="field"><label>Состояние</label><select id="m-state">
      ${["free", "paused", "rented"].map((s) => `<option value="${s}" ${a.state === s ? "selected" : ""}>${s}</option>`).join("")}</select></div>` : "";
  modal((edit ? "Настройки аккаунта — " + a.login : "Новый мобильный аккаунт"), `
    <div class="row2">
      <div class="field"><label>Тип входа</label><select id="m-lt">${ltOpts}</select></div>
      <div class="field"><label>Игра / метка</label><input id="m-game" value="${esc(a ? a.game : "pubg_mobile")}"></div>
    </div>
    <div class="field"><label>Логин (email/телефон/username)</label><input id="m-login" value="${esc(a ? a.login : "")}"></div>
    <div class="field"><label>Пароль${edit ? " (пусто — не менять)" : ""}</label><input id="m-pass" type="password" placeholder="${edit ? "••••••" : ""}"></div>
    <div class="row2">
      <div class="field"><label>Подконтрольная почта</label><input id="m-email" value="${esc(a ? (a.email || "") : "")}" placeholder="для кодов и возврата"></div>
      <div class="field"><label>IMAP-хост (необяз.)</label><input id="m-imap" value="${esc(a ? a.imap_host : "")}" placeholder="imap.gmail.com"></div>
    </div>
    <div class="field"><label>Пароль почты${edit ? " (пусто — не менять)" : ""}</label><input id="m-epass" type="password" placeholder="${edit ? "••••••" : "пароль приложения IMAP"}"></div>
    <div class="field"><label>Прокси (необяз.)</label><input id="m-proxy" value="${esc(a ? a.proxy : "")}"></div>
    <div class="row2">
      <div class="field"><label>Теги (через запятую)</label><input id="m-tags" value="${esc(a ? (a.tags || []).join(", ") : "")}"></div>
      <div class="field"><label>Заметка</label><input id="m-note" value="${esc(a ? a.note : "")}"></div>
    </div>
    ${stateSel}
    <div class="form-err" id="m-err"></div>
  `, async () => {
    const data = {
      login_type: $("#m-lt").value, game: $("#m-game").value, login: $("#m-login").value.trim(),
      password: $("#m-pass").value, email: $("#m-email").value, imap_host: $("#m-imap").value,
      email_password: $("#m-epass").value, proxy: $("#m-proxy").value,
      tags: $("#m-tags").value, note: $("#m-note").value,
    };
    if (!data.login) { $("#m-err").textContent = "Укажите логин"; return false; }
    if (edit) { data.id = a.id; data.state = $("#m-state") ? $("#m-state").value : undefined; }
    const r = await api(edit ? "mobile_update" : "mobile_add", data);
    if (r.ok) { toast(edit ? "Сохранено" : "Аккаунт добавлен", "ok"); closeModal(); renderMobAccounts(); }
    else { $("#m-err").textContent = r.error; return false; }
  }, edit ? "Сохранить" : "Добавить");
}

async function openMobCreds(id) {
  const r = await api("mobile_credentials", id);
  if (r.error || !r.ok) return toast(r.error || "ошибка", "err");
  infoModal("Доступы — " + r.login, `
    <div style="padding:6px 0">
      <div class="sub" style="margin-bottom:8px">Тип входа: <b>${esc(MOB_LOGIN[r.login_type] || r.login_type)}</b></div>
      ${mobField("Логин", r.login)}
      ${mobField("Пароль", r.password, true)}
      ${r.email ? mobField("Почта", r.email) : ""}
      ${r.email_password ? mobField("Пароль почты", r.email_password, true) : ""}
      <div class="sub" style="margin-top:8px">Коды входа приходят на подконтрольную почту — скоро бот будет пересылать их покупателю по <b>!код</b>.</div>
    </div>`);
  // навесить копирование/показ
  document.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = (e) => copyText(b.getAttribute("data-copy"), e.currentTarget)));
  document.querySelectorAll("[data-eye]").forEach((b) => (b.onclick = () => { const i = document.getElementById(b.getAttribute("data-eye")); i.type = i.type === "password" ? "text" : "password"; }));
}

let _mobFieldN = 0;
function mobField(label, value, secret) {
  const id = "mf-" + (_mobFieldN++);
  return `<div class="field"><label>${esc(label)}</label>
    <div style="display:flex;gap:8px">
      <input id="${id}" readonly ${secret ? 'type="password"' : ""} value="${esc(value)}" style="flex:1">
      ${secret ? `<button class="btn sm icon" data-eye="${id}" title="Показать/скрыть">👁️</button>` : ""}
      <button class="btn sm" data-copy="${esc(value)}">Копировать</button></div></div>`;
}

function confirmMobDelete(a) {
  modal("Удалить аккаунт", `<div style="padding:6px 0"><p>Удалить мобильный аккаунт <b>${esc(a.login)}</b> из пула?</p></div>`,
    async () => {
      const r = await api("mobile_delete", a.id);
      if (r.ok) { toast("Удалён", "ok"); closeModal(); renderMobAccounts(); }
      else { toast(r.error || "ошибка", "err"); return false; }
    }, "Удалить", true);
}

function renderMobRoadmap() {
  mobPluginMain("roadmap", `
    <div class="card"><div class="body" style="padding:18px;line-height:1.7">
      <h3 style="margin-top:0">Готово ✅</h3>
      <ul class="sub" style="padding-left:20px">
        <li><b>Пул аккаунтов</b> — тип входа, логин/пароль, подконтрольная почта, прокси, теги, состояние; просмотр/копирование доступов.</li>
        <li><b>Лоты</b> — привязка лота Playerok к пулу PUBG (срок аренды), счётчик свободных.</li>
        <li><b>Автовыдача</b> — при заказе воркер берёт свободный аккаунт, отдаёт доступ покупателю, помечает «в аренде».</li>
        <li><b>Код по почте</b> — <b>!код</b> читает OTP из подконтрольного ящика по IMAP и шлёт покупателю (кнопка 📧 проверяет вручную).</li>
        <li><b>Автовозврат</b> — по окончании срока аккаунт освобождается под следующую аренду + напоминание сменить пароль.</li>
      </ul>
      <h3>Можно усилить дальше</h3>
      <ul class="sub" style="padding-left:20px">
        <li>Автосмена пароля соц-логина при возврате (зависит от провайдера).</li>
        <li>Гарантия/очередь, если в пуле нет свободных (частично уже: открывается гарантийный случай).</li>
        <li>Свой чёрный список покупателей — уже есть (вкладка «Чёрный список» этого плагина).</li>
      </ul>
    </div></div>
  `);
}

/* лёгкая оболочка плагина без под-вкладок (для гарантии/репрайсера) */
function simplePluginShell(title, sub, body) {
  $("#main").innerHTML = `
    <div class="topbar"><div>
      <div style="margin-bottom:6px"><a href="#" id="back-plugins-s" style="color:var(--accent);text-decoration:none;font-size:12.5px">← Все плагины</a></div>
      <h1>${title}</h1><div class="sub">${sub}</div>
    </div></div>
    <div id="sbody">${body}</div>`;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === "plugins"));
  $("#back-plugins-s").onclick = (e) => { e.preventDefault(); go("plugins"); };
}

/* ---- плагин Гарантия/Автозамена ---- */
const WARR_REASON = { no_stock: "нет стока", buyer_replace: "замена по запросу", rent_error: "ошибка аренды" };
const WARR_ACTION = { notified: "уведомлён", resent_creds: "перевыдан доступ", retried: "повтор" };
VIEWS.warranty = renderWarranty;
async function renderWarranty() {
  simplePluginShell("🛡️ Гарантия / Автозамена", "автоповтор при нехватке стока и перевыдача по !замена",
    `<div class="card"><div class="body" style="padding:18px">Загрузка…</div></div>`);
  const [st, cs] = await Promise.all([api("warranty_settings_get"), api("warranty_list")]);
  const rows = (cs.items || []).length ? cs.items.map((w) => `
    <tr>
      <td class="mono">${new Date(w.ts * 1000).toLocaleString("ru-RU")}</td>
      <td><b>${esc(w.product || "—")}</b>${w.deal_id ? `<div class="mono">заказ ${esc(w.deal_id)}</div>` : ""}</td>
      <td>${esc(WARR_REASON[w.reason] || w.reason)}</td>
      <td>${esc(WARR_ACTION[w.action] || w.action)}</td>
      <td><span class="badge-state s-${w.status === "open" ? "paused" : "active"}">${w.status === "open" ? "открыт" : "закрыт"}</span></td>
      <td>${w.status === "open" ? `<button class="btn sm" data-wres="${w.id}">Закрыть</button>` : "—"}</td>
    </tr>`).join("") : `<tr><td colspan="6" class="empty">Гарантийных случаев нет.</td></tr>`;
  simplePluginShell("🛡️ Гарантия / Автозамена", "автоповтор при нехватке стока и перевыдача по !замена", `
    <div class="card"><h3>⚙️ Настройки</h3><div class="body" style="padding:18px">
      <label style="display:flex;align-items:center;gap:10px;cursor:pointer">
        <input type="checkbox" id="w-en" ${st.enabled ? "checked" : ""} style="width:auto">
        Автоповтор аренды при «нет стока»</label>
      <div class="field" style="margin-top:12px;max-width:220px"><label>Сколько раз повторять (0–5)</label>
        <input id="w-retry" type="number" min="0" max="5" value="${st.autoretry ?? 2}"></div>
      <button class="btn primary" id="w-save" style="margin-top:6px">Сохранить</button>
      <div class="sub" style="margin-top:10px">При нехватке аккаунта бот подождёт и попробует снова (сток KoSell пополняется). Покупатель командой <b>!замена</b> получает перевыдачу доступа + свежий код.</div>
    </div></div>
    <div class="card"><h3>📋 Гарантийные случаи</h3><div class="body">
      <table><thead><tr><th>Время</th><th>Игра / заказ</th><th>Причина</th><th>Действие</th><th>Статус</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
  `);
  $("#w-save").onclick = async () => {
    const r = await api("warranty_settings_set", { enabled: $("#w-en").checked, autoretry: +$("#w-retry").value });
    r.ok ? toast("Сохранено", "ok") : toast(r.error || "ошибка", "err");
  };
  document.querySelectorAll("[data-wres]").forEach((b) => (b.onclick = async () => {
    const r = await api("warranty_resolve", +b.dataset.wres);
    r.ok ? (toast("Закрыто", "ok"), renderWarranty()) : toast(r.error || "ошибка", "err");
  }));
}

/* ---- плагин Просьба об отзыве ---- */
const REVIEW_SUB = "вежливая просьба об отзыве после успешного завершения аренды";
VIEWS.review = renderReview;
async function renderReview() {
  simplePluginShell("⭐ Просьба об отзыве", REVIEW_SUB,
    `<div class="card"><div class="body" style="padding:18px">Загрузка…</div></div>`);
  const r = await api("review_get");
  if (!r.ok) return simplePluginShell("⭐ Просьба об отзыве", REVIEW_SUB,
    `<div class="card"><div class="body" style="padding:18px;color:var(--amber)">⚠️ ${esc(r.error || "ошибка")}</div></div>`);
  const text = r.text || r.default || "";
  simplePluginShell("⭐ Просьба об отзыве", REVIEW_SUB, `
    <div class="card"><h3>⚙️ Настройки</h3><div class="body" style="padding:18px">
      <label style="display:flex;align-items:center;gap:10px;cursor:pointer">
        <input type="checkbox" id="rev-en" ${r.enabled ? "checked" : ""} style="width:auto">
        Включить просьбу об отзыве</label>
      <div class="field" style="margin-top:16px">
        <label>Текст сообщения покупателю</label>
        <textarea id="rev-text" rows="4" placeholder="${esc(r.default || "")}">${esc(text)}</textarea>
        <div class="sub" style="margin-top:6px">Отправляется один раз, только после успешного завершения сделки (не при споре/возврате). Пусто — будет использован текст по умолчанию.</div>
      </div>
      <div style="display:flex;gap:10px;align-items:center;margin-top:8px">
        <button class="btn primary" id="rev-save">Сохранить</button>
        <button class="btn ghost" id="rev-default">Текст по умолчанию</button>
      </div>
      <div class="sub" style="margin-top:14px;color:var(--amber)">⚠️ Просим честный отзыв, без «платы за 5★» — это безопасно, в отличие от накрутки. Работает для всех видов аренды: Steam, KoSell и PUBG Mobile.</div>
    </div></div>
  `);
  $("#rev-save").onclick = async () => {
    const res = await api("review_set", { enabled: $("#rev-en").checked, text: $("#rev-text").value });
    res.ok ? toast("Сохранено", "ok") : toast(res.error || "ошибка", "err");
  };
  $("#rev-default").onclick = () => { $("#rev-text").value = r.default || ""; };
}

/* ---- плагин Репрайсер (рыночная статистика по всем лотам Playerok) ---- */
VIEWS.repricer = renderRepricer;
const RP_SUB = "рыночная статистика по всем твоим лотам Playerok";

async function renderRepricer() {
  simplePluginShell("📈 Репрайсер", RP_SUB,
    `<div class="card"><div class="body" style="padding:18px">Считываю твои лоты и похожие на Playerok… это может занять до минуты.</div></div>`);
  const r = await api("repricer_scan");
  if (!r.ok) return simplePluginShell("📈 Репрайсер", RP_SUB,
    `<div class="card"><div class="body" style="padding:18px;color:var(--amber)">⚠️ ${esc(r.message || "ошибка")}
      <div class="sub" style="margin-top:8px">Нужен подключённый Playerok (Настройки). Похожие лоты берутся из публичного каталога.</div></div></div>`);

  const fmt = (v) => v == null ? "—" : money(v) + "₽";
  const items = (r.items || []).slice().sort((a, b) => {
    const da = (a.avg != null && a.price) ? a.price - a.avg : -1e18;
    const db = (b.avg != null && b.price) ? b.price - b.avg : -1e18;
    return db - da; // сначала самые дорогие относительно средней — кандидаты снизить
  });
  const pos = (p) => {
    if (p.avg == null) return `<span class="sub">нет похожих</span>`;
    if (p.is_lowest) return `<span style="color:var(--green)">🔝 самый дешёвый</span>`;
    if (p.price > p.avg) return `<span style="color:var(--amber)">выше средней на ${money(p.price - p.avg)}₽</span>`;
    return `<span style="color:var(--green)">ниже средней</span>`;
  };
  const rows = items.length ? items.map((p) => `
    <tr>
      <td title="${esc(p.name || "")}">${esc((p.name || "").slice(0, 44))}${(p.name || "").length > 44 ? "…" : ""}
        <div class="sub">${esc(p.game || "")}${p.category ? " · " + esc(p.category) : ""}</div></td>
      <td><b>${fmt(p.price)}</b></td>
      <td>${p.similar_count || 0}</td>
      <td>${fmt(p.min)}</td>
      <td><b>${fmt(p.avg)}</b></td>
      <td>${fmt(p.max)}</td>
      <td>${pos(p)}</td>
    </tr>`).join("") : `<tr><td colspan="7" class="empty">Лотов не найдено. Проверь, что лоты опубликованы и Playerok подключён.</td></tr>`;

  simplePluginShell("📈 Репрайсер", RP_SUB, `
    <div class="toolbar">
      <button class="btn" id="rp-refresh">↻ Пересканировать</button>
      <span class="sub" style="margin-left:10px">Продавец: <b>${esc(r.username || "—")}</b> · лотов: ${r.my_count || 0}</span>
      <button class="btn" id="rp-kosell" style="margin-left:auto">Маржа KoSell →</button>
    </div>
    <div class="card"><div class="body">
      <table><thead><tr><th>Лот</th><th>Моя цена</th><th>Похожих</th><th>Мин</th><th>Средняя</th><th>Макс</th><th>Позиция</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
    <div class="sub">«Похожие» — лоты той же игры и категории Playerok у других продавцов (до 300 на категорию). Мин / средняя / макс — по их ценам. Это аналитика — цены никуда не пишутся, выставляешь сам. Отсортировано: сверху те, что дороже средней.</div>
  `);
  $("#rp-refresh").onclick = renderRepricer;
  $("#rp-kosell").onclick = renderRepricerKosell;
}

/* старый расчёт маржи по KoSell — вторичный экран */
async function renderRepricerKosell() {
  simplePluginShell("📈 Репрайсер — маржа KoSell", "рекомендованная цена по живым ценам KoSell",
    `<div class="card"><div class="body" style="padding:18px">Считаю по живым ценам KoSell…</div></div>`);
  const r = await api("kosell_pricing");
  if (!r.ok) return simplePluginShell("📈 Репрайсер — маржа KoSell", "рекомендованная цена по живым ценам KoSell",
    `<div class="card"><div class="body" style="padding:18px;color:var(--amber)">⚠️ ${esc(r.message || "ошибка")}</div></div>`);
  const cur = (c) => c === "USD" ? "$" : "₽";
  const rows = (r.items || []).length ? r.items.map((p) => `
    <tr>
      <td><b>${esc(p.product_name)}</b></td>
      <td class="mono">${esc(p.lot_id)}</td>
      <td>${p.hours} ч</td>
      <td>${money(p.cost)}${cur(p.currency)}</td>
      <td>+${money(p.markup)}%</td>
      <td><b style="color:var(--accent)">${money(p.suggested)}${cur(p.currency)}</b></td>
      <td style="color:var(--green)">+${money(p.margin)}${cur(p.currency)}</td>
    </tr>`).join("") : `<tr><td colspan="7" class="empty">Нет связанных лотов. Свяжите их в плагине KoSell → Товары.</td></tr>`;
  simplePluginShell("📈 Репрайсер — маржа KoSell", "рекомендованная цена по живым ценам KoSell", `
    <div class="toolbar"><button class="btn" id="rp-back">← К рынку</button>
      <button class="btn" id="rpk-refresh" style="margin-left:8px">↻ Пересчитать</button></div>
    <div class="card"><div class="body">
      <table><thead><tr><th>Игра</th><th>Лот Playerok</th><th>Срок</th><th>Себестоимость KoSell</th><th>Наценка</th><th>Рекоменд. цена</th><th>Маржа</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
    <div class="sub">Себестоимость берётся вживую у KoSell. Рекомендованная цена = себестоимость × (1 + наценка).</div>
  `);
  $("#rp-back").onclick = renderRepricer;
  $("#rpk-refresh").onclick = renderRepricerKosell;
}

/* оболочка плагина аренды: шапка + под-вкладки */
const RENTAL_TABS = [["rentals", "⏱️ Аренды"], ["lots", "🏷️ Лоты"], ["accounts", "👤 Аккаунты"],
                     ["rlogs", "📜 Логи аренды"], ["blacklist", "🚫 Чёрный список"], ["rsettings", "🔧 Настройки"]];
let rentalTab = "rentals";
function openRentalPlugin() { rentalTab = "rentals"; renderRentalTab(); }
function renderRentalTab() {
  ({ rentals: renderRentalsTab, lots: renderLotsTab, accounts: renderAccountsTab,
     rlogs: renderRentalLogsTab, blacklist: renderRentalBlacklistTab, rsettings: renderRentalSettingsTab
  }[rentalTab] || renderRentalsTab)();
}
VIEWS.rental = renderRentalTab;

function pluginMain(activeTab, bodyHtml) {
  const bar = RENTAL_TABS.map(([k, l]) => `<button class="${k === activeTab ? "active" : ""}" data-ptab="${k}">${l}</button>`).join("");
  $("#main").innerHTML = `
    <div class="topbar"><div>
      <div style="margin-bottom:6px"><a href="#" id="back-plugins" style="color:var(--accent);text-decoration:none;font-size:12.5px">← Все плагины</a></div>
      <h1>🔑 Аренда Steam-аккаунтов</h1><div class="sub">плагин аренды: выдача, таймеры, автовозврат</div>
    </div></div>
    <div class="tabs" style="flex-wrap:wrap;margin-bottom:16px">${bar}</div>
    <div id="pbody">${bodyHtml}</div>`;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === "plugins"));
  $("#back-plugins").onclick = (e) => { e.preventDefault(); go("plugins"); };
  document.querySelectorAll("[data-ptab]").forEach((b) => (b.onclick = () => { rentalTab = b.dataset.ptab; renderRentalTab(); }));
}

/* ===================== Плагин KoSell-аренда ===================== */
const KO_TABS = [["kmap", "🎮 Товары"], ["kactive", "⏱️ Активные аренды"], ["kbl", "🚫 Чёрный список"], ["ksettings", "🔧 Настройки"]];
let koTab = "kmap";
let koProducts = [];           // кэш каталога KoSell для формы маппинга
function openKoSellPlugin() { koTab = "kmap"; renderKoTab(); }
function renderKoTab() {
  ({ kmap: renderKoMapTab, kactive: renderKoActiveTab, kbl: renderKoBlacklistTab, ksettings: renderKoSettingsTab }[koTab] || renderKoMapTab)();
}
VIEWS.kosell = renderKoTab;

function koPluginMain(activeTab, bodyHtml) {
  const bar = KO_TABS.map(([k, l]) => `<button class="${k === activeTab ? "active" : ""}" data-ktab="${k}">${l}</button>`).join("");
  $("#main").innerHTML = `
    <div class="topbar"><div>
      <div style="margin-bottom:6px"><a href="#" id="back-plugins-k" style="color:var(--accent);text-decoration:none;font-size:12.5px">← Все плагины</a></div>
      <h1>🛒 KoSell-аренда</h1><div class="sub">реселл почасовой аренды Steam: KoSell → Playerok</div>
    </div></div>
    <div class="tabs" style="flex-wrap:wrap;margin-bottom:16px">${bar}</div>
    <div id="kbody">${bodyHtml}</div>`;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === "plugins"));
  $("#back-plugins-k").onclick = (e) => { e.preventDefault(); go("plugins"); };
  document.querySelectorAll("[data-ktab]").forEach((b) => (b.onclick = () => { koTab = b.dataset.ktab; renderKoTab(); }));
}

async function renderKoMapTab() {
  koPluginMain("kmap", `<div class="card"><div class="body" style="padding:18px">Загрузка…</div></div>`);
  const [mapR, prodR] = await Promise.all([api("kosell_map_list"), api("kosell_products")]);
  if (prodR.ok) koProducts = prodR.items;
  const noKey = !prodR.ok;
  const rows = (mapR.items || []).length ? mapR.items.map((m) => `
    <tr>
      <td><b>${esc(m.product_name || "#" + m.product_id)}</b> <span class="mono">#${m.product_id}</span></td>
      <td class="mono">${esc(m.lot_id)}</td>
      <td>${m.hours} ч</td>
      <td>+${money(m.markup_percent)}%</td>
      <td>${esc(m.currency)}</td>
      <td>${m.enabled ? "🟢" : "⏸️"}</td>
      <td><button class="btn sm icon danger" data-kdel="${esc(m.lot_id)}" title="Удалить">🗑️</button></td>
    </tr>`).join("") : `<tr><td colspan="7" class="empty">Нет соответствий. Свяжите лот Playerok с игрой KoSell.</td></tr>`;
  koPluginMain("kmap", `
    ${noKey ? `<div class="card" style="border-color:var(--amber)"><div class="body" style="padding:14px 18px;color:var(--amber)">⚠️ ${esc(prodR.message || "Задайте API-ключ KoSell")} — вкладка «Настройки».</div></div>` : ""}
    <div class="toolbar"><button class="btn primary" id="ko-add" ${noKey ? "disabled" : ""}>+ Связать лот с игрой</button>
      <span class="sub">Каталог KoSell: ${koProducts.length} игр</span></div>
    <div class="card"><div class="body">
      <table><thead><tr><th>Игра KoSell</th><th>Лот Playerok</th><th>Срок</th><th>Наценка</th><th>Валюта</th><th>Вкл</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
    <div class="sub">Когда на Playerok покупают связанный лот, бот берёт аренду у KoSell на указанный срок и отдаёт покупателю доступ. Цена = цена KoSell × срок × (1 + наценка).</div>
  `);
  if (!noKey) $("#ko-add").onclick = openKoMapForm;
  document.querySelectorAll("[data-kdel]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Удалить соответствие лота " + b.dataset.kdel + "?")) return;
    const r = await api("kosell_map_delete", { lot_id: b.dataset.kdel });
    r.ok ? (toast("Удалено", "ok"), renderKoMapTab()) : toast(r.error || "ошибка", "err");
  }));
}

function openKoMapForm() {
  const sorted = koProducts.slice().sort((a, b) => a.name.localeCompare(b.name));
  let selected = null;                    // выбранная игра KoSell

  modal("Связать лот Playerok с игрой KoSell", `
    <div class="field"><label>Игра KoSell <span class="sub">(${sorted.length} игр — начните вводить название)</span></label>
      <input id="km-search" placeholder="🔎 Поиск игры по названию…" autocomplete="off">
      <div id="km-results" class="km-results" style="display:none"></div>
      <div id="km-chosen" class="sub" style="margin-top:6px"></div>
    </div>
    <div class="field"><label>ID лота на Playerok</label><input id="km-lot" placeholder="например slug или id лота"></div>
    <div class="row2">
      <div class="field"><label>Срок аренды за 1 заказ (часы)</label><input id="km-hours" type="number" value="24" min="1"></div>
      <div class="field"><label>Наценка, %</label><input id="km-markup" type="number" value="150" min="0"></div>
    </div>
    <div class="field"><label>Валюта</label><select id="km-cur"><option value="RUB">RUB</option><option value="USD">USD</option></select></div>
    <div class="form-err" id="km-err"></div>
  `, async () => {
    if (!selected) { $("#km-err").textContent = "Выберите игру из списка поиска"; return false; }
    const data = {
      product_id: selected.id, product_name: selected.name, lot_id: $("#km-lot").value.trim(),
      hours: +$("#km-hours").value, markup_percent: +$("#km-markup").value, currency: $("#km-cur").value,
    };
    if (!data.lot_id) { $("#km-err").textContent = "Укажите id лота Playerok"; return false; }
    const r = await api("kosell_map_set", data);
    if (r.ok) { toast("Связано", "ok"); closeModal(); renderKoMapTab(); }
    else { $("#km-err").textContent = r.error; return false; }
  }, "Связать");

  // --- поиск по названию (автодополнение) ---
  const search = $("#km-search"), results = $("#km-results"), chosen = $("#km-chosen");
  let matches = [], active = -1;
  const pick = (p) => {
    selected = p;
    search.value = p.name;
    chosen.innerHTML = `✓ Выбрано: <b>${esc(p.name)}</b> <span class="mono">#${p.id}</span> — ${money(p.price_per_hour_rub)}₽/ч · своб. ${p.available}`;
    results.style.display = "none"; active = -1;
    $("#km-err").textContent = "";
  };
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    selected = null; chosen.textContent = "";
    if (!q) { results.style.display = "none"; return; }
    matches = sorted.filter((p) => p.name.toLowerCase().includes(q)).slice(0, 50);
    active = -1;
    results.innerHTML = matches.length
      ? matches.map((p, i) => `<div class="km-item" data-i="${i}">${esc(p.name)} <span class="sub">— ${money(p.price_per_hour_rub)}₽/ч · своб. ${p.available}</span></div>`).join("")
      : `<div class="km-item" style="cursor:default;color:var(--text-dim)">Ничего не найдено</div>`;
    results.style.display = "block";
    results.querySelectorAll("[data-i]").forEach((el) => (el.onclick = () => pick(matches[+el.dataset.i])));
  };
  const highlight = () => results.querySelectorAll(".km-item").forEach((el, i) => el.classList.toggle("active", i === active));
  search.oninput = draw;
  search.onkeydown = (e) => {
    if (results.style.display === "none" || !matches.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, matches.length - 1); highlight(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); highlight(); }
    else if (e.key === "Enter") { e.preventDefault(); pick(matches[active >= 0 ? active : 0]); }
    else if (e.key === "Escape") { results.style.display = "none"; }
  };
  setTimeout(() => search.focus(), 50);
}

async function renderKoActiveTab() {
  koPluginMain("kactive", `<div class="card"><div class="body" style="padding:18px">Загрузка активных аренд…</div></div>`);
  const r = await api("kosell_active");
  if (!r.ok) return koPluginMain("kactive", `<div class="card"><div class="body" style="padding:18px;color:var(--amber)">⚠️ ${esc(r.message || "ошибка")}</div></div>`);
  const now = Date.now();
  const rows = (r.items || []).length ? r.items.map((a) => {
    const left = Math.max(0, Math.round((a.time_remaining_seconds || 0) / 3600));
    return `<tr>
      <td><b>${esc(a.product_name)}</b></td>
      <td class="mono">${esc(a.steam_login)}</td>
      <td><span class="badge-state s-${a.status === "active" ? "active" : "paused"}">${esc(a.status)}</span></td>
      <td>${left} ч</td>
      <td style="white-space:nowrap">
        <button class="btn sm" data-kcode="${esc(a.rental_uid)}">🔑 Код</button>
        <button class="btn sm" data-kcred="${esc(a.rental_uid)}">👁️ Доступ</button>
      </td>
      <td style="white-space:nowrap">
        <button class="btn sm icon" data-kext="${esc(a.rental_uid)}" title="Продлить">➕</button>
        <button class="btn sm icon" data-klogout="${esc(a.rental_uid)}" title="Выход со всех устройств">🚪</button>
        <button class="btn sm icon danger" data-kterm="${esc(a.rental_uid)}" title="Завершить">⏹️</button>
      </td></tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">Нет активных аренд на KoSell.</td></tr>`;
  koPluginMain("kactive", `
    <div class="toolbar"><button class="btn" id="ko-refresh">↻ Обновить</button></div>
    <div class="card"><div class="body">
      <table><thead><tr><th>Игра</th><th>Логин Steam</th><th>Статус</th><th>Осталось</th><th>Покупателю</th><th>Управление</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
    <div class="sub">Это аренды на твоём аккаунте KoSell. «Код» — Steam Guard, «Доступ» — логин/пароль, «🚪» — выход со всех устройств, «⏹️» — завершить досрочно.</div>
  `);
  $("#ko-refresh").onclick = renderKoActiveTab;
  document.querySelectorAll("[data-kcode]").forEach((b) => (b.onclick = () => koShowCode(b.dataset.kcode)));
  document.querySelectorAll("[data-kcred]").forEach((b) => (b.onclick = () => koShowCreds(b.dataset.kcred)));
  document.querySelectorAll("[data-kext]").forEach((b) => (b.onclick = () => koExtend(b.dataset.kext)));
  document.querySelectorAll("[data-klogout]").forEach((b) => (b.onclick = () => koLogout(b.dataset.klogout)));
  document.querySelectorAll("[data-kterm]").forEach((b) => (b.onclick = () => koTerminate(b.dataset.kterm)));
}

async function koShowCode(uid) {
  infoModal("Steam Guard код", `<div style="text-align:center;padding:18px 0"><div style="font-size:30px">⏳</div></div>`);
  const r = await api("kosell_code", uid);
  if (!r.ok || !r.code) return infoModal("Код", `<div style="text-align:center;padding:16px 0;color:var(--amber)">${esc(r.message || r.error || "код недоступен")}</div>`);
  infoModal("Steam Guard код", `
    <div style="text-align:center;padding:6px 0">
      <div style="font-size:42px;font-weight:700;letter-spacing:7px;margin:14px 0;font-family:Consolas,monospace;color:var(--accent)">${esc(r.code)}</div>
      <div class="sub">Действует ещё ~${r.expires_in ?? "?"} c. Код выдаёт KoSell.</div>
    </div>`);
}

async function koShowCreds(uid) {
  const r = await api("kosell_credentials", uid);
  if (!r.ok) return toast(r.message || r.error || "ошибка", "err");
  credsModal("Доступ к аккаунту (от KoSell)", r.steam_login, r.steam_password,
    "Расходный аккаунт KoSell. По окончании аренды KoSell сам меняет пароль / выкидывает со всех устройств.");
}

function koExtend(uid) {
  modal("Продлить аренду", `
    <div class="field"><label>На сколько часов</label><input id="ke-h" type="number" value="24" min="1"></div>
    <div class="field"><label>Валюта</label><select id="ke-cur"><option value="RUB">RUB</option><option value="USD">USD</option></select></div>
    <div class="form-err" id="ke-err"></div>`, async () => {
    const r = await api("kosell_extend", { rental_uid: uid, hours: +$("#ke-h").value, currency: $("#ke-cur").value });
    if (r.ok) { toast("Продлено до " + (r.new_expires_at || ""), "ok"); closeModal(); renderKoActiveTab(); }
    else { $("#ke-err").textContent = r.message || r.error; return false; }
  }, "Продлить");
}

async function koTerminate(uid) {
  if (!confirm("Завершить аренду досрочно? Покупатель потеряет доступ.")) return;
  const r = await api("kosell_terminate", uid);
  r.ok ? (toast("Аренда завершена", "ok"), renderKoActiveTab()) : toast(r.message || r.error || "ошибка", "err");
}

async function koLogout(uid) {
  if (!confirm("Выкинуть аккаунт со всех устройств (на стороне KoSell)?")) return;
  const r = await api("kosell_logout", uid);
  r.ok ? toast(r.message || "готово", "ok") : toast(r.message || r.error || "ошибка", "err");
}

async function renderKoSettingsTab() {
  koPluginMain("ksettings", `<div class="card"><div class="body" style="padding:18px">Загрузка…</div></div>`);
  const s = await api("kosell_settings_get");
  koPluginMain("ksettings", `
    <div class="card"><h3>🔑 KoSell — доступ</h3><div class="body" style="padding:18px">
      <div class="field"><label>API-ключ ${s.key_set ? `<span class="sub">(задан: ${esc(s.key_mask)})</span>` : ""}</label>
        <input id="ks-key" type="password" placeholder="${s.key_set ? "•••• — оставьте пустым, чтобы не менять" : "kosell_..."}"></div>
      <div class="row2">
        <div class="field"><label>Валюта расчётов</label><select id="ks-cur">
          <option value="RUB" ${s.currency === "RUB" ? "selected" : ""}>RUB</option>
          <option value="USD" ${s.currency === "USD" ? "selected" : ""}>USD</option></select></div>
        <div class="field"><label>Наценка по умолчанию, %</label><input id="ks-markup" type="number" value="${esc(s.markup)}" min="0"></div>
      </div>
      <div class="field"><label>Прокси (необязательно)</label><input id="ks-proxy" value="${esc(s.proxy || "")}" placeholder="http://user:pass@host:port"></div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:6px">
        <button class="btn primary" id="ks-save">Сохранить</button>
        <button class="btn" id="ks-check">Проверить подключение</button>
        <button class="btn" id="ks-bal">Обновить баланс</button>
      </div>
      <div id="ks-status" class="sub" style="margin-top:12px"></div>
    </div></div>
    <div class="sub">Ключ берётся в кабинете KoSell → раздел API. Хранится в зашифрованном сейфе, в логи не попадает.</div>
  `);
  $("#ks-save").onclick = async () => {
    const data = { currency: $("#ks-cur").value, markup: $("#ks-markup").value, proxy: $("#ks-proxy").value };
    if ($("#ks-key").value.trim()) data.key = $("#ks-key").value.trim();
    const r = await api("kosell_settings_set", data);
    r.ok ? toast("Сохранено", "ok") : toast(r.error || "ошибка", "err");
  };
  $("#ks-check").onclick = async () => {
    $("#ks-status").textContent = "Проверяю…";
    const r = await api("kosell_check");
    $("#ks-status").innerHTML = r.connected
      ? `<span style="color:var(--green)">✓ ${esc(r.message)}</span>`
      : `<span style="color:var(--amber)">✕ ${esc(r.message)}</span>`;
  };
  $("#ks-bal").onclick = async () => {
    $("#ks-status").textContent = "Запрашиваю баланс…";
    const r = await api("kosell_balance");
    $("#ks-status").innerHTML = r.ok
      ? `<span style="color:var(--green)">Баланс: ${money(r.balance_rub)}₽ / $${money(r.balance_usd)} · ${esc(r.username || "")}</span>`
      : `<span style="color:var(--amber)">${esc(r.message || "ошибка")}</span>`;
  };
}

async function renderAccountsTab() {
  const r = await api("accounts");
  if (r.error) return fail(r.error);
  const rows = r.items.length ? r.items.map((a) => `
    <tr>
      <td class="mono">#${a.id}</td>
      <td><b>${esc(a.login)}</b>${a.note ? `<div class="mono">${esc(a.note)}</div>` : ""}</td>
      <td>${esc(a.game)}</td>
      <td><span class="badge-state s-${a.state}">${esc(a.state_ru)}</span></td>
      <td>${a.has_guard ? "🔑 Guard" : "—"} ${a.has_mafile ? "📄" : ""}</td>
      <td class="mono">${a.proxy ? esc(a.proxy) : "—"}</td>
      <td>${(a.tags || []).map((t) => `<span class="chip">${esc(t)}</span>`).join("") || "—"}</td>
      <td style="white-space:nowrap">
        <button class="btn sm" data-check="${a.id}">🔑 Код</button>
        <button class="btn sm" data-login="${a.id}">🔐 Вход</button>
      </td>
      <td style="white-space:nowrap">
        <button class="btn sm icon" data-creds="${a.id}" title="Показать логин и пароль">👁️</button>
        <button class="btn sm icon" data-edit="${a.id}" title="Настройки аккаунта">⚙️</button>
        <button class="btn sm icon" data-deauth="${a.id}" title="Выход со всех устройств">🚪</button>
        <button class="btn sm icon danger" data-del="${a.id}" title="Удалить аккаунт">🗑️</button>
      </td>
    </tr>`).join("") : `<tr><td colspan="9" class="empty">Пул пуст. Добавьте аккаунт или нажмите «Демо-данные».</td></tr>`;

  pluginMain("accounts", `
    <div class="toolbar">
      <button class="btn primary" id="add-acc">+ Добавить аккаунт</button>
      <button class="btn" id="seed">Демо-данные</button>
    </div>
    <div class="card"><div class="body">
      <table><thead><tr><th>ID</th><th>Логин</th><th>Игра</th><th>Состояние</th><th>Секреты</th><th>Прокси</th><th>Теги</th><th>Проверка</th><th>Действия</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
  `);
  $("#add-acc").onclick = openAddAccount;
  $("#seed").onclick = async () => { const s = await api("seed_demo"); s.ok ? (toast("Демо-данные добавлены", "ok"), renderAccountsTab(), refreshBadges()) : toast(s.error, "err"); };
  const byId = Object.fromEntries(r.items.map((a) => [a.id, a]));
  document.querySelectorAll("[data-check]").forEach((b) => (b.onclick = () => openCheck(+b.dataset.check)));
  document.querySelectorAll("[data-login]").forEach((b) => (b.onclick = () => openLoginCheck(+b.dataset.login)));
  document.querySelectorAll("[data-creds]").forEach((b) => (b.onclick = () => openAccountCreds(+b.dataset.creds)));
  document.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => openAccountSettings(byId[+b.dataset.edit])));
  document.querySelectorAll("[data-deauth]").forEach((b) => (b.onclick = () => openDeauth(byId[+b.dataset.deauth])));
  document.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => confirmDeleteAccount(byId[+b.dataset.del])));
};

function openDeauth(a) {
  modal("Выход со всех устройств — " + a.login, `
    <div style="padding:6px 0">
      <p>Выйти со всех устройств аккаунта <b>${esc(a.login)}</b> (${esc(a.game)})?</p>
      <div class="sub" style="margin-top:10px;line-height:1.6">
        Бот войдёт через Guard и повторит действие сайта Steam «Выйти из аккаунта везде» —
        завершатся <b>все</b> сессии (и твоя, и арендатора). <b>Пароль и мобильный аутентификатор
        не трогаются</b>; вход заново проходит штатно. Это не удаление аутентификатора.
      </div>
      <div class="sub" style="margin-top:8px">Нужен рабочий maFile и Node-сайдкар. До ~1 минуты.</div>
    </div>`, async () => {
    infoModal("Выхожу со всех устройств…", `
      <div style="text-align:center;padding:18px 0">
        <div style="font-size:34px">⏳</div>
        <div class="sub" style="margin-top:12px">Вхожу через Guard и завершаю сессии…<br>Окно обновится само.</div>
      </div>`);
    const r = await api("account_deauth", a.id);
    if (r.error || !r.ok) {
      return infoModal("Не удалось", `
        <div style="text-align:center;padding:16px 0">
          <div style="font-size:44px">⚠️</div>
          <div style="margin-top:14px;font-size:15px;color:var(--amber)">${esc(r.message || r.error || "")}</div>
          <div class="sub" style="margin-top:14px">Проверьте пароль, maFile, прокси и что Node-сайдкар установлен.</div>
        </div>`);
    }
    const ses = r.sessions || [];
    const rows = ses.length
      ? ses.map((s) => `<tr>
          <td>${esc(s.description || "—")}</td>
          <td class="mono">${s.platform ?? "—"}</td>
          <td>${s.logged_in ? "🟢 был вход" : "—"}</td>
          <td>${esc([s.city, s.country].filter(Boolean).join(", ") || "—")}</td></tr>`).join("")
      : `<tr><td colspan="4" class="empty">Активных чужих сессий не было.</td></tr>`;
    infoModal("Выход выполнен — " + a.login, `
      <div style="padding:4px 0">
        <div style="text-align:center;margin-bottom:12px">
          <div style="font-size:40px">✅</div>
          <div style="margin-top:8px;font-size:15px;color:var(--green)">${esc(r.message || "Выход выполнен со всех устройств")}</div>
        </div>
        <div class="sub" style="margin-bottom:8px">Завершённые сессии${ses.length ? "" : " — не найдено"}:</div>
        <table><thead><tr><th>Устройство</th><th>Платф.</th><th>Статус</th><th>Геолокация</th></tr></thead>
        <tbody>${rows}</tbody></table>
        <div class="sub" style="margin-top:14px;line-height:1.6">
          Арендатор выкинут. Пароль и аутентификатор не менялись — для нового входа выдай логин/пароль и код Guard как обычно.
        </div>
      </div>`);
    return false;
  }, "Выйти везде", true);
}

const EDITABLE_STATES = [["free", "свободен"], ["paused", "выключен"], ["quarantine", "карантин"], ["resting", "отдых"]];
function openAccountSettings(a) {
  const stateOpts = EDITABLE_STATES.map(([v, ru]) => `<option value="${v}" ${a.state === v ? "selected" : ""}>${ru}</option>`).join("")
    + (EDITABLE_STATES.some(([v]) => v === a.state) ? "" : `<option value="${a.state}" selected>${esc(a.state_ru)} (текущее)</option>`);
  modal("Настройки аккаунта — " + a.login, `
    <div class="row2">
      <div class="field"><label>Логин (не меняется)</label><input value="${esc(a.login)}" disabled></div>
      <div class="field"><label>Игра / метка пула</label><input id="e-game" value="${esc(a.game)}"></div>
    </div>
    <div class="row2">
      <div class="field"><label>Состояние</label><select id="e-state">${stateOpts}</select></div>
      <div class="field"><label>Теги (через запятую)</label><input id="e-tags" value="${esc((a.tags || []).join(", "))}"></div>
    </div>
    <div class="field"><label>Прокси</label><input id="e-proxy" value="${esc(a.proxy || "")}" placeholder="http://user:pass@host:port"></div>
    <div class="field"><label>Заметка</label><input id="e-note" value="${esc(a.note || "")}"></div>
    <div class="field"><label>Новый пароль (пусто — не менять)</label><input id="e-pass" type="password" placeholder="••••••"></div>
    <div class="field"><label>Обновить maFile (пусто — не менять)</label><textarea id="e-mafile" rows="2" placeholder='{"shared_secret":"...","identity_secret":"..."}'></textarea></div>
    <div class="form-err" id="e-err"></div>
  `, async () => {
    const data = {
      account_id: a.id, game: $("#e-game").value, state: $("#e-state").value,
      tags: $("#e-tags").value, proxy: $("#e-proxy").value, note: $("#e-note").value,
      password: $("#e-pass").value, mafile: $("#e-mafile").value.trim(),
    };
    const res = await api("account_update", data);
    if (res.ok) { toast("Сохранено", "ok"); closeModal(); renderAccountsTab(); refreshBadges(); }
    else { $("#e-err").textContent = res.error; return false; }
  });
}

function confirmDeleteAccount(a) {
  modal("Удалить аккаунт", `
    <div style="padding:6px 0">
      <p>Удалить аккаунт <b>${esc(a.login)}</b> (${esc(a.game)}) из пула?</p>
      <p class="sub" style="margin-top:10px">Действие необратимо. Секреты и maFile этого аккаунта будут стёрты из базы.</p>
      <div class="form-err" id="del-err"></div>
    </div>
  `, async () => {
    const res = await api("account_delete", a.id);
    if (res.ok) { toast("Аккаунт удалён", "ok"); closeModal(); renderAccountsTab(); refreshBadges(); }
    else { $("#del-err").textContent = res.error; return false; }
  }, "Удалить", true);
}

async function openLoginCheck(id) {
  infoModal("Проверка входа в Steam", `
    <div style="text-align:center;padding:18px 0">
      <div style="font-size:34px">⏳</div>
      <div class="sub" style="margin-top:12px">Пробую войти через Guard${""} и прокси…<br>Это может занять до ~30 секунд. Окно обновится само.</div>
    </div>`);
  const r = await api("account_login_check", id);
  if (r.error) { closeModal(); return toast(r.error, "err"); }
  const good = r.login_ok;
  infoModal("Проверка входа — " + (good ? "успех" : "не удалось"), `
    <div style="text-align:center;padding:16px 0">
      <div style="font-size:44px">${good ? "✅" : "⚠️"}</div>
      <div style="margin-top:14px;font-size:15px;color:${good ? "var(--green)" : "var(--amber)"}">${esc(r.message)}</div>
      <div class="sub" style="margin-top:14px">${good
        ? "Логин, пароль, Steam Guard и прокси рабочие — бот сможет управлять аккаунтом."
        : "Проверьте пароль, maFile и прокси. Частые причины: неверный пароль, протухший maFile, недоступный прокси, лимит Steam."}</div>
    </div>`);
}

async function openCheck(id) {
  const r = await api("account_check", id);
  if (r.error) return toast(r.error, "err");
  if (!r.has_guard) return toast(r.message || "У аккаунта нет Guard", "err");
  let left = r.seconds_left;
  infoModal("Проверка аккаунта — " + r.login, `
    <div style="text-align:center;padding:4px 0">
      <div class="sub">Steam Guard код (как в мобильном приложении):</div>
      <div id="gc-code" style="font-size:42px;font-weight:700;letter-spacing:7px;margin:16px 0;font-family:'Cascadia Code',Consolas,monospace;color:var(--accent)">${esc(r.code)}</div>
      <div class="util" style="max-width:280px;margin:0 auto"><div class="track"><div class="fill" id="gc-bar" style="width:${left / 30 * 100}%"></div></div><span id="gc-sec">${left}</span></div>
      <div class="sub" style="margin-top:16px;color:var(--green)">✓ maFile валиден — бот сможет выдавать коды по команде !код во время аренды</div>
    </div>
  `);
  _modalTimer = setInterval(async () => {
    left--;
    if (left <= 0) {
      const r2 = await api("account_check", id);
      if (r2.code) { const el = $("#gc-code"); if (el) el.textContent = r2.code; left = r2.seconds_left; }
      else left = 30;
    }
    const bar = $("#gc-bar"); if (bar) bar.style.width = (left / 30 * 100) + "%";
    const sec = $("#gc-sec"); if (sec) sec.textContent = left;
  }, 1000);
}

function openAddAccount() {
  modal("Добавить аккаунт", `
    <div class="row2">
      <div class="field"><label>Логин Steam *</label><input id="f-login"></div>
      <div class="field"><label>Игра / метка пула *</label><input id="f-game" placeholder="cs2, dota2, faceit"></div>
    </div>
    <div class="field"><label>Пароль *</label><input id="f-pass" type="password"><div class="tip">Хранится зашифрованным, на эту машину.</div></div>
    <div class="field"><label>maFile (JSON, необязательно)</label><textarea id="f-mafile" rows="3" placeholder='{"shared_secret":"...","identity_secret":"..."}'></textarea><div class="tip">Нужен для Guard-кодов и подтверждений. Парсится локально.</div></div>
    <div class="row2">
      <div class="field"><label>Прокси</label><input id="f-proxy" placeholder="http://user:pass@host:port"></div>
      <div class="field"><label>Теги (через запятую)</label><input id="f-tags" placeholder="prime, faceit_verified"></div>
    </div>
    <div class="field"><label>Заметка</label><input id="f-note"></div>
    <div class="form-err" id="f-err"></div>
  `, async () => {
    const data = {
      login: $("#f-login").value, game: $("#f-game").value, password: $("#f-pass").value,
      mafile: $("#f-mafile").value.trim(), proxy: $("#f-proxy").value,
      tags: $("#f-tags").value, note: $("#f-note").value,
    };
    const r = await api("add_account", data);
    if (r.ok) { toast("Аккаунт добавлен", "ok"); closeModal(); renderAccountsTab(); refreshBadges(); }
    else { $("#f-err").textContent = r.error; return false; }
  });
}

/* ---------- лоты ---------- */
async function renderLotsTab() {
  const r = await api("lots");
  if (r.error) return fail(r.error);
  const rows = r.items.length ? r.items.map((l) => `
    <tr><td class="mono">${esc(l.marketplace)}</td><td class="mono">${esc(l.lot_id)}</td>
    <td>${esc(l.title) || "—"}</td><td>${esc(l.game)}</td><td>${l.hours} ч</td>
    <td><span class="chip">${esc(l.rotation)}</span></td></tr>`).join("")
    : `<tr><td colspan="6" class="empty">Нет привязанных лотов. Бот реагирует только на лоты из этого списка.</td></tr>`;
  pluginMain("lots", `
    <div class="toolbar"><button class="btn primary" id="add-lot">+ Привязать лот</button></div>
    <div class="card"><div class="body">
      <table><thead><tr><th>Площадка</th><th>ID лота</th><th>Название</th><th>Пул</th><th>Срок</th><th>Ротация</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
  `);
  $("#add-lot").onclick = () => modal("Привязать лот", `
    <div class="row2">
      <div class="field"><label>Площадка</label><input id="l-mk" value="playerok"></div>
      <div class="field"><label>ID лота *</label><input id="l-id"></div>
    </div>
    <div class="field"><label>Название</label><input id="l-title"></div>
    <div class="row2">
      <div class="field"><label>Пул (игра) *</label><input id="l-game" placeholder="cs2"></div>
      <div class="field"><label>Срок аренды, часов *</label><input id="l-hours" type="number" step="0.5"></div>
    </div>
    <div class="field"><label>Ротация</label><select id="l-rot"><option value="auto">auto — бот сам меняет пароль</option><option value="semi">semi — бот готовит, человек жмёт</option><option value="manual">manual — только напоминание</option></select></div>
    <div class="form-err" id="l-err"></div>
  `, async () => {
    const r2 = await api("bind_lot", {
      marketplace: $("#l-mk").value, lot_id: $("#l-id").value, title: $("#l-title").value,
      game: $("#l-game").value, hours: $("#l-hours").value, rotation: $("#l-rot").value,
    });
    if (r2.ok) { toast("Лот привязан", "ok"); closeModal(); renderLotsTab(); refreshBadges(); }
    else { $("#l-err").textContent = r2.error; return false; }
  });
}

/* ---------- аренды ---------- */
let rentTab = "active";
async function renderRentalsTab() {
  const r = await api("rentals");
  if (r.error) return fail(r.error);
  const g = r.groups;
  const labels = { active: "Активные", queued: "В очереди", ended: "Завершённые", cancelled: "Отменённые" };
  const tabs = Object.keys(labels).map((k) =>
    `<button class="${k === rentTab ? "active" : ""}" data-tab="${k}">${labels[k]} (${g[k].length})</button>`).join("");
  const list = g[rentTab];
  const rows = list.length ? list.map((x) => `
    <tr><td class="mono">#${x.id}</td><td>${esc(x.buyer_name) || "—"}</td><td class="mono">${esc(x.lot_id)}</td>
    <td>${x.account_id ? "#" + x.account_id : "—"}</td>
    <td>${x.started_at ? new Date(x.started_at * 1000).toLocaleString("ru-RU") : "—"}</td>
    <td>${x.ends_at ? new Date(x.ends_at * 1000).toLocaleString("ru-RU") : "—"}</td>
    <td>${money(x.price)} ₽</td></tr>`).join("")
    : `<tr><td colspan="7" class="empty">Нет аренд в этой категории</td></tr>`;
  pluginMain("rentals", `
    <div class="tabs">${tabs}</div>
    <div class="card"><div class="body">
      <table><thead><tr><th>ID</th><th>Покупатель</th><th>Лот</th><th>Аккаунт</th><th>Начало</th><th>Конец</th><th>Цена</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
  `);
  document.querySelectorAll("#pbody .tabs button").forEach((b) => (b.onclick = () => { rentTab = b.dataset.tab; renderRentalsTab(); }));
}

/* ---------- логи аренды ---------- */
async function renderRentalLogsTab() {
  const r = await api("rental_events");
  const rows = (r.items && r.items.length) ? r.items.map((e) => `
    <tr><td class="mono">${new Date(e.ts * 1000).toLocaleString("ru-RU")}</td>
    <td><span class="chip">${esc(e.kind)}</span></td>
    <td class="mono">${e.rental_id ? "аренда #" + e.rental_id : ""} ${e.account_id ? "акк #" + e.account_id : ""}</td>
    <td class="mono">${esc(e.data || "")}</td></tr>`).join("")
    : `<tr><td colspan="4" class="empty">Событий аренды пока нет</td></tr>`;
  pluginMain("rlogs", `<div class="card"><div class="body">
    <table><thead><tr><th>Время</th><th>Событие</th><th>Объект</th><th>Данные</th></tr></thead><tbody>${rows}</tbody></table>
  </div></div>`);
}

/* ---------- чёрный список аренды (у каждого плагина свой, scope) ---------- */
// shellFn — оболочка плагина (pluginMain/koPluginMain/mobPluginMain); activeTab — ключ её вкладки;
// rerender — функция перерисовки этой же вкладки.
async function renderScopedBlacklist(scope, shellFn, activeTab, rerender) {
  const r = await api("blacklist_list", scope);
  const items = (r && r.items) || [];
  const rows = items.length ? items.map((x) => `
    <tr>
      <td class="mono">${esc(x.buyer_id)}</td>
      <td>${esc(x.reason)}</td>
      <td><button class="btn sm" data-bl-rm="${esc(x.buyer_id)}">Убрать</button></td>
    </tr>`).join("") : `<tr><td colspan="3" class="empty">ЧС пуст. Добавленный покупатель блокируется только в этой аренде.</td></tr>`;
  shellFn(activeTab, `
    <div class="toolbar"><button class="btn primary" id="bl-add">+ Добавить в ЧС</button></div>
    <div class="sub" style="margin:-6px 0 14px;line-height:1.5">Свой чёрный список <b>этой аренды</b>. Скамеру автоматически не выдаём и зовём тебя. На другие плагины (аренды) не влияет — у каждой свой ЧС.</div>
    <div class="card"><div class="body">
      <table><thead><tr><th>Покупатель (ID)</th><th>Причина (за что)</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div></div>
  `);
  $("#bl-add").onclick = () => modal("Добавить в чёрный список", `
    <div class="field"><label>ID покупателя *</label><input id="bl-id"></div>
    <div class="field"><label>Причина — за что в ЧС *</label><input id="bl-reason" placeholder="не вернул аккаунт, чарджбэк, угон…"></div>
    <div class="form-err" id="bl-err"></div>
  `, async () => {
    if (!$("#bl-reason").value.trim()) { $("#bl-err").textContent = "Укажите причину — за что добавляете"; return false; }
    const res = await api("blacklist_add", { scope, buyer_id: $("#bl-id").value, reason: $("#bl-reason").value });
    if (res.ok) { toast("Добавлен в ЧС", "ok"); closeModal(); rerender(); }
    else { $("#bl-err").textContent = res.error; return false; }
  }, "Добавить");
  document.querySelectorAll("[data-bl-rm]").forEach((b) => (b.onclick = async () => {
    await api("blacklist_remove", scope, "playerok", b.dataset.blRm); toast("Убран из ЧС", "ok"); rerender();
  }));
}

function renderRentalBlacklistTab() { renderScopedBlacklist("steam", pluginMain, "blacklist", renderRentalBlacklistTab); }
function renderKoBlacklistTab() { renderScopedBlacklist("kosell", koPluginMain, "kbl", renderKoBlacklistTab); }
function renderMobBlacklistTab() { renderScopedBlacklist("mobile", mobPluginMain, "mbl", renderMobBlacklistTab); }

/* ---------- настройки аренды ---------- */
async function renderRentalSettingsTab() {
  const r = await api("rental_settings_get");
  if (r.error) return fail(r.error);
  pluginMain("rsettings", `
    <div class="card"><h3>Политика аренды</h3><div class="body" style="padding:18px">
      <div class="field" style="max-width:560px">
        <label style="display:flex;align-items:center;gap:10px;cursor:pointer;font-weight:600">
          <input id="rs-autokick" type="checkbox" ${r.autokick ? "checked" : ""} style="width:auto;margin:0">
          Авто-кик по окончании аренды (выход со всех устройств)
        </label>
      </div>
      <div class="tip" style="margin:7px 0 18px;max-width:560px">По окончании аренды бот выкидывает арендатора со всех устройств Steam — <b>без смены пароля</b> и без удаления аутентификатора (вход заново штатный). Нужен рабочий maFile и Node-сайдкар. Если кик не удастся — бот подстрахуется прежней схемой (ротация/карантин). Касается только Steam-аренды.</div>
      <div class="field" style="max-width:360px"><label>Бонус за отзыв 5★ — часов к аренде</label>
        <input id="rs-bonus" type="number" step="0.5" min="0" max="24" value="${r.bonus_5star_hours}"></div>
      <div class="tip" style="margin:7px 0 16px;max-width:460px">Когда покупатель оставит отзыв на 5 звёзд, аренда автоматически продлевается на это число часов. 0 — выключено. Применяется при обработке отзыва площадки.</div>
      <button class="btn primary" id="rs-save">Сохранить</button>
    </div></div>`);
  $("#rs-save").onclick = async () => {
    const res = await api("rental_settings_set", {
      bonus_5star_hours: $("#rs-bonus").value,
      autokick: $("#rs-autokick").checked,
    });
    res.ok ? toast("Сохранено", "ok") : toast(res.error, "err");
  };
}

/* ---------- автоматизация: автовыдача + автоответчик + автоподнятие ---------- */
const DELIV_RU = { text: "текст", keys: "ключи/аккаунты", file: "файл" };
VIEWS.automation = async () => {
  const [dl, ar, bl] = await Promise.all([api("delivery_list"), api("autoresponses_list"), api("bump_list")]);
  if (dl.error) return fail(dl.error);

  const delRows = dl.items.length ? dl.items.map((d) => `
    <tr><td class="mono">${esc(d.lot_id)}</td><td>${DELIV_RU[d.mode] || d.mode}</td>
    <td>${d.mode === "keys" ? `<b>${d.stock ?? 0}</b> шт` : (d.mode === "file" ? esc(d.template) : `«${esc((d.template || "").slice(0, 40))}…»`)}</td>
    <td><span class="badge-state ${d.enabled ? "s-free" : "s-paused"}">${d.enabled ? "вкл" : "выкл"}</span></td>
    <td>${d.mode === "keys" ? `<button class="btn sm" data-stock="${esc(d.lot_id)}">+ ключи</button>` : ""}</td></tr>`).join("")
    : `<tr><td colspan="5" class="empty">Автовыдача не настроена. Бот сам отдаст товар покупателю после оплаты.</td></tr>`;

  const greet = ar.items.filter((r) => r.kind === "greeting");
  const kw = ar.items.filter((r) => r.kind === "keyword");
  const arRows = (rules) => rules.length ? rules.map((r) => `
    <tr>${r.kind === "keyword" ? `<td class="mono">${esc(r.pattern)}</td>` : ""}
    <td>${esc(r.reply)}</td>
    <td><button class="btn sm ghost" data-del-ar="${r.id}">удалить</button></td></tr>`).join("") : "";

  const bumpRows = bl.items.length ? bl.items.map((b) => {
    const h = (b.interval_sec / 3600).toFixed(b.interval_sec % 3600 ? 1 : 0);
    return `<tr><td class="mono">${esc(b.lot_id)}</td>
      <td><span class="badge-state ${b.enabled ? "s-free" : "s-paused"}">${b.enabled ? "вкл" : "выкл"}</span></td>
      <td>каждые ${h} ч</td>
      <td>${b.last_bump ? new Date(b.last_bump * 1000).toLocaleString("ru-RU") : "—"}</td>
      <td><button class="btn sm" data-bump="${esc(b.lot_id)}">изменить</button></td></tr>`;
  }).join("") : `<tr><td colspan="5" class="empty">Автоподнятие выключено. Включайте с осторожностью (антибан).</td></tr>`;

  setMain("Автоматизация", "бесплатные функции площадки", `
    <div class="card"><h3>📦 Автовыдача после оплаты</h3><div class="body">
      <div style="padding:12px 18px"><button class="btn primary" id="add-deliv">+ Настроить лот</button></div>
      <table><thead><tr><th>Лот</th><th>Режим</th><th>Содержимое / склад</th><th>Статус</th><th></th></tr></thead><tbody>${delRows}</tbody></table>
    </div></div>

    <div class="card"><h3>💬 Автоответчик</h3><div class="body">
      <div style="padding:12px 18px"><button class="btn primary" id="add-ar">+ Добавить ответ</button></div>
      <table><thead><tr><th>Приветствие нового покупателя</th><th></th></tr></thead>
        <tbody>${arRows(greet) || `<tr><td colspan="2" class="empty">Приветствие не задано</td></tr>`}</tbody></table>
      <table style="margin-top:4px"><thead><tr><th>Ключевое слово</th><th>Ответ</th><th></th></tr></thead>
        <tbody>${arRows(kw) || `<tr><td colspan="3" class="empty">Правил по ключевым словам нет</td></tr>`}</tbody></table>
    </div></div>

    <div class="card"><h3>🔼 Автоподнятие / перевыставление</h3><div class="body">
      <div style="padding:12px 18px;display:flex;gap:10px;align-items:center">
        <button class="btn primary" id="add-bump">+ Настроить лот</button>
        <span class="sub" style="color:var(--amber);font-size:12px">⚠️ По умолчанию выключено. Частое поднятие повышает риск бана — ставьте разумный интервал.</span>
      </div>
      <table><thead><tr><th>Лот</th><th>Статус</th><th>Интервал</th><th>Последнее</th><th></th></tr></thead><tbody>${bumpRows}</tbody></table>
    </div></div>
  `);

  $("#add-deliv").onclick = () => deliveryModal();
  $("#add-ar").onclick = () => arModal();
  $("#add-bump").onclick = () => bumpModal();
  document.querySelectorAll("[data-stock]").forEach((b) => (b.onclick = () => stockModal(b.dataset.stock)));
  document.querySelectorAll("[data-del-ar]").forEach((b) => (b.onclick = async () => { await api("autoresponse_delete", +b.dataset.delAr); toast("Удалено", "ok"); go("automation"); }));
  document.querySelectorAll("[data-bump]").forEach((b) => (b.onclick = () => bumpModal(b.dataset.bump)));
};

function deliveryModal() {
  modal("Настроить автовыдачу", `
    <div class="field"><label>ID лота *</label><input id="d-lot"></div>
    <div class="field"><label>Режим выдачи</label><select id="d-mode">
      <option value="text">Текст-шаблон (одинаковый всем)</option>
      <option value="keys">Ключи/аккаунты со склада (по одному на заказ)</option>
      <option value="file">Файл (логин-пароль файлом)</option></select></div>
    <div class="field"><label id="d-tpl-label">Текст (плейсхолдеры: {buyer}, {price})</label>
      <textarea id="d-tpl" rows="3" placeholder="Спасибо, {buyer}! Ваш товар: ..."></textarea></div>
    <div class="form-err" id="d-err"></div>
  `, async () => {
    const r = await api("delivery_set", { lot_id: $("#d-lot").value, mode: $("#d-mode").value, template: $("#d-tpl").value, enabled: true });
    if (r.ok) { toast("Автовыдача настроена", "ok"); closeModal(); go("automation"); }
    else { $("#d-err").textContent = r.error; return false; }
  });
  const lbl = $("#d-tpl-label"), tpl = $("#d-tpl");
  $("#d-mode").onchange = (e) => {
    const m = e.target.value;
    if (m === "keys") { lbl.textContent = "Для режима «ключи» шаблон не нужен — добавьте ключи кнопкой «+ ключи» после сохранения"; tpl.style.display = "none"; }
    else if (m === "file") { lbl.textContent = "Путь к файлу на этой машине"; tpl.style.display = "block"; tpl.placeholder = "C:/goods/account.txt"; }
    else { lbl.textContent = "Текст (плейсхолдеры: {buyer}, {price})"; tpl.style.display = "block"; tpl.placeholder = "Спасибо, {buyer}! Ваш товар: ..."; }
  };
}

function stockModal(lotId) {
  modal("Добавить ключи на склад — лот " + lotId, `
    <div class="field"><label>По одному на строку (каждая строка — один заказ)</label>
      <textarea id="s-keys" rows="7" placeholder="login1:pass1&#10;login2:pass2&#10;KEY-XXXX-YYYY"></textarea></div>
    <div class="field tip">Значения шифруются и выдаются по одному на каждую оплату.</div>
    <div class="form-err" id="s-err"></div>
  `, async () => {
    const r = await api("stock_add", "playerok", lotId, $("#s-keys").value);
    if (r.ok) { toast(`Добавлено ${r.added}, на складе ${r.stock}`, "ok"); closeModal(); go("automation"); }
    else { $("#s-err").textContent = r.error; return false; }
  });
}

function arModal() {
  modal("Добавить автоответ", `
    <div class="field"><label>Тип</label><select id="a-kind">
      <option value="greeting">Приветствие нового покупателя</option>
      <option value="keyword">Ответ по ключевому слову</option></select></div>
    <div class="field" id="a-pat-wrap" style="display:none"><label>Ключевое слово / фраза</label><input id="a-pat" placeholder="prime"></div>
    <div class="field"><label>Текст ответа ({buyer} — имя покупателя)</label><textarea id="a-reply" rows="3"></textarea></div>
    <div class="form-err" id="a-err"></div>
  `, async () => {
    const r = await api("autoresponse_add", { kind: $("#a-kind").value, pattern: $("#a-pat").value, reply: $("#a-reply").value });
    if (r.ok) { toast("Ответ добавлен", "ok"); closeModal(); go("automation"); }
    else { $("#a-err").textContent = r.error; return false; }
  });
  $("#a-kind").onchange = (e) => { $("#a-pat-wrap").style.display = e.target.value === "keyword" ? "block" : "none"; };
}

function bumpModal(lotId) {
  modal("Настроить автоподнятие", `
    <div class="field"><label>ID лота *</label><input id="b-lot" value="${esc(lotId || "")}"></div>
    <div class="field"><label><input type="checkbox" id="b-en" style="width:auto;margin-right:6px">Включить автоподнятие</label></div>
    <div class="field"><label>Интервал, минут (мин. 30 — антибан)</label><input id="b-int" type="number" value="240" min="30"></div>
    <div class="field tip">Поднимает лот бесплатным статусом приоритета. Слишком часто — риск санкций площадки.</div>
    <div class="form-err" id="b-err"></div>
  `, async () => {
    const r = await api("bump_set", { lot_id: $("#b-lot").value, enabled: $("#b-en").checked, interval_min: +$("#b-int").value });
    if (r.ok) { toast("Сохранено", "ok"); closeModal(); go("automation"); }
    else { $("#b-err").textContent = r.error; return false; }
  });
}

/* ---------- настройки (Telegram) ---------- */
VIEWS.settings = async () => {
  const s = await api("settings_get");
  if (s.error) return fail(s.error);
  const sec = await api("security_get");
  setMain("Настройки", "подключения, безопасность и уведомления", `
    <div class="card"><h3>🔒 Безопасность входа</h3><div class="body" style="padding:18px">
      <div class="field" style="margin-bottom:14px"><label>Логин</label>
        <input id="sec-user" value="${esc(sec.username || "")}" placeholder="имя для входа"></div>
      <label style="display:flex;align-items:center;gap:9px;margin-bottom:8px">
        <input type="checkbox" id="sec-2fa" style="width:auto" ${sec.twofa_enabled ? "checked" : ""}>
        Подтверждение входа кодом из Telegram (2FA)
      </label>
      <div class="tip" style="margin-bottom:16px">${sec.tg_ready
        ? "При входе бот пришлёт код в ваш Telegram — без него доступа нет."
        : "⚠️ Для 2FA сначала настройте Telegram ниже (токен бота + Chat ID)."}</div>
      <button class="btn primary" id="sec-save">Сохранить</button>
    </div></div>

    <div class="card"><h3>🔑 Playerok — доступ продавца</h3><div class="body" style="padding:18px">
      <p class="sub" style="margin-bottom:12px">${s.pl_token_set
        ? `<span style="color:var(--green)">✓ Подключено</span> · способ: ${s.pl_auth_method === "email" ? "email " + esc(s.pl_email) : "cookie"} · ${esc(s.pl_token_mask)}`
        : "Не подключено. Выберите способ входа — результат один (сессия Playerok), отличается только удобство."}</p>
      ${s.pl_token_set ? `<div style="margin-bottom:14px"><button class="btn" id="pl-check">⟳ Проверить подключение</button> <span id="pl-check-res" class="sub" style="margin-left:8px"></span></div>` : ""}
      <div class="field" style="margin-bottom:14px"><label>Прокси (необязательно; применяется и к входу)</label>
        <input id="st-proxy" value="${esc(s.pl_proxy)}" placeholder="http://user:pass@host:port"></div>

      <div class="tabs" style="margin-bottom:16px">
        <button data-authm="cookie" class="${s.pl_auth_method !== "email" ? "active" : ""}">🍪 Cookie token</button>
        <button data-authm="email" class="${s.pl_auth_method === "email" ? "active" : ""}">✉️ Вход по email</button>
      </div>

      <div id="auth-cookie" style="display:${s.pl_auth_method === "email" ? "none" : "block"}">
        <div class="field" style="margin-bottom:12px"><label>Cookie token ${s.pl_token_set ? `<span class="chip">сохранён</span>` : ""}</label>
          <input id="st-pl" type="password" placeholder="${s.pl_token_set ? "оставьте пустым, чтобы не менять" : "DevTools → Application → Cookies → playerok.com → token"}"></div>
        <button class="btn primary" id="st-save-pl">Сохранить</button>
      </div>

      <div id="auth-email" style="display:${s.pl_auth_method === "email" ? "block" : "none"}">
        <div class="field" style="margin-bottom:12px"><label>Email аккаунта Playerok</label>
          <input id="em-email" value="${esc(s.pl_email)}" placeholder="you@mail.com"></div>
        <button class="btn primary" id="em-get">Получить код</button>
        <div id="em-code-wrap" style="display:none;margin-top:14px">
          <div class="field" style="margin-bottom:12px"><label>Код из письма</label>
            <input id="em-code" placeholder="код действует пару минут"></div>
          <button class="btn primary" id="em-login">Войти</button>
        </div>
        <div class="tip" style="margin-top:10px">У Playerok нет пароля — только email-код. Бот получит и сохранит токен сам, DevTools не нужен.</div>
      </div>
    </div></div>

    <div class="card"><h3>📨 Telegram-уведомления</h3><div class="body" style="padding:18px">
      <p class="sub" style="margin-bottom:16px">Уведомления о заказах, сообщениях и проблемах (пустой склад, сбой поднятия, карантин).</p>
      <div class="field" style="margin-bottom:13px"><label>Токен бота ${s.tg_token_set ? `<span class="chip">сохранён: ${esc(s.tg_token_mask)}</span>` : ""}</label>
        <input id="st-token" type="password" placeholder="${s.tg_token_set ? "оставьте пустым, чтобы не менять" : "12345:AA..."}"><div class="tip">Создаётся у @BotFather.</div></div>
      <div class="field" style="margin-bottom:16px"><label>Chat ID (куда слать)</label>
        <input id="st-chat" value="${esc(s.tg_chat)}" placeholder="узнать у @userinfobot"></div>
      <button class="btn primary" id="st-save">Сохранить</button>
    </div></div>
  `);
  // безопасность входа
  $("#sec-save").onclick = async () => {
    const r = await api("security_set", { username: $("#sec-user").value, twofa_enabled: $("#sec-2fa").checked });
    if (r.error) { $("#sec-2fa").checked = sec.twofa_enabled; return toast(r.error, "err"); }
    toast("Сохранено", "ok"); go("settings");
  };
  // проверка подключения к Playerok
  const plCheck = $("#pl-check");
  if (plCheck) plCheck.onclick = async () => {
    const res = $("#pl-check-res");
    plCheck.disabled = true; res.textContent = "проверяю…"; res.style.color = "var(--text-dim)";
    const r = await api("pl_check_connection");
    plCheck.disabled = false;
    if (r.error) { res.textContent = r.error; res.style.color = "var(--red)"; return; }
    if (r.connected) {
      res.innerHTML = `<span style="color:var(--green)">✓ Подключено как ${esc(r.username)}</span>` +
        (r.unread != null ? ` · непрочитанных чатов: ${r.unread}` : "");
    } else {
      res.textContent = "✗ " + (r.message || "нет подключения");
      res.style.color = "var(--amber)";
    }
  };
  // переключение метода входа
  document.querySelectorAll("[data-authm]").forEach((b) => (b.onclick = async () => {
    const m = b.dataset.authm;
    document.querySelectorAll("[data-authm]").forEach((x) => x.classList.toggle("active", x === b));
    $("#auth-cookie").style.display = m === "cookie" ? "block" : "none";
    $("#auth-email").style.display = m === "email" ? "block" : "none";
    await api("settings_set", { pl_auth_method: m });  // запомнить выбор
  }));
  $("#st-save-pl").onclick = async () => {
    const data = { pl_proxy: $("#st-proxy").value };
    if ($("#st-pl").value) data.pl_token = $("#st-pl").value;
    const r = await api("settings_set", data);
    r.ok ? (toast("Сохранено", "ok"), go("settings")) : toast(r.error, "err");
  };
  // email-вход: шаг 1 — код
  $("#em-get").onclick = async () => {
    const email = $("#em-email").value.trim();
    await api("settings_set", { pl_proxy: $("#st-proxy").value });  // прокси нужен серверу до входа
    $("#em-get").textContent = "Отправляю…"; $("#em-get").disabled = true;
    const r = await api("pl_email_request_code", email);
    $("#em-get").textContent = "Получить код снова"; $("#em-get").disabled = false;
    if (r.error) return toast(r.error, "err");
    $("#em-code-wrap").style.display = "block";
    toast(r.message || "Код отправлен", "ok");
  };
  // email-вход: шаг 2 — подтверждение
  $("#em-login").onclick = async () => {
    const email = $("#em-email").value.trim(), code = $("#em-code").value.trim();
    $("#em-login").textContent = "Вхожу…"; $("#em-login").disabled = true;
    const r = await api("pl_email_submit_code", email, code);
    $("#em-login").textContent = "Войти"; $("#em-login").disabled = false;
    if (r.error) return toast(r.error, "err");
    toast("Подключено" + (r.username ? " как " + r.username : ""), "ok");
    go("settings");
  };
  $("#st-save").onclick = async () => {
    const data = { tg_chat: $("#st-chat").value };
    if ($("#st-token").value) data.tg_token = $("#st-token").value;
    const r = await api("settings_set", data);
    r.ok ? (toast("Сохранено", "ok"), go("settings")) : toast(r.error, "err");
  };
};

/* ---------- логи ---------- */
VIEWS.logs = async () => {
  const r = await api("events");
  if (r && r.items) {
    const rows = r.items.length ? r.items.map((e) => `
      <tr><td class="mono">${new Date(e.ts * 1000).toLocaleString("ru-RU")}</td>
      <td><span class="chip">${esc(e.kind)}</span></td>
      <td class="mono">${e.rental_id ? "аренда #" + e.rental_id : ""} ${e.account_id ? "акк #" + e.account_id : ""}</td>
      <td class="mono">${esc(e.data || "")}</td></tr>`).join("")
      : `<tr><td colspan="4" class="empty">Журнал пуст</td></tr>`;
    setMain("Логи", "журнал действий бота", `<div class="card"><div class="body">
      <table><thead><tr><th>Время</th><th>Событие</th><th>Объект</th><th>Данные</th></tr></thead><tbody>${rows}</tbody></table>
    </div></div>`);
  } else {
    setMain("Логи", "журнал действий бота", `<div class="empty">Журнал недоступен</div>`);
  }
};

/* ---------- вспомогательное ---------- */
function setMain(title, sub, html) {
  $("#main").innerHTML = `<div class="topbar"><div><h1>${esc(title)}</h1><div class="sub">${esc(sub)}</div></div></div>${html}`;
  refreshBadges();
}
function fail(msg) { setMain("Ошибка", "", `<div class="empty">${esc(msg)}</div>`); }

let _modalTimer = null;  // интервал живого Guard-кода, чистится при закрытии

function modal(title, bodyHtml, onOk, okLabel = "Сохранить", danger = false) {
  $("#modal-title").textContent = title;
  $("#modal-content").innerHTML = bodyHtml;
  const ok = $("#modal-ok");
  ok.style.display = "";
  ok.textContent = okLabel;
  ok.className = danger ? "btn danger" : "btn primary";
  $("#modal-cancel").textContent = "Отмена";
  $("#modal-bg").classList.add("show");
  ok.onclick = async () => { const res = await onOk(); if (res !== false) {} };
}

function infoModal(title, bodyHtml) {
  $("#modal-title").textContent = title;
  $("#modal-content").innerHTML = bodyHtml;
  $("#modal-ok").style.display = "none";       // у инфо-модалки одна кнопка
  $("#modal-cancel").textContent = "Закрыть";
  $("#modal-bg").classList.add("show");
}

function closeModal() {
  if (_modalTimer) { clearInterval(_modalTimer); _modalTimer = null; }
  $("#modal-bg").classList.remove("show");
}
$("#modal-close").onclick = closeModal;
$("#modal-cancel").onclick = closeModal;
$("#modal-bg").onclick = (e) => { if (e.target === $("#modal-bg")) closeModal(); };

/* ---------- старт ---------- */
(async () => {
  MODE = await bridgeReady();
  initLock();
})();
