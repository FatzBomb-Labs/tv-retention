"""Shared Sonarr-shaped test data for the freshness and sync suites."""
import datetime as dt

INSTANCE = {'id': 'i1', 'name': 'Series', 'url': 'http://sonarr:8989', 'api_key': 'a' * 32}


def episode(number, monitored=True, has_file=True):
    aired = (dt.date.today() - dt.timedelta(days=400 - number)).isoformat()
    return {'episode_id': number, 'file_id': number if has_file else None, 'has_file': has_file,
            'series_id': 1, 'season': 1, 'episode': number, 'title': f'E{number}',
            'air_date': aired, 'air_source': 'sonarr', 'date_added': '', 'monitored': monitored,
            'path': f'/tv/A/S01E{number:02d}.mkv' if has_file else f'sonarr:episode:{number}',
            'size': 1000}
