"""One editable research slide built only after actual evidence verification."""
from pathlib import Path
import argparse,json,statistics
from pptx import Presentation
from pptx.util import Inches,Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN

def build_slide(root:Path,out:Path)->Path:
 from ketris_integration.result_validation import verify_result
 result=verify_result(root)
 if not result.passed:raise ValueError('cannot present an unverified run: '+str(result.errors))
 from capture_logs import render
 out.mkdir(parents=True,exist_ok=True);load=lambda n:json.loads((root/n).read_text());a=load('state-a-final.json');b=load('state-b-final.json');target=load('target.json');run=load('run-info.json')['run_id'];mean=statistics.mean(x['a_share_percent'] for x in result.measurements);error=max(x['error_pp'] for x in result.measurements)
 colors={'navy':'004366','teal':'0085B3','yellow':'FFF300','bg':'F6F8FA','ink':'183449','muted':'586B79','line':'D5E2EA','pink':'C83F86','green':'287A65','light':'EAF4F9','console':'102C3B','white':'FFFFFF'}
 rgb=lambda c:RGBColor.from_string(colors.get(c,c));prs=Presentation();prs.slide_width=Inches(16);prs.slide_height=Inches(9);s=prs.slides.add_slide(prs.slide_layouts[6]);s.background.fill.solid();s.background.fill.fore_color.rgb=rgb('bg')
 def rect(x,y,w,h,fill='white',stroke=None):
  q=s.shapes.add_shape(MSO_SHAPE.RECTANGLE,Inches(x),Inches(y),Inches(w),Inches(h));q.fill.solid();q.fill.fore_color.rgb=rgb(fill)
  if stroke:q.line.color.rgb=rgb(stroke)
  else:q.line.fill.background()
  return q
 def text(x,y,w,h,value,size=16,color='ink',bold=False,font='NanumGothic'):
  q=s.shapes.add_textbox(Inches(x),Inches(y),Inches(w),Inches(h));tf=q.text_frame;tf.clear();tf.word_wrap=True;tf.margin_left=Inches(.03);tf.margin_top=Inches(.02);tf.margin_bottom=0
  for i,line in enumerate(str(value).split('\n')):
   p=tf.paragraphs[0] if not i else tf.add_paragraph();p.text=line;p.font.name=font;p.font.size=Pt(size);p.font.bold=bold;p.font.color.rgb=rgb(color);p.space_after=Pt(2)
  return q
 rect(0,0,16,.9,'navy');text(.43,.19,15,.55,'기관 gRPC 연계 기반 KETRIS MIG 실행 검증',30,'yellow',True)
 text(.46,1.03,15,.34,'모의 프로파일·파티션 결정 → 실제 통신·승인 → 동일 MIG 내부 공간 상한·시간 크레딧 적용',17,'navy',True)
 flow=[('연세대 모의 클라이언트','PushProfile / GetProfile','SYNTHETIC GPU 메모리 1 GiB'),('성균관대 모의 클라이언트','SubmitPartitionPlan','실제 MIG UUID · 상한50% · weight3/1'),('KETI 승인·계획 일치 검증','기존 승인 + 새 guard','Kueue Admitted → default-scheduler'),('KETRIS 실제 MIG 실행','직접 CUDA GEMM2048 FP32','A/B 실제 UUID · 커널·계상시간 증가')]
 for i,(title,api,desc) in enumerate(flow):
  x=.45+i*3.83;rect(x,1.62,3.60,1.42,'white','line');rect(x,1.62,3.60,.42,'teal' if i%2==0 else 'navy');text(x+.10,1.71,3.4,.25,title,15,'white',True);text(x+.10,2.17,3.4,.27,api,12,'navy',True);text(x+.10,2.57,3.4,.25,desc,11,'muted')
  if i<3:text(x+3.63,2.15,.20,.35,'→',20,'teal',True)
 rect(.45,3.27,7.45,3.16,'white','line');rect(.45,3.27,7.45,.42,'teal');text(.62,3.36,7.1,.25,'입력·적용·실측의 연결',17,'white',True)
 rows=[('기관 입력 출처','SYNTHETIC · 요구량 가정 / 실제 RPC'),('실행 대상',f'devmaster · MIG 1g.24gb · 46 SM'),('공간 적용 A / B','MPS 상한50% /50% · CUDA 보고22 SM씩'),('시간 적용(동시 활성 A/B)','7,500µs /2,500µs · 10ms 주기'),('실측 평균 계상시간 비중',f'A {mean:.4f}% / B {100-mean:.4f}%'),('최대 목표 오차 / 검증 창',f'{error:.4f}%p ·20초 이상 ×3회')]
 for i,(key,value) in enumerate(rows):
  y=3.87+i*.38;text(.62,y,2.65,.27,key,12,'muted');text(3.25,y,4.4,.27,value,12,'ink',i>=4)
 rect(8.14,3.27,7.41,3.16,'console');text(8.31,3.39,7.0,.28,'실제 기록 조회 · 필드 요약',15,'yellow',True)
 console=render(root,'guard').splitlines()+render(root,'gpu').splitlines()[1:]
 selected=[console[1],console[2]]+[line for line in console if line.startswith('[A] COMPLETED') or line.startswith('[B] COMPLETED') or line.startswith('[WINDOW')]
 compact=[line.replace('[GUARD] operator Workload weight drift: ','[GUARD] ').replace('KETI=Ready / plan guard=Rejected','KETI Ready / guard Rejected') for line in selected]
 sh=text(8.31,3.93,7.05,2.2,'\n'.join(compact),12,'white',font='NanumGothicCoding')
 for p in sh.text_frame.paragraphs:p.space_after=Pt(2);p.line_spacing=Pt(17)
 rect(.45,6.67,15.1,.78,'light','line');text(.63,6.82,14.7,.27,'검증 결과  |  실제 기관 Pod 4개 · 두 승인 체크 통과 · A/B 정상 종료 · 재시작 후 동일 receipt/Job UID',16,'green',True)
 text(.48,7.67,15,.26,'캡처  |  python3 capture_logs.py <증거폴더> --section all   /   원본 kubectl logs 명령은 첨부 TXT·발표자 노트 제공',13,'navy',True)
 text(.48,8.13,15,.25,'해석  |  기관 알고리즘·공식 .proto 검증은 제외 · 첫 실행은 GEMM(LLM/NPU 새 실행 아님) · MPS 상한은 전용 SM 격리가 아님',11,'muted')
 text(.48,8.52,15,.22,'출처  |  '+run+' · 실제 Protobuf·UID·runtime generation·원본 SHA-256 연결 · 통합 VM10.0.1.211',10,'muted')
 commands='\n'.join(['kubectl -n ketris-integration logs job/yonsei-'+run+'-a','kubectl -n ketris-integration logs job/skku-'+run+'-a','kubectl -n ketris-integration-workloads logs job/'+a['job_name'],'kubectl -n ketris-integration-workloads logs job/'+b['job_name'],'python3 capture_logs.py <evidence-dir> --section all','python3 capture_logs.py <evidence-dir> --raw'])
 notes='Recorded actual execution; synthetic institution demand and test policy. This is not an official institution algorithm/protocol. The actual guard rejection is operator annotation drift before quota reservation. Full console below is a derived view of SHA-verified original fields, not a newly executed runtime log.\n'+commands+'\n\n'+render(root,'all')+'\nTarget:\n'+json.dumps(target,indent=2)+'\nLimitations:\n'+'\n'.join(result.limits)
 s.notes_slide.notes_text_frame.text=notes;prs.core_properties.title='기관 gRPC 연계 기반 KETRIS MIG 실행 검증';prs.core_properties.author='KETRIS';path=out/'KETRIS_기관_gRPC_연계_실행검증_1장.pptx';prs.save(path);assert len(prs.slides)==1
 (out/'캡처_명령어.txt').write_text(commands+'\n\n'+str(root)+'\n');(out/'console-all.txt').write_text(render(root,'all'));return path

def main():
 p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('out',type=Path);a=p.parse_args();print(build_slide(a.root,a.out))
if __name__=='__main__':main()
