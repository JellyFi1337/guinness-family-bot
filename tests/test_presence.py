import unittest

from utils.presence import PresenceManager


class PresenceTests(unittest.TestCase):
    def test_extracts_milton_players_from_master_list(self):
        payload = {
            "one": {"name": "GTA5RP.COM | Milton", "players": 843},
            "two": {"name": "GTA5RP.COM | Eclipse", "players": 1200},
        }

        self.assertEqual(PresenceManager._extract_players(payload, "Milton"), 843)

    def test_player_word_uses_russian_plural_rules(self):
        self.assertEqual(PresenceManager._players_word(1), "игрок")
        self.assertEqual(PresenceManager._players_word(2), "игрока")
        self.assertEqual(PresenceManager._players_word(5), "игроков")
        self.assertEqual(PresenceManager._players_word(11), "игроков")
        self.assertEqual(PresenceManager._players_word(22), "игрока")
