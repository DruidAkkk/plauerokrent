'use strict';
/*
 * rentbot Steam-сайдкар (путь B).
 * Локальный HTTP-сервис на 127.0.0.1:PORT. Питон-панель зовёт его, когда нужен надёжный
 * вход в Steam (steampy нестабилен против нового auth-флоу).
 *
 * Эндпоинты:
 *   GET  /health       -> {ok:true}
 *   POST /login-check  {login, password, shared_secret, proxy?} -> {ok, message, steamid?}
 *       Пробный вход (read-only): логинится через steam-session и сразу завершает сессию.
 *   POST /deauth       {login, password, shared_secret, proxy?}
 *                      -> {ok, supported, message, revoked, total, sessions:[...]}
 *       «Выйти из аккаунта везде»: логинится как WebBrowser, считает активные сессии
 *       (EnumerateTokens) и шлёт ТОТ ЖЕ запрос, что и сайт Steam по кнопке «Выйти из аккаунта
 *       везде» — POST /twofactor/manage_action {action:'deauthorize', sessionid} с веб-куками.
 *       Пароль и мобильный аутентификатор НЕ трогаются; все сессии (включая эту техническую)
 *       завершаются — владелец и арендатор входят заново штатно.
 *
 * Запуск: node index.js   (PORT из env RENTBOT_SIDECAR_PORT, по умолчанию 8799)
 */
const http = require('http');
const https = require('https');
const { LoginSession, EAuthTokenPlatformType } = require('steam-session');
const SteamTotp = require('steam-totp');

// Низкоуровневые куски steam-session для прямых вызовов IAuthenticationService
// (клиентский sendRequest не знает методов EnumerateTokens/RevokeRefreshToken, поэтому
// работаем транспортом напрямую с заранее закодированными протобафами).
const { HttpClient } = require('@doctormckay/stdlib/http');
const StdLib = require('@doctormckay/stdlib');
const { SocksProxyAgent } = require('socks-proxy-agent');
const protos = require('steam-session/dist/protobuf-generated/load').default;
const EResult = require('steam-session/dist/enums-steam/EResult').default;
const { API_HEADERS } = require('steam-session/dist/helpers');

const WEBAPI_BASE = 'https://api.steampowered.com';

const EN_REQ = protos.CAuthentication_RefreshToken_Enumerate_Request;
const EN_RESP = protos.CAuthentication_RefreshToken_Enumerate_Response;

// «Выход со всех устройств» повторяет действие самого сайта Steam: после веб-логина (refresh-токен
// из maFile) шлём POST https://store.steampowered.com/twofactor/manage_action с полями
// action=deauthorize и sessionid (из веб-куки). Это НЕ удаление аутентификатора и НЕ смена пароля
// (для них на сайте отдельные действия), поэтому 15-дневного ограничения Маркета быть не должно —
// но на боевом аккаунте стоит один раз проверить. Прямой IAuthenticationService/RevokeRefreshToken
// здесь НЕ используется: он требует подпись, привязанную к секрету самого токена (есть только у
// устройства-владельца токена), поэтому кикнуть чужую сессию по её token_id нельзя by design.

const PORT = parseInt(process.env.RENTBOT_SIDECAR_PORT || '8799', 10);
const HOST = '127.0.0.1';

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => { data += c; if (data.length > 1e6) req.destroy(); });
    req.on('end', () => {
      try { resolve(data ? JSON.parse(data) : {}); }
      catch (e) { reject(new Error('bad json')); }
    });
    req.on('error', reject);
  });
}

/**
 * Залогиниться в Steam по паролю + мобильному Guard. На успехе резолвит объектом
 * { ok:true, session, accessToken, steamid }. Сессию НЕ завершает — это делает вызывающий
 * (accessToken остаётся валиден и после cancelLoginAttempt). На неуспехе — { ok:false, message }.
 */
async function authenticate({ login, password, shared_secret, proxy }, platform) {
  if (!login || !password) return { ok: false, message: 'нужны логин и пароль' };
  if (!shared_secret) return { ok: false, message: 'нужен shared_secret из maFile' };

  const opts = {};
  if (proxy) opts.httpProxy = proxy; // steam-session поддерживает http(s)-прокси строкой

  let session;
  try {
    session = new LoginSession(platform || EAuthTokenPlatformType.SteamClient, opts);
  } catch (e) {
    return { ok: false, message: 'прокси/инициализация: ' + (e.message || String(e)) };
  }

  let code;
  try {
    code = SteamTotp.generateAuthCode(shared_secret);
  } catch (e) {
    return { ok: false, message: 'shared_secret не base64/повреждён' };
  }

  return await new Promise((resolve) => {
    let done = false;
    const finish = (r) => {
      if (done) return;
      done = true;
      if (!r.ok) { try { session.cancelLoginAttempt(); } catch (_) {} }
      resolve(r);
    };
    const timer = setTimeout(() => finish({ ok: false, message: 'таймаут входа (30с)' }), 30000);

    session.on('authenticated', () => {
      clearTimeout(timer);
      const steamid = session.steamID ? session.steamID.toString() : null;
      finish({ ok: true, session, accessToken: session.accessToken, steamid });
    });
    session.on('timeout', () => { clearTimeout(timer); finish({ ok: false, message: 'Steam: таймаут подтверждения' }); });
    session.on('error', (e) => { clearTimeout(timer); finish({ ok: false, message: 'Steam: ' + (e && e.message ? e.message : String(e)) }); });

    session.startWithCredentials({ accountName: login, password: password, steamGuardCode: code })
      .then((res) => {
        if (res && res.actionRequired) {
          clearTimeout(timer);
          const kinds = (res.validActions || []).map((a) => a.type).join(',');
          finish({ ok: false, message: 'Steam требует доп. подтверждение (' + kinds + ') — вероятно, email-Guard, а не мобильный' });
        }
      })
      .catch((e) => {
        clearTimeout(timer);
        const msg = (e && e.message) ? e.message : String(e);
        finish({ ok: false, message: 'Steam: ' + msg });
      });
  });
}

function buildClient(proxy) {
  let agent;
  if (!proxy) agent = new https.Agent({ keepAlive: true });
  else if (/^socks/i.test(proxy)) agent = new SocksProxyAgent(proxy);
  else agent = StdLib.HTTP.getProxyAgent(true, proxy); // http(s)-прокси — как в самом steam-session
  return new HttpClient({ httpsAgent: agent });
}

/**
 * Прямой вызов IAuthenticationService/<method> (v1). В отличие от WebApiTransport всегда
 * шлёт поле input_protobuf_encoded (даже пустое — иначе Steam отвечает JSON-ошибкой на
 * EnumerateTokens), проверяет заголовок x-eresult и отдаёт понятный текст на любой сбой.
 */
async function webApi(client, accessToken, method, reqType, respType, data) {
  const bytes = reqType.encode(data).finish();
  const b64 = Buffer.from(bytes).toString('base64'); // '' для пустого запроса — это нормально
  const res = await client.request({
    method: 'POST',
    url: `${WEBAPI_BASE}/IAuthenticationService/${method}/v1/`,
    headers: { ...API_HEADERS },
    queryString: { access_token: accessToken },
    urlEncodedForm: { input_protobuf_encoded: b64 },
  });

  if (res.statusCode >= 300) {
    const em = res.headers['x-error_message'];
    throw new Error(`HTTP ${res.statusCode}` + (em ? ` (${em})` : ''));
  }
  const eresult = res.headers['x-eresult'];
  if (typeof eresult === 'string' && parseInt(eresult, 10) !== EResult.OK) {
    const em = res.headers['x-error_message'];
    throw new Error(`Steam EResult ${eresult}` + (em ? ` — ${em}` : ''));
  }
  if (res.jsonBody && typeof res.jsonBody === 'object') {
    // протобаф-метод не должен отвечать JSON — это признак ошибки/отказа
    throw new Error('Steam вернул JSON вместо protobuf: ' + JSON.stringify(res.jsonBody).slice(0, 300));
  }
  if (!respType) return null;
  const buf = Buffer.isBuffer(res.rawBody) ? res.rawBody : Buffer.alloc(0);
  return respType.decode(buf);
}

async function loginCheck(body) {
  const auth = await authenticate(body);
  if (!auth.ok) return { ok: false, message: auth.message };
  try {
    return { ok: true, message: 'вход выполнен, сессия активна', steamid: auth.steamid };
  } finally {
    try { auth.session.cancelLoginAttempt(); } catch (_) {}
  }
}

async function deauth(body) {
  // «Выйти из аккаунта везде»: логинимся как WebBrowser, считаем активные сессии (для отчёта),
  // затем повторяем действие кнопки сайта Steam — POST /twofactor/manage_action
  // {action:'deauthorize', sessionid} с веб-куками. Это завершает ВСЕ сессии (включая эту).
  const auth = await authenticate(body, EAuthTokenPlatformType.WebBrowser);
  if (!auth.ok) return { ok: false, supported: true, message: auth.message };
  const { session, accessToken } = auth;
  try {
    const client = buildClient(body.proxy);

    // 1) Посчитать активные чужие сессии (best-effort, чтобы показать владельцу «сколько вышло»).
    let sessions = [];
    try {
      const list = await webApi(client, accessToken, 'EnumerateTokens', EN_REQ, EN_RESP, {});
      const tokens = list.refresh_tokens || [];
      const mine = list.requesting_token ? String(list.requesting_token) : null;
      sessions = tokens
        .filter((t) => !(mine && String(t.token_id) === mine)) // свой технический вход не считаем
        .map((t) => ({
          description: t.token_description || '',
          platform: t.platform_type,
          logged_in: !!t.logged_in,
          city: (t.last_seen && t.last_seen.city) || '',
          country: (t.last_seen && t.last_seen.country) || '',
        }));
    } catch (e) { /* не критично — всё равно делаем выход */ }

    // 2) Веб-куки для store.steampowered.com (steamLoginSecure + sessionid).
    let cookies;
    try {
      cookies = await session.getWebCookies();
    } catch (e) {
      const m = (e && e.message) ? e.message : String(e);
      return { ok: false, supported: true, sessions, total: sessions.length,
               message: 'не удалось получить веб-куки: ' + m };
    }
    // getWebCookies() (WebBrowser) отдаёт МАССИВ полных Set-Cookie строк с атрибутами
    // ("name=value; Path=/; Secure; Domain=store.steampowered.com; ..."), по одной на домен.
    // Берём только куки домена store.steampowered.com и чистые пары name=value для заголовка Cookie
    // (иначе Path/Secure/Domain попадают в Cookie как «куки» и store не видит steamLoginSecure → 302).
    const WANT_HOST = 'store.steampowered.com';
    const picked = {};
    let sessionid = null;
    for (const sc of (cookies || [])) {
      const head = String(sc).split(';')[0];
      const eq = head.indexOf('=');
      if (eq < 0) continue;
      const name = head.slice(0, eq).trim();
      const value = head.slice(eq + 1).trim();
      const dm = /domain=([^;]+)/i.exec(sc);
      const dom = dm ? dm[1].trim().replace(/^\./, '') : null;
      if (dom && dom !== WANT_HOST) continue; // только store-домен (и куки без Domain)
      picked[name] = value;
      if (name === 'sessionid') sessionid = value;
    }
    if (!sessionid || !picked['steamLoginSecure']) {
      return { ok: false, supported: true, sessions, total: sessions.length,
               message: 'не удалось собрать store-куки (sessionid/steamLoginSecure)' };
    }
    const cookieHeader = Object.entries(picked).map(([k, v]) => `${k}=${v}`).join('; ');

    // 3) Тот же запрос, что шлёт сайт Steam по кнопке «Выйти из аккаунта везде».
    let res;
    try {
      res = await client.request({
        method: 'POST',
        url: 'https://store.steampowered.com/twofactor/manage_action',
        headers: {
          Cookie: cookieHeader,
          Accept: 'application/json, text/plain, */*',
          Origin: 'https://store.steampowered.com',
          Referer: 'https://store.steampowered.com/account/authorizeddevices',
        },
        multipartForm: {
          action: { content: 'deauthorize' },
          sessionid: { content: sessionid },
        },
      });
    } catch (e) {
      const m = (e && e.message) ? e.message : String(e);
      return { ok: false, supported: true, sessions, total: sessions.length,
               message: 'запрос выхода не прошёл: ' + m };
    }

    // ВАЖНО: при УСПЕХЕ manage_action отвечает 302 (редирект на страницу аккаунта) — это и есть
    // «ок», выход выполнен. 302 на /login (или login.steampowered.com) — наоборот, сессия не принята.
    // 200 — тоже успех (иногда JSON {success:1}). >=400 — ошибка.
    const loc = (res.headers && (res.headers['location'] || res.headers['Location'])) || '';
    if (res.statusCode >= 300 && res.statusCode < 400) {
      if (/\/login|login\.steampowered/i.test(loc)) {
        return { ok: false, supported: true, sessions, total: sessions.length,
                 message: 'Steam не принял сессию (редирект на логин) — проверьте maFile/прокси' };
      }
      // иначе — успешный редирект на аккаунт, продолжаем к ответу об успехе
    } else if (res.statusCode >= 400) {
      return { ok: false, supported: true, sessions, total: sessions.length,
               message: `Steam ответил HTTP ${res.statusCode} на manage_action` };
    } else if (res.jsonBody && typeof res.jsonBody === 'object' && ('success' in res.jsonBody)) {
      const s = res.jsonBody.success;
      if (!(s === true || s === 1)) {
        return { ok: false, supported: true, sessions, total: sessions.length,
                 message: 'Steam отклонил выход: ' + JSON.stringify(res.jsonBody).slice(0, 200) };
      }
    }

    const n = sessions.length;
    return {
      ok: true, supported: true, revoked: n, sessions, total: n,
      message: n
        ? `выход выполнен со всех устройств (было активных сессий: ${n})`
        : 'выход выполнен со всех устройств',
    };
  } finally {
    try { session.cancelLoginAttempt(); } catch (_) {}
  }
}

const server = http.createServer(async (req, res) => {
  const send = (code, obj) => {
    res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(obj));
  };
  try {
    if (req.method === 'GET' && req.url === '/health') return send(200, { ok: true, service: 'rentbot-steam-sidecar' });
    if (req.method === 'POST' && req.url === '/login-check') {
      const body = await readBody(req);
      return send(200, await loginCheck(body));
    }
    if (req.method === 'POST' && req.url === '/deauth') {
      const body = await readBody(req);
      return send(200, await deauth(body));
    }
    send(404, { ok: false, message: 'not found' });
  } catch (e) {
    send(500, { ok: false, message: (e && e.message) ? e.message : String(e) });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`rentbot-steam-sidecar listening on http://${HOST}:${PORT}`);
});
