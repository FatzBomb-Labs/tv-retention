import unittest

import context  # noqa: F401
from core import DEFAULTS, Rejected, redact, validate_cron, validate_settings


def base(**overrides):
    document = {
        'instances': [{'id': 'i1', 'name': 'Series', 'url': 'http://sonarr:8989',
                       'api_key': 'a' * 32, 'path_maps': [{'from': '/tv', 'to': '/mnt/user/media/TV'}]}],
        'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 5, 'series_title': 'Show',
                   'tvdb_id': 99, 'path': '/mnt/user/media/TV/Show', 'keep_days': 30}],
    }
    document.update(overrides)
    return document


class Cron(unittest.TestCase):
    def test_accepts_five_fields(self):
        self.assertEqual(validate_cron(' 0  4 * * * '), '0 4 * * *')

    def test_rejects_wrong_field_count(self):
        with self.assertRaises(Rejected):
            validate_cron('0 4 * *')

    def test_rejects_shell_injection(self):
        with self.assertRaises(Rejected):
            validate_cron('0 4 * * * ; rm -rf /')


class Settings(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(validate_settings(DEFAULTS)['schedule']['test_mode'], True)
        self.assertEqual(validate_settings(DEFAULTS)['schedule']['timezone'], 'America/New_York')

    def test_schedule_timezone_is_preserved_and_invalid_zones_are_refused(self):
        document = validate_settings(base(schedule={'timezone': 'America/New_York'}))
        self.assertEqual(document['schedule']['timezone'], 'America/New_York')
        with self.assertRaises(Rejected):
            validate_settings(base(schedule={'timezone': 'Not/A_Timezone'}))

    def test_old_alert_display_preferences_are_dropped(self):
        document = base(alerts={'header': 'all', 'acknowledge': False, 'muted': [],
                                'test_banner': 'full'})
        alerts = validate_settings(document)['alerts']
        self.assertNotIn('header', alerts)
        self.assertNotIn('acknowledge', alerts)
        self.assertNotIn('muted', alerts)
        self.assertNotIn('test_banner', alerts)

    def test_test_mode_defaults_on_and_the_schedule_defaults_off(self):
        # A fresh install cannot delete unattended: no schedule, and Test Mode on if one
        # is enabled before anybody has watched a run go through.
        settings = validate_settings({})
        self.assertTrue(settings['schedule']['test_mode'])
        self.assertFalse(settings['schedule']['enabled'])

    def test_rule_requires_a_condition(self):
        document = base()
        document['rules'][0].pop('keep_days')
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_rule_requires_a_known_instance(self):
        document = base()
        document['rules'][0]['instance_id'] = 'missing'
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_rule_without_series_is_unmatched(self):
        document = base()
        document['rules'][0].pop('series_id')
        self.assertEqual(validate_settings(document)['rules'][0]['match_status'], 'unmatched')

    def test_duplicate_folders_are_rejected(self):
        document = base()
        second = dict(document['rules'][0], id='r2')
        document['rules'].append(second)
        with self.assertRaises(Rejected):
            validate_settings(document)


    def test_traversal_is_rejected(self):
        document = base()
        document['rules'][0]['path'] = '/mnt/user/media/../../etc/passwd'
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_masked_key_keeps_the_stored_secret(self):
        stored = validate_settings(base())
        document = base()
        document['instances'][0]['api_key'] = '********'
        self.assertEqual(validate_settings(document, previous=stored)['instances'][0]['api_key'], 'a' * 32)

    def test_masked_key_without_a_stored_secret_is_rejected(self):
        document = base()
        document['instances'][0]['api_key'] = '********'
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_redaction_hides_keys(self):
        self.assertEqual(redact(validate_settings(base()))['instances'][0]['api_key'], '********')




    def test_combine_mode_is_checked(self):
        document = base()
        document['rules'][0]['combine'] = 'whenever'
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_keep_age_accepts_days_weeks_months_and_years(self):
        expected = {'30': 30, '30d': 30, '24w': 168, '6m': 180, '1y': 365}
        for entered, days in expected.items():
            document = base()
            document['rules'][0]['keep_days'] = entered
            self.assertEqual(validate_settings(document)['rules'][0]['keep_days'], days)

    def test_keep_age_rejects_fractions_unknown_units_and_excessive_values(self):
        for entered in ('1.5w', '2q', '101y'):
            document = base()
            document['rules'][0]['keep_days'] = entered
            with self.assertRaises(Rejected):
                validate_settings(document)

    def test_auto_reenable_is_a_validated_rule_setting(self):
        document = base()
        document['rules'][0]['auto_reenable'] = 'yes'
        self.assertTrue(validate_settings(document)['rules'][0]['auto_reenable'])

    def test_url_must_be_http(self):
        document = base()
        document['instances'][0]['url'] = 'ftp://sonarr'
        with self.assertRaises(Rejected):
            validate_settings(document)


if __name__ == '__main__':
    unittest.main()


class OneRulePerSeries(unittest.TestCase):
    """A rule binds to one Sonarr series, so that binding is what must be unique.

    Keyed on the folder instead, a rule stored before Sonarr moved the series and one
    added afterwards carried different paths, bound to the same series, and were both
    processed — two keep windows deleting each other's episodes.
    """

    def rule(self, rid, **over):
        entry = {'id': rid, 'instance_id': 'i1', 'series_id': 5, 'series_title': 'Show',
                 'tvdb_id': 99, 'path': '/mnt/user/media/TV/Show', 'keep_days': 30}
        entry.update(over)
        return entry

    def test_two_rules_on_one_series_are_refused_whatever_their_folders_say(self):
        document = base(rules=[self.rule('r1'),
                               self.rule('r2', path='/mnt/user/media/TV/Show (2019)')])
        with self.assertRaises(Rejected) as caught:
            validate_settings(document)
        self.assertIn('same Sonarr series', str(caught.exception))

    def test_two_sonarrs_may_share_a_folder(self):
        # The old folder key was instance-blind, so two Sonarrs over one tree could not
        # both be managed. Different series, same path, and nothing is ambiguous.
        document = base(instances=[
            {'id': 'i1', 'name': 'Series', 'url': 'http://sonarr:8989', 'api_key': 'a' * 32},
            {'id': 'i2', 'name': 'Anime', 'url': 'http://sonarr:8990', 'api_key': 'b' * 32}])
        document['rules'] = [self.rule('r1'), self.rule('r2', instance_id='i2', series_id=6)]
        self.assertEqual(len(validate_settings(document)['rules']), 2)

    def test_a_rule_that_never_matched_is_still_identified_by_where_it_points(self):
        # No series id to be unique by, so the folder is all there is.
        document = base(rules=[self.rule('r1', series_id=None),
                               self.rule('r2', series_id=None)])
        with self.assertRaises(Rejected) as caught:
            validate_settings(document)
        self.assertIn('same folder', str(caught.exception))

    def test_one_matched_and_one_not_are_not_compared_at_all(self):
        document = base(rules=[self.rule('r1'), self.rule('r2', series_id=None)])
        self.assertEqual(len(validate_settings(document)['rules']), 2)


class AutomationFromTheForm(unittest.TestCase):
    """The Automation page posts what somebody typed, so validation gets strings.

    Both fields are free text — a comma list of season numbers and a line per phrase — and
    the browser has no way to know a season number from a word. Everything that makes them
    a list rather than a paragraph is done here.
    """

    def automation(self, **over):
        return validate_settings(base(automation=over))['automation']

    def test_season_numbers_arrive_as_strings_and_are_sorted(self):
        found = self.automation(exclude_seasons=['2', '0', '2'])
        self.assertEqual(found['exclude_seasons'], [0, 2])

    def test_a_word_where_a_season_number_belongs_is_refused(self):
        with self.assertRaises(Rejected) as caught:
            self.automation(exclude_seasons=['specials'])
        self.assertIn('whole number', str(caught.exception))

    def test_phrases_keep_the_order_they_were_typed_in(self):
        # A pattern list is read top to bottom by whoever wrote it. Sorting it would make
        # the box disagree with itself the first time it was saved.
        found = self.automation(exclude_episodes=['christmas special', 'behind the scenes'])
        self.assertEqual(found['exclude_episodes'], ['christmas special', 'behind the scenes'])

    def test_the_same_phrase_twice_is_stored_once(self):
        found = self.automation(exclude_episodes=['pilot', 'pilot'])
        self.assertEqual(found['exclude_episodes'], ['pilot'])

    def test_a_document_with_no_automation_at_all_still_gets_the_keys(self):
        # Settings written before the section existed. Every reader indexes every key, and
        # every question answers with its default rather than with nothing.
        document = base()
        document.pop('automation', None)
        found = validate_settings(document)['automation']
        self.assertEqual(found['exclude_seasons'], [])
        self.assertEqual(found['exclude_folders'], [])
        self.assertEqual(found['exclude_episodes'], [])
        self.assertIs(found['exclude_specials'], True)
        self.assertIs(found['search_after_monitor'], False)

    def test_the_two_groups_nothing_ever_read_are_gone(self):
        """They validated, stored and rendered, and no code anywhere consulted them.

        A setting that does nothing is worse than an absent one, because the page it sits
        on is the documentation. A stale value posted by an old client is dropped.
        """
        found = validate_settings(base(automation={
            'monitoring': {'in_scope_unmonitored': 'monitor'},
            'persistence': {'monitored_out_scope': 'unmonitor'},
        }))['automation']
        self.assertNotIn('monitoring', found)
        self.assertNotIn('persistence', found)

    def test_specials_are_excluded_until_somebody_says_otherwise(self):
        # The safety it always was, now visible: it appears on the series card as a cause
        # rather than quietly removing season 0 from consideration.
        self.assertIs(validate_settings(base())['automation']['exclude_specials'], True)


class AirDates(unittest.TestCase):
    """Which services may be asked, in what order, and what happens when none can answer."""

    def air(self, **over):
        return validate_settings(base(air_dates=over))['air_dates']

    def test_the_order_is_the_setting(self):
        # The first enabled provider that answers wins, so moving a row is the whole of how
        # somebody says "ask Plex before TMDB".
        found = self.air(providers=['plex', 'tmdb'])
        self.assertEqual(found['providers'][:2], ['plex', 'tmdb'])

    def test_a_provider_this_version_knows_about_is_never_silently_absent(self):
        found = self.air(providers=['tmdb'])
        self.assertEqual(set(found['providers']), set(DEFAULTS['air_dates']['providers']))

    def test_a_provider_from_a_later_version_is_dropped_rather_than_refused(self):
        # A settings document must still load after a provider is removed.
        self.assertNotIn('napster', self.air(providers=['napster', 'tmdb'])['providers'])

    def test_enabling_something_not_in_the_list_enables_nothing(self):
        self.assertEqual(self.air(providers=['tmdb'], enabled=['napster'])['enabled'], [])

    def test_estimating_is_the_default_and_reaches_the_evaluator(self):
        # Two names for one decision, derived rather than stored twice, so the radio on the
        # Safety page and the flag `evaluate` reads cannot drift apart.
        document = validate_settings(base())
        self.assertEqual(document['air_dates']['unresolved'], 'estimate')
        self.assertIs(document['retention']['allow_estimated_dates'], True)

    def test_leaving_it_unresolved_reaches_the_evaluator_too(self):
        document = validate_settings(base(air_dates={'unresolved': 'leave'}))
        self.assertIs(document['retention']['allow_estimated_dates'], False)

    def test_a_document_written_before_the_page_existed_keeps_its_answer(self):
        # The setting lived under `retention` and said the same thing the other way up.
        document = validate_settings(base(retention={'allow_estimated_dates': False}))
        self.assertEqual(document['air_dates']['unresolved'], 'leave')
        self.assertIs(document['retention']['allow_estimated_dates'], False)

    def test_an_answer_nobody_offered_is_refused(self):
        with self.assertRaises(Rejected):
            self.air(unresolved='guess')
