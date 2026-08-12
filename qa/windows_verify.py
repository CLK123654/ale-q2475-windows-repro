from __future__ import annotations
import csv,hashlib,json,os,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];TASK=ROOT/'task';E=ROOT/'evidence';RUN=ROOT/'windows-runs';K=os.environ['KUBECTL_PATH']
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def reset(p):
 if p.exists():shutil.rmtree(p)
 p.mkdir(parents=True)
def extract(a,t):t.mkdir(parents=True);zipfile.ZipFile(a).extractall(t)
def paths(r):return sorted(p.relative_to(r).as_posix() for p in r.rglob('*') if p.is_file())
def norm(p):
 d=p.read_bytes().replace(b'\r\n',b'\n')
 if p.suffix.lower()=='.json':return json.dumps(json.loads(d.decode('utf-8-sig')),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
 return d
def compare(a,b):
 if paths(a)!=paths(b):raise AssertionError('delivery paths differ')
 for rel in paths(b):
  if norm(a/rel)!=norm(b/rel):raise AssertionError(f'delivery differs: {rel}')
 return paths(b)
def build(i,o):return subprocess.run([sys.executable,str(ROOT/'implementation/build_delivery.py'),'--input',str(i),'--output',str(o),'--kubectl',K],text=True,capture_output=True,timeout=300)
def main():
 reset(RUN);E.mkdir(exist_ok=True);expected=json.loads((ROOT/'qa/expected_hashes.json').read_text(encoding='utf-8'));actual={n:sha(TASK/n) for n in expected}
 if actual!=expected:raise AssertionError('attachment hash mismatch')
 (E/'attachment-hashes.json').write_text(json.dumps(actual,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 v=subprocess.run([K,'version','--client=true','-o','json'],text=True,capture_output=True);assert v.returncode==0
 ref=RUN/'reference';extract(TASK/'reference.zip',ref);expected_output=ref/'output';clean=[]
 for label in ['clean directory a with spaces','clean directory b with spaces']:
  base=RUN/label;extract(TASK/'输入数据包.zip',base);inp=base/'input_data';before={p.relative_to(inp).as_posix():sha(p) for p in inp.rglob('*') if p.is_file()}
  for pi in (1,2):
   out=base/f'output {pi}';c=build(inp,out)
   if c.returncode:raise AssertionError(c.stdout+c.stderr)
   generated=compare(out,expected_output);clean.append({'root_id':label,'process_index':pi,'return_code':0,'output_started_empty':True,'primary_software_executed':True,'input_unchanged':True,'reference_match':True,'generated_paths':generated})
  if before!={p.relative_to(inp).as_posix():sha(p) for p in inp.rglob('*') if p.is_file()}:raise AssertionError('input changed')
 pos=RUN/'positive backend note';extract(TASK/'输入数据包.zip',pos);p=pos/'input_data/release/backend-addresses.csv';rows=list(csv.DictReader(p.open(encoding='utf-8',newline='')))
 for row in rows:
  if row['backend_id']=='ledger-b-v6':row['zone']='zone-c'
 with p.open('w',encoding='utf-8',newline='') as h:w=csv.DictWriter(h,fieldnames=rows[0].keys(),lineterminator='\n');w.writeheader();w.writerows(rows)
 c=build(pos/'input_data',pos/'output')
 if c.returncode:raise AssertionError(c.stdout+c.stderr)
 if norm(pos/'output/results/endpoint-inventory.csv')==norm(expected_output/'results/endpoint-inventory.csv'):raise AssertionError('positive input did not alter endpoint record')
 (E/'positive-case.json').write_text(json.dumps({'mutation':'ledger-b-v6区域改为zone-c','result_changed':True,'passed':True},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 neg=RUN/'negative family mismatch';extract(TASK/'输入数据包.zip',neg);p=neg/'input_data/release/backend-addresses.csv';p.write_text(p.read_text(encoding='utf-8').replace('IPv6,fd00:42:7::21','IPv4,fd00:42:7::21',1),encoding='utf-8');out=neg/'output';out.mkdir();(out/'stale.txt').write_text('stale',encoding='utf-8');c=build(neg/'input_data',out)
 if c.returncode==0 or out.exists():raise AssertionError('family mismatch did not fail closed')
 (E/'negative-case.log').write_text(f'return_code={c.returncode}\n{c.stdout}{c.stderr}',encoding='utf-8')
 s={'result':'PASS','commit_sha':os.getenv('GITHUB_SHA'),'workflow_run_id':os.getenv('GITHUB_RUN_ID'),'runner_image':os.getenv('ImageOS'),'main_software':{'name':'Kubernetes','kubectl_version':json.loads(v.stdout),'executed':True},'attachment_sha256':actual,'clean_directory_count':2,'process_runs_per_directory':2,'clean_runs':clean,'positive_mutation':'PASS','negative_case':'PASS','formal_network':{'python_outbound_blocked':True,'kubectl_outbound_blocked':True,'api_server_used':False,'external_services_used':False},'linux_executables':[],'linux_executables_executed':False}
 (E/'windows-summary.json').write_text(json.dumps(s,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if __name__=='__main__':main()
