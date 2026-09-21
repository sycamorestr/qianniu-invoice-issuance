async function (element, input) {
  var rawInput = input;
  var args = input;
  if (typeof args === "string") {
    try { args = JSON.parse(args); } catch (e) { args = { codes: args.split(/[,\r\n]+/) }; }
  }
  if (typeof args === "string") {
    try { args = JSON.parse(args); } catch (e) { args = { codes: args.split(/[,\r\n]+/) }; }
  }
  args = args || {};
  var codes = Array.isArray(args.codes) ? args.codes : [];
  codes = codes.map(function (x) { return String(x || "").trim(); }).filter(function (x, i, a) { return x && a.indexOf(x) === i; });
  if (!codes.length) return JSON.stringify({ ok: false, blocked: true, reason: "codes_empty", rawInput: rawInput, href: location.href, data: [] });
  if (location.href.indexOf("src.erp321.com/erp-web-group/erp-scm-invoice-goods/") < 0) {
    return JSON.stringify({ ok: false, blocked: true, reason: "wrong_frame", message: "请在票聚商品管理 iframe 中运行", rawInput: rawInput, href: location.href, data: [] });
  }
  var context = args.context || {};
  if (!context.coid || !context.uid) {
    return JSON.stringify({ ok: false, blocked: true, reason: "request_context_missing", message: "从当前页面真实 GetPageListV2 请求读取 coid、uid 后传入，不能固定其他租户值", rawInput: rawInput, href: location.href, data: [] });
  }
  var queryFlds = ["i_id", "sku_id", "sku_type", "name", "properties_value", "vc_name", "enabled", "invoice_name", "invoice_spec", "issuing_office", "invoice_qty", "tax_code_short", "tax_code", "tax_rate", "tax_rate_zero", "invoice_enabled"];
  var results = [];
  for (var i = 0; i < codes.length; i += 1) {
    var code = codes[i];
    var body = {
      page: { currentPage: 1, pageSize: 50, hasPageInfo: false, pageAction: 1 },
      // 商品类型、商品状态均不限；enabled 是状态，不是 invoice_enabled。
      data: { sku_id: "@@" + code, queryFlds: queryFlds },
      ip: "",
      coid: String(context.coid),
      uid: String(context.uid)
    };
    var response;
    try {
      response = await fetch("//apiweb.erp321.com/webapi/ItemApi/ItemSku/GetPageListV2", {
        method: "POST", credentials: "include", signal: AbortSignal.timeout(30000), headers: { "content-type": "application/json" }, body: JSON.stringify(body)
      });
    } catch (error) {
      results.push({ input_goods_code: code, ok: false, reason: "request_failed", error: error.name });
      continue;
    }
    var content = await response.text();
    var parsed = null;
    try { parsed = JSON.parse(content); } catch (e) {}
    if (!response.ok || !parsed || parsed.code !== 0 || parsed.act !== 0) {
      results.push({ input_goods_code: code, ok: false, reason: "request_failed", status: response.status });
      continue;
    }
    var rows = Array.isArray(parsed.data) ? parsed.data : [];
    if ((parsed.page && Number(parsed.page.pages) > 1) || rows.length >= 50) {
      results.push({ input_goods_code: code, ok: false, reason: "request_failed", error: "uniqueness_requires_more_pages" });
      continue;
    }
    var matches = rows.filter(function (row) { return String(row.sku_id || "") === code; });
    results.push({ input_goods_code: code, ok: matches.length === 1, reason: matches.length === 1 ? null : matches.length ? "multiple_exact_matches" : "no_exact_match", rows: rows, exact_matches: matches });
  }
  var failures = results.filter(function (x) { return !x.ok; });
  return JSON.stringify({ ok: !failures.length, blocked: !!failures.length, rawInput: rawInput, href: location.href, requested_count: codes.length, mapped_count: codes.length - failures.length, failures: failures, data: results });
}
