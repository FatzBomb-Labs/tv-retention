import tempfile
import unicodedata
import unittest
from pathlib import Path

import context  # noqa: F401
from core import empty_directories, map_path, normalise, scan_media, sidecars_for, unmap_path

MAPS = [{'from': '/tv', 'to': '/mnt/user/media/TV'},
        {'from': '/tv/anime', 'to': '/mnt/user/media/Anime'}]


class Mapping(unittest.TestCase):
    def test_maps_a_container_path(self):
        self.assertEqual(map_path('/tv/Show/S01/E01.mkv', MAPS), '/mnt/user/media/TV/Show/S01/E01.mkv')

    def test_longest_prefix_wins(self):
        self.assertEqual(map_path('/tv/anime/Show/E01.mkv', MAPS), '/mnt/user/media/Anime/Show/E01.mkv')

    def test_unmapped_paths_pass_through(self):
        self.assertEqual(map_path('/movies/A.mkv', MAPS), '/movies/A.mkv')

    def test_a_partial_name_is_not_a_prefix(self):
        self.assertEqual(map_path('/tvshows/A.mkv', MAPS), '/tvshows/A.mkv')

    def test_round_trip(self):
        host = map_path('/tv/Show/E01.mkv', MAPS)
        self.assertEqual(unmap_path(host, MAPS), '/tv/Show/E01.mkv')

    def test_unicode_forms_compare_equal(self):
        composed = '/mnt/user/media/TV/That’s My Jam'
        self.assertEqual(normalise(composed), normalise(unicodedata.normalize('NFD', composed)))

    def test_trailing_slashes_are_ignored(self):
        self.assertEqual(normalise('/mnt/user/media/TV/Show/'), '/mnt/user/media/TV/Show')

    def test_mapping_the_root_itself(self):
        self.assertEqual(map_path('/tv', MAPS), '/mnt/user/media/TV')


class Files(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'Show'
        (self.root / 'Season 01').mkdir(parents=True)
        self.episode = self.root / 'Season 01' / 'Show - S01E01 - Pilot.mkv'
        self.episode.write_text('x')
        for name in ['Show - S01E01 - Pilot.en.srt', 'Show - S01E01 - Pilot.nfo',
                     'Show - S01E01 - Pilot-thumb.jpg', 'Show - S01E02 - Next.mkv',
                     'Show - S01E01 - Pilot.exe']:
            (self.root / 'Season 01' / name).write_text('x')

    def tearDown(self):
        self.temp.cleanup()

    def test_sidecars_match_only_their_own_episode(self):
        found = {Path(path).name for path in sidecars_for(self.episode, ['srt', 'nfo', 'jpg'])}
        self.assertEqual(found, {'Show - S01E01 - Pilot.en.srt', 'Show - S01E01 - Pilot.nfo',
                                 'Show - S01E01 - Pilot-thumb.jpg'})

    def test_unlisted_extensions_are_left_alone(self):
        found = {Path(path).name for path in sidecars_for(self.episode, ['srt'])}
        self.assertEqual(found, {'Show - S01E01 - Pilot.en.srt'})

    def test_media_scan_finds_video_only(self):
        found = {Path(entry['path']).name for entry in scan_media(self.root)}
        self.assertEqual(found, {'Show - S01E01 - Pilot.mkv', 'Show - S01E02 - Next.mkv'})

    def test_empty_directories_exclude_the_root(self):
        (self.root / 'Season 02').mkdir()
        found = empty_directories(self.root)
        self.assertEqual([Path(path).name for path in found], ['Season 02'])

    def test_an_empty_show_folder_is_never_listed(self):
        empty = Path(self.temp.name) / 'Empty Show'
        empty.mkdir()
        self.assertEqual(empty_directories(empty), [])

    def test_missing_folders_scan_cleanly(self):
        self.assertEqual(scan_media(Path(self.temp.name) / 'nope'), [])
        self.assertEqual(empty_directories(Path(self.temp.name) / 'nope'), [])


if __name__ == '__main__':
    unittest.main()
