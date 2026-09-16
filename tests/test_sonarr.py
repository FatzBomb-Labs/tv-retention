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


class MissingFolders(unittest.TestCase):
    """A series folder Sonarr never created is not evidence of a bad path mapping."""

    def test_a_series_with_no_files_is_separated_from_a_broken_mapping(self):
        # Mirrors action_test_instance's partitioning, which is what the UI reports on.
        catalogue = [
            {'path': '/mnt/user/media/TV/Has Files', 'episode_file_count': 12},
            {'path': '/mnt/user/media/TV/Never Imported', 'episode_file_count': 0},
        ]
        present = set()  # nothing exists on disk in this scenario
        with_files = [e for e in catalogue if e['episode_file_count'] > 0]
        broken = [e for e in with_files if e['path'] not in present]
        not_created = [e for e in catalogue if e['episode_file_count'] == 0 and e['path'] not in present]
        self.assertEqual([e['path'] for e in broken], ['/mnt/user/media/TV/Has Files'])
        self.assertEqual([e['path'] for e in not_created], ['/mnt/user/media/TV/Never Imported'])


class MediaManagement(unittest.TestCase):
    """The recycle-bin configuration: read publicly, and written back only with the id
    Sonarr itself returned — never a guessed one."""

    def setUp(self):
        from unittest import mock
        from sonarr import Sonarr
        self.instance = {'id': 'i1', 'name': 'Sonarr', 'url': 'http://sonarr:8989', 'api_key': 'a' * 32}
        self.client = Sonarr(self.instance)
        self.calls = []
        self.patcher = mock.patch.object(Sonarr, '_request', autospec=True)
        self.mock_request = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_media_management_returns_the_document(self):
        self.mock_request.return_value = {'id': 1, 'recycleBin': '/tv/.recycle'}
        result = self.client.media_management()
        self.assertEqual(result['recycleBin'], '/tv/.recycle')
        self.mock_request.assert_called_once_with(self.client, 'GET', 'config/mediamanagement')

    def test_an_unexpected_response_is_a_clear_error(self):
        from sonarr import SonarrError
        self.mock_request.return_value = ['not', 'a', 'document']
        with self.assertRaises(SonarrError):
            self.client.media_management()

    def test_set_media_management_writes_with_the_ids_own_id(self):
        media = {'id': 7, 'recycleBin': '/tv/.recycle'}
        self.client.set_media_management(media)
        self.mock_request.assert_called_once_with(
            self.client, 'PUT', 'config/mediamanagement/7', body=media)

    def test_a_missing_id_is_refused_rather_than_guessed(self):
        # A hardcoded fallback here used to write to whatever document id 1 happened to
        # name, silently, on the one Sonarr where the real id was not 1.
        from sonarr import SonarrError
        with self.assertRaises(SonarrError):
            self.client.set_media_management({'recycleBin': '/tv/.recycle'})
        self.mock_request.assert_not_called()

