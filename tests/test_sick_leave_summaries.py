"""Regression tests for sick leave exemptions in bot reports."""
import unittest
from datetime import date

from app.summaries import format_daily_debt, format_summary, weekly_summary


class SickLeaveSummaryTests(unittest.TestCase):
    def setUp(self):
        self.members = [
            {'telegram_user_id': 1, 'display_name': 'Пациент'},
            {'telegram_user_id': 2, 'display_name': 'Спортсмен'},
        ]
        self.sick = {1: {'starts_on': '2026-11-23', 'ends_on': '2026-11-25'}}
        self.day = date(2026, 11, 23)

    def test_today_shows_hospital_and_preserves_actual_total(self):
        text = format_summary(self.members, {1, 2}, self.day, pushup_totals={1: 15, 2: 25},
                              daily_norm=20, sick_leaves=self.sick)
        self.assertIn('🏥 Пациент', text)
        self.assertIn('🏋️ Всего отжиманий: 40', text)
        self.assertIn('Выполнили норму: 1/1', text)

    def test_debt_excludes_patient(self):
        text = format_daily_debt(self.members, {1: 0, 2: 0}, 20, self.sick)
        self.assertIn('🏥 Пациент', text)
        self.assertIn('Не выполнили: 1/1', text)
        self.assertNotIn('❌ Пациент', text)

    def test_week_preserves_pushups_and_shows_leave(self):
        text = weekly_summary(self.members, {1: 40, 2: 50}, self.day, sick_leaves=self.sick)
        self.assertIn('🏥 Пациент', text)
        self.assertIn('Всего за неделю: 90', text)


if __name__ == '__main__':
    unittest.main()
