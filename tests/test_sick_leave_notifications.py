"""Verify human-readable sick leave Telegram messages."""
import unittest
from app.sick_leave import _event_text


class FakeDb:
    def member(self, chat_id, user_id):
        return {'display_name': {1: 'Пациент', 2: 'Доктор'}[user_id]}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        self.state = {'patient_id': 1, 'starts_on': '2026-11-23', 'ends_on': '2026-11-25'}

    def event(self, kind, before=None, after=None):
        return {'event_type': kind, 'chat_id': -100, 'actor_id': 2,
                'before_state': before, 'after_state': after}

    def test_created(self):
        message = _event_text(self.db, self.event('created', after=self.state))
        self.assertIn('\n🤒 Пациент: Пациент\n', message)
        self.assertIn('Врач: Доктор', message)

    def test_updated(self):
        new = {**self.state, 'ends_on': '2026-11-28'}
        message = _event_text(self.db, self.event('updated', before=self.state, after=new))
        self.assertIn('Было: 2026-11-23 — 2026-11-25', message)
        self.assertIn('Стало: 2026-11-23 — 2026-11-28', message)

    def test_deleted(self):
        message = _event_text(self.db, self.event('deleted', before=self.state))
        self.assertIn('аннулирован', message)
        self.assertIn('Аннулировал: Доктор', message)

    def test_recovered(self):
        message = _event_text(self.db, self.event('recovered', after=self.state))
        self.assertIn('возвращается к отжиманиям', message)


if __name__ == '__main__':
    unittest.main()
