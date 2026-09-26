async function (element, input) {
  // This page script is intentionally self-contained: it is evaluated in the
  // logged-in 票聚 iframe and must not depend on Node/browser extension APIs.
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    return { ok: false, blocked: true, reason: "input_invalid", href: location.href, data: [] };
  }
  var args = input;
  var codes = Array.isArray(args.codes) ? args.codes : [];
  codes = codes.map(function (x) { return String(x || "").trim(); }).filter(function (x, i, a) {
    return x && a.indexOf(x) === i;
  });
  if (!codes.length) return { ok: false, blocked: true, reason: "codes_empty", href: location.href, data: [] };
  if (location.href.indexOf("src.erp321.com/erp-web-group/erp-scm-invoice-goods/") < 0) {
    return { ok: false, blocked: true, reason: "wrong_frame", message: "请在票聚商品管理 iframe 中运行", href: location.href, data: [] };
  }
  var context = args.context || {};
  if (!context.coid || !context.uid) {
    return { ok: false, blocked: true, reason: "request_context_missing", message: "从当前页面真实 GetPageListV2 请求读取 coid、uid 后传入，不能固定其他租户值", href: location.href, data: [] };
  }

  var queryFlds = ["i_id", "sku_id", "sku_type", "name", "properties_value", "vc_name", "enabled", "invoice_name", "invoice_spec", "issuing_office", "invoice_qty", "tax_code_short", "tax_code", "tax_rate", "tax_rate_zero", "invoice_enabled"];
  var endpoint = "//apiweb.erp321.com/webapi/ItemApi/ItemSku/GetPageListV2";
  var concurrency = 8;
  var maxRetries = 2;
  var timeoutMs = 8000;
  var results = new Array(codes.length);
  var cursor = 0;
  var authError = null;
  var activeControllers = [];
  var retryCountTotal = 0;

  function errorWith(code, message, details) {
    var error = new Error(message || code);
    error.code = code;
    if (details) Object.keys(details).forEach(function (key) { error[key] = details[key]; });
    return error;
  }
  function responseUrl(response) { return response && response.url ? String(response.url) : ""; }
  function authFailure(response, content, parsed) {
    var status = Number(response && response.status || 0);
    var url = responseUrl(response).toLowerCase();
    var body = String(content || "");
    var bodyLower = body.toLowerCase();
    if (status === 401 || status === 403) return "http_" + status;
    if (url.indexOf("login") >= 0 || url.indexOf("passport") >= 0) return "redirect_login";
    if (/<html[\s>]/i.test(body) && /(login|passport|登录|统一登录|请先登录)/i.test(body)) return "login_html";
    if (/(未登录|登录失效|请先登录|请重新登录|登录超时|登录后继续|未授权|login required|please login|not logged in|not authenticated|authentication required|unauthorized|login expired|token expired|session expired|invalid token)/i.test(bodyLower)) return "auth_body";
    if (parsed && (Number(parsed.code) === 401 || Number(parsed.code) === 403 || parsed.isLogin === false || parsed.isLogin === 0 || parsed.login === false || parsed.login === 0)) return "auth_payload";
    return null;
  }
  function isRetryableStatus(status) { return status === 429 || status === 502 || status === 503 || status === 504; }
  function classifyNetworkError(error) {
    var name = String(error && error.name || "");
    if (name === "TimeoutError" || name === "AbortError") return "timeout";
    if (name === "TypeError") return "network";
    return "request_error";
  }
  function removeController(controller) {
    var index = activeControllers.indexOf(controller);
    if (index >= 0) activeControllers.splice(index, 1);
  }
  function abortActiveRequests() {
    activeControllers.slice().forEach(function (controller) {
      try { controller.abort(); } catch (e) {}
    });
  }
  function markAuth(details) {
    var error = errorWith("auth_required", "auth_required", details);
    if (!authError) {
      authError = error;
      abortActiveRequests();
    }
    return authError;
  }
  async function fetchWithTimeout(body, code) {
    // Auth failure cancels all other in-flight requests instead of waiting
    // for their individual request timeouts.
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = null;
    if (controller) activeControllers.push(controller);
    try {
      if (controller) timer = setTimeout(function () { controller.abort(); }, timeoutMs);
      var options = { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify(body) };
      if (controller) options.signal = controller.signal;
      var response = await fetch(endpoint, options);
      // Reject explicit auth responses before waiting for their response body.
      var authReason = authFailure(response, "", null);
      if (authReason) {
        if (controller) controller.abort();
        throw markAuth({ input_goods_code: code, auth_reason: authReason, status: Number(response.status || 0), response_url: responseUrl(response) });
      }
      // Keep both the timeout and cancellation controller alive until the body
      // is consumed; fetch resolves as soon as response headers arrive.
      var content = await response.text();
      return { response: response, content: content };
    } finally {
      if (timer !== null) clearTimeout(timer);
      if (controller) removeController(controller);
    }
  }

  async function queryOne(code) {
    var body = {
      page: { currentPage: 1, pageSize: 50, hasPageInfo: false, pageAction: 1 },
      // 商品类型、商品状态均不限；enabled 是状态，不是 invoice_enabled。
      data: { sku_id: "@@" + code, queryFlds: queryFlds },
      ip: "", coid: String(context.coid), uid: String(context.uid)
    };
    var attempts = 0;
    var retries = 0;
    var lastFailure = null;
    while (attempts <= maxRetries) {
      if (authError) throw authError;
      attempts += 1;
      var response = null;
      var content = "";
      var parsed = null;
      try {
        var fetched = await fetchWithTimeout(body, code);
        response = fetched.response;
        content = fetched.content;
        try { parsed = JSON.parse(content); } catch (e) { parsed = null; }
        var authReason = authFailure(response, content, parsed);
        if (authReason) throw markAuth({ input_goods_code: code, auth_reason: authReason, status: Number(response.status || 0), response_url: responseUrl(response) });
        if (!response.ok || !parsed || parsed.code !== 0 || parsed.act !== 0) {
          var status = Number(response && response.status || 0);
          var errorClass = !parsed ? "invalid_json" : response && !response.ok ? "http_" + status : "api_error";
          var retryable = isRetryableStatus(status);
          lastFailure = { status: status, error_class: errorClass, retryable: retryable };
          if (authError) throw authError;
          if (retryable && attempts <= maxRetries) {
            retries += 1; retryCountTotal += 1; continue;
          }
          return { input_goods_code: code, ok: false, reason: "request_failed", error_class: errorClass, retry_exhausted: retryable, status: status, attempts: attempts, retry_count: retries };
        }
        var rows = Array.isArray(parsed.data) ? parsed.data : [];
        if ((parsed.page && Number(parsed.page.pages) > 1) || rows.length >= 50) {
          return { input_goods_code: code, ok: false, reason: "request_failed", error_class: "uniqueness_requires_more_pages", attempts: attempts, retry_count: retries };
        }
        var exactMatches = rows.filter(function (row) { return String(row.sku_id || "") === code; });
        var normalizeCode = function (value) { return String(value || "").replace(/（/g, "(").replace(/）/g, ")"); };
        var normalizedCode = normalizeCode(code);
        var matches = rows.filter(function (row) { return normalizeCode(row.sku_id) === normalizedCode; });
        var unique = matches.length === 1;
        var matchBasis = unique && exactMatches.length === 1 ? "sku_id_exact" : unique ? "sku_id_paren_width" : null;
        return { input_goods_code: code, ok: unique, match_basis: matchBasis, reason: unique ? null : matches.length ? "multiple_exact_matches" : "no_exact_match", rows: rows, exact_matches: unique ? matches : [], attempts: attempts, retry_count: retries };
      } catch (error) {
        if (error && error.code === "auth_required") throw error;
        // Another worker may have detected a logged-out response while this
        // request was in flight. Do not retry or start another request then.
        if (authError) throw authError;
        var errorClass = classifyNetworkError(error);
        var retryableError = errorClass === "network" || errorClass === "timeout";
        lastFailure = { error_class: errorClass, retryable: retryableError };
        if (retryableError && attempts <= maxRetries) {
          retries += 1; retryCountTotal += 1; continue;
        }
        return { input_goods_code: code, ok: false, reason: "request_failed", error_class: errorClass, retry_exhausted: retryableError, error: String(error && error.message || error || "request_failed"), attempts: attempts, retry_count: retries, last_failure: lastFailure };
      }
    }
    return { input_goods_code: code, ok: false, reason: "request_failed", error_class: lastFailure && lastFailure.error_class, retry_exhausted: true, attempts: attempts, retry_count: retries };
  }

  async function worker() {
    while (!authError) {
      var index = cursor;
      cursor += 1;
      if (index >= codes.length) return;
      try {
        results[index] = await queryOne(codes[index]);
      } catch (error) {
        if (error && error.code === "auth_required") {
          authError = authError || error;
          abortActiveRequests();
          return;
        }
        results[index] = { input_goods_code: codes[index], ok: false, reason: "request_failed", error_class: classifyNetworkError(error), error: String(error && error.message || error) };
      }
    }
  }

  var workers = [];
  for (var workerIndex = 0; workerIndex < Math.min(concurrency, codes.length); workerIndex += 1) workers.push(worker());
  await Promise.all(workers);
  if (authError) throw authError;
  var failures = results.filter(function (x) { return !x || !x.ok; });
  // Top-level status describes completed collection, not match success. Keep
  // successful rows and per-code failures in one resumable checkpoint.
  return { ok: true, blocked: false, partial: !!failures.length, has_failures: !!failures.length, all_matched: !failures.length, href: location.href, queried_at: new Date().toISOString(), requested_count: codes.length, mapped_count: codes.length - failures.length, concurrency: concurrency, max_retries: maxRetries, retry_count: retryCountTotal, failures: failures, data: results };
}
