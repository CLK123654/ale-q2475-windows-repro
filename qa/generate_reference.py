from __future__ import annotations
import json,os,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];E=ROOT/'evidence';W=ROOT/'work-reference'
if W.exists():shutil.rmtree(W)
W.mkdir();E.mkdir(exist_ok=True)
with zipfile.ZipFile(ROOT/'task/输入数据包.zip') as z:z.extractall(W)
c=subprocess.run([sys.executable,str(ROOT/'implementation/build_delivery.py'),'--input',str(W/'input_data'),'--output',str(W/'output'),'--kubectl',os.environ['KUBECTL_PATH']],text=True,capture_output=True,timeout=300)
if c.returncode:raise SystemExit(c.stdout+c.stderr)
with zipfile.ZipFile(E/'reference-candidate.zip','w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
 for p in sorted((W/'output').rglob('*')):
  if p.is_file():z.write(p,p.relative_to(W).as_posix())
(E/'reference-generation.json').write_text(json.dumps({'result':'PASS','commit_sha':os.getenv('GITHUB_SHA'),'workflow_run_id':os.getenv('GITHUB_RUN_ID'),'reference_members':sorted(p.relative_to(W).as_posix() for p in (W/'output').rglob('*') if p.is_file())},indent=2)+'\n',encoding='utf-8')
