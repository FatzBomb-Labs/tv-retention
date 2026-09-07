import unittest

import context  # noqa: F401
from core import describe_selectability


def series(path='/mnt/user/media/TV/Show', files=0):
    return {'title': 'Show', 'path': path, 'episode_file_count': files}


class Selectability(unittest.TestCase):
    def test_a_present_folder_is_selectable(self):
        outcome = describe_selectability(series(files=12), exists=True, in_use=False)
        self.assertTrue(outcome['selectable'])
        self.assertEqual(outcome['reason'], '')

    def test_a_series_never_imported_is_selectable_and_tagged(self):
        # A rule binds to a series id, so a show that has not aired yet is a valid choice:
        # Sonarr creates the folder on its first import and the rule picks it up then.
        outcome = describe_selectability(series(files=0), exists=False, in_use=False)
        self.assertTrue(outcome['selectable'])
        self.assertTrue(outcome['awaiting'])
        self.assertEqual(outcome['reason'], 'awaiting first episode')

    def test_a_missing_folder_with_files_names_the_mapping(self):
        outcome = describe_selectability(series(files=12), exists=False, in_use=False)
        self.assertFalse(outcome['selectable'])
        self.assertIn('path mapping', outcome['reason'])
        self.assertIn('12', outcome['reason'])

    def test_a_series_with_no_folder_in_sonarr_is_not_selectable(self):
        outcome = describe_selectability(series(path=''), exists=False, in_use=False)
        self.assertFalse(outcome['selectable'])
        self.assertIn('no folder configured in Sonarr', outcome['reason'])

    def test_a_folder_already_ruled_is_not_selectable_twice(self):
        outcome = describe_selectability(series(files=12), exists=True, in_use=True)
        self.assertFalse(outcome['selectable'])
        self.assertIn('already used', outcome['reason'])

    def test_being_in_use_outranks_a_present_folder(self):
        # Otherwise the picker offers a folder that settings validation will then reject.
        self.assertFalse(describe_selectability(series(files=1), exists=True, in_use=True)['selectable'])
