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
        self.assertEqual(found['monitoring'],
                         {'in_scope_unmonitored': 'ask', 'out_scope_monitored': 'ask'})
        self.assertEqual(found['persistence'],
                         {'unmonitored_in_scope': 'ignore', 'monitored_out_scope': 'notice-exclude'})

    def test_ask_me_is_the_default_for_both_monitoring_questions(self):
        """Either half can move hundreds of episodes.

        A default that acts is a default nobody chose, and the two that act are the two
        that download or stop downloading at scale.
        """
        found = validate_settings(base())['automation']['monitoring']
        self.assertEqual(set(found.values()), {'ask'})

    def test_an_answer_nobody_offered_is_refused(self):
        with self.assertRaises(Rejected):
            validate_settings(base(automation={'monitoring': {'in_scope_unmonitored': 'maybe'}}))

    def test_specials_are_excluded_until_somebody_says_otherwise(self):
        # The safety it always was, now visible: it appears on the series card as a cause
        # rather than quietly removing season 0 from consideration.
        self.assertIs(validate_settings(base())['automation']['exclude_specials'], True)
