"""One SQLite owner thread for both listeners. History samples at most every 5s."""
from concurrent.futures import Future
from datetime import datetime,timezone
import json,queue,re,sqlite3,threading,time
from .models import ViewSnapshotV1,ReplayFrame

def encode(value):return json.dumps(value,ensure_ascii=False,allow_nan=False)
def timestamp_us(value):
    delta=datetime.fromisoformat(value.replace('Z','+00:00'))-datetime(1970,1,1,tzinfo=timezone.utc)
    return (delta.days*86400+delta.seconds)*1000000+delta.microseconds

class ExperimentStore:
    def __init__(self,path):
        self.path=str(path);self.jobs=queue.Queue();self.closed=False;ready=Future()
        def worker():
            try:
                self.db=sqlite3.connect(self.path);self.db.row_factory=sqlite3.Row
                self.db.create_function('snapshot_time_us',1,timestamp_us,deterministic=True)
                self.db.executescript('''PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY, metadata TEXT, summary TEXT, manifest TEXT);
                    CREATE TABLE IF NOT EXISTS frames(experiment TEXT, idx INTEGER, json TEXT, PRIMARY KEY(experiment,idx));
                    CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, json TEXT);
                    CREATE TABLE IF NOT EXISTS heads(node TEXT PRIMARY KEY, session TEXT, seq INTEGER, captured REAL, received REAL, json TEXT);
                    CREATE TABLE IF NOT EXISTS sessions(node TEXT, session TEXT, retired INTEGER, PRIMARY KEY(node,session));
                    CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, node TEXT, received REAL, json TEXT);
                    CREATE INDEX IF NOT EXISTS history_node ON history(node,id);
                ''');self.db.commit();ready.set_result(None)
            except Exception as e:ready.set_exception(e);return
            while True:
                job=self.jobs.get()
                if job is None:break
                fn,future=job
                try:future.set_result(fn())
                except Exception as e:self.db.rollback();future.set_exception(e)
            self.db.close()
        self.thread=threading.Thread(target=worker,daemon=True);self.thread.start();ready.result(timeout=10)
    def _call(self,fn):
        if self.closed:raise RuntimeError('store closed')
        f=Future();self.jobs.put((fn,f));return f.result(timeout=30)
    def close(self):
        if not self.closed:self.closed=True;self.jobs.put(None);self.thread.join(timeout=10)
    def import_bundle(self,bundle):
        b=bundle.to_dict()
        def write():
            self.db.execute('INSERT OR IGNORE INTO experiments VALUES(?,?,?,?)',(b['experiment_id'],encode(b['metadata']),encode(b['summary']),encode(b['source_manifest'])))
            self.db.executemany('INSERT OR IGNORE INTO frames VALUES(?,?,?)',[(b['experiment_id'],f['index'],encode(f)) for f in b['frames']])
            self.db.executemany('INSERT OR IGNORE INTO evidence VALUES(?,?)',[(k,encode(v)) for k,v in b['evidence'].items()])
            self.db.commit();return b['experiment_id']
        return self._call(write)
    def experiments(self):
        return self._call(lambda:[{'id':r['id'],**json.loads(r['metadata'])} for r in self.db.execute('SELECT * FROM experiments ORDER BY id')])
    def experiment(self,id):
        def read():
            r=self.db.execute('SELECT * FROM experiments WHERE id=?',(id,)).fetchone()
            return {'id':id,'metadata':json.loads(r['metadata']),'summary':json.loads(r['summary']),'source_manifest':json.loads(r['manifest']),
                    'frame_count':self.db.execute('SELECT count(*) FROM frames WHERE experiment=?',(id,)).fetchone()[0]} if r else None
        return self._call(read)
    @staticmethod
    def page(cursor,limit):
        if type(cursor) is not int or cursor<0 or type(limit) is not int or not 1<=limit<=500:raise ValueError('invalid page')
    def load_frames(self,id,cursor=0,limit=500):
        self.page(cursor,limit)
        return self._call(lambda:[json.loads(r[0]) for r in self.db.execute('SELECT json FROM frames WHERE experiment=? AND idx>=? ORDER BY idx LIMIT ?',(id,cursor,limit))])
    def evidence(self,id):
        if not re.fullmatch(r'e-[0-9a-f]{24}',id):raise ValueError('invalid evidence ID')
        def read():
            r=self.db.execute('SELECT json FROM evidence WHERE id=?',(id,)).fetchone();return json.loads(r[0]) if r else None
        return self._call(read)
    def accept(self,session,sequence,snapshot):
        snapshot=ViewSnapshotV1.model_validate(snapshot)
        if snapshot.mode!='live' or not session or len(session)>128 or type(sequence) is not int or sequence<0:raise ValueError('invalid observer envelope')
        captured=datetime.fromisoformat(snapshot.captured_at.replace('Z','+00:00')).timestamp();now=time.time();node=snapshot.node
        def write():
            old=self.db.execute('SELECT * FROM heads WHERE node=?',(node,)).fetchone()
            status=self.db.execute('SELECT retired FROM sessions WHERE node=? AND session=?',(node,session)).fetchone()
            if status and status[0]:raise ValueError('retired observer session')
            if old:
                if old['session']==session and sequence<=old['seq']:raise ValueError('out-of-order observer sequence')
                if captured<old['captured']:raise ValueError('older snapshot')
                if old['session']!=session:self.db.execute('UPDATE sessions SET retired=1 WHERE node=? AND session=?',(node,old['session']))
            data=encode(snapshot.model_dump())
            self.db.execute('INSERT OR IGNORE INTO sessions VALUES(?,?,0)',(node,session))
            # Sequence/session head is durable on every ingest; expensive history is 5s sampled.
            self.db.execute('INSERT OR REPLACE INTO heads VALUES(?,?,?,?,?,?)',(node,session,sequence,captured,now,data))
            last=self.db.execute('SELECT max(received) FROM history WHERE node=?',(node,)).fetchone()[0]
            if last is None or now-last>=5:self.db.execute('INSERT INTO history(node,received,json) VALUES(?,?,?)',(node,now,data))
            self.db.execute('DELETE FROM history WHERE received<?',(now-7*86400,))
            self.db.commit();return True
        return self._call(write)
    def nodes(self):
        return self._call(lambda:[{'name':r['node'],'received_at':datetime.fromtimestamp(r['received'],timezone.utc).isoformat(),'connected':time.time()-r['received']<=10}
                                 for r in self.db.execute('SELECT node,received FROM heads ORDER BY node')])
    def snapshot(self,node,stale=False):
        def read():
            r=self.db.execute('SELECT json,received FROM heads WHERE node=?',(node,)).fetchone()
            if not r:return None
            s=json.loads(r[0])
            if stale and time.time()-r[1]>10:
                for status in s['source_status'].values():status.update(status='stale',reason='observer disconnected')
                for entity in s['devices']+s['partitions']+s['workloads']:
                    for group in ['metrics','requested','applied','observed']:
                        for value in entity.get(group,{}).values():
                            if value['value'] is not None:value.update(quality='stale',reason='observer disconnected; last success shown')
            return s
        return self._call(read)
    def history(self,node,cursor=0,limit=500,recent=False,at=None):
        self.page(cursor,limit)
        def read():
            order='DESC' if recent else 'ASC'
            # Integer microseconds preserve exact ordering across timezone offsets.
            before=" AND snapshot_time_us(json_extract(json,'$.captured_at'))<=?" if at is not None else ''
            params=(node,cursor,timestamp_us(at),limit) if at is not None else (node,cursor,limit)
            rows=list(self.db.execute('SELECT id,json FROM history WHERE node=? AND id>?'+before+' ORDER BY id '+order+' LIMIT ?',params))
            if recent:rows.reverse()
            return [{'id':r['id'],'snapshot':json.loads(r['json'])} for r in rows]
        return self._call(read)
