import unittest
from types import SimpleNamespace
from unittest.mock import patch
from timetrace.model_catalog import read_catalog, ModelCatalogCache
from timetrace.billing import StaticBilling

class CatalogTest(unittest.TestCase):
    def adapter(self, name='codex'):
        return SimpleNamespace(name=name,cfg={'bin':name},billing=StaticBilling(True))

    @patch('timetrace.model_catalog.identity',return_value=('account','current'))
    def test_codex_pages_hidden_duplicates_and_names(self,_):
        with patch('timetrace.model_catalog.app_server_request',side_effect=[
            {'data':[{'id':'one','displayName':'Model One'},{'id':'hidden','hidden':True}], 'nextCursor':'next'},
            {'data':[{'id':'one'},{'id':'two'},{'id':'a;cmd'}]}]) as rpc:
            c=read_catalog(self.adapter())
        self.assertEqual(c['status'],'ready')
        self.assertEqual([m['id'] for m in c['models']],['one','two'])
        self.assertEqual(c['models'][0]['name'],'Model One')
        self.assertEqual(rpc.call_args.args[2]['cursor'],'next')

    @patch('timetrace.model_catalog.identity',return_value=('account','current'))
    def test_claude_uses_authenticated_initialize_not_help(self,_):
        with patch('timetrace.model_catalog.claude_models',return_value=[{'value':'sonnet','displayName':'Sonnet','resolvedModel':'claude-sonnet-5-5'}]):
            c=read_catalog(self.adapter('claude'))
        self.assertEqual(c['status'],'ready')
        self.assertEqual(c['models'][0]['resolved_model'],'claude-sonnet-5-5')
        self.assertEqual(c['source'],'claude_initialize')

    def test_login_change_during_query_clears_results(self):
        with patch('timetrace.model_catalog.identity',side_effect=[('old',''),('new','')]),patch('timetrace.model_catalog.app_server_request',return_value={'data':[{'id':'one'}]}):
            c=read_catalog(self.adapter())
        self.assertEqual((c['status'],c['models']),('error',[]))

    def test_error_does_not_leak_provider_output_or_keep_old_models(self):
        with patch('timetrace.model_catalog.identity',return_value=('account','')),patch('timetrace.model_catalog.app_server_request',side_effect=ValueError('secret')):
            c=read_catalog(self.adapter())
        self.assertEqual(c['status'],'error');self.assertNotIn('secret',str(c));self.assertEqual(c['models'],[])
        with patch('timetrace.model_catalog.identity',return_value=('','')):
            self.assertEqual(read_catalog(self.adapter())['status'],'login_required')

    def test_cache_invalidated_on_account_change_and_failed_reads_retry(self):
        cache=ModelCatalogCache();a=self.adapter()
        with patch('timetrace.model_catalog.identity',return_value=('a','')) as account,patch('timetrace.model_catalog.read_catalog',return_value={'status':'ready','models':[{'id':'one'}]}) as read,patch('timetrace.model_catalog.time.monotonic',return_value=0) as clock:
            c=cache.get(a);c['models'].clear()
            self.assertEqual(len(cache.get(a)['models']),1);self.assertEqual(read.call_count,1)
            account.return_value=('b','');read.return_value={'status':'error','models':[]}
            self.assertEqual(cache.get(a)['status'],'error');self.assertEqual(read.call_count,2)
            clock.return_value=31;cache.get(a);self.assertEqual(read.call_count,3)

    @patch('timetrace.model_catalog.identity',return_value=('retained-profile',''))
    def test_logged_out_profile_does_not_publish_default_models(self,_):
        a=self.adapter('claude');a.billing=StaticBilling(False,'not_logged_in')
        with patch('timetrace.model_catalog.claude_models') as query:
            c=read_catalog(a)
        query.assert_not_called()
        self.assertEqual(c['status'],'login_required');self.assertEqual(c['models'],[])
