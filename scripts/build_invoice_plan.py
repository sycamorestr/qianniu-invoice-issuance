#!/usr/bin/env python3
"""Build and validate a local Qianniu invoice plan from normalized JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4
from html import unescape


MONEY = Decimal("0.01")
ZERO = Decimal("0")


ALIASES = {
    "application_no": ("application_no", "申请流水号", "发票流水号"),
    "order_no": ("order_no", "订单编号"),
    "source_row": ("source_row", "源行号", "__source_row"),
    "source_goods_name": ("source_goods_name", "货物名称", "商品标题", "商品名称"),
    "source_specification": ("source_specification", "源规格", "规格", "skuText", "商品规格"),
    "sub_order_no": ("sub_order_no", "子订单号", "子订单编号"),
    "real_total": ("real_total", "实收金额", "商品实收金额"),
    "match_amount": ("match_amount", "核对商品金额"),
    "match_amount_source": ("match_amount_source", "金额核对来源"),
    "invoice_total_amount": ("invoice_total_amount", "开票总金额"),
    "application_status": ("application_status", "申请状态"),
    "invoice_type": ("invoice_type", "发票类型"),
    "header_type": ("header_type", "抬头类型"),
    "invoice_title": ("invoice_title", "发票抬头"),
    "buyer_tax_id": ("buyer_tax_id", "购方税号"),
    "buyer_address": ("buyer_address", "企业地址", "购方地址"),
    "buyer_phone": ("buyer_phone", "企业电话", "购方电话"),
    "buyer_bank": ("buyer_bank", "开户行", "购方开户银行"),
    "buyer_bank_account": ("buyer_bank_account", "开户账号", "购方银行账号"),
    "show_buyer_contact": ("show_buyer_contact", "是否展示购买方地址电话银行账号"),
    "remark": ("remark", "发票备注", "备注"),
    "quantity": ("quantity", "数量"),
    "goods_amount": ("goods_amount", "商品金额"),
    "tax_rate": ("tax_rate", "税率"),
    "tax_rate_zero": ("tax_rate_zero", "零税率标识"),
    "virtual_category": ("virtual_category", "虚拟分类", "vc_name"),
    "goods_code": ("goods_code", "商品编码", "sku_id"),
    "item_name": ("item_name", "开票名称", "项目名称", "invoice_name"),
    "tax_classification_code": ("tax_classification_code", "税收编码", "税务编码", "商品和服务税收编码", "tax_code"),
    "specification": ("properties_value", "颜色及规格", "specification", "规格型号"),
    "unit": ("unit", "开票单位", "单位", "issuing_office"),
    "invoice_enabled": ("invoice_enabled", "是否开票"),
}


def value(row: dict[str, Any], field_name: str) -> Any:
    # Raw color/specification is authoritative, including an explicitly blank value.
    # invoice_spec is a separate business field and must never fill this column.
    if field_name == "specification":
        for key in ALIASES[field_name]:
            if key in row:
                return row[key]
        return None
    for key in ALIASES[field_name]:
        current = row.get(key)
        if current is not None and str(current).strip() != "":
            return current
    return None


def clean_text(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def decimal_text(number: Decimal) -> str:
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def parse_decimal(raw: Any, label: str, errors: list[str]) -> Decimal | None:
    text = clean_text(raw)
    if text is None:
        errors.append(f"缺少{label}")
        return None
    try:
        number = Decimal(text.replace(",", ""))
        if not number.is_finite():
            raise InvalidOperation
        return number
    except InvalidOperation:
        errors.append(f"{label}不是有效数字: {text}")
        return None


def parse_tax_rate(raw: Any, errors: list[str], label: str = "税率") -> Decimal | None:
    text = clean_text(raw)
    if text is None:
        errors.append(f"缺少{label}")
        return None
    try:
        # 聚水潭的虚拟分类页面值可能是“13%税率”；只接受
        # 这种明确的百分比格式，不能从“专（13-4）”等分类名称猜税率。
        percent_match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*(?:税率)?", text)
        if percent_match:
            rate = Decimal(percent_match.group(1)) / Decimal("100")
        else:
            rate = Decimal(text)
            if Decimal("1") < rate <= Decimal("100"):
                rate /= Decimal("100")
        if not rate.is_finite() or rate < ZERO or rate > Decimal("1"):
            raise InvalidOperation
        return rate
    except InvalidOperation:
        errors.append(f"{label}不是有效百分比: {text}")
        return None


def money_equal(left: Decimal, right: Decimal) -> bool:
    return left.quantize(MONEY, rounding=ROUND_HALF_UP) == right.quantize(MONEY, rounding=ROUND_HALF_UP)


def first_consistent(rows: Iterable[dict[str, Any]], field_name: str, errors: list[str]) -> str | None:
    values = []
    for row in rows:
        candidate = clean_text(value(row, field_name))
        if candidate is not None and candidate not in values:
            values.append(candidate)
    if len(values) > 1:
        errors.append(f"同一申请流水号的{field_name}存在冲突: {values}")
    return values[0] if values else None


def normalize_tax_text(raw: Any, errors: list[str], label: str) -> str | None:
    rate = parse_tax_rate(raw, errors, label)
    if rate is None:
        return None
    # The tax bureau template requires the rate as a decimal text value,
    # e.g. 0.13 for 13%.
    return decimal_text(rate)


def canonical_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def normalize_identifier(raw: Any) -> str | None:
    return clean_text(raw)


def normalize_invoice_type(raw: Any, errors: list[str]) -> str | None:
    text = clean_text(raw)
    if text is None:
        return None
    mapping = {
        "普通发票": "普通发票",
        "增值税普通发票": "普通发票",
        "增值税电子普通发票": "普通发票",
        "全电普通发票": "普通发票",
        "数电普通发票": "普通发票",
        "增值税专用发票": "增值税专用发票",
        "全电专用发票": "增值税专用发票",
        "数电专用发票": "增值税专用发票",
    }
    normalized = mapping.get(text)
    if normalized is None:
        errors.append(f"发票类型不在模板允许值内: {text}")
    return normalized


def normalize_natural_person_indicator(raw: Any, errors: list[str]) -> str | None:
    text = clean_text(raw)
    if text is None:
        return None
    if text in {"是", "否"}:
        return text
    lowered = text.lower()
    if any(marker in lowered for marker in ("企业", "公司", "个体工商户", "enterprise", "company")):
        return "否"
    if any(marker in lowered for marker in ("个人", "自然人", "individual", "person")):
        return "是"
    errors.append(f"抬头类型无法转换为受票方自然人标识: {text}")
    return None


@dataclass
class InvoiceBuild:
    application_no: str
    rows: list[tuple[int, dict[str, Any]]]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    matched_items: dict[int, dict[str, Any]] = field(default_factory=dict)
    used_items: set[tuple[str, int]] = field(default_factory=set)

    def error(self, message: str) -> None:
        self.errors.append(message)


def make_order_goods_index(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        order_no = normalize_identifier(value(row, "order_no"))
        goods_code = normalize_identifier(value(row, "goods_code"))
        if order_no and goods_code:
            result[order_no].add(goods_code)
    return result


def make_order_items_index(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Keep sub-order evidence for resolving a multi-product order to source rows."""
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        order_no = normalize_identifier(value(row, "order_no"))
        goods_code = normalize_identifier(value(row, "goods_code"))
        if order_no and goods_code:
            result[order_no].append(row)
    return result


def make_jst_index(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        goods_code = normalize_identifier(value(row, "goods_code"))
        if goods_code:
            result[goods_code].append(row)
    return result


def base_fields(build: InvoiceBuild) -> dict[str, Any]:
    rows = [row for _, row in build.rows]
    header_type = first_consistent(rows, "header_type", build.errors)
    fields = {
        "invoice_type": normalize_invoice_type(first_consistent(rows, "invoice_type", build.errors), build.errors),
        "header_type": header_type,
        "invoice_title": first_consistent(rows, "invoice_title", build.errors),
        "buyer_tax_id": first_consistent(rows, "buyer_tax_id", build.errors),
        "buyer_address": first_consistent(rows, "buyer_address", build.errors),
        "buyer_phone": first_consistent(rows, "buyer_phone", build.errors),
        "buyer_bank": first_consistent(rows, "buyer_bank", build.errors),
        "buyer_bank_account": first_consistent(rows, "buyer_bank_account", build.errors),
        "show_buyer_contact": None,
        "remark": first_consistent(rows, "remark", build.errors),
        "application_status_snapshot": first_consistent(rows, "application_status", build.errors),
    }
    fields["special_business_type"] = None
    fields["tax_included"] = "是"
    fields["recipient_natural_person_indicator"] = normalize_natural_person_indicator(
        fields["header_type"], build.errors
    )
    for required in ("invoice_type", "header_type", "invoice_title"):
        if not fields[required]:
            build.error(f"缺少开票基本字段: {required}")
    header = (fields["header_type"] or "").lower()
    if any(marker in header for marker in ("企业", "公司", "个体工商户", "enterprise", "company")) and not fields["buyer_tax_id"]:
        build.error("企业抬头缺少购方税号")
    status = fields["application_status_snapshot"] or ""
    if status and status not in {"待处理", "待开票", "1"}:
        build.error(f"申请状态不在已验证的待处理值内: {status}")
    if status:
        build.warnings.append("申请状态为导出快照；全流程入口另核对本次申请列表")
    return fields


def normalized_match_text(raw: Any) -> str | None:
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(item) for item in raw)
    text = clean_text(raw)
    if text is None:
        return None
    return re.sub(r"\s+", "", unescape(text)).casefold()


def resolve_goods_code(
    raw_row: dict[str, Any],
    order_goods: dict[str, set[str]],
    build: InvoiceBuild,
    source_row: int,
    order_items: dict[str, list[dict[str, Any]]] | None = None,
) -> str | None:
    direct = normalize_identifier(value(raw_row, "goods_code"))
    order_no = normalize_identifier(value(raw_row, "order_no"))
    if not order_no:
        build.error(f"源行 {source_row} 缺少订单编号")
        return None
    candidates = sorted(order_goods.get(order_no, set()))
    if direct and direct not in candidates:
        build.error(f"源行 {source_row} 的商品编码 {direct} 不属于订单 {order_no} 的查询结果")
        return None
    items = list(enumerate((order_items or {}).get(order_no, [])))
    if items:
        matches = []
        amount_conflict = False
        for index, item in items:
            code = normalize_identifier(value(item, "goods_code"))
            if code not in candidates or (direct and code != direct):
                continue
            if (order_no, index) in build.used_items:
                continue
            agrees = True
            for field_name in ("sub_order_no", "source_goods_name", "source_specification"):
                source_text = normalized_match_text(value(raw_row, field_name))
                item_text = normalized_match_text(value(item, field_name))
                if source_text and item_text and source_text != item_text:
                    agrees = False
            source_quantity = value(raw_row, "quantity")
            item_quantity = value(item, "quantity")
            if source_quantity is not None and item_quantity is not None:
                quantities = [parse_decimal(q, "匹配数量", []) for q in (source_quantity, item_quantity)]
                if None in quantities or quantities[0] != quantities[1]:
                    agrees = False
            amount = verified_match_amount(item)
            if amount is not None and not money_equal(amount, Decimal(str(value(raw_row, "goods_amount")).replace(",", ""))):
                amount_conflict = True
                agrees = False
            if agrees:
                matches.append((index, item))
        if len(matches) == 1:
            index, item = matches[0]
            build.used_items.add((order_no, index))
            build.matched_items[source_row] = item
            return normalize_identifier(value(item, "goods_code"))
        suffix = "金额核对证据冲突" if direct and amount_conflict else "商品字段冲突、重复使用子订单或存在多个商品编码/子订单候选"
        build.error(f"源行 {source_row} 的订单 {order_no} 无法唯一匹配: {suffix}")
        return None
    # Compatibility for local, order-code-only plans. The full workflow requires items.
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        build.error(f"源行 {source_row} 的订单 {order_no} 未查到商品编码")
    else:
        build.error(f"源行 {source_row} 的订单 {order_no} 存在多个商品编码: {candidates}")
    return None


def verified_match_amount(item: dict[str, Any]) -> Decimal | None:
    """Amount already verified to share the export's positive-row basis."""
    if clean_text(value(item, "match_amount_source")) not in {"order_detail_gross", "promotion_detail_gross"}:
        return None
    raw = value(item, "match_amount")
    try:
        amount = Decimal(str(raw))
        return amount if amount.is_finite() and amount > ZERO else None
    except InvalidOperation:
        return None


def resolve_jst_mapping(
    goods_code: str,
    jst_index: dict[str, list[dict[str, Any]]],
    build: InvoiceBuild,
    source_row: int,
) -> dict[str, Any] | None:
    matches = jst_index.get(goods_code, [])
    if len(matches) != 1:
        if matches:
            build.error(f"源行 {source_row} 的商品编码 {goods_code} 在聚水潭票据中多匹配")
        else:
            build.error(f"源行 {source_row} 的商品编码 {goods_code} 未在聚水潭票据中匹配")
        return None
    mapping = matches[0]
    required = {
        "item_name": value(mapping, "item_name"),
        "tax_classification_code": value(mapping, "tax_classification_code"),
        "unit": value(mapping, "unit"),
    }
    for name, candidate in required.items():
        if clean_text(candidate) is None:
            build.error(f"商品编码 {goods_code} 的聚水潭票据映射缺少{name}")
    enabled = value(mapping, "invoice_enabled")
    if enabled is None:
        build.error(f"商品编码 {goods_code} 的聚水潭票据映射缺少invoice_enabled")
    elif str(enabled).strip().lower() in {"false", "0", "否", "不使用", "不开票"}:
        build.error(f"商品编码 {goods_code} 在聚水潭票据中标记为不开票")
    elif str(enabled).strip().lower() not in {"true", "1", "是", "开票"}:
        build.error(f"商品编码 {goods_code} 的invoice_enabled为未知值")
    return mapping


def details_for(
    build: InvoiceBuild,
    order_goods: dict[str, set[str]],
    jst_index: dict[str, list[dict[str, Any]]],
    order_items: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    previous_positive: dict[str, Any] | None = None
    previous_was_negative = False

    for source_row, row in build.rows:
        amount = parse_decimal(value(row, "goods_amount"), f"源行 {source_row} 商品金额", build.errors)
        if amount is None:
            previous_positive = None
            previous_was_negative = False
            continue
        if amount == ZERO:
            build.error(f"源行 {source_row} 商品金额为零，无法生成明细")
            previous_positive = None
            previous_was_negative = False
            continue

        if amount < ZERO:
            if previous_positive is None:
                build.error(f"源行 {source_row} 的负商品金额前没有正商品金额")
                previous_was_negative = True
                continue
            if previous_was_negative:
                build.error(f"源行 {source_row} 为连续负商品金额，折扣归属不明确")
                continue
            if abs(amount) > Decimal(previous_positive["amount"]):
                build.error(f"源行 {source_row} 的折扣绝对值超过其前正商品金额")
            negative_order_no = normalize_identifier(value(row, "order_no"))
            if negative_order_no and negative_order_no != previous_positive["order_no"]:
                build.error(f"源行 {source_row} 的折扣订单编号与其前商品不一致")
            negative_rate = value(row, "tax_rate")
            if clean_text(negative_rate) is not None:
                row_errors: list[str] = []
                normalized = normalize_tax_text(negative_rate, row_errors, f"源行 {source_row} 税率")
                if row_errors:
                    build.errors.extend(row_errors)
                elif normalized != previous_positive["tax_rate_effective"]:
                    build.error(f"源行 {source_row} 折扣税率与其前商品税率不一致")
            previous_positive["discount_amount"] = decimal_text(amount)
            previous_positive["discount_source_row"] = source_row
            previous_was_negative = True
            continue

        row_errors: list[str] = []
        if clean_text(value(row, "source_goods_name")) == "价外费用":
            row_errors.append(f"源行 {source_row} 为价外费用，当前未定义映射规则，整票暂缓")
        quantity = parse_decimal(value(row, "quantity"), f"源行 {source_row} 数量", row_errors)
        if quantity is not None and quantity <= ZERO:
            row_errors.append(f"源行 {source_row} 数量必须为正数")
        goods_code = resolve_goods_code(row, order_goods, build, source_row, order_items)
        mapping = resolve_jst_mapping(goods_code, jst_index, build, source_row) if goods_code else None
        tax_rate = None
        tax_rate_effective = None
        tax_rate_source = None
        if mapping:
            jst_tax_rate_raw = value(mapping, "tax_rate")
            virtual_category_raw = value(mapping, "virtual_category")
            raw_rate = jst_tax_rate_raw if clean_text(jst_tax_rate_raw) is not None else virtual_category_raw
            tax_rate_source = "jst_tax_rate" if clean_text(jst_tax_rate_raw) is not None else "jst_virtual_category"
            zero_category = clean_text(virtual_category_raw) == "零税率"
            if clean_text(jst_tax_rate_raw) is not None:
                api_rate = parse_decimal(jst_tax_rate_raw, f"商品编码 {goods_code} 接口税率", row_errors)
                if api_rate is not None and ZERO <= api_rate <= Decimal('1'):
                    tax_rate_effective = decimal_text(api_rate)
                elif api_rate is not None:
                    row_errors.append(f"商品编码 {goods_code} 接口税率必须为0到1的小数")
            elif not zero_category and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?\s*%\s*(?:税率)?", clean_text(raw_rate) or ""):
                row_errors.append(f"商品编码 {goods_code} 聚水潭虚拟分类/税率不是有效百分比: {raw_rate}")
            else:
                tax_rate_effective = "0" if zero_category and clean_text(jst_tax_rate_raw) is None else normalize_tax_text(raw_rate, row_errors, f"商品编码 {goods_code} 聚水潭虚拟分类/税率")
            if zero_category and tax_rate_effective not in {None, "0"}:
                row_errors.append(f"商品编码 {goods_code} 的零税率分类与显式税率冲突")
            tax_rate = tax_rate_effective
            if tax_rate_effective == "0":
                # User's tax-template rule: keep the output blank, retain zero for validation.
                tax_rate = None
                tax_rate_source = "jst_zero_rate_blank"
            template_rate_raw = value(row, "tax_rate")
            if clean_text(template_rate_raw) is not None and tax_rate_effective is not None:
                template_rate = normalize_tax_text(template_rate_raw, row_errors, f"源行 {source_row} 千牛税率")
                if template_rate is not None and template_rate != tax_rate_effective:
                    build.error(f"商品编码 {goods_code} 的千牛税率 {template_rate} 与聚水潭虚拟分类税率 {tax_rate_effective} 不一致")
        build.errors.extend(row_errors)

        detail = {
            "invoice_serial_no": build.application_no,
            "source_row": source_row,
            "order_no": normalize_identifier(value(row, "order_no")),
            "goods_code": goods_code,
            "sub_order_no": clean_text(value(build.matched_items[source_row], "sub_order_no")) if source_row in build.matched_items else None,
            "order_item_evidence": build.matched_items.get(source_row),
            "item_name": clean_text(value(mapping, "item_name")) if mapping else None,
            "tax_classification_code": clean_text(value(mapping, "tax_classification_code")) if mapping else None,
            "specification": clean_text(value(mapping, "specification")) if mapping else None,
            "unit": clean_text(value(mapping, "unit")) if mapping else None,
            "quantity": decimal_text(quantity) if quantity is not None else None,
            "computed_unit_price": decimal_text(amount / quantity) if quantity and quantity > ZERO else None,
            "amount": decimal_text(amount),
            "tax_rate": tax_rate,
            "tax_rate_effective": tax_rate_effective,
            "tax_rate_source": tax_rate_source,
            "tax_rate_zero": clean_text(value(mapping, "tax_rate_zero")) if mapping else None,
            "discount_amount": "0",
            "discount_source_row": None,
        }
        details.append(detail)
        previous_positive = detail
        previous_was_negative = False

    if not details:
        build.error("没有可开具的正商品金额明细")
    return details


def expected_total(build: InvoiceBuild) -> Decimal | None:
    values = []
    for _, row in build.rows:
        raw = value(row, "invoice_total_amount")
        if clean_text(raw) is None:
            continue
        parsed = parse_decimal(raw, "开票总金额", build.errors)
        if parsed is not None:
            values.append(parsed)
    unique = []
    for item in values:
        if item not in unique:
            unique.append(item)
    if not unique:
        build.error("缺少开票总金额")
        return None
    if len(unique) > 1:
        build.error(f"同一申请流水号的开票总金额存在冲突: {[decimal_text(item) for item in unique]}")
    return unique[0]


def build_invoice(
    build: InvoiceBuild,
    order_goods: dict[str, set[str]],
    jst_index: dict[str, list[dict[str, Any]]],
    run: dict[str, Any],
    order_items: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    basic = base_fields(build)
    details = details_for(build, order_goods, jst_index, order_items)
    expected = expected_total(build)
    computed = sum((Decimal(line["amount"]) + Decimal(line["discount_amount"]) for line in details), ZERO)
    if expected is not None and not money_equal(expected, computed):
        build.error(
            f"开票总金额不平: 模板 {decimal_text(expected)}，明细 {decimal_text(computed)}"
        )
    normalized_invoice = {
        "invoice_serial_no": build.application_no,
        "basic": basic,
        "detail_lines": details,
        "invoice_total_amount": decimal_text(expected) if expected is not None else None,
        "computed_total_amount": decimal_text(computed),
        "issuer_taxpayer_id_fingerprint": clean_text(run.get("issuer_taxpayer_id_fingerprint")),
    }
    plan_hash = canonical_hash(normalized_invoice)
    issuer = normalized_invoice["issuer_taxpayer_id_fingerprint"] or clean_text(run.get("store_name"))
    if not issuer:
        build.warnings.append("缺少开票主体税号指纹和店铺名，幂等键强度不足")
        issuer = "unverified-issuer"
    idempotency_key = canonical_hash(
        {"issuer": issuer, "invoice": normalized_invoice}
    )
    status = "blocked" if build.errors else "ready_for_export"
    return {
        **normalized_invoice,
        "plan_hash": plan_hash,
        "idempotency_key": idempotency_key,
        "status": status,
        "errors": build.errors,
        "warnings": build.warnings,
    }


def load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取输入 JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("输入 JSON 顶层必须是对象")
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="规范化后的输入 JSON")
    parser.add_argument("--output", type=Path, help="计划输出 JSON；默认写到标准输出")
    args = parser.parse_args()

    try:
        source = load_json(args.input)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    run = source.get("run") or {}
    if not isinstance(run, dict):
        print("run 必须是对象", file=sys.stderr)
        return 2
    mode = clean_text(run.get("mode")) or "preview"
    if mode != "preview":
        print("run.mode 只能是 preview；本技能仅生成模板", file=sys.stderr)
        return 2
    run = {**run, "mode": mode}
    raw_selected = source.get("selected_application_ids", [])
    if not isinstance(raw_selected, list):
        print("selected_application_ids 必须是数组", file=sys.stderr)
        return 2
    selected = []
    duplicate_selected: list[str] = []
    for item in raw_selected:
        application_no = str(item).strip()
        if not application_no:
            continue
        if application_no in selected:
            duplicate_selected.append(application_no)
        else:
            selected.append(application_no)
    selected_set = set(selected)

    raw_template_rows = source.get("template_rows")
    if not isinstance(raw_template_rows, list):
        print("template_rows 必须是数组", file=sys.stderr)
        return 2
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    all_application_ids: set[str] = set()
    top_errors: list[str] = []
    seen_source_rows: set[int] = set()
    if duplicate_selected:
        top_errors.append(f"selected_application_ids 存在重复值: {sorted(set(duplicate_selected))}")
    for index, row in enumerate(raw_template_rows, start=1):
        if not isinstance(row, dict):
            top_errors.append(f"模板第 {index} 行不是对象")
            continue
        application_no = normalize_identifier(value(row, "application_no"))
        if not application_no:
            top_errors.append(f"模板第 {index} 行缺少申请流水号")
            continue
        all_application_ids.add(application_no)
        if application_no in selected_set:
            raw_source_row = value(row, "source_row")
            try:
                source_row = int(raw_source_row) if raw_source_row is not None else index
                if source_row <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                top_errors.append(f"模板第 {index} 行源行号无效: {raw_source_row}")
                source_row = index
            if source_row in seen_source_rows:
                top_errors.append(f"重复源行号: {source_row}")
            seen_source_rows.add(source_row)
            grouped[application_no].append((source_row, row))
    missing_selected = sorted(selected_set - set(grouped))
    if missing_selected:
        top_errors.append(f"未在模板中找到已选申请流水号: {missing_selected}")

    order_rows = source.get("order_goods") or []
    order_item_rows = source.get("order_items") or []
    jst_rows = source.get("jst_invoice_goods") or []
    if not isinstance(order_rows, list) or not isinstance(order_item_rows, list) or not isinstance(jst_rows, list):
        print("order_goods、order_items 和 jst_invoice_goods 必须是数组", file=sys.stderr)
        return 2
    order_goods = make_order_goods_index([row for row in order_rows if isinstance(row, dict)])
    order_items = make_order_items_index([row for row in order_item_rows if isinstance(row, dict)])
    jst_index = make_jst_index([row for row in jst_rows if isinstance(row, dict)])

    invoices = []
    for application_no in selected:
        rows = grouped.get(application_no)
        if not rows:
            continue
        invoices.append(build_invoice(InvoiceBuild(application_no, rows), order_goods, jst_index, run, order_items))

    blocked = bool(top_errors or any(invoice["errors"] for invoice in invoices))
    output = {
        "run_id": clean_text(run.get("run_id")) or str(uuid4()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "selected_application_ids": selected,
        "ignored_application_ids": sorted(all_application_ids - selected_set),
        "blocked": blocked,
        "fatal": bool(top_errors),
        "errors": top_errors,
        "invoices": invoices,
        "summary": {
            "selected_count": len(selected),
            "plan_count": len(invoices),
            "ready_count": sum(not invoice["errors"] for invoice in invoices),
            "blocked_count": sum(bool(invoice["errors"]) for invoice in invoices) + len(missing_selected),
            "missing_application_ids": missing_selected,
            "invoice_total_amount": decimal_text(
                sum((Decimal(invoice["computed_total_amount"]) for invoice in invoices), ZERO)
            ),
            "ready_invoice_total_amount": decimal_text(
                sum((Decimal(invoice["computed_total_amount"]) for invoice in invoices if not invoice["errors"]), ZERO)
            ),
            "blocked_invoice_total_amount": decimal_text(
                sum((Decimal(invoice["computed_total_amount"]) for invoice in invoices if invoice["errors"]), ZERO)
            ),
        },
    }
    rendered = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0 if not blocked else 1


if __name__ == "__main__":
    raise SystemExit(main())
