import tempfile
import unittest
from pathlib import Path

import context  # noqa: F401
from core import classify_orphan, describe_lifecycle, normalise_title

CATALOGUE = [
    {'series_id': 1, 'title': 'Survivor', 'tvdb_id': 76733,
     'path': '/mnt/user/media/TV/Reality/Survivor (2000) {tvdb-76733}'},
    {'series_id': 2, 'title': 'The Daily Show', 'tvdb_id': 71256,
     'path': '/mnt/user/media/TV/News & Talk/Daily Show, The (1996) {tvdb-71256}'},
]
BY_TVDB = {entry['tvdb_id']: entry for entry in CATALOGUE}
BY_TITLE = {normalise_title(entry['title']): entry for entry in CATALOGUE}


class Titles(unittest.TestCase):
    def test_articles_and_punctuation_fold_together(self):
        self.assertEqual(normalise_title('Daily Show, The (1996) {tvdb-71256}'),
                         normalise_title('The Daily Show'))

    def test_accents_fold(self):
        self.assertEqual(normalise_title('Pokémon'), normalise_title('Pokemon'))


class Orphans(unittest.TestCase):
    def test_a_known_id_at_another_path_reads_as_moved(self):
        outcome = classify_orphan('Survivor (2000) {tvdb-76733}', '/mnt/user/media/TV/Old/Survivor', BY_TVDB, BY_TITLE)
        self.assertEqual(outcome['kind'], 'moved')
        self.assertIn('different path', outcome['detail'])

    def test_an_id_sonarr_does_not_know_is_named_as_such(self):
        outcome = classify_orphan('Gone Show (2001) {tvdb-999999}', '/x', BY_TVDB, BY_TITLE)
        self.assertEqual(outcome['kind'], 'unknown_id')
        self.assertEqual(outcome['tvdb_id'], 999999)
        self.assertIn('removed from Sonarr, or its id changed', outcome['detail'])

    def test_a_title_match_without_an_id_is_offered_as_a_guess(self):
        outcome = classify_orphan('The Daily Show', '/x', BY_TVDB, BY_TITLE)
        self.assertEqual(outcome['kind'], 'title_match')
        self.assertIn('No TVDB id in the folder name', outcome['detail'])

    def test_an_unrecognised_folder_is_not_guessed_at(self):
        outcome = classify_orphan('Home Videos 2019', '/x', BY_TVDB, BY_TITLE)
        self.assertEqual(outcome['kind'], 'unmanaged')
        self.assertIsNone(outcome['tvdb_id'])

    def test_the_id_wins_over_a_misleading_title(self):
        # The folder says Survivor but carries the Daily Show's id; the id is authoritative.
        outcome = classify_orphan('Survivor (2000) {tvdb-71256}', '/x', BY_TVDB, BY_TITLE)
        self.assertEqual(outcome['series_title'], 'The Daily Show')


class Lifecycle(unittest.TestCase):
    def test_a_running_show_is_never_flagged(self):
        outcome = describe_lifecycle({'files_in_frame': 0, 'files_total': 5},
                                     {'status': 'continuing', 'ended': False})
        self.assertFalse(outcome['ended'])
        self.assertFalse(outcome['retention_expired'])

    def test_an_ended_show_with_nothing_in_frame_is_expired(self):
        outcome = describe_lifecycle({'files_in_frame': 0, 'files_total': 5},
                                     {'status': 'ended', 'ended': True})
        self.assertEqual(outcome['lifecycle'], 'ended_expired')

    def test_an_ended_show_with_no_files_reads_differently(self):
        outcome = describe_lifecycle({'files_in_frame': 0, 'files_total': 0},
                                     {'status': 'ended', 'ended': True})
        self.assertEqual(outcome['lifecycle'], 'ended_empty')

    def test_an_ended_show_still_inside_its_frame_is_not_expired(self):
        outcome = describe_lifecycle({'files_in_frame': 3, 'files_total': 5},
                                     {'status': 'ended', 'ended': True})
        self.assertTrue(outcome['ended'])
        self.assertFalse(outcome['retention_expired'])
        self.assertEqual(outcome['lifecycle'], 'ended')


class Scan(unittest.TestCase):
    """The walk that finds folders Sonarr does not claim."""

    def setUp(self):
        from main import scan_library
        self.scan_library = scan_library
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for relative in ('Reality/Survivor (2000) {tvdb-76733}/Season 01',
                         'Reality/Orphan Show (2011) {tvdb-999}/Season 01',
                         'News/Daily Show, The (1996) {tvdb-71256}/Season 31'):
            (self.root / relative).mkdir(parents=True)
            (self.root / relative / 'episode.mkv').write_text('x')
        (self.root / 'Reality' / 'Empty Folder').mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def known(self):
        return [str(self.root / 'Reality' / 'Survivor (2000) {tvdb-76733}'),
                str(self.root / 'News' / 'Daily Show, The (1996) {tvdb-71256}')]

    def test_only_the_unclaimed_show_folder_is_reported(self):
        found = [path.name for path in self.scan_library(self.root, self.known())]
        self.assertEqual(found, ['Orphan Show (2011) {tvdb-999}'])

    def test_a_category_folder_is_walked_through_not_reported(self):
        found = [path.name for path in self.scan_library(self.root, self.known())]
        self.assertNotIn('Reality', found)
        self.assertNotIn('News', found)

    def test_an_empty_folder_is_not_an_orphan(self):
        found = [path.name for path in self.scan_library(self.root, self.known())]
        self.assertNotIn('Empty Folder', found)

    def test_with_nothing_known_every_show_folder_is_reported(self):
        found = sorted(path.name for path in self.scan_library(self.root, []))
        self.assertEqual(len(found), 3)


if __name__ == '__main__':
    unittest.main()
