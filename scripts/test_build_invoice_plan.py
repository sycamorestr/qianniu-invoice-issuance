#!/usr/bin/env python3
"""Focused tests for the invoice-plan business invariants."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path


SCRIPT = Path(__file__).with_name("build_invoice_plan.py")
SPEC = importlib.util.spec_from_file_location("build_invoice_plan", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def valid_source() -> dict:
    return {
        "run": {"mode": "preview", "store_name": "test-store", "issuer_taxpayer_id_fingerprint": "issuer-fp"},
        "selected_application_ids": ["A-1"],
        "template_rows": [
            {
                "申请流水号": "A-1",
                "订单编号": "O-1",
                "开票总金额": "65.30",
                "发票类型": "全电普通发票",
                "抬头类型": "企业",
                "发票抬头": "测试购方",
                "购方税号": "buyer-fp",
                "数量": "7",
                "商品金额": "69.30",
            },
            {"申请流水号": "A-1", "订单编号": "O-1", "商品金额": "-4.00"},
        ],
        "order_goods": [{"订单编号": "O-1", "商品编码": "SKU-1"}],
        "jst_invoice_goods": [
            {
                "商品编码": "SKU-1",
                "开票名称": "测试项目",
                "税收编码": "1010101010000000000",
                "颜色及规格": "测试规格",
                "开票单位": "件",
                "虚拟分类": "13%",
                "是否开票": True,
            }
        ],
    }


def build(source: dict) -> dict:
    run = source["run"]
    groups = {"A-1": list(enumerate(source["template_rows"], start=1))}
    order_index = MODULE.make_order_goods_index(source["order_goods"])
    order_items = MODULE.make_order_items_index(source.get("order_items", []))
    jst_index = MODULE.make_jst_index(source["jst_invoice_goods"])
    return MODULE.build_invoice(MODULE.InvoiceBuild("A-1", groups["A-1"]), order_index, jst_index, run, order_items)


def test_missing_code_sibling_cannot_be_dropped_to_claim_unique_match() -> None:
    source = valid_source()
    source['order_items'] = [
        {'order_no': 'O-1', 'goods_code': 'SKU-1', 'sub_order_no': 'S-1', 'quantity': '7'},
        {'order_no': 'O-1', 'goods_code': '', 'sub_order_no': 'S-2', 'quantity': '7'},
    ]
    invoice = build(source)
    assert any('缺少商家编码' in error for error in invoice['errors']), invoice
    assert invoice['status'] != 'ready_for_export'


def test_valid_positive_and_discount() -> None:
    invoice = build(valid_source())
    assert invoice["errors"] == [], invoice["errors"]
    assert invoice["basic"]["invoice_type"] == "普通发票"
    assert invoice["basic"]["recipient_natural_person_indicator"] == "否"
    assert invoice["computed_total_amount"] == "65.3"
    assert invoice["detail_lines"][0]["discount_amount"] == "-4"
    assert invoice["detail_lines"][0]["invoice_serial_no"] == "A-1"
    assert invoice["detail_lines"][0]["goods_code"] == "SKU-1"
    assert invoice["detail_lines"][0]["tax_rate"] == "0.13"
    assert invoice["detail_lines"][0]["tax_rate_source"] == "jst_virtual_category"
    assert invoice["status"] == "ready_for_export"


def test_paper_ordinary_request_maps_to_digital_ordinary_without_changing_source() -> None:
    source = valid_source()
    baseline = build(source)
    source['template_rows'][0]['发票类型'] = '增值税纸质普通发票'
    original = deepcopy(source)
    invoice = build(source)
    assert invoice['errors'] == [], invoice['errors']
    assert invoice['status'] == 'ready_for_export'
    assert invoice['basic']['invoice_type'] == '普通发票'
    assert invoice['detail_lines'] == baseline['detail_lines']
    assert invoice['invoice_total_amount'] == baseline['invoice_total_amount']
    assert source == original


def test_paper_ordinary_request_keeps_missing_goods_code_and_unit_blocked() -> None:
    for missing in ('goods_code', 'unit'):
        source = valid_source()
        source['template_rows'][0]['发票类型'] = '增值税纸质普通发票'
        if missing == 'goods_code':
            source['order_items'] = [{'order_no': 'O-1', 'goods_code': '',
                                      'sub_order_no': 'S-1', 'quantity': '7'}]
            expected = '缺少商家编码'
        else:
            source['jst_invoice_goods'][0]['开票单位'] = ''
            expected = '缺少unit'
        invoice = build(source)
        assert invoice['status'] == 'blocked', invoice
        assert any(expected in error for error in invoice['errors']), invoice
        assert not any('发票类型' in error for error in invoice['errors']), invoice


def test_paper_ordinary_alias_does_not_accept_other_unknown_invoice_types() -> None:
    for invoice_type in ('增值税纸质专用发票', '未知普通发票'):
        source = valid_source()
        source['template_rows'][0]['发票类型'] = invoice_type
        invoice = build(source)
        assert invoice['status'] == 'blocked'
        assert any('发票类型不在模板允许值内' in error for error in invoice['errors'])


def test_multiple_positive_rows_keep_same_invoice_group() -> None:
    source = valid_source()
    source["template_rows"] = [
        source["template_rows"][0],
        source["template_rows"][1],
        {
            "申请流水号": "A-1",
            "订单编号": "O-1",
            "数量": "2",
            "商品金额": "10.00",
        },
    ]
    source["template_rows"][0]["开票总金额"] = "75.30"
    invoice = build(source)
    assert invoice["errors"] == [], invoice["errors"]
    assert len(invoice["detail_lines"]) == 2
    assert [line["source_row"] for line in invoice["detail_lines"]] == [1, 3]
    assert [line["amount"] for line in invoice["detail_lines"]] == ["69.3", "10"]
    assert invoice["detail_lines"][0]["discount_amount"] == "-4"
    assert invoice["detail_lines"][1]["discount_amount"] == "0"
    assert invoice["computed_total_amount"] == "75.3"


def test_two_suborders_match_by_source_title_and_quantity() -> None:
    source = valid_source()
    source["template_rows"] = [
        {
            "申请流水号": "A-1",
            "订单编号": "O-1",
            "开票总金额": "20.00",
            "发票类型": "全电普通发票",
            "抬头类型": "企业",
            "发票抬头": "测试购方",
            "购方税号": "buyer-fp",
            "货物名称": "商品甲",
            "数量": "1",
            "商品金额": "12.00",
        },
        {
            "申请流水号": "A-1",
            "订单编号": "O-1",
            "货物名称": "商品乙",
            "数量": "2",
            "商品金额": "8.00",
        },
    ]
    source["order_goods"] = [
        {"订单编号": "O-1", "商品编码": "SKU-1"},
        {"订单编号": "O-1", "商品编码": "SKU-2"},
    ]
    source["order_items"] = [
        {"订单编号": "O-1", "子订单号": "SO-1", "商品编码": "SKU-1", "商品标题": "商品甲", "数量": "1"},
        {"订单编号": "O-1", "子订单号": "SO-2", "商品编码": "SKU-2", "商品标题": "商品乙", "数量": "2"},
    ]
    source["jst_invoice_goods"] = [
        {
            "商品编码": "SKU-1",
            "开票名称": "票据甲",
            "税收编码": "1010101010000000000",
            "开票单位": "件",
            "虚拟分类": "13%",
            "是否开票": True,
        },
        {
            "商品编码": "SKU-2",
            "开票名称": "票据乙",
            "税收编码": "1010101010000000000",
            "开票单位": "件",
            "虚拟分类": "13%",
            "是否开票": True,
        },
    ]
    invoice = build(source)
    assert invoice["errors"] == [], invoice["errors"]
    assert [line["goods_code"] for line in invoice["detail_lines"]] == ["SKU-1", "SKU-2"]
    assert [line["invoice_serial_no"] for line in invoice["detail_lines"]] == ["A-1", "A-1"]


def test_direct_goods_code_must_belong_to_order_result() -> None:
    source = valid_source()
    source["template_rows"][0]["商品编码"] = "SKU-OTHER"
    invoice = build(source)
    assert any("不属于订单" in error for error in invoice["errors"])


def test_negative_without_predecessor_is_blocked() -> None:
    source = valid_source()
    source["template_rows"] = source["template_rows"][1:]
    source["template_rows"][0]["开票总金额"] = "4.00"
    invoice = build(source)
    assert any("负商品金额前没有正商品金额" in error for error in invoice["errors"])


def red_source() -> dict:
    source = valid_source()
    row = source["template_rows"][0]
    row.update({"订单编号": "9000000000000000001", "开票总金额": "-18.90",
                "货物名称": "测试商品", "数量": "-1", "商品金额": "-18.90"})
    source["template_rows"] = [row]
    source["order_goods"] = [{"订单编号": row["订单编号"], "商品编码": "TEST-SKU-2"}]
    source["order_items"] = [{"订单编号": row["订单编号"], "子订单号": "original-suborder-1",
                               "商品编码": "TEST-SKU-2", "商品标题": row["货物名称"], "数量": "1"}]
    source["jst_invoice_goods"][0]["商品编码"] = "TEST-SKU-2"
    return source


def test_negative_invoice_is_excluded_before_detail_generation() -> None:
    source = red_source()
    invoice = build(source)
    assert invoice["errors"] == [], invoice["errors"]
    assert invoice["red_reversal"] is True
    assert invoice["status"] == "excluded_negative"
    assert invoice["exclusion_reason"] == "负数发票按规则不开具"
    assert invoice["invoice_total_amount"] == "-18.9"
    assert invoice["computed_total_amount"] is None
    assert invoice["detail_lines"] == []
    ordinary = build(valid_source())
    assert ordinary["red_reversal"] is False
    assert ordinary["detail_lines"][0]["discount_amount"] == "-4"


def test_negative_invoice_with_multiple_rows_is_excluded_as_a_whole() -> None:
    source = red_source()
    source["template_rows"][0]["开票总金额"] = "-26.90"
    other = {"申请流水号": "A-1", "订单编号": "O-2", "货物名称": "商品乙",
             "数量": "-2", "商品金额": "-8.00"}
    source["template_rows"].append(other)
    invoice = build(source)
    assert invoice["errors"] == [], invoice["errors"]
    assert invoice["status"] == "excluded_negative"
    assert invoice["detail_lines"] == []
    assert invoice["invoice_total_amount"] == "-26.9"


def test_negative_exclusion_does_not_require_invoice_fields_or_order_mapping() -> None:
    for quantity in [None, "", "0", "1", "NaN", "Infinity", "-Infinity"]:
        source = red_source()
        source["template_rows"][0]["数量"] = quantity
        for field in ("发票类型", "抬头类型", "发票抬头", "购方税号"):
            del source["template_rows"][0][field]
        source["order_goods"] = []
        source["order_items"] = []
        source["jst_invoice_goods"] = []
        invoice = build(source)
        assert invoice["status"] == "excluded_negative", quantity
        assert invoice["errors"] == [], invoice["errors"]
        assert invoice["detail_lines"] == []


def test_negative_exclusion_requires_consistent_finite_application_total() -> None:
    for total in ["18.90", "-19.90", "NaN", "Infinity", "-Infinity"]:
        source = red_source()
        other = deepcopy(source["template_rows"][0])
        other["开票总金额"] = total
        source["template_rows"].append(other)
        invoice = build(source)
        assert invoice["status"] == "blocked"
        assert invoice["red_reversal"] is False
        assert any("开票总金额" in error for error in invoice["errors"])


def test_negative_total_excludes_mixed_rows_without_partial_output() -> None:
    source = red_source()
    source["template_rows"].append({"申请流水号": "A-1", "订单编号": "O-2",
                                    "商品金额": "5.00", "数量": "1"})
    source["template_rows"][0]["开票总金额"] = "-13.90"
    invoice = build(source)
    assert invoice["status"] == "excluded_negative"
    assert invoice["detail_lines"] == [] and invoice["errors"] == []
    for amount in ["5.00", "-5.00"]:
        source = red_source()
        source["template_rows"].append({"申请流水号": "A-1", "订单编号": "9000000000000000001",
                                        "货物名称": "价外费用", "商品金额": amount, "数量": "-1"})
        invoice = build(source)
        assert invoice["status"] == "excluded_negative"
        assert invoice["detail_lines"] == [] and invoice["errors"] == []


def test_negative_exclusion_is_separate_from_ready_and_blocked_totals() -> None:
    with tempfile.TemporaryDirectory() as directory:
        input_path = Path(directory) / "input.json"
        output_path = Path(directory) / "plan.json"
        input_path.write_text(json.dumps(red_source(), ensure_ascii=False), encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPT), str(input_path), "--output", str(output_path)],
                                check=False, capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr
        plan = json.loads(output_path.read_text(encoding="utf-8"))
        assert plan["blocked"] is False
        assert plan["summary"]["ready_count"] == plan["summary"]["blocked_count"] == 0
        assert plan["summary"]["excluded_negative_count"] == 1
        assert plan["summary"]["ready_invoice_total_amount"] == "0"
        assert plan["summary"]["blocked_invoice_total_amount"] == "0"
        assert plan["summary"]["excluded_negative_invoice_total_amount"] == "-18.9"


def test_multiple_order_goods_is_blocked() -> None:
    source = valid_source()
    source["order_goods"].append({"订单编号": "O-1", "商品编码": "SKU-2"})
    invoice = build(source)
    assert any("存在多个商品编码" in error for error in invoice["errors"])


def test_unparseable_virtual_category_is_blocked() -> None:
    source = valid_source()
    source["jst_invoice_goods"][0]["虚拟分类"] = "默认税率分类"
    invoice = build(source)
    assert any("聚水潭虚拟分类/税率不是有效百分比" in error for error in invoice["errors"])


def test_jst_api_field_aliases_and_strict_virtual_category() -> None:
    source = valid_source()
    source["jst_invoice_goods"] = [
        {
            "sku_id": "SKU-1",
            "invoice_name": "中秋礼盒",
            "properties_value": "8枚装",
            "invoice_spec": "另一种开票规格",
            "issuing_office": "组",
            "tax_code": "1030201010000000000",
            "vc_name": "13%税率",
            "invoice_enabled": True,
        }
    ]
    invoice = build(source)
    assert invoice["errors"] == [], invoice["errors"]
    detail = invoice["detail_lines"][0]
    assert detail["item_name"] == "中秋礼盒"
    assert detail["specification"] == "8枚装"
    assert detail["unit"] == "组"
    assert detail["tax_rate"] == "0.13"


def test_disabled_jst_item_is_blocked() -> None:
    source = valid_source()
    source["jst_invoice_goods"][0]["invoice_enabled"] = False
    invoice = build(source)
    assert any("标记为不开票" in error for error in invoice["errors"])


def test_specification_uses_color_spec_only() -> None:
    source = valid_source()
    mapping = source["jst_invoice_goods"][0]
    mapping["properties_value"] = "原始颜色规格"
    mapping["specification"] = "过时的规范值"
    mapping["invoice_spec"] = "开票规格"
    assert build(source)["detail_lines"][0]["specification"] == "原始颜色规格"
    mapping["properties_value"] = None
    assert build(source)["detail_lines"][0]["specification"] is None
    del mapping["properties_value"]
    del mapping["specification"]
    del mapping["颜色及规格"]
    assert build(source)["detail_lines"][0]["specification"] is None


def test_duplicate_selected_application_is_reported() -> None:
    source = valid_source()
    source["selected_application_ids"] = ["A-1", "A-1"]
    root = Path(tempfile.mkdtemp())
    try:
        input_path = root / "input.json"
        output_path = root / "plan.json"
        input_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(input_path), "--output", str(output_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 1
        plan = json.loads(output_path.read_text(encoding="utf-8"))
        assert plan["summary"]["plan_count"] == 1
        assert any("存在重复值" in error for error in plan["errors"])
    finally:
        for path in root.glob("*"):
            path.unlink()
        root.rmdir()


def test_cli_writes_a_plan() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        input_path = root / "input.json"
        output_path = root / "plan.json"
        input_path.write_text(json.dumps(valid_source(), ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(input_path), "--output", str(output_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        plan = json.loads(output_path.read_text(encoding="utf-8"))
        assert plan["blocked"] is False
        assert plan["invoices"][0]["status"] == "ready_for_export"


def test_zero_rate_is_written_explicitly_and_conflicts_are_checked() -> None:
    for rate,category in [(None,'零税率'),('0','零税率'),(0,'默认'),(None,'0%税率')]:
        source=valid_source();mapping=source['jst_invoice_goods'][0]
        mapping['虚拟分类']=category;mapping['tax_rate']=rate
        source['template_rows'][0]['税率']='0'
        source['template_rows'][1]['税率']='0'
        result=build(source)
        assert result['errors']==[],result['errors']
        line=result['detail_lines'][0]
        assert line['tax_rate']=='0' and line['tax_rate_effective']=='0'
        assert line['tax_rate_source']==('jst_tax_rate' if rate is not None else 'jst_virtual_category')
        assert line['discount_amount']=='-4'
        source['template_rows'][1]['税率']='13%'
        assert any('折扣税率' in e for e in build(source)['errors'])
    source=valid_source();source['jst_invoice_goods'][0].update({'虚拟分类':'零税率','tax_rate':'0.13'})
    assert any('显式税率冲突' in e for e in build(source)['errors'])
    source=valid_source();source['jst_invoice_goods'][0]['虚拟分类']=''
    assert build(source)['errors']


def test_inactive_goods_are_allowed_when_invoice_enabled() -> None:
    source=valid_source();source['jst_invoice_goods'][0]['enabled']=False
    assert build(source)['errors']==[]
    source['jst_invoice_goods'][0]['是否开票']=False
    assert any('标记为不开票' in e for e in build(source)['errors'])


def test_verified_amount_disambiguates_without_changing_discount() -> None:
    source=valid_source();source['template_rows'][0].update({'货物名称':'同名商品','数量':'1','商品金额':'28.80','开票总金额':'86.40'})
    source['template_rows'][1]['商品金额']='-1.40'
    row=deepcopy(source['template_rows'][0]);row['商品金额']='62.00'
    source['template_rows'] += [row,{'申请流水号':'A-1','订单编号':'O-1','商品金额':'-3.00'}]
    source['order_goods'].append({'订单编号':'O-1','商品编码':'SKU-2'})
    second=deepcopy(source['jst_invoice_goods'][0]);second['商品编码']='SKU-2';source['jst_invoice_goods'].append(second)
    source['order_items']=[{'订单编号':'O-1','商品编码':code,'商品标题':'同名商品','数量':'1','match_amount':amount,'match_amount_source':'order_detail_gross','real_total':listed} for code,amount,listed in [('SKU-2','62.00','95.00'),('SKU-1','28.80','47.90')]]
    result=build(source)
    assert result['errors']==[],result['errors']
    assert [r['goods_code'] for r in result['detail_lines']]==['SKU-1','SKU-2']
    assert [r['discount_amount'] for r in result['detail_lines']]==['-1.4','-3']
    source['template_rows'][0]['商品编码']='SKU-2'
    assert any('金额核对证据冲突' in e for e in build(source)['errors'])
    del source['template_rows'][0]['商品编码']
    for item in source['order_items']:item['match_amount_source']='unverified'
    assert any('多个商品编码' in e for e in build(source)['errors'])


def test_additional_fee_without_merchandise_blocks_whole_invoice() -> None:
    source=valid_source();source['template_rows'][0]['货物名称']='价外费用'
    assert any('价外费用' in e for e in build(source)['errors'])


def fee_row(amount: str = '5.00', **fields: str) -> dict:
    return {'申请流水号':'A-1','订单编号':'O-1','货物名称':'价外费用',
            '数量':'1','商品金额':amount, **fields}


def test_additional_fees_accumulate_without_changing_quantity_or_discount() -> None:
    source=valid_source()
    source['template_rows'][0]['开票总金额']='71.30'
    source['template_rows'] += [fee_row(), fee_row('1.00', 税率='13%')]
    invoice=build(source)
    assert invoice['errors']==[], invoice['errors']
    assert invoice['computed_total_amount']=='71.3'
    assert len(invoice['detail_lines'])==1
    line=invoice['detail_lines'][0]
    assert line['original_amount']=='69.3'
    assert line['extra_fee_amount']=='6'
    assert line['extra_fee_source_rows']==[3,4]
    assert line['amount']=='75.3'
    assert line['quantity']=='7'
    assert line['discount_amount']=='-4' and line['discount_source_row']==2
    assert line['goods_code']=='SKU-1'


def test_additional_fee_can_precede_its_unique_product() -> None:
    source=valid_source()
    source['template_rows'][0]['开票总金额']='70.30'
    source['template_rows'].insert(0,fee_row())
    invoice=build(source)
    assert invoice['errors']==[], invoice['errors']
    line=invoice['detail_lines'][0]
    assert line['source_row']==2 and line['extra_fee_source_rows']==[1]
    assert line['discount_source_row']==3 and line['amount']=='74.3'


def test_additional_fee_matching_uses_original_product_amount() -> None:
    source=valid_source()
    source['template_rows'][0]['开票总金额']='70.30'
    source['template_rows'].append(fee_row())
    source['order_items']=[{'订单编号':'O-1','子订单号':'S1','商品编码':'SKU-1','数量':'7',
                            'match_amount':'69.30','match_amount_source':'order_detail_gross'}]
    invoice=build(source)
    assert invoice['errors']==[], invoice['errors']
    assert invoice['detail_lines'][0]['amount']=='74.3'
    assert invoice['detail_lines'][0]['order_item_evidence']['match_amount']=='69.30'


def test_additional_fee_does_not_guess_among_multiple_product_rows() -> None:
    source=valid_source()
    source['template_rows'][0]['开票总金额']='80.30'
    source['template_rows'] += [{'申请流水号':'A-1','订单编号':'O-1','数量':'1','商品金额':'10.00'}, fee_row()]
    invoice=build(source)
    assert invoice['status']=='blocked'
    assert any('价外费用' in error and '2 个正商品源行' in error for error in invoice['errors'])
    assert all(line['extra_fee_amount']=='0' and line['extra_fee_source_rows']==[] for line in invoice['detail_lines'])


def test_additional_fee_requires_same_order_and_no_explicit_field_conflicts() -> None:
    for fields, expected in [({'订单编号':'O-OTHER'}, '0 个正商品源行'),
                             ({'订单编号':''}, '缺少订单编号'),
                             ({'税率':'9%'}, '价外费用税率与归属商品税率不一致'),
                             ({'税率':'未知'}, '不是有效百分比'),
                             ({'商品编码':'OTHER'}, '商品编码与归属商品不一致'),
                             ({'子订单号':'OTHER'}, '子订单号与归属商品不一致')]:
        source=valid_source()
        source['template_rows'][0]['开票总金额']='70.30'
        source['template_rows'].append(fee_row(**fields))
        invoice=build(source)
        assert invoice['status']=='blocked'
        assert any(expected in error for error in invoice['errors']), invoice['errors']


def test_nonpositive_or_nonfinite_additional_fee_is_not_a_discount() -> None:
    for amount in ['-5.00','0','NaN','Infinity']:
        source=valid_source()
        source['template_rows']=source['template_rows'][:1]+[fee_row(amount)]
        invoice=build(source)
        assert invoice['status']=='blocked'
        assert any('价外费用金额' in error for error in invoice['errors'])
        assert invoice['detail_lines'][0]['discount_amount']=='0'
        assert invoice['detail_lines'][0]['extra_fee_source_rows']==[]


def test_additional_fee_does_not_bridge_discount_adjacency() -> None:
    source=valid_source()
    source['template_rows'][0]['开票总金额']='70.30'
    source['template_rows'].insert(1,fee_row())
    invoice=build(source)
    assert invoice['status']=='blocked'
    assert any('负商品金额前没有正商品金额' in error for error in invoice['errors'])
    assert invoice['detail_lines'][0]['discount_amount']=='0'


def test_unknown_status_and_invoice_enabled_are_blocked() -> None:
    source=valid_source();source['template_rows'][0]['申请状态']='未知'
    assert build(source)['errors']
    source=valid_source();source['jst_invoice_goods'][0]['是否开票']='unknown'
    assert build(source)['errors']


def test_bare_virtual_category_is_not_a_rate() -> None:
    for category in ['13','0.13','0','专（13-4）']:
        source=valid_source();source['jst_invoice_goods'][0]['虚拟分类']=category
        assert build(source)['errors']


def test_nonfinite_numbers_report_errors_without_crashing() -> None:
    for field in ['数量','商品金额','开票总金额']:
        for number in ['NaN','Infinity','-Infinity']:
            source=valid_source();source['template_rows'][0][field]=number
            assert build(source)['errors'],(field,number)


def test_explicit_api_rate_must_use_decimal_fraction() -> None:
    for rate in ['13','13%','NaN']:
        source=valid_source();source['jst_invoice_goods'][0]['tax_rate']=rate
        assert build(source)['errors']


def test_direct_code_cannot_override_order_item_conflicts() -> None:
    source=valid_source();source['template_rows'][0].update({'商品编码':'SKU-1','货物名称':'甲'})
    source['order_items']=[{'订单编号':'O-1','子订单号':'S1','商品编码':'SKU-1','商品标题':'乙','数量':'7'}]
    assert build(source)['errors']
    source['order_items'][0].update({'商品标题':'甲','数量':'8'})
    assert build(source)['errors']


def test_no_title_match_cannot_fall_back_to_quantity() -> None:
    source=valid_source();source['template_rows'][0]['货物名称']='不存在的商品'
    source['order_goods'].append({'订单编号':'O-1','商品编码':'SKU-2'})
    source['order_items']=[{'订单编号':'O-1','商品编码':'SKU-1','商品标题':'甲','数量':'7'},
                           {'订单编号':'O-1','商品编码':'SKU-2','商品标题':'乙','数量':'8'}]
    assert build(source)['errors']


def test_suborder_cannot_be_consumed_twice() -> None:
    source=valid_source();source['template_rows']=source['template_rows'][:1]
    source['template_rows'][0]['开票总金额']='138.60'
    source['template_rows'].append(deepcopy(source['template_rows'][0]))
    source['order_items']=[{'订单编号':'O-1','子订单号':'S1','商品编码':'SKU-1','数量':'7'}]
    assert build(source)['errors']


def test_global_missing_application_prevents_export() -> None:
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch
    from template_io import make_output_rows
    source=valid_source();source['selected_application_ids'].append('missing')
    stream=io.StringIO()
    with patch.object(MODULE,'load_json',return_value=source),patch.object(sys,'argv',['build','unused']),redirect_stdout(stream):
        assert MODULE.main()==1
    plan=json.loads(stream.getvalue())
    assert plan['fatal'] and plan['summary']['blocked_count']==1
    try:make_output_rows(plan)
    except ValueError:pass
    else:raise AssertionError('Global failure must prevent export')


def test_empty_selection_and_duplicate_source_rows() -> None:
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch
    for empty in [True,False]:
        source=valid_source()
        if empty:source['selected_application_ids']=[]
        else:
            for row in source['template_rows']:row['源行号']=1
        stream=io.StringIO()
        with patch.object(MODULE,'load_json',return_value=source),patch.object(sys,'argv',['build','unused']),redirect_stdout(stream):
            code=MODULE.main()
        plan=json.loads(stream.getvalue())
        assert (code==0 and not plan['invoices']) if empty else (code==1 and plan['fatal'])


if __name__ == "__main__":
    tests=[fn for name,fn in globals().copy().items() if name.startswith('test_') and callable(fn)]
    for test in tests:test()
    print(f"{len(tests)} invoice plan tests passed")
