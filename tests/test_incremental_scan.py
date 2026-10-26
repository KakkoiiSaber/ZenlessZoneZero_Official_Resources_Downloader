from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from src.scrape import collect
from src.scrape.http import now


def old_post(ident, kind='news'):
    return {'id': ident, 'title': ident, 'url': 'https://zzz.mihoyo.com/news/' + ident,
            'published_at': '2020-01-01T00:00:00+00:00', 'index_signature': 'same',
            'status': 'ok', 'parser_version': collect.PARSER_VERSION,
            'checked_at': '2020-01-01T00:00:00+00:00', 'source_kind': kind,
            'videos': [], 'images': [], 'minas_links': []}


def official_item(ident):
    return {'iInfoId': ident, 'sTitle': ident, 'dtStartTime': '2026-10-25 12:00:00'}


def mi_item(ident, top=False):
    return {'post': {'post_id': ident, 'subject': ident, 'created_at': 1792900800,
                     'post_status': {'is_top': top}}}


class IncrementalScanTests(unittest.TestCase):
    def test_wiki_cover_comes_from_index_without_refetching_old_body(self):
        client = Mock()
        cover = 'https://upload-bbs.miyoushe.com/upload/cover.jpg?x-oss-process=image/format,webp'
        client.get.return_value = {'list': [{'id': cid, 'name': str(cid), 'list': [
            {'content_id': cid, 'title': '主题', 'icon': cover}]} for cid in collect.CATEGORY_IDS]}
        entries, _ = collect.discover_wiki(client)
        expected = cover.split('?')[0].replace('upload-bbs.miyoushe.com', 'upload-bbs.mihoyo.com')
        self.assertEqual(entries['73']['cover_url'], expected)
        self.assertNotIn('cover_url', entries['98'])
        previous = {ident: {**entry, 'status': 'ok', 'parser_version': collect.PARSER_VERSION}
                    for ident, entry in entries.items()}
        for entry in previous.values():
            entry.pop('cover_url', None)
        fetch = Mock()
        with tempfile.TemporaryDirectory() as temp:
            updated, failures, changed = collect.update_records('wiki', entries, previous,
                Path(temp) / 'progress.json', fetch, 1, 0)
        self.assertEqual((failures, changed), (0, 0))
        fetch.assert_not_called()
        self.assertEqual(updated['73']['cover_url'], expected)

    def test_official_consumes_page_with_old_pinned_post_and_stops_on_known_page(self):
        previous = {ident: old_post(ident) for ident in ['pin', 'old', 'older']}
        previous['video'] = old_post('video', 'video')
        calls = []

        def get(url, params):
            calls.append((params['iChanId'], params['iPage']))
            if params['iChanId'] == 1330:
                return {'iTotal': 1000, 'list': [official_item('video')]}
            items = {1: ['pin', 'new', 'old'], 2: ['older']}[params['iPage']]
            return {'iTotal': 1000, 'list': [official_item(ident) for ident in items]}

        client = Mock(get=get)
        result = collect.discover_official(client, previous)
        self.assertIn('new', result)
        self.assertEqual(calls, [(273, 1), (273, 2), (1330, 1)])

    def test_official_channel_boundaries_are_independent(self):
        client = Mock()
        client.get.side_effect = [
            {'iTotal': 500, 'list': [official_item('known-news')]},
            {'iTotal': 2, 'list': [official_item('new-video')]},
            {'iTotal': 2, 'list': [official_item('known-video')]},
        ]
        result = collect.discover_official(client, {
            'known-news': old_post('known-news'), 'known-video': old_post('known-video', 'video')})
        self.assertIn('new-video', result)
        self.assertEqual(client.get.call_count, 3)

    def test_miyoushe_checks_whole_page_not_first_known_pinned_post(self):
        client = Mock()
        client.get.side_effect = [
            {'list': [mi_item('pin', True), mi_item('new'), mi_item('old')], 'last_id': 'cursor1'},
            {'list': [mi_item('older')], 'last_id': 'cursor2'},
        ]
        result = collect.discover_miyoushe(client, {ident: old_post(ident) for ident in ['pin', 'old', 'older']})
        self.assertIn('new', result)
        self.assertEqual(client.get.call_count, 2)
        self.assertEqual(client.get.call_args.args[1]['last_id'], 'cursor1')

    def test_initial_or_explicit_full_scan_reads_to_end(self):
        client = Mock()
        client.get.side_effect = [
            {'list': [mi_item('old')], 'last_id': 'cursor1'},
            {'list': [mi_item('new-backdated')], 'is_last': True},
        ]
        self.assertIn('new-backdated', collect.discover_miyoushe(client))
        self.assertEqual(client.get.call_count, 2)

    def test_default_skip_old_bodies_but_retry_failures_and_parser_changes(self):
        old = old_post('old')
        self.assertFalse(collect.needs_refresh(old, old, 0, refresh_expiring=False))
        self.assertTrue(collect.needs_refresh({**old, 'status': 'error'}, old, 0, refresh_expiring=False))
        self.assertTrue(collect.needs_refresh({**old, 'parser_version': 0}, old, 0, refresh_expiring=False))
        self.assertTrue(collect.needs_refresh(old, {**old, 'index_signature': 'changed'}, 0, refresh_expiring=False))

    def test_delta_retains_history_and_only_fetches_new_post(self):
        with tempfile.TemporaryDirectory() as temp:
            old = old_post('old')
            new = {**collect.post_index_entry(old_post('new')), 'index_signature': 'new'}
            entries = {'old': collect.post_index_entry(old), 'new': new}
            fetch = Mock(side_effect=lambda entry: {**entry, 'status': 'ok', 'checked_at': now(),
                                                    'parser_version': collect.PARSER_VERSION})
            records, errors, changed = collect.update_records('posts', entries, {'old': old},
                Path(temp) / 'progress.json', fetch, 1, 0, refresh_expiring=False)
            self.assertEqual(set(records), {'old', 'new'})
            self.assertEqual(records['old'], old)
            self.assertEqual((errors, changed), (0, 1))
            fetch.assert_called_once_with(new)

    def test_resumed_fresh_body_not_overwritten_with_stale_previous_body(self):
        with tempfile.TemporaryDirectory() as temp:
            old = old_post('old')
            old['videos'] = [{'selected': {'url': 'old-url'}}]
            fresh = {**old, 'videos': [{'selected': {'url': 'renewed-url'}}]}
            from src.scrape.http import atomic_json
            path = Path(temp) / 'progress.json'
            atomic_json(path, {'old': fresh})
            fetch = Mock()
            records, _, _ = collect.update_records('posts', {'old': collect.post_index_entry(old)},
                {'old': old}, path, fetch, 1, 0, refresh_expiring=False)
            fetch.assert_not_called()
            self.assertEqual(records['old']['videos'], fresh['videos'])

    def test_only_expiring_finally_selected_videos_are_renewed(self):
        expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        good = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        selected, unused, valid = (old_post(ident) for ident in ['selected', 'unused', 'valid'])
        for post, expiry in [(selected, expired), (unused, expired), (valid, good)]:
            post['videos'] = [{'selected': {'url': 'signed-url', 'expires_at': expiry}}]
        mi = {post['id']: post for post in [selected, unused, valid]}
        wiki = {'1': {'resources': [
            {'id': 'wiki:1:video:selected:v1', 'selected_source': 'miyoushe', 'expires_at': expired},
            {'id': 'wiki:1:video:valid:v2', 'selected_source': 'miyoushe', 'expires_at': good},
        ]}}
        client = Mock()
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp, patch.object(collect, 'fetch_post') as fetch:
            renewed = deepcopy(selected)
            renewed['videos'][0]['selected']['expires_at'] = good
            fetch.return_value = renewed
            errors, count = collect.renew_selected_videos(client, wiki, {'official': {}, 'miyoushe': mi}, Path(temp), 1)
            self.assertEqual((errors, count), (0, 1))
            self.assertEqual(fetch.call_args.args[2]['id'], 'selected')
            self.assertEqual(mi['unused']['videos'][0]['selected']['expires_at'], expired)
            self.assertEqual(mi['selected']['videos'][0]['selected']['expires_at'], good)


if __name__ == '__main__':
    unittest.main()
