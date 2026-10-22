import unittest,json,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
from kubernetes import client
from ketris_integration.kube import KubeAdapter
class Transport(unittest.TestCase):
 def test_sdk_http_boundary_preserves_own_named_check(self):
  records=[]
  class Handler(BaseHTTPRequestHandler):
   def do_GET(self):
    self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps({'status':{'conditions':[{'type':'Active','status':'True' if records else 'False'}]}}).encode())
   def do_PATCH(self):
    records.append((self.path,self.headers['Content-Type'],json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
    self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"status":{"conditions":[{"type":"Active","status":"True"}]}}')
   def log_message(self,*a):pass
  server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
  cfg=client.Configuration();cfg.host='http://127.0.0.1:'+str(server.server_port)
  with client.ApiClient(cfg) as api:
   adapter=KubeAdapter(api);result=adapter.activate_check();adapter.activate_check()
  self.assertEqual(len(records),1)  # A stable Active condition must not be rewritten every poll.
  self.assertEqual(result['status']['conditions'][0]['status'],'True');self.assertIn('/admissionchecks/ketris-plan-validation/status?',records[0][0]);self.assertEqual(records[0][1],'application/apply-patch+yaml');self.assertEqual(records[0][2]['metadata']['name'],'ketris-plan-validation')
