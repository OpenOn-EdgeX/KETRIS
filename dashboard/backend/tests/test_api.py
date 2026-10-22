from pathlib import Path
import tempfile
import unittest
import time
from unittest.mock import patch
from fastapi.testclient import TestClient
from ketris_dashboard.api import public_app
from ketris_dashboard.ingest import ingest_app
from ketris_dashboard.store import ExperimentStore

class ApiTests(unittest.TestCase):
    def test_history_at_never_selects_a_snapshot_even_microseconds_in_the_future(self):
        s={'schema_version':1,'node':'n','mode':'live','captured_at':'2026-10-13T00:00:10.000400Z'}
        self.s.accept('microsecond',0,s)
        params={'at':'2026-10-13T00:00:10.000000Z','recent':True,'limit':1}
        self.assertEqual(self.public.get('/api/v1/nodes/n/history',params=params).json(),[])
        params['at']='2026-10-13T09:00:10.000400+09:00'
        self.assertEqual(len(self.public.get('/api/v1/nodes/n/history',params=params).json()),1)

    def test_history_at_returns_preceding_whole_snapshot_and_validates_timezone(self):
        for i in range(3):
            s={'schema_version':1,'node':'n','mode':'live','captured_at':f'2026-10-13T00:00:{i*10:02d}Z'}
            with patch('ketris_dashboard.store.time.time',return_value=time.time()+i*6):self.s.accept('test-at',i,s)
        rows=self.public.get('/api/v1/nodes/n/history',params={'at':'2026-10-13T09:00:15+09:00','recent':True,'limit':1}).json()
        self.assertEqual([r['id'] for r in rows],[2])
        self.assertEqual(rows[0]['snapshot']['captured_at'],'2026-10-13T00:00:10Z')
        self.assertEqual(self.public.get('/api/v1/nodes/n/history',params={'at':'2026-10-12T00:00:00Z','recent':True,'limit':1}).json(),[])
        for invalid in ['invalid','2026-10-13T00:00:15']:
            self.assertEqual(self.public.get('/api/v1/nodes/n/history',params={'at':invalid}).status_code,422)

    def test_recent_history_returns_latest_bounded_records_in_time_order(self):
        for i in range(5):
            snapshot={'schema_version':1,'node':'n','mode':'live','captured_at':'2026-10-13T00:00:'+str(i*6).zfill(2)+'Z'}
            with patch('ketris_dashboard.store.time.time',return_value=time.time()+i*6):
                self.s.accept('test-session',i,snapshot)
        oldest=self.public.get('/api/v1/nodes/n/history?limit=2').json()
        newest=self.public.get('/api/v1/nodes/n/history?limit=2&recent=true').json()
        self.assertEqual([r['id'] for r in oldest],[1,2])
        self.assertEqual([r['id'] for r in newest],[4,5])
        self.assertEqual(self.public.get('/api/v1/nodes/n/history?recent=true&limit=501').status_code,422)
        self.assertEqual(self.public.get('/api/v1/nodes/other/history?recent=true').json(),[])
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.s=ExperimentStore(Path(self.t.name)/'db');self.addCleanup(self.s.close)
        self.public=TestClient(public_app(self.s));self.private=TestClient(ingest_app(self.s,'test-token'))
        self.payload={'session':'s1','sequence':1,'snapshot':{'schema_version':1,'node':'n','mode':'live','captured_at':'2026-10-13T00:00:00Z'}}
    def test_listener_boundary_token_and_sequence(self):
        self.assertEqual(self.public.post('/internal/v1/snapshots',json={}).status_code,404)
        for headers in [{},{'Authorization':'Bearer wrong'}]:
            self.assertEqual(self.private.post('/internal/v1/snapshots',json=self.payload,headers=headers).status_code,401)
        headers={'Authorization':'Bearer test-token'}
        self.assertEqual(self.private.post('/internal/v1/snapshots',json=self.payload,headers=headers).status_code,202)
        self.assertEqual(self.private.post('/internal/v1/snapshots',json=self.payload,headers=headers).status_code,409)
        self.assertEqual(self.public.get('/api/v1/nodes/n/snapshot').json()['mode'],'live')
        self.assertEqual(self.private.get('/api/v1/nodes').status_code,404)
    def test_bounds_and_invalid_schema(self):
        headers={'Authorization':'Bearer test-token'}
        self.assertEqual(self.private.post('/internal/v1/snapshots',content=b' '*1048577,headers=headers).status_code,413)
        self.payload['snapshot']['env']={'SECRET':'hidden'}
        self.assertEqual(self.private.post('/internal/v1/snapshots',json=self.payload,headers=headers).status_code,422)
        self.assertEqual(self.public.get('/api/v1/nodes/n/history?limit=501').status_code,422)
        self.assertEqual(self.public.get('/api/v1/evidence/%2e%2e%2fetc%2fpasswd').status_code,404)
