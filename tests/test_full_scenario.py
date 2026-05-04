import base64
import datetime
import io
import json
import os
import tempfile
import unittest


class FullScenarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, 'data')
        self.upload_dir = os.path.join(self.tmp.name, 'uploads')
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.upload_dir, exist_ok=True)

        os.environ.pop('DATABASE_URL', None)
        os.environ.pop('STORAGE', None)
        os.environ['DATA_DIR'] = self.data_dir
        os.environ['UPLOAD_DIR'] = self.upload_dir
        os.environ['ADMIN_TOKEN'] = 'test-admin-token'
        os.environ['ADMIN_IDS'] = '999999999'
        os.environ['TELEGRAM_BOT_TOKEN'] = 'test-bot-token'
        os.environ['SITE_URL'] = 'https://example.test'
        os.environ.pop('GOOGLE_ANALYTICS_ID', None)
        os.environ.pop('YANDEX_METRIKA_ID', None)
        os.environ.pop('YANDEX_METRICA_ID', None)

        import app as app_module

        self.sent_messages = []

        def fake_send_message(chat_id, text, parse_mode=None, reply_markup=None):
            self.sent_messages.append({
                'chat_id': str(chat_id),
                'text': text,
                'reply_markup': reply_markup,
            })
            return {'ok': True, 'result': {'message_id': len(self.sent_messages)}}

        app_module.send_message = fake_send_message
        self.app_module = app_module
        self.app = app_module.create_app()
        self.client = self.app.test_client()
        self.base_url = 'https://example.test'
        token = base64.b64encode(b'admin:test-admin-token').decode('ascii')
        self.admin_headers = {'Authorization': 'Basic ' + token}

    def tearDown(self):
        self.tmp.cleanup()

    def read_json(self, name, default):
        path = os.path.join(self.data_dir, name)
        if not os.path.exists(path):
            return default
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def write_json(self, name, value):
        with open(os.path.join(self.data_dir, name), 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)

    def test_client_nanny_admin_finance_notification_flow(self):
        admin_page = self.client.get('/admin', headers=self.admin_headers, base_url=self.base_url)
        self.assertEqual(admin_page.status_code, 200)

        nannies = self.read_json('nannies.json', [])
        self.assertTrue(nannies)
        nannies[0]['telegram_user_id'] = 222222222
        self.write_json('nannies.json', nannies)
        nanny_id = str(nannies[0]['id'])
        nanny_token = nannies[0]['portal_token']

        work_date = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
        lead_resp = self.client.post('/api/lead', json={
            'parent_name': 'Test Client',
            'telegram': '111111111',
            'child_name': 'Mila',
            'child_age': '4',
            'work_dates': {work_date: {'time': '09:00-13:00'}},
        }, base_url=self.base_url)
        self.assertEqual(lead_resp.status_code, 200, lead_resp.get_data(as_text=True))
        lead_token = lead_resp.get_json()['lk_url'].rstrip('/').split('/')[-1]

        assign_resp = self.client.post('/admin/assign', headers=self.admin_headers, data={
            'token': lead_token,
            'nanny_id': nanny_id,
            'client_rate_per_hour': '130000',
            'nanny_rate_per_hour': '110000',
        }, base_url=self.base_url)
        self.assertEqual(assign_resp.status_code, 302)

        portal_resp = self.client.get(f'/nanny/portal/{nanny_token}', base_url=self.base_url)
        self.assertEqual(portal_resp.status_code, 200)

        confirm_resp = self.client.post(f'/api/nanny/{nanny_token}/confirm_date', json={
            'client_token': lead_token,
            'date': work_date,
            'action': 'confirm',
        }, base_url=self.base_url)
        self.assertEqual(confirm_resp.status_code, 200, confirm_resp.get_data(as_text=True))

        fact_resp = self.client.post(f'/api/nanny/{nanny_token}/submit_fact', json={
            'client_token': lead_token,
            'date': work_date,
            'fact_start': '09:00',
            'fact_end': '13:00',
        }, base_url=self.base_url)
        self.assertEqual(fact_resp.status_code, 200, fact_resp.get_data(as_text=True))

        client_fact_resp = self.client.post(f'/api/client/{lead_token}/date_action', json={
            'date': work_date,
            'actual_start': '09:00',
            'actual_end': '13:00',
            'review': 'Все прошло хорошо, няня приехала вовремя.',
            'review_stars': 5,
        }, base_url=self.base_url)
        self.assertEqual(client_fact_resp.status_code, 200, client_fact_resp.get_data(as_text=True))

        upload_resp = self.client.post(
            f'/api/client/{lead_token}/upload_receipt?date={work_date}',
            data={'file': (io.BytesIO(b'%PDF-1.4 test receipt'), 'receipt.pdf', 'application/pdf')},
            content_type='multipart/form-data',
            base_url=self.base_url,
        )
        self.assertEqual(upload_resp.status_code, 200, upload_resp.get_data(as_text=True))

        profit_resp = self.client.get('/api/admin/profit?period=all', headers=self.admin_headers, base_url=self.base_url)
        self.assertEqual(profit_resp.status_code, 200, profit_resp.get_data(as_text=True))
        summary = profit_resp.get_json()['summary']
        self.assertEqual(summary['client_total'], 520000)
        self.assertEqual(summary['nanny_total'], 440000)
        self.assertEqual(summary['margin'], 80000)
        self.assertEqual(summary['shifts_done'], 1)

        leads = self.read_json('leads.json', [])
        slot = leads[0]['work_dates'][work_date]
        self.assertEqual(slot['status'], 'confirmed')

        notification_log = self.read_json('notification_log.json', [])
        self.assertGreaterEqual(len(notification_log), 6)
        self.assertTrue(any(item.get('status') == 'delivered' for item in notification_log))
        self.assertGreaterEqual(len(self.sent_messages), 6)

    def test_same_telegram_id_can_open_client_and_agent_portals(self):
        tg_id = 333333333
        self.write_json('referral_agents.json', [{
            'id': '1',
            'name': 'Dual Role Agent',
            'telegram_user_id': tg_id,
            'portal_token': 'agent-dual-token',
            'referral_code': 'dual-agent-code',
            'commission_vnd': 200000,
            'payout_delay_days': 14,
            'is_active': True,
            'created_at': datetime.datetime.utcnow().isoformat(),
        }])

        lead_resp = self.client.post('/api/lead', json={
            'parent_name': 'Dual Client',
            'telegram': '@dualuser',
            'child_name': 'Mila',
            'child_age': '4',
            'work_dates': {},
        }, base_url=self.base_url)
        self.assertEqual(lead_resp.status_code, 200, lead_resp.get_data(as_text=True))
        lead_token = lead_resp.get_json()['lk_url'].rstrip('/').split('/')[-1]

        old_validate = self.app_module.validate_webapp_init_data
        self.app_module.validate_webapp_init_data = lambda init_data, bot_token: {
            'user': json.dumps({'id': tg_id, 'username': 'dualuser', 'first_name': 'Dual'})
        }
        try:
            auth_resp = self.client.post('/api/auth/telegram', json={'init_data': 'valid'}, base_url=self.base_url)
        finally:
            self.app_module.validate_webapp_init_data = old_validate

        self.assertEqual(auth_resp.status_code, 200, auth_resp.get_data(as_text=True))
        data = auth_resp.get_json()
        roles = {p.get('role') for p in data.get('available_portals', [])}
        self.assertIn('client', roles)
        self.assertIn('agent', roles)
        self.assertTrue(any(p.get('url', '').startswith(f'/client/{lead_token}') for p in data['available_portals']))
        self.assertTrue(any(p.get('url', '').startswith('/agent/app') for p in data['available_portals']))

        agent_resp = self.client.get('/agent/app', base_url=self.base_url)
        self.assertEqual(agent_resp.status_code, 302)
        self.assertIn('/agent/agent-dual-token', agent_resp.headers.get('Location', ''))

        client_resp = self.client.get(f'/client/{lead_token}', base_url=self.base_url)
        self.assertEqual(client_resp.status_code, 200)
        self.assertIn(b'/agent/app', client_resp.data)

    def test_nanny_gets_referral_agent_portal_by_default(self):
        tg_id = 444444444
        admin_page = self.client.get('/admin', headers=self.admin_headers, base_url=self.base_url)
        self.assertEqual(admin_page.status_code, 200)

        nannies = self.read_json('nannies.json', [])
        self.assertTrue(nannies)
        nannies[0]['telegram_user_id'] = tg_id
        self.write_json('nannies.json', nannies)

        old_validate = self.app_module.validate_webapp_init_data
        self.app_module.validate_webapp_init_data = lambda init_data, bot_token: {
            'user': json.dumps({'id': tg_id, 'username': 'defaultpartner', 'first_name': 'Nanny'})
        }
        try:
            auth_resp = self.client.post('/api/auth/telegram', json={'init_data': 'valid'}, base_url=self.base_url)
        finally:
            self.app_module.validate_webapp_init_data = old_validate

        self.assertEqual(auth_resp.status_code, 200, auth_resp.get_data(as_text=True))
        data = auth_resp.get_json()
        roles = {p.get('role') for p in data.get('available_portals', [])}
        self.assertIn('nanny', roles)
        self.assertIn('agent', roles)

        agents = self.read_json('referral_agents.json', [])
        agent = next((a for a in agents if str(a.get('telegram_user_id')) == str(tg_id)), None)
        self.assertIsNotNone(agent)
        self.assertTrue(agent.get('is_active'))
        self.assertEqual(agent.get('commission_vnd'), 200000)
        self.assertEqual(agent.get('payout_delay_days'), 14)

        agent_resp = self.client.get('/agent/app', base_url=self.base_url)
        self.assertEqual(agent_resp.status_code, 302)
        self.assertIn('/agent/', agent_resp.headers.get('Location', ''))

    def test_public_visit_stats_and_tracking_tags(self):
        os.environ['GOOGLE_ANALYTICS_ID'] = 'G-TEST12345'
        os.environ['YANDEX_METRIKA_ID'] = '12345678'

        home_resp = self.client.get('/', base_url=self.base_url, headers={
            'User-Agent': 'Mozilla/5.0 Test Browser',
            'Referer': 'https://t.me/nanny_nya_trang',
        })
        self.assertEqual(home_resp.status_code, 200, home_resp.get_data(as_text=True))
        home_html = home_resp.get_data(as_text=True)
        self.assertIn('googletagmanager.com/gtag/js?id=G-TEST12345', home_html)
        self.assertIn('mc.yandex.ru/metrika/tag.js', home_html)

        tariffs_resp = self.client.get('/tariffs', base_url=self.base_url, headers={'User-Agent': 'Mozilla/5.0 Test Browser'})
        self.assertEqual(tariffs_resp.status_code, 200, tariffs_resp.get_data(as_text=True))

        admin_resp = self.client.get('/admin', headers=self.admin_headers, base_url=self.base_url)
        self.assertEqual(admin_resp.status_code, 200, admin_resp.get_data(as_text=True))
        admin_html = admin_resp.get_data(as_text=True)
        self.assertIn('SEO и посещения сайта', admin_html)
        self.assertIn('G-TEST12345', admin_html)
        self.assertIn('12345678', admin_html)

        visits = self.read_json('visit_log.json', [])
        self.assertGreaterEqual(len(visits), 2)
        self.assertTrue(any(v.get('path') == '/' for v in visits))
        self.assertTrue(all('remote_addr' not in v for v in visits))


if __name__ == '__main__':
    unittest.main()
