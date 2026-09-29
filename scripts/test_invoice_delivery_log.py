import codecs
import unittest

from invoice_delivery_log import render_summary_log


def shop(store='甲店', status='complete', **changes):
    return {
        'store': store, 'status': status, 'selected_count': 4,
        'ready_count': 2, 'blocked_count': 1, 'excluded_count': 1,
        'ready_amount': '10.5', 'blocked_amount': '2', 'excluded_amount': '-3',
        **changes,
    }


def batch(rows, **changes):
    return {
        'type': 'batch', 'status': 'partial', 'shops': rows,
        'query_scope': {'mode': 'all_pending', 'start_date': '2026-07-29',
                        'end_date': '2026-09-29', 'countdown': 'started'},
        **changes,
    }


class SummaryLogTests(unittest.TestCase):
    def render(self, report):
        return render_summary_log(report).decode('utf-8-sig')

    def test_mixed_statuses_show_per_shop_and_only_finished_totals(self):
        report = batch([
            shop(),
            shop('乙店', 'all_blocked', selected_count=1, ready_count=0, blocked_count=1,
                 excluded_count=0, ready_amount='0', blocked_amount='5', excluded_amount='0',
                 exception_reasons=[{'reason': '类型不支持；商品编码缺失', 'count': 1, 'amount': '5'}]),
            shop('丙店', 'failed', ready_count=900, ready_amount='9999', failure_reason='登录已失效'),
            shop('丁店', 'not_executed', ready_count=800, ready_amount='8888'),
        ])
        text = self.render(report)
        self.assertIn('已完成 2 家；失败 1 家；未执行或未完成 1 家', text)
        self.assertIn('已生成税局模板的店铺（1 家）：\r\n  甲店', text)
        self.assertIn('入选申请：5 笔', text)
        self.assertIn('可生成：2 笔，金额 10.50 元', text)
        self.assertIn('暂缓：2 笔，金额 7.00 元', text)
        self.assertIn('负数排除：1 笔，金额 -3.00 元', text)
        self.assertIn('类型不支持；商品编码缺失：1 笔，金额 5.00 元', text)
        self.assertEqual(text.count('类型不支持；商品编码缺失'), 1)
        self.assertIn('处理说明：登录已失效', text)
        self.assertNotIn('9999', text)
        self.assertNotIn('8888', text)
        for section in text.split('逐店结果：', 1)[1].split('\r\n\r\n')[:4]:
            self.assertIn('入选申请：', section)
            self.assertIn('可生成：', section)
            self.assertIn('暂缓：', section)
            self.assertIn('负数排除：', section)

    def test_unknown_is_not_replaced_by_zero(self):
        text = self.render(batch([shop(ready_count=None, ready_amount=None), shop('乙店')]))
        self.assertIn('已生成税局模板：未确认（已知 2 笔；1 家未确认），金额 未确认（已知 10.50 元；1 家未确认）', text)
        self.assertIn('可生成：数量未确认，金额 未确认', text)
        self.assertNotIn('可生成：0 笔', text)
        self.assertNotIn('金额 0.00 元', text)

    def test_no_finished_shops_have_unknown_totals(self):
        text = self.render(batch([shop(status='failed'), shop('乙店', 'pending')]))
        self.assertIn('入选申请：未确认（无已完成店铺）', text)
        self.assertNotIn('金额 0.00 元', text)
        self.assertIn('任务执行失败，未完成资料核验', text)

    def test_single_date_report_and_zero_values(self):
        report = {'type': 'single', 'query_scope': {'mode': 'date', 'date': '2026-09-25'},
                  **shop(status='no_applications', selected_count=0, ready_count=0,
                         blocked_count=0, excluded_count=0, ready_amount='0',
                         blocked_amount='0', excluded_amount='0')}
        text = self.render(report)
        self.assertIn('申请日期：2026-09-25', text)
        self.assertIn('开票倒计时：按本任务范围', text)
        self.assertIn('本次查询及筛选范围无待处理申请', text)
        self.assertIn('可生成：0 笔，金额 0.00 元', text)
        self.assertIn('已生成税局模板的店铺（0 家）', text)

    def test_plan_only_and_negative_exclusion_are_not_generated(self):
        text = self.render(batch([shop('计划店', 'plan_only'), shop('负数店', 'all_excluded',
                               ready_count=0, ready_amount='0')]))
        self.assertIn('仅完成计划，未生成税局模板', text)
        self.assertIn('负数申请按规则全部排除', text)
        self.assertIn('已生成税局模板的店铺（0 家）', text)
        self.assertNotIn('税局模板：已生成', text)

    def test_deterministic_bom_no_actual_issuance_claim_or_unapproved_fields(self):
        report = batch([shop(store='甲店\r\n伪标题', exception_reasons=[{
            'reason': '类型不支持\n商品缺资料', 'count': 1, 'amount': None}])],
            private_path='D:/private/secret.json', webhook_url='https://secret.invalid/token')
        first = render_summary_log(report)
        self.assertEqual(first, render_summary_log(report))
        self.assertTrue(first.startswith(codecs.BOM_UTF8))
        text = first.decode('utf-8-sig')
        self.assertIn('本次仅生成税局模板，尚未提交实际开票', text)
        self.assertIn('甲店 伪标题', text)
        self.assertIn('类型不支持 商品缺资料：1 笔，金额 未确认', text)
        self.assertNotIn('已开票：', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('private', text)

    def test_nonfinite_and_boolean_metrics_remain_unknown(self):
        text = self.render(batch([shop(ready_count=True, ready_amount='NaN', blocked_amount='Infinity')]))
        self.assertIn('可生成：数量未确认，金额 未确认', text)
        self.assertIn('暂缓：1 笔，金额 未确认', text)


if __name__ == '__main__':
    unittest.main()
