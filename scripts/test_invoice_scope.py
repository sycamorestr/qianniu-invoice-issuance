"""Frozen invoice scope boundaries; no browser or local account data."""
from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import invoice_scope


class InvoiceScopeTests(unittest.TestCase):
    def test_two_calendar_months_clamp_month_end_and_cross_year(self):
        cases = (
            ('2026-09-28', '2026-07-28'),
            ('2024-04-30', '2024-02-29'),
            ('2023-04-30', '2023-02-28'),
            ('2026-01-31', '2025-11-30'),
            ('2026-03-31', '2026-01-31'),
            ('2024-02-29', '2023-12-29'),
        )
        for end, start in cases:
            with self.subTest(end=end):
                scope = invoice_scope.make_scope(all_pending=True, today=date.fromisoformat(end))
                self.assertEqual(scope, {'mode': 'all_pending', 'start_date': start, 'end_date': end})
                self.assertEqual(invoice_scope.scope_date_range(scope), {'start': start, 'end': end})

    def test_today_uses_beijing_midnight_not_utc_or_host_timezone(self):
        for hour, minute, expected in ((15, 59, '2026-09-28'), (16, 0, '2026-09-29')):
            with self.subTest(hour=hour, minute=minute):
                instant = datetime(2026, 9, 28, hour, minute, tzinfo=timezone.utc)
                with patch.object(invoice_scope, 'datetime') as clock:
                    clock.now.side_effect = lambda tz: instant.astimezone(tz)
                    scope = invoice_scope.make_scope(all_pending=True)
                    clock.now.assert_called_once()
                    self.assertEqual(clock.now.call_args.args[0].utcoffset(None), timedelta(hours=8))
                self.assertEqual(scope['end_date'], expected)

    def test_all_pending_resume_never_asks_clock_for_a_new_window(self):
        frozen = invoice_scope.make_scope(all_pending=True, today=date(2024, 4, 30))
        saved = {'date': None, 'query_scope': frozen}
        with patch.object(invoice_scope, 'datetime') as clock:
            clock.now.side_effect = AssertionError('resume must not use today')
            self.assertEqual(invoice_scope.resolve_scope(saved=saved), frozen)
            self.assertEqual(invoice_scope.resolve_scope(all_pending=True, saved=saved), frozen)

    def test_dated_legacy_records_keep_original_shape_and_label(self):
        scope = invoice_scope.resolve_scope(saved={'date': '2026-09-22'})
        self.assertEqual(scope, {'mode': 'date', 'date': '2026-09-22'})
        self.assertEqual(invoice_scope.scope_fields(scope), {})
        self.assertEqual(invoice_scope.scope_label(scope), '2026-09-22')
        self.assertEqual(invoice_scope.scope_date_range(scope), {'start': '2026-09-22', 'end': '2026-09-22'})

    def test_all_scope_fields_copy_the_frozen_range(self):
        scope = invoice_scope.make_scope(all_pending=True, today=date(2026, 9, 28))
        fields = invoice_scope.scope_fields(scope)
        self.assertEqual(fields, {'query_scope': scope})
        self.assertIsNot(fields['query_scope'], scope)
        self.assertEqual(invoice_scope.scope_label(scope), 'all-pending')

    def test_new_scopes_default_to_started_countdown(self):
        dated = invoice_scope.resolve_scope(date='2026-09-28')
        self.assertEqual(dated, {'mode': 'date', 'date': '2026-09-28', 'countdown': 'started'})
        self.assertEqual(invoice_scope.scope_fields(dated), {'query_scope': dated})
        self.assertEqual(invoice_scope.scope_label(dated), '2026-09-28')
        all_pending = invoice_scope.resolve_scope(all_pending=True)
        self.assertEqual(all_pending['countdown'], 'started')
        self.assertEqual(invoice_scope.scope_from_record({'date': None, 'query_scope': all_pending}), all_pending)

    def test_filtered_scopes_survive_explicit_and_inferred_resume(self):
        for kwargs in ({'date': '2026-09-28'}, {'all_pending': True}):
            frozen = invoice_scope.resolve_scope(**kwargs)
            saved = {'date': frozen.get('date'), 'query_scope': frozen}
            with self.subTest(scope=frozen):
                self.assertEqual(invoice_scope.resolve_scope(saved=saved), frozen)
                self.assertEqual(invoice_scope.resolve_scope(saved=saved, **kwargs), frozen)
                self.assertEqual(invoice_scope.scope_fields(frozen), {'query_scope': frozen})

    def test_legacy_scopes_never_acquire_filter_on_resume(self):
        for scope in (invoice_scope.make_scope('2026-09-28'),
                      invoice_scope.make_scope(all_pending=True, today=date(2026, 9, 28))):
            saved = {'date': scope.get('date'), **invoice_scope.scope_fields(scope)}
            kwargs = {'all_pending': True} if scope['mode'] == 'all_pending' else {'date': scope['date']}
            with self.subTest(scope=scope):
                self.assertEqual(invoice_scope.resolve_scope(saved=saved), scope)
                self.assertEqual(invoice_scope.resolve_scope(saved=saved, **kwargs), scope)
                self.assertNotIn('countdown', invoice_scope.resolve_scope(saved=saved, **kwargs))

    def test_invalid_or_unrecognized_countdown_is_not_ignored(self):
        for value in (None, False, True, 100, '100', 'all', 'not_started', ''):
            for scope in (invoice_scope.make_scope('2026-09-28'),
                          invoice_scope.make_scope(all_pending=True, today=date(2026, 9, 28))):
                with self.subTest(value=value, mode=scope['mode']), self.assertRaises(ValueError):
                    invoice_scope.scope_from_record({'date': scope.get('date'),
                                                     'query_scope': {**scope, 'countdown': value}})

    def test_exactly_one_new_scope_is_required(self):
        for kwargs in ({}, {'date': ''}, {'date': '2026-02-30'}, {'date': '20260922'},
                       {'date': '2026-09-22', 'all_pending': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                invoice_scope.make_scope(**kwargs)

    def test_resume_refuses_switching_modes_or_dates(self):
        frozen = invoice_scope.make_scope(all_pending=True, today=date(2026, 9, 28))
        cases = (
            ({'date': '2026-09-22'}, {'all_pending': True}),
            ({'date': '2026-09-22'}, {'date': '2026-09-23'}),
            ({'date': None, 'query_scope': frozen}, {'date': '2026-09-22'}),
            ({'date': None, 'query_scope': frozen}, {'date': '2026-09-22', 'all_pending': True}),
        )
        for saved, kwargs in cases:
            with self.subTest(saved=saved, kwargs=kwargs), self.assertRaises(ValueError):
                invoice_scope.resolve_scope(saved=saved, **kwargs)

    def test_corrupt_or_ambiguous_saved_ranges_are_rejected(self):
        scope = invoice_scope.make_scope(all_pending=True, today=date(2026, 9, 28))
        cases = (
            {'date': None},
            {'date': '2026-09-22', 'query_scope': scope},
            {'date': None, 'query_scope': {'mode': 'all_pending'}},
            {'date': None, 'query_scope': {**scope, 'start_date': '2026-07-27'}},
            {'date': None, 'query_scope': {**scope, 'end_date': 'invalid'}},
            {'date': None, 'query_scope': {**scope, 'extra': True}},
            {'date': None, 'query_scope': []},
            {'date': '2026-09-22', 'query_scope': {'mode': 'date', 'date': '2026-09-23'}},
            {'date': '2026-09-22', 'query_scope': {'mode': 'unknown'}},
        )
        for record in cases:
            with self.subTest(record=record), self.assertRaises(ValueError):
                invoice_scope.scope_from_record(record)


if __name__ == '__main__':
    unittest.main()
