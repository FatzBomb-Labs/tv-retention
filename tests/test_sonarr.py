import unicodedata
import unittest

import context  # noqa: F401
from sonarr import match_rule

CATALOGUE = [
    {'series_id': 1, 'title': 'The Daily Show', 'tvdb_id': 71256, 'path': '/mnt/user/media/TV/News & Talk/Daily Show, The (1996) {tvdb-71256}'},
    {'series_id': 2, 'title': 'Survivor', 'tvdb_id': 76733, 'path': '/mnt/user/media/TV/Reality/Survivor (2000) {tvdb-76733}'},
    {'series_id': 3, 'title': "That's My Jam", 'tvdb_id': 412647, 'path': '/mnt/user/media/TV/Reality/That’s My Jam (2021) {tvdb-412647}'},
]


class Matching(unittest.TestCase):
    def test_matches_by_stored_series_id(self):
        outcome = match_rule({'series_id': 2, 'path': '/somewhere/else'}, CATALOGUE)
        self.assertTrue(outcome['ok'])
        self.assertEqual(outcome['how'], 'series id')

    def test_falls_back_to_tvdb_when_the_id_changed(self):
        outcome = match_rule({'series_id': 999, 'tvdb_id': 76733, 'path': '/gone'}, CATALOGUE)
        self.assertTrue(outcome['ok'])
        self.assertEqual(outcome['series']['series_id'], 2)

    def test_matches_a_folder_the_user_picked(self):
        outcome = match_rule({'path': CATALOGUE[0]['path']}, CATALOGUE)
        self.assertTrue(outcome['ok'])
        self.assertEqual(outcome['how'], 'folder path')

    def test_matches_a_folder_with_a_trailing_slash(self):
        self.assertTrue(match_rule({'path': CATALOGUE[1]['path'] + '/'}, CATALOGUE)['ok'])

    def test_matches_across_unicode_normal_forms(self):
        # Sonarr may report a decomposed form while the share stores a composed one.
        decomposed = unicodedata.normalize('NFD', CATALOGUE[2]['path'])
        self.assertTrue(match_rule({'path': decomposed}, CATALOGUE)['ok'])

    def test_an_unknown_folder_is_reported_not_guessed(self):
        outcome = match_rule({'path': '/mnt/user/media/TV/Reality/Nope'}, CATALOGUE)
        self.assertFalse(outcome['ok'])
        self.assertIn('No Sonarr series', outcome['error'])

    def test_an_ambiguous_folder_is_refused(self):
        duplicated = CATALOGUE + [dict(CATALOGUE[1], series_id=9)]
        outcome = match_rule({'path': CATALOGUE[1]['path']}, duplicated)
        self.assertFalse(outcome['ok'])
        self.assertIn('more than one', outcome['error'])

    def test_an_ambiguous_tvdb_id_falls_through_to_the_path(self):
        duplicated = CATALOGUE + [dict(CATALOGUE[1], series_id=9, path='/other')]
        outcome = match_rule({'tvdb_id': 76733, 'path': CATALOGUE[1]['path']}, duplicated)
        self.assertTrue(outcome['ok'])
        self.assertEqual(outcome['how'], 'folder path')


if __name__ == '__main__':
    unittest.main()
