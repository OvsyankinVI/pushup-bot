"""Authorization and validation tests for sick leave API."""
import unittest
from unittest.mock import Mock
from fastapi import HTTPException
from app.sick_leave import _active_member, _doctor, _dates


class SickLeaveAuthorizationTests(unittest.TestCase):
    def test_invalid_dates_rejected(self):
        for payload in ({'starts_on': '2026-11-30', 'ends_on': '2026-11-29'},
                        {'starts_on': 'bad', 'ends_on': '2026-11-30'},
                        {'starts_on': '2026-11-30'}):
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as ctx:
                _dates(payload)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_inclusive_one_day_period(self):
        self.assertEqual(_dates({'starts_on': '2026-11-23', 'ends_on': '2026-11-23'}),
                         ('2026-11-23', '2026-11-23'))

    def test_inactive_member_rejected(self):
        db = Mock()
        db.member.return_value = {'active': False}
        with self.assertRaises(HTTPException) as ctx:
            _active_member(db, -100, 42)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_non_doctor_rejected(self):
        db = Mock()
        db.member.return_value = {'active': True}
        db.client.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        with self.assertRaises(HTTPException) as ctx:
            _doctor(db, -100, 42)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_active_doctor_allowed(self):
        db = Mock()
        db.member.return_value = {'active': True}
        db.client.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = [{'telegram_user_id': 42}]
        self.assertIsNone(_doctor(db, -100, 42))


if __name__ == '__main__':
    unittest.main()
